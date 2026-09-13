"""Validated metadata and DST configuration generation. No user-supplied Lua is executed."""
import configparser
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import uuid


class PanelError(Exception):
    def __init__(self, message, status=400, *, params=None):
        self.i18n = {"key": message, "params": params or {}}
        super().__init__(message.format(**params) if params else message)
        self.status = status


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            os.chmod(temporary, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def lua(value, depth=0):
    if depth > 12:
        raise PanelError("Options are nested too deeply.")
    if value is None:
        raise PanelError("Lua options cannot contain null. Remove the option to use its default.")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise PanelError("Options must contain finite numbers.")
        return str(value)
    if isinstance(value, str):
        # Decimal byte escapes work in Lua 5.1, including controls and UTF-8.
        return '"' + "".join(chr(b) if 32 <= b < 127 and b not in (34, 92)
                              else f"\\{b:03d}" for b in value.encode("utf-8")) + '"'
    if isinstance(value, list):
        return "{" + ",".join(lua(item, depth + 1) for item in value) + "}"
    if isinstance(value, dict):
        return "{" + ",".join(f"[{lua(str(k), depth + 1)}]={lua(v, depth + 1)}"
                              for k, v in value.items()) + "}"
    raise PanelError("Unsupported option type.")


def line(value, name, maximum=200, required=False):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise PanelError("{name} must be a single line, at most {maximum} characters.",
                         params={"name": name, "maximum": maximum})
    if required and not value.strip():
        raise PanelError("{name} is required.", params={"name": name})
    return value.strip()


def validate_world(data, previous=None):
    if not isinstance(data, dict):
        raise PanelError("Expected a JSON object.")
    old = previous or {}
    result = {}
    for key, default, limit in [("name", "", 80), ("description", "", 300),
                                 ("password", "", 64), ("token", "", 2048)]:
        value = data.get(key, old.get(key, default))
        if key == "token" and not value and old.get("token"):
            value = old["token"]
        result[key] = line(value, key, limit, required=key == "name")
    result["game_mode"] = data.get("game_mode", old.get("game_mode", "survival"))
    if result["game_mode"] not in ("survival", "endless", "wilderness"):
        raise PanelError("Choose survival, endless, or wilderness.")
    for key, default in [("max_players", 6), ("snapshots", 10)]:
        value = data.get(key, old.get(key, default))
        if type(value) is not int or not 1 <= value <= (64 if key == "max_players" else 50):
            raise PanelError("Invalid {key}.", params={"key": key})
        result[key] = value
    for key, default in [("caves", True), ("pvp", False), ("pause_when_empty", True),
                         ("autostart", False)]:
        result[key] = data.get(key, old.get(key, default))
        if type(result[key]) is not bool:
            raise PanelError("{key} must be true or false.", params={"key": key})
    for key in ("master_overrides", "caves_overrides"):
        result[key] = data.get(key, old.get(key, {}))
        if not isinstance(result[key], dict) or len(json.dumps(result[key])) > 20000:
            raise PanelError("World overrides must be a JSON object under 20 KB.")
        lua(result[key])
    for key in ("admins", "banned", "whitelist"):
        ids = data.get(key, old.get(key, []))
        if not isinstance(ids, list) or len(ids) > 500 or any(
            not isinstance(item, str) or not re.fullmatch(r"KU_[A-Za-z0-9_-]{4,64}", item) for item in ids
        ):
            raise PanelError("{key} must contain Klei IDs such as KU_abcdefgh.", params={"key": key})
        result[key] = list(dict.fromkeys(ids))
    result["mods"] = validate_mods(data.get("mods", old.get("mods", [])))
    result["id"] = old.get("id", uuid.uuid4().hex[:12])
    result["cluster_key"] = old.get("cluster_key", secrets.token_hex(24))
    shard_ids = data.get("shard_ids", old.get("shard_ids", {"Master": "1", "Caves": "2"}))
    if not isinstance(shard_ids, dict) or set(shard_ids) != {"Master", "Caves"} or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value) for value in shard_ids.values()
    ) or len(set(shard_ids.values())) != 2:
        raise PanelError("Shard IDs must be two distinct numeric strings for Master and Caves.")
    result["shard_ids"] = shard_ids
    return result


def validate_mods(mods):
    if not isinstance(mods, list) or len(mods) > 100:
        raise PanelError("Use at most 100 mods.")
    result, seen = [], set()
    for mod in mods:
        if not isinstance(mod, dict):
            raise PanelError("Each mod must be an object.")
        identifier = str(mod.get("id", ""))
        if not re.fullmatch(r"[1-9][0-9]{4,19}", identifier) or identifier in seen:
            raise PanelError("Workshop IDs must be unique numbers (5–20 digits).")
        options = mod.get("options", {})
        enabled = mod.get("enabled", True)
        if not isinstance(options, dict) or len(json.dumps(options)) > 20000 or type(enabled) is not bool:
            raise PanelError("Mod options must be a JSON object under 20 KB; enabled must be boolean.")
        lua(options)
        result.append({"id": identifier, "enabled": enabled, "options": options})
        seen.add(identifier)
    return result


def ini(sections):
    parser = configparser.ConfigParser(interpolation=None)
    for section, values in sections.items():
        parser[section] = {key: str(value).lower() if isinstance(value, bool) else str(value)
                           for key, value in values.items()}
    stream = io.StringIO()
    parser.write(stream)
    return stream.getvalue()


class Store:
    def __init__(self, root, game):
        self.root, self.game = Path(root).resolve(), Path(game).resolve()
        self.clusters = self.root / "clusters"
        self.backups = self.root / "backups"
        self.logs = self.root / "logs"
        for path in (self.clusters, self.backups, self.logs):
            path.mkdir(parents=True, exist_ok=True)

    def world_path(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{12}", identifier):
            raise PanelError("Invalid world ID.", 404)
        path = self.clusters / identifier
        if path.is_symlink():
            raise PanelError("Linked world directories are not supported.")
        return path

    def get(self, identifier):
        path = self.world_path(identifier) / "pera.json"
        if not path.is_file():
            raise PanelError("World not found.", 404)
        return json.loads(path.read_text(encoding="utf-8"))

    def all(self):
        return [self.get(path.parent.name) for path in sorted(self.clusters.glob("*/pera.json"))
                if re.fullmatch(r"[a-f0-9]{12}", path.parent.name)]

    def write_world(self, world):
        root = self.world_path(world["id"])
        root.mkdir(parents=True, exist_ok=True)
        atomic_write(root / "cluster.ini", ini({
            "GAMEPLAY": {"game_mode": world["game_mode"], "max_players": world["max_players"],
                         "pvp": world["pvp"], "pause_when_empty": world["pause_when_empty"]},
            "NETWORK": {"cluster_name": world["name"], "cluster_description": world["description"],
                        "cluster_password": world["password"], "cluster_intention": "cooperative"},
            "MISC": {"console_enabled": True, "max_snapshots": world["snapshots"]},
            "SHARD": {"shard_enabled": world["caves"], "bind_ip": "127.0.0.1",
                      "master_ip": "127.0.0.1", "master_port": 10888, "cluster_key": world["cluster_key"]},
        }))
        atomic_write(root / "cluster_token.txt", world["token"] + "\n")
        for key, file in [("admins", "adminlist.txt"), ("banned", "blocklist.txt"),
                          ("whitelist", "whitelist.txt")]:
            atomic_write(root / file, "\n".join(world[key]) + "\n")
        mods = {f"workshop-{mod['id']}": {"enabled": mod["enabled"], "configuration_options": mod["options"]}
                for mod in world["mods"]}
        for index, shard in enumerate(("Master", "Caves")):
            if shard == "Caves" and not world["caves"]:
                continue
            atomic_write(root / shard / "server.ini", ini({
                "NETWORK": {"server_port": 10999 + index},
                "SHARD": {"is_master": index == 0, "name": shard,
                          "id": world.get("shard_ids", {}).get(shard, str(index + 1))},
                "STEAM": {"authentication_port": 8766 + index, "master_server_port": 27016 + index},
            }))
            atomic_write(root / shard / "modoverrides.lua", "return " + lua(mods) + "\n")
            overrides = world["master_overrides" if index == 0 else "caves_overrides"]
            atomic_write(root / shard / "worldgenoverride.lua", "return " + lua({
                "override_enabled": True, "preset": "SURVIVAL_TOGETHER" if index == 0 else "DST_CAVE",
                "overrides": overrides,
            }) + "\n")
        write_json(root / "pera.json", world)

    def write_mod_setup(self, world):
        atomic_write(self.game / "mods" / "dedicated_server_mods_setup.lua",
                     "-- Managed by Pera Panel; regenerated before each start.\n" + "".join(
                         f'ServerModSetup("{mod["id"]}")\n' for mod in world["mods"] if mod["enabled"]))

    @staticmethod
    def public(world):
        return {**{k: v for k, v in world.items() if k not in ("token", "cluster_key")},
                "has_token": bool(world["token"])}
