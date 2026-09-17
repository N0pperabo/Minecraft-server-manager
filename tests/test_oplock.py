"""Operation-lock + journal tests (issues 25, 26, 33)."""
from __future__ import annotations

import pytest

from mcmanager.oplock import OpLocks, OperationConflict, Journal

from conftest import make_server


def test_update_blocks_backup_and_restart():
    locks = OpLocks()
    with locks.guard("s1", "update"):
        with pytest.raises(OperationConflict):
            locks.try_acquire("s1", "backup")
        with pytest.raises(OperationConflict):
            locks.try_acquire("s1", "restart")
        with pytest.raises(OperationConflict):
            locks.try_acquire("s1", "restore")


def test_backup_blocks_restart():
    locks = OpLocks()
    with locks.guard("s1", "backup"):
        with pytest.raises(OperationConflict):
            locks.try_acquire("s1", "restart")
        with pytest.raises(OperationConflict):
            locks.try_acquire("s1", "backup")  # no double backup


def test_status_reads_run_during_backup():
    locks = OpLocks()
    with locks.guard("s1", "backup"):
        locks.try_acquire("s1", "status")   # reads never block
        locks.try_acquire("s1", "monitor")


def test_lock_released_on_exception():
    locks = OpLocks()
    with pytest.raises(RuntimeError):
        with locks.guard("s1", "update"):
            raise RuntimeError("boom")
    # lock must be free again
    with locks.guard("s1", "update"):
        pass


def test_servers_are_independent():
    locks = OpLocks()
    with locks.guard("s1", "update"):
        with locks.guard("s2", "update"):
            pass


def test_active_listing_and_callback():
    locks = OpLocks()
    seen = []
    locks.on_change = lambda sid, op: seen.append((sid, op))
    with locks.guard("s9", "backup"):
        assert locks.active("s9") == "backup"
    assert locks.active("s9") is None
    assert ("s9", "backup") in seen and ("s9", None) in seen


# ------------------------------------------------------------------ journal --
def test_journal_write_read_clear(fake_ssh, server):
    j = Journal(fake_ssh, server)
    j.write("update", "swapped", {"bak": "/opt/mc/paper.jar.bak-1"})
    rec = j.read()
    assert rec["op"] == "update"
    assert rec["step"] == "swapped"
    assert rec["data"]["bak"].endswith(".bak-1")
    j.clear()
    assert j.read() is None


def test_journal_recovery_paths(fake_ssh, server):
    j = Journal(fake_ssh, server)
    j.write("update", "swapped", {"bak": "/x"})
    rec = j.recover()
    assert rec and "rollback" in rec["recovery"]

    j.write("update", "downloaded", {})
    rec = j.recover()
    assert rec and rec["recovery"] == ["cleanup"]

    j.write("update", "done", {})
    assert j.recover() is None

    j.write("update", "rolled_back", {})
    assert j.recover() is None

    j.write("restore", "extracting", {"snapshot": "/s"})
    rec = j.recover()
    assert rec and set(rec["recovery"]) == {"rollback", "cleanup"}
