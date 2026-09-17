"""Tiny background-task bridge: run work in threads, deliver results to the
Tk main loop through a queue that the app drains every 100 ms."""
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
                self.q.put((on_success, result, None))
            except Exception as exc:  # noqa: BLE001
                if os.environ.get("MCM_DEBUG"):
                    traceback.print_exc()
                else:
                    print(f"[task error] {type(exc).__name__}: {exc}")
                self.q.put((on_error, None, exc))
        threading.Thread(target=worker, daemon=True).start()

    def emit(self, cb, value) -> None:
        """Post a value (e.g. a streamed log line) from any thread."""
        self.q.put((cb, value, None))

    # -- called only from the Tk main loop ----------------------------------
    def poll(self) -> int:
        handled = 0
        while True:
            try:
                cb, result, err = self.q.get_nowait()
            except queue.Empty:
                return handled
            handled += 1
            if cb is None:
                continue
            try:
                cb(err if err is not None else result)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
