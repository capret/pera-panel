"""Own the game processes as an unprivileged user, with bounded logs and graceful shutdown."""
from collections import deque
from datetime import datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import subprocess
import threading
import time

import psutil

from .storage import PanelError, lua
from .players import PlayerRegistry


PAUSE_SETTING = re.compile(r"^\[[0-9:.]+\]:\s*PauseWhenEmpty:\s*(true|false)\s*$")
PAUSE_EVENT = re.compile(r"^\[[0-9:.]+\]:\s*Server (Paused|Autopaused|Unpaused)\s*$")
PAUSE_STATES = {"Paused": "paused", "Autopaused": "auto_paused", "Unpaused": "unpaused"}
PAUSE_CONFIRM_TIMEOUT = 10


class Runtime:
    def __init__(self, store):
        self.store = store
        self.processes = {}
        self.readers = {}
        self.world_id = None
        self.started_at = None
        self.lock = threading.RLock()
        self.updater = None
        self.update_context = None
        self.players = PlayerRegistry(store)
        self.loaded_pause_settings = {}
        self.pause_states = {}
        self.pause_changed = threading.Condition(self.lock)

    def binary(self):
        binary = self.store.game / "bin64" / "dontstarve_dedicated_server_nullrenderer_x64"
        if not binary.is_file():
            raise PanelError("DST is not installed. Run sudo pera-panel update-game on the server.", 409)
        return binary

    def arguments(self, world, shard):
        return [str(self.binary()), "-console", "-persistent_storage_root", str(self.store.root),
                "-conf_dir", "clusters", "-cluster", world["id"], "-shard", shard,
                "-monitor_parent_process", str(os.getpid()), "-backup_log_count", "3"]

    def _spawn(self, args, world, shard, *, interactive=True):
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(self.binary().parent / "lib64") + ":" + str(self.binary().parent)
        # Do not pass panel secrets to game processes or mods.
        for key in tuple(env):
            if key.startswith("PERA_"):
                env.pop(key)
        process = subprocess.Popen(args, cwd=self.binary().parent, env=env,
                                   stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
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
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            handler.emit(logging.LogRecord("dst", logging.INFO, "", 0,
                                           f"[Pera Panel] {stamp} | {shard} process started (PID {process.pid})",
                                           (), None))
            for output in iter(lambda: process.stdout.readline(65536), ""):
                self._record_pause_setting(process, identifier, shard, output)
                self._record_pause_state(process, identifier, shard, output)
                try:
                    self.players.log_line(identifier, output, shard)
                except (OSError, ValueError):
                    logging.exception("Could not record player discovery")
                for secret in secrets:
                    if secret:
                        output = output.replace(secret, "[redacted]")
                handler.emit(logging.LogRecord("dst", logging.INFO, "", 0, output.rstrip(), (), None))
        finally:
            process.stdout.close()
            handler.close()

    def _record_pause_setting(self, process, identifier, shard, output):
        # Only current game processes can report their startup configuration.
        # Chat text, updater output, and late output from an old run are ignored.
        match = PAUSE_SETTING.fullmatch(output.rstrip("\r\n"))
        if match:
            with self.lock:
                if self.world_id == identifier and self.processes.get(shard) is process:
                    self.loaded_pause_settings[shard] = match[1] == "true"

    def _record_pause_state(self, process, identifier, shard, output):
        # Observe native events only. Reading status must never wake an empty
        # world with console queries, and echoed commands/chat are not evidence.
        match = PAUSE_EVENT.fullmatch(output.rstrip("\r\n"))
        if match:
            with self.pause_changed:
                if self.world_id == identifier and self.processes.get(shard) is process:
                    self.pause_states[shard] = PAUSE_STATES[match[1]]
                    self.pause_changed.notify_all()

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
            # An updater never accepts console commands. Give it EOF immediately so its
            # console reader cannot keep the process alive after it logs "Shutting down".
            process = self._spawn(self.arguments(world, shard) + ["-only_update_server_mods"],
                                  world, "Mods", interactive=False)
            with self.lock:
                self.updater = process
                self.update_context = {"world_id": world["id"], "shard": shard}
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
                    self.update_context = None

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
            self.loaded_pause_settings = {}
            self.pause_states = {}
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
        if not process.stdin or process.stdin.closed:
            if required:
                raise PanelError("The game console disconnected. Inspect the shard log.", 409)
            return
        try:
            process.stdin.write(command + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise PanelError("The game console disconnected. Inspect the shard log.", 409) from exc

    def command(self, identifier, action, message=""):
        if action in ("pause", "resume"):
            return self._set_pause(identifier, action == "pause")
        with self.lock:
            if not self.active(identifier):
                raise PanelError("This world is stopped.", 409)
            if action == "save":
                for process in self.processes.values():
                    self._send(process, "c_save()", required=True)
            elif action == "announce":
                master = self.processes.get("Master")
                if not master or master.poll() is not None:
                    raise PanelError("The surface shard is stopped.", 409)
                self._send(master, f"c_announce({lua(message)})", required=True)
            else:
                raise PanelError("Unknown console action.")

    def _set_pause(self, identifier, paused):
        with self.pause_changed:
            world = self.store.get(identifier)
            expected = ["Master", "Caves"] if world["caves"] else ["Master"]
            targets = {name: self.processes.get(name) for name in expected}

            def available():
                return self.world_id == identifier and all(
                    process is not None and self.processes.get(name) is process and process.poll() is None
                    for name, process in targets.items())

            if not available():
                raise PanelError("Start all configured shards before pausing or resuming the world.", 409)
            desired = {"paused"} if paused else {"unpaused", "auto_paused"}
            for name, process in targets.items():
                if self.pause_states.get(name) not in desired:
                    self._send(process, f"TheNet:SetServerPaused({'true' if paused else 'false'})", required=True)
            # A successful pipe write is only a request, not a game acknowledgement.
            # Condition.wait releases the runtime lock so readers/status/stop can run.
            deadline = time.monotonic() + PAUSE_CONFIRM_TIMEOUT
            while True:
                if not available():
                    raise PanelError("A shard stopped before the pause change was confirmed. Check the shard logs.", 409)
                if all(self.pause_states.get(name) in desired for name in targets):
                    if not paused and any(self.pause_states.get(name) == "auto_paused" for name in targets):
                        return {"message": "Manual pause is cleared. DST still reports an automatic pause."}
                    return {"message": "DST confirmed the world is paused." if paused else "DST confirmed the world is unpaused."}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PanelError("DST did not confirm the pause change on every shard. Check the shard logs; the game may have ignored the command.", 409)
                self.pause_changed.wait(timeout=min(remaining, .25))

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

    def stop(self, force=False):
        with self.lock:
            processes = list(self.processes.values())
            for process in processes:
                try:
                    self._send(process, "c_shutdown(true)")
                except PanelError:
                    pass
                finally:
                    # Flush the shutdown request, then deliver EOF before waiting. A console
                    # input reader can otherwise keep a saving/exiting child waiting forever.
                    if process.stdin and not process.stdin.closed:
                        try:
                            process.stdin.close()
                        except OSError:
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
            self.loaded_pause_settings = {}
            self.pause_states = {}
            self.pause_changed.notify_all()

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
                                   "exit_code": code, "memory_mb": round(memory / 1024 ** 2, 1),
                                   "pause_state": self.pause_states.get(name, "unknown") if code is None else "stopped",
                                   "loaded_pause_when_empty": self.loaded_pause_settings.get(name)
                                   if code is None else None})
            count = sum(shard["running"] for shard in shards)
            state = "running" if shards and count == len(shards) else "degraded" if count else "failed" if shards else "stopped"
            observed = {shard["pause_state"] for shard in shards}
            pause_state = ("stopped" if not count else "partial" if state == "degraded"
                           else "unknown" if "unknown" in observed
                           else next(iter(observed)) if len(observed) == 1
                           else "paused" if observed <= {"paused", "auto_paused"} else "mixed")
            update = self.update_context
            mod_update = ({"shard": update["shard"]} if update and update["world_id"] == identifier
                          and self.updater and self.updater.poll() is None else None)
            return {"state": state, "shards": shards,
                    "pause_state": pause_state,
                    "mod_update": mod_update,
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
