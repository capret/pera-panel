import json
import sys

from pera_panel.__main__ import main


def test_network_reconfiguration_preserves_admin_and_world_paths(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    config = {"host": "127.0.0.1", "port": 8080, "password_hash": "existing-hash",
              "secret_key": "existing-key", "data_dir": "/worlds", "game_dir": "/game", "username": "admin"}
    path.write_text(json.dumps(config))
    monkeypatch.setattr(sys, "argv", ["pera_panel", "init-admin", "--config", str(path),
                                    "--configure-network", "--host", "0.0.0.0", "--port", "18080"])
    main()
    assert json.loads(path.read_text()) == {**config, "host": "0.0.0.0", "port": 18080}
