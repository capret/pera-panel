# Pera Panel

A self-hosted **Don’t Starve Together** server manager for **Ubuntu 22.04+ / Debian 12+, x86_64, with systemd**. One installer sets up SteamCMD, the DST dedicated server, and a Flask + Waitress dashboard. Run the same script again, or `sudo pera-panel`, for an interactive maintenance menu.

Inspired by the install-and-manage workflow of [3x-ui](https://github.com/MHSanaei/3x-ui). This is an independent project, with no 3x-ui source code or Klei/Valve affiliation.

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

**Rollback means restoring a panel backup**, not selecting an arbitrary in-game day. “Save game now” requests a native save; use “Create backup” to make a restore point. World generation happens on the first game start. The UI reports process health, not Steam lobby reachability or live player counts.

## Install by uploading the project

Copy the entire project to your server, for example `/home/ubuntu/pera-panel`, then run:

```bash
cd /home/ubuntu/pera-panel
sudo bash install.sh install
```

The installer downloads the game through SteamCMD with anonymous login (app `343050`), creates the `pera-panel` system user, and starts the panel at `127.0.0.1:8080`. It prints a generated password for username **admin**. Existing credentials are preserved on subsequent installs.

Keep the uploaded source directory: panel updates reuse it. Upload changed code there, then run `sudo pera-panel update-panel`.

## Install from your GitHub repository

Push these project files to your repository first. Replace `YOUR_NAME` and `main` below with your actual owner and branch:

```bash
curl -fL https://raw.githubusercontent.com/YOUR_NAME/pera-panel/main/install.sh -o install.sh
sudo bash install.sh install --repo https://github.com/YOUR_NAME/pera-panel.git --ref main
```

The repository URL and branch/tag are saved for future updates. Only public HTTPS GitHub repositories are supported by the remote installer. For a private repository, clone it yourself on the server and use `--source /absolute/path/to/checkout`.

The familiar single-command form is also supported:

```bash
sudo bash -c 'bash <(curl -fsSL https://raw.githubusercontent.com/YOUR_NAME/pera-panel/main/install.sh) install --repo https://github.com/YOUR_NAME/pera-panel.git --ref main'
```

Only execute installer code from a repository you trust. This installer requires root for package installation and service setup; the web app and game run as `pera-panel`.

## Open the panel and create your first world

From **your computer**, keep this SSH tunnel running:

```bash
ssh -L 8080:127.0.0.1:8080 ubuntu@YOUR_SERVER_IP
```

1. Open [http://127.0.0.1:8080](http://127.0.0.1:8080) and sign in with the printed credentials.
2. Click **Create a world**. Choose a name, game mode, slots, and whether to include caves.
3. Generate a dedicated server token at [Klei Accounts → Game Servers](https://accounts.klei.com/account/game/servers?game=DontStarveTogether) and paste it into the world form or settings. You need a valid Klei token to start an online world.
4. Add any Workshop mods before the first start, especially world generation mods.
5. Click **Start world**, and check **Live logs** for generation, token authentication, and registration errors. First startup and mod downloads can take several minutes.
6. Find the configured world name in DST’s server browser.

For public HTTPS access, adapt [deploy/nginx.conf.example](deploy/nginx.conf.example). Keep the panel on loopback behind your reverse proxy. Set `secure_cookie` to `true` in `/etc/pera-panel/config.json` when using HTTPS, then restart. The panel does not automatically obtain TLS certificates or modify your firewall.

If needed, first-install binding can be changed with `sudo PERA_BIND=0.0.0.0 PERA_PORT=8080 bash install.sh install`. Subsequent changes belong in `/etc/pera-panel/config.json`; use HTTPS for access over the public Internet.

## Network ports

Allow the game ports in both your VPS provider’s firewall and the server firewall. No inbound panel port is needed with the SSH tunnel.

| Purpose | Ports | Exposure |
| --- | --- | --- |
| Surface / caves | UDP 10999 / 11000 | Player access |
| Steam authentication | UDP 8766 / 8767 | Steam networking |
| Steam master server | UDP 27016 / 27017 | Steam networking |
| Shard connection | UDP 10888 | Loopback only; keep private |
| Web panel | TCP 8080 | Loopback by default |

For an **already enabled** UFW firewall:

```bash
sudo ufw allow 10999:11000/udp
sudo ufw allow 8766:8767/udp
sudo ufw allow 27016:27017/udp
```

Do not enable a new firewall remotely without first allowing your SSH port. Outbound access to Steam, Steam Workshop, and Klei must also work. The installer does not configure networking or NAT for you.

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
```

Tests cover authentication/CSRF, hostile config inputs, shard configuration, operation serialization, process control using a fake game executable, paired backup restoration, failure recovery, and preservation of current credentials. GitHub Actions runs them on Linux. The installer’s full apt/SteamCMD/systemd lifecycle and real Steam/Klei connectivity still require a smoke test on your target server with a valid token.

## References

- [Klei’s Linux dedicated server setup](https://kleiforums.com/forums/topic/64441-dedicated-server-quick-setup-guide-linux/)
- [Klei’s dedicated server command-line options](https://support.klei.com/hc/en-us/articles/360029556192-Dedicated-Server-Command-Line-Options-Guide)
- [Klei’s dedicated server settings](https://forums.kleientertainment.com/forums/topic/64552-dedicated-server-settings-guide/)
- [Valve SteamCMD documentation](https://developer.valvesoftware.com/wiki/SteamCMD)

MIT license. Don’t Starve Together and Steam are trademarks of their respective owners.
