"""Backup system tests (issues 5, 6, 7, 34): retention pruning, include
lists, disk guards, create-backup flow on the FakeSSH."""
from __future__ import annotations

import time

import pytest

from mcmanager import backup as BK
from mcmanager.backup import (prune_plan, create_backup, list_backups,
                              DiskSpaceError, guard_disk, _include_expr)

from conftest import make_server


def _mk(name, age_days):
    return {"name": name, "path": f"/opt/mc/backups/{name}",
            "size": 1000, "mtime": time.time() - age_days * 86400}


# --------------------------------------------------------------- retention --
def test_retention_keeps_newest_n():
    items = [_mk(f"b{i}.tar.gz", i) for i in range(20)]
    doomed = prune_plan(items, keep=5)
    # the 5 newest survive
    for i in range(5):
        assert f"b{i}.tar.gz" not in doomed


def test_retention_tiered_daily():
    # 3 backups per day for the last 3 days, keep=2
    items = []
    for d in range(3):
        for h in (1, 5, 9):
            items.append(_mk(f"d{d}-h{h}.tar.gz", d + h / 24.0))
    doomed = prune_plan(items, keep=2)
    # one per day survives even beyond keep=2
    for d in range(3):
        kept = [i for i in items if i["name"].startswith(f"d{d}-")
                and i["name"] not in doomed]
        assert kept, f"day {d} lost every archive"


def test_retention_never_deletes_everything():
    items = [_mk(f"old{i}.tar.gz", 60 + i) for i in range(10)]
    doomed = prune_plan(items, keep=3)
    assert len(doomed) == 7  # everything but the newest 3


def test_include_expr_contains_worlds_plugins_configs():
    expr = _include_expr(make_server(), "quick")
    for token in ("world", "plugins", "mods", "server.properties",
                  "start.sh", "user_jvm_args.txt"):
        assert token in expr
    # shell-quoted
    assert "'server.properties'" in expr


# ------------------------------------------------------------- disk guards --
def test_guard_disk_aborts_when_full(fake_ssh, server):
    fake_ssh.on("df -B1", "1000000000:100000000000")  # 1 GB free of 100 GB
    with pytest.raises(DiskSpaceError):
        guard_disk(fake_ssh, server, need=50 * (1 << 30), margin=0.35)


def test_guard_disk_passes_when_room(fake_ssh, server):
    fake_ssh.on("df -B1", "90000000000:100000000000")  # 90 GB free
    guard_disk(fake_ssh, server, need=50 * (1 << 30), margin=0.35)


# ------------------------------------------------------------ create flow ---
def _fake_backup_env(fake_ssh, server, monkeypatch=None):
    from mcmanager.backup import archive_name
    if monkeypatch is not None:
        # freeze the timestamp so the listing name matches the created one
        class _FixedTime:
            @staticmethod
            def strftime(fmt, *a):
                return "20990101-000000" if "%H" in fmt else time.strftime(fmt)
            @staticmethod
            def time():
                return 1900000000.0
        monkeypatch.setattr(BK, "time", _FixedTime)
    fake_ssh.on("du -sb", "524288000:/opt/mc")           # 500 MB estimate
    fake_ssh.on("df -B1", "90000000000:100000000000")     # plenty of space
    fake_ssh.on("TAR_EXIT", "TAR_EXIT=0")
    fake_ssh.on("tar -tzf", "V=0")
    # list_backups rule FIRST: its script also contains 'stat -c %s'
    name = archive_name(server, "test")
    fake_ssh.on("for f in *.tar.gz",
                f"{name}|20480000|1900000000")
    fake_ssh.on("stat -c %s", "20480000")                 # archive grows


def test_create_backup_full_flow(fake_ssh, server, monkeypatch):
    progresses = []
    _fake_backup_env(fake_ssh, server, monkeypatch)
    info = create_backup(fake_ssh, server, log=None,
                         progress=lambda f, m: progresses.append(f),
                         tag="test")
    assert info["name"].endswith(".tar.gz")
    assert info["size"] > 0
    assert progresses, "progress callback must fire"
    assert progresses[-1] == 1.0
    # tar must run without the per-server lock (progress polls in parallel)
    assert any("tar -czf" in c for c in fake_ssh.calls)


def test_create_backup_aborts_on_tar_error(fake_ssh, server, monkeypatch):
    _fake_backup_env(fake_ssh, server, monkeypatch)
    fake_ssh.rules = [r for r in fake_ssh.rules if r[0] != "TAR_EXIT"]
    fake_ssh.on("TAR_EXIT", "TAR_EXIT=2", code=1)
    fake_ssh.on("rm -f", "")
    with pytest.raises(RuntimeError):
        create_backup(fake_ssh, server, tag="bad")


def test_list_backups_parses(fake_ssh, server):
    now = int(time.time())
    fake_ssh.on("for f in *.tar.gz",
                "box-20250101-010101.tar.gz|2048|%d\n"
                "box-20250102-020202.tar.gz|4096|%d" % (now - 86400, now))
    items = list_backups(fake_ssh, server)
    assert len(items) == 2
    assert items[0]["name"] == "box-20250102-020202.tar.gz"  # newest first
    assert items[0]["size"] == 4096
