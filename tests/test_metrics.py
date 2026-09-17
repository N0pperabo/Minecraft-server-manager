"""Metrics parsing tests (issue 11): TPS/MSPT strings, jcmd heap output,
RCON session flow, entity list parsing."""
from __future__ import annotations

from mcmanager import metrics as MX
from mcmanager.control import _rcon_pkt
from conftest import FakeChan, make_server

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_parse_tps_output():
    text = "TPS from last 1m, 5m, 15m: 19.98, 20.0, 19.4"
    floats = MX._to_floats(text)
    assert floats == [19.98, 20.0, 19.4]


def test_parse_tps_with_stars_and_commas():
    # Paper marks low values with *
    floats = MX._to_floats("TPS from last 1m, 5m, 15m: 18.2*, 17.9*, 19.99")
    assert floats[0] == 18.2


def test_parse_mspt_output():
    floats = MX._to_floats(
        "MSPT from last 1m, 5m, 15m: 4.282, 3.512, 3.498")
    assert len(floats) == 3 and abs(floats[0] - 4.282) < 0.001


def test_parse_garbage_returns_empty():
    assert MX._to_floats("Unknown command. Type /help for help.") == []
    assert MX._to_floats("") == []


def test_heap_parse_from_jcmd(fake_ssh, server):
    server = make_server(java_path="/opt/jdk/bin/java")
    fake_ssh.on("GC.heap_info",
                " garbage-first heap:\n"
                "   garbage-first heap   used 512M, committed 1024M, "
                "reserved 4096M")
    out = MX._heap_from_jcmd(fake_ssh, server, "4242")
    assert out["heap_used"] == 512 * (1 << 20)
    assert out["heap_committed"] == 1024 * (1 << 20)
    assert out["gc"] == "G1"


def test_heap_parse_missing_jcmd(fake_ssh, server):
    server = make_server(java_path="/usr/bin/java")
    fake_ssh.on("GC.heap_info", "NO_JCMD")
    out = MX._heap_from_jcmd(fake_ssh, server, "4242")
    assert out["heap_used"] is None
    assert out["gc"] == ""


def test_entities_parse():
    class FakeRCON:
        def cmd(self, c):
            if c.startswith("paper entity list"):
                return ("world: 312 entities\nworld_nether: 4 entities")
            return ""
    assert MX._entities_from_rcon(FakeRCON()) == "316"


def test_entities_unavailable():
    class FakeRCON:
        def cmd(self, c):
            return "Unknown command. Type /help for help."
    assert MX._entities_from_rcon(FakeRCON()) == ""


# --------------------------------------------------------------- RCON pkt ----
def test_rcon_packet_layout():
    pkt = _rcon_pkt(7, 3, b"pass")
    # length(4) + requestId(4) + type(4) + payload(4) + 2 NULs = 18
    assert len(pkt) == 18
    import struct
    (length,) = struct.unpack("<i", pkt[:4])
    assert length == 14  # rid + type + payload + 2 NULs
    rid, ptype = struct.unpack("<ii", pkt[4:12])
    assert (rid, ptype) == (7, 3)


def test_collect_end_to_end(fake_ssh, server, monkeypatch):
    """Full collect() with scripted RCON + SLP + jcmd."""
    from mcmanager import health
    from conftest import FakeControl, make_server

    server = make_server(rcon_port=25575, rcon_pass="pw")

    # SLP answer
    def fake_ping(ssh, srv, port=None, timeout=6.0):
        r = health.SLPResult()
        r.ok = True
        r.players_online, r.players_max = 3, 20
        r.version = "Paper 26.2"
        return r
    monkeypatch.setattr(health, "slp_ping", fake_ping)

    # process info
    fake_ssh.on("ps -o etime=,rss=", "  1:02:03  2200000")
    # jcmd heap
    fake_ssh.on("GC.heap_info",
                " garbage-first heap:\n  used 1500M, committed 2048M")
    # RCON session: auth ok + tps + mspt + entities
    import struct

    def rcon_response(rid, payload: bytes) -> bytes:
        body = struct.pack("<ii", rid, 0) + payload + b"\x00\x00"
        return struct.pack("<i", len(body)) + body

    script = b""
    script += rcon_response(0x2A7D, b"")  # auth ack
    script += rcon_response(0x2A7E, b"TPS from last 1m, 5m, 15m: 19.9, 20.0, 20.0")
    script += rcon_response(0x2A7F, b"MSPT from last 1m, 5m, 15m: 5.1, 4.9, 4.8")
    script += rcon_response(0x2A80, b"world: 120 entities")
    fake_ssh.add_channel(script)

    ctrl = FakeControl(running=True)
    m = MX.collect(fake_ssh, server, ctrl)
    assert m["players"] == (3, 20)
    assert m["tps"] == (19.9, 20.0, 20.0)
    assert m["mspt"] == (5.1, 4.9, 4.8)
    assert m["entities"] == "120"
    assert m["heap_used"] == 1500 * (1 << 20)
    assert m["gc"] == "G1"
    assert m["rss_mb"] == round(2200000 / 1024, 1)
    assert m["state"] == "ready"


def test_rcon_session_needs_port(fake_ssh, server):
    """No RCON configured -> collect() degrades gracefully, no crash."""
    from conftest import FakeControl
    fake_ssh.channels.clear()
    m = MX.collect(fake_ssh, server, FakeControl(running=False))
    assert m["tps"] == (None, None, None)  # RCON off is a normal state
