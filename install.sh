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
BIND="${PERA_BIND:-0.0.0.0}"
PORT="${PERA_PORT:-8080}"
FIREWALL="${PERA_FIREWALL:-auto}"
SSH_PORT="${PERA_SSH_PORT:-}"
LANGUAGE="${PERA_LANG:-en}"
BIND_GIVEN=0; [[ -z "${PERA_BIND:-}" ]] || BIND_GIVEN=1
PORT_GIVEN=0; [[ -z "${PERA_PORT:-}" ]] || PORT_GIVEN=1
FIREWALL_GIVEN=0; [[ -z "${PERA_FIREWALL:-}" ]] || FIREWALL_GIVEN=1
NETWORK_SETUP=0

say() { if [[ "$LANGUAGE" == zh-CN ]]; then printf '%s\n' "$2"; else printf '%s\n' "$1"; fi; }

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
  configure        Change panel bind/port and configure firewall ports
  uninstall        Remove software and service; keep data and credentials
  help

Options:
  --repo URL       Your HTTPS GitHub repository (saved for subsequent updates)
  --ref REF        Branch or tag, default main
  --source PATH    Local checkout/copy on the server (saved for updates)
  --bind ADDRESS   Panel IPv4 bind address (default 0.0.0.0)
  --port PORT      Panel TCP port (default 8080, range 1024–65535)
  --firewall MODE  auto: add UFW rules and enable UFW; manual: leave firewall alone
  --ssh-port PORT  Additional SSH TCP port to preserve (also detects sshd listeners)
  --lang LANGUAGE  en or zh-CN for setup and maintenance menus
  --yes            Accept setup defaults / noninteractive uninstall confirmation
  --purge          With uninstall: also erase worlds, backups and credentials

Upload the complete project and run: sudo bash install.sh install
Or download this script and run:
  sudo bash install.sh install --repo https://github.com/YOU/pera-panel.git

Interactive installation starts with language, bind address, port, and firewall settings.
Default: 0.0.0.0:8080, automatic UFW rules. Cloud security groups need provider-side rules.
PERA_BIND, PERA_PORT, PERA_FIREWALL, PERA_SSH_PORT, and PERA_LANG set defaults.
HELP
}

while (($#)); do
    case "$1" in
        --repo|--ref|--source|--bind|--port|--firewall|--ssh-port|--lang)
            (($# >= 2)) || die "$1 requires a value"
            case "$1" in
                --repo) REPO="$2";; --ref) REF="$2"; REF_GIVEN=1;; --source) SOURCE="$2";;
                --bind) BIND="$2"; BIND_GIVEN=1;; --port) PORT="$2"; PORT_GIVEN=1;;
                --firewall) FIREWALL="$2"; FIREWALL_GIVEN=1;; --ssh-port) SSH_PORT="$2";; --lang) LANGUAGE="$2";;
            esac
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

check_systemd() {
    local _attempt
    for _attempt in {1..5}; do
        if systemctl show --property=Version --value >/dev/null 2>&1; then return; fi
        sleep 2
    done
    die "systemd is unreachable (system bus/manager connection failed). Reboot the server after package upgrades, then rerun this installer. Existing downloads and saves will be reused."
}

valid_port() { [[ "$1" =~ ^[0-9]{1,5}$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 )); }
valid_bind() {
    local octet
    local -a octets
    [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    IFS=. read -r -a octets <<< "$1"
    for octet in "${octets[@]}"; do (( 10#$octet <= 255 )) || return 1; done
}

ssh_ports() {
    local _client_ip _client_port _server_ip server_port address
    if [[ -n "${SSH_CONNECTION:-}" ]]; then
        read -r _client_ip _client_port _server_ip server_port <<< "$SSH_CONNECTION"
        if valid_port "$server_port"; then printf '%s\n' "$server_port"; fi
    fi
    if command -v sshd >/dev/null; then
        sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' || true
    fi
    if command -v ss >/dev/null; then
        while read -r address; do
            server_port="${address##*:}"
            if valid_port "$server_port"; then printf '%s\n' "$server_port"; fi
        done < <(ss -H -ltnp 2>/dev/null | awk '/"sshd"|"sshd-session"/ {print $4}')
    fi
}

setup_network() {
    local answer saved detected
    if [[ -f "$CONFIG/network.env" ]] && (( ! FIREWALL_GIVEN )); then
        # Root-owned installation preferences, never writable by the panel service.
        # shellcheck disable=SC1091
        source "$CONFIG/network.env"
        FIREWALL="${SAVED_FIREWALL:-auto}"
    fi
    if [[ -f "$CONFIG/config.json" ]]; then
        saved="$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c["host"], c["port"])' "$CONFIG/config.json")"
        if (( ! BIND_GIVEN )); then BIND="${saved% *}"; fi
        if (( ! PORT_GIVEN )); then PORT="${saved##* }"; fi
    fi
    detected="$(ssh_ports | sort -nu | head -1)"
    SSH_PORT="${SSH_PORT:-${detected:-22}}"
    if [[ "$COMMAND" == install || "$COMMAND" == reinstall || "$COMMAND" == configure || ! -f "$CONFIG/config.json" ]] || (( BIND_GIVEN || PORT_GIVEN || FIREWALL_GIVEN )); then
        NETWORK_SETUP=1
        if [[ -t 0 ]] && (( ! YES )); then
            printf '\nPera Panel · Setup / 安装设置\n1) 简体中文  2) English\n'
            read -r -p "Language / 语言 [${LANGUAGE}]: " answer
            case "$answer" in 1|zh|zh-CN) LANGUAGE=zh-CN;; 2|en) LANGUAGE=en;; "") :;; *) die "Choose 1 (简体中文) or 2 (English).";; esac
            say 'Step 1/3 · Panel address and port' '第 1/3 步 · 面板地址与端口'
            read -r -p "$(say 'Bind IPv4 address' '监听 IPv4 地址') [$BIND]: " answer
            BIND="${answer:-$BIND}"
            read -r -p "$(say 'Panel TCP port' '面板 TCP 端口') [$PORT]: " answer
            PORT="${answer:-$PORT}"
            say 'Step 2/3 · Firewall (auto enables UFW; manual leaves it unchanged)' '第 2/3 步 · 防火墙（auto 自动启用 UFW；manual 不修改防火墙）'
            read -r -p "$(say 'Firewall mode' '防火墙模式') [${FIREWALL}]: " answer
            FIREWALL="${answer:-$FIREWALL}"
            if [[ "$FIREWALL" == auto ]]; then
                read -r -p "$(say 'SSH TCP port to preserve (detected listeners are also allowed)' '保留的 SSH TCP 端口（也会放行检测到的 SSH 监听端口）') [$SSH_PORT]: " answer
                SSH_PORT="${answer:-$SSH_PORT}"
            fi
            say 'Step 3/3 · Applying setup and installing' '第 3/3 步 · 应用设置并开始安装'
        fi
    fi
    valid_bind "$BIND" || die "Use an IPv4 address, e.g. 0.0.0.0 or 127.0.0.1."
    if ! valid_port "$PORT" || (( 10#$PORT < 1024 )); then die "Panel port must be 1024–65535."; fi
    PORT="$((10#$PORT))"
    valid_port "$SSH_PORT" || die "SSH port must be 1–65535."
    [[ "$FIREWALL" == auto || "$FIREWALL" == manual ]] || die "Firewall mode must be auto or manual."
    [[ "$LANGUAGE" == en || "$LANGUAGE" == zh-CN ]] || die "Language must be en or zh-CN."
    say "Panel: $BIND:$PORT | Firewall: $FIREWALL | SSH: $SSH_PORT" "面板：$BIND:$PORT | 防火墙：$FIREWALL | SSH：$SSH_PORT"
}

configure_firewall() {
    (( NETWORK_SETUP )) || return 0
    [[ "$FIREWALL" == auto ]] || return 0
    info "$(say 'Allowing SSH, panel TCP, and DST UDP ports in UFW' '正在通过 UFW 放行 SSH、面板 TCP 和饥荒 UDP 端口')"
    if ! command -v ufw >/dev/null; then
        DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y ufw
    fi
    local port ssh_rules
    ssh_rules="$({ ssh_ports; printf '%s\n' "$SSH_PORT"; } | sort -nu)"
    [[ -n "$ssh_rules" ]] || die "No SSH ports available; refusing to enable UFW."
    while read -r port; do
        valid_port "$port" || die "Invalid detected SSH port. Use --firewall manual to configure it yourself."
        ufw allow "$port/tcp" comment 'SSH preserved by Pera Panel'
    done <<< "$ssh_rules"
    if [[ "$BIND" != 127.* ]]; then ufw allow "$PORT/tcp" comment 'Pera Panel'; fi
    # Public player connections use the two NETWORK/server_port values. Steam's
    # internal authentication/master ports do not need blanket inbound rules.
    ufw allow 10999:11000/udp comment 'DST Pera Panel'
    # Keep existing rules/policies. Add SSH exceptions BEFORE activating the firewall.
    ufw --force enable
    ufw status
    say 'Cloud/VPS security groups are separate: allow the panel TCP port and the listed DST UDP ports in your provider console.' '云服务器安全组需另行设置：请在云厂商控制台放行面板 TCP 端口和上述饥荒 UDP 端口。'
}

dependencies() {
    platform
    info "Installing Python and SteamCMD runtime dependencies"
    export DEBIAN_FRONTEND=noninteractive
    export NEEDRESTART_MODE=l  # Do not restart SSH/network/system services during dependency installation.
    dpkg --add-architecture i386
    apt-get update
    apt-get install -y ca-certificates curl git python3 python3-venv python3-pip \
        lib32gcc-s1 lib32stdc++6 libcurl4 libstdc++6 libcurl4:i386 tar util-linux iproute2
    check_systemd
    if ! id pera-panel >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "$DATA" --create-home --shell /usr/sbin/nologin pera-panel
    fi
    install -d -o root -g root -m 755 "$APP" "$APP/releases" "$MANAGER"
    install -d -o root -g pera-panel -m 750 "$CONFIG"
    install -d -o pera-panel -g pera-panel -m 750 "$DATA" "$APP/game" "$APP/steamcmd"
    # Ownership is intentionally restricted to game files and persistent data.
    chown -R pera-panel:pera-panel "$DATA" "$APP/game" "$APP/steamcmd"
}

as_game_user() (
    # A sudo caller may be in /root or a private /home/ubuntu. Recent tools can fail
    # with a bare permission error even when their arguments use absolute paths.
    cd -- "$DATA"
    runuser -u pera-panel -- env HOME="$DATA" "$@" 9>&-
)

steam_directories() {
    local directory
    # Create every parent explicitly: install -d does not assign the requested
    # owner/mode to implicitly created intermediate directories.
    for directory in "$DATA/.steam" "$DATA/.steam/sdk32" "$DATA/.steam/sdk64"; do
        [[ ! -L "$directory" ]] || die "Steam SDK directory is a link: $directory. Inspect it before retrying."
        install -d -o pera-panel -g pera-panel -m 750 "$directory"
    done
    as_game_user test -w "$DATA/.steam/sdk32" || die "The game user cannot write $DATA/.steam/sdk32."
    as_game_user test -w "$DATA/.steam/sdk64" || die "The game user cannot write $DATA/.steam/sdk64."
}

steam_links() {
    local destination="$1"
    steam_directories
    info "Linking Steam client libraries / 配置 Steam 客户端库链接"
    as_game_user ln -sfnT "$APP/steamcmd/linux32/steamclient.so" "$DATA/.steam/sdk32/steamclient.so"
    if [[ -f "$APP/steamcmd/linux64/steamclient.so" ]]; then
        as_game_user ln -sfnT "$APP/steamcmd/linux64/steamclient.so" "$DATA/.steam/sdk64/steamclient.so"
    elif [[ -f "$destination/bin64/lib64/steamclient.so" ]]; then
        as_game_user ln -sfnT "$destination/bin64/lib64/steamclient.so" "$DATA/.steam/sdk64/steamclient.so"
    fi
}

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
    steam_directories
}

download_game() {
    local destination="$1"
    info "Installing/updating Don’t Starve Together (Steam app 343050). This may take several minutes."
    as_game_user "$APP/steamcmd/steamcmd.sh" +force_install_dir "$destination" \
        +login anonymous +app_update 343050 validate +quit
    [[ -x "$destination/bin64/dontstarve_dedicated_server_nullrenderer_x64" ]] || die "SteamCMD did not produce the DST server executable. Retry update-game."
    steam_links "$destination"
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
    local -a network_args=()
    previous="$(readlink -f "$APP/current" 2>/dev/null || true)"
    if [[ -f "$CONFIG/config.json" ]]; then cp -p -- "$CONFIG/config.json" "$WORK/config.previous.json"; fi
    (( ! NETWORK_SETUP )) || network_args+=(--configure-network)
    stop_panel
    ln -sfn "$RELEASE" "$APP/current.next"
    mv -Tf "$APP/current.next" "$APP/current"
    install -m 644 "$RELEASE/deploy/pera-panel.service" /etc/systemd/system/pera-panel.service
    (cd "$RELEASE" && "$RELEASE/.venv/bin/python" -m pera_panel init-admin \
        --host "$BIND" --port "$PORT" "${network_args[@]}")
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
        if [[ -f "$WORK/config.previous.json" ]]; then cp -p -- "$WORK/config.previous.json" "$CONFIG/config.json"; fi
        if [[ "$previous" == "$APP/releases/"* && -d "$previous" ]]; then
            ln -sfn "$previous" "$APP/current.next"
            mv -Tf "$APP/current.next" "$APP/current"
            install -m 644 "$previous/deploy/pera-panel.service" /etc/systemd/system/pera-panel.service
            systemctl daemon-reload
            systemctl restart pera-panel.service || true
        fi
        die "The new panel failed to start; previous release restored when available. Run journalctl -u pera-panel -n 80."
    fi
    configure_firewall
    if (( NETWORK_SETUP )); then
        printf 'SAVED_FIREWALL=%q\n' "$FIREWALL" > "$CONFIG/network.env"
        chown root:root "$CONFIG/network.env"
        chmod 600 "$CONFIG/network.env"
    fi
    save_source
    write_manager
    info "Panel ready. World data: $DATA"
    if [[ "$BIND" == 127.* ]]; then
        printf 'SSH tunnel: ssh -L %s:%s:%s USER@SERVER\nhttp://127.0.0.1:%s\n' "$PORT" "$BIND" "$PORT" "$PORT"
    else
        say "Open http://SERVER_PUBLIC_IP:$PORT (bind address: $BIND)." "打开 http://服务器公网IP:$PORT（监听地址：$BIND）。"
    fi
    say 'Run sudo pera-panel to reopen the maintenance menu.' '运行 sudo pera-panel 可再次打开维护菜单。'
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
    steam_links "$APP/game"
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

   1) Install / 安装                   7) Stop panel + worlds / 停止
   2) Update panel + game / 更新全部   8) Restart panel / 重启
   3) Update panel / 更新面板          9) Status / 状态
   4) Update game / 更新游戏          10) Logs / 日志
   5) Reinstall / 重新安装            11) Reset admin password / 重置密码
   6) Start panel / 启动              12) Uninstall / 卸载
  13) Network setup / 网络设置         0) Exit / 退出

MENU
    local choice
    read -r -p 'Select an operation / 请选择: ' choice
    case "$choice" in
        1) COMMAND=install;; 2) COMMAND=update;; 3) COMMAND=update-panel;; 4) COMMAND=update-game;;
        5) COMMAND=reinstall;; 6) COMMAND=start;; 7) COMMAND=stop;; 8) COMMAND=restart;;
        9) COMMAND=status;; 10) COMMAND=logs;; 11) COMMAND=reset-password;; 12) COMMAND=uninstall;;
        13) COMMAND=configure;; 0) exit 0;; *) die "Unknown selection.";;
    esac
}

if [[ -z "$COMMAND" ]]; then
    if [[ -f "$CONFIG/config.json" ]]; then menu; else COMMAND=install; fi
fi
case "$COMMAND" in
    install|update|update-panel|update-game|reinstall|configure|start|stop|restart|status|logs|reset-password|uninstall) ;;
    *) die "Unknown command: $COMMAND. Use --help." ;;
esac
if (( PURGE )) && [[ "$COMMAND" != uninstall ]]; then die "--purge is only valid with uninstall."; fi
if [[ "$COMMAND" != status && "$COMMAND" != logs ]]; then
    exec 9>/run/lock/pera-panel-maintenance.lock
    flock -n 9 || die "Another maintenance operation is already running."
fi
WORK="$(mktemp -d /tmp/pera-panel.XXXXXXXX)"
case "$COMMAND" in
    install|update|update-panel|update-game|reinstall|configure)
        platform
        check_systemd
        setup_network ;;
esac
case "$COMMAND" in
    install|update) install_all ;;
    update-panel) update_panel ;;
    update-game) update_game ;;
    reinstall) reinstall ;;
    configure) installed; load_source; RELEASE="$(readlink -f "$APP/current")"; switch_release ;;
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
