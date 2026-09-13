import pytest
from werkzeug.security import generate_password_hash

from pera_panel.service import Service
from pera_panel.storage import PanelError
from pera_panel.web import create_app


class FakeRuntime:
    def __init__(self, store):
        self.store = store
        self.world_id = None
        self.updater = None
        self.events = []
        self.fail_stop = False

    def active(self, identifier=None):
        return self.world_id is not None and identifier in (None, self.world_id)

    def start(self, world):
        if self.active():
            raise PanelError("Stop the active world first.", 409)
        self.world_id = world["id"]
        self.events.append("start")

    def stop(self):
        self.events.append("stop")
        if self.fail_stop:
            raise PanelError("Shutdown timed out", 409)
        self.world_id = None

    def status(self, identifier):
        return {"state": "running" if self.active(identifier) else "stopped", "shards": [],
                "uptime_seconds": 0, "memory_mb": 0}

    def command(self, identifier, action, message=""):
        self.events.append((action, message))

    def request_players(self, identifier):
        pass

    def rollback(self, identifier, count):
        self.events.append(("rollback", count))

    def log(self, identifier, shard):
        return "Test output"

    def update_mods(self, world):
        self.events.append("update-mods")

    def shutdown(self):
        self.world_id = None


@pytest.fixture
def service(tmp_path):
    result = Service(tmp_path / "data", tmp_path / "game", FakeRuntime)
    yield result
    result.close()


@pytest.fixture
def config():
    return {"secret_key": "test-secret-only", "username": "admin",
            "password_hash": generate_password_hash("test-password-123")}


@pytest.fixture
def app(service, config):
    result = create_app(config, service)
    result.config["TESTING"] = True
    return result


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth(client):
    token = client.get("/api/session").json["csrf"]
    response = client.post("/api/login", json={"username": "admin", "password": "test-password-123"},
                           headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json["csrf"]}
