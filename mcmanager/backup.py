"""Backup / restore system with retention policy and live progress.

Issues addressed:
- 5: complete backup + restore of world(s), configuration, mods, plugins
  (profiles: 'quick' = everything except libraries/logs/backups,
  'full' = everything except logs/backups).
- 6: automatic backups + retention - `prune_plan()` keeps the N newest
  archives plus a tiered history (one per day for a week, one per week
  for a month) and deletes the rest; schedules live in sched.py, the
  pre-update/pre-restore automatic backup is `create_backup(tag=...)`.
- 7: `create_backup()` is called automatically before every update and
  before every restore (journal-protected).
- 34: tar runs on its own SSH channel while the archive file is polled
  on another - the UI gets real 0..1 progress, not a frozen window.
- 29: a disk-space guard refuses to start a backup that would fill the
  disk (needs ~35% of the estimated size free for gzip headroom).
"""
from __future__ import annotations

import posixpath
import re
import threading
import time

from .models import Server
from .oplock import Journal
from .ssh_manager import SSHManager, shq

TS_RE = re.compile(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")

# what goes into a backup -----------------------------------------------------
WORLD_DIRS = ("world", "world_nether", "world_the_end", "world_*")
CONFIG_GLOBS = ("server.properties", "eula.txt", "*.yml", "*.json",
                "*.toml", "user_jvm_args.txt", "start.sh", "run.sh",
                ".mcmanager/mods.json")
QUICK_SKIP = ("logs", "backups", "libraries", "cache", "versions",
              ".mcmanager/update", "crash-reports", "plugins/.mcmanager-cache")


def _backup_dir(server: Server) -> str:
    if server.backup_dir:
        return server.backup_dir.rstrip("/")
    return posixpath.join(server.mc_dir.rstrip("/"), "backups", "mcmanager")


def archive_name(server: Server, tag: str = "") -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", server.name).strip("-").lower() \
        or "server"
    ts = time.strftime("%Y%m%d-%H%M%S")
    return f"{stem}{('-' + tag) if tag else ''}-{ts}.tar.gz"


# ------------------------------------------------------------------ sizing --
def _estimate_bytes(ssh: SSHManager, server: Server, profile: str) -> int:
    """Uncompressed size of everything that would go into the archive."""
    d = shq(server.mc_dir)
    excl = " ".join(f"--exclude=./{s}" for s in QUICK_SKIP)
    if profile == "full":
        expr = f"cd {d} && du -sb --exclude=./logs --exclude=./backups . 2>/dev/null | cut -f1"
    else:
        expr = f"cd {d} && du -sb {excl} . 2>/dev/null | cut -f1"
    res = ssh.exec(server, expr, timeout=60)
    try:
        return int(res.stdout.strip().splitlines()[0] or "0")
    except (ValueError, IndexError):
        return 0


def disk_free(ssh: SSHManager, server: Server,
              path: str | None = None) -> tuple[int, int]:
    """(free_bytes, total_bytes) of the filesystem holding path."""
    p = shq(path or server.mc_dir or "/")
    res = ssh.exec(server, f"df -B1 {p} | awk 'NR==2{{print $4\":\"$2}}'",
                   timeout=20)
    try:
        free_s, total_s = res.stdout.strip().split(":", 1)
        return int(free_s), int(total_s)
    except (ValueError, IndexError):
        return 0, 1


class DiskSpaceError(RuntimeError):
    pass


def guard_disk(ssh: SSHManager, server: Server, need: int,
               margin: float = 0.35) -> None:
    free, total = disk_free(ssh, server, _backup_dir(server))
    if free and need and free < need * margin + (512 << 20):
        raise DiskSpaceError(
            f"Not enough disk space: backup needs ~{need * margin / (1 << 30):.1f} GB "
            f"headroom, only {free / (1 << 30):.1f} GB free. Clean up old "
            "backups first (Backups page -> Clean up).")


# ----------------------------------------------------------------- include --
def _include_expr(server: Server, profile: str) -> str:
    """Shell word list of things to archive (relative to mc_dir)."""
    parts: list[str] = []
    if profile == "full":
        return ". --exclude=./logs --exclude=./backups"
    parts += list(WORLD_DIRS)
    parts += ["plugins", "mods", "config", "defaultconfigs"]
    parts += list(CONFIG_GLOBS)
    return " ".join(shq(p) for p in parts)


# ------------------------------------------------------------------ create --
def create_backup(ssh: SSHManager, server: Server, log=None, progress=None,
                  profile: str = "quick", tag: str = "",
                  verify: bool = True) -> dict:
    """Create a .tar.gz backup. Returns archive info dict.

    Progress: tar runs lock-free on its own channel; a poller watches the
    archive grow against the up-front estimate (issue 34).
    """
    if not server.mc_dir:
        raise RuntimeError("No install folder linked.")
    d = server.mc_dir.rstrip("/")
    bdir = _backup_dir(server)
    name = archive_name(server, tag)
    dest = posixpath.join(bdir, name)

    log and log(f"[backup] estimating size ({profile} profile)...")
    est = _estimate_bytes(ssh, server, profile)
    guard_disk(ssh, server, est if est else (200 << 20), margin=0.4)

    ssh.exec(server, f"mkdir -p {shq(bdir)}", timeout=20)
    log and log(f"[backup] writing {name} "
                f"(~{est / (1 << 20):.0f} MB raw)...")

    includes = _include_expr(server, profile)
    cmd = (f"cd {shq(d)} && tar -czf {shq(dest)} {includes} 2>>"
           f"{shq(posixpath.join(bdir, 'backup.errors'))}; echo TAR_EXIT=$?")

    result: dict = {}
    done = threading.Event()

    def _tar():
        try:
            res = ssh.exec(server, cmd, timeout=3600, lock=False)
            result["res"] = res
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_tar, daemon=True)
    worker.start()

    # poll the growing archive for progress
    while not done.wait(2.0):
        try:
            res = ssh.exec(server, f"stat -c %s {shq(dest)} 2>/dev/null; true",
                           timeout=15)
            size = int(res.stdout.strip() or "0")
        except Exception:  # noqa: BLE001
            size = 0
        if est and progress:
            try:
                progress(min(0.98, (size * 2.8) / max(1, est)),
                         f"{size / (1 << 20):.0f} MB written")
            except Exception:  # noqa: BLE001
                pass
    worker.join(timeout=5)

    if "exc" in result:
        raise result["exc"]
    res = result.get("res")
    if res is None or "TAR_EXIT=0" not in (res.stdout or ""):
        tail = ((res and (res.stderr or res.stdout)) or "tar failed")[-300:]
        ssh.exec(server, f"rm -f {shq(dest)}", timeout=20)
        raise RuntimeError(f"Backup failed: {tail}")

    if verify:
        log and log("[backup] verifying archive integrity...")
        vres = ssh.exec(server, f"tar -tzf {shq(dest)} >/dev/null 2>&1; echo V=$?",
                        timeout=600)
        if "V=0" not in vres.stdout:
            ssh.exec(server, f"rm -f {shq(dest)}", timeout=20)
            raise RuntimeError("Archive failed integrity check - deleted. "
                               "Check free disk space.")

    info = list_backups(ssh, server)
    for item in info:
        if item["name"] == name:
            log and log(f"[backup] done - {name} "
                        f"({item['size'] / (1 << 20):.0f} MB)")
            progress and progress(1.0, "complete")
            return item
    return {"name": name, "path": dest, "size": 0, "mtime": time.time()}


# -------------------------------------------------------------------- list --
def list_backups(ssh: SSHManager, server: Server) -> list[dict]:
    """All backups, newest first: [{name, path, size, mtime}]."""
    bdir = _backup_dir(server)
    res = ssh.exec(
        server,
        f"cd {shq(bdir)} 2>/dev/null && for f in *.tar.gz; do "
        '[ -f "$f" ] || continue; '
        's=$(stat -c %s "$f"); m=$(stat -c %Y "$f"); '
        'printf "%s|%s|%s\\n" "$f" "$s" "$m"; done; true',
        timeout=30)
    items: list[dict] = []
    for line in res.stdout.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        try:
            size, mtime = int(parts[1]), int(parts[2])
        except ValueError:
            continue
        items.append({"name": parts[0], "path": posixpath.join(bdir, parts[0]),
                      "size": size, "mtime": mtime})
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items


# ---------------------------------------------------------------- retention --
def prune_plan(items: list[dict], keep: int, now: float | None = None) -> list[str]:
    """Tiered retention: always keep the newest `keep`; additionally keep
    one archive per calendar day for the last 7 days and one per ISO week
    for the last 4 weeks.  Returns the names to DELETE."""
    now = now or time.time()
    keep = max(1, keep)
    items = sorted(items, key=lambda i: i["mtime"], reverse=True)
    survivors: set[str] = {i["name"] for i in items[:keep]}
    seen_days: set[str] = set()
    seen_weeks: set[str] = set()
    for it in items:
        age = now - it["mtime"]
        if age > 31 * 86400:
            continue  # too old for the tiered window; only keep= protects it
        day = time.strftime("%Y-%m-%d", time.localtime(it["mtime"]))
        wk = time.strftime("%G-W%V", time.localtime(it["mtime"]))  # ISO week
        if age <= 7 * 86400 and day not in seen_days:
            seen_days.add(day)
            survivors.add(it["name"])
        if age <= 28 * 86400 and wk not in seen_weeks:
            seen_weeks.add(wk)
            survivors.add(it["name"])
    return [i["name"] for i in items if i["name"] not in survivors]


def prune_backups(ssh: SSHManager, server: Server, log=None) -> int:
    """Apply the retention policy; returns number of deleted archives."""
    items = list_backups(ssh, server)
    doomed = prune_plan(items, server.backup_keep)
    if not doomed:
        log and log("[backup] retention: nothing to delete.")
        return 0
    for name in doomed:
        ssh.exec(server, f"rm -f {shq(posixpath.join(_backup_dir(server), name))}",
                 timeout=30)
    log and log(f"[backup] retention: deleted {len(doomed)} old "
                f"archive(s), kept {len(items) - len(doomed)}.")
    return len(doomed)


# ------------------------------------------------------------------ restore --
def restore_backup(ssh: SSHManager, server: Server, archive: str,
                   control, log=None, progress=None) -> dict:
    """Restore an archive onto the server (transactional).

    1. journal starts, archive integrity is verified
    2. running server is stopped
    3. CURRENT data is snapshotted to .mcmanager/pre-restore-<ts>.tar.gz
       (rollback path, issue 27-style safety for restores)
    4. archive extracted into mc_dir
    5. server restarted if it was running before
    Journal steps make an SSH drop recoverable (issue 33).
    """
    if not server.mc_dir:
        raise RuntimeError("No install folder linked.")
    d = server.mc_dir.rstrip("/")
    journal = Journal(ssh, server)
    was_running = False
    snapshot = ""

    progress and progress(0.02, "verifying archive")
    res = ssh.exec(server, f"tar -tzf {shq(archive)} >/dev/null 2>&1; echo V=$?",
                   timeout=600)
    if "V=0" not in res.stdout:
        raise RuntimeError("Archive is corrupted or unreadable - restore "
                           "aborted, nothing was changed.")

    try:
        was_running = bool(control.status(server))
        journal.write("restore", "started", {"archive": archive})
        if was_running:
            progress and progress(0.08, "stopping server")
            log and log("[restore] stopping the server...")
            control.stop(server, log)

        # snapshot the CURRENT live data so a bad restore can be undone
        progress and progress(0.2, "snapshotting current data")
        snapshot = posixpath.join(d, ".mcmanager",
                                  "pre-restore-%s.tar.gz"
                                  % time.strftime("%Y%m%d-%H%M%S"))
        journal.write("restore", "snapshot", {"snapshot": snapshot})
        ssh.exec(server, f"mkdir -p {shq(posixpath.dirname(snapshot))}", timeout=20)
        snap_cmd = (f"cd {shq(d)} && tar -czf {shq(snapshot)} "
                    f"{_include_expr(server, 'quick')} 2>/dev/null; echo S=$?")
        snap = ssh.exec(server, snap_cmd, timeout=1200, lock=False)
        if "S=0" not in (snap.stdout or ""):
            raise RuntimeError("Could not snapshot current data - restore "
                               "aborted (server data is unchanged).")

        progress and progress(0.55, "extracting archive")
        journal.write("restore", "extracting", {"snapshot": snapshot})
        log and log("[restore] extracting backup...")
        ext = ssh.exec(
            server, f"cd {shq(d)} && tar -xzf {shq(archive)}; echo X=$?",
            timeout=1800, lock=False)
        if "X=0" not in (ext.stdout or ""):
            raise RuntimeError("Extraction failed: "
                               + ((ext.stderr or "")[-300:] or "unknown error"))

        if was_running:
            progress and progress(0.85, "restarting server")
            journal.write("restore", "restarting", {"snapshot": snapshot})
            log and log("[restore] starting the server again...")
            control.start(server, log)
        journal.write("restore", "done", {"snapshot": snapshot})
        journal.clear()
        progress and progress(1.0, "restore complete")
        log and log("[restore] done. A rollback snapshot of the previous "
                    "state was kept in .mcmanager/ on the server.")
        return {"ok": True, "snapshot": snapshot}
    except Exception:
        journal.write("restore", "failed", {"snapshot": snapshot})
        raise


def rollback_restore(ssh: SSHManager, server: Server, snapshot: str,
                     control, log=None) -> None:
    """Undo a restore using the pre-restore snapshot."""
    restore_backup(ssh, server, snapshot, control, log=log)


def delete_backup(ssh: SSHManager, server: Server, name: str) -> bool:
    res = ssh.exec(server, f"rm -f {shq(posixpath.join(_backup_dir(server), name))}",
                   timeout=30)
    return res.exit_code == 0
