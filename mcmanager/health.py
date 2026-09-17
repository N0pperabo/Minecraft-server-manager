"""Deep health checking + crash-recovery watchdog (v2.0).

Issues addressed:
- 22/23: "is the server up" used to mean "a java process exists and the
  log mentions something".  Now `slp_ping()` speaks the actual Minecraft
  Server List Ping protocol over an SSH tunnel to 127.0.0.1:<port>:
  if the SERVER answers the handshake, it is genuinely responsive -
  players online, MOTD, version and round-trip latency included.
  `deep_status()` combines process + SLP + log parsing into
  stopped / starting / ready / unresponsive.
- 8/9: `Watchdog` is a background thread that notices CRASHES (process
  gone while it was running before), restarts the server automatically
  (respecting a min-restart interval and an hourly cap so a crash loop
  cannot machine-gun the VPS), and emits notifications for every crash,
  auto-restart and failed recovery.
- 29 (partly): the watchdog also samples disk usage every few minutes
  and raises warn/critical notifications before the disk fills.
"""
from __future__ import annotations

import json
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

from .models import Server
from .ssh_manager import SSHManager, shq

DONE_RE = re.compile(r"Done \([0-9.]+s\)", re.IGNORECASE)


# ================================================================ varints ===
def write_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 32
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varint(chan, _max: int = 5) -> int:
    number = 0
    for i in range(_max):
        raw = chan.recv(1)
        if not raw:
            raise RuntimeError("SLP: connection closed while reading varint")
        byte = raw[0]
        number |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            break
    else:
        raise RuntimeError("SLP: varint too long")
    if number & (1 << 31):
        number -= 1 << 32
    return number


def _pack_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return write_varint(len(raw)) + raw


# =============================================================== SLP ping ===
@dataclass
class SLPResult:
    ok: bool = False
    latency_ms: float = 0.0
    players_online: int = -1
    players_max: int = -1
    version: str = ""
    protocol: int = 0
    motd: str = ""
    error: str = ""


def slp_ping(ssh: SSHManager, server: Server, port: int | None = None,
             timeout: float = 6.0) -> SLPResult:
    """Minecraft Server List Ping through an SSH direct-tcpip channel.

    Works even when the Minecraft port is firewalled - the ping travels
    inside the SSH connection to 127.0.0.1:<port>, exactly like the RCON
    tunnel.  A successful handshake proves the SERVER (not just the java
    process) is alive and answering.
    """
    port = int(port or server.mc_port or 25565)
    res = SLPResult()
    try:
        client = ssh.connect(server)
    except Exception as exc:  # noqa: BLE001
        res.error = f"ssh: {exc}"
        return res
    try:
        chan = client.get_transport().open_channel(
            "direct-tcpip", ("127.0.0.1", port), ("127.0.0.1", 0))
    except Exception as exc:  # noqa: BLE001
        res.error = f"tunnel to 127.0.0.1:{port} failed: {exc}"
        return res
    try:
        chan.settimeout(timeout)
        t0 = time.monotonic()
        host = "127.0.0.1"
        handshake = (write_varint(0x00) + write_varint(770)
                     + _pack_string(host) + struct.pack(">H", port)
                     + write_varint(1))
        chan.sendall(write_varint(len(handshake)) + handshake)
        req = write_varint(0x00)
        chan.sendall(write_varint(len(req)) + req)

        _total = read_varint(chan)          # response packet length
        _pid = read_varint(chan)            # packet id 0x00
        jlen = read_varint(chan, _max=6)    # json length
        raw = bytearray()
        while len(raw) < jlen:
            chunk = chan.recv(jlen - len(raw))
            if not chunk:
                break
            raw += chunk
        res.latency_ms = (time.monotonic() - t0) * 1000.0
        data = json.loads(bytes(raw).decode("utf-8", "replace"))
        res.ok = True
        players = data.get("players") or {}
        res.players_online = int(players.get("online", -1))
        res.players_max = int(players.get("max", -1))
        res.version = str((data.get("version") or {}).get("name", ""))
        res.protocol = int((data.get("version") or {}).get("protocol", 0))
        desc = data.get("description")
        if isinstance(desc, dict):
            txt = desc.get("text", "")
            extra = desc.get("extra") or []
            res.motd = (txt + "".join(str(e.get("text", "")) for e in extra
                                      if isinstance(e, dict))).strip()
        elif isinstance(desc, str):
            res.motd = desc
        return res
    except (RuntimeError, OSError, socket.timeout, ValueError,
            json.JSONDecodeError, struct.error) as exc:
        res.error = f"slp: {exc}"
        return res
    finally:
        try:
            chan.close()
        except Exception:  # noqa: BLE001
            pass


# ============================================================ deep status ===
def log_ready_marker(ssh: SSHManager, server: Server,
                     tail_bytes: int = 20000) -> bool:
    """True if the current boot finished ('Done (12.3s)!' line)."""
    if not server.mc_dir:
        return False
    f = shq(server.mc_dir.rstrip("/") + "/logs/latest.log")
    res = ssh.exec(
        server, f"tail -c {tail_bytes} {f} 2>/dev/null | grep -m1 -c 'Done ('",
        timeout=15)
    try:
        return int(res.stdout.strip() or "0") > 0
    except ValueError:
        return False


@dataclass
class HealthReport:
    state: str = "unknown"       # stopped|starting|ready|unresponsive|unreachable
    process: bool = False
    slp: SLPResult = field(default_factory=SLPResult)
    since_boot_s: int = 0
    detail: str = ""

    @property
    def running(self) -> bool:
        return self.state in ("ready", "starting", "unresponsive")


def deep_status(ssh: SSHManager, server: Server, control) -> HealthReport:
    """Real server state - process AND protocol answer AND boot marker."""
    rep = HealthReport()
    try:
        rep.process = bool(control.mc_pid(server)) if server.mc_dir else \
            bool(control.status(server))
    except Exception as exc:  # noqa: BLE001
        rep.state = "unreachable"
        rep.detail = str(exc)[:160]
        return rep
    if not rep.process:
        rep.state = "stopped"
        return rep
    rep.slp = slp_ping(ssh, server)
    if rep.slp.ok:
        rep.state = "ready"
        return rep
    # process exists but no protocol answer: starting (until 'Done') or stuck
    if log_ready_marker(ssh, server):
        rep.state = "unresponsive"   # booted but not answering - hang?
        rep.detail = rep.slp.error[:120]
    else:
        rep.state = "starting"
    return rep


# =============================================================== watchdog ===
class Watchdog:
    """Background crash-recovery + disk watchdog.

    Runs one loop; per saved server with a linked install it tracks the
    last known state.  A crash = process disappeared while previously
    running.  Then: wait `grace_s` (rules out a manual stop that is in
    progress), restart, notify, and verify the server comes back.  A
    min-restart interval + hourly cap prevent restart loops.
    """

    CHECK_INTERVAL = 30      # seconds between health checks
    DISK_INTERVAL = 300      # seconds between disk samples
    GRACE_S = 12             # crash confirmation delay
    MAX_RESTARTS_PER_HOUR = 5

    def __init__(self, app_refs) -> None:
        """app_refs: object with .store, .ssh, .control, .notify_event(...),
        .locks (oplock.OpLocks) - the MCManagerApp itself."""
        self.ref = app_refs
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mcm-watchdog")
        self._last_running: dict[str, bool] = {}
        self._restart_times: dict[str, list[float]] = {}
        self._last_disk_check = 0.0
        self._disk_warned: dict[str, float] = {}

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- main loop ------------------------------------------------------------
    def _run(self) -> None:
        # first pass just seeds the state without notifying
        time.sleep(3)
        while not self._stop.is_set():
            try:
                self._pass(seed=False)
            except Exception:  # noqa: BLE001 - the watchdog must never die
                pass
            self._stop.wait(self.CHECK_INTERVAL)
        # cleanup ignored - daemon thread

    def _pass(self, seed: bool = False) -> None:
        for srv in list(self.ref.store.servers):
            if not srv.mc_dir or not srv.auto_restart:
                continue
            try:
                rep = deep_status(self.ref.ssh, srv, self.ref.control)
            except Exception:  # noqa: BLE001
                continue
            was = self._last_running.get(srv.id, rep.running)
            self._last_running[srv.id] = rep.running
            if seed:
                continue
            if was and not rep.running and rep.state == "stopped":
                self._on_crash(srv)
        self._disk_pass()

    # -- crash handling --------------------------------------------------------
    def _on_crash(self, srv: Server) -> None:
        time.sleep(self.GRACE_S)
        try:
            still = deep_status(self.ref.ssh, srv, self.ref.control)
        except Exception:  # noqa: BLE001
            return
        if still.running:      # came back on its own (script loop) - fine
            return
        if not self._restart_allowed(srv):
            self.ref.notify_event("crash", srv,
                                  f"{srv.name}: crashed; auto-restart "
                                  "paused (too many restarts in the last "
                                  "hour) - check the logs.", ok=False)
            return
        self.ref.notify_event("crash", srv,
                              f"{srv.name}: server process crashed - "
                              "attempting auto-restart...", ok=False)
        try:
            with self.ref.locks.guard(srv.id, "start"):
                self.ref.control.start(srv)
        except Exception as exc:  # noqa: BLE001
            self.ref.notify_event("restart.auto", srv,
                                  f"{srv.name}: auto-restart FAILED: "
                                  f"{exc}", ok=False)
            return
        ok = self._wait_ready(srv, 150)
        if ok:
            self.ref.notify_event("restart.auto", srv,
                                  f"{srv.name}: auto-restarted after crash "
                                  "and is back online.")
        else:
            self.ref.notify_event("restart.auto", srv,
                                  f"{srv.name}: auto-restart ran but the "
                                  "server did not become ready within 150s "
                                  "- check the console.", ok=False)

    def _restart_allowed(self, srv: Server) -> bool:
        now = time.time()
        times = [t for t in self._restart_times.get(srv.id, [])
                 if now - t < 3600]
        if times and now - max(times) < srv.restart_guard_s:
            return False
        if len(times) >= self.MAX_RESTARTS_PER_HOUR:
            return False
        times.append(now)
        self._restart_times[srv.id] = times
        return True

    def _wait_ready(self, srv: Server, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                rep = deep_status(self.ref.ssh, srv, self.ref.control)
                if rep.state == "ready":
                    return True
                if rep.state == "stopped":
                    self._restart_times.setdefault(srv.id, []).append(
                        time.time())
                    return False
            except Exception:  # noqa: BLE001
                pass
            time.sleep(6)
        return False

    # -- disk watchdog (issue 29) ----------------------------------------------
    def _disk_pass(self) -> None:
        now = time.time()
        if now - self._last_disk_check < self.DISK_INTERVAL:
            return
        self._last_disk_check = now
        for srv in list(self.ref.store.servers):
            if not srv.mc_dir or not srv.notify:
                continue
            try:
                free, total = self._disk_of(srv)
            except Exception:  # noqa: BLE001
                continue
            if total <= 0:
                continue
            pct = free / total
            critical = pct < 0.05 or free < 2 * (1 << 30)
            warn = pct < 0.10 or free < 5 * (1 << 30)
            last = self._disk_warned.get(srv.id, 0)
            if (critical or warn) and now - last > 3600:
                level = "CRITICAL" if critical else "low"
                self._disk_warned[srv.id] = now
                self.ref.notify_event(
                    "disk", srv,
                    f"{srv.name}: disk {level} - {free / (1 << 30):.1f} GB "
                    f"free of {total / (1 << 30):.1f} GB. Run Backups -> "
                    "Clean up to free space.", ok=False)

    def _disk_of(self, srv: Server) -> tuple[int, int]:
        res = self.ref.ssh.exec(
            srv, f"df -B1 {shq(srv.mc_dir)} | awk 'NR==2{{print $4\":\"$2}}'",
            timeout=20)
        free_s, total_s = res.stdout.strip().split(":", 1)
        return int(free_s), int(total_s)
