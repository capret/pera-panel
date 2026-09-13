import configparser
import json
import math

import pytest

from pera_panel.storage import PanelError, lua, validate_world


@pytest.mark.parametrize("payload", [
    {"name": "x\n[NETWORK]\nserver_port=2"}, {"name": "ok", "max_players": True},
    {"name": "ok", "max_players": 65}, {"name": "ok", "caves": "false"},
    {"name": "ok", "token": "abc\ndef"}, {"name": "ok", "admins": ["../file"]},
    {"name": "ok", "master_overrides": {"test": None}},
    {"name": "ok", "mods": [{"id": '12345\");os.execute(\"bad\")'}]},
    {"name": "ok", "mods": [{"id": "12345"}, {"id": "12345"}]},
])
def test_invalid_settings_rejected(payload):
    with pytest.raises(PanelError):
        validate_world(payload)


def test_lua_escape_is_data_only():
    assert lua('a"\\\n饥') == '"a\\034\\092\\010\\233\\165\\165"'
    assert lua({"enabled": True, "options": [1, False]}) == '{["enabled"]=true,["options"]={1,false}}'
    for value in (None, math.inf, math.nan):
        with pytest.raises(PanelError):
            lua(value)


def test_generated_shards_and_mods(service):
    identifier = service.create({"name": "Our camp 100%", "token": "secret-token", "caves": True,
                                 "mods": [{"id": "378160973", "options": {"SHOW": True}}],
                                 "admins": ["KU_abcdefgh"]})["world_id"]
    root = service.store.world_path(identifier)
    cluster = configparser.ConfigParser(interpolation=None)
    cluster.read(root / "cluster.ini")
    assert cluster["NETWORK"]["cluster_name"] == "Our camp 100%"
    assert cluster["SHARD"]["bind_ip"] == "127.0.0.1"
    ports = set()
    for shard in ("Master", "Caves"):
        parser = configparser.ConfigParser()
        parser.read(root / shard / "server.ini")
        for section, key in [("NETWORK", "server_port"), ("STEAM", "authentication_port"),
                             ("STEAM", "master_server_port")]:
            ports.add(parser[section][key])
        assert 'workshop-378160973' in (root / shard / "modoverrides.lua").read_text()
    assert len(ports) == 6
    assert "DST_CAVE" in (root / "Caves/worldgenoverride.lua").read_text()
    assert (root / "adminlist.txt").read_text().strip() == "KU_abcdefgh"
    service.store.write_mod_setup(service.store.get(identifier))
    assert 'ServerModSetup("378160973")' in (service.store.game / "mods/dedicated_server_mods_setup.lua").read_text()
    public = service.store.public(service.store.get(identifier))
    assert public["has_token"]
    assert "secret-token" not in json.dumps(public)
    assert "cluster_key" not in public


@pytest.mark.parametrize("identifier", ["../secret", "/etc/passwd", "123", "a" * 13])
def test_path_traversal_rejected(service, identifier):
    with pytest.raises(PanelError):
        service.store.world_path(identifier)
