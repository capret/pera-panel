"""Pause reports must be game evidence, not settings or a successful pipe write."""
from unittest.mock import MagicMock

import pytest

from pera_panel.runtime import Runtime
from pera_panel.storage import PanelError
from test_web import wait_job


@pytest.fixture
def game(service):
    identifier = service.create({"name": "Pause camp", "caves": True})["world_id"]
    runtime = Runtime(service.store)
    runtime.world_id = identifier
    for name in ("Master", "Caves"):
        process = MagicMock(pid=99999999)
        process.poll.return_value = None
        process.stdin.closed = False
        runtime.processes[name] = process
    return runtime, identifier


def report(runtime, identifier, shard, event):
    runtime._record_pause_state(runtime.processes[shard], identifier, shard, f"[02:17:09]: Server {event}\r\n")


def test_only_current_native_events_change_pause_state(game):
    runtime, identifier = game
    master = runtime.processes["Master"]
    report(runtime, identifier, "Master", "Autopaused")
    for output in (
        '[02:20:20]: [Say] Player: Server Unpaused',
        '[02:20:20]: RemoteCommandInput: "print(\"Server Unpaused\")"',
        '[02:20:20]: Server Unpaused extra',
        'Server Unpaused',
        '[02:20:20]: PauseWhenEmpty: false',
    ):
        runtime._record_pause_state(master, identifier, "Master", output)
    runtime._record_pause_state(MagicMock(), identifier, "Master", '[02:20:20]: Server Unpaused')
    runtime._record_pause_state(master, identifier, "Mods", '[02:20:20]: Server Unpaused')
    runtime._record_pause_state(master, "other-world", "Master", '[02:20:20]: Server Unpaused')
    assert runtime.pause_states == {"Master": "auto_paused"}
    assert runtime.status(identifier)["pause_state"] == "unknown"
    report(runtime, identifier, "Caves", "Autopaused")
    assert runtime.status(identifier)["pause_state"] == "auto_paused"
    report(runtime, identifier, "Master", "Unpaused")
    assert runtime.status(identifier)["pause_state"] == "mixed"
    report(runtime, identifier, "Caves", "Unpaused")
    assert runtime.status(identifier)["pause_state"] == "unpaused"
    report(runtime, identifier, "Master", "Paused")
    report(runtime, identifier, "Caves", "Autopaused")
    assert runtime.status(identifier)["pause_state"] == "paused"
    assert runtime.status("other-world")["pause_state"] == "stopped"
    for _ in range(5):
        runtime.status(identifier)
    for process in runtime.processes.values():
        process.stdin.write.assert_not_called()
    master.poll.return_value = 1
    assert runtime.status(identifier)["pause_state"] == "partial"
    assert runtime.status(identifier)["shards"][0]["pause_state"] == "stopped"


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_commands_require_all_configured_shards_and_correct_world(game, action):
    runtime, identifier = game
    runtime.processes["Caves"].poll.return_value = 1
    with pytest.raises(PanelError, match="Start all configured shards"):
        runtime.command(identifier, action)
    runtime.processes["Caves"].poll.return_value = None
    runtime.world_id = "other-world"
    with pytest.raises(PanelError, match="Start all configured shards"):
        runtime.command(identifier, action)
    for process in runtime.processes.values():
        process.stdin.write.assert_not_called()


def test_surface_only_world_does_not_require_caves(game):
    runtime, identifier = game
    world = runtime.store.get(identifier)
    runtime.store.write_world({**world, "caves": False})
    del runtime.processes["Caves"]
    runtime.processes["Master"].stdin.write.side_effect = lambda _: report(runtime, identifier, "Master", "Paused")
    runtime.command(identifier, "pause")
    assert runtime.status(identifier)["pause_state"] == "paused"


def test_silent_game_times_out_without_optimistic_status(game, monkeypatch):
    runtime, identifier = game
    monkeypatch.setattr("pera_panel.runtime.PAUSE_CONFIRM_TIMEOUT", .01)
    report(runtime, identifier, "Master", "Unpaused")
    report(runtime, identifier, "Caves", "Unpaused")
    with pytest.raises(PanelError, match="DST did not confirm"):
        runtime.command(identifier, "pause")
    assert runtime.status(identifier)["pause_state"] == "unpaused"
    for process in runtime.processes.values():
        process.stdin.write.assert_called_once_with("TheNet:SetServerPaused(true)\n")


def test_confirmation_required_from_both_shards(game, monkeypatch):
    runtime, identifier = game
    monkeypatch.setattr("pera_panel.runtime.PAUSE_CONFIRM_TIMEOUT", .01)
    runtime.processes["Master"].stdin.write.side_effect = lambda _: report(runtime, identifier, "Master", "Paused")
    with pytest.raises(PanelError, match="DST did not confirm"):
        runtime.command(identifier, "pause")
    assert runtime.pause_states == {"Master": "paused"}
    assert runtime.status(identifier)["pause_state"] == "unknown"


def test_exit_while_waiting_is_reported(game):
    runtime, identifier = game
    runtime.processes["Caves"].stdin.flush.side_effect = lambda: setattr(runtime.processes["Caves"].poll, "return_value", 1)
    with pytest.raises(PanelError, match="A shard stopped"):
        runtime.command(identifier, "pause")


def test_resume_respects_automatic_pause_and_is_idempotent(game):
    runtime, identifier = game
    for name in runtime.processes:
        report(runtime, identifier, name, "Paused")
    runtime.command(identifier, "pause")
    for process in runtime.processes.values():
        process.stdin.write.assert_not_called()
    for name, process in runtime.processes.items():
        process.stdin.write.side_effect = lambda _, name=name: report(runtime, identifier, name, "Autopaused")
    result = runtime.command(identifier, "resume")
    assert "automatic pause" in result["message"]
    assert runtime.status(identifier)["pause_state"] == "auto_paused"
    runtime.command(identifier, "resume")
    for process in runtime.processes.values():
        process.stdin.write.assert_called_once_with("TheNet:SetServerPaused(false)\n")


def test_pause_routes_are_authenticated_and_queued(client, auth, service):
    identifier = service.create({"name": "Camp"})["world_id"]
    for action in ("pause", "resume"):
        path = f"/api/worlds/{identifier}/actions/{action}"
        assert client.post(path, json={}).status_code == 403
        response = client.post(path, json={}, headers=auth)
        assert response.status_code == 202
        assert response.json["job"]["title"] == f"{action.title()} world"
        assert wait_job(service)["state"] == "completed"
        assert service.runtime.events[-1] == (action, "")
    client.post("/api/logout", headers=auth)
    assert client.post(f"/api/worlds/{identifier}/actions/pause", json={}, headers=auth).status_code == 401


def test_unconfirmed_pause_is_a_failed_api_job(client, auth, service, game, monkeypatch):
    runtime, identifier = game
    monkeypatch.setattr("pera_panel.runtime.PAUSE_CONFIRM_TIMEOUT", .01)
    service.runtime = runtime
    response = client.post(f"/api/worlds/{identifier}/actions/pause", json={}, headers=auth)
    assert response.status_code == 202
    job = wait_job(service)
    assert job["state"] == "failed"
    assert "DST did not confirm" in job["error"]
    world = next(w for w in client.get('/api/status').json['worlds'] if w['id'] == identifier)
    assert world['runtime']['pause_state'] == 'unknown'
