# MC Manager — Python Edition · v1.4.2

A desktop control panel for Linux Minecraft servers. Runs on Windows (also works on Linux/macOS), connects to your server over SSH, and manages everything from one window: automatic installation, whole-disk detection of existing servers, plugin/mod management via Modrinth, a live console, a full terminal, an SFTP file manager, and monitoring — wrapped in a violet-indigo theme with a generated wallpaper background.

## What's in this version

**v1.4.2 — bugfix release**

- **Fixed: file list was empty** — a duplicated keyword argument crashed row rendering inside the Files page, so almost no files appeared. Row construction is now defensive: a single broken row can never kill the whole listing again.
- **Fixed: scrolling** — the wallpaper layer was accidentally replacing the scroll-region update binding of every scrollable list (Files, Mods & Plugins, Dashboard, Detect). Lists now scroll normally with the mouse wheel.

**v1.4.1 — visual polish (found by screenshot + pixel analysis)**

- Card and button corners now sample the exact wallpaper color behind each of their 4 corners, so no dark squares leak outside rounded corners.
- The sidebar gradient is continuous — sub-frames reuse the parent wallpaper image instead of drawing their own.
- Status toasts are invisible when idle and no longer overlap the console input bar.
- Redesigned sidebar with an "ACTIVE SERVER" card; the current page is highlighted with a violet pill.
- Button corner radius reduced to 8 px (larger radii render stray "spike" pixels on short CustomTkinter buttons).
- Platform-aware fonts (Segoe UI/Consolas on Windows, best available on Linux/macOS), 3×2 shortcut grid on Control, gauges framed in cards, and loading/empty placeholders everywhere.

## Features

| Section | What it does |
|---|---|
| **Dashboard** | Status card per server with a live running/stopped light |
| **Control** | Start / stop / restart in one click, plus the latest log lines |
| **Setup Wizard** | 4 steps: system check → platform & version → install → run |
| **Server detection** | Scans the whole disk and live Java processes for existing installs |
| **Mods & Plugins** | Installed list with ON/OFF switches + Modrinth search with auto-matching to your server's platform and version |
| **Console** | Live server log (refreshes every 2.5 s) + quick commands (list, tps, save-all…) |
| **Terminal** | Full SSH shell on the same server |
| **Files** | Complete SFTP browser: all files/folders (even hidden), jump to any path, live filter, upload/download/delete |
| **Monitor** | CPU / RAM / disk gauges + Java process status |

Installable platforms: **Paper** (plugins), **Fabric** (mods), **Vanilla**, **Forge** (mods) — all downloaded from official sources (PaperMC Fill API, Fabric Meta, Mojang, Forge Maven).

## Requirements

1. **Python 3.10 or newer** — from [python.org](https://www.python.org/downloads/). Check **Add Python to PATH** during installation.
2. Windows 10/11 (the same code also runs on Linux and macOS).
3. Internet access for the first run (dependency download).

## Quick start

1. Open the `MCManager-Python` folder.
2. Double-click **`run.bat`**.
3. Done — on first run it installs the dependencies (customtkinter, paramiko, requests, pillow) automatically, then the app opens.

Manual alternative:

```
python -m pip install -r requirements.txt
python main.py
```

Optional: double-click **`build_exe.bat`** to produce a standalone `dist\MCManager.exe` that runs on any Windows machine without Python.

## First use

1. **+ Add Server** → enter IP, username, password and port (default 22) → **Test connection** → Save.
2. Click **Manage** on the server card.
3. Use the **Setup Wizard** for a fresh install, or **Find existing installs** to adopt a server that is already on the machine (the whole disk is searched — no fixed paths).
4. **Mods & Plugins** → search from the **Get from Modrinth** tab (e.g. EssentialsX) → **Install** → pick a compatible build (badged `compatible`) → then **Reload Plugins** or restart. Manage installed plugins with the ON/OFF switches in the **Installed on server** tab.
5. Players connect via `SERVER-IP:25565`.

Vanilla/Paper servers accept unmodified clients; Fabric/Forge servers require a matching modded client.

## Preparing the Linux server

The app connects over SSH (default port 22). For automatic Java installation the user needs passwordless sudo:

```bash
sudo visudo
# add this line (replace USERNAME):
USERNAME ALL=(ALL) NOPASSWD:ALL
```

Open the Minecraft port in the firewall (ufw example):

```bash
sudo ufw allow 25565/tcp
```

> Already have a Minecraft server and only want to manage it? No sudo needed — **Find existing installs** is enough.

## How the server is run

Minecraft runs inside a **GNU screen** session named `mc-<server-name>`:

```bash
screen -ls               # list running sessions
screen -r mc-myserver    # attach to the console manually
```

> If your server already runs under systemd or another service, that's fine: **Find existing installs** locates it and attaches console/monitor/terminal to that process without stopping it. Sending commands requires screen or RCON — the app can enable RCON for you automatically.

Key files on the server: `start.sh` (run script), `server.properties` (port/motd), `eula.txt`, `logs/latest.log`.

## Smart command channel

It does not matter how or under what name the server was started — the app picks the best channel automatically:

1. **screen session** — saved names first, then exact match, then it walks the Java process's parent chain to find the owning screen session;
2. no screen → **tmux** (`send-keys`);
3. no tmux → **RCON** over an SSH tunnel;
4. neither → the app offers to enable RCON automatically (writes `enable-rcon=true`, a port and a random password into `server.properties`, then restarts if needed).

Commands are sent in a POSIX-safe form so a real Enter reaches the server console under any login shell (bash/dash/zsh/fish). The Console shows which channel was used (`sent via screen session 'mc-main'`).

## Notes

- SSH passwords are stored as plain text in `%USERPROFILE%\.mcmanager\servers.json`. If that is a concern, create a dedicated SSH user with a strong password.
- The app only connects from you to your server — nothing is sent anywhere else.
- Host fingerprints are accepted on first connect (AutoAddPolicy), like the first `ssh` in a terminal.

## Troubleshooting

| Problem | Fix |
|---|---|
| `python is not recognized` | Reinstall Python with "Add to PATH", or install from the Microsoft Store |
| Window opens and closes instantly | Run `python main.py` from cmd to see the error |
| Connection timed out | Check IP/port; make sure the SSH port is open in the server firewall |
| Wrong username or password | Test manually with `ssh user@ip` |
| Java installation failed | Passwordless sudo (NOPASSWD) is required — see server preparation |
| Nothing appears in Console | Is the server actually running? Run `screen -ls` in the Terminal page |
| Permission denied in Files | The SSH user cannot read that folder; try another user |
| Detection finds nothing | The full-disk scan can take up to ~1 minute; if still nothing, browse the folder in Files and confirm a server jar or `server.properties` is there |
| Forge install is slow | Normal (2–10 minutes depending on the machine) — follow the live log |

## Project structure

```
MCManager-Python/
├── main.py                  # entry point
├── run.bat                  # Windows quick start
├── build_exe.bat            # PyInstaller EXE build
├── requirements.txt
└── mcmanager/
    ├── theme.py             # violet-indigo palette + fonts
    ├── models.py            # Server model + JSON storage
    ├── tasks.py             # background tasks delivered to the UI thread
    ├── ssh_manager.py       # paramiko: exec / stream / SFTP / interactive shell
    ├── apis.py              # Paper Fill v3 / Fabric / Mojang / Forge / Modrinth
    ├── detect.py            # existing-server detection: disk scan + processes + adopt
    ├── plugins.py           # plugin/mod management: list, on/off, delete, reload
    ├── installer.py         # automatic install: probe → java → jar → config
    ├── control.py           # start/stop via screen or existing session or RCON, console, monitor
    └── ui/                  # 8 pages + 2 dialogs (CustomTkinter)
        ├── app.py           # main window & navigation
        ├── wallpaper.py     # generated gradient background + corner blending
        ├── widgets.py       # cards, buttons, colored log, toast
        ├── pages_dashboard / pages_editor / pages_server
        ├── pages_detect     # existing-server detection dialog
        ├── pages_wizard / pages_browse / pages_console
        └── pages_terminal / pages_files / pages_monitor
```
