# MC Manager — Python Edition (v2.0.2)

A desktop controller for Minecraft servers running on remote Linux machines
(VPS, dedicated box, home server). Connect over SSH and manage everything —
install, start/stop, live console, files, mods, backups, updates, monitoring —
without ever opening a terminal by hand.

Built with Python + CustomTkinter. Works with Paper, Purpur, Folia, Spigot,
Bukkit, Pufferfish, Fabric, Quilt, Forge, NeoForge, Vanilla, Mohist and
popular proxies.

---

## What's new in 2.0.2 (hotfix)

- **Fixed the first-connection dialog never appearing**: an internal
  `TypeError` made the app treat every new server as if you had clicked
  "No" (`HostKeyChanged ... NOT accepted by the user`). The trust dialog
  now opens properly on first connect.
- **One dialog instead of a flood**: simultaneous connects to the same
  server (status polling + your click) share a single confirmation dialog,
  and the answer is remembered for the session.
- **Declined servers stop retrying**: if you click "No", further attempts
  are refused instantly WITHOUT contacting the server — no more endless
  handshakes that can get your IP banned by fail2ban
  (`WinError 10054`). Restart the app if you declined by mistake.
- Background event toasts (crash/restart/backup notifications) now show
  correctly (same root cause).

## What's new in 2.0.1 (hotfix)

- **Fixed a startup crash on Windows** (`PermissionError: ...\.mcmanager\known_hosts`):
  a locked, unreadable or missing host-key file no longer kills every
  connection. The store degrades to in-memory for the session and the app
  shows one warning with the reason instead.
- **Fixed a deadlock** in first-use host-key confirmation and in
  Settings → Trusted hosts → Forget (a non-reentrant lock self-deadlocked
  the connecting thread).
- **Fixed silent data loss**: saving a newly trusted host key no longer
  wipes previously trusted hosts from `known_hosts`.
- Host-key saves are now **atomic** (temp file + rename) and self-heal a
  stale read-only flag.
- The credential vault survives a locked/unreadable key file by falling
  back to a session key (stored passwords must be re-entered, but the app
  keeps running).

## What's new in 2.0 (hardening release)

**Security**
- SSH passwords and RCON passwords are **encrypted at rest** in `servers.json`
  (authenticated encryption, key stored with `0600` permissions).
- **Host key verification (TOFU)** — the first connection shows the
  fingerprint for confirmation; every later key change is **rejected as a
  possible man-in-the-middle**. No more `AutoAddPolicy`.
- Installs never require `NOPASSWD: ALL`: privileged commands run through
  `sudo -S` with the password piped, and missing Java runtimes are installed
  **user-locally (Temurin, no root)**.

**Correctness**
- **Minecraft 26.x needs Java 25** — fixed. The version table now handles the
  2026 year-based versions (26.1, 26.2, …), snapshots (`26w13a`) and every
  classic 1.x mapping. The installer discovers **all** JVMs on the machine
  (a present Java 25 no longer triggers a pointless Java 21 install) and
  bakes the chosen runtime into `start.sh`.

**Reliability**
- **Backups & restore** for worlds, configs, mods and plugins — quick or full
  profile, integrity-verified archives, live progress, tiered **retention**
  (keep newest N + daily/weekly history).
- **Automatic pre-update backup** before every update or restore.
- **Crash watchdog**: detects crashed servers, restarts them automatically
  (min-interval + hourly cap prevents restart loops), and verifies they come
  back healthy.
- **Secure update pipeline**: checksum-verified download → graceful stop →
  atomic jar swap → start → **real protocol health check** → **automatic
  rollback** if the new build fails to boot.
- **Journal-protected operations**: if SSH drops mid-update/restore, the app
  detects the interrupted operation on reconnect and offers rollback.
- **Operation locks**: update / restore / backup / restart can never run at
  the same time on one server.

**Operations**
- **Live console** — `tail -F` streaming (no polling), plus RCON answers:
  `list` shows the player list, `tps` shows TPS.
- **Monitor with Minecraft metrics**: TPS, MSPT, players, ping, Java heap,
  GC, entity count (Paper), uptime, RSS.
- **Mod/plugin manager**: installs download **on the server** with sha1
  verification, required **dependencies are auto-installed**, versions are
  tracked in a server-side manifest, and every update keeps a **rollback
  copy**.
- **Scheduler**: daily backups and daily restarts, per server, with missed-run
  catch-up.
- **Notifications**: in-app alert history plus optional **Discord / generic
  webhooks** for crashes, auto-restarts, backup failures, updates and low disk.
- **Disk & log hygiene**: usage breakdown per folder, one-click cleanup
  (retention + old log rotation + crash reports), low-disk watchdog alerts.
- **Direct runner mode**: servers can run without screen/tmux (setsid +
  pidfile); screen and tmux remain available.
- **Fast server detection**: default scan checks running processes and common
  install roots in seconds; the full-disk `find /` is an explicit opt-in.
- **Restricted mode**: optional master password with per-server permissions
  (console / files / restart / edit).

**Tests** — 76 automated tests cover the Java version table, the credential
vault, host-key TOFU, the SLP protocol codec, deep health states, backup
retention, the update pipeline (including checksum-mismatch abort and failed-
boot rollback), scheduler firing, notifier history and the models layer.

---

## Requirements

- **Local**: Windows 10/11, macOS or Linux with Python 3.10+
- **Remote**: any Linux machine you can SSH into (root or a normal user)
- Remote extras (all optional): `curl`, GNU screen or tmux, systemd for CPU
  limits

## Quick start

```bash
pip install -r requirements.txt
python main.py
```

1. **Add Server** — enter the SSH address, username and password. On first
   connect, confirm the server's host-key fingerprint (it is remembered and
   verified from then on).
2. Link an install:
   - **Setup Wizard** — installs Paper / Fabric / Vanilla / Forge for any
     Minecraft version (Java is provisioned automatically), or
   - **Find existing installs** — fast scan for servers already on the
     machine, or
   - Adopt a **running** server: the app detects its screen session / RCON
     automatically.
3. Press **Start**. Everything else (console, files, mods, backups, monitor)
   follows from the sidebar.

## Daily use

| Page | What you do there |
|---|---|
| Control | deep status (ready / starting / unresponsive), start / stop / restart, update check |
| Console | live log stream, commands with RCON answers, quick commands |
| Mods & Plugins | browse Modrinth (auto-filtered to your platform + MC version), on/off switches, update + rollback |
| Files | full SFTP browser: upload, download, rename, delete, edit |
| Backups | backup / restore / delete, schedules, retention, disk usage, cleanup |
| Monitor | TPS, MSPT, players, heap, GC, entities, CPU / RAM / disk |
| Terminal | full interactive SSH shell |

**Tips**
- TPS/MSPT and RCON answers need RCON — the Console page offers to enable it
  with one click when it is off.
- Set a backup time (e.g. `04:30`) on the Backups page and the scheduler
  handles the rest.
- After a VPS reinstall the host key changes on purpose — remove the old
  entry in Settings → Trusted hosts.

## Running the tests

```bash
pip install pytest
python -m pytest tests/ -q
```

## Security notes

- Secrets are encrypted at rest; the key lives in `~/.mcmanager/.vault.key`.
  Losing the key only means re-entering passwords.
- Host keys are stored in `~/.mcmanager/known_hosts` (OpenSSH-compatible
  format). A changed key is treated as an attack until you remove the old one.
- For unattended Java installs via the package manager you can grant a
  **scoped** sudoers rule (`youruser ALL=(root) NOPASSWD: /usr/bin/apt-get`)
  — the app never needs blanket passwordless root.
- Optional master password (Settings) enables restricted mode with per-server
  permission flags for shared machines.

## Project structure

```
mcmanager/
  secure.py      credential vault, TOFU host keys, scoped sudo
  javas.py       Java discovery / version table / Temurin installs
  oplock.py      operation locks + remote step journal (transactions)
  health.py      SLP ping, deep status, crash watchdog, disk watchdog
  metrics.py     TPS / MSPT / players / heap / GC / entities
  backup.py      backup / restore / retention with progress
  updater.py     verified updates with health gate + rollback, mod manager
  sched.py       daily backup / restart scheduler
  notify.py      alerts history + Discord/generic webhooks
  maint.py       disk cleanup, log rotation
  migrate.py     move install locally or transfer to another VPS
  control.py     start/stop/restart, console channels, runner modes
  detect.py      fast + deep install detection and adoption
  installer.py   full unattended installs
  ssh_manager.py paramiko layer (verified host keys, lock-free long ops)
  ui/            CustomTkinter pages and dialogs
tests/           97-test pytest suite with a scripted FakeSSH
```

## Troubleshooting

- **"Permission denied: ...known_hosts" or a "host key file problem"
  warning** — the file is locked by another program or has broken
  permissions. The app keeps working (keys are remembered for the session
  only). To fix it permanently, close other instances of the app and delete
  the file, then restart:
  `del "%USERPROFILE%\.mcmanager\known_hosts"` (Windows) or
  `rm ~/.mcmanager/known_hosts` (Linux/macOS).
- **"Host key ... NOT accepted"** — you clicked **No** on the trust dialog
  (or it could not be shown). The app refuses that server for the rest of
  the session without contacting it. Restart MC Manager to be asked again,
  then click **Yes**.
- **"HOST KEY CHANGED"** — the machine was reinstalled or something is
  intercepting the connection. Verify out-of-band, then Settings → Trusted
  hosts → Forget.
- **"connection forcibly closed" (10054) / banner error** — the server or
  a firewall (fail2ban) dropped repeated connections. Wait a few minutes
  and connect once; the app no longer hammers the server after v2.0.2.
- **Commands don't reach the server** — enable RCON from the Console page
  (one click) or restart the server from the Control page so the screen
  session is re-attached.
- **Update rolled back** — the new build failed the health check; the
  previous jar was restored automatically. Check the console output for the
  crash reason.
- **Low disk alerts** — Backups → *Clean up disk* applies retention, rotates
  old logs and removes stale crash reports.
