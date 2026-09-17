"""Data models and JSON persistence for saved servers.

v2.0 hardening:
- secrets (SSH password, RCON password) are ENCRYPTED at rest in
  servers.json (see secure.CredentialVault); the file is also written
  with 0600 permissions as defense in depth.
- per-server POLICY fields (issue 31): java override, runner mode, CPU
  limit, auto-restart, backup/restart schedules, retention, update
  channel.
- per-server PERMISSION flags (issue 24) used by restricted mode:
  console / files / restart / edit can each be disallowed.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, asdict, field, fields as dc_fields
from pathlib import Path

from .secure import VAULT, VaultError

CONFIG_DIR = Path.home() / ".mcmanager"
SERVERS_FILE = CONFIG_DIR / "servers.json"

# fields that must never leave this machine in plaintext
_SECRET_FIELDS = ("password", "rcon_pass")


@dataclass
class Server:
    id: str
    name: str
    host: str
    port: int = 22
    username: str = "root"
    password: str = ""
    mc_dir: str = ""            # remote install dir, e.g. /home/user/mc-server
    mc_version: str = ""        # e.g. 1.21.4 or 26.2 (year scheme)
    platform: str = ""          # paper / purpur / spigot / fabric / ...
    ram_mb: int = 4096
    created_at: float = field(default_factory=time.time)
    # --- fields used by "adopt existing install" -----------------------------
    external_screen: str = ""   # pre-existing screen session name
    rcon_port: int = 0          # RCON port from server.properties
    rcon_pass: str = ""         # RCON password (encrypted at rest)
    # --- v2.0: runtime / policies (issue 31) ---------------------------------
    mc_port: int = 0            # Minecraft port (server.properties) for SLP
    java_path: str = ""         # explicit java binary ("" = auto-discover)
    java_major: int = 0         # required Java major override (0 = auto)
    runner: str = "auto"        # auto | screen | tmux | direct (no screen)
    extra_jvm: str = ""         # extra JVM args for start.sh
    cpu_limit_pct: int = 0      # systemd-run CPUQuota % (0 = unlimited)
    # --- v2.0: reliability policies -------------------------------------------
    auto_restart: bool = True   # watchdog restarts the server after a crash
    restart_guard_s: int = 300  # min seconds between two auto-restarts
    backup_time: str = ""       # "HH:MM" daily backup ("" = off)
    backup_keep: int = 10       # retention: always keep the N newest archives
    backup_dir: str = ""        # "" = <mc_dir>/backups/mcmanager
    restart_time: str = ""      # "HH:MM" daily restart ("" = off)
    update_channel: str = "stable"   # stable | latest
    notify: bool = True         # this server emits notification events
    # --- v2.0: permission flags for restricted mode (issue 24) ----------------
    allow_console: bool = True
    allow_files: bool = True
    allow_restart: bool = True
    allow_edit: bool = True

    @property
    def screen_tag(self) -> str:
        base = re.sub(r"[^a-zA-Z0-9_-]", "", self.name).lower()
        return (base or "mcserver")[:20] + "-" + self.id[:6]

    # -- policy helpers --------------------------------------------------------
    def parsed_backup_time(self) -> tuple[int, int] | None:
        return _parse_hhmm(self.backup_time)

    def parsed_restart_time(self) -> tuple[int, int] | None:
        return _parse_hhmm(self.restart_time)


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (value or "").strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh < 24 and 0 <= mm < 60:
        return hh, mm
    return None


def _secure_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class ServerStore:
    """Load / save / CRUD for the server list (secrets encrypted at rest)."""

    def __init__(self) -> None:
        self.servers: list[Server] = []
        self.load_errors: list[str] = []
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        self.servers = []
        self.load_errors = []
        if not SERVERS_FILE.exists():
            return
        try:
            raw = json.loads(SERVERS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self.load_errors.append(f"servers.json unreadable: {exc}")
            return
        known = {f.name for f in dc_fields(Server)}
        for d in raw.get("servers", []):
            try:
                d = {k: v for k, v in d.items() if k in known}  # drop unknown
                d["port"] = int(d.get("port") or 22)
                d["ram_mb"] = int(d.get("ram_mb") or 4096)
                srv = Server(**d)
            except TypeError as exc:
                self.load_errors.append(f"skipped a server entry: {exc}")
                continue
            # decrypt secrets (legacy plaintext passes through unchanged and
            # is re-encrypted on the next save())
            for f in _SECRET_FIELDS:
                token = getattr(srv, f)
                if token:
                    try:
                        setattr(srv, f, VAULT.decrypt(token))
                    except VaultError as exc:
                        self.load_errors.append(
                            f"{srv.name}: {exc}")
                        setattr(srv, f, "")
            self.servers.append(srv)

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        out = []
        for s in self.servers:
            d = asdict(s)
            for f in _SECRET_FIELDS:
                d[f] = VAULT.encrypt(d[f]) if d[f] else ""
            out.append(d)
        SERVERS_FILE.write_text(
            json.dumps({"servers": out}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _secure_permissions(SERVERS_FILE)

    # -- crud --------------------------------------------------------------
    def add(self, name: str, host: str, username: str, password: str,
            port: int = 22) -> Server:
        srv = Server(
            id=uuid.uuid4().hex[:12], name=name.strip() or host,
            host=host.strip(), port=port or 22,
            username=username.strip() or "root", password=password,
        )
        self.servers.append(srv)
        self.save()
        return srv

    def update(self, srv: Server) -> None:
        self.save()

    def delete(self, srv: Server) -> None:
        self.servers = [s for s in self.servers if s.id != srv.id]
        self.save()

    def get(self, server_id: str | None) -> Server | None:
        if not server_id:
            return None
        for s in self.servers:
            if s.id == server_id:
                return s
        return None
