"""World creation settings survive API, configuration generation and later edits."""
from pathlib import Path
import shutil
import subprocess

import pytest

from pera_panel.lua_settings import LiteralTable
from test_web import wait_job


def test_creation_overrides_are_saved_per_shard_and_preserved_on_other_edits(client, auth, service):
    surface = {"world_size": "huge", "season_start": "winter", "winter": "longseason",
               "day": "longday", "grass": "often", "hounds": "never", "custom_mod_option": {"enabled": True},
               "specialevent": "none", "winters_feast": "enabled", "hallowed_nights": "enabled"}
    caves = {"world_size": "small", "rock": "often", "bunnymen": "rare", "custom_seed": 12345}
    response = client.post("/api/worlds", headers=auth, json={"name": "Configured camp",
                           "master_overrides": surface, "caves_overrides": caves})
    assert response.status_code == 202
    job = wait_job(service)
    assert job["state"] == "completed"
    identifier = job["result"]["world_id"]
    service.configure(identifier, {"description": "Preserve generation choices"})
    world = next(item for item in client.get("/api/status").json["worlds"] if item["id"] == identifier)
    assert world["master_overrides"] == surface
    assert world["caves_overrides"] == caves
    for shard, preset, expected in (("Master", "SURVIVAL_TOGETHER", surface), ("Caves", "DST_CAVE", caves)):
        generated = LiteralTable((service.store.world_path(identifier) / shard / "worldgenoverride.lua").read_text()).read()
        assert generated == {"override_enabled": True, "preset": preset, "overrides": expected}


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed for frontend logic checks")
def test_world_settings_frontend_logic():
    result = subprocess.run([shutil.which("node"), "--test", str(Path(__file__).with_name("world-settings.test.cjs"))],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
