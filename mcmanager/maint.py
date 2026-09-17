"""Maintenance: disk-space management and log rotation (issues 29, 30).

- `usage()` reports disk + the size of the big consumers (world, backups,
  logs, libraries) so the UI can show WHERE the space went.
- `cleanup()` is a one-click (and schedulable) reclaim:
    * applies the backup retention policy (backup.prune_backups)
    * keeps only the newest N rotated server logs (logs/*.log.gz),
      gzips an oversized plain .log and deletes ancient ones
    * deletes crash-reports older than 30 days
    * empties the app trash (.mcmanager/trash) and stale update tempdir
- `pre_op_check()` runs before every update/install/download: refuses to
  start when free space is below the estimated need (issue 29).
"""
from __future__ import annotations

import posixpath

from . import backup as bk
from .models import Server
from .ssh_manager import SSHManager, shq

KEEP_ROTATED_LOGS = 12          # logs/1.log.gz ... 12.log.gz
MAX_LATEST_LOG_MB = 64          # gzip latest.log when it exceeds this
CRASH_REPORTS_MAX_AGE_D = 30


def usage(ssh: SSHManager, server: Server) -> dict:
    """Disk usage of the server filesystem + per-directory breakdown."""
    if not server.mc_dir:
        return {}
    d = server.mc_dir.rstrip("/")
    free, total = bk.disk_free(ssh, server, d)
    res = ssh.exec(
        server,
        f"cd {shq(d)} && for p in world world_nether world_the_end "
        "plugins mods config libraries logs backups .mcmanager; do "
        '[ -e "$p" ] && printf "%s|%s\\n" "$p" "$(du -sm "$p" 2>/dev/null | cut -f1)"; '
        "done; true",
        timeout=60)
    parts: dict[str, int] = {}
    for line in res.stdout.splitlines():
        name, _, mb = line.partition("|")
        try:
            parts[name] = int(mb or 0)
        except ValueError:
            continue
    return {"free": free, "total": total, "parts_mb": parts}


def cleanup(ssh: SSHManager, server: Server, log=None,
            progress=None) -> dict:
    """Reclaim disk space. Returns {'freed_mb': N, 'deleted_backups': N}."""
    if not server.mc_dir:
        raise RuntimeError("No install folder linked.")
    d = server.mc_dir.rstrip("/")
    out = {"freed_mb": 0, "deleted_backups": 0}
    log and log("[cleanup] applying backup retention...")
    progress and progress(0.1, "pruning old backups")
    out["deleted_backups"] = bk.prune_backups(ssh, server, log)

    progress and progress(0.4, "rotating logs")
    log and log("[cleanup] rotating/compressing server logs...")
    logs_dir = posixpath.join(d, "logs")
    # delete rotated logs beyond the newest N
    res = ssh.exec(
        server,
        f"cd {shq(logs_dir)} 2>/dev/null && ls -1t *.log.gz 2>/dev/null "
        f"| tail -n +{KEEP_ROTATED_LOGS + 1} | xargs -r rm -f; "
        # compress only OLD plain logs - never the live latest.log
        f"find {shq(logs_dir)} -maxdepth 1 -name '*.log' ! -name 'latest.log' "
        f"-size +{MAX_LATEST_LOG_MB}M -exec gzip -9 {{}} \\; 2>/dev/null; "
        f"find {shq(posixpath.join(d, 'crash-reports'))} -type f "
        f"-mtime +{CRASH_REPORTS_MAX_AGE_D} -delete 2>/dev/null; "
        f"rm -rf {shq(posixpath.join(d, '.mcmanager', 'trash'))}/* "
        f"{shq(posixpath.join(d, '.mcmanager', 'update'))} 2>/dev/null; "
        "echo CLEANED",
        timeout=300)
    if "CLEANED" not in res.stdout:
        log and log("[cleanup] log rotation produced no output (ok on "
                    "fresh installs).")

    progress and progress(0.75, "measuring result")
    free2, _total = bk.disk_free(ssh, server, d)
    out["free_after"] = free2
    log and log(f"[cleanup] done - {free2 / (1 << 30):.1f} GB free now.")
    progress and progress(1.0, "done")
    return out


def pre_op_check(ssh: SSHManager, server: Server, need_bytes: int,
                 what: str) -> None:
    """Refuse to start a disk-hungry operation when space is tight."""
    free, total = bk.disk_free(ssh, server)
    if free and free < need_bytes * 1.15 + (256 << 20):
        raise bk.DiskSpaceError(
            f"Cannot {what}: needs ~{need_bytes / (1 << 20):.0f} MB but only "
            f"{free / (1 << 20):.0f} MB are free. Run the Backups page "
            "'Clean up' first.")


def log_stats(ssh: SSHManager, server: Server) -> dict:
    """Size of logs/ + count of rotated files (for the UI card)."""
    if not server.mc_dir:
        return {"mb": 0, "rotated": 0}
    res = ssh.exec(
        server,
        f"du -sm {shq(posixpath.join(server.mc_dir, 'logs'))} 2>/dev/null "
        "| cut -f1; "
        f"ls -1 {shq(posixpath.join(server.mc_dir, 'logs'))}/*.log.gz "
        "2>/dev/null | wc -l",
        timeout=30)
    lines = res.stdout.split()
    try:
        return {"mb": int(lines[0]), "rotated": int(lines[1])}
    except (ValueError, IndexError):
        return {"mb": 0, "rotated": 0}
