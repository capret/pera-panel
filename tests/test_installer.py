"""Exercise installer functions with command doubles; never touch host services/firewalls."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

SCRIPT = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH, reason="Bash is needed for installer checks")


def function(name):
    match = re.search(rf"^{name}\(\) (?:\{{.*?^\}}|\(.*?^\))", SCRIPT, re.M | re.S)
    assert match, name
    return match.group()


def run(code, *arguments):
    result = subprocess.run([BASH, "-c", "set -Eeuo pipefail\nexport PATH=/usr/bin:/bin:$PATH\n" + code, "test", *arguments],
                            text=True, capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_validate_bind_and_ports_reject_shell_input_and_out_of_range():
    run(function("valid_bind") + "\n" + function("valid_port") + '''
valid_bind 0.0.0.0
valid_bind 127.0.0.1
! valid_bind 300.1.1.1
! valid_bind '0.0.0.0;echo injected'
valid_port 8080
valid_port 65535
! valid_port 65536
! valid_port 0
! valid_port '-1'
! valid_port '80/tcp'
''')


def test_game_user_commands_use_accessible_home_not_callers_directory(tmp_path):
    home = tmp_path / "game home"
    home.mkdir()
    output = run(function("as_game_user") + '''
DATA="$1"
runuser() { pwd; printf '%s\n' "$@"; }
as_game_user ln -sfnT /source /destination
''', home.as_posix())
    assert "game home" in output.splitlines()[0]
    assert "HOME=" in output and "-sfnT" in output


def test_firewall_preserves_ssh_before_enabling_and_never_resets():
    definitions = "\n".join(function(name) for name in ("valid_port", "configure_firewall"))
    output = run(definitions + '''
NETWORK_SETUP=1; FIREWALL=auto; SSH_PORT=2222; PORT=18080; BIND=0.0.0.0
die() { echo "$*" >&2; exit 1; }
say() { printf '%s\n' "$1"; }
info() { :; }
ssh_ports() { printf '22\n2200\n'; }
ufw() { printf 'ufw %s\n' "$*"; }
configure_firewall
''')
    assert "ufw allow 18080/tcp" in output
    for port in ("22/tcp", "2200/tcp", "2222/tcp", "10999:11000/udp"):
        assert output.index("ufw allow " + port) < output.index("ufw --force enable")
    assert "ufw reset" not in output and "10888" not in output
    udp_rules = [line for line in output.splitlines() if line.startswith("ufw allow ") and "/udp" in line]
    assert udp_rules == ["ufw allow 10999:11000/udp comment DST Pera Panel"]


def test_manual_firewall_and_ordinary_updates_do_not_modify_rules():
    run(function("configure_firewall") + '''
ufw() { exit 99; }
NETWORK_SETUP=0; FIREWALL=auto
configure_firewall
NETWORK_SETUP=1; FIREWALL=manual
configure_firewall
''')


def test_first_install_defaults_and_saved_update_networking(tmp_path):
    definitions = "\n".join(function(name) for name in ("valid_port", "valid_bind", "setup_network"))
    output = run(definitions + '''
CONFIG="$1"; BIND=0.0.0.0; PORT=8080; FIREWALL=auto; SSH_PORT=""; LANGUAGE=en
BIND_GIVEN=0; PORT_GIVEN=0; FIREWALL_GIVEN=0; NETWORK_SETUP=0; YES=1; COMMAND=install
say() { printf '%s\n' "$1"; }
die() { echo "$*" >&2; exit 1; }
ssh_ports() { printf '2222\n'; }
setup_network
test "$BIND:$PORT:$SSH_PORT:$NETWORK_SETUP" = '0.0.0.0:8080:2222:1'
touch "$CONFIG/config.json"
python3() { printf '127.0.0.1 9090\n'; }
COMMAND=update-panel; NETWORK_SETUP=0
setup_network
test "$BIND:$PORT:$NETWORK_SETUP" = '127.0.0.1:9090:0'
BIND_GIVEN=1; PORT_GIVEN=1; BIND=0.0.0.0; PORT=18080
setup_network
test "$BIND:$PORT:$NETWORK_SETUP" = '0.0.0.0:18080:1'
''', tmp_path.as_posix())
    assert '0.0.0.0:8080' in output and '127.0.0.1:9090' in output


def test_steam_directories_repairs_all_parents_and_links_as_service_user():
    output = run("\n".join(function(name) for name in ("steam_directories", "steam_links")) + '''
DATA=/test-pera-data; APP=/test-pera-app
die() { exit 1; }; info() { :; }
install() { printf 'install %s\n' "$*"; }
as_game_user() { printf 'user %s\n' "$*"; }
steam_links /test-pera-game
''')
    for directory in (".steam", ".steam/sdk32", ".steam/sdk64"):
        assert f"install -d -o pera-panel -g pera-panel -m 750 /test-pera-data/{directory}\n" in output
    assert "user ln -sfnT" in output


def test_systemd_unreachable_stops_with_recovery_guidance():
    output = run(function("check_systemd") + '''
systemctl() { return 1; }
sleep() { :; }
die() { printf '%s\n' "$*"; exit 31; }
set +e
(check_systemd)
code=$?
test "$code" = 31
''')
    assert "Reboot the server" in output and "downloads and saves will be reused" in output
