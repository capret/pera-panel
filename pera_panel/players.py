"""Bounded player discovery. Save folders are evidence, never account ownership proof."""
from datetime import datetime, timezone
import json
import re
import threading

from .lua_settings import LiteralTable
from .storage import PanelError, write_json

USER_ID = re.compile(r"KU_[A-Za-z0-9_-]{4,64}\Z")
FOLDER = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
SESSION = re.compile(r"[A-Fa-f0-9]{16}\Z")
SNAPSHOT = re.compile(r"[0-9]{10}\Z")
AUTHENTICATED = re.compile(r"^(?:\[[0-9:.]+\]:\s*)?Client authenticated: \((KU_[A-Za-z0-9_-]{4,64})\) (.*)$")
CHARACTER_PATH = re.compile(r"(?:Master|Caves)/save/session/[A-Fa-f0-9]{16}/[A-Za-z0-9_-]{1,80}\Z")
RESUMING = re.compile(r"^(?:\[([0-9:.]+)\]:\s*)?Resuming user: session/([A-Fa-f0-9]{16})/([A-Za-z0-9_-]{1,80})/[0-9]{10}\s*$")
OWNERSHIP = re.compile(r"^(?:\[([0-9:.]+)\]:\s*)?User ID\s+(KU_[A-Za-z0-9_-]{4,64})\s+assigned ownership to entity\s+[0-9]+ - [A-Za-z0-9_]+\s*$")
MAX_PLAYERS = 1000


def clean(value, limit=100):
    return "".join(c for c in value if ord(c) >= 32)[:limit] if isinstance(value, str) else ""


class ResumeLinks:
    """Adjacent game log lines are display hints, never permission grants or recovery targets."""
    def __init__(self):
        self.pending = None
        self.previous_resume = False
        self.authenticated = set()

    def feed(self, output, shard):
        output = output.rstrip("\r\n")
        auth = AUTHENTICATED.fullmatch(output)
        if auth and len(self.authenticated) < MAX_PLAYERS:
            self.authenticated.add(auth[1])
        resumed, owner = RESUMING.fullmatch(output), OWNERSHIP.fullmatch(output)
        pending = self.pending
        self.pending = resumed if resumed and not self.previous_resume else None
        self.previous_resume = bool(resumed)
        if pending and owner and pending[1] and pending[1] == owner[1] and owner[2] in self.authenticated:
            return {"userid": owner[2], "character_path": f"{shard}/save/session/{pending[2]}/{pending[3]}"}
        return None


def players_from_log(outputs, shard):
    players, links, parser = {}, [], ResumeLinks()
    for output in outputs:
        match = AUTHENTICATED.fullmatch(output.rstrip("\r\n"))
        if match and len(players) < MAX_PLAYERS:
            players[match[1]] = clean(match[2])
        link = parser.feed(output, shard)
        if link and len(links) < MAX_PLAYERS:
            links.append(link)
    return players, links


def folder_userid(folder, known_ids=()):
    """A raw save path can append '_'; never trim an authenticated account ID."""
    if not USER_ID.fullmatch(folder):
        return None
    candidates = {folder}
    if folder.endswith("_") and USER_ID.fullmatch(folder[:-1]):
        candidates.add(folder[:-1])
    matched = candidates.intersection(known_ids)
    if len(matched) == 1:
        return matched.pop()
    if len(matched) > 1 or folder.endswith("_"):
        return None  # Ambiguous suffixes need an account hint, not a guessed identity.
    return folder


def characters(root, known_ids=()):
    """Inspect the fixed session directory depth; never follow links or read game payloads."""
    found = []
    budget = 20000
    for shard in ("Master", "Caves"):
        base = root / shard / "save" / "session"
        if not base.is_dir() or any(p.is_symlink() for p in (root / shard, base.parent, base)):
            continue
        for session in base.iterdir():
            budget -= 1
            if budget <= 0:
                return found
            if not SESSION.fullmatch(session.name) or session.is_symlink() or not session.is_dir():
                continue
            for folder in session.iterdir():
                budget -= 1
                if budget <= 0 or len(found) >= MAX_PLAYERS:
                    return found
                if not FOLDER.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                    continue
                latest = None
                for path in folder.iterdir():
                    budget -= 1
                    if budget <= 0:
                        return found
                    if SNAPSHOT.fullmatch(path.name) and not path.is_symlink() and path.is_file():
                        if latest is None or path.name > latest.name:
                            latest = path
                if latest is None:
                    continue
                prefab = ""
                meta = latest.with_suffix(".meta")
                if meta.is_file() and not meta.is_symlink() and meta.stat().st_size <= 8192:
                    try:
                        text = meta.read_text(encoding="utf-8-sig").rstrip("\0")
                        text = re.sub(r"^KLEI\s+1\s*", "", text)
                        prefab = clean(LiteralTable(text).read().get("character"), 60)
                    except (PanelError, UnicodeError, ValueError, RecursionError):
                        pass  # Metadata is optional; compressed/unknown formats remain untouched.
                found.append({"path": folder.relative_to(root).as_posix(), "shard": shard,
                              "session": session.name, "folder": folder.name, "character": prefab,
                              "snapshot": latest.name, "userid": folder_userid(folder.name, known_ids),
                              "offline": folder.name.startswith("OU_")})
    return sorted(found, key=lambda p: p["path"])


class PlayerRegistry:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.log_parsers = {}

    def _read(self, root):
        path = root / "pera-players.json"
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 1024**2:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {k: v for k, v in list(data.items())[:MAX_PLAYERS]
                            if USER_ID.fullmatch(k) and isinstance(v, dict) and v.get("userid") == k
                            and isinstance(v.get("name"), str) and len(v["name"]) <= 100
                            and isinstance(v.get("sources"), list) and len(v["sources"]) <= 6
                            and all(s in ("join", "console", "save", "cached_owner", "imported_log", "permissions")
                                    for s in v["sources"])
                            and isinstance(v.get("folders"), dict)
                            and all(shard in ("Master", "Caves") and isinstance(folder, str) and FOLDER.fullmatch(folder)
                                    for shard, folder in v["folders"].items())
                            and isinstance(v.get("paths", {}), dict) and len(v.get("paths", {})) <= 128
                            and all(CHARACTER_PATH.fullmatch(path) and source in ("join", "imported_log")
                                    for path, source in v.get("paths", {}).items())}
            except (ValueError, UnicodeError):
                pass
        return {}

    def record(self, identifier, userid, *, name="", source="join", folder="", shard="Master"):
        self.record_many(identifier, [{"userid": userid, "name": name, "source": source,
                                       "folder": folder, "shard": shard}])

    def record_many(self, identifier, rows):
        root = self.store.world_path(identifier)
        # Late output from an old process must not recreate an archived world.
        with self.lock:
            if not (root / "pera.json").is_file():
                return
            data = self._read(root)
            before = json.dumps(data, sort_keys=True)
            for row in rows:
                userid = row["userid"]
                if not isinstance(userid, str) or not USER_ID.fullmatch(userid):
                    continue
                if userid not in data and len(data) >= MAX_PLAYERS:
                    continue
                player = data.setdefault(userid, {"userid": userid, "name": "", "sources": [], "folders": {}})
                if row.get("name"):
                    player["name"] = clean(row["name"])
                source = row.get("source", "join")
                if source not in player["sources"]:
                    player["sources"].append(source)
                folder, shard = row.get("folder", ""), row.get("shard", "Master")
                if isinstance(folder, str) and FOLDER.fullmatch(folder) and shard in ("Master", "Caves"):
                    player["folders"][shard] = folder
                character_path = row.get("character_path")
                if isinstance(character_path, str) and CHARACTER_PATH.fullmatch(character_path) and source in ("join", "imported_log"):
                    paths = player.setdefault("paths", {})
                    if character_path in paths or len(paths) < 128:
                        paths[character_path] = source
                if source == "join":
                    player["last_seen"] = datetime.now(timezone.utc).isoformat()
            if json.dumps(data, sort_keys=True) != before:
                write_json(root / "pera-players.json", data)

    def log_line(self, identifier, output, shard="Master"):
        match = AUTHENTICATED.fullmatch(output.rstrip("\r\n"))
        if match:
            self.record(identifier, match[1], name=match[2])
        with self.lock:
            key = (identifier, shard)
            if key not in self.log_parsers:
                if len(self.log_parsers) >= 100:
                    self.log_parsers.pop(next(iter(self.log_parsers)))
                self.log_parsers[key] = ResumeLinks()
            link = self.log_parsers[key].feed(output, shard)
            if link:
                self.record_many(identifier, [{**link, "source": "join"}])

    def discover(self, identifier):
        """Run on import and explicit refresh, not on every status poll."""
        root = self.store.world_path(identifier)
        rows = []
        for shard in ("Master", "Caves"):
            log = root / shard / "server_log.txt"
            if log.is_file() and not log.is_symlink() and not log.parent.is_symlink() and log.stat().st_size <= 2 * 1024**2:
                names, links = players_from_log(log.read_text(encoding="utf-8", errors="replace").splitlines(), shard)
                rows.extend({"userid": userid, "name": name, "source": "imported_log"} for userid, name in names.items())
                rows.extend({**link, "source": "imported_log"} for link in links)
            path = root / shard / "save" / "cached_userid"
            if path.is_file() and not any(p.is_symlink() for p in (root / shard, path.parent, path)) \
                    and path.stat().st_size <= 512:
                try:
                    owner = path.read_text(encoding="utf-8-sig").strip()
                    owner = re.sub(r"^KLEI\s+1\s*", "", owner).strip()
                    rows.append({"userid": owner, "source": "cached_owner"})
                except UnicodeError:
                    pass
        with self.lock:
            self.record_many(identifier, rows)
            data = self._read(root)
            world = self.store.get(identifier)
            known = {p["userid"] for p in data.values() if set(p["sources"]) - {"save"}}
            known.update(row["userid"] for row in rows if USER_ID.fullmatch(row["userid"]))
            known.update(userid for key in ("admins", "banned", "whitelist") for userid in world[key])
            saved = characters(root, known)
            # Drop stale save-only discoveries, including the old literal KU_..._ records.
            # Authenticated IDs and administrator-configured permissions are never normalized.
            valid = {c["userid"] for c in saved if c["userid"]}
            for userid in list(data):
                player = data[userid]
                if "save" in player["sources"] and userid not in valid:
                    player["sources"].remove("save")
                    if not player["sources"]:
                        del data[userid]
            if data != self._read(root):
                write_json(root / "pera-players.json", data)
            for character in saved:
                if character["userid"]:
                    rows.append({"userid": character["userid"], "source": "save", "folder": character["folder"],
                                 "shard": character["shard"]})
                else:
                    # Legacy live-console hints may name encoded folders. They are display hints only;
                    # recovery always validates the user's explicitly selected destination on disk.
                    matches = [p["userid"] for p in data.values() if "console" in p["sources"]
                               and p["folders"].get(character["shard"]) == character["folder"]]
                    matches.extend(p["userid"] for p in data.values() if character["path"] in p.get("paths", {}))
                    if len(set(matches)) == 1:
                        character["userid"] = matches[0]
                        character["identity_source"] = "saved_log"
            self.record_many(identifier, rows)
            return saved

    def list(self, identifier):
        world = self.store.get(identifier)
        with self.lock:
            data = self._read(self.store.world_path(identifier))
        for key in ("admins", "banned", "whitelist"):
            for userid in world[key]:
                data.setdefault(userid, {"userid": userid, "name": "", "sources": ["permissions"], "folders": {}})
        return sorted(data.values(), key=lambda p: (p.get("name", "").casefold(), p["userid"]))

    def character_path(self, identifier, relative):
        if not isinstance(relative, str):
            raise PanelError("Choose a saved character folder.")
        parts = relative.split("/")
        if len(parts) != 5 or parts[0] not in ("Master", "Caves") or parts[1:3] != ["save", "session"] \
                or not SESSION.fullmatch(parts[3]) or not FOLDER.fullmatch(parts[4]):
            raise PanelError("Choose a saved character folder.")
        root = self.store.world_path(identifier)
        path = root.joinpath(*parts)
        if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
            raise PanelError("Linked character files are not supported.")
        if not any(item["path"] == relative for item in characters(root)):
            raise PanelError("The saved character folder no longer exists. Refresh the list.", 409)
        return path
