"""Model persistence (issue 2) + notifier (issue 18) + scheduler (17)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mcmanager import models, notify
from mcmanager.models import Server, ServerStore
from mcmanager.notify import Notifier
from mcmanager import sched


# ---------------------------------------------------- encrypted at rest (2) --
@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(models, "SERVERS_FILE", tmp_path / "servers.json")
    return ServerStore()


def test_passwords_encrypted_at_rest(isolated_store):
    isolated_store.add("Box", "10.0.0.1", "root", "topsecret", 22)
    raw = json.loads(models.SERVERS_FILE.read_text())
    stored = raw["servers"][0]["password"]
    assert stored != "topsecret"
    assert stored.startswith("enc:v1:")
    # and decrypts cleanly on reload
    store2 = ServerStore()
    assert store2.servers[0].password == "topsecret"


def test_legacy_plaintext_file_migrates(isolated_store, monkeypatch):
    models.SERVERS_FILE.write_text(json.dumps({
        "servers": [{"id": "abc", "name": "Old", "host": "1.2.3.4",
                     "password": "plainpw", "mc_dir": "/opt/mc"}]}))
    store = ServerStore()
    assert store.servers[0].password == "plainpw"
    store.save()  # now encrypted
    raw = json.loads(models.SERVERS_FILE.read_text())
    assert raw["servers"][0]["password"].startswith("enc:v1:")
    assert "plainpw" not in models.SERVERS_FILE.read_text()


def test_unknown_fields_dont_kill_load(isolated_store):
    models.SERVERS_FILE.write_text(json.dumps({
        "servers": [{"id": "z", "name": "Future", "host": "1.1.1.1",
                     "password": "", "some_new_field": True}]}))
    store = ServerStore()
    assert len(store.servers) == 1


def test_server_file_permissions_0600(isolated_store):
    import os
    isolated_store.add("Box", "10.0.0.1", "root", "pw", 22)
    mode = os.stat(models.SERVERS_FILE).st_mode & 0o777
    assert mode == 0o600


def test_schedule_field_parsing():
    srv = Server(id="x", name="x", host="h")
    srv.backup_time = "04:30"
    assert srv.parsed_backup_time() == (4, 30)
    srv.backup_time = "25:99"
    assert srv.parsed_backup_time() is None
    srv.backup_time = ""
    assert srv.parsed_backup_time() is None


# ------------------------------------------------------------- notifier (18) --
@pytest.fixture
def notifier(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "HISTORY_FILE", tmp_path / "notif.json")
    monkeypatch.setattr(notify, "SETTINGS_FILE", tmp_path / "hooks.json")
    monkeypatch.setattr(notify, "CONFIG_DIR", tmp_path)
    return Notifier()


def test_notifier_history_and_subscribe(notifier):
    got = []
    notifier.subscribe(lambda ev: got.append(ev))
    notifier.publish("crash", "Box", "crashed!", ok=False)
    assert got and got[0]["kind"] == "crash"
    assert notifier.history()[0]["message"] == "crashed!"


def test_notifier_history_cap(notifier):
    for i in range(400):
        notifier.publish("backup", "Box", f"b{i}")
    assert len(notifier.history(limit=1000)) == 300  # capped at 300


def test_notifier_webhook_config_persists(notifier, tmp_path):
    notifier.set_webhooks(discord="https://discord.com/api/webhooks/x",
                          generic="")
    n2 = Notifier()
    assert n2.webhooks()["discord"].endswith("/x")


def test_notifier_listener_errors_do_not_break_publish(notifier):
    def bad(_ev):
        raise RuntimeError("listener bug")
    notifier.subscribe(bad)
    notifier.publish("disk", "Box", "low")   # must not raise
    assert notifier.history()


# ------------------------------------------------------------- scheduler (17) --
class _App:
    def __init__(self, servers):
        from mcmanager.oplock import OpLocks
        self.store = type("S", (), {"servers": servers})()
        self.locks = OpLocks()
        self.notify_event = lambda *a, **k: None

        class _Runner:
            def run(self, fn, on_success=None, on_error=None):
                fn()
        self.runner = _Runner()


def _bare_scheduler(app):
    sch = sched.Scheduler.__new__(sched.Scheduler)
    sch.ref = app
    sch._state = {}
    sch._stop = None
    return sch


def test_scheduler_fires_due_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "STATE_FILE", tmp_path / "sched.json")
    monkeypatch.setattr(sched, "CONFIG_DIR", tmp_path)
    hh, mm = time.localtime()[3], time.localtime()[4]
    srv = Server(id="s1", name="B", host="h", mc_dir="/opt/mc",
                 backup_time=f"{hh:02d}:{mm:02d}")
    app = _App([srv])
    sch = _bare_scheduler(app)
    fired = []
    sch._do_backup = lambda s: fired.append(("backup", s.id))
    sch._do_restart = lambda s: fired.append(("restart", s.id))
    sch._tick()
    assert ("backup", "s1") in fired
    # second tick same minute: must NOT fire twice (state persisted)
    fired.clear()
    sch._tick()
    assert fired == []


def test_scheduler_skips_undue_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "STATE_FILE", tmp_path / "sched.json")
    monkeypatch.setattr(sched, "CONFIG_DIR", tmp_path)
    srv = Server(id="s2", name="B", host="h", mc_dir="/opt/mc",
                 backup_time="23:59")
    app = _App([srv])
    sch = _bare_scheduler(app)
    fired = []
    sch._do_backup = lambda s: fired.append(("backup", s.id))
    sch._do_restart = lambda s: fired.append(("restart", s.id))
    sch._tick()
    # 23:59 is (almost) never 'now'; nothing should have been dispatched
    if time.localtime()[3] == 23 and time.localtime()[4] == 59:
        pytest.skip("ran exactly at 23:59")
    assert fired == []
