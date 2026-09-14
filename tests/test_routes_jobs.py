import pytest

from test_web import wait_job


@pytest.mark.parametrize("path", ["/", "/overview", "/archives", *[
    "/worlds/abcdef123456/" + tab for tab in ("overview", "settings", "mods", "backups", "logs")]])
def test_direct_pages_support_authenticated_load_and_login(client, auth, path):
    response = client.get(path)
    assert response.status_code == 200
    assert b'id="server-overview"' in response.data and b'id="archives-page"' in response.data
    assert b'/static/app.js' in response.data
    client.post("/api/logout", headers=auth)
    response = client.get(path)
    assert response.status_code == 200 and b'Welcome back' in response.data


@pytest.mark.parametrize("path", ["/worlds/abcdef123456/unknown", "/worlds/not-an-id/settings"])
def test_invalid_world_routes_return_not_found(client, auth, path):
    assert client.get(path).status_code == 404


def test_job_scope_survives_create_failure_archive_and_permanent_deletion(client, auth, service):
    ids = []
    for name in ("Camp A", "Camp B"):
        client.post("/api/worlds", json={"name": name}, headers=auth)
        job = wait_job(service)
        ids.append(job["result"]["world_id"])
        assert job["world_id"] == ids[-1] and job["world_name"] == name
    first, second = ids
    client.patch(f"/api/worlds/{first}", json={"description": "A only"}, headers=auth)
    assert wait_job(service)["world_id"] == first
    client.post(f"/api/worlds/{second}/actions/backup", json={}, headers=auth)
    assert wait_job(service)["world_id"] == second
    client.delete(f"/api/worlds/{first}", json={"confirmation": "wrong"}, headers=auth)
    failed = wait_job(service)
    assert failed["state"] == "failed" and failed["world_id"] == first
    service.jobs.append({"id": "legacy", "title": "Legacy action", "state": "completed"})
    assert all(job["world_id"] == first for job in service.job_list(first))
    assert all(job["world_id"] == second for job in service.job_list(second))
    assert any(job["id"] == "legacy" for job in service.job_list())
    client.delete(f"/api/worlds/{second}", json={"confirmation": "Camp B"}, headers=auth)
    job = wait_job(service)
    assert job["world_id"] == second and job["world_name"] == "Camp B"
    client.delete(f"/api/archived-worlds/{second}", json={"confirmation": "Camp B"}, headers=auth)
    job = wait_job(service)
    assert job["state"] == "completed" and job["world_id"] == second and job["world_name"] == "Camp B"
    assert all(job["world_id"] != second for job in service.job_list(first))
