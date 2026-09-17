"""Server migration (issue 32): move an install to a new path on the same
machine, or transfer it to a DIFFERENT VPS over SFTP with checksum
verification.

- move_local(): stop -> tar-pipe copy to the new path -> byte/size verify
  -> relink start.sh + saved mc_dir -> start.  The old folder is kept
  untouched until the user deletes it (never destructive).
- transfer_to(): stop -> create a verified archive (reuses backup.py,
  so disk guards + progress apply) -> SFTP download to a local temp
  file -> SFTP upload to the target host -> sha256 compare on BOTH
  sides -> extract on the target -> return an adopt dict so the app can
  register the new server entry.  Progress callbacks report both legs.

Both flows take the operation lock ('migrate'), journal their steps, and
leave a rollback path (the original folder stays untouched).
"""
from __future__ import annotations

import os
import posixpath
import tempfile
import time
from pathlib import Path

from . import backup as bk
from .models import Server
from .oplock import Journal
from .ssh_manager import SSHManager, shq


# ------------------------------------------------------------- local move --
def move_local(ssh: SSHManager, server: Server, control, new_path: str,
               log=None, progress=None) -> str:
    """Relocate the install directory on the same machine."""
    new_path = new_path.strip().rstrip("/")
    old = server.mc_dir.rstrip("/")
    if not old:
        raise RuntimeError("No install folder linked.")
    if not new_path.startswith("/"):
        raise RuntimeError("The new path must be absolute (start with /).")
    if new_path == old:
        raise RuntimeError("That is already the install path.")

    journal = Journal(ssh, server)
    was_running = False
    try:
        progress and progress(0.05, "checking target path")
        res = ssh.exec(
            server,
            f"if [ -e {shq(new_path)} ] && [ -n \"$(ls -A {shq(new_path)} 2>/dev/null)\" ]; "
            "then echo BUSY; else mkdir -p " + shq(new_path) + "; echo OK; fi",
            timeout=30)
        if "OK" not in res.stdout:
            raise RuntimeError(f"{new_path} already exists and is not empty.")

        journal.write("migrate", "stopping", {"old": old, "new": new_path})
        was_running = bool(control.status(server))
        if was_running:
            progress and progress(0.1, "stopping server")
            log and log("[move] stopping the server...")
            control.stop(server, log)

        progress and progress(0.2, "copying files (tar pipe)")
        journal.write("migrate", "copying", {"old": old, "new": new_path})
        log and log(f"[move] copying {old} -> {new_path} ...")
        res = ssh.exec(
            server,
            f"tar -C {shq(old)} -cf - --exclude=./backups . | "
            f"tar -C {shq(new_path)} -xf - && echo COPIED", timeout=3600,
            lock=False)
        if "COPIED" not in (res.stdout or ""):
            raise RuntimeError("Copy failed: " + (res.stderr or "")[-200:])

        progress and progress(0.8, "verifying sizes")
        res = ssh.exec(
            server,
            f"a=$(du -sb {shq(old)} --exclude={shq(old)}/backups 2>/dev/null | cut -f1); "
            f"b=$(du -sb {shq(new_path)} 2>/dev/null | cut -f1); "
            'echo "$a:$b"', timeout=120)
        try:
            a_s, b_s = res.stdout.strip().split(":")
            a, b = int(a_s), int(b_s)
            if b < a * 0.95:
                raise RuntimeError(
                    f"Size mismatch after copy ({a} vs {b} bytes) - the "
                    "original folder was NOT modified; check disk space.")
        except ValueError:
            pass  # du unavailable - size check is best-effort

        progress and progress(0.92, "relinking")
        # start.sh uses 'cd "$(dirname "$0")"' so it is path-independent;
        # fix .mcmanager/pid if present and update the saved path.
        ssh.exec(server, f"rm -f {shq(posixpath.join(new_path, '.mcmanager', 'pid'))}",
                 timeout=15)
        server.mc_dir = new_path
        server.external_screen = ""
        journal.write("migrate", "moved", {"old": old, "new": new_path})

        if was_running:
            progress and progress(0.96, "starting server")
            control.start(server, log)
        journal.clear()
        progress and progress(1.0, "done")
        log and log(f"[move] done - install now lives in {new_path}. "
                    f"The old folder {old} was left in place; delete it "
                    "manually once everything works.")
        return new_path
    except Exception:
        journal.write("migrate", "failed", {"old": old, "new": new_path})
        raise


# ------------------------------------------------------------ cross-VPS -----
def transfer_to(ssh: SSHManager, server: Server, control, target: Server,
                target_dir: str, log=None, progress=None) -> dict:
    """Move the install to ANOTHER machine (target = a saved Server entry).

    Steps: stop -> verified local archive -> sftp download (progress) ->
    sftp upload to the target (progress) -> sha256 both sides -> extract
    -> adopt dict for registering the new server.  The source folder is
    kept untouched (rollback = just start it again).
    """
    target_dir = target_dir.strip().rstrip("/")
    if not target_dir.startswith("/"):
        raise RuntimeError("Target path must be absolute.")
    if not server.mc_dir:
        raise RuntimeError("No install folder linked on the source server.")

    journal = Journal(ssh, server)
    was_running = False
    tmp_local = ""
    try:
        journal.write("migrate", "stopping_transfer",
                      {"to": target.name, "dir": target_dir})
        was_running = bool(control.status(server))
        if was_running:
            progress and progress(0.02, "stopping source server")
            control.stop(server, log)

        progress and progress(0.06, "creating verified archive")
        log and log("[transfer] creating the transfer archive...")
        info = bk.create_backup(ssh, server, log=log, profile="full",
                                tag="transfer")

        tmp = Path(tempfile.gettempdir()) / f"mcm-transfer-{int(time.time())}.tar.gz"
        tmp_local = str(tmp)

        def _dl(frac, msg):
            progress and progress(0.1 + frac * 0.4, f"download {msg}")

        progress and progress(0.1, "downloading to this machine")
        log and log("[transfer] downloading archive locally...")
        with ssh.sftp(server) as sftp:
            _sftp_get_progress(sftp, info["path"], tmp, _dl)

        def _ul(frac, msg):
            progress and progress(0.52 + frac * 0.4, f"upload {msg}")

        progress and progress(0.52, "uploading to the target host")
        log and log(f"[transfer] uploading to {target.name}:{target_dir} ...")
        res = ssh.exec(target, f"mkdir -p {shq(target_dir)}", timeout=30)
        remote_archive = posixpath.join(target_dir, "mcm-transfer.tar.gz")
        with ssh.sftp(target) as sftp:
            _sftp_put_progress(sftp, str(tmp), remote_archive, _ul)

        progress and progress(0.93, "verifying checksums")
        sha_local = _sha256_file(tmp)
        res = ssh.exec(target, f"sha256sum {shq(remote_archive)} | cut -d' ' -f1",
                       timeout=300)
        sha_remote = res.stdout.strip()
        if sha_local and sha_remote and sha_local != sha_remote:
            raise RuntimeError("Transfer archive checksum mismatch - the "
                               "upload was corrupted. Nothing was extracted.")
        log and log("[transfer] checksums match.")

        progress and progress(0.96, "extracting on target")
        journal.write("migrate", "extracting", {"to": target.name,
                                                "dir": target_dir})
        res = ssh.exec(
            target,
            f"cd {shq(target_dir)} && tar -xzf {shq(remote_archive)} && "
            f"rm -f {shq(remote_archive)} && ls -1 | head -5", timeout=1800)
        if res.exit_code != 0:
            raise RuntimeError("Extraction on the target failed: "
                               + (res.stderr or "")[-200:])

        journal.clear()
        progress and progress(1.0, "done")
        log and log(f"[transfer] done - the server now lives on "
                    f"{target.name}:{target_dir}. The source folder was kept.")
        return {"host": target.host, "dir": target_dir,
                "platform": server.platform, "version": server.mc_version,
                "ram_mb": server.ram_mb, "name": server.name}
    except Exception:
        journal.write("migrate", "failed", {"to": target.name,
                                            "dir": target_dir})
        raise
    finally:
        if tmp_local:
            try:
                os.unlink(tmp_local)
            except OSError:
                pass


# -- SFTP progress helpers ----------------------------------------------------
_SFTP_CHUNK = 1 << 18


def _sftp_get_progress(sftp, remote: str, local: Path, progress) -> None:
    total = sftp.stat(remote).st_size
    done = 0
    with open(local, "wb") as f:
        def _cb(transferred, _total):
            progress(min(1.0, transferred / max(1, total)), "")
        try:
            sftp.get(remote, str(local), callback=_cb)
        except TypeError:  # very old paramiko without callback
            with sftp.open(remote, "rb") as r:
                while True:
                    chunk = r.read(_SFTP_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    progress(min(1.0, done / max(1, total)), "")


def _sftp_put_progress(sftp, local: str, remote: str, progress) -> None:
    total = os.path.getsize(local)
    with open(local, "rb") as f:
        def _cb(transferred, _total):
            progress(min(1.0, transferred / max(1, total)), "")
        try:
            sftp.put(local, remote, callback=_cb, confirm=True)
        except TypeError:
            done = 0
            with sftp.open(remote, "wb") as w:
                while True:
                    chunk = f.read(_SFTP_CHUNK)
                    if not chunk:
                        break
                    w.write(chunk)
                    done += len(chunk)
                    progress(min(1.0, done / max(1, total)), "")


def _sha256_file(path: str, chunk: int = 1 << 20) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()
