"""Minecraft-specific metrics collection (issue 11).

The v1 Monitor page showed only system CPU/RAM/disk.  This module adds
the numbers server admins actually care about, using three best-effort
sources (whatever the server supports is used, the rest reports n/a):

- TPS  - `/tps` console command (Paper/Spigot/Purpur family) via RCON,
         parsed into 1m/5m/15m values.
- MSPT - `/mspt` (Paper 1.16.5+) - milliseconds per tick.
- players / latency / version - Server List Ping (health.slp_ping).
- Java heap + GC - `jcmd <pid> GC.heap_info` from the SAME runtime the
  server runs (server.java_path's bin dir), parsed to used/committed.
- entity count - Paper's `paper entity list` (only on Paper family).
- process RSS/uptime - /proc via ps (kept from v1).

Everything runs in ONE RCON session + a couple of exec calls, so the
monitor refresh stays cheap (2 SSH round trips).
"""
from __future__ import annotations

import os
import re
import struct

from . import health
from .control import _rcon_pkt, _rcon_recv  # packet helpers, reused
from .models import Server
from .ssh_manager import SSHManager, shq

TPS_RE = re.compile(
    r"(\d+[.,]\d+)[^,]*,\s*(\d+[.,]\d+)[^,]*,\s*(\d+[.,]\d+)")
HEAP_RE = re.compile(
    r"used\s+(\d+)([KMG]?)\s*,\s*committed\s+(\d+)([KMG]?)", re.IGNORECASE)
_HEAP_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}


def _to_floats(text: str) -> list[float]:
    """Parse 'a, b, c' number triples from TPS/MSPT output (Paper marks
    low values with '*', commas as decimal separators on some locales)."""
    matches = TPS_RE.findall((text or "").replace("\n", " "))
    if not matches:
        return []
    a, b, c = matches[-1]
    return [float(a.replace(",", ".")), float(b.replace(",", ".")),
            float(c.replace(",", "."))]


class RCONSession:
    """One authenticated RCON channel that can run several commands."""

    def __init__(self, ssh: SSHManager, server: Server, timeout: float = 8.0):
        if not server.rcon_port:
            raise RuntimeError("RCON not configured")
        client = ssh.connect(server)
        self.chan = client.get_transport().open_channel(
            "direct-tcpip", ("127.0.0.1", int(server.rcon_port)),
            ("127.0.0.1", 0))
        self.chan.settimeout(timeout)
        rid = 0x2A7D
        self.chan.sendall(_rcon_pkt(rid, 3, server.rcon_pass.encode()))
        r, _, _ = _rcon_recv(self.chan)
        if r == -1:
            self.close()
            raise RuntimeError("RCON auth failed")
        self._rid = rid

    def cmd(self, command: str) -> str:
        self._rid += 1
        self.chan.sendall(_rcon_pkt(self._rid, 2, command.encode()))
        try:
            _rid, ptype, payload = _rcon_recv(self.chan)
            return payload
        except (RuntimeError, struct.error, OSError):
            return ""

    def close(self) -> None:
        try:
            self.chan.close()
        except Exception:  # noqa: BLE001
            pass


def _heap_from_jcmd(ssh: SSHManager, server: Server,
                    pid: str) -> dict:
    """Heap usage via jcmd of the runtime actually running the server."""
    out: dict = {"heap_used": None, "heap_committed": None, "gc": "",
                 "java_version": ""}
    if not pid:
        return out
    java = server.java_path or "java"
    jcmd = os.path.join(os.path.dirname(java), "jcmd")
    res = ssh.exec(
        server,
        f"if [ -x {shq(jcmd)} ]; then {shq(jcmd)} {pid} GC.heap_info 2>&1 "
        "| head -6; else echo NO_JCMD; fi",
        timeout=20)
    text = res.stdout
    if "NO_JCMD" in text or not text.strip():
        return out
    m = HEAP_RE.search(text)
    if m:
        out["heap_used"] = int(m.group(1)) * _HEAP_UNITS.get(m.group(2).upper(), 1)
        out["heap_committed"] = int(m.group(3)) * _HEAP_UNITS.get(m.group(4).upper(), 1)
    head = text.splitlines()[0].lower() if text else ""
    if "garbage-first" in head or "g1" in head:
        out["gc"] = "G1"
    elif "zgarbage" in head or " z " in head:
        out["gc"] = "ZGC"
    elif "shenandoah" in head:
        out["gc"] = "Shenandoah"
    elif "parallel" in head or "psyounggen" in head:
        out["gc"] = "Parallel"
    return out


def _entities_from_rcon(rcon: RCONSession) -> str:
    """Paper family: `paper entity list` prints per-world entity counts."""
    out = rcon.cmd("paper entity list")
    if not out or "Unknown" in out[:60] or "Usage" in out[:80]:
        out = rcon.cmd("paper entity list all")
    nums = re.findall(r"(\d+)\s+entities?", out or "", re.IGNORECASE)
    if nums:
        return str(sum(int(n) for n in nums))
    return ""


def collect(ssh: SSHManager, server: Server, control) -> dict:
    """All available Minecraft metrics in one call (2-3 SSH round trips)."""
    m: dict = {
        "tps": (None, None, None), "mspt": (None, None, None),
        "players": (None, None), "latency_ms": None, "version": "",
        "motd": "", "heap_used": None, "heap_committed": None,
        "heap_max_mb": None, "gc": "", "entities": "", "rss_mb": None,
        "uptime": "", "state": "unknown",
    }
    # process info (cheap)
    try:
        pid = control.mc_pid(server) if server.mc_dir else ""
    except Exception:  # noqa: BLE001
        pid = ""
    if pid and server.mc_dir:
        res = ssh.exec(
            server,
            f"ps -o etime=,rss= -p {pid} 2>/dev/null", timeout=15)
        parts = res.stdout.split()
        if len(parts) >= 2:
            m["uptime"] = parts[0]
            try:
                m["rss_mb"] = round(int(parts[1]) / 1024, 1)
            except ValueError:
                pass

    # protocol-level health (players, latency, version)
    try:
        slp = health.slp_ping(ssh, server)
        m["slp"] = slp
        if slp.ok:
            m["players"] = (slp.players_online, slp.players_max)
            m["latency_ms"] = round(slp.latency_ms, 1)
            m["version"] = slp.version
            m["motd"] = slp.motd
            m["state"] = "ready"
    except Exception:  # noqa: BLE001
        m["slp"] = None
    if m["state"] == "unknown" and pid:
        m["state"] = "starting"

    # console-sourced metrics through one RCON session
    try:
        rcon = RCONSession(ssh, server)
        try:
            tps_raw = rcon.cmd("tps")
            floats = _to_floats(tps_raw)
            if len(floats) == 3:
                m["tps"] = tuple(floats)
            elif tps_raw:  # spark fallback
                floats = _to_floats(rcon.cmd("spark tps"))
                if len(floats) == 3:
                    m["tps"] = tuple(floats)
            mspt_raw = rcon.cmd("mspt")
            if "MSPT" in mspt_raw or "mspt" in mspt_raw:
                floats = _to_floats(mspt_raw)
                if len(floats) == 3:
                    m["mspt"] = tuple(floats)
            m["entities"] = _entities_from_rcon(rcon)
        finally:
            rcon.close()
    except Exception:  # noqa: BLE001 - RCON off is a normal situation
        pass

    # heap + GC via jcmd of the running JVM
    if pid:
        try:
            m.update(_heap_from_jcmd(ssh, server, pid))
        except Exception:  # noqa: BLE001
            pass
    if m["heap_max_mb"] is None and server.ram_mb:
        m["heap_max_mb"] = server.ram_mb  # -Xmx we configured
    return m
