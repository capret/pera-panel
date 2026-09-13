"""Inspect and stage local DST saves without extracting executable configuration files."""
import configparser
from pathlib import Path
import re
import shutil
import stat
import struct
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
import zlib

from .storage import PanelError
from .lua_settings import read_mods
from .players import AUTHENTICATED, FOLDER, SESSION, SNAPSHOT

MAX_UPLOAD_BYTES = 256 * 1024**2
UPLOAD_REQUEST_LIMIT = MAX_UPLOAD_BYTES + 1024**2  # Multipart envelope allowance.
MAX_EXPANDED_BYTES = 2 * 1024**3
MAX_MEMBERS = 20000
MAX_MEMBER_BYTES = 512 * 1024**2
MAX_RATIO = 1000
CHUNK = 1024**2


def preflight(upload):
    # Bound the central directory before ZipFile allocates a ZipInfo object per entry.
    with upload.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        if size > MAX_UPLOAD_BYTES:
            raise PanelError("The save ZIP must be 256 MiB or smaller.", 413)
        stream.seek(max(0, size - 65557))
        tail = stream.read()
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise PanelError("The ZIP is damaged or incomplete.")
    _, disk, central_disk, disk_entries, entries, central_size, _, comment = struct.unpack(
        "<4s4H2LH", tail[offset:offset + 22])
    if offset + 22 + comment != len(tail):
        raise PanelError("The ZIP is damaged or incomplete.")
    if disk or central_disk or disk_entries != entries:
        raise PanelError("Split ZIP archives are not supported.")
    if offset >= 20 and tail[offset - 20:offset - 16] == b"PK\x06\x07":
        raise PanelError("Use a standard ZIP archive without ZIP64 metadata.")
    if entries > MAX_MEMBERS or central_size > 16 * 1024**2:
        raise PanelError("The ZIP has too many entries or too much directory metadata.")


def member_parts(member):
    name = member.orig_filename
    # Backslashes are normal separators in some Windows ZIP tools.
    name = name.replace("\\", "/")
    if name.endswith("/"):
        name = name[:-1]
    parts = tuple(name.split("/"))
    if not name or len(name) > 1024 or len(parts) > 30 or any(
        not part or part in (".", "..") or part.endswith((".", " "))
        or any(ord(char) < 32 or char in ':<>"|?*' for char in part)
        or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
        for part in parts
    ):
        raise PanelError("The ZIP contains an unsafe file path.")
    mode = member.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise PanelError("The ZIP contains a symbolic link or a special file.")
    if member.flag_bits & 1:
        raise PanelError("Password-protected ZIP files are not supported.")
    if member.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
        raise PanelError("Use a standard ZIP archive with Store or Deflate compression.")
    if member.file_size > MAX_MEMBER_BYTES:
        raise PanelError("A file in the ZIP exceeds the 512 MiB per-file limit.")
    if member.file_size > max(member.compress_size, 1) * MAX_RATIO:
        raise PanelError("The ZIP has an excessive compression ratio.")
    return parts


def inspect_archive(archive, inherit_mods):
    members = archive.infolist()
    if not members or len(members) > MAX_MEMBERS:
        raise PanelError("The ZIP must contain between 1 and 20,000 entries.")
    entries, seen, files = [], set(), set()
    expanded = 0
    for member in members:
        parts = member_parts(member)
        key = tuple(part.casefold() for part in parts)
        if key in seen:
            raise PanelError("The ZIP contains duplicate file paths.")
        seen.add(key)
        if not member.is_dir():
            files.add(key)
        expanded += member.file_size
        if expanded > MAX_EXPANDED_BYTES:
            raise PanelError("The ZIP exceeds the 2 GiB expanded size limit.")
        entries.append((member, parts))
    if any(key[:index] in files for key in seen for index in range(1, len(key))):
        raise PanelError("The ZIP contains conflicting file and directory paths.")

    candidates = set()
    for member, parts in entries:
        if member.is_dir() or not member.file_size or "__MACOSX" in parts:
            continue
        for index in range(len(parts) - 4):
            if parts[index:index + 3] == ("Master", "save", "session"):
                candidates.add(parts[:index])
    if len(candidates) > 1:
        raise PanelError("This ZIP contains multiple worlds. Zip just one Cluster folder and upload it again.")
    if not candidates:
        raise PanelError("No local world save found. Include Master/save/session and optional Caves/save/session. "
                         "For Steam Cloud saves, extract Master.zip and Caves.zip into those shard folders first.")
    prefix = candidates.pop()
    selected, shards, ids, mod_files = [], set(), {}, {}
    formats, players = {}, {}
    for member, parts in entries:
        if parts[:len(prefix)] != prefix:
            continue
        relative = parts[len(prefix):]
        if len(relative) >= 2 and relative[1] == "save" and relative[0] not in ("Master", "Caves"):
            raise PanelError("Only standard Master and Caves shards can be imported.")
        if relative in (("Master.zip",), ("Caves.zip",)):
            raise PanelError("Extract the Steam Cloud shard ZIPs into Master and Caves folders before uploading.")
        if len(relative) >= 2 and relative[0] in ("Master", "Caves"):
            if relative[1] == "save":
                shards.add(relative[0])
                if not member.is_dir() and len(relative) > 2:
                    selected.append((member, relative))
            elif inherit_mods and relative[1:] == ("modoverrides.lua",) and not member.is_dir():
                if member.file_size > 128 * 1024:
                    raise PanelError("Each uploaded modoverrides.lua must be 128 KiB or smaller.")
                mod_files[relative[0]] = read_mods(archive.read(member))
            elif relative[1:] == ("server.ini",) and not member.is_dir():
                if member.file_size > 65536:
                    raise PanelError("An uploaded server.ini is too large.")
                parser = configparser.ConfigParser(interpolation=None)
                try:
                    parser.read_string(archive.read(member).decode("utf-8-sig"))
                    identifier = parser.get("SHARD", "id", fallback="")
                    if parser.has_option("ACCOUNT", "encode_user_path"):
                        formats[relative[0]] = parser.getboolean("ACCOUNT", "encode_user_path")
                except (configparser.Error, UnicodeError, ValueError) as exc:
                    raise PanelError("An uploaded server.ini is invalid.") from exc
                if identifier:
                    if not re.fullmatch(r"[0-9]{1,20}", identifier):
                        raise PanelError("An uploaded shard ID is invalid.")
                    ids[relative[0]] = identifier
            elif relative[1:] == ("server_log.txt",) and not member.is_dir() and member.file_size <= 2 * 1024**2:
                for output in archive.read(member).decode("utf-8", errors="replace").splitlines():
                    match = AUTHENTICATED.fullmatch(output)
                    if match and len(players) < 1000:
                        players[match[1]] = match[2][:100]
    for shard in shards:
        if not any(relative[:3] == (shard, "save", "session") and len(relative) >= 5
                   and member.file_size > 0 and not relative[-1].endswith(".meta")
                   for member, relative in selected):
            raise PanelError("{shard} has no saved session data. Include the complete shard save folder.",
                             params={"shard": shard})
    if not selected or "Master" not in shards:
        raise PanelError("The ZIP must include a saved surface world under Master/save/session.")
    # Missing server.ini is common for locally hosted worlds. Infer only uniform layouts.
    for shard in shards - formats.keys():
        folders = {relative[4] for _, relative in selected if len(relative) == 6
                   and relative[:3] == (shard, "save", "session") and SESSION.fullmatch(relative[3])
                   and FOLDER.fullmatch(relative[4]) and SNAPSHOT.fullmatch(relative[5])}
        raw = {folder.startswith(("KU_", "OU_")) for folder in folders}
        if len(raw) == 1:
            formats[shard] = not raw.pop()
    metadata = {"caves": "Caves" in shards, "shard_ids": ids, "encode_user_path": formats, "players": players}
    if inherit_mods:
        settings = [mod_files[shard] for shard in sorted(shards) if shard in mod_files]
        if len(settings) == 2 and settings[0] != settings[1]:
            raise PanelError("Surface and caves have different mod settings. This panel uses one shared mod list. "
                             "Make the two modoverrides.lua files match, or turn off ‘Inherit mod settings’ "
                             "and configure mods in the panel before starting.")
        metadata["mods"] = settings[0] if settings else []
    return selected, metadata


def stage_save(upload: Path, destination: Path, inherit_mods=True):
    """Validate the whole ZIP directory, then stream only save data into a fresh directory."""
    try:
        preflight(upload)
        with ZipFile(upload) as archive:
            selected, metadata = inspect_archive(archive, inherit_mods)
            needed = sum(member.file_size for member, _ in selected)
            if shutil.disk_usage(destination.parent).free < needed + 64 * 1024**2:
                raise PanelError("There is not enough free disk space to unpack this save.", 409)
            written = 0
            for member, relative in selected:
                target = destination.joinpath(*relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as output:
                    member_written = 0
                    while chunk := source.read(CHUNK):
                        member_written += len(chunk)
                        written += len(chunk)
                        if member_written > member.file_size or written > MAX_EXPANDED_BYTES:
                            raise PanelError("The ZIP expands beyond its declared size.")
                        output.write(chunk)
                    if member_written != member.file_size:
                        raise PanelError("The ZIP contains an incomplete save file.")
            return metadata
    except (BadZipFile, EOFError, zlib.error, NotImplementedError) as exc:
        raise PanelError("The ZIP is damaged or unsupported. Create a new ZIP from your local save folder.") from exc
