"""Per-server operation locks + a remote step journal for transactions.

Issues addressed:
- 25/26: nothing prevented contradictory operations from running at the
  same time (restart DURING update, backup DURING restore, ...).  Every
  mutating operation now goes through `OpLocks.guard(server_id, op)`,
  which checks a conflict matrix and rejects overlapping work with a
  clear, human-readable message instead of racing.
- 33: if SSH drops in the middle of a multi-step operation (update,
  restore), a JOURNAL on the remote server (.mcmanager/op.journal)
  remembers which step was reached.  On the next connect the app can
  offer "resume or roll back" instead of leaving the server half-updated.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager

from .models import Server
from .ssh_manager import SSHManager, shq

# operation groups -----------------------------------------------------------
# exclusive ops modify the server's files or process and must never overlap
# anything; consult ops only read state and may run in parallel.
_EXCLUSIVE = {"start", "stop", "restart", "backup", "restore", "update",
              "install", "migrate", "cleanup", "modops"}
_CONSULT = {"status", "monitor", "list", "read"}

# matrix entries: op -> set of ops that MUST NOT run while op is active.
# everything not listed may run concurrently (e.g. status polls during a
# backup, which is safe and keeps the UI live).
_CONFLICTS: dict[str, set[str]] = {
    "update": _EXCLUSIVE,
    "restore": _EXCLUSIVE,
    "install": _EXCLUSIVE,
    "migrate": _EXCLUSIVE,
    "backup": {"update", "restore", "restart", "stop", "migrate",
               "install", "backup"},
    "restart": {"update", "restore", "backup", "migrate", "restart",
                "stop", "start"},
    "stop": {"update", "restore", "backup", "restart", "stop", "migrate"},
    "start": {"update", "restore", "migrate", "restart"},
    "cleanup": {"update", "restore", "backup", "migrate"},
    "modops": {"update", "restore", "migrate"},
}


class OperationConflict(RuntimeError):
    """Another operation is already running on this server."""

    def __init__(self, op: str, active_op: str, active_since: float):
        mins = int((time.time() - active_since) / 60)
        super().__init__(
            f"Cannot start '{op}' - '{active_op}' is already running on this "
            f"server (for {mins} min). Wait for it to finish or stop it from "
            "the Operations panel.")
        self.op = op
        self.active_op = active_op


class OpLocks:
    """In-memory registry of running operations, per server."""

    def __init__(self) -> None:
        self._active: dict[str, tuple[str, float]] = {}  # server_id -> (op, since)
        self._lock = threading.Lock()
        self.on_change = None  # optional callback(server_id, op|None)

    def active(self, server_id: str) -> str | None:
        with self._lock:
            entry = self._active.get(server_id)
            return entry[0] if entry else None

    def all_active(self) -> dict[str, str]:
        with self._lock:
            return {sid: op for sid, (op, _t) in self._active.items()}

    def try_acquire(self, server_id: str, op: str) -> None:
        if op not in _CONSULT and op not in _CONFLICTS:
            raise ValueError(f"Unknown operation '{op}'")
        with self._lock:
            entry = self._active.get(server_id)
            if entry:
                active_op, since = entry
                if op in _CONFLICTS.get(active_op, set()) or \
                        active_op in _CONFLICTS.get(op, set()) and op not in _CONSULT:
                    raise OperationConflict(op, active_op, since)
                if op in _EXCLUSIVE:  # exclusive ops never share
                    raise OperationConflict(op, active_op, since)
            if op in _EXCLUSIVE:
                self._active[server_id] = (op, time.time())
        cb = self.on_change
        if cb and op in _EXCLUSIVE:
            try:
                cb(server_id, op)
            except Exception:  # noqa: BLE001
                pass

    def release(self, server_id: str, op: str) -> None:
        with self._lock:
            entry = self._active.get(server_id)
            if entry and entry[0] == op:
                self._active.pop(server_id, None)
                cb = self.on_change
                if cb:
                    try:
                        cb(server_id, None)
                    except Exception:  # noqa: BLE001
                        pass

    @contextmanager
    def guard(self, server_id: str, op: str):
        """`with locks.guard(sid, 'update'): ...` - acquires, yields,
        always releases (even on exceptions)."""
        self.try_acquire(server_id, op)
        try:
            yield
        finally:
            self.release(server_id, op)


LOCKS = OpLocks()


# ======================================================== remote journal ====
class Journal:
    """Step journal stored NEXT TO THE SERVER (.mcmanager/op.journal).

    Written before/after every critical step of update / restore so a
    dropped SSH connection never leaves the app guessing what happened.
    `recover()` reads a leftover journal and tells the UI exactly which
    recovery paths exist.
    """

    def __init__(self, ssh: SSHManager, server: Server) -> None:
        self.ssh = ssh
        self.server = server
        self.path = (server.mc_dir.rstrip("/") + "/.mcmanager/op.journal"
                     if server.mc_dir else "")

    # -- raw IO ---------------------------------------------------------------
    def _exec(self, script: str, timeout: float = 20):
        return self.ssh.exec(self.server, script, timeout=timeout)

    def write(self, op: str, step: str, data: dict | None = None) -> None:
        if not self.path:
            return
        payload = json.dumps({"op": op, "step": step, "data": data or {},
                              "ts": time.time()}, ensure_ascii=False)
        self._exec(
            f"mkdir -p {shq(self.path.rsplit('/', 1)[0])} && "
            f"printf '%s' {shq(payload)} > {shq(self.path)} && sync")

    def read(self) -> dict | None:
        if not self.path:
            return None
        res = self._exec(f"cat {shq(self.path)} 2>/dev/null; true")
        raw = res.stdout.strip()
        if not raw:
            return None
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(j, dict) or "op" not in j:
            return None
        return j

    def clear(self) -> None:
        if not self.path:
            return
        self._exec(f"rm -f {shq(self.path)}")

    # -- recovery -------------------------------------------------------------
    def recover(self) -> dict | None:
        """Return a leftover in-progress journal entry, or None.

        The returned dict includes suggested recovery actions:
          update:  step>=swap -> rollback possible via server.jar.bak-<ts>
                   step<swap  -> nothing destructive happened, just clean up
          restore: snapshot dir .pre-restore-<ts> exists -> rollback possible
        """
        j = self.read()
        if not j:
            return None
        step = (j.get("step") or "").lower()
        if step in ("done", "rolled_back", "aborted", ""):
            return None
        out = dict(j)
        out["recovery"] = []
        op = j.get("op")
        if op == "update":
            if step in ("stopped", "swapped", "starting"):
                out["recovery"] = ["rollback"]
            else:
                out["recovery"] = ["cleanup"]
        elif op == "restore":
            out["recovery"] = ["rollback", "cleanup"]
        else:
            out["recovery"] = ["cleanup"]
        return out
