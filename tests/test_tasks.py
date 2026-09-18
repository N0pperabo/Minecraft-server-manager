"""TaskRunner queue protocol tests (v2.0.2).

The v2.0.1 signature `emit(cb, value)` made `runner.emit(ask)` raise
TypeError, which the TOFU policy swallowed as "user refused" - the host-key
dialog never appeared and every first connection was rejected.  These tests
pin the protocol so that can never regress.
"""
from __future__ import annotations

import threading
import time

from mcmanager.tasks import TaskRunner


def _drain_until(runner, done, timeout=5.0):
    deadline = time.time() + timeout
    while not done.is_set() and time.time() < deadline:
        runner.poll()
        time.sleep(0.01)
    runner.poll()


def test_emit_zero_arg_callback():
    """The exact v2.0.1 crash: emit(cb) with no value must work."""
    runner = TaskRunner()
    got = []
    runner.emit(lambda: got.append("x"))
    runner.poll()
    assert got == ["x"]


def test_emit_one_and_multi_arg_callbacks():
    runner = TaskRunner()
    got = []
    runner.emit(got.append, 42)
    runner.emit(lambda a, b: got.append(a + b), 1, 2)
    runner.poll()
    assert got == [42, 3]


def test_run_success_delivers_result():
    runner = TaskRunner()
    out = {}
    done = threading.Event()

    def on_ok(res):
        out["r"] = res
        done.set()

    runner.run(lambda: "payload", on_success=on_ok)
    _drain_until(runner, done)
    assert out["r"] == "payload"


def test_run_error_delivers_exception():
    runner = TaskRunner()
    out = {}
    done = threading.Event()

    def on_err(exc):
        out["e"] = exc
        done.set()

    def boom():
        raise ValueError("nope")

    runner.run(boom, on_error=on_err)
    _drain_until(runner, done)
    assert isinstance(out["e"], ValueError)
    assert str(out["e"]) == "nope"


def test_poll_survives_crashing_callback():
    """A broken callback must not poison the queue for later calls."""
    runner = TaskRunner()
    got = []
    runner.emit(lambda: 1 / 0)
    runner.emit(got.append, "still alive")
    runner.poll()
    assert got == ["still alive"]
