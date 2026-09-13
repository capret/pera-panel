import json
from pathlib import Path
import shutil
import threading
import time

import pytest

from pera_panel.service import Service
from pera_panel.storage import PanelError, atomic_write, write_json


def make_world(service):
    identifier = service.create({"name": "Camp", "token": "old-token"})["world_id"]
    for shard in ("Master", "Caves"):
        atomic_write(service.store.world_path(identifier) / shard / "save/session/day", "day 10")
    return identifier


def test_backup_restore_both_shards_and_preserve_current_token(service):
    identifier = make_world(service)
    service.start(identifier)
    backup_id = service.backup(identifier)["backup_id"]
    assert service.runtime.events == ["start", "stop", "start"]
    service.stop(identifier)
    service.configure(identifier, {"token": "new-token"})
    for shard in ("Master", "Caves"):
        atomic_write(service.store.world_path(identifier) / shard / "save/session/day", "day 20")
    service.start(identifier)
    result = service.restore(identifier, backup_id)
    assert service.runtime.active(identifier)
    assert service.resume_after_operation is None
    assert service.store.get(identifier)["token"] == "new-token"
    for shard in ("Master", "Caves"):
        assert (service.store.world_path(identifier) / shard / "save/session/day").read_text() == "day 10"
        safety = service.backup_path(identifier, result["safety_backup_id"])
        assert (safety / "cluster" / shard / "save/session/day").read_text() == "day 20"


def test_failed_shutdown_never_copies_live_save(service):
    identifier = make_world(service)
    service.start(identifier)
    service.runtime.fail_stop = True
    with pytest.raises(PanelError, match="timed out"):
        service.backup(identifier)
    assert service.backups(identifier) == []
    assert service.runtime.active(identifier)
    assert service.resume_after_operation is None


def test_failed_restore_copy_leaves_current_world_and_restarts(service, monkeypatch):
    identifier = make_world(service)
    snapshot = service.backup(identifier)["backup_id"]
    service.start(identifier)
    original = shutil.copytree

    def fail_restore(source, destination, *args, **kwargs):
        if Path(destination).name.startswith(".restore-"):
            raise OSError("disk full")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", fail_restore)
    with pytest.raises(OSError, match="disk full"):
        service.restore(identifier, snapshot)
    assert service.store.get(identifier)["name"] == "Camp"
    assert service.runtime.active(identifier)


def test_restore_write_failure_recovers_previous_directory(service, monkeypatch):
    identifier = make_world(service)
    snapshot = service.backup(identifier)["backup_id"]
    day = service.store.world_path(identifier) / "Master/save/session/day"
    atomic_write(day, "day 20")

    def failed_write(world):
        raise OSError("write failed")

    monkeypatch.setattr(service.store, "write_world", failed_write)
    with pytest.raises(OSError):
        service.restore(identifier, snapshot)
    assert day.read_text() == "day 20"


def test_running_config_blocked_and_only_one_boot_world(service):
    first = service.create({"name": "One", "autostart": True})["world_id"]
    with pytest.raises(PanelError, match="Only one"):
        service.create({"name": "Two", "autostart": True})
    service.start(first)
    with pytest.raises(PanelError, match="Stop"):
        service.configure(first, {"name": "Changed"})


def test_caves_topology_cannot_change_after_generation(service):
    identifier = make_world(service)
    with pytest.raises(PanelError, match="Caves cannot"):
        service.configure(identifier, {"caves": False})


def test_jobs_serialized_and_errors_visible(service):
    release = threading.Event()
    first = service.submit("Slow action", lambda: release.wait(timeout=3))
    try:
        with pytest.raises(PanelError, match="in progress"):
            service.submit("Conflicting action", lambda: None)
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while service.busy and time.monotonic() < deadline:
        time.sleep(.01)
    assert service.job_list()[0]["id"] == first["id"]
    assert service.job_list()[0]["state"] == "completed"
    service.submit("Bad input", lambda: service.store.get("../../bad"))
    while service.busy and time.monotonic() < deadline:
        time.sleep(.01)
    assert service.job_list()[0]["state"] == "failed"
    assert "world ID" in service.job_list()[0]["error"]


def test_interrupted_jobs_marked_failed(service):
    write_json(service.jobs_file, [{"id": "1", "state": "running"}])
    second = Service(service.store.root, service.store.game, type(service.runtime))
    try:
        assert second.job_list()[0]["state"] == "failed"
    finally:
        second.close()


def test_shutdown_remembers_active_world_for_restart(service):
    identifier = make_world(service)
    service.start(identifier)
    service.close()
    assert json.loads((service.store.root / "resume.json").read_text())["world_id"] == identifier


def test_delete_keeps_recoverable_world(service):
    identifier = make_world(service)
    with pytest.raises(PanelError):
        service.delete(identifier, "wrong name")
    service.delete(identifier, "Camp")
    assert service.store.all() == []
    assert list((service.store.root / "deleted").glob("*/Master/save/session/day"))
    assert list((service.store.backups / identifier).glob("*/backup.json"))
