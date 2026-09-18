"""Tiny background-task bridge: run work in threads, deliver results to the
Tk main loop through a queue that the app drains every 100 ms.

v2.0.2 protocol fix: the queue now carries (kind, cb, args) and `emit()`
accepts ANY number of callback arguments - including zero.  The v2.0.1
signature `emit(cb, value)` demanded a second argument, so the host-key
confirmation's `runner.emit(ask)` raised TypeError inside the TOFU policy;
the policy swallowed it as "user refused" and EVERY first connection was
rejected without the dialog ever being shown.
"""
from __future__ import annotations

import os
import queue
import threading
import traceback


class TaskRunner:
    def __init__(self) -> None:
        self.q: "queue.Queue[tuple]" = queue.Queue()

    # -- API used from UI threads ------------------------------------------
    def run(self, fn, on_success=None, on_error=None) -> None:
        """Run fn() in a worker thread; deliver result or exception."""
        def worker():
            try:
                result = fn()
                self.q.put(("ok", on_success, (result,)))
            except Exception as exc:  # noqa: BLE001
                if os.environ.get("MCM_DEBUG"):
                    traceback.print_exc()
                else:
                    print(f"[task error] {type(exc).__name__}: {exc}")
                self.q.put(("err", on_error, (exc,)))
        threading.Thread(target=worker, daemon=True).start()

    def emit(self, cb, *args) -> None:
        """Post a call from any thread; poll() runs cb(*args) on the Tk loop.

        cb may take zero arguments (`emit(lambda: toast.show())`), one
        (`emit(append_line, line)`) or several - whatever matches cb.
        """
        self.q.put(("ok", cb, args))

    # -- called only from the Tk main loop ----------------------------------
    def poll(self) -> int:
        handled = 0
        while True:
            try:
                _kind, cb, args = self.q.get_nowait()
            except queue.Empty:
                return handled
            handled += 1
            if cb is None:
                continue
            try:
                cb(*args)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
