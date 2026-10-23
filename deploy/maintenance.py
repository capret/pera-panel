#!/usr/bin/env python3
"""Root-owned, socket-activated updater. Stdlib only; no web-supplied commands."""
from datetime import datetime, timezone
import codecs
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import threading
import time
import urllib.request
import uuid

STATE = Path("/var/lib/pera-panel-maintenance/job.json")
COMMANDS = {target: ["/bin/bash", "/usr/local/lib/pera-panel/install.sh", "update-" + target, "--yes"]
            for target in ("panel", "game")}
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
       "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"}


def now():
    return datetime.now(timezone.utc).isoformat()


def acquire_lock(path="/run/lock/pera-panel-maintenance.lock"):
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    handle = os.fdopen(fd, "r+")
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise OSError("Invalid maintenance lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BaseException:
        handle.close()
        raise


def ensure_panel():
    """Recover even if SteamCMD/installation failed after stopping the web service."""
    subprocess.run(["/usr/bin/systemctl", "start", "pera-panel.service"], check=True, timeout=210, env=ENV)
    config = json.loads(Path("/etc/pera-panel/config.json").read_text())
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(config["host"], config["host"])
    if ":" in host:
        host = "[" + host + "]"
    url = f"http://{host}:{int(config['port'])}/healthz"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(30):
        try:
            with opener.open(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("Panel health check failed. Run sudo journalctl -u pera-panel -n 80.")


class Updater:
    def __init__(self, path=STATE, recover=ensure_panel, acquire=acquire_lock):
        self.path, self.recover = path, recover
        self.acquire = acquire
        self.maintenance_lock = None
        self.lock = threading.Lock()
        self.worker = None
        self.reported_write_error = False
        self.job = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if self.job and self.job["state"] == "running":
            self.job.update(state="failed", finished_at=now())
            self.job["log"] += "\nUpdater interrupted. Inspect the server before retrying."
            self.persist()

    def persist(self):
        temporary = self.path.with_suffix(".next")
        temporary.write_text(json.dumps(self.job), encoding="utf-8")
        temporary.replace(self.path)

    def snapshot(self):
        return dict(self.job) if self.job else None

    def persist_progress(self):
        try:
            self.persist()
        except OSError as exc:
            # A full/unavailable log disk must never prevent panel recovery or
            # leave the maintenance lock held. In-memory status remains usable.
            if not self.reported_write_error:
                print(f"Could not persist update progress: {exc}", flush=True)
                self.reported_write_error = True

    def dispatch(self, request):
        with self.lock:
            if request == {"operation": "status"}:
                return {"job": self.snapshot()}
            if (not isinstance(request, dict) or set(request) != {"operation", "target"}
                    or request.get("operation") != "start" or not isinstance(request.get("target"), str)
                    or request["target"] not in COMMANDS):
                return {"error": "Invalid update request", "job": self.snapshot()}
            if self.worker and self.worker.is_alive():
                return {"error": "An update is already running", "job": self.snapshot()}
            try:
                self.maintenance_lock = self.acquire()
            except OSError:
                return {"error": "Server maintenance is already running or unavailable", "job": self.snapshot()}
            self.job = {"id": uuid.uuid4().hex, "target": request["target"], "state": "running",
                        "created_at": now(), "finished_at": None, "log": "Starting update…\n"}
            try:
                self.persist()
                self.worker = threading.Thread(target=self.run, args=(request["target"],))
                self.worker.start()
            except BaseException:
                self.maintenance_lock.close()
                raise
            return {"job": self.snapshot()}

    def append(self, text):
        with self.lock:
            self.job["log"] = (self.job["log"] + text)[-16000:]
            self.persist_progress()

    def run(self, target):
        # Let the initiating HTTP request return before stopping the panel.
        time.sleep(1)
        success = False
        try:
            fd = self.maintenance_lock.fileno()
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            with subprocess.Popen(COMMANDS[target], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, cwd="/", pass_fds=(fd,),
                                  env={**ENV, "PERA_MAINTENANCE_LOCK_FD": str(fd)}) as process:
                while chunk := process.stdout.read1(2048):
                    self.append(decoder.decode(chunk))
                self.append(decoder.decode(b"", final=True))
                success = process.wait() == 0
        except Exception as exc:
            self.append(f"\nUpdate failed: {exc}\n")
        finally:
            try:
                self.append("\nChecking that the panel is available…\n")
                self.recover()
            except Exception as exc:
                success = False
                self.append(f"\nPanel recovery failed: {exc}\n")
            try:
                with self.lock:
                    self.job.update(state="completed" if success else "failed", finished_at=now())
                    self.persist_progress()
            finally:
                self.maintenance_lock.close()


def serve_connection(connection, updater, allowed_uid):
    connection.settimeout(3)
    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid not in (0, allowed_uid):
        return
    data = bytearray()
    while b"\n" not in data:
        chunk = connection.recv(1024)
        if not chunk or len(data) + len(chunk) > 1024:
            return
        data.extend(chunk)
    try:
        response = updater.dispatch(json.loads(data))
    except (ValueError, TypeError):
        response = {"error": "Invalid update request", "job": None}
    connection.sendall(json.dumps(response).encode() + b"\n")


def main():
    import pwd

    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise SystemExit("This helper must be started by its systemd socket.")
    updater = Updater()
    uid = pwd.getpwnam("pera-panel").pw_uid
    with socket.socket(fileno=3) as listener:
        listener.settimeout(1)
        started = time.monotonic()
        while True:
            try:
                connection, _ = listener.accept()
                with connection:
                    try:
                        serve_connection(connection, updater, uid)
                    except (OSError, ValueError):
                        pass
            except socket.timeout:
                pass
            # Reload the root-owned helper after upgrades, without killing an update.
            if time.monotonic() - started > 60 and not (updater.worker and updater.worker.is_alive()):
                return


if __name__ == "__main__":
    main()
