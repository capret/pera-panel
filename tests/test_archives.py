from pathlib import Path
import shutil
import threading

import pytest

from pera_panel.storage import PanelError, atomic_write
from test_web import wait_job


def archived(service, name="Old camp"):
    identifier = service.create({"name": name, "token": "private-token", "password": "private-password"})["world_id"]
    atomic_write(service.store.world_path(identifier) / "Master/save/session/day", "saved progress")
    atomic_write(service.store.logs / identifier / "Master.log", "old log")
    service.delete(identifier, name)
    return identifier, next((service.store.root / "deleted").glob(identifier + "-*"))


def test_lists_existing_archives_without_exposing_world_secrets(client, auth, service):
    identifier, _ = archived(service)
    response = client.get("/api/archived-worlds")
    assert response.status_code == 200
    row = response.json["worlds"][0]
    assert row["id"] == identifier and row["name"] == "Old camp"
    assert row["copies"] == row["backup_count"] == 1
    assert set(row) == {"id", "name", "copies", "backup_count", "size_mb"}
    assert "private-token" not in response.text and "private-password" not in response.text
    assert service.store.all() == []
    assert b'id="open-archives"' in client.get("/").data


def test_delete_removes_all_copies_backups_logs_and_preserves_other_worlds(service):
    identifier, path = archived(service)
    copy = path.with_name(identifier + "-aabbccdd")
    shutil.copytree(path, copy)
    other, other_path = archived(service, "Keep me")
    live = service.create({"name": "Live camp"})["world_id"]
    assert next(row for row in service.archives.list() if row["id"] == identifier)["copies"] == 2
    service.delete_archived(identifier, "Old camp")
    assert not path.exists() and not copy.exists()
    assert not (service.store.backups / identifier).exists()
    assert not (service.store.logs / identifier).exists()
    assert other_path.exists() and (service.store.backups / other).exists()
    assert service.store.get(live)["name"] == "Live camp"
    assert [row["id"] for row in service.archives.list()] == [other]


@pytest.mark.parametrize("confirmation", [None, "", "old camp", "Old camp "])
def test_confirmation_failure_changes_nothing(service, confirmation):
    identifier, path = archived(service)
    with pytest.raises(PanelError, match="exactly"):
        service.delete_archived(identifier, confirmation)
    assert (path / "Master/save/session/day").read_text() == "saved progress"
    assert (service.store.backups / identifier).exists() and (service.store.logs / identifier).exists()


@pytest.mark.parametrize("identifier", ["..", "../deleted", "a" * 12 + "/..", "A" * 12, "a" * 11, "a" * 12])
def test_invalid_or_unknown_id_cannot_delete_other_data(service, identifier):
    _, path = archived(service)
    with pytest.raises(PanelError):
        service.delete_archived(identifier, "Old camp")
    assert path.exists()


def test_refuses_world_restored_to_dashboard_or_still_running(service):
    identifier, path = archived(service)
    shutil.copytree(path, service.store.world_path(identifier))
    with pytest.raises(PanelError, match="dashboard"):
        service.delete_archived(identifier, "Old camp")
    service.start(identifier)
    with pytest.raises(PanelError, match="Stop"):
        service.delete_archived(identifier, "Old camp")
    assert path.exists() and (service.store.backups / identifier).exists()


@pytest.mark.parametrize("location", ["archive", "save", "backups", "logs", "deleted"])
def test_link_preflight_refuses_before_any_deletion(service, monkeypatch, location):
    identifier, path = archived(service)
    linked = {"archive": path, "save": path / "Master/save/session/day",
              "backups": service.store.backups / identifier, "logs": service.store.logs,
              "deleted": service.store.root / "deleted"}[location]
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == linked or original(p))
    with pytest.raises(PanelError, match="Linked"):
        service.delete_archived(identifier, "Old camp")
    assert path.exists() and (service.store.backups / identifier).exists()
    assert (service.store.logs / identifier).exists()


def test_real_symlink_does_not_touch_external_files(service, tmp_path):
    identifier, path = archived(service)
    outside = tmp_path / "outside"
    atomic_write(outside / "keep.txt", "keep this")
    try:
        (path / "outside-link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating filesystem symlinks requires privileges on this host")
    with pytest.raises(PanelError, match="Linked"):
        service.delete_archived(identifier, "Old camp")
    assert (outside / "keep.txt").read_text() == "keep this"
    assert (service.store.backups / identifier).exists()


def test_damaged_metadata_uses_id_as_confirmation(service):
    identifier, path = archived(service)
    atomic_write(path / "pera.json", "broken json")
    assert service.archives.list()[0]["name"] == identifier
    service.delete_archived(identifier, identifier)
    assert service.archives.list() == []


def test_archive_api_auth_csrf_jobs_and_completion(client, auth, service):
    identifier, path = archived(service)
    url = "/api/archived-worlds/" + identifier
    assert client.delete(url, json={"confirmation": "Old camp"}).status_code == 403
    response = client.delete(url, json={"confirmation": "wrong"}, headers=auth)
    assert response.status_code == 202
    assert wait_job(service)["state"] == "failed" and path.exists()
    response = client.delete(url, json={"confirmation": "Old camp"}, headers=auth)
    assert response.status_code == 202
    assert wait_job(service)["state"] == "completed"
    assert client.get("/api/archived-worlds").json == {"worlds": []}
    client.post("/api/logout", headers=auth)
    assert client.get("/api/archived-worlds").status_code == 401
    assert client.delete(url, json={"confirmation": "Old camp"}, headers=auth).status_code == 401


def test_archive_mutations_and_listing_wait_for_other_jobs(client, auth, service):
    identifier, path = archived(service)
    release = threading.Event()
    service.submit("Test busy operation", lambda: release.wait(timeout=5))
    try:
        assert client.get("/api/archived-worlds").status_code == 409
        assert client.delete("/api/archived-worlds/" + identifier,
                             json={"confirmation": "Old camp"}, headers=auth).status_code == 409
        assert path.exists()
    finally:
        release.set()
        wait_job(service)
