"""Build portable, import-compatible save ZIPs from a stopped world."""
import stat
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .save_import import (CHUNK, MAX_EXPANDED_BYTES, MAX_MEMBERS, MAX_UPLOAD_BYTES,
                          inspect_archive, member_parts, preflight)
from .storage import PanelError, ini, lua


def checked_stat(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise PanelError("Linked or special save files cannot be downloaded.")
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise PanelError("Linked or special save files cannot be downloaded.")
    return info


def save_files(root, shards):
    """Check directories before descending, including Windows junctions."""
    checked_stat(root)
    pending = []
    for shard in shards:
        try:
            checked_stat(root / shard)
        except FileNotFoundError as exc:
            raise PanelError("{shard} has no saved session data. Include the complete shard save folder.",
                             params={"shard": shard}) from exc
        path = root / shard / "save"
        if path.exists() or path.is_symlink():
            pending.append(path)
    count = 0
    while pending:
        path = pending.pop()
        info = checked_stat(path)
        count += 1
        if count > MAX_MEMBERS:
            raise PanelError("The ZIP has too many entries or too much directory metadata.")
        relative = path.relative_to(root).as_posix()
        member = ZipInfo(relative + ("/" if stat.S_ISDIR(info.st_mode) else ""))
        member.file_size = member.compress_size = info.st_size if stat.S_ISREG(info.st_mode) else 0
        member_parts(member)
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        else:
            yield path, relative, info.st_size


def settings(world, shards):
    # Regenerate only portable configuration; never include tokens, passwords, or pera.json.
    yield "cluster.ini", ini({
        "GAMEPLAY": {key: world[key] for key in ("game_mode", "max_players", "pvp", "pause_when_empty")},
        "NETWORK": {"cluster_name": world["name"], "cluster_description": world["description"],
                    "cluster_intention": "cooperative"},
        "MISC": {"console_enabled": True, "max_snapshots": world["snapshots"]},
        "SHARD": {"shard_enabled": world["caves"]},
    })
    mods = {f"workshop-{mod['id']}": {"enabled": mod["enabled"], "configuration_options": mod["options"]}
            for mod in world["mods"]}
    for index, shard in enumerate(shards):
        yield f"{shard}/server.ini", ini({
            "SHARD": {"is_master": index == 0, "name": shard,
                      "id": world.get("shard_ids", {}).get(shard, str(index + 1))},
            **({"ACCOUNT": {"encode_user_path": world["encode_user_path"][shard]}}
               if shard in world.get("encode_user_path", {}) else {}),
        })
        yield f"{shard}/modoverrides.lua", "return " + lua(mods) + "\n"
        yield f"{shard}/worldgenoverride.lua", "return " + lua({
            "override_enabled": True, "preset": "SURVIVAL_TOGETHER" if index == 0 else "DST_CAVE",
            "overrides": world["master_overrides" if index == 0 else "caves_overrides"],
        }) + "\n"


def create_save_zip(root, world, temporary_dir):
    shards = ("Master", "Caves") if world["caves"] else ("Master",)
    files = list(save_files(root, shards))
    if not any(relative.startswith("Master/save/session/") and len(relative.split("/")) >= 5
               and size and not relative.endswith(".meta") for _, relative, size in files):
        raise PanelError("This world has no saved game yet. Start and stop it before downloading.", 409)
    for shard in shards:
        if not any(relative.startswith(f"{shard}/save/session/") and len(relative.split("/")) >= 5
                   and size and not relative.endswith(".meta") for _, relative, size in files):
            raise PanelError("{shard} has no saved session data. Include the complete shard save folder.",
                             params={"shard": shard})
    if sum(size for _, _, size in files) > MAX_EXPANDED_BYTES:
        raise PanelError("The ZIP exceeds the 2 GiB expanded size limit.")
    stream = tempfile.TemporaryFile(prefix=".save-export-", dir=temporary_dir)
    prefix = f"Cluster_{world['id']}/"
    try:
        with ZipFile(stream, "w", compression=ZIP_DEFLATED, compresslevel=1, allowZip64=False) as archive:
            for relative, content in settings(world, shards):
                archive.writestr(prefix + relative, content)
            for path, relative, size in files:
                checked_stat(path)
                written = 0
                with path.open("rb") as source, archive.open(prefix + relative, "w") as output:
                    while chunk := source.read(CHUNK):
                        written += len(chunk)
                        if written > size:
                            raise PanelError("Save files changed during download. Try again after stopping the world.", 409)
                        output.write(chunk)
                        if stream.tell() > MAX_UPLOAD_BYTES:
                            raise PanelError("The save ZIP must be 256 MiB or smaller.", 413)
                if written != size:
                    raise PanelError("Save files changed during download. Try again after stopping the world.", 409)
        size = stream.tell()
        if size > MAX_UPLOAD_BYTES:
            raise PanelError("The save ZIP must be 256 MiB or smaller.", 413)
        # Use the importer's validation so every completed download can be uploaded again.
        preflight(stream)
        stream.seek(0)
        with ZipFile(stream) as archive:
            inspect_archive(archive, inherit_mods=True)
        stream.seek(0)
        return stream, size
    except BaseException:
        stream.close()
        raise
