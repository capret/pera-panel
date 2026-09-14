import configparser
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pera_panel.players import characters, folder_userid, players_from_log
from pera_panel.storage import PanelError, atomic_write
from test_save_import import local_zip
from test_web import wait_job


SESSION = "0123456789ABCDEF"
SOURCE = f"Master/save/session/{SESSION}/OU_76561198000000000"
TARGET = f"Master/save/session/{SESSION}/A7ONLINE1234"
USER = "KU_abcdefgh"


def setup_characters(service):
    identifier = service.create({"name": "Camp", "caves": False})["world_id"]
    root = service.store.world_path(identifier)
    atomic_write(root / SOURCE / "0000000010", "old character inventory and progress")
    atomic_write(root / SOURCE / "0000000010.meta", 'KLEI     1 return {character="wilson"}')
    atomic_write(root / TARGET / "0000000011", "new character")
    return identifier, root


def test_import_preserves_offline_character_and_does_not_assign_cached_owner(service, tmp_path):
    identifier = service.create({"name": "Camp", "caves": False})["world_id"]
    archive = local_zip(tmp_path, caves=False, extra={
        "Cluster_1/" + SOURCE + "/0000000010": "original offline character",
        "Cluster_1/Master/save/cached_userid": USER,
        "Cluster_1/Master/server_log.txt": "[00:00:10]: Client authenticated: (KU_other123) Survivor\n"
            "[00:00:11]: [Say] (KU_other123) Survivor: Client authenticated: (KU_fake1234) Fake\n",
    })
    service.import_save(identifier, archive, "Camp")
    root = service.store.world_path(identifier)
    assert (root / SOURCE / "0000000010").read_text() == "original offline character"
    rows = {p["userid"]: p for p in service.players.list(identifier)}
    assert rows[USER]["sources"] == ["cached_owner"]
    assert rows[USER]["folders"] == {}
    assert rows["KU_other123"]["sources"] == ["imported_log"]
    assert "KU_fake1234" not in rows
    assert service.store.get(identifier)["admins"] == []
    assert characters(root)[0]["offline"]
    assert characters(root)[0]["userid"] is None
    assert service.store.get(identifier)["encode_user_path"] == {"Master": False}


@pytest.mark.parametrize("folder,setting,expected", [
    ("KU_abcdefgh", "", False), ("A7ENCODED1234", "", True),
    ("A7ENCODED1234", "[ACCOUNT]\nencode_user_path = false\n", False),
])
def test_import_retains_explicit_encoding_or_infers_uniform_layout(service, tmp_path, folder, setting, expected):
    identifier = service.create({"name": "Camp", "caves": False})["world_id"]
    archive = local_zip(tmp_path, caves=False, extra={
        f"Cluster_1/Master/save/session/{SESSION}/{folder}/0000000010": "character",
        "Cluster_1/Master/server.ini": setting,
    })
    service.import_save(identifier, archive, "Camp")
    service.configure(identifier, {"description": "Keep encoding"})
    parser = configparser.ConfigParser()
    parser.read(service.store.world_path(identifier) / "Master/server.ini")
    assert parser.getboolean("ACCOUNT", "encode_user_path") == expected


def test_discovery_reads_metadata_without_executing_lua_and_keeps_names(service):
    identifier, root = setup_characters(service)
    service.players.log_line(identifier, f'[00:01:00]: Client authenticated: ({USER}) <survivor> 中文\n')
    service.players.log_line(identifier, '[00:01:01]: [Say] (KU_fake1234) hi')
    assert service.players.list(identifier)[0]["name"] == "<survivor> 中文"
    source = next(c for c in service.character_list(identifier) if c["path"] == SOURCE)
    assert source["character"] == "wilson"
    atomic_write(root / SOURCE / "0000000010.meta", 'return {character=os.execute("bad")}')
    assert next(c for c in characters(root) if c["path"] == SOURCE)["character"] == ""
    service.players.discover(identifier)
    assert service.store.get(identifier)["admins"] == []


def test_recovery_requires_game_mapping_and_existing_target_and_keeps_backup(service):
    identifier, root = setup_characters(service)
    service.players.record(identifier, USER, source="cached_owner")
    with pytest.raises(PanelError, match="existing destination"):
        service.recover_character(identifier, SOURCE, USER, "Camp")
    service.players.record(identifier, USER, source="console", folder="A7ONLINE1234")
    result = service.recover_character(identifier, SOURCE, USER, "Camp")
    assert (root / TARGET / "0000000010").read_bytes() == (root / SOURCE / "0000000010").read_bytes()
    assert not (root / TARGET / "0000000011").exists()
    backup = service.backup_path(identifier, result["safety_backup_id"]) / "cluster"
    assert (backup / TARGET / "0000000011").read_text() == "new character"
    assert service.runtime.events == []
    service.restore(identifier, result["safety_backup_id"])
    assert (root / TARGET / "0000000011").read_text() == "new character"


@pytest.mark.parametrize("source", ["../../other", "/tmp", "Master/save/session/0123456789ABCDEF/..", None])
def test_recovery_rejects_path_input(service, source):
    identifier, _ = setup_characters(service)
    with pytest.raises(PanelError):
        service.recover_character(identifier, source, USER, "Camp")
    assert not service.backups(identifier)


def test_recovery_refuses_running_world_wrong_confirmation_and_missing_target(service):
    identifier, root = setup_characters(service)
    service.players.record(identifier, USER, source="console", folder="A7MISSING")
    with pytest.raises(PanelError, match="existing destination"):
        service.recover_character(identifier, SOURCE, USER, "Camp")
    with pytest.raises(PanelError, match="world name"):
        service.recover_character(identifier, SOURCE, USER, "Wrong")
    service.start(identifier)
    with pytest.raises(PanelError, match="Stop this world"):
        service.recover_character(identifier, SOURCE, USER, "Camp")
    assert not service.backups(identifier)
    assert (root / TARGET / "0000000011").exists()


def test_recovery_reverts_target_on_failed_swap(service, monkeypatch):
    identifier, root = setup_characters(service)
    service.players.record(identifier, USER, source="console", folder="A7ONLINE1234")
    original = Path.rename

    def fail_prepared(self, target):
        if self.name == "prepared":
            raise OSError("disk error")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", fail_prepared)
    with pytest.raises(OSError):
        service.recover_character(identifier, SOURCE, USER, "Camp")
    assert (root / TARGET / "0000000011").read_text() == "new character"
    assert (root / SOURCE / "0000000010").exists()


def test_player_api_requires_login_and_updates_after_new_join(client, auth, service):
    identifier, _ = setup_characters(service)
    service.players.record(identifier, USER, name="First", source="join")
    endpoint = f"/api/worlds/{identifier}/players"
    assert client.get(endpoint).json["players"][0]["name"] == "First"
    service.players.log_line(identifier, "[00:00:15]: Client authenticated: (KU_new12345) New player")
    assert len(client.get(endpoint).json["players"]) == 2
    assert client.get(f"/api/worlds/{identifier}/characters").status_code == 200
    assert client.post(f"/api/worlds/{identifier}/recover-character", json={}).status_code == 403
    client.post("/api/logout", headers=auth)
    assert client.get(endpoint).status_code == 401


def test_native_rollback_api_uses_serial_operation_and_confirmation(client, auth, service):
    identifier, _ = setup_characters(service)
    url = f"/api/worlds/{identifier}/actions/rollback"
    assert client.post(url, json={"count": 1, "confirmation": "Camp"}).status_code == 403
    response = client.post(url, json={"count": 1, "confirmation": "wrong"}, headers=auth)
    assert response.status_code == 202
    assert wait_job(service)["state"] == "failed"
    assert service.runtime.events == []
    client.post(url, json={"count": 1, "confirmation": "Camp"}, headers=auth)
    assert wait_job(service)["state"] == "completed"
    assert service.runtime.events == [("rollback", 1)]
    assert not service.backups(identifier)


def test_registry_rejects_invalid_ids_and_does_not_recreate_archived_world(service):
    identifier, root = setup_characters(service)
    service.players.record(identifier, "../evil", name="bad")
    service.delete(identifier, "Camp")
    service.players.record(identifier, USER, name="late")
    assert not root.exists()


def test_console_response_requires_exact_current_marker_and_safe_folder(service):
    from pera_panel.runtime import Runtime
    identifier, _ = setup_characters(service)
    runtime = Runtime(service.store)
    runtime.probes[(identifier, "Master")] = "PERA_PLAYERS_current"
    rows = json.dumps([{"userid": USER, "name": "Player", "folder": "A7ONLINE1234"}])
    runtime._player_response(identifier, "Master", "[00:01:00]: [Say] PERA_PLAYERS_current" + rows)
    runtime._player_response(identifier, "Master", "PERA_PLAYERS_old" + rows)
    assert runtime.players.list(identifier) == []
    runtime._player_response(identifier, "Master", "[00:01:00]: PERA_PLAYERS_current" + rows)
    assert runtime.players.list(identifier)[0]["folders"] == {"Master": "A7ONLINE1234"}
    rows = json.dumps([{"userid": "KU_other123", "folder": "../escape"}])
    runtime._player_response(identifier, "Master", "PERA_PLAYERS_current" + rows)
    assert next(p for p in runtime.players.list(identifier) if p["userid"] == "KU_other123")["folders"] == {}


def test_player_probe_is_throttled_and_uses_random_markers(service):
    from pera_panel.runtime import Runtime
    identifier, _ = setup_characters(service)
    runtime = Runtime(service.store)
    runtime.world_id = identifier
    process = MagicMock()
    process.poll.return_value = None
    process.stdin.closed = False
    runtime.processes = {"Master": process}
    runtime.request_players(identifier)
    runtime.request_players(identifier)
    assert process.stdin.write.call_count == 1
    assert "GetClientTable()" in process.stdin.write.call_args[0][0]
    assert "EncodeUserPath" not in process.stdin.write.call_args[0][0]
    assert runtime.probes[(identifier, "Master")].startswith("PERA_PLAYERS_")


def test_repeated_discovery_does_not_rewrite_registry(service, monkeypatch):
    identifier, _ = setup_characters(service)
    service.players.record(identifier, USER, source="console", folder="A7ONLINE1234")
    write = MagicMock()
    monkeypatch.setattr("pera_panel.players.write_json", write)
    service.players.record_many(identifier, [{"userid": USER, "source": "console", "folder": "A7ONLINE1234"}])
    write.assert_not_called()


def test_malformed_registry_record_does_not_break_logging(service):
    identifier, root = setup_characters(service)
    atomic_write(root / "pera-players.json", json.dumps({USER: {"userid": USER, "sources": None}}))
    service.players.log_line(identifier, f"Client authenticated: ({USER}) Survivor")
    assert service.players.list(identifier)[0]["name"] == "Survivor"


def test_saved_folder_suffix_is_normalized_only_with_an_unambiguous_account_hint():
    assert folder_userid(USER + "_", {USER}) == USER
    assert folder_userid(USER + "_", {USER + "_"}) == USER + "_"
    assert folder_userid(USER + "_", {USER, USER + "_"}) is None
    assert folder_userid(USER + "_") is None
    assert folder_userid(USER) == USER
    assert folder_userid(USER + "__", {USER + "_"}) == USER + "_"


def test_upgrade_repairs_suffixed_save_candidate_without_changing_permissions(service):
    identifier, root = setup_characters(service)
    atomic_write(root / SOURCE.replace("OU_76561198000000000", USER + "_") / "0000000010", "online")
    atomic_write(root / "Master/save/cached_userid", USER)
    service.players.record(identifier, USER + "_", source="save", folder=USER + "_")
    saved = service.character_list(identifier)
    player = next(p for p in service.players.list(identifier) if p["userid"] == USER)
    assert "save" in player["sources"]
    assert player["folders"] == {"Master": USER + "_"}
    assert USER + "_" not in {p["userid"] for p in service.players.list(identifier)}
    assert next(c for c in saved if c["folder"] == USER + "_")["userid"] == USER
    assert service.store.get(identifier)["admins"] == []


def test_suffix_discovery_preserves_authentic_ids_that_end_in_underscore(service):
    identifier, root = setup_characters(service)
    service.players.record(identifier, USER + "_", source="join", name="Real underscore account")
    atomic_write(root / SOURCE.replace("OU_76561198000000000", USER + "_") / "0000000010", "online")
    atomic_write(root / "Master/save/cached_userid", USER)
    service.character_list(identifier)
    assert {USER, USER + "_"}.issubset({p["userid"] for p in service.players.list(identifier)})


def test_nul_terminated_character_metadata_is_read_without_altering_files(service):
    identifier, root = setup_characters(service)
    content = b'return {character="woodie"}\0'
    meta = root / SOURCE / "0000000010.meta"
    meta.write_bytes(content)
    assert next(c for c in service.character_list(identifier) if c["path"] == SOURCE)["character"] == "woodie"
    assert meta.read_bytes() == content


def test_explicit_folder_recovery_does_not_require_live_console_or_known_account(client, auth, service):
    identifier, root = setup_characters(service)
    response = client.post(f"/api/worlds/{identifier}/recover-character", headers=auth,
                           json={"source": SOURCE, "destination": TARGET, "confirmation": "Camp"})
    assert response.status_code == 202
    job = wait_job(service)
    assert job["state"] == "completed", job.get("error")
    assert job["result"]["destination"] == TARGET
    assert (root / TARGET / "0000000010").read_bytes() == (root / SOURCE / "0000000010").read_bytes()
    assert service.players.list(identifier) == []
    assert service.runtime.events == []


@pytest.mark.parametrize("destination", [SOURCE, "../outside", "Caves/save/session/0123456789ABCDEF/A7OTHER",
                                         "Master/save/session/1123456789ABCDEF/A7OTHER"])
def test_explicit_destination_rejects_same_folder_traversal_and_other_shard_session(service, destination):
    identifier, root = setup_characters(service)
    if destination.startswith(("Caves/", "Master/save/session/1123")):
        atomic_write(root / destination / "0000000010", "other character")
    with pytest.raises(PanelError):
        service.recover_character(identifier, SOURCE, None, "Camp", destination)
    assert not service.backups(identifier)
    assert (root / TARGET / "0000000011").read_text() == "new character"


def resume_log(folder="A7ENCODED1234", userid=USER):
    return [f"[00:00:26]: Client authenticated: ({userid}) Survivor",
            f"[00:00:49]: Resuming user: session/{SESSION}/{folder}/0000000010",
            f"[00:00:49]: User ID\t{userid}\tassigned ownership to entity\t119701 - woodie\t"]


def test_saved_log_recognizes_encoded_character_without_moving_or_granting_permissions(service, tmp_path):
    identifier = service.create({"name": "Camp", "caves": False})["world_id"]
    path = f"Master/save/session/{SESSION}/A7ENCODED1234"
    archive = local_zip(tmp_path, caves=False, extra={
        "Cluster_1/" + path + "/0000000010": "original character bytes",
        "Cluster_1/" + path + "/0000000010.meta": 'return {character="woodie"}\0',
        "Cluster_1/Master/server_log.txt": "\n".join(resume_log()),
    })
    service.import_save(identifier, archive, "Camp")
    saved = service.character_list(identifier)
    assert len(saved) == 1 and saved[0]["userid"] == USER
    assert saved[0]["identity_source"] == "saved_log" and saved[0]["character"] == "woodie"
    assert saved[0]["path"] == path
    assert (service.store.world_path(identifier) / path / "0000000010").read_text() == "original character bytes"
    assert service.store.get(identifier)["admins"] == []


@pytest.mark.parametrize("variant", ["chat", "no-auth", "gap", "clock", "interleaved", "bad-path", "no-timestamp"])
def test_saved_log_does_not_guess_links_from_ambiguous_or_unrelated_lines(variant):
    lines = resume_log()
    if variant == "chat":
        lines[1] = "[00:00:49]: [Say] (" + USER + ") Player: " + lines[1]
    elif variant == "no-auth":
        lines = lines[1:]
    elif variant == "gap":
        lines.insert(2, "[00:00:49]: Unrelated output")
    elif variant == "clock":
        lines[-1] = lines[-1].replace("00:00:49", "00:00:50")
    elif variant == "interleaved":
        lines.insert(2, lines[1].replace("A7ENCODED1234", "A7ANOTHER123"))
    elif variant == "no-timestamp":
        lines = [line.split(": ", 1)[1] for line in lines]
    else:
        lines[1] = lines[1].replace("A7ENCODED1234", "../../escape")
    assert players_from_log(lines, "Master")[1] == []


def test_existing_server_log_and_live_output_identify_only_the_matching_shard_session(service):
    identifier, root = setup_characters(service)
    atomic_write(root / "Master/server_log.txt", "\n".join(resume_log("OU_76561198000000000")))
    original = next(c for c in service.character_list(identifier) if c["path"] == SOURCE)
    assert original["userid"] == USER
    # A different account associated with the same encoded path makes the hint ambiguous.
    for output in resume_log("OU_76561198000000000", "KU_other123"):
        service.players.log_line(identifier, output, "Master")
    original = next(c for c in service.character_list(identifier) if c["path"] == SOURCE)
    assert original["userid"] is None
    caves = root / SOURCE.replace("Master/", "Caves/")
    atomic_write(caves / "0000000010", "cave character")
    assert next(c for c in service.character_list(identifier) if c["shard"] == "Caves")["userid"] is None
