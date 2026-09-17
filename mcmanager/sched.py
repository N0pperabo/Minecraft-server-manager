"""Internal scheduler (issue 17): daily backups, daily restarts,
post-backup retention pruning - all per server, all in-app.

`Scheduler` is one background thread that wakes every 30 s, compares
per-server schedule times (Server.backup_time / restart_time, "HH:MM")
against the local clock, and runs the due action through the same code
paths the UI uses (so operation locks, notifications and progress all
apply).  Last-run state is persisted in ~/.mcmanager/schedule.json so a
restart of the app never re-triggers a job that already ran today, and a
job missed while the app was closed runs up to 45 minutes late instead
 of being skipped silently.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".mcmanager"
STATE_FILE = CONFIG_DIR / "schedule.json"
TICK_S = 30
LATE_WINDOW_S = 45 * 60


def _today_key() -> str:
    return time.strftime("%Y-%m-%d")


class Scheduler:
    """Runs scheduled per-server actions.  `ref` is the app object exposing
    .store, .ssh, .control, .locks, .runner, .notify_event()."""

    def __init__(self, ref) -> None:
        self.ref = ref
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mcm-scheduler")
        self._state: dict[str, str] = {}   # "server:action" -> last run day
        self._load()

    def _load(self) -> None:
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self._state = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._state = {}

    def _save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self._state), encoding="utf-8")
        except OSError:
            pass

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- loop ---------------------------------------------------------------
    def _run(self) -> None:
        time.sleep(5)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - scheduler must never die
                pass
            self._stop.wait(TICK_S)

    def _tick(self) -> None:
        now = time.localtime()
        hhmm_now = now.tm_hour * 60 + now.tm_min
        today = _today_key()
        for srv in list(self.ref.store.servers):
            if not srv.mc_dir:
                continue
            for action, parsed in (("backup", srv.parsed_backup_time()),
                                   ("restart", srv.parsed_restart_time())):
                if parsed is None:
                    continue
                target = parsed[0] * 60 + parsed[1]
                key = f"{srv.id}:{action}"
                if self._state.get(key) == today:
                    continue
                late_ok = 0 <= hhmm_now - target <= LATE_WINDOW_S // 60
                due = (hhmm_now == target) or late_ok
                if not due:
                    continue
                self._state[key] = today
                self._save()
                self._dispatch(action, srv)

    def _dispatch(self, action: str, srv) -> None:
        if action == "backup":
            self.ref.runner.run(
                lambda: self._do_backup(srv),
                on_success=None,
                on_error=lambda e: self.ref.notify_event(
                    "backup", srv, f"{srv.name}: scheduled backup FAILED: {e}",
                    ok=False))
        elif action == "restart":
            self.ref.runner.run(
                lambda: self._do_restart(srv),
                on_success=None,
                on_error=lambda e: self.ref.notify_event(
                    "restart", srv,
                    f"{srv.name}: scheduled restart FAILED: {e}", ok=False))

    # -- actions (same guard/notify semantics as the UI buttons) -------------
    def _do_backup(self, srv) -> None:
        from . import backup as bk
        with self.ref.locks.guard(srv.id, "backup"):
            bk.create_backup(self.ref.ssh, srv, log=None)
            bk.prune_backups(self.ref.ssh, srv, log=None)
        self.ref.notify_event("backup", srv,
                              f"{srv.name}: scheduled backup completed and "
                              "retention applied.")

    def _do_restart(self, srv) -> None:
        if not srv.allow_restart:
            return
        with self.ref.locks.guard(srv.id, "restart"):
            self.ref.control.restart(srv)
        self.ref.notify_event("restart", srv,
                              f"{srv.name}: scheduled restart completed.")
