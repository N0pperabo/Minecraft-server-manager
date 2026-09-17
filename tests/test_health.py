"""Health tests (issues 8, 9, 22, 23): SLP protocol, deep status,
watchdog restart guards."""
from __future__ import annotations

import json
import time

from mcmanager import health
from mcmanager.health import (write_varint, read_varint, slp_ping,
                              deep_status, Watchdog)

from conftest import FakeChan, FakeControl, make_server


# ------------------------------------------------------------ varint codec --
def test_varint_roundtrip():
    for v in (0, 1, 127, 128, 255, 4096, 32767, 770, 2097151):
        chan = FakeChan(write_varint(v))
        assert read_varint(chan) == v


def test_varint_encoding_known_bytes():
    assert write_varint(0) == b"\x00"
    assert write_varint(127) == b"\x7f"
    assert write_varint(128) == b"\x80\x01"
    assert write_varint(770) == b"\x82\x06"


# ------------------------------------------------------------------ SLP ------
def _slp_bytes(payload: dict) -> bytes:
    j = json.dumps(payload).encode()
    inner = write_varint(0x00) + write_varint(len(j)) + j
    return write_varint(len(inner)) + inner


def test_slp_ping_success(fake_ssh, server):
    status = {
        "version": {"name": "Paper 26.2", "protocol": 772},
        "players": {"online": 7, "max": 40},
        "description": {"text": "A Minecraft Server"},
    }
    fake_ssh.add_channel(_slp_bytes(status))
    res = slp_ping(fake_ssh, server)
    assert res.ok, res.error
    assert res.players_online == 7 and res.players_max == 40
    assert res.version == "Paper 26.2"
    assert res.motd == "A Minecraft Server"
    assert res.latency_ms >= 0


def test_slp_ping_refused(fake_ssh, server):
    fake_ssh.channels.clear()

    def boom(*a, **k):
        raise RuntimeError("no route")

    fake_ssh.open_channel = boom
    res = slp_ping(fake_ssh, server)
    assert not res.ok
    assert "tunnel" in res.error


def test_slp_ping_garbage(fake_ssh, server):
    fake_ssh.add_channel(b"\xff\xff\xff\xff\xffnot-json")
    res = slp_ping(fake_ssh, server)
    assert not res.ok


# ------------------------------------------------------------ deep status ----
def test_deep_status_stopped(fake_ssh, server):
    rep = deep_status(fake_ssh, server, FakeControl(running=False))
    assert rep.state == "stopped"
    assert not rep.running


def test_deep_status_ready_via_slp(fake_ssh, server):
    fake_ssh.add_channel(_slp_bytes({"players": {"online": 1, "max": 20},
                                     "version": {"name": "Paper"}}))
    rep = deep_status(fake_ssh, server, FakeControl(running=True))
    assert rep.state == "ready"
    assert rep.slp.players_online == 1


def test_deep_status_unresponsive_after_boot(fake_ssh, server):
    # process alive, SLP broken, but log shows Done -> hung server
    fake_ssh.channels.clear()
    fake_ssh.open_channel = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("closed"))
    fake_ssh.on("grep -m1 -c 'Done ('", "1")
    rep = deep_status(fake_ssh, server, FakeControl(running=True))
    assert rep.state == "unresponsive"


def test_deep_status_starting(fake_ssh, server):
    fake_ssh.channels.clear()
    fake_ssh.open_channel = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("closed"))
    fake_ssh.on("grep -m1 -c 'Done ('", "0")
    rep = deep_status(fake_ssh, server, FakeControl(running=True))
    assert rep.state == "starting"


# ---------------------------------------------------------------- watchdog --
class _Hub:
    """Minimal app stand-in for the watchdog."""

    def __init__(self, servers, control):
        from mcmanager.oplock import OpLocks
        self.store = type("S", (), {"servers": servers})()
        self.ssh = None
        self.control = control
        self.locks = OpLocks()
        self.events = []

    def notify_event(self, kind, srv, message, ok=True):
        self.events.append((kind, message, ok))


def test_watchdog_restart_rate_limit():
    hub = _Hub([], FakeControl())
    wd = Watchdog.__new__(Watchdog)   # no thread
    wd.ref = hub
    wd._restart_times = {}
    srv = make_server(restart_guard_s=300)   # default guard interval
    assert wd._restart_allowed(srv)
    assert not wd._restart_allowed(srv)      # inside the guard interval
    # after the guard interval passes, restarts are allowed again
    wd._restart_times[srv.id] = [time.time() - 400]
    assert wd._restart_allowed(srv)
    # hourly cap
    wd._restart_times[srv.id] = [time.time() - i * 60 for i in range(1, 6)]
    assert not wd._restart_allowed(srv)


def test_watchdog_disk_threshold(monkeypatch):
    srv = make_server(mc_dir="/opt/mc")
    hub = _Hub([srv], FakeControl())
    wd = Watchdog.__new__(Watchdog)
    wd.ref = hub
    wd._last_disk_check = 0
    wd._disk_warned = {}
    monkeypatch.setattr(wd, "_disk_of",
                        lambda s: (1 * (1 << 30), 100 * (1 << 30)))
    wd._disk_pass()
    assert any(kind == "disk" and not ok
               for kind, _m, ok in hub.events), "low disk must raise an event"
    # throttle: no duplicate alarms within the hour
    hub.events.clear()
    wd._disk_pass()
    assert hub.events == []
