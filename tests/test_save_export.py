import io
import stat
import threading
from zipfile import ZipFile

import pytest

from pera_panel import save_export
from pera_panel.save_import import stage_save
from pera_panel.storage import PanelError, atomic_write
from test_save_import import SESSION, existing_world
from test_web import wait_job


@pytest.mark.parametrize("caves", [True, False])
def test_download_round_trip_preserves_saves_mods_and_shard_format(client, auth, service, tmp_path, caves):
    identifier = service.create({"name": "测试 / Camp", "token": "private-token", "password": "private-password",
                                 "caves": caves, "shard_ids": {"Master": "13", "Caves": "27"},
                                 "encode_user_path": {"Master": False, "Caves": True},
                                 "mods": [{"id": "378160973", "enabled": False, "options": {"test": "中文"}}]})["world_id"]
    root = service.store.world_path(identifier)
    for shard in (("Master", "Caves") if caves else ("Master",)):
        atomic_write(root / shard / SESSION, shard + " progress")
        atomic_write(root / shard / (SESSION + ".meta"), "metadata")
        atomic_write(root / shard / "save/session/0123456789ABCDEF/KU_abcdefgh/0000000010", "player progress")
        atomic_write(root / shard / "server_log.txt", "private-log")
    if not caves:
        atomic_write(root / "Caves" / SESSION, "unused caves")
    response = client.post(f"/api/worlds/{identifier}/export-save", headers=auth)
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert response.headers["Content-Disposition"].startswith(f"attachment; filename=pera-save-{identifier}-")
    assert response.headers["Cache-Control"] == "no-store"
    data = response.data
    assert response.content_length == len(data)
    response.close()
    with ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        assert all(name.startswith(f"Cluster_{identifier}/") for name in names)
        assert any(name.endswith("modoverrides.lua") for name in names)
        assert not any(name.endswith(("pera.json", "cluster_token.txt", "server_log.txt")) for name in names)
        if not caves:
            assert not any(name.startswith(f"Cluster_{identifier}/Caves/") for name in names)
        contents = b"\n".join(archive.read(name) for name in names)
        assert b"private-token" not in contents and b"private-password" not in contents and b"private-log" not in contents
    upload = tmp_path / "download.zip"
    upload.write_bytes(data)
    restored = tmp_path / "restored"
    metadata = stage_save(upload, restored)
    assert metadata["caves"] == caves
    assert metadata["mods"] == service.store.get(identifier)["mods"]
    assert metadata["shard_ids"]["Master"] == "13"
    assert metadata["encode_user_path"]["Master"] is False
    for shard in (("Master", "Caves") if caves else ("Master",)):
        assert (restored / shard / SESSION).read_text() == shard + " progress"
        assert (restored / shard / "save/session/0123456789ABCDEF/KU_abcdefgh/0000000010").read_text() == "player progress"
    assert not service.runtime.events and not service.backups(identifier)
    assert not list(service.store.root.glob(".save-export-*"))


def test_export_auth_csrf_missing_world_and_empty_save(client, auth, service):
    identifier = service.create({"name": "New world"})["world_id"]
    url = f"/api/worlds/{identifier}/export-save"
    assert client.post(url).status_code == 403
    assert client.get(url).status_code == 405
    assert client.post(url, headers=auth).status_code == 409
    assert client.post("/api/worlds/abcdef123456/export-save", headers=auth).status_code == 404
    assert client.post("/api/worlds/not-an-id/export-save", headers=auth).status_code == 404
    client.post("/api/logout", headers=auth)
    assert client.post(url, headers=auth).status_code == 401


def test_download_can_be_uploaded_into_another_world(client, auth, service):
    source = existing_world(service)
    response = client.post(f"/api/worlds/{source}/export-save", headers=auth)
    data = response.data
    response.close()
    target = service.create({"name": "Restored", "token": "destination-token", "caves": False})["world_id"]
    response = client.post(f"/api/worlds/{target}/import-save", headers=auth,
                           data={"save_zip": (io.BytesIO(data), "download.zip"), "confirmation": "Restored"})
    assert response.status_code == 202
    assert wait_job(service)["state"] == "completed"
    world = service.store.get(target)
    assert world["caves"] and world["mods"] == service.store.get(source)["mods"]
    assert world["token"] == "destination-token" and world["name"] == "Restored"
    for shard in ("Master", "Caves"):
        assert (service.store.world_path(target) / shard / SESSION).read_text() == "old progress"


def test_running_busy_and_closing_worlds_cannot_export(client, auth, service):
    identifier = existing_world(service)
    url = f"/api/worlds/{identifier}/export-save"
    service.start(identifier)
    response = client.post(url, headers=auth)
    assert response.status_code == 409 and "Stop this world" in response.json["error"]
    assert service.runtime.active(identifier) and service.runtime.events == ["start"]
    service.stop(identifier)
    release = threading.Event()
    service.submit("Test operation", lambda: release.wait(5))
    try:
        assert client.post(url, headers=auth).status_code == 409
    finally:
        release.set()
        wait_job(service)
    service.closing = True
    assert client.post(url, headers=auth).status_code == 409


def test_export_holds_operation_lock_until_zip_is_independent(service, monkeypatch):
    identifier = existing_world(service)
    original = save_export.create_save_zip
    started, release, submitted = threading.Event(), threading.Event(), threading.Event()
    exports = []

    def delayed(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    def submit_start():
        service.submit("Start world", lambda: service.start(identifier), world_id=identifier)
        submitted.set()

    monkeypatch.setattr("pera_panel.service.create_save_zip", delayed)
    export_thread = threading.Thread(target=lambda: exports.append(service.export_save(identifier)))
    start_thread = threading.Thread(target=submit_start)
    export_thread.start()
    try:
        assert started.wait(5)
        start_thread.start()
        assert not submitted.wait(.05)
    finally:
        release.set()
        export_thread.join(5)
        start_thread.join(5)
    assert submitted.is_set() and len(exports) == 1
    stream, _ = exports[0]
    stream.close()
    assert wait_job(service)["state"] == "completed"


def test_linked_save_is_rejected_before_reading(service, tmp_path):
    identifier = existing_world(service)
    secret = tmp_path / "outside.txt"
    secret.write_text("outside data")
    link = service.store.world_path(identifier) / "Master/save/outside"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("Creating symlinks is unavailable")
    with pytest.raises(PanelError, match="Linked"):
        service.export_save(identifier)


def test_special_file_and_windows_junction_are_rejected():
    class Path:
        def lstat(self):
            return type("Stat", (), {"st_mode": stat.S_IFIFO, "st_file_attributes": 0})()

    with pytest.raises(PanelError, match="special"):
        save_export.checked_stat(Path())
    Path.lstat = lambda self: type("Stat", (), {"st_mode": stat.S_IFDIR, "st_file_attributes": 0x400})()
    with pytest.raises(PanelError, match="Linked"):
        save_export.checked_stat(Path())


@pytest.mark.parametrize("limit", ["MAX_UPLOAD_BYTES", "MAX_EXPANDED_BYTES", "MAX_MEMBERS"])
def test_export_size_limits_and_temp_cleanup(service, monkeypatch, limit):
    identifier = existing_world(service)
    monkeypatch.setattr(save_export, limit, 1)
    with pytest.raises(PanelError):
        service.export_save(identifier)
    assert not list(service.store.root.glob(".save-export-*"))


def test_partial_caves_save_is_not_offered_as_reimportable(service):
    identifier = existing_world(service)
    (service.store.world_path(identifier) / "Caves" / SESSION).unlink()
    with pytest.raises(PanelError, match="Caves has no saved session"):
        service.export_save(identifier)
    assert not list(service.store.root.glob(".save-export-*"))


def test_export_write_failure_closes_temporary_stream(service, monkeypatch):
    identifier = existing_world(service)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(save_export.ZipFile, "writestr", fail)
    with pytest.raises(OSError, match="disk full"):
        service.export_save(identifier)
    assert not list(service.store.root.glob(".save-export-*"))


def test_download_section_is_next_to_import(client, auth):
    html = client.get("/").data.decode()
    assert 'id="download-save"' in html and "Download saved game" in html
    assert "Stop the world before downloading" in html
