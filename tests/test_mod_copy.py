"""The mod editor can save another world's public mod configuration independently."""
from copy import deepcopy

from pera_panel.lua_settings import LiteralTable
from test_web import wait_job


def test_copy_saved_mods_preserves_source_and_other_destination_settings(client, auth, service):
    configured = [{"id": "378160973", "enabled": True,
                   "options": {"mode": "中文", "range": 0, "sharing": False, "nested": {"scale": 1.5}}},
                  {"id": "123456789", "enabled": False, "options": {"enabled": True}}]
    source_id = service.create({"name": "Configured world", "mods": configured, "token": "source-secret",
                                "password": "source-pass", "caves": False})["world_id"]
    target_id = service.create({"name": "New world", "token": "target-secret", "password": "target-pass",
                                "caves": True, "master_overrides": {"world_size": "small"},
                                "mods": [{"id": "999999999"}]})["world_id"]
    source_before, target_before = service.store.get(source_id), service.store.get(target_id)
    progress = service.store.world_path(target_id) / "Master" / "save" / "progress"
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_bytes(b"existing save")
    service.runtime.start(source_before)  # A running source is safe to read.
    source = next(w for w in client.get("/api/status").json["worlds"] if w["id"] == source_id)
    assert "token" not in source
    copied = deepcopy(source["mods"])
    copied[0]["options"]["nested"]["scale"] = 2  # Review/edit the copy before saving.
    response = client.patch(f"/api/worlds/{target_id}", json={"mods": copied}, headers=auth)
    assert response.status_code == 202
    assert wait_job(service)["state"] == "completed"
    target = service.store.get(target_id)
    assert target["mods"] == copied
    assert {k: v for k, v in target.items() if k not in ("mods", "updated_at")} == {
        k: v for k, v in target_before.items() if k not in ("mods", "updated_at")}
    assert service.store.get(source_id) == source_before
    assert service.runtime.active(source_id)
    assert progress.read_bytes() == b"existing save"
    expected = {f"workshop-{mod['id']}": {"enabled": mod["enabled"], "configuration_options": mod["options"]}
                for mod in copied}
    for shard in ("Master", "Caves"):
        generated = (service.store.world_path(target_id) / shard / "modoverrides.lua").read_text(encoding="utf-8")
        assert LiteralTable(generated).read() == expected


def test_copy_cannot_overwrite_running_destination(client, auth, service):
    source_id = service.create({"name": "Source", "mods": [{"id": "123456789"}]})["world_id"]
    target_id = service.create({"name": "Running target", "mods": [{"id": "378160973"}]})["world_id"]
    before = service.store.get(target_id)
    service.runtime.start(before)
    source = next(w for w in client.get("/api/status").json["worlds"] if w["id"] == source_id)
    assert client.patch(f"/api/worlds/{target_id}", json={"mods": source["mods"]}, headers=auth).status_code == 202
    assert wait_job(service)["state"] == "failed"
    assert service.store.get(target_id) == before
