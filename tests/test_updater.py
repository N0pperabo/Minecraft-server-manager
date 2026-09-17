"""Update pipeline tests (issues 14, 16, 27, 28): verified download,
journal steps, automatic rollback when the new jar fails to boot."""
from __future__ import annotations

import pytest

from mcmanager import updater
from mcmanager.updater import (_ver_tuple, _is_newer, current_jar,
                               check_update, perform_update,
                               manifest_read, manifest_write)

from conftest import make_server


def test_version_compare():
    assert _is_newer("26.3", "26.2")
    assert not _is_newer("26.2", "26.2")
    assert _is_newer("1.21.5", "1.21.4")
    assert _is_newer("2", "")
    assert not _is_newer("", "1.0")
    assert _ver_tuple("26.2") == (26, 2)


def test_current_jar_from_start_sh(fake_ssh, server):
    fake_ssh.on("grep -hoE", "exec java -Xms512M -Xmx4G -jar paper.jar nogui")
    assert current_jar(fake_ssh, server) == "paper.jar"


def test_current_jar_falls_back_to_ls(fake_ssh, server):
    fake_ssh.on("ls -1", "/opt/mc/purpur-1.21.4.jar\n/opt/mc/other.jar")
    assert current_jar(fake_ssh, server) == "purpur-1.21.4.jar"


def test_check_update_detects_newer_version(fake_ssh, server, monkeypatch):
    fake_ssh.on("grep -hoE", "exec java -jar paper.jar nogui")
    monkeypatch.setattr(updater.apis, "paper_jar_info", lambda v: {
        "url": "https://fill.papermc.io/paper-26.3.jar",
        "sha256": "ab" * 32, "sha1": "", "size": 1000, "build": "77",
        "version": "26.3"})
    srv = make_server(mc_version="26.2")
    rep = check_update(fake_ssh, srv)
    assert rep["available"] is True
    assert rep["reason"] == "newer version"
    assert rep["current_jar"] == "paper.jar"
    assert rep["info"]["sha256"] == "ab" * 32


def test_check_update_detects_newer_build(fake_ssh, server, monkeypatch):
    """Same MC version, newer Paper BUILD (26.2 build 71 -> 77)."""
    fake_ssh.on("grep -hoE", "exec java -jar paper.jar nogui")
    fake_ssh.on("grep -m1 -hoE", "version 26.2-71")
    monkeypatch.setattr(updater.apis, "paper_jar_info", lambda v: {
        "url": "https://fill.papermc.io/paper-26.2-77.jar",
        "sha256": "ab" * 32, "sha1": "", "size": 1000, "build": "77",
        "version": "26.2"})
    srv = make_server(mc_version="26.2")
    rep = check_update(fake_ssh, srv)
    assert rep["available"] is True
    assert rep["current_build"] == "71"
    assert "71" in rep["reason"] and "77" in rep["reason"]


def test_check_update_up_to_date(fake_ssh, server, monkeypatch):
    fake_ssh.on("grep -hoE", "exec java -jar paper.jar nogui")
    fake_ssh.on("grep -m1 -hoE", "version 26.2-77")
    monkeypatch.setattr(updater.apis, "paper_jar_info", lambda v: {
        "url": "u", "sha256": "", "sha1": "", "size": 1, "build": "77",
        "version": "26.2"})
    srv = make_server(mc_version="26.2")
    rep = check_update(fake_ssh, srv)
    assert rep["available"] is False


# ------------------------------------------------------- full pipeline ------
@pytest.fixture
def update_env(fake_ssh, monkeypatch):
    server = make_server(mc_version="26.2")
    # NOTE: the curl compound command embeds 'stat -c %s' inside $( ),
    # so the curl rule must be registered BEFORE the generic stat rule.
    fake_ssh.on("curl -fL", "SIZE=1000")
    fake_ssh.on("grep -hoE", "exec java -jar paper.jar nogui")
    fake_ssh.on("du -sb", "524288000:/opt/mc")
    fake_ssh.on("df -B1", "90000000000:100000000000")
    fake_ssh.on("TAR_EXIT", "TAR_EXIT=0")
    fake_ssh.on("tar -tzf", "V=0")
    fake_ssh.on("for f in *.tar.gz", "pre-update|1000|%d" % 1700000000)
    fake_ssh.on("stat -c %s", "1000")
    fake_ssh.on("sha256sum", "ab" * 32)
    fake_ssh.on("SWAPPED", "SWAPPED")
    fake_ssh.on("mv ", "OK")
    monkeypatch.setattr(updater.apis, "paper_jar_info", lambda v: {
        "url": "https://fill.papermc.io/paper-26.3.jar",
        "sha256": "ab" * 32, "sha1": "", "size": 1000, "build": "77",
        "version": "26.3"})
    monkeypatch.setattr(updater, "WAIT_READY_S", 0.5)
    return server


def _no_slp(fake_ssh):
    fake_ssh.channels.clear()
    fake_ssh.open_channel = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("no slp"))


def test_update_rolls_back_when_health_fails(fake_ssh, update_env,
                                             monkeypatch):
    """Issue 27: bad jar -> stop -> swap -> start -> NOT ready -> rollback."""
    from conftest import FakeControl
    _no_slp(fake_ssh)
    fake_ssh.on("grep -m1 -c 'Done ('", "1")   # booted marker but no SLP
    control = FakeControl(running=False)
    events = []

    result = perform_update(fake_ssh, update_env, control, log=None,
                            progress=None,
                            notify=lambda k, s, m, ok=True: events.append((k, ok)))

    assert result["ok"] is False
    assert result["rolled_back"] is True
    # rollback path must restore the .bak and (re)start
    assert control.starts >= 1
    assert any(kind == "update" and ok is False for kind, ok in events)
    # journal ended in a terminal state
    assert any("rolled_back" in c for c in fake_ssh.calls)


def test_update_succeeds_when_slp_answers(fake_ssh, update_env):
    from conftest import FakeControl
    from mcmanager import health
    status = {"players": {"online": 0, "max": 20},
              "version": {"name": "Paper 26.3"}}

    def fake_ping(ssh, srv, port=None, timeout=6.0):
        r = health.SLPResult()
        r.ok = True
        r.players_online, r.players_max = 0, 20
        r.version = "Paper 26.3"
        return r

    monkey = pytest.MonkeyPatch()
    monkey.setattr(health, "slp_ping", fake_ping)
    try:
        control = FakeControl(running=False)
        result = perform_update(fake_ssh, update_env, control, log=None)
        assert result["ok"] is True
        assert result["rolled_back"] is False
        assert update_env.mc_version == "26.3"
        # old jar preserved as .bak for manual rollback
        assert any(".bak-" in c for c in fake_ssh.calls)
    finally:
        monkey.undo()


def test_checksum_mismatch_aborts_before_swap(fake_ssh, update_env):
    from conftest import FakeControl
    fake_ssh.rules = [r for r in fake_ssh.rules if r[0] != "sha256sum"]
    fake_ssh.on("sha256sum", "deadbeef" * 8)   # WRONG hash
    control = FakeControl()
    with pytest.raises(RuntimeError, match="(?i)checksum"):
        perform_update(fake_ssh, update_env, control, log=None)
    # server was never stopped, nothing swapped
    assert control.stops == 0
    assert not any("SWAPPED" in c for c in fake_ssh.calls if "mv " not in c)


# ------------------------------------------------------------------ mods ----
def test_manifest_roundtrip(fake_ssh, server):
    fake_ssh.on("cat ", '{"worldedit.jar": {"slug": "worldedit"}}')
    data = manifest_read(fake_ssh, server)
    assert "worldedit.jar" in data
    manifest_write(fake_ssh, server, {"x.jar": {"slug": "x"}})
    assert any("printf" in c and "mods.json" in c
               for c in fake_ssh.calls[-2:])
