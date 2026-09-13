#!/usr/bin/env bash
# Pera Panel: Ubuntu/Debian installer and repeatable maintenance command.
set -Eeuo pipefail
umask 027

APP=/opt/pera-panel
DATA=/var/lib/pera-panel
CONFIG=/etc/pera-panel
MANAGER=/usr/local/lib/pera-panel
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
COMMAND=""
REPO="${PERA_REPO:-}"
REF="${PERA_REF:-main}"
REF_GIVEN=0
[[ -z "${PERA_REF:-}" ]] || REF_GIVEN=1
SOURCE=""
YES=0
PURGE=0
WORK=""

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
info() { printf '\n[pera] %s\n' "$*"; }
cleanup() {
    if [[ -n "$WORK" && "$WORK" == /tmp/pera-panel.* && -d "$WORK" ]]; then
        rm -rf -- "$WORK"
    fi
}
trap cleanup EXIT
trap 'printf "\nPera operation failed at line %s. Your saved worlds remain in /var/lib/pera-panel.\n" "$LINENO" >&2' ERR

usage() {
    cat <<'HELP'
Pera Panel · Don’t Starve Together

sudo bash install.sh [command] [options]
sudo pera-panel                         Open the maintenance menu

Commands:
  install          Install dependencies, SteamCMD, DST, panel and systemd service
  update           Update the panel and game (keeps all worlds and credentials)
  update-panel     Deploy a fresh panel release from the saved source
  update-game      Update/validate DST with SteamCMD
  reinstall        Fresh game and panel installation; keeps world data and login
  start|stop|restart|status|logs
  reset-password   Generate a new admin password and invalidate old sessions
  uninstall        Remove software and service; keep data and credentials
  help

Options:
  --repo URL       Your HTTPS GitHub repository (saved for subsequent updates)
  --ref REF        Branch or tag, default main
  --source PATH    Local checkout/copy on the server (saved for updates)
  --yes            Noninteractive uninstall confirmation
  --purge          With uninstall: also erase worlds, backups and credentials

Upload the complete project and run: sudo bash install.sh install
Or download this script and run:
  sudo bash install.sh install --repo https://github.com/YOU/pera-panel.git

First install binds the panel to 127.0.0.1:8080. Connect through an SSH tunnel.
PERA_BIND and PERA_PORT override this on first install only.
HELP
}

while (($#)); do
    case "$1" in
        --repo|--ref|--source)
            (($# >= 2)) || die "$1 requires a value"
            case "$1" in --repo) REPO="$2";; --ref) REF="$2"; REF_GIVEN=1;; --source) SOURCE="$2";; esac
            shift 2 ;;
        --yes) YES=1; shift ;;
        --purge) PURGE=1; shift ;;
        -h|--help) COMMAND=help; shift ;;
        *) [[ -z "$COMMAND" ]] || die "Unexpected argument: $1"; COMMAND="$1"; shift ;;
    esac
done
[[ "$COMMAND" != help ]] || { usage; exit 0; }
[[ "$EUID" -eq 0 ]] || die "Run this command with sudo."

load_source() {
    local requested_repo="$REPO" requested_ref="$REF" requested_source="$SOURCE"
    if [[ -f "$CONFIG/source.env" ]]; then
        # This file and its parent are root-owned and not writable by the panel user.
        # shellcheck disable=SC1091
        source "$CONFIG/source.env"
        REPO="${requested_repo:-${SAVED_REPO:-}}"
        REF="${PERA_REF:-${SAVED_REF:-main}}"
        (( ! REF_GIVEN )) || REF="$requested_ref"
        SOURCE="${requested_source:-${SAVED_SOURCE:-}}"
    fi
    if [[ -n "$requested_source" ]]; then
        REPO=""  # An explicit local source overrides the saved remote.
    elif [[ -n "$requested_repo" ]]; then
        SOURCE=""
    elif [[ -f "$SOURCE_DIR/pera_panel/web.py" && "$SOURCE_DIR" != "$APP"/* ]]; then
        SOURCE="$SOURCE_DIR"
        REPO=""
    fi
}

platform() {
    [[ -r /etc/os-release ]] || die "Ubuntu or Debian is required."
    # shellcheck disable=SC1091
    source /etc/os-release
    [[ "$ID" == ubuntu || "$ID" == debian ]] || die "Supported systems: Ubuntu 22.04+, Debian 12+."
    [[ "$(uname -m)" == x86_64 ]] || die "An x86_64 server is required; native ARM is not supported by this installer."
    [[ -d /run/systemd/system ]] || die "A running systemd installation is required."
    if [[ "$ID" == ubuntu ]]; then
        dpkg --compare-versions "$VERSION_ID" ge 22.04 || die "Ubuntu 22.04+ is required."
    else
        dpkg --compare-versions "$VERSION_ID" ge 12 || die "Debian 12+ is required."
    fi
}

dependencies() {
    platform
    info "Installing Python and SteamCMD runtime dependencies"
    export DEBIAN_FRONTEND=noninteractive
    dpkg --add-architecture i386
    apt-get update
    apt-get install -y ca-certificates curl git python3 python3-venv python3-pip \
        lib32gcc-s1 lib32stdc++6 libcurl4 libstdc++6 libcurl4:i386 tar util-linux
    if ! id pera-panel >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "$DATA" --create-home --shell /usr/sbin/nologin pera-panel
    fi
    install -d -o root -g root -m 755 "$APP" "$APP/releases" "$MANAGER"
    install -d -o root -g pera-panel -m 750 "$CONFIG"
    install -d -o pera-panel -g pera-panel -m 750 "$DATA" "$APP/game" "$APP/steamcmd"
    # Ownership is intentionally restricted to game files and persistent data.
    chown -R pera-panel:pera-panel "$DATA" "$APP/game" "$APP/steamcmd"
}

as_game_user() { runuser -u pera-panel -- env HOME="$DATA" "$@" 9>&-; }

stop_panel() {
    if systemctl cat pera-panel.service >/dev/null 2>&1; then
        systemctl stop pera-panel.service
    fi
}

steam_bootstrap() {
    if [[ ! -f "$APP/steamcmd/steamcmd.sh" ]]; then
        info "Downloading Valve SteamCMD"
        curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
            https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz -o "$WORK/steamcmd.tar.gz"
        chmod 644 "$WORK/steamcmd.tar.gz"
        # Extract as the game user so the network archive cannot write root-owned files.
        chmod 755 "$WORK"
        as_game_user tar -xzf "$WORK/steamcmd.tar.gz" -C "$APP/steamcmd" --no-same-owner
    fi
    install -d -o pera-panel -g pera-panel -m 750 "$DATA/.steam/sdk32" "$DATA/.steam/sdk64"
}

download_game() {
    local destination="$1"
    info "Installing/updating Don’t Starve Together (Steam app 343050). This may take several minutes."
    as_game_user "$APP/steamcmd/steamcmd.sh" +force_install_dir "$destination" \
        +login anonymous +app_update 343050 validate +quit
    [[ -x "$destination/bin64/dontstarve_dedicated_server_nullrenderer_x64" ]] || die "SteamCMD did not produce the DST server executable. Retry update-game."
    as_game_user ln -sfn "$APP/steamcmd/linux32/steamclient.so" "$DATA/.steam/sdk32/steamclient.so"
    if [[ -f "$APP/steamcmd/linux64/steamclient.so" ]]; then
        as_game_user ln -sfn "$APP/steamcmd/linux64/steamclient.so" "$DATA/.steam/sdk64/steamclient.so"
    elif [[ -f "$destination/bin64/lib64/steamclient.so" ]]; then
        as_game_user ln -sfn "$destination/bin64/lib64/steamclient.so" "$DATA/.steam/sdk64/steamclient.so"
    fi
}

prepare_source() {
    load_source
    if [[ -n "$REPO" ]]; then
        [[ "$REPO" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?$ ]] || die "--repo must be an HTTPS GitHub repository URL."
        [[ "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || die "Invalid branch or tag."
        info "Fetching $REPO ($REF)"
        git -c core.hooksPath=/dev/null clone --depth 1 --branch "$REF" -- "$REPO" "$WORK/source"
        RELEASE_SOURCE="$WORK/source"
        SOURCE=""
    else
        [[ -n "$SOURCE" && -f "$SOURCE/pera_panel/web.py" ]] || die "Provide the full project using --source /path/to/project or --repo https://github.com/YOU/pera-panel.git"
        SOURCE="$(cd -- "$SOURCE" && pwd -P)"
        RELEASE_SOURCE="$SOURCE"
    fi
    [[ -f "$RELEASE_SOURCE/requirements.txt" && -f "$RELEASE_SOURCE/install.sh" ]] || die "Incomplete project source."
}

prepare_release() {
    prepare_source
    RELEASE="$APP/releases/$(date -u +%Y%m%dT%H%M%S)-$$"
    install -d -o root -g root -m 755 "$RELEASE"
    cp -R -- "$RELEASE_SOURCE/pera_panel" "$RELEASE/pera_panel"
    cp -- "$RELEASE_SOURCE/requirements.txt" "$RELEASE_SOURCE/install.sh" "$RELEASE/"
    cp -R -- "$RELEASE_SOURCE/deploy" "$RELEASE/deploy"
    python3 -m venv "$RELEASE/.venv"
    "$RELEASE/.venv/bin/pip" install --disable-pip-version-check -r "$RELEASE/requirements.txt"
    "$RELEASE/.venv/bin/python" -m compileall -q "$RELEASE/pera_panel"
    (cd "$RELEASE" && "$RELEASE/.venv/bin/python" -c 'from pera_panel.web import create_app; import waitress, psutil')
    chown -R root:root "$RELEASE"
    chmod -R go-w "$RELEASE"
    chmod -R a+rX "$RELEASE"
}

save_source() {
    printf 'SAVED_REPO=%q\nSAVED_REF=%q\nSAVED_SOURCE=%q\n' "$REPO" "$REF" "$SOURCE" > "$CONFIG/source.env"
    chown root:root "$CONFIG/source.env"
    chmod 600 "$CONFIG/source.env"
}

write_manager() {
    install -m 755 "$RELEASE/install.sh" "$MANAGER/install.sh.next"
    mv -f "$MANAGER/install.sh.next" "$MANAGER/install.sh"
    cat > /usr/local/bin/pera-panel <<'WRAPPER'
#!/usr/bin/env bash
exec bash /usr/local/lib/pera-panel/install.sh "$@"
WRAPPER
    chmod 755 /usr/local/bin/pera-panel
}

switch_release() {
    local previous=""
    previous="$(readlink -f "$APP/current" 2>/dev/null || true)"
    stop_panel
    ln -sfn "$RELEASE" "$APP/current.next"
    mv -Tf "$APP/current.next" "$APP/current"
    install -m 644 "$RELEASE/deploy/pera-panel.service" /etc/systemd/system/pera-panel.service
    (cd "$RELEASE" && "$RELEASE/.venv/bin/python" -m pera_panel init-admin \
        --host "${PERA_BIND:-127.0.0.1}" --port "${PERA_PORT:-8080}")
    chown root:pera-panel "$CONFIG/config.json"
    chmod 640 "$CONFIG/config.json"
    systemctl daemon-reload
    systemctl enable pera-panel.service
    systemctl start pera-panel.service || true
    local healthy=0 health_url
    health_url="$("$RELEASE/.venv/bin/python" -c 'import json; c=json.load(open("/etc/pera-panel/config.json")); h=c["host"]; h={"0.0.0.0":"127.0.0.1","::":"::1"}.get(h,h); h="["+h+"]" if ":" in h else h; print("http://"+h+":"+str(c["port"])+"/healthz")')"
    for _ in {1..15}; do
        if systemctl is-active --quiet pera-panel.service && curl --noproxy '*' -fsS --max-time 2 "$health_url" >/dev/null 2>&1; then
            healthy=1
            break
        fi
        sleep 1
    done
    if (( ! healthy )); then
        if [[ "$previous" == "$APP/releases/"* && -d "$previous" ]]; then
            ln -sfn "$previous" "$APP/current.next"
            mv -Tf "$APP/current.next" "$APP/current"
            install -m 644 "$previous/deploy/pera-panel.service" /etc/systemd/system/pera-panel.service
            systemctl daemon-reload
            systemctl restart pera-panel.service || true
        fi
        die "The new panel failed to start; previous release restored when available. Run journalctl -u pera-panel -n 80."
    fi
    save_source
    write_manager
    info "Panel ready. World data: $DATA"
    printf 'From your computer: ssh -L 8080:127.0.0.1:8080 USER@SERVER\nThen open http://127.0.0.1:8080\n'
    printf 'Run sudo pera-panel to reopen the maintenance menu.\n'
}

installed() { [[ -f "$CONFIG/config.json" && -d "$APP/current" ]] || die "Install Pera Panel first."; }

install_all() {
    dependencies
    prepare_release
    steam_bootstrap
    stop_panel
    download_game "$APP/game"
    switch_release
}

update_panel() {
    installed
    prepare_release
    switch_release
}

update_game() {
    installed
    steam_bootstrap
    systemctl stop pera-panel.service
    download_game "$APP/game"
    systemctl start pera-panel.service
}

reinstall() {
    dependencies
    prepare_release
    steam_bootstrap
    local replacement
    replacement="$APP/game-new-$(date -u +%Y%m%dT%H%M%S)-$$"
    install -d -o pera-panel -g pera-panel -m 750 "$replacement"
    stop_panel
    download_game "$replacement"
    # Keep the prior game installation as a recovery copy. Never delete world data.
    if [[ -d "$APP/game" ]]; then mv -- "$APP/game" "$APP/game-previous-$(date -u +%Y%m%dT%H%M%S)-$$"; fi
    mv -- "$replacement" "$APP/game"
    if [[ -f "$APP/steamcmd/linux64/steamclient.so" ]]; then
        as_game_user ln -sfn "$APP/steamcmd/linux64/steamclient.so" "$DATA/.steam/sdk64/steamclient.so"
    elif [[ -f "$APP/game/bin64/lib64/steamclient.so" ]]; then
        as_game_user ln -sfn "$APP/game/bin64/lib64/steamclient.so" "$DATA/.steam/sdk64/steamclient.so"
    fi
    switch_release
}

uninstall_panel() {
    local answer=""
    if (( ! YES )); then
        [[ -t 0 ]] || die "Use --yes for a noninteractive uninstall."
        read -r -p 'Remove Pera Panel software and stop all game processes? [y/N] ' answer
        [[ "$answer" == y || "$answer" == Y ]] || return 0
    fi
    if (( PURGE )); then
        if (( ! YES )); then
            read -r -p 'This permanently deletes ALL worlds, backups and credentials. Type DELETE: ' answer
            [[ "$answer" == DELETE ]] || die "Purge cancelled."
        fi
    fi
    stop_panel
    systemctl disable pera-panel.service 2>/dev/null || true
    rm -f -- /etc/systemd/system/pera-panel.service /usr/local/bin/pera-panel
    # Fixed absolute application paths only. Persistent data has a separate, explicit purge path.
    rm -rf -- /opt/pera-panel /usr/local/lib/pera-panel
    systemctl daemon-reload
    if (( PURGE )); then
        rm -rf -- /var/lib/pera-panel /etc/pera-panel
        if id pera-panel >/dev/null 2>&1; then userdel pera-panel; fi
        info "Software, worlds, backups and credentials removed."
    else
        info "Software removed. Worlds/backups kept at $DATA; credentials kept at $CONFIG."
        printf 'Run the original project install.sh to install again.\n'
    fi
}

status() {
    systemctl --no-pager --full status pera-panel.service || true
    printf '\nGame processes (owned by pera-panel):\n'
    if id pera-panel >/dev/null 2>&1; then pgrep -a -u pera-panel -f 'dontstarve_dedicated_server' || true; fi
    printf '\nPersistent data: %s\n' "$DATA"
    df -h "$DATA" 2>/dev/null || true
}

menu() {
    [[ -t 0 ]] || die "Specify a command in noninteractive mode; see --help."
    cat <<'MENU'

  P E R A   /   P A N E L
  Don’t Starve Together, on your terms.

   1) Install                 7) Stop panel + worlds
   2) Update panel + game     8) Restart panel
   3) Update panel            9) Status
   4) Update game            10) Logs
   5) Reinstall              11) Reset admin password
   6) Start panel            12) Uninstall
   0) Exit

MENU
    local choice
    read -r -p 'Select an operation: ' choice
    case "$choice" in
        1) COMMAND=install;; 2) COMMAND=update;; 3) COMMAND=update-panel;; 4) COMMAND=update-game;;
        5) COMMAND=reinstall;; 6) COMMAND=start;; 7) COMMAND=stop;; 8) COMMAND=restart;;
        9) COMMAND=status;; 10) COMMAND=logs;; 11) COMMAND=reset-password;; 12) COMMAND=uninstall;;
        0) exit 0;; *) die "Unknown selection.";;
    esac
}

if [[ -z "$COMMAND" ]]; then
    if [[ -f "$CONFIG/config.json" ]]; then menu; else COMMAND=install; fi
fi
case "$COMMAND" in
    install|update|update-panel|update-game|reinstall|start|stop|restart|status|logs|reset-password|uninstall) ;;
    *) die "Unknown command: $COMMAND. Use --help." ;;
esac
if (( PURGE )) && [[ "$COMMAND" != uninstall ]]; then die "--purge is only valid with uninstall."; fi
if [[ "$COMMAND" != status && "$COMMAND" != logs ]]; then
    exec 9>/run/lock/pera-panel-maintenance.lock
    flock -n 9 || die "Another maintenance operation is already running."
fi
WORK="$(mktemp -d /tmp/pera-panel.XXXXXXXX)"
case "$COMMAND" in
    install|update) install_all ;;
    update-panel) update_panel ;;
    update-game) update_game ;;
    reinstall) reinstall ;;
    start|stop|restart) installed; systemctl "$COMMAND" pera-panel.service ;;
    status) status ;;
    logs) journalctl -u pera-panel.service -n 100 -f ;;
    reset-password)
        installed
        (cd "$APP/current" && .venv/bin/python -m pera_panel init-admin --reset)
        chown root:pera-panel "$CONFIG/config.json"
        chmod 640 "$CONFIG/config.json"
        systemctl restart pera-panel.service ;;
    uninstall) uninstall_panel ;;
esac
