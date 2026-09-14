"""List and permanently remove archived worlds within the panel's data directory."""
import json
import os
from pathlib import Path
import re
import shutil

from .storage import PanelError


ARCHIVE_ID = re.compile(r"([a-f0-9]{12})-[a-f0-9]{8}")


class Archives:
    def __init__(self, store):
        self.store = store
        self.root = store.root / "deleted"

    def _guard(self, path):
        # Validate the absolute destination and every parent before traversing or deleting.
        if not path.is_relative_to(self.store.root) or path == self.store.root:
            raise PanelError("Archived data must stay inside the panel data directory.")
        for part in (path, *path.parents):
            if part == self.store.root:
                break
            reparse = os.name == "nt" and part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400
            if part.is_symlink() or reparse:
                raise PanelError("Linked archived files, backups, or logs cannot be deleted.")
        if path.resolve() != path:
            raise PanelError("Archived data must stay inside the panel data directory.")

    def _size(self, path):
        self._guard(path)
        if not path.exists():
            return 0
        if not path.is_dir():
            raise PanelError("Expected an archived data directory.")
        size = 0

        def failed(error):
            raise error

        for directory, dirs, files in os.walk(path, followlinks=False, onerror=failed):
            for name in dirs:
                self._guard(Path(directory) / name)
            for name in files:
                child = Path(directory) / name
                self._guard(child)
                size += child.stat().st_size
        return size

    def _groups(self):
        self._guard(self.root)
        groups = {}
        if self.root.exists():
            for path in sorted(self.root.iterdir()):
                match = ARCHIVE_ID.fullmatch(path.name)
                if match and path.is_dir():
                    groups.setdefault(match[1], []).append(path)
        return groups

    def _entry(self, identifier, paths):
        related = [self.store.backups / identifier, self.store.logs / identifier]
        size = sum(self._size(path) for path in [*paths, *related])
        name = identifier
        for path in reversed(paths):
            metadata = path / "pera.json"
            if metadata.is_file() and metadata.stat().st_size <= 262144:
                try:
                    saved = json.loads(metadata.read_text(encoding="utf-8"))
                    candidate = saved.get("name") if isinstance(saved, dict) else None
                    if isinstance(candidate, str) and candidate.strip() and len(candidate) <= 80:
                        name = candidate
                        break
                except (ValueError, UnicodeError):
                    pass  # Older/damaged metadata can still be removed by confirming the ID.
        backups = related[0]
        count = sum(path.is_dir() and not path.name.startswith(".") for path in backups.iterdir()) if backups.exists() else 0
        return {"id": identifier, "name": name, "copies": len(paths), "backup_count": count,
                "size_mb": round(size / 1024 ** 2, 2)}

    def list(self):
        return [self._entry(identifier, paths) for identifier, paths in self._groups().items()]

    def delete(self, identifier, confirmation):
        # Validate the ID even if there are no archived worlds.
        active = self.store.world_path(identifier)
        paths = self._groups().get(identifier)
        if not paths:
            raise PanelError("Archived world not found.", 404)
        if active.exists():
            raise PanelError("This world is still in your dashboard. Archive it before deleting its data.", 409)
        entry = self._entry(identifier, paths)  # Preflight every tree before deleting any data.
        if confirmation != entry["name"]:
            raise PanelError("Type the archived world name exactly to permanently delete it.")
        # Keep the archive identity until related data is removed, allowing retries after I/O errors.
        for path in [self.store.backups / identifier, self.store.logs / identifier, *paths]:
            self._guard(path)
            if path.exists():
                shutil.rmtree(path)
        return {"world_name": entry["name"], "message": "Archived world, backups, and logs permanently deleted."}
