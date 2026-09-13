import configparser
import io
import stat
import struct
import threading
import time
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from pera_panel import save_import
from pera_panel.storage import PanelError, atomic_write
from pera_panel.storage import lua


SESSION = "save/session/0123456789ABCDEF/0000000010"


def zip_bytes(entries):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def local_zip(tmp_path, prefix="Cluster_1/", caves=True, extra=None):
    entries = {prefix + "Master/" + SESSION: b"imported surface",
               prefix + "Master/" + SESSION + ".meta": b"snapshot metadata",
               prefix + "Master/save/saveindex": b"saved index"}
    if caves:
        entries[prefix + "Caves/" + SESSION] = b"imported caves"
    entries.update(extra or {})
    path = tmp_path / "local-save.zip"
    path.write_bytes(zip_bytes(entries))
    return path


def existing_world(service):
    identifier = service.create({"name": "Camp", "token": "keep-token", "password": "keep-password",
                                 "admins": ["KU_abcdefgh"], "mods": [{"id": "378160973"}]})["world_id"]
    for shard in ("Master", "Caves"):
        atomic_write(service.store.world_path(identifier) / shard / SESSION, "old progress")
    return identifier


def completed(service):
    deadline = time.monotonic() + 5
    while service.busy and time.monotonic() < deadline:
        time.sleep(.01)
    assert service.busy is None
    return service.job_list()[0]


@pytest.mark.parametrize("prefix", ["", "Cluster_1/", "Documents/Klei/123456/Cluster_1/"])
def test_import_wrapped_or_flat_save_keeps_configuration_and_backup(service, tmp_path, prefix):
    identifier = existing_world(service)
    archive = local_zip(tmp_path, prefix=prefix, extra={
        prefix + "cluster_token.txt": "do-not-import-this-token",
        prefix + "pera.json": '{"name":"untrusted"}',
        prefix + "Master/modoverrides.lua": 'os.execute("untrusted")',
        prefix + "Caves/server.ini": '[SHARD]\nid = 371\n[NETWORK]\nserver_port = 12345\n',
    })
    service.start(identifier)
    result = service.import_save(identifier, archive, "Camp", inherit_mods=False)
    assert not service.runtime.active()
    assert service.runtime.events == ["start", "stop"]
    assert service.resume_after_operation is None
    world = service.store.get(identifier)
    assert world["name"] == "Camp" and world["token"] == "keep-token"
    assert world["password"] == "keep-password" and world["admins"] == ["KU_abcdefgh"]
    assert world["mods"][0]["id"] == "378160973"
    root = service.store.world_path(identifier)
    assert (root / "Master" / SESSION).read_bytes() == b"imported surface"
    assert (root / "Caves" / SESSION).read_bytes() == b"imported caves"
    assert "untrusted" not in (root / "Master/modoverrides.lua").read_text()
    parser = configparser.ConfigParser()
    parser.read(root / "Caves/server.ini")
    assert parser["SHARD"]["id"] == "371"
    assert parser["NETWORK"]["server_port"] == "11000"
    backup = service.backup_path(identifier, result["safety_backup_id"])
    assert (backup / "cluster/Master" / SESSION).read_text() == "old progress"
    assert not list(service.store.clusters.glob(".import-*"))
    service.restore(identifier, result["safety_backup_id"])
    assert (root / "Master" / SESSION).read_text() == "old progress"
    assert service.store.get(identifier)["shard_ids"]["Caves"] == "2"


def test_surface_only_import_removes_old_caves_and_old_session_files(service, tmp_path):
    identifier = existing_world(service)
    root = service.store.world_path(identifier)
    atomic_write(root / "Master/save/session/OLDSESSION/old-file", "remove old data")
    archive = local_zip(tmp_path, caves=False)
    service.import_save(identifier, archive, "Camp")
    assert not service.store.get(identifier)["caves"]
    assert not (root / "Caves").exists()
    assert not (root / "Master/save/session/OLDSESSION").exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/Windows/file", "Master/../../escape",
                                 "Master\\..\\escape", "Master/save/NUL", "Master/save/file:stream"])
def test_unsafe_archive_never_stops_or_changes_world(service, tmp_path, name):
    identifier = existing_world(service)
    service.start(identifier)
    archive = local_zip(tmp_path, extra={name: "bad"})
    with pytest.raises(PanelError, match="unsafe"):
        service.import_save(identifier, archive, "Camp")
    assert service.runtime.events == ["start"]
    assert service.backups(identifier) == []
    assert (service.store.world_path(identifier) / "Master" / SESSION).read_text() == "old progress"
    assert not list(service.store.clusters.glob(".import-*"))


@pytest.mark.parametrize("extra, message", [
    ({"Cluster_2/Master/" + SESSION: "another world"}, "multiple worlds"),
    ({"Cluster_1/Master/" + SESSION.lower(): "case collision"}, "duplicate"),
    ({"Cluster_1/Master/save": "file blocks directory"}, "conflicting"),
    ({"Cluster_1/Caves.zip": "nested zip"}, "Steam Cloud"),
    ({"Cluster_1/ExtraCaves/save/session/123/0001": "other shard"}, "standard Master"),
])
def test_ambiguous_or_incomplete_layout_rejected(service, tmp_path, extra, message):
    identifier = existing_world(service)
    archive = local_zip(tmp_path, extra=extra)
    with pytest.raises(PanelError, match=message):
        service.import_save(identifier, archive, "Camp")
    assert service.backups(identifier) == []


def test_empty_caves_rejected(service, tmp_path):
    identifier = existing_world(service)
    archive = local_zip(tmp_path, caves=False, extra={"Cluster_1/Caves/save/": ""})
    with pytest.raises(PanelError, match="no saved session"):
        service.import_save(identifier, archive, "Camp")


def test_symlinks_rejected(service, tmp_path):
    identifier = existing_world(service)
    archive = local_zip(tmp_path)
    link = ZipInfo("Cluster_1/Master/save/session/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(archive, "a") as zipped:
        zipped.writestr(link, "/etc/passwd")
    with pytest.raises(PanelError, match="symbolic link"):
        service.import_save(identifier, archive, "Camp")


@pytest.mark.parametrize("limit, value, message", [("MAX_EXPANDED_BYTES", 1, "expanded size"),
                                                  ("MAX_MEMBER_BYTES", 1, "per-file"),
                                                  ("MAX_MEMBERS", 1, "entries")])
def test_archive_resource_limits(service, tmp_path, monkeypatch, limit, value, message):
    identifier = existing_world(service)
    archive = local_zip(tmp_path)
    monkeypatch.setattr(save_import, limit, value)
    with pytest.raises(PanelError, match=message):
        service.import_save(identifier, archive, "Camp")


def test_corrupt_save_crc_fails_before_stop(service, tmp_path):
    identifier = existing_world(service)
    archive = local_zip(tmp_path)
    data = bytearray(archive.read_bytes())
    offset = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, offset + 16, 0)  # Wrong central-directory CRC.
    archive.write_bytes(data)
    service.start(identifier)
    with pytest.raises(PanelError, match="damaged"):
        service.import_save(identifier, archive, "Camp")
    assert service.runtime.events == ["start"]


def test_failed_install_restores_original_and_restarts(service, tmp_path, monkeypatch):
    identifier = existing_world(service)
    archive = local_zip(tmp_path)
    service.start(identifier)

    def fail_write(world):
        raise OSError("disk full")

    monkeypatch.setattr(service.store, "write_world", fail_write)
    with pytest.raises(OSError, match="disk full"):
        service.import_save(identifier, archive, "Camp")
    assert service.runtime.active(identifier)
    assert (service.store.world_path(identifier) / "Master" / SESSION).read_text() == "old progress"
    assert len(service.backups(identifier)) == 1


def test_failed_shutdown_keeps_original_and_cleans_staging(service, tmp_path):
    identifier = existing_world(service)
    service.start(identifier)
    service.runtime.fail_stop = True
    with pytest.raises(PanelError, match="timed out"):
        service.import_save(identifier, local_zip(tmp_path), "Camp")
    assert service.runtime.active(identifier)
    assert service.backups(identifier) == []
    assert not list(service.store.clusters.glob(".import-*"))


def test_upload_api_and_cleanup(client, auth, service, tmp_path):
    identifier = existing_world(service)
    archive = local_zip(tmp_path)
    response = client.post(f"/api/worlds/{identifier}/import-save", headers=auth, data={
        "confirmation": "Camp", "save_zip": (io.BytesIO(archive.read_bytes()), "local.zip")})
    assert response.status_code == 202
    assert completed(service)["state"] == "completed"
    assert not list(service.store.root.glob(".upload-*"))
    assert not service.runtime.active()


def test_upload_route_auth_confirmation_and_file_required(client, auth, service):
    identifier = existing_world(service)
    url = f"/api/worlds/{identifier}/import-save"
    assert client.post(url).status_code == 403
    assert client.post(url, headers=auth, data={"confirmation": "wrong"}).status_code == 400
    assert client.post(url, headers=auth, data={"confirmation": "Camp"}).status_code == 400
    assert not service.jobs


def test_busy_operation_rejects_upload(client, auth, service):
    identifier = existing_world(service)
    release = threading.Event()
    service.submit("Busy", lambda: release.wait(3))
    try:
        assert client.post(f"/api/worlds/{identifier}/import-save", headers=auth).status_code == 409
    finally:
        release.set()
    completed(service)
    assert not list(service.store.root.glob(".upload-*"))


def test_invalid_zip_job_reports_error_and_removes_upload(client, auth, service):
    identifier = existing_world(service)
    response = client.post(f"/api/worlds/{identifier}/import-save", headers=auth, data={
        "confirmation": "Camp", "save_zip": (io.BytesIO(b"not a zip"), "bad.zip")})
    assert response.status_code == 202
    assert completed(service)["state"] == "failed"
    assert not list(service.store.root.glob(".upload-*"))


def test_upload_body_can_exceed_json_limit(client, auth, service):
    identifier = existing_world(service)
    import os
    content = zip_bytes({"Master/" + SESSION: os.urandom(300000)})
    response = client.post(f"/api/worlds/{identifier}/import-save", headers=auth, data={
        "confirmation": "Camp", "save_zip": (io.BytesIO(content), "save.zip")})
    assert response.status_code == 202
    assert completed(service)["state"] == "completed"


def test_import_inherits_uploaded_mods_and_options_by_default(client, auth, service, tmp_path):
    identifier = existing_world(service)
    imported = {"workshop-123456789": {"enabled": True, "configuration_options": {"DIFFICULTY": 2}},
                "workshop-987654321": {"enabled": False, "configuration_options": {"mode": "off"}}}
    content = "return " + lua(imported)
    archive = local_zip(tmp_path, extra={"Cluster_1/Master/modoverrides.lua": content,
                                         "Cluster_1/Caves/modoverrides.lua": content})
    response = client.post(f"/api/worlds/{identifier}/import-save", headers=auth, data={
        "confirmation": "Camp", "save_zip": (io.BytesIO(archive.read_bytes()), "local.zip")})
    assert response.status_code == 202
    job = completed(service)
    assert job["state"] == "completed"
    assert job["result"]["mods_inherited"] and job["result"]["mod_count"] == 2
    mods = service.store.get(identifier)["mods"]
    assert mods == [{"id": "123456789", "enabled": True, "options": {"DIFFICULTY": 2}},
                    {"id": "987654321", "enabled": False, "options": {"mode": "off"}}]
    service.store.write_mod_setup(service.store.get(identifier))
    setup = (service.store.game / "mods/dedicated_server_mods_setup.lua").read_text()
    assert 'ServerModSetup("123456789")' in setup and "987654321" not in setup
    for shard in ("Master", "Caves"):
        saved = (service.store.world_path(identifier) / shard / "modoverrides.lua").read_text()
        assert 'DIFFICULTY' in saved and '123456789' in saved
    service.restore(identifier, job["result"]["safety_backup_id"])
    assert service.store.get(identifier)["mods"][0]["id"] == "378160973"


def test_conflicting_mod_files_fail_without_stopping_world(service, tmp_path):
    identifier = existing_world(service)
    service.start(identifier)
    archive = local_zip(tmp_path, extra={
        "Cluster_1/Master/modoverrides.lua": 'return {["workshop-123456789"]={enabled=true}}',
        "Cluster_1/Caves/modoverrides.lua": 'return {}'})
    with pytest.raises(PanelError, match="different mod settings"):
        service.import_save(identifier, archive, "Camp")
    assert service.runtime.events == ["start"]
    assert service.backups(identifier) == []


def test_upload_can_opt_out_of_inheritance(client, auth, service, tmp_path):
    identifier = existing_world(service)
    archive = local_zip(tmp_path, extra={"Cluster_1/Master/modoverrides.lua": "return some_function()"})
    response = client.post(f"/api/worlds/{identifier}/import-save", headers=auth, data={
        "confirmation": "Camp", "inherit_mods": "false",
        "save_zip": (io.BytesIO(archive.read_bytes()), "local.zip")})
    assert response.status_code == 202
    assert completed(service)["state"] == "completed"
    assert service.store.get(identifier)["mods"][0]["id"] == "378160973"


def test_absent_mod_settings_inherits_vanilla_world(service, tmp_path):
    identifier = existing_world(service)
    service.import_save(identifier, local_zip(tmp_path), "Camp")
    assert service.store.get(identifier)["mods"] == []


def test_upload_byte_limit_cleans_temporary_files(client, auth, service, monkeypatch):
    from pera_panel import web
    monkeypatch.setattr(web, "MAX_UPLOAD_BYTES", 10)
    identifier = existing_world(service)
    response = client.post(f"/api/worlds/{identifier}/import-save", headers=auth, data={
        "confirmation": "Camp", "save_zip": (io.BytesIO(b"x" * 11), "large.zip")})
    assert response.status_code == 413
    assert not list(service.store.root.glob(".upload-*"))
    assert not service.jobs
