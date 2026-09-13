# Pera Panel

A self-hosted **Don’t Starve Together** server manager for **Ubuntu 22.04+ / Debian 12+, x86_64, with systemd**. One installer sets up SteamCMD, the DST dedicated server, and a Flask + Waitress dashboard. Run the same script again, or `sudo pera-panel`, for an interactive maintenance menu.

## What it does

- Creates separate saved worlds with surface and optional caves. **One world runs at a time**, using fixed ports.
- Starts, gracefully stops, restarts, saves, and announces to the game.
- Configures name, password, game mode, player slots, PvP, pause when empty, world generation, and startup at boot.
- Adds/enables/disables Steam Workshop mods and edits their configuration options as JSON. Both shards receive the same mod configuration.
- Manages in-game administrators (OP), banned users, and whitelisted users by Klei ID.
- Shows process status, exit codes, uptime, game memory, host resources, recent operations, and rotating shard/mod logs.
- Makes consistent backups after stopping both shards; restores both together and creates a safety backup first.
- Replaces a world from an uploaded local save ZIP, optionally inheriting Workshop mod settings (on by default).
- Updates or reinstalls software while preserving worlds, backups, and admin credentials.
- Uses a generated admin password, hashed credentials, CSRF protection, login throttling, and an unprivileged systemd service.
- Supports Simplified Chinese and English on the login page and throughout the dashboard. The language selector remembers your preference in this browser and preserves unsaved form edits. The initial language follows your browser; world names, custom backup labels, mod option keys, and raw game logs remain unchanged.

**Rollback means restoring a panel backup**, not selecting an arbitrary in-game day. “Save game now” requests a native save; use “Create backup” to make a restore point. World generation happens on the first game start. The UI reports process health, not Steam lobby reachability or live player counts.

## Install by uploading the project

Copy the entire project to your server, for example `/home/ubuntu/pera-panel`, then run:

```bash
cd /home/ubuntu/pera-panel
sudo bash install.sh install
```

Before installing, the script guides you through language, panel address/port, and firewall settings. Press Enter to accept **0.0.0.0:8080** and **automatic UFW setup**. It detects SSH ports and lets you specify an additional SSH port to preserve. Choose `manual` if you manage your firewall separately. The maintenance menu includes Chinese and English labels.

The installer downloads the game through SteamCMD with anonymous login (app `343050`), creates the `pera-panel` system user, and prints a generated password for username **admin**. Existing credentials, game downloads, and worlds are retained when you rerun installation. Existing bind/port settings become the setup defaults; ordinary updates keep them without prompting.

For unattended setup, explicit arguments or environment variables supply the same defaults:

```bash
sudo bash install.sh install --yes --bind 0.0.0.0 --port 8080 --firewall auto --ssh-port 22 --lang zh-CN
# Equivalent defaults: PERA_BIND, PERA_PORT, PERA_FIREWALL, PERA_SSH_PORT, PERA_LANG
```

The panel port must be 1024–65535. Setup currently accepts IPv4 bind addresses. To change networking after installation, run `sudo pera-panel configure` or select **13) Network setup / 网络设置**. Your administrator credentials remain unchanged.

Keep the uploaded source directory: panel updates reuse it. Upload changed code there, then run `sudo pera-panel update-panel`.

## Install from your GitHub repository

```bash
curl -fL https://raw.githubusercontent.com/capret/pera-panel/main/install.sh -o install.sh
sudo bash install.sh install --repo https://github.com/capret/pera-panel.git --ref main
```

The repository URL and branch/tag are saved for future updates. Only public HTTPS GitHub repositories are supported by the remote installer. For a private repository, clone it yourself on the server and use `--source /absolute/path/to/checkout`.

The familiar single-command form is also supported:

```bash
sudo bash -c 'bash <(curl -fsSL https://raw.githubusercontent.com/capret/pera-panel/main/install.sh) install --repo https://github.com/capret/pera-panel.git --ref main'
```

Only execute installer code from a repository you trust. This installer requires root for package installation and service setup; the web app and game run as `pera-panel`.

## Open the panel and create your first world

With the default setup, open `http://YOUR_SERVER_IP:8080`. The bind address `0.0.0.0` means all local IPv4 interfaces; it is not the address to enter in your browser. For a cloud VPS, allow TCP 8080 and the game UDP ports in the provider’s security group/firewall as well.

If you choose a loopback bind (`127.0.0.1`), keep this SSH tunnel running from **your computer**:

```bash
ssh -L 8080:127.0.0.1:8080 ubuntu@YOUR_SERVER_IP
```

1. Open your server’s panel URL (or [http://127.0.0.1:8080](http://127.0.0.1:8080) through the tunnel) and sign in with the printed credentials. Use **Language / 语言** to switch between English and Simplified Chinese.
2. Click **Create a world**. Choose a name, game mode, slots, and whether to include caves.
3. Generate a dedicated server token at [Klei Accounts → Game Servers](https://accounts.klei.com/account/game/servers?game=DontStarveTogether) and paste it into the world form or settings. You need a valid Klei token to start an online world.
4. Add any Workshop mods before the first start, especially world generation mods.
5. Click **Start world**, and check **Live logs** for generation, token authentication, and registration errors. First startup and mod downloads can take several minutes.
6. Find the configured world name in DST’s server browser.

For HTTPS access, adapt [deploy/nginx.conf.example](deploy/nginx.conf.example). Bind the panel to loopback behind your reverse proxy and set `secure_cookie` to `true` in `/etc/pera-panel/config.json`, then restart. The installer does not obtain TLS certificates. With a proxy, allow its HTTPS port in your host/provider firewall.

Public binding uses HTTP until you configure a TLS reverse proxy; use HTTPS or the SSH tunnel when sending credentials over an untrusted network.

## Network ports

Automatic setup installs UFW if needed, adds the panel/game rules and detected/configured SSH ports, then enables UFW. Existing rules and default policies are retained; no firewall reset is performed. Enabling an inactive UFW applies its existing policies to other services too, so use `--firewall manual` on a host whose firewall you manage yourself. Loopback panel binds do not add a public panel rule. Port changes add the new rule without deleting older rules, which may serve other applications.

**Cloud provider rules are separate.** For Tencent Cloud, allow UDP **10999–11000** for players and TCP **8080** (or your chosen panel port) in the instance’s security group or Lighthouse firewall. Keep your SSH access rule. The installer cannot change cloud rules without your provider account/API access. No inbound panel port is needed when using the SSH tunnel.

| Purpose | Ports | Exposure |
| --- | --- | --- |
| Surface / caves | UDP 10999 / 11000 | Player access |
| Shard connection | UDP 10888 | Loopback only; keep private |
| Web panel | TCP 8080 (or your selected port) | All IPv4 interfaces by default |

For this surface/caves setup, normal player connections use only UDP 10999 and 11000. Klei’s [command-line guide](https://support.klei.com/hc/en-us/articles/360029556192-Dedicated-Server-Command-Line-Options-Guide) identifies `server_port` as the UDP connection port and describes the Steam authentication/master ports as internal Steam ports. We retain distinct Steam ports (8766/8767 and 27016/27017) in the generated configuration, but do not add public inbound firewall rules for them. Port 10888 connects shards on this same machine and stays on loopback.

When using manual firewall mode, these are the default application rules:

```bash
sudo ufw allow 8080/tcp
sudo ufw allow 10999:11000/udp
```

Allow the actual SSH port before manually enabling a firewall. Outbound access to Steam, Steam Workshop, and Klei must also work. The installer does not configure provider firewalls, NAT, or port forwarding.

Earlier installer versions added inbound UDP rules for 8766–8767 and 27016–27017. Updating the script does not delete existing host or cloud firewall rules. If you already installed those rules, review and remove them manually if they are not used by another service.

## Maintenance

```bash
sudo pera-panel                    # Interactive menu
sudo pera-panel status
sudo pera-panel update             # Update panel and game
sudo pera-panel update-panel       # Code/dependencies only
sudo pera-panel update-game        # SteamCMD update + validate
sudo pera-panel reinstall          # Fresh game and panel, preserve data
sudo pera-panel start
sudo pera-panel stop               # Stops panel and game processes
sudo pera-panel restart
sudo pera-panel logs
sudo pera-panel reset-password
sudo pera-panel configure          # Guided bind/port/firewall settings
sudo pera-panel uninstall          # Preserve worlds/backups/credentials
```

You can also rerun `sudo bash install.sh` for the menu or append any command to the original script. Override the update source using `--repo … --ref …` or `--source …`.

Maintenance disconnects players while the game stops and saves. A world that was running before a clean panel restart resumes afterward. The **Start at boot** preference also handles cold boots. A crash is shown in shard status and logs; failed game shards are not endlessly restarted. Use Restart world after fixing the cause.

The panel deploys into a new release directory and falls back to the prior release if the new service fails its startup check. Old releases remain under `/opt/pera-panel/releases`. Reinstall downloads a fresh game tree and keeps the previous installation as `game-previous-*`; these recovery copies use additional disk space. A failed SteamCMD update leaves the service stopped so you can inspect and retry it.

Ordinary uninstall retains the system user, data directory, and credentials. Reinstall from your original source to recover them. To permanently erase everything, use the explicit destructive form:

```bash
sudo pera-panel uninstall --purge
# Unattended destructive removal (no prompts):
# sudo pera-panel uninstall --purge --yes
```

Installed OS packages are not removed, since other software may use them.

## Recover an interrupted installation

If Steam reports **Success! App '343050' fully installed.** followed by **ln: Permission denied**, the game download succeeded and setup failed during Steam library link creation. This version runs service-user commands from `/var/lib/pera-panel` instead of inheriting a private caller directory, explicitly repairs ownership/mode on `.steam` and both SDK subdirectories, and creates the library links as `pera-panel`. It reports the link step separately. An unexpected linked SDK directory is reported for inspection instead of following it.

Upload this updated project (or publish it to your GitHub repository and download the updated installer) and rerun **install**, not reinstall. SteamCMD validates/reuses the existing game files; it does not intentionally create a fresh game tree. For an uploaded copy:

```bash
cd /home/ubuntu/pera-panel
sudo bash install.sh install --source "$PWD"
```

Messages such as **Failed to connect to system scope bus** indicate a separate systemd problem. Setup now checks the manager before work and after package installation, and sets `NEEDRESTART_MODE=l` so dependency installation does not automatically restart SSH/network services. If systemd remains unreachable, reboot the server after its package upgrades and rerun installation. The script reports this condition instead of continuing into service deployment. A real server retry is still needed to confirm resolution of a reported host failure.

## Replace a world with a local save ZIP

Open the selected world’s **Backups & rollback → Upload local save**. Select a ZIP, type the current world name, and click **Upload & replace save**. Upload progress is displayed; the import result appears in Recent activity.

Zip **one** local Cluster folder, or its contents. A containing folder (such as `Cluster_1/`) is detected automatically. The save should have this layout:

```text
Cluster_1/
  Master/
    save/session/<session-id>/...    # World snapshots and player saves
    modoverrides.lua                # Optional inherited mod settings
    server.ini                      # Optional original shard ID
  Caves/                            # Optional; include its complete save if used
    save/session/<session-id>/...
    modoverrides.lua
    server.ini
```

Klei’s [save location guide](https://support.klei.com/hc/en-us/articles/360029881191-Logs-and-Useful-Information-for-Bug-Reports-for-Don-t-Starve-Together) lists the local Cluster folders and Steam Cloud locations. For Cloud saves, first extract `Master.zip` into a `Master` folder and `Caves.zip` into a `Caves` folder, then ZIP the containing world folder. A bare `save` folder, multiple worlds in one archive, nested shard ZIPs, and extra custom shards are not accepted. Separate `client_save` folders are not merged; include the world/player files under the shard save folders.

**Inherit mod settings** is enabled by default. It reads Workshop IDs, enabled states, and `configuration_options` from the uploaded `Master/modoverrides.lua` and/or `Caves/modoverrides.lua`, then regenerates safe managed configuration files. Standard literal tables, comments, strings, numbers, booleans, and nested options are supported; Lua expressions and functions are never executed. If neither file is provided, inheritance imports an empty mod list. If only one is provided, that list applies to both shards. If both are provided, their settings must match because the current panel uses one shared mod list. Uncheck inheritance to retain your existing panel mods instead. Uploaded mod binaries and `dedicated_server_mods_setup.lua` are not used; Steam Workshop downloads the enabled mods when you start the world.

The archive and its saved data are validated and staged before the running world is stopped. A **Before save import** backup preserves its original saves, settings, and mods. Both shards are replaced together, and a surface-only import disables caves instead of retaining old cave data. Valid original shard IDs are retained when supplied. The panel’s world name, token, join password, player lists, other settings, and boot preference are preserved. The imported world stays **stopped**; review its mods and then click Start world. A failed replacement attempts to restore and restart the original world if it was running.

Limits: **256 MiB ZIP**, **2 GiB expanded**, **512 MiB per file**, and **20,000 entries**. Unsafe paths, symbolic links, encrypted files, duplicate paths, and excessive expansion are rejected. Temporary uploads are removed after successful or failed jobs. The archive must have enough free space to stage its contents and back up the current world. A power loss can leave `.upload-*`, `.import-*`, or `.previous-import-*` directories; inspect these before cleanup, especially the recovery directories.

If you already deployed the Nginx example, update `client_max_body_size` to **257m**, allow sufficient upload time, and reload Nginx. The updated example includes these values. Ordinary JSON API requests still have a 256 KiB limit.

## Worlds, mods, and backups

Stop the selected world before saving settings or mods. Caves cannot be enabled or disabled after save data is generated. Create a new world for a different shard layout. Surface and cave overrides accept JSON, such as `{"world_size":"default"}`; option names and supported values come from DST. Changes to generation options do not regenerate existing terrain.

The panel owns the generated INI/Lua files and regenerates them from each world’s `pera.json` metadata before startup. Use the dashboard to edit these settings; direct edits to generated files are overwritten.

Add a numeric Workshop ID (the `id=` part of its URL). An empty options object `{}` uses mod defaults. Example:

```json
{"SHOWPLAYERICONS": true}
```

Options are mod-specific; inspect that mod’s `modinfo.lua` or documentation. Arbitrary Lua is not accepted by the editor. The panel writes `dedicated_server_mods_setup.lua` before startup and `modoverrides.lua` for each shard. Enabled mods download sequentially before game launch; the Mods log shows progress. Client-only mods are not supported as server mods. Steam validation may overwrite the setup file; the panel regenerates it on the next start.

Backups are local directories containing the cluster’s saved games, configuration, token, player lists, and mod settings. Downloaded mod binaries are **not** versioned in a backup; Workshop may deliver a newer mod version after restoring. Backups are manual and retained until explicitly deleted. They are not scheduled automatically. The in-game snapshot setting controls DST’s own save history, not panel backup retention.

Before a restore, the current world is backed up as **Before rollback**. Both shards are restored together; current Klei credentials and boot preference are retained. If a graceful shutdown times out, backup/restore aborts instead of copying a live save. A filesystem error during restore attempts to put the original world directory back. Unexpected power loss during a rename may leave `.restore-*` or `.previous-*` directories for manual recovery; do not delete them without inspection.

Archive world removes it from the UI and keeps its files under `deleted/`, plus a safety backup. To unarchive manually, stop the panel and move the archived directory back to `clusters/<original-12-character-id>`; then start the panel. There is no archive browser in this first version.

| Server path | Contents |
| --- | --- |
| `/etc/pera-panel/config.json` | Admin hash, session secret, bind address, data paths |
| `/etc/pera-panel/source.env` | Root-only update source |
| `/opt/pera-panel/current` | Symlink to active application release |
| `/opt/pera-panel/game` | DST installation and Workshop files |
| `/opt/pera-panel/steamcmd` | SteamCMD |
| `/var/lib/pera-panel/clusters/<id>` | World config and surface/cave saves |
| `/var/lib/pera-panel/backups/<id>` | Full panel backups |
| `/var/lib/pera-panel/logs/<id>` | Rotating Master, Caves, and Mods console logs |
| `/var/lib/pera-panel/deleted` | Archived worlds |

For disaster recovery, copy `/var/lib/pera-panel` and `/etc/pera-panel` off-server while the panel is stopped. These directories contain credentials and tokens; keep the copies private. Trusted server mods run with the same OS permissions as the panel. This is a single-administrator tool, not a multi-tenant hosting platform.

## Local development

Python 3.10+ can run the dashboard on Windows, Linux, or macOS. Actual game process management and the installer target Linux x86_64.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m pera_panel init-admin --config var/config.json --data var/data --game var/game
python -m pera_panel serve --config var/config.json
```

Use the printed local password. The dashboard starts without a game installation; Start world reports that DST is missing. Local development does not install Steam or system services.

```bash
pytest -q
ruff check .
bash -n install.sh
shellcheck install.sh
node --check pera_panel/static/app.js
node --check pera_panel/static/i18n.js
```

Tests cover authentication/CSRF, hostile config inputs, shard configuration, operation serialization, process control using a fake game executable, paired backup restoration, failure recovery, and preservation of current credentials. GitHub Actions runs them on Linux. The installer’s full apt/SteamCMD/systemd lifecycle and real Steam/Klei connectivity still require a smoke test on your target server with a valid token.

## References

- [Klei’s Linux dedicated server setup](https://kleiforums.com/forums/topic/64441-dedicated-server-quick-setup-guide-linux/)
- [Klei’s dedicated server command-line options](https://support.klei.com/hc/en-us/articles/360029556192-Dedicated-Server-Command-Line-Options-Guide)
- [Klei’s dedicated server settings](https://forums.kleientertainment.com/forums/topic/64552-dedicated-server-settings-guide/)
- [Valve SteamCMD documentation](https://developer.valvesoftware.com/wiki/SteamCMD)

MIT license. Don’t Starve Together and Steam are trademarks of their respective owners.
