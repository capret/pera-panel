"""Own the game processes as an unprivileged user, with bounded logs and graceful shutdown."""
from collections import deque
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import secrets
import subprocess
import threading
import time

import psutil

from .storage import PanelError, lua
from .players import PlayerRegistry, USER_ID, FOLDER


class Runtime:
    def __init__(self, store):
        self.store = store
        self.processes = {}
        self.readers = {}
        self.world_id = None
        self.started_at = None
        self.lock = threading.RLock()
        self.updater = None
        self.players = PlayerRegistry(store)
        self.probes = {}
        self.probe_at = 0

    def binary(self):
        binary = self.store.game / "bin64" / "dontstarve_dedicated_server_nullrenderer_x64"
        if not binary.is_file():
            raise PanelError("DST is not installed. Run sudo pera-panel update-game on the server.", 409)
        return binary

    def arguments(self, world, shard):
        return [str(self.binary()), "-console", "-persistent_storage_root", str(self.store.root),
                "-conf_dir", "clusters", "-cluster", world["id"], "-shard", shard,
                "-monitor_parent_process", str(os.getpid()), "-backup_log_count", "3"]

    def _spawn(self, args, world, shard):
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(self.binary().parent / "lib64") + ":" + str(self.binary().parent)
        # Do not pass panel secrets to game processes or mods.
        for key in tuple(env):
            if key.startswith("PERA_"):
                env.pop(key)
        process = subprocess.Popen(args, cwd=self.binary().parent, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1)
        path = self.store.logs / world["id"] / f"{shard}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        reader = threading.Thread(target=self._read_log,
                                  args=(process, path, [world["token"], world["cluster_key"], world["password"]],
                                        world["id"], shard),
                                  daemon=True)
        reader.start()
        self.readers[process.pid] = reader
        return process

    def _read_log(self, process, path, secrets, identifier, shard):
        handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        try:
            for output in iter(lambda: process.stdout.readline(65536), ""):
                try:
                    self.players.log_line(identifier, output)
                    self._player_response(identifier, shard, output)
                except (OSError, ValueError):
                    logging.exception("Could not record player discovery")
                for secret in secrets:
                    if secret:
                        output = output.replace(secret, "[redacted]")
                handler.emit(logging.LogRecord("dst", logging.INFO, "", 0, output.rstrip(), (), None))
        finally:
            process.stdout.close()
            handler.close()

    def active(self, identifier=None):
        with self.lock:
            return (identifier is None or identifier == self.world_id) and any(
                process.poll() is None for process in self.processes.values())

    def update_mods(self, world):
        self.store.write_mod_setup(world)
        if not any(mod["enabled"] for mod in world["mods"]):
            return
        # Update sequentially so two Steam Workshop clients do not write shared mods together.
        for shard in (["Master", "Caves"] if world["caves"] else ["Master"]):
            process = self._spawn(self.arguments(world, shard) + ["-only_update_server_mods"], world, "Mods")
            with self.lock:
                self.updater = process
            try:
                try:
                    code = process.wait(timeout=900)
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.wait()
                    raise PanelError("Mod download timed out. Check the Mods log and retry.", 502) from exc
                if code != 0:
                    raise PanelError("Mod download failed. Check the Mods log.", 502)
            finally:
                if process.stdin:
                    process.stdin.close()
                self.readers.pop(process.pid).join(timeout=3)
                with self.lock:
                    self.updater = None

    def start(self, world):
        if self.active():
            raise PanelError("Stop the active world first. One world can run at a time.", 409)
        if not world["token"]:
            raise PanelError("Add your Klei cluster token in world settings before starting.")
        # Reap any exited processes/readers from an earlier failed world.
        if self.processes:
            self.stop()
        self.binary()
        # Metadata is authoritative; recover from an interrupted settings write before launching.
        self.store.write_world(world)
        self.update_mods(world)
        with self.lock:
            self.processes = {}
            self.world_id = world["id"]
            self.started_at = time.time()
            self.probes = {}
            self.probe_at = 0
            try:
                for shard in (["Master", "Caves"] if world["caves"] else ["Master"]):
                    self.processes[shard] = self._spawn(
                        self.arguments(world, shard) + ["-skip_update_server_mods"], world, shard)
            except Exception:
                self.stop(force=True)
                raise
        time.sleep(1)
        if any(process.poll() is not None for process in self.processes.values()):
            self.stop(force=True)
            raise PanelError("A shard exited during startup. Inspect its log for the cause.", 502)

    @staticmethod
    def _send(process, command, required=False):
        if process.poll() is not None:
            if required:
                raise PanelError("The game console disconnected. Inspect the shard log.", 409)
            return
        try:
            process.stdin.write(command + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise PanelError("The game console disconnected. Inspect the shard log.", 409) from exc

    def command(self, identifier, action, message=""):
        with self.lock:
            if not self.active(identifier):
                raise PanelError("This world is stopped.", 409)
            if action == "save":
                for process in self.processes.values():
                    self._send(process, "c_save()")
            elif action == "announce":
                master = self.processes.get("Master")
                if not master or master.poll() is not None:
                    raise PanelError("The surface shard is stopped.", 409)
                self._send(master, f"c_announce({lua(message)})")
            else:
                raise PanelError("Unknown console action.")

    def rollback(self, identifier, count):
        with self.lock:
            world = self.store.get(identifier)
            if type(count) is not int or not 1 <= count <= world["snapshots"]:
                raise PanelError("Choose a rollback count between 1 and {maximum}.", params={"maximum": world["snapshots"]})
            expected = ["Master", "Caves"] if world["caves"] else ["Master"]
            if self.world_id != identifier or any(shard not in self.processes or self.processes[shard].poll() is not None
                                                  for shard in expected):
                raise PanelError("Start all configured shards before requesting native rollback.", 409)
            self._send(self.processes["Master"], f"c_rollback({count})", required=True)
        return {"message": "Native rollback requested. Check the game logs for completion; DST reloads the world and players may reconnect."}

    def request_players(self, identifier):
        with self.lock:
            if not self.active(identifier) or time.monotonic() - self.probe_at < 10:
                return
            self.probe_at = time.monotonic()
            for shard, process in self.processes.items():
                if process.poll() is not None:
                    continue
                marker = "PERA_PLAYERS_" + secrets.token_hex(16)
                self.probes[(identifier, shard)] = marker
                # Only game-generated responses with the current random marker can provide path mappings.
                command = ('local p={} for _,v in ipairs(TheNet:GetClientTable() or {}) do '
                           'if v.performance==nil and #p<64 then '
                           'local ok,f=pcall(function() return TheNet:GetDefaultEncodeUserPath() '
                           'and TheNet:EncodeUserPath(v.userid) or v.userid end) '
                           'p[#p+1]={userid=v.userid,name=string.sub(v.name or "",1,100),folder=ok and f or ""} '
                           'end end print("' + marker + '"..json.encode(p))')
                self._send(process, command)

    def _player_response(self, identifier, shard, output):
        marker = self.probes.get((identifier, shard))
        if not marker:
            return
        output = re.sub(r"^\[[0-9:.]+\]:\s*", "", output).rstrip("\r\n")
        if not output.startswith(marker):
            return
        try:
            rows = json.loads(output[len(marker):])
        except ValueError:
            return
        if not isinstance(rows, list) or len(rows) > 64:
            return
        players = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("userid"), str) or not USER_ID.fullmatch(row["userid"]):
                continue
            folder = row.get("folder", "")
            if not isinstance(folder, str) or not FOLDER.fullmatch(folder):
                folder = ""
            players.append({"userid": row["userid"], "name": row.get("name", ""), "source": "console",
                            "folder": folder, "shard": shard})
        self.players.record_many(identifier, players)

    def stop(self, force=False):
        with self.lock:
            processes = list(self.processes.values())
        for process in processes:
            try:
                self._send(process, "c_shutdown(true)")
            except PanelError:
                pass
        deadline = time.monotonic() + 60
        for process in processes:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                if not force:
                    raise PanelError("Graceful shutdown timed out. No backup or restore was performed. "
                                     "Inspect the logs, then retry Stop.", 409) from exc
                logging.error("Shard %s required forced termination; its final save is not guaranteed.", process.pid)
                process.kill()
                process.wait(timeout=10)
            if process.stdin:
                process.stdin.close()
            reader = self.readers.pop(process.pid, None)
            if reader:
                reader.join(timeout=3)
        with self.lock:
            self.processes = {}
            self.world_id = None
            self.started_at = None

    def status(self, identifier):
        with self.lock:
            shards = []
            if self.world_id == identifier:
                for name, process in self.processes.items():
                    code = process.poll()
                    memory = 0
                    if code is None:
                        try:
                            memory = psutil.Process(process.pid).memory_info().rss
                        except psutil.Error:
                            pass
                    shards.append({"name": name, "running": code is None, "pid": process.pid,
                                   "exit_code": code, "memory_mb": round(memory / 1024 ** 2, 1)})
            count = sum(shard["running"] for shard in shards)
            state = "running" if shards and count == len(shards) else "degraded" if count else "failed" if shards else "stopped"
            return {"state": state, "shards": shards,
                    "uptime_seconds": int(time.time() - self.started_at) if count and self.started_at else 0,
                    "memory_mb": round(sum(shard["memory_mb"] for shard in shards), 1)}

    def log(self, identifier, shard):
        if shard not in ("Master", "Caves", "Mods"):
            raise PanelError("Unknown shard.")
        path = self.store.logs / identifier / f"{shard}.log"
        if not path.exists():
            return "No output yet. Start this world to see its logs."
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 64000))
            content = stream.read().decode("utf-8", errors="replace")
        return "\n".join(deque(content.splitlines(), maxlen=300))

    def shutdown(self):
        with self.lock:
            if self.updater and self.updater.poll() is None:
                self.updater.kill()
        self.stop(force=True)
