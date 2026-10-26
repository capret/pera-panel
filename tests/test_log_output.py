"""Browser log formatting is shared by software updates and shard/mod logs."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed for frontend logic checks")
def test_log_output_frontend_logic():
    result = subprocess.run([shutil.which("node"), "--test", str(Path(__file__).with_name("log-output.test.cjs"))],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


def test_log_formatter_loads_before_the_dashboard(client, auth):
    html = client.get("/overview").data.decode()
    assert html.index("/static/log-output.js") < html.index("/static/app.js")
