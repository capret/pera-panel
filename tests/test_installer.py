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
    match = re.search(rf"^{name}\(\) (?:\{{[^\n]*\}}$|\{{.*?^\}}|\(.*?^\))", SCRIPT, re.M | re.S)
    assert match, name
    return match.group()


def run(code, *arguments, input_text=None):
    result = subprocess.run([BASH, "-c", "set -Eeuo pipefail\nexport PATH=/usr/bin:/bin:$PATH\n" + code, "test", *arguments],
                            capture_output=True, input=input_text.encode("utf-8") if input_text is not None else None)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    return result.stdout.decode("utf-8")


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


def test_browser_lock_is_adopted_and_extra_descriptor_closed(tmp_path):
    run(function("lock_maintenance") + r'''
exec 7>"$1"
PERA_MAINTENANCE_LOCK_FD=7
flock() { test "$*" = '-n 9'; }
die() { exit 99; }
lock_maintenance
test -z "${PERA_MAINTENANCE_LOCK_FD:-}"
test "$BROWSER_UPDATE" = 1
printf 'held' >&9
if (printf 'leaked' >&7) 2>/dev/null; then exit 98; fi
''', (tmp_path / "lock").as_posix())
    assert (tmp_path / "lock").read_text() == "held"


@pytest.mark.parametrize("address,public", [("93.184.216.34", True), ("10.0.0.4", False),
                                           ("192.168.1.8", False), ("172.20.2.5", False),
                                           ("100.64.0.2", False)])
def test_panel_ipv4_distinguishes_public_and_local_addresses(address, public):
    run(function("usable_panel_ipv4") + '\nusable_panel_ipv4 "$1"\n' +
        ('usable_panel_ipv4 "$1" public\n' if public else '! usable_panel_ipv4 "$1" public\n'), address)


@pytest.mark.parametrize("address", ["", "0.0.0.0", "0.1.2.3", "127.0.0.1", "169.254.169.254",
                                     "224.1.2.3", "255.255.255.255", "300.1.2.3", "::1",
                                     "<html>unavailable</html>", "1.2.3.4;echo injected"])
def test_panel_ipv4_rejects_invalid_or_unusable_lookup_output(address):
    run(function("usable_panel_ipv4") + '\n! usable_panel_ipv4 "$1"\n! usable_panel_ipv4 "$1" public\n', address)


def test_local_address_candidates_use_ssh_server_and_primary_route_first():
    output = run(function("local_panel_ipv4s") + r'''
SSH_CONNECTION='8.8.8.8 54500 10.0.0.5 22'
ip() {
    if [[ "$*" == '-4 route get 1.1.1.1' ]]; then
        printf '1.1.1.1 via 10.0.0.1 dev eth0 src 10.0.0.5 uid 0\n'
    else
        printf '2: eth0 inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0\n'
        printf '3: eth1 inet 93.184.216.34/24 scope global eth1\n'
    fi
}
local_panel_ipv4s
''')
    assert output.splitlines() == ["10.0.0.5", "10.0.0.5", "10.0.0.5", "93.184.216.34"]


@pytest.mark.parametrize("language", ["en", "zh-CN"])
def test_access_url_uses_public_local_address_without_external_lookup(language):
    definitions = '\n'.join(function(name) for name in ("say", "usable_panel_ipv4", "print_panel_access"))
    output = run(definitions + r'''
BIND=0.0.0.0; PORT=18080; LANGUAGE="$1"
local_panel_ipv4s() { printf '10.0.0.5\n93.184.216.34\n'; }
curl() { echo unexpected-lookup; return 1; }
print_panel_access
''', language)
    assert "http://93.184.216.34:18080" in output
    assert "unexpected-lookup" not in output and "SERVER_PUBLIC_IP" not in output


@pytest.mark.parametrize("bind", ["93.184.216.34", "192.168.1.20", "127.0.0.1"])
def test_explicit_and_loopback_binds_keep_the_actual_destination(bind):
    definitions = '\n'.join(function(name) for name in ("say", "print_panel_access"))
    output = run(definitions + r'''
BIND="$1"; PORT=18080; LANGUAGE=en
local_panel_ipv4s() { exit 99; }
curl() { exit 98; }
print_panel_access
''', bind)
    assert f"http://{bind}:18080" in output
    assert ("SSH tunnel" in output) == bind.startswith("127.")


@pytest.mark.parametrize("reply,exit_code,expected", [
    ("93.184.216.34", 0, "http://93.184.216.34:8080"),
    ("", 28, "Local network URL: http://10.0.0.5:8080"),
    ("<html>blocked</html>", 0, "Local network URL: http://10.0.0.5:8080"),
    ("127.0.0.1", 0, "Local network URL: http://10.0.0.5:8080"),
    ("10.0.0.6", 0, "Local network URL: http://10.0.0.5:8080"),
])
def test_nat_public_lookup_is_bounded_and_failure_keeps_install_successful(reply, exit_code, expected):
    definitions = '\n'.join(function(name) for name in ("say", "usable_panel_ipv4", "print_panel_access"))
    output = run(definitions + r'''
BIND=0.0.0.0; PORT=8080; LANGUAGE=en; reply="$1"; result="$2"
local_panel_ipv4s() { printf '10.0.0.5\n172.17.0.1\n'; }
curl() {
    [[ "$*" == *'--ipv4 --noproxy *'* ]] || exit 98
    [[ "$*" == *'--connect-timeout 2 --max-time 4 --max-filesize 64'* ]] || exit 98
    [[ "${!#}" == https://api.ipify.org ]] || exit 98
    printf '%s' "$reply"
    return "$result"
}
print_panel_access
echo deployment-succeeded
''', reply, str(exit_code))
    assert expected in output
    assert "deployment-succeeded" in output and "http://0.0.0.0" not in output
    assert "blocked" not in output


def test_no_address_is_reported_clearly_without_fake_url():
    definitions = '\n'.join(function(name) for name in ("say", "usable_panel_ipv4", "print_panel_access"))
    output = run(definitions + r'''
BIND=0.0.0.0; PORT=8080; LANGUAGE=en
local_panel_ipv4s() { :; }; curl() { return 28; }
print_panel_access
echo success
''')
    assert "Could not detect" in output and "8080" in output and "success" in output
    assert "http://" not in output


@pytest.mark.parametrize("browser", [0, 1])
@pytest.mark.parametrize("download_result", [0, 7])
def test_game_update_keeps_browser_online_but_stops_worlds_for_console(browser, download_result):
    output = run(function("update_game") + r'''
BROWSER_UPDATE="$1"; APP=/test-game; download_result="$2"
installed() { :; }; steam_bootstrap() { :; }; say() { printf '%s\n' "$1"; }
systemctl() { printf 'service %s\n' "$*"; }
download_game() { echo downloading; return "$download_result"; }
print_panel_access() { echo access-url; }
set +e
(set -e; update_game)
code=$?
test "$code" = "$download_result"
''', str(browser), str(download_result))
    assert "downloading" in output
    if browser:
        assert "service " not in output
        assert "keeping the panel online" in output
    else:
        assert output.index("service stop pera-panel.service") < output.index("downloading")
        assert ("service start pera-panel.service" in output) == (download_result == 0)
    assert ("access-url" in output) == (download_result == 0)


def test_manager_installs_socket_without_restarting_active_helper():
    output = run(r'''
RELEASE=/release; MANAGER=/manager
install() { printf 'install %s\n' "$*"; }
mv() { printf 'mv %s\n' "$*"; }
chmod() { :; }
cat() { command cat >/dev/null; }
systemctl() { printf 'systemctl %s\n' "$*"; }
''' + '\n' + function("write_manager").replace(
        "cat > /usr/local/bin/pera-panel", "cat") + '\nwrite_manager\n')
    assert "maintenance.py.next" in output
    assert "systemctl enable --now pera-panel-maintenance.socket" in output
    assert "systemctl restart" not in output


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


@pytest.mark.parametrize("direct_result", [0, 1, 124])
def test_source_fetch_prefers_github_and_retries_failures_in_separate_directory(tmp_path, direct_result):
    output = run(function("valid_github_proxy") + "\n" + function("prepare_source") + r'''
WORK="$1"; direct_result="$2"; SOURCE=""
REPO=https://github.com/capret/pera-panel.git; REF=feature/pause
GITHUB_PROXY=https://ghfast.top/
load_source() { :; }; info() { :; }; say() { printf '%s\n' "$1"; }
prompt_github_proxy() { exit 99; }
die() { echo "$*" >&2; exit 31; }
clone_source() {
    printf 'fetch %s %s %s\n' "$1" "$REF" "${2##*/}"
    mkdir -p "$2"
    if [[ "$1" == "$REPO" && "$direct_result" != 0 ]]; then
        touch "$2/partial-clone"
        return "$direct_result"
    fi
    touch "$2/requirements.txt" "$2/install.sh"
}
prepare_source
test "$REPO" = https://github.com/capret/pera-panel.git
test "$GITHUB_PROXY" = https://ghfast.top
test -z "$SOURCE"
test ! -e "$RELEASE_SOURCE/partial-clone"
printf 'release %s\n' "${RELEASE_SOURCE##*/}"
''', tmp_path.as_posix(), str(direct_result))
    fetches = [line for line in output.splitlines() if line.startswith("fetch ")]
    assert fetches[0] == "fetch https://github.com/capret/pera-panel.git feature/pause source"
    if direct_result:
        assert fetches[1:] == ["fetch https://ghfast.top/https://github.com/capret/pera-panel.git feature/pause source-proxy"]
        assert "release source-proxy\n" in output
    else:
        assert len(fetches) == 1
        assert "release source\n" in output


@pytest.mark.parametrize("proxy,attempts", [("https://ghfast.top", 2), ("off", 1)])
def test_failed_fetch_does_not_switch_or_stop_running_release(tmp_path, proxy, attempts):
    definitions = "\n".join(function(name) for name in ("valid_github_proxy", "prepare_source", "update_panel"))
    output = run(definitions + r'''
WORK="$1"; GITHUB_PROXY="$2"; SOURCE=""
REPO=https://github.com/capret/pera-panel.git; REF=main
load_source() { :; }; info() { :; }; say() { :; }; installed() { :; }
die() { printf '%s\n' "$*"; exit 31; }
clone_source() { printf 'fetch %s\n' "$1"; return 1; }
prepare_release() { prepare_source; echo prepared; }
switch_release() { echo switched; }
systemctl() { echo service-touched; }
set +e
(set -e; update_panel)
code=$?
test "$code" = 31
''', tmp_path.as_posix(), proxy)
    assert len([line for line in output.splitlines() if line.startswith("fetch ")]) == attempts
    assert all(marker not in output for marker in ("prepared", "switched", "service-touched"))
    assert "fetch" in output and "failed" in output


@pytest.mark.parametrize("proxy", ["http://ghfast.top", "https://user:secret@ghfast.top", "https://ghfast.top/path", "https://ghfast.top?token=secret"])
def test_invalid_proxy_is_rejected_before_fetch(tmp_path, proxy):
    run(function("valid_github_proxy") + "\n" + function("prepare_source") + r'''
WORK="$1"; GITHUB_PROXY="$2"; REPO=https://github.com/capret/pera-panel.git; REF=main
load_source() { :; }; info() { :; }; clone_source() { exit 99; }
die() { exit 31; }
set +e
(prepare_source)
code=$?
test "$code" = 31
''', tmp_path.as_posix(), proxy)


def test_public_clone_has_timeouts_and_disables_interactive_authentication():
    output = run(function("clone_source") + r'''
REF=feature/pause
timeout() { printf 'timeout %s %s %s\n' "$1" "$2" "$3"; shift 3; "$@"; }
git() {
    test "$GIT_TERMINAL_PROMPT" = 0
    test "$GIT_ASKPASS" = /bin/false
    printf '%s\n' "$@"
}
clone_source https://ghfast.top/https://github.com/capret/pera-panel.git '/tmp/path with spaces/source'
''')
    lines = output.splitlines()
    assert lines[0] == "timeout --signal=TERM --kill-after=5s 120s"
    for setting in ("core.hooksPath=/dev/null", "credential.helper=", "http.extraHeader=", "http.lowSpeedLimit=1024", "http.lowSpeedTime=20"):
        assert setting in lines
    assert lines[-8:] == ["clone", "--depth", "1", "--branch", "feature/pause", "--", "https://ghfast.top/https://github.com/capret/pera-panel.git", "/tmp/path with spaces/source"]


def test_saved_proxy_preference_and_explicit_override(tmp_path):
    definitions = "\n".join(function(name) for name in ("load_source", "save_source"))
    run(definitions + r'''
CONFIG="$1"; APP="$1/app"; SOURCE_DIR="$1/manager"
REPO=https://github.com/capret/pera-panel.git; REF=main; SOURCE=""; REF_GIVEN=0
GITHUB_PROXY=https://bridge.example:8443; GITHUB_PROXY_GIVEN=0
chown() { :; }; chmod() { :; }
save_source
GITHUB_PROXY=https://ghfast.top
load_source
test "$GITHUB_PROXY" = https://bridge.example:8443
test "$REPO" = https://github.com/capret/pera-panel.git
GITHUB_PROXY_GIVEN=1; GITHUB_PROXY=off
load_source
test "$GITHUB_PROXY" = off
save_source
GITHUB_PROXY_GIVEN=0; GITHUB_PROXY=https://ghfast.top
load_source
test "$GITHUB_PROXY" = off
# Existing installations without this setting use the new default.
printf 'SAVED_REPO=https://github.com/capret/pera-panel.git\n' > "$CONFIG/source.env"
unset SAVED_GITHUB_PROXY
load_source
test "$GITHUB_PROXY" = https://ghfast.top
''', tmp_path.as_posix())


def test_local_source_does_not_fetch_through_proxy(tmp_path):
    source = tmp_path / "local checkout"
    (source / "pera_panel").mkdir(parents=True)
    for name in ("pera_panel/web.py", "requirements.txt", "install.sh"):
        (source / name).touch()
    run(function("prepare_source") + r'''
SOURCE="$1"; REPO=""; GITHUB_PROXY=https://ghfast.top
load_source() { :; }; clone_source() { exit 99; }
die() { echo "$*" >&2; exit 31; }
prepare_source
test "$RELEASE_SOURCE" = "$(cd -- "$1" && pwd -P)"
''', source.as_posix())


@pytest.mark.parametrize("configured,answer,expected", [
    ("https://ghfast.top", "\n", "https://ghfast.top"),
    ("https://saved.example", "\n", "https://saved.example"),
    ("https://ghfast.top", "https://replacement.example:8443/\n", "https://replacement.example:8443"),
    ("https://ghfast.top", "http://invalid.example\nhttps://replacement.example\n", "https://replacement.example"),
    ("https://ghfast.top", "https://user:secret@invalid.example\nhttps://invalid.example/path\n\n", "https://ghfast.top"),
    ("https://ghfast.top", "off\n", "off"),
])
def test_proxy_prompt_accepts_default_replacement_or_cancel_and_reprompts_invalid_input(configured, answer, expected):
    definitions = "\n".join(function(name) for name in ("say", "valid_github_proxy", "prompt_github_proxy"))
    output = run(definitions + r'''
LANGUAGE=en; GITHUB_PROXY="$1"
die() { echo "$*" >&2; exit 31; }
prompt_github_proxy
printf 'selected %s\n' "$GITHUB_PROXY"
''', configured, input_text=answer)
    assert f"selected {expected}\n" in output
    assert "Press Enter" in output
    assert ("Enter an HTTPS origin" in output) == ("invalid.example" in answer)


@pytest.mark.parametrize("language,cancelled", [("en", "Proxy fallback cancelled."), ("zh-CN", "已取消代理重试。")])
def test_proxy_prompt_eof_cancels_without_using_default(language, cancelled):
    definitions = "\n".join(function(name) for name in ("say", "valid_github_proxy", "prompt_github_proxy"))
    output = run(definitions + r'''
LANGUAGE="$1"; GITHUB_PROXY=https://ghfast.top
die() { printf '%s\n' "$*"; exit 31; }
set +e
(prompt_github_proxy; echo continued)
code=$?
test "$code" = 31
''', language, input_text="")
    assert cancelled in output
    assert "continued" not in output
