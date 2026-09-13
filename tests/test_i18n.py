"""Guard translation coverage and stable API messages used by either UI language."""
import ast
import json
from pathlib import Path
import re

from pera_panel.storage import PanelError, validate_world
from test_web import wait_job

ROOT = Path(__file__).resolve().parents[1] / "pera_panel"
CATALOG = json.loads((ROOT / "static/zh-CN.json").read_text(encoding="utf-8"))


def test_all_panel_errors_have_chinese_translations():
    missing = []
    for path in ROOT.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "PanelError":
                assert isinstance(node.args[0], ast.Constant), f"Use parameterized messages in {path.name}"
                if node.args[0].value not in CATALOG:
                    missing.append(node.args[0].value)
    assert not missing


def test_translation_placeholders_match_source():
    for source, translation in CATALOG.items():
        assert translation.strip()
        assert set(re.findall(r"\{(\w+)\}", source)) == set(re.findall(r"\{(\w+)\}", translation)), source


def test_parameterized_errors_preserve_english_api_and_expose_localization(client, auth, service):
    response = client.post("/api/worlds", json={"name": "Camp", "max_players": 100}, headers=auth)
    assert response.status_code == 202
    job = wait_job(service)
    assert job["error"] == "Invalid max_players."
    assert job["error_i18n"] == {"key": "Invalid {key}.", "params": {"key": "max_players"}}
    status = client.get("/api/status").json
    assert status["jobs"][0]["state"] == "failed"
    assert status["jobs"][0]["title"] == "Create world"


def test_login_and_panel_have_language_controls(client, auth):
    for authenticated in (True, False):
        if not authenticated:
            client.post("/api/logout", headers=auth)
        html = client.get("/").data.decode()
        assert 'data-language' in html and 'value="zh-CN"' in html and 'value="en"' in html
        assert '/static/i18n.js' in html and 'id="i18n-catalog"' in html


def test_dynamic_error_formatting_and_user_data_are_unchanged():
    error = PanelError("{name} is required.", params={"name": "name"})
    assert str(error) == "name is required."
    world = validate_world({"name": "Overview", "description": "世界名称"})
    assert world["name"] == "Overview" and world["description"] == "世界名称"


def test_only_system_backup_labels_are_marked_for_translation(service):
    identifier = service.create({"name": "Camp"})["world_id"]
    service.backup(identifier, "Before rollback")
    assert service.backups(identifier)[0]["label_i18n"] is None
    service._snapshot(identifier, "Before rollback", system_label=True)
    labels = service.backups(identifier)
    assert any(item["label_i18n"] == "Before rollback" for item in labels)
