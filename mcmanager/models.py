"""Data models and JSON persistence for saved servers."""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".mcmanager"
SERVERS_FILE = CONFIG_DIR / "servers.json"


@dataclass
class Server:
    id: str
    name: str
    host: str
    port: int = 22
    username: str = "root"
    password: str = ""
    mc_dir: str = ""            # remote install dir, e.g. /home/user/mc-server
    mc_version: str = ""        # e.g. 1.21.4
    platform: str = ""          # paper / purpur / spigot / bukkit / fabric / vanilla / forge / ...
    ram_mb: int = 4096
    created_at: float = field(default_factory=time.time)
    # --- fields used by "adopt existing install" -----------------------------
    external_screen: str = ""   # pre-existing screen session name (server started outside the app)
    rcon_port: int = 0          # RCON port from server.properties (fallback command channel)
    rcon_pass: str = ""         # RCON password (kept only locally)

    @property
    def screen_tag(self) -> str:
        base = re.sub(r"[^a-zA-Z0-9_-]", "", self.name).lower()
        return (base or "mcserver")[:20] + "-" + self.id[:6]


class ServerStore:
    """Load / save / CRUD for the server list."""

    def __init__(self) -> None:
        self.servers: list[Server] = []
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        self.servers = []
        try:
            if SERVERS_FILE.exists():
                raw = json.loads(SERVERS_FILE.read_text(encoding="utf-8"))
                for d in raw.get("servers", []):
                    try:
                        d["port"] = int(d.get("port") or 22)
                        d["ram_mb"] = int(d.get("ram_mb") or 4096)
                        self.servers.append(Server(**d))
                    except TypeError:
                        continue
        except (json.JSONDecodeError, OSError):
            pass

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {"servers": [asdict(s) for s in self.servers]}
        SERVERS_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

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
