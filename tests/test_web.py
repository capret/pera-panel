import time


def wait_job(service):
    deadline = time.monotonic() + 5
    while service.busy and time.monotonic() < deadline:
        time.sleep(.01)
    assert not service.busy
    return service.job_list()[0]


def test_auth_csrf_and_security_headers(client):
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/login", json={}).status_code == 403
    response = client.get("/")
    assert b"Welcome back" in response.data
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "HttpOnly" in response.headers["Set-Cookie"]


def test_authenticated_world_flow_and_secret_redaction(client, auth, service):
    assert b"World overview" in client.get("/").data
    response = client.post("/api/worlds", json={"name": "Camp", "token": "private-token"}, headers=auth)
    assert response.status_code == 202
    identifier = wait_job(service)["result"]["world_id"]
    status = client.get("/api/status")
    assert b"private-token" not in status.data
    assert status.json["worlds"][0]["has_token"]
    assert client.patch(f"/api/worlds/{identifier}", json={"name": "Bad"}).status_code == 403
    assert client.patch(f"/api/worlds/{identifier}", json={"description": "new"}, headers=auth).status_code == 202
    assert wait_job(service)["state"] == "completed"
    assert service.store.get(identifier)["token"] == "private-token"
    assert client.get(f"/api/worlds/{identifier}/backups").json == {"backups": []}
    assert client.post(f"/api/worlds/{identifier}/actions/backup", json={}, headers=auth).status_code == 202
    assert wait_job(service)["state"] == "completed"
    snapshot = client.get(f"/api/worlds/{identifier}/backups").json["backups"][0]["id"]
    assert client.post(f"/api/worlds/{identifier}/backups/{snapshot}/restore", json={}, headers=auth).status_code == 400
    assert client.post("/api/logout", headers=auth).status_code == 200
    assert client.get("/api/status").status_code == 401


def test_login_rate_limit(client):
    csrf = client.get("/api/session").json["csrf"]
    for _ in range(10):
        assert client.post("/api/login", json={"username":"admin", "password":"wrong"},
                           headers={"X-CSRF-Token": csrf}).status_code == 401
    assert client.post("/api/login", json={"username":"admin", "password":"wrong"},
                       headers={"X-CSRF-Token": csrf}).status_code == 429


def test_request_size_limit(client, auth):
    response = client.post("/api/worlds", json={"name": "x" * 300000}, headers=auth)
    assert response.status_code == 413


def test_invalid_json_schema_is_rejected(client, auth):
    assert client.post("/api/worlds", json=[], headers=auth).status_code == 400


def test_old_csrf_is_rotated_after_login(client, auth):
    assert client.post("/api/logout", headers={"X-CSRF-Token": "old"}).status_code == 403
