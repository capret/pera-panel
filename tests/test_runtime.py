import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

from pera_panel.runtime import Runtime
from pera_panel.storage import PanelError, Store, atomic_write, validate_world


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "data", tmp_path / "game")
    fake = tmp_path / "fake_dst.py"
    atomic_write(fake, '''import pathlib, sys
if "-only_update_server_mods" in sys.argv:
    print("Workshop update complete", flush=True)
    sys.exit(0)
print("Booted fake DST; token private-test-token", flush=True)
for line in sys.stdin:
    print(line.strip(), flush=True)
    if line.startswith("c_shutdown"):
        print("Shutting down", flush=True)
        if pathlib.Path(sys.argv[1]).with_suffix(".wait-eof").exists():
            sys.stdin.read()
            print("Console EOF received; exiting", flush=True)
        pathlib.Path(sys.argv[1]).write_text("saved")
        break
''')

    class FakeBinaryRuntime(Runtime):
        def binary(self):
            return fake

        def arguments(self, world, shard):
            return [sys.executable, "-u", str(fake), str(tmp_path / (shard + ".saved"))]

    instance = FakeBinaryRuntime(store)
    yield instance
    instance.shutdown()


def test_real_pipes_paired_start_save_announce_and_graceful_stop(runtime, tmp_path):
    world = validate_world({"name": "Fake", "token": "private-test-token", "caves": True})
    runtime.store.write_world(world)
    runtime.start(world)
    assert runtime.status(world["id"])["state"] == "running"
    assert len(runtime.status(world["id"])["shards"]) == 2
    runtime.command(world["id"], "save")
    runtime.command(world["id"], "announce", 'Hello "survivors"!')
    with pytest.raises(PanelError, match="Stop the active"):
        runtime.start(world)
    runtime.stop()
    for shard in ("Master", "Caves"):
        assert (tmp_path / f"{shard}.saved").read_text() == "saved"
        log = runtime.log(world["id"], shard)
        assert "c_save()" in log
        assert "private-test-token" not in log
        assert "[redacted]" in log
    assert "c_announce" in runtime.log(world["id"], "Master")
    assert not runtime.active()


def test_mod_update_finishes_before_shards_start(runtime):
    world = validate_world({"name": "Mods", "token": "abc", "mods": [{"id":"378160973"}]})
    runtime.store.write_world(world)
    runtime.start(world)
    assert "Workshop update complete" in runtime.log(world["id"], "Mods")
    assert runtime.updater is None
    assert runtime.active()


def test_missing_token_rejected_before_spawn(runtime):
    with pytest.raises(PanelError, match="token"):
        runtime.start(validate_world({"name": "No token"}))
    assert not runtime.processes


def test_native_rollback_sends_only_to_master_and_keeps_processes(runtime):
    world = validate_world({"name": "Native", "token": "abc", "caves": True})
    runtime.store.write_world(world)
    runtime.start(world)
    pids = {shard: process.pid for shard, process in runtime.processes.items()}
    result = runtime.rollback(world["id"], 2)
    assert "requested" in result["message"]
    assert {shard: process.pid for shard, process in runtime.processes.items()} == pids
    assert runtime.active(world["id"])
    assert not list(runtime.store.backups.rglob("backup.json"))
    runtime.stop()
    assert "c_rollback(2)" in runtime.log(world["id"], "Master")
    assert "c_rollback" not in runtime.log(world["id"], "Caves")


@pytest.mark.parametrize("count", [0, -1, 51, True, "1", "1);os.execute('bad')", None])
def test_native_rollback_rejects_invalid_counts(runtime, count):
    world = validate_world({"name": "Native"})
    runtime.store.write_world(world)
    with pytest.raises(PanelError, match="rollback count"):
        runtime.rollback(world["id"], count)


def test_native_rollback_rejects_missing_configured_shards(runtime):
    world = validate_world({"name": "Native", "caves": True})
    runtime.store.write_world(world)
    runtime.world_id = world["id"]
    process = MagicMock()
    process.poll.return_value = None
    runtime.processes = {"Master": process}
    with pytest.raises(PanelError, match="all configured shards"):
        runtime.rollback(world["id"], 1)
    process.stdin.write.assert_not_called()
    runtime.processes = {}


def test_stop_timeout_does_not_force_kill_in_normal_operations(runtime):
    process = MagicMock()
    process.poll.return_value = None
    process.stdin.closed = False
    process.wait.side_effect = subprocess.TimeoutExpired("fake", 60)
    runtime.processes = {"Master": process}
    with pytest.raises(PanelError, match="No backup or restore"):
        runtime.stop()
    process.kill.assert_not_called()
    runtime.processes = {}


def test_stop_delivers_eof_before_waiting_for_console_reader_exit(runtime, tmp_path, monkeypatch):
    world = validate_world({"name": "EOF shutdown", "token": "abc", "caves": True})
    for shard in ("Master", "Caves"):
        (tmp_path / f"{shard}.wait-eof").touch()
    runtime.store.write_world(world)
    runtime.start(world)
    processes = list(runtime.processes.values())
    for process in processes:
        wait = process.wait
        monkeypatch.setattr(process, "wait", lambda timeout=None, wait=wait: wait(timeout=min(timeout or 3, 3)))
    started = time.monotonic()
    runtime.stop()
    assert time.monotonic() - started < 5
    assert not runtime.active()
    for shard in ("Master", "Caves"):
        assert "Console EOF received; exiting" in runtime.log(world["id"], shard)
        assert (tmp_path / f"{shard}.saved").read_text() == "saved"
    assert all(p.stdin.closed and p.returncode == 0 for p in processes)


def test_console_action_rejected_after_shutdown_input_is_closed(runtime):
    world = validate_world({"name": "Exiting", "token": "abc", "caves": False})
    runtime.store.write_world(world)
    process = MagicMock()
    process.poll.return_value = None
    process.stdin.closed = True
    runtime.world_id = world["id"]
    runtime.processes = {"Master": process}
    with pytest.raises(PanelError, match="console disconnected"):
        runtime.command(world["id"], "save")
    runtime.request_players(world["id"])
    process.stdin.write.assert_not_called()
    runtime.processes = {}
