"""Update handoff, privilege boundary, and restart recovery without host updates."""
import importlib.util
import io
import json
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

from pera_panel.maintenance import Maintenance
from pera_panel.storage import PanelError

spec = importlib.util.spec_from_file_location("update_helper", Path(__file__).parents[1] / "deploy/maintenance.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@pytest.fixture
def updater_client(service, monkeypatch):
    backend = {"job": None}

    def request(data):
        if data["operation"] == "start":
            if backend["job"] and backend["job"]["state"] == "running":
                return {**backend, "error": "busy"}
            backend["job"] = {"id": "update1", "state": "running", "target": data["target"], "log": "Starting"}
        return backend.copy()

    monkeypatch.setattr(service.maintenance, "_request", request)
    return backend


def test_updates_require_auth_csrf_and_exact_target(client, auth, updater_client):
    assert client.post("/api/maintenance", json={"target": "game"}).status_code == 403
    for body in ({}, {"target": "reinstall"}, {"target": []}, {"target": "panel; id"},
                 {"target": "panel", "repo": "https://example.com"}):
        assert client.post("/api/maintenance", json=body, headers=auth).status_code == 400
    client.post("/api/logout", headers=auth)
    assert client.post("/api/maintenance", json={"target": "game"}, headers=auth).status_code == 401


def test_update_requires_idle_stopped_world_and_no_upload(client, auth, service, updater_client):
    identifier = service.create({"name": "Camp"})["world_id"]
    service.start(identifier)
    assert client.post("/api/maintenance", json={"target": "game"}, headers=auth).status_code == 409
    service.stop(identifier)
    service.upload_lock.acquire()
    try:
        assert client.post("/api/maintenance", json={"target": "panel"}, headers=auth).status_code == 409
    finally:
        service.upload_lock.release()
    service.busy = "other-job"
    assert client.post("/api/maintenance", json={"target": "panel"}, headers=auth).status_code == 409
    service.busy = None
    result = client.post("/api/maintenance", json={"target": "game"}, headers=auth)
    assert result.status_code == 202
    assert result.json["maintenance"]["job"]["target"] == "game"
    assert client.post("/api/worlds", json={"name": "Conflict"}, headers=auth).status_code == 409
    assert client.post(f"/api/worlds/{identifier}/export-save", headers=auth).status_code == 409
    assert client.post(f"/api/worlds/{identifier}/import-save", headers=auth).status_code == 409
    assert not service.upload_lock.locked()
    assert client.post("/api/maintenance", json={"target": "panel"}, headers=auth).status_code == 409
    assert client.get("/api/status").json["maintenance"]["job"]["state"] == "running"
    assert client.get("/healthz").status_code == 200
    assert client.get("/overview").status_code == 200
    updater_client["job"]["log"] = "SteamCMD download: 42%"
    assert client.get("/api/status").json["maintenance"]["job"]["log"] == "SteamCMD download: 42%"
    updater_client["job"]["state"] = "completed"
    assert client.post("/api/worlds", json={"name": "Allowed"}, headers=auth).status_code == 202


def test_missing_helper_and_lost_connection_during_update(service, monkeypatch):
    monkeypatch.setattr(service.maintenance, "_request", Mock(side_effect=OSError("offline")))
    assert not service.maintenance.status()["available"]
    service.maintenance.require_idle()  # Local development / installations without the helper.
    with pytest.raises(PanelError, match="unavailable"):
        service.update_software("game")
    service.maintenance.last_job = {"state": "running"}
    with pytest.raises(PanelError, match="in progress"):
        service.maintenance.require_idle()


@pytest.mark.parametrize("outcome", ["completed", "failed"])
def test_autostart_waits_for_update_health_and_skips_failed_updates(service, updater_client, monkeypatch, outcome):
    service.create({"name": "Boot world", "autostart": True})
    updater_client["job"] = {"state": "running"}
    started, checked = threading.Event(), threading.Event()
    real_wait = service.shutdown_event.wait
    monkeypatch.setattr(service.shutdown_event, "wait", lambda timeout: real_wait(.01))
    monkeypatch.setattr(service, "_autostart", started.set)
    service.autostart()
    assert not started.is_set()
    original = service.maintenance.status

    def status():
        result = original()
        if result["job"]["state"] == outcome:
            checked.set()
        return result

    monkeypatch.setattr(service.maintenance, "status", status)
    updater_client["job"]["state"] = outcome
    assert checked.wait(2)
    if outcome == "completed":
        assert started.wait(2)
    else:
        assert not started.wait(.05)


@pytest.mark.parametrize("payload", [None, [], {}, {"operation": "status", "extra": True},
                                      {"operation": "start", "target": "game; touch /tmp/pwn"},
                                      {"operation": "start", "target": ["game"]},
                                      {"operation": "start", "target": "panel", "args": ["--source", "/tmp"]}])
def test_root_helper_rejects_untrusted_commands(tmp_path, payload):
    acquire = Mock()
    updater = helper.Updater(tmp_path / "job.json", acquire=acquire)
    assert updater.dispatch(payload)["error"]
    acquire.assert_not_called()
    assert not (tmp_path / "job.json").exists()


def test_existing_console_update_prevents_browser_update(tmp_path):
    updater = helper.Updater(tmp_path / "job.json", acquire=Mock(side_effect=BlockingIOError()))
    assert updater.dispatch({"operation": "start", "target": "game"})["error"]
    assert updater.worker is None


@pytest.mark.parametrize("exit_code,recovery_failure", [(0, False), (7, False), (0, True)])
def test_worker_fixed_command_bounded_progress_recovery_and_persistence(tmp_path, monkeypatch, exit_code, recovery_failure):
    gate = threading.Event()
    lock = Mock()
    lock.fileno.return_value = 12
    process = Mock()
    process.stdout = io.BytesIO(("Game progress\n" * 2000).encode())
    process.wait.return_value = exit_code
    process.__enter__ = Mock(side_effect=lambda: (gate.wait(3), process)[1])
    process.__exit__ = Mock(return_value=False)
    launch = Mock(return_value=process)
    monkeypatch.setattr(helper.subprocess, "Popen", launch)
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)
    recovered = []

    def recover():
        assert updater.dispatch({"operation": "status"})["job"]["state"] == "running"
        recovered.append(True)
        if recovery_failure:
            raise RuntimeError("health unavailable")

    updater = helper.Updater(tmp_path / "job.json", recover=recover, acquire=lambda: lock)
    first = updater.dispatch({"operation": "start", "target": "panel"})
    assert first["job"]["state"] == "running"
    assert updater.dispatch({"operation": "start", "target": "game"})["error"]
    gate.set()
    updater.worker.join(5)
    assert not updater.worker.is_alive()
    assert recovered == [True]
    lock.close.assert_called_once()
    args, kwargs = launch.call_args
    assert args[0] == ["/bin/bash", "/usr/local/lib/pera-panel/install.sh", "update-panel", "--yes"]
    assert kwargs["pass_fds"] == (12,)
    assert kwargs["env"]["PERA_MAINTENANCE_LOCK_FD"] == "12"
    assert kwargs["stdin"] == helper.subprocess.DEVNULL
    job = updater.dispatch({"operation": "status"})["job"]
    assert job["state"] == ("completed" if not exit_code and not recovery_failure else "failed")
    assert len(job["log"]) <= 16000
    assert helper.Updater(tmp_path / "job.json").dispatch({"operation": "status"})["job"] == job


def test_interrupted_worker_is_reported_after_restart(tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "old", "state": "running", "log": "Downloading"}))
    updater = helper.Updater(path)
    assert updater.job["state"] == "failed"
    assert "interrupted" in updater.job["log"]


def test_progress_disk_failure_does_not_prevent_panel_recovery_or_lock_release(tmp_path, monkeypatch):
    recovered, lock = Mock(), Mock()
    updater = helper.Updater(tmp_path / "job.json", recover=recovered)
    updater.job = {"state": "running", "log": ""}
    updater.maintenance_lock = lock
    monkeypatch.setattr(updater, "persist", Mock(side_effect=OSError("disk full")))
    monkeypatch.setattr(helper.subprocess, "Popen", Mock(side_effect=OSError("launch failed")))
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)
    updater.run("game")
    recovered.assert_called_once()
    lock.close.assert_called_once()
    assert updater.job["state"] == "failed"
    assert "launch failed" in updater.job["log"]


def test_unix_protocol_and_peer_permissions(tmp_path):
    import socket
    import struct

    if not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("Linux peer credentials")
    updater = helper.Updater(tmp_path / "job.json")
    connection = Mock()
    connection.getsockopt.return_value = struct.pack("3i", 10, 123, 456)
    connection.recv.return_value = b'{"operation":"status"}\n'
    helper.serve_connection(connection, updater, allowed_uid=123)
    assert json.loads(connection.sendall.call_args.args[0]) == {"job": None}
    connection.reset_mock()
    helper.serve_connection(connection, updater, allowed_uid=999)
    connection.recv.assert_not_called()
    connection.sendall.assert_not_called()


def test_client_round_trip_uses_real_local_socket(tmp_path):
    import socket

    if not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("Linux Unix socket integration")
    path = str(tmp_path / "updater.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen(1)

        def respond():
            connection, _ = listener.accept()
            with connection:
                assert json.loads(connection.recv(1024))["operation"] == "status"
                connection.sendall(b'{"job":null}\n')

        thread = threading.Thread(target=respond)
        thread.start()
        assert Maintenance(path).status() == {"available": True, "job": None}
        thread.join(2)


def test_real_maintenance_lock_excludes_second_worker_and_rejects_links(tmp_path):
    pytest.importorskip("fcntl")
    path = tmp_path / "lock"
    with helper.acquire_lock(path):
        with pytest.raises(BlockingIOError):
            helper.acquire_lock(path)
    with helper.acquire_lock(path):
        pass
    linked = tmp_path / "linked-lock"
    linked.symlink_to(path)
    with pytest.raises(OSError):
        helper.acquire_lock(linked)
