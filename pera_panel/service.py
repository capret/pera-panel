"""Serialize mutations and snapshot both shards only after they have stopped."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import shutil
import threading
import tempfile
import uuid

from .runtime import Runtime
from .archives import Archives
from .players import PlayerRegistry, USER_ID
from .save_import import stage_save
from .save_export import create_save_zip
from .storage import PanelError, Store, line, validate_world, write_json


class Service:
    def __init__(self, root, game, runtime_factory=Runtime):
        self.store = Store(root, game)
        self.players = PlayerRegistry(self.store)
        self.archives = Archives(self.store)
        self.runtime = runtime_factory(self.store)
        self.runtime.players = self.players
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pera-operations")
        self.lock = threading.RLock()
        self.upload_lock = threading.Lock()
        self.busy = None
        self.closing = False
        self.resume_after_operation = None
        self.jobs_file = self.store.root / "jobs.json"
        self.jobs = json.loads(self.jobs_file.read_text()) if self.jobs_file.exists() else []
        for job in self.jobs:
            if job["state"] in ("queued", "running"):
                job.update(state="failed", error="Panel restarted before this operation completed.")
        self._persist_jobs()

    def _persist_jobs(self):
        write_json(self.jobs_file, self.jobs[-50:])

    def submit(self, title, operation, *, world_id=None):
        with self.lock:
            if self.closing or self.busy:
                raise PanelError("Another operation is in progress. Wait for it to finish.", 409)
            job = {"id": uuid.uuid4().hex, "title": title, "state": "queued",
                   "created_at": datetime.now(timezone.utc).isoformat(), "error": None, "result": None,
                   "world_id": world_id, "world_name": None}
            if world_id:
                self.store.world_path(world_id)
                try:
                    job["world_name"] = self.store.get(world_id)["name"]
                except PanelError as exc:
                    if exc.status != 404:
                        raise
            self.jobs = self.jobs[-49:] + [job]
            self.busy = job["id"]
            self._persist_jobs()
            self.executor.submit(self._run, job, operation)
            return dict(job)

    def _run(self, job, operation):
        with self.lock:
            job["state"] = "running"
            self._persist_jobs()
        try:
            result = operation()
            with self.lock:
                job.update(state="completed", result=result)
                if not job.get("world_id") and isinstance(result, dict) and result.get("world_id"):
                    job["world_id"] = result["world_id"]
                    job["world_name"] = self.store.get(result["world_id"])["name"]
                if isinstance(result, dict) and result.get("world_name"):
                    job["world_name"] = result["world_name"]
        except Exception as exc:
            logging.exception("Operation failed: %s", job["title"])
            with self.lock:
                job.update(state="failed", error=str(exc) if isinstance(exc, PanelError)
                           else "Operation failed. Check sudo journalctl -u pera-panel for details.")
                if isinstance(exc, PanelError):
                    job["error_i18n"] = exc.i18n
        finally:
            with self.lock:
                self.busy = None
                self._persist_jobs()

    def job_list(self, identifier=None):
        with self.lock:
            return [dict(job) for job in reversed(self.jobs)
                    if identifier is None or job.get("world_id") == identifier]

    def require_stopped(self, identifier):
        if self.runtime.active(identifier):
            raise PanelError("Stop this world before changing its settings.", 409)

    def create(self, data):
        world = validate_world(data)
        self._check_autostart(world)
        if len(self.store.all()) >= 50:
            raise PanelError("The maximum of 50 saved worlds has been reached.")
        self.store.write_world(world)
        return {"world_id": world["id"]}

    def _check_autostart(self, world):
        if world["autostart"] and any(other["autostart"] and other["id"] != world["id"]
                                     for other in self.store.all()):
            raise PanelError("Only one world can start at boot. Disable autostart on the other world first.")

    def configure(self, identifier, data):
        self.require_stopped(identifier)
        previous = self.store.get(identifier)
        world = validate_world(data, previous)
        self._check_autostart(world)
        if world["caves"] != previous["caves"] and any(self.store.world_path(identifier).glob("*/save")):
            raise PanelError("Caves cannot be changed after world generation. Create another world instead.")
        self.store.write_world(world)

    def start(self, identifier):
        if self.closing:
            raise PanelError("The panel is shutting down.", 409)
        self.runtime.start(self.store.get(identifier))
        return "Game processes started. Check the logs for world generation and network readiness."

    def stop(self, identifier):
        self.store.get(identifier)
        if self.runtime.world_id == identifier:
            self.runtime.stop()

    def restart(self, identifier):
        self.stop(identifier)
        return self.start(identifier)

    @contextmanager
    def paused(self, identifier, restart=True):
        running = self.runtime.active(identifier)
        self.resume_after_operation = identifier if running else None
        stopped = False
        completed = False
        try:
            self.stop(identifier)
            stopped = True
            yield
            completed = True
        finally:
            try:
                if running and stopped and not self.closing and (restart or not completed):
                    self.start(identifier)
            finally:
                self.resume_after_operation = None

    def _snapshot(self, identifier, label, *, system_label=False):
        source = self.store.world_path(identifier)
        # Never follow local symlinks into unrelated files when copying or restoring a world.
        if any(path.is_symlink() for path in source.rglob("*")):
            raise PanelError("A world contains a symbolic link; snapshot refused.")
        snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        root = self.store.backups / identifier
        root.mkdir(parents=True, exist_ok=True)
        staging = root / ("." + snapshot_id)
        try:
            shutil.copytree(source, staging / "cluster", ignore=shutil.ignore_patterns("server_log.txt", "*.tmp"))
            write_json(staging / "backup.json", {"id": snapshot_id, "label": label,
                       "label_i18n": label if system_label else None,
                       "created_at": datetime.now(timezone.utc).isoformat(),
                       "size_mb": round(sum(p.stat().st_size for p in staging.rglob("*") if p.is_file()) / 1024**2, 2)})
            staging.rename(root / snapshot_id)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return snapshot_id

    def backup(self, identifier, label="Manual backup"):
        self.store.get(identifier)
        label = line(label, "Backup label", 100, required=True)
        with self.paused(identifier):
            return {"backup_id": self._snapshot(identifier, label)}

    def backup_path(self, identifier, snapshot):
        self.store.get(identifier)
        if not re.fullmatch(r"\d{8}T\d{6}-[a-f0-9]{8}", snapshot):
            raise PanelError("Invalid backup ID.", 404)
        path = self.store.backups / identifier / snapshot
        if path.is_symlink() or not (path / "backup.json").is_file():
            raise PanelError("Backup not found.", 404)
        return path

    def backups(self, identifier):
        self.store.get(identifier)
        root = self.store.backups / identifier
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*/backup.json"), reverse=True)
                if not path.parent.name.startswith(".")]

    def restore(self, identifier, snapshot):
        source = self.backup_path(identifier, snapshot) / "cluster"
        if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
            raise PanelError("Linked files cannot be restored.")
        current = self.store.get(identifier)
        saved = json.loads((source / "pera.json").read_text(encoding="utf-8"))
        saved.setdefault("shard_ids", {"Master": "1", "Caves": "2"})
        saved.setdefault("encode_user_path", {})
        restored = validate_world(saved, current)
        # Preserve current credentials and boot policy while rolling back world data and settings.
        restored.update(token=current["token"], cluster_key=current["cluster_key"], autostart=current["autostart"])
        root = self.store.world_path(identifier)
        staging = root.with_name(".restore-" + uuid.uuid4().hex)
        previous = root.with_name(".previous-" + uuid.uuid4().hex)
        with self.paused(identifier):
            try:
                safety_id = self._snapshot(identifier, "Before rollback", system_label=True)
                shutil.copytree(source, staging)
                root.rename(previous)
                try:
                    staging.rename(root)
                    self.store.write_world(restored)
                except Exception:
                    if root.exists():
                        shutil.rmtree(root)
                    previous.rename(root)
                    raise
                shutil.rmtree(previous)
                return {"safety_backup_id": safety_id}
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def delete_backup(self, identifier, snapshot):
        shutil.rmtree(self.backup_path(identifier, snapshot))

    def export_save(self, identifier):
        # Hold the operation lock only while preparing an independent temporary ZIP.
        # A queued start, restore, or deletion cannot change these files mid-export.
        with self.lock:
            if self.busy or self.closing:
                raise PanelError("Another operation is in progress. Wait for it to finish.", 409)
            world = self.store.get(identifier)
            if self.runtime.active(identifier):
                raise PanelError("Stop this world before downloading its save.", 409)
            return create_save_zip(self.store.world_path(identifier), world, self.store.root)

    def import_save(self, identifier, upload, confirmation, inherit_mods=True):
        """Replace a cluster as one operation; preserve panel settings and keep success stopped."""
        current = self.store.get(identifier)
        if confirmation != current["name"]:
            raise PanelError("Type the world name exactly to replace its save.")
        with tempfile.TemporaryDirectory(prefix=".import-", dir=self.store.clusters) as temporary:
            prepared = Path(temporary) / "prepared"
            metadata = stage_save(upload, prepared, inherit_mods)  # Validate before stopping the world.
            changes = {"caves": metadata["caves"],
                       "shard_ids": {"Master": "1", "Caves": "2", **metadata["shard_ids"]},
                       "encode_user_path": metadata["encode_user_path"]}
            if inherit_mods:
                changes["mods"] = metadata["mods"]
            restored = validate_world(changes, current)
            root = self.store.world_path(identifier)
            # Keep recovery data outside the temporary tree if rollback itself encounters an I/O error.
            previous = root.with_name(".previous-import-" + uuid.uuid4().hex)
            with self.paused(identifier, restart=False):
                safety_id = self._snapshot(identifier, "Before save import", system_label=True)
                root.rename(previous)
                try:
                    prepared.rename(root)
                    self.store.write_world(restored)
                    self.players.discover(identifier)
                    self.players.record_many(identifier, [{"userid": userid, "name": name, "source": "imported_log"}
                                                          for userid, name in metadata["players"].items()])
                    self.players.record_many(identifier, [{**link, "source": "imported_log"}
                                                          for link in metadata["character_links"]])
                except Exception:
                    if root.exists():
                        shutil.rmtree(root)
                    previous.rename(root)
                    raise
                shutil.rmtree(previous)
                return {"world_id": identifier, "safety_backup_id": safety_id,
                        "mods_inherited": inherit_mods, "mod_count": len(restored["mods"]),
                        "message_i18n": {"key": "Save imported. Mod settings inherited: {count}. The world is stopped. Enabled mods download on the next start."
                                         if inherit_mods else "Save imported. Panel mods kept. The world is stopped. Enabled mods download on the next start.",
                                         "params": {"count": len(restored["mods"]) }},
                        "message": (f"Local save imported with {len(restored['mods'])} mod settings. "
                                    if inherit_mods else "Local save imported; panel mods kept. ")
                                   + "The world is stopped. Enabled mods download on the next start."}

    def rollback(self, identifier, count, confirmation):
        if confirmation != self.store.get(identifier)["name"]:
            raise PanelError("Type the world name exactly to roll back.")
        return self.runtime.rollback(identifier, count)

    def character_list(self, identifier):
        self.store.get(identifier)
        return self.players.discover(identifier)

    def recover_character(self, identifier, source, userid, confirmation, destination=None):
        """Copy between explicitly chosen existing character folders in one shard/session."""
        self.require_stopped(identifier)
        if confirmation != self.store.get(identifier)["name"]:
            raise PanelError("Type the world name exactly to recover a character.")
        origin = self.players.character_path(identifier, source)
        if destination is None:
            # Backward compatibility for an older open dashboard. Resolve only a unique
            # account-linked folder actually present in this session, never construct a new path.
            if not isinstance(userid, str) or not USER_ID.fullmatch(userid):
                raise PanelError("Choose the online Klei account that should receive this character.")
            matches = [c["path"] for c in self.character_list(identifier) if c["userid"] == userid
                       and c["path"].rsplit("/", 1)[0] == source.rsplit("/", 1)[0] and c["path"] != source]
            if len(matches) != 1:
                raise PanelError("Choose an existing destination character save. Refresh the page to use folder selection.", 409)
            destination = matches[0]
        target = self.players.character_path(identifier, destination)
        if origin.parent != target.parent:
            raise PanelError("Source and destination must belong to the same shard and world session.")
        if origin == target:
            raise PanelError("The selected character already belongs to this destination folder.")
        if any(path.is_symlink() for path in origin.rglob("*")) or any(path.is_symlink() for path in target.rglob("*")):
            raise PanelError("Linked character files are not supported.")
        safety = self._snapshot(identifier, "Before character recovery", system_label=True)
        with tempfile.TemporaryDirectory(prefix=".character-", dir=self.store.clusters) as temporary:
            prepared = Path(temporary) / "prepared"
            previous = target.with_name(".previous-character-" + uuid.uuid4().hex)
            shutil.copytree(origin, prepared)
            target.rename(previous)
            try:
                prepared.rename(target)
            except Exception:
                previous.rename(target)
                raise
            shutil.rmtree(previous)
        return {"safety_backup_id": safety, "source": source, "destination": destination,
                "message": "Character files recovered for the selected shard. The world is stopped. Start it and verify your character in game."}

    def delete(self, identifier, confirmation):
        world = self.store.get(identifier)
        if confirmation != world["name"]:
            raise PanelError("Type the world name exactly to delete it.")
        self.require_stopped(identifier)
        safety = self._snapshot(identifier, "Before world deletion", system_label=True)
        # Archive instead of deleting so the world remains recoverable through the filesystem.
        archive = self.store.root / "deleted"
        archive.mkdir(exist_ok=True)
        self.store.world_path(identifier).rename(archive / f"{identifier}-{uuid.uuid4().hex[:8]}")
        return {"backup_id": safety, "message": "World archived under the data/deleted directory."}

    def delete_archived(self, identifier, confirmation):
        self.require_stopped(identifier)
        return self.archives.delete(identifier, confirmation)

    def autostart(self):
        resume_file = self.store.root / "resume.json"
        if resume_file.exists():
            identifier = json.loads(resume_file.read_text()).get("world_id")
            resume_file.unlink()
            if identifier and any(w["id"] == identifier for w in self.store.all()):
                self.submit("Resume world after maintenance", lambda: self.start(identifier), world_id=identifier)
                return
        worlds = [world for world in self.store.all() if world["autostart"]]
        if worlds:
            self.submit("Start world at boot", lambda: self.start(worlds[0]["id"]), world_id=worlds[0]["id"])

    def close(self):
        self.closing = True
        resume_id = self.runtime.world_id if self.runtime.active() else self.resume_after_operation
        if resume_id:
            write_json(self.store.root / "resume.json", {"world_id": resume_id})
        # Kill a long-running mod downloader before waiting for the operation thread.
        if self.runtime.updater and self.runtime.updater.poll() is None:
            self.runtime.updater.kill()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.runtime.shutdown()
