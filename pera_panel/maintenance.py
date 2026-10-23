"""Unprivileged client for the installer's fixed-command update service."""
import json
import socket
import threading

from .storage import PanelError


SOCKET = "/run/pera-panel-maintenance.sock"


class Maintenance:
    def __init__(self, path=SOCKET):
        self.path = path
        self.lock = threading.Lock()
        self.last_job = None

    def _request(self, request):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(self.path)
            connection.sendall(json.dumps(request).encode() + b"\n")
            data = bytearray()
            while b"\n" not in data:
                chunk = connection.recv(8192)
                if not chunk or len(data) + len(chunk) > 128 * 1024:
                    raise ValueError("Invalid updater response")
                data.extend(chunk)
        response = json.loads(data)
        if not isinstance(response, dict) or "job" not in response:
            raise ValueError("Invalid updater response")
        return response

    def status(self):
        with self.lock:
            try:
                result = self._request({"operation": "status"})
                self.last_job = result["job"]
                return {"available": True, "job": self.last_job}
            except (OSError, ValueError, AttributeError):
                # Losing the helper during an update must not enable mutations.
                return {"available": False, "job": self.last_job}

    def require_idle(self):
        if (self.status()["job"] or {}).get("state") == "running":
            raise PanelError("A software update is in progress. Wait for it to finish.", 409)

    def start(self, target):
        if target not in ("game", "panel"):
            raise PanelError("Choose a game or panel update.")
        with self.lock:
            try:
                result = self._request({"operation": "start", "target": target})
            except (OSError, ValueError, AttributeError) as exc:
                raise PanelError("The updater is unavailable. Install the update service from the server console first.", 503) from exc
            self.last_job = result["job"]
            if result.get("error"):
                raise PanelError("The updater could not accept this request. Check update progress and try again.", 409)
            return {"available": True, "job": self.last_job}
