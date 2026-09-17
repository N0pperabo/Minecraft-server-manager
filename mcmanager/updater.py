"""Secure server-jar and mod updates with verification + rollback.

Issues addressed:
- 14: `perform_update()` is a full pipeline - pre-update backup (7),
  download to a temp name, CHECKSUM verification (28), graceful stop,
  atomic swap, start, HEALTH CHECK via the real SLP protocol, and
  automatic ROLLBACK if the new jar cannot boot (27).
- 15: version info comes straight from the platform APIs (Paper Fill,
  Mojang manifest, Fabric meta, Forge promos) - including the 2026
  year-based Minecraft versions.
- 16: mod/plugin updates with a server-side manifest
  (.mcmanager/mods.json): version tracking, dependency resolution
  (required deps are auto-installed), per-mod rollback (old jars are
  kept in .mcmanager/trash instead of deleted).
- 26/33: every critical step is journaled to .mcmanager/op.journal so
  an SSH drop mid-update is detectable and recoverable.
"""
from __future__ import annotations

import json
import posixpath
import re
import threading
import time

from . import apis, maint
from .models import Server
from .oplock import Journal
from .ssh_manager import SSHManager, shq

JAR_FROM_START_RE = re.compile(r"-jar\s+(?:\./)?([A-Za-z0-9._@-]+\.jar)")
BUILD_LOG_RE = re.compile(r"version (\d+(?:\.\d+)*)-(\d+)")
WAIT_READY_S = 180


# ============================================================ current state ==
def current_jar(ssh: SSHManager, server: Server) -> str:
    """Filename of the server jar start.sh launches ('' if unknown)."""
    if not server.mc_dir:
        return ""
    start = posixpath.join(server.mc_dir, "start.sh")
    res = ssh.exec(
        server,
        f"grep -hoE -- '-jar (\\\\./)?[A-Za-z0-9._@-]+\\\\.jar' "
        f"{shq(start)} 2>/dev/null | head -1; "
        f"[ -s {shq(start)} ] || ls -1 {shq(server.mc_dir)}/*.jar "
        "2>/dev/null | head -3",
        timeout=20)
    m = JAR_FROM_START_RE.search(res.stdout)
    if m:
        return m.group(1)
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.endswith(".jar"):
            return posixpath.basename(line)
    return ""


def latest_for(platform: str, mc_version: str, channel: str = "stable") -> dict:
    """Latest release info for the platform: url + checksums + version."""
    if platform == "paper":
        info = apis.paper_jar_info(mc_version)
        info.setdefault("version", mc_version)
        return info
    if platform == "vanilla":
        info = apis.vanilla_jar_info(mc_version)
        info["version"] = mc_version
        return info
    if platform == "forge":
        promos = apis.forge_versions_for(mc_version)
        if not promos:
            raise RuntimeError(f"Forge has no build for MC {mc_version}.")
        promo = promos[0]
        info = apis.forge_installer_info(mc_version, promo)
        info["version"] = promo
        return info
    if platform == "fabric":
        # fabric server jar is assembled per-request; no published checksum
        url = apis.fabric_jar_url(mc_version)
        return {"url": url, "sha256": "", "sha1": "", "size": 0,
                "version": mc_version, "build": ""}
    raise RuntimeError(f"Automatic updates are not supported for "
                       f"'{platform}' - use the Setup Wizard.")


def _current_build(ssh: SSHManager, server: Server) -> str:
    """Build number from the boot log, e.g. 'Paper version 26.2-77' -> 77.
    Paper-family servers ship builds per MC version; the running build is
    in the log, the latest build comes from the Fill API."""
    if not server.mc_dir:
        return ""
    log = posixpath.join(server.mc_dir, "logs", "latest.log")
    res = ssh.exec(
        server,
        f"grep -m1 -hoE 'version [0-9.]+-[0-9]+' {shq(log)} 2>/dev/null",
        timeout=20)
    m = BUILD_LOG_RE.search(res.stdout)
    return m.group(2) if m else ""


def check_update(ssh: SSHManager, server: Server) -> dict:
    """Compare the installed version/build with the latest; returns a report."""
    if not server.mc_dir or not server.platform or not server.mc_version:
        raise RuntimeError("Link an install first (Wizard or Detect).")
    jar = current_jar(ssh, server)
    latest = latest_for(server.platform, server.mc_version,
                        server.update_channel)
    version_newer = _is_newer(latest.get("version", ""), server.mc_version)
    cur_build = _current_build(ssh, server) if server.platform == "paper" else ""
    latest_build = str(latest.get("build", ""))
    build_newer = bool(cur_build and latest_build and cur_build != latest_build)
    return {
        "platform": server.platform,
        "current_version": server.mc_version,
        "current_build": cur_build,
        "current_jar": jar,
        "latest_version": latest.get("version", ""),
        "latest_build": latest_build,
        "available": bool(version_newer or build_newer),
        "reason": "newer version" if version_newer else (
            f"build {cur_build} -> {latest_build}" if build_newer else ""),
        "info": latest,
    }


def _ver_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v or ""))


def _is_newer(candidate: str, current: str, build: str = "") -> bool:
    if not candidate:
        return False
    if not current:
        return True
    return _ver_tuple(candidate) > _ver_tuple(current)


# ================================================================ download ==
def _download_verified(ssh: SSHManager, server: Server, url: str,
                       sha256: str, sha1: str, size: int, dest: str,
                       log=None, progress=None) -> None:
    """curl to dest on the server, poll size for progress, verify hash."""
    ssh.exec(server, f"mkdir -p {shq(posixpath.dirname(dest))}", timeout=20)
    if log:
        log(f"[update] downloading {posixpath.basename(dest)}...")
    cmd = (f"curl -fL --retry 3 -o {shq(dest)} {shq(url)} && "
           f"echo 'SIZE='$(stat -c %s {shq(dest)})")
    result: dict = {}
    done = threading.Event()

    def _dl():
        try:
            result["res"] = ssh.exec(server, cmd, timeout=3600, lock=False)
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc
        finally:
            done.set()

    threading.Thread(target=_dl, daemon=True).start()
    while not done.wait(2.0):
        try:
            res = ssh.exec(server, f"stat -c %s {shq(dest)} 2>/dev/null; true",
                           timeout=15)
            cur = int(res.stdout.strip() or "0")
        except Exception:  # noqa: BLE001
            cur = 0
        if size and progress:
            try:
                progress(min(0.95, cur / size), f"{cur / (1 << 20):.0f} MB")
            except Exception:  # noqa: BLE001
                pass
    worker_res = result.get("res")
    if "exc" in result or worker_res is None or "SIZE=" not in (worker_res.stdout or ""):
        raise RuntimeError("Download failed: "
                           + str(result.get("exc", "curl error"))[:200])

    # verification (issue 28): compare against the API-published checksum
    if sha256 or sha1:
        algo, want = ("sha256sum", sha256) if sha256 else ("sha1sum", sha1)
        res = ssh.exec(server, f"{algo} {shq(dest)} | cut -d' ' -f1", timeout=120)
        got = res.stdout.strip().lower()
        if got != want.lower():
            ssh.exec(server, f"rm -f {shq(dest)}", timeout=20)
            raise RuntimeError(
                f"CHECKSUM MISMATCH ({algo}) - the downloaded file is "
                "corrupted or was tampered with. Update aborted, temp file "
                "deleted.")
        if log:
            log(f"[update] {algo} verified - OK")
    else:
        if log:
            log("[update] NOTE: this platform publishes no checksum "
                "(Fabric) - verified by download success only.")
    progress and progress(1.0, "downloaded")


# ============================================================ server update ==
def perform_update(ssh: SSHManager, server: Server, control, log=None,
                   progress=None, notify=None) -> dict:
    """The whole guarded pipeline. Returns {'rolled_back': bool, ...}."""
    from . import backup as bk
    from . import health as hlth

    report = check_update(ssh, server)
    if not report["available"]:
        return {"ok": True, "nothing_to_do": True, "report": report}
    info = report["info"]
    jar = report["current_jar"] or "server.jar"
    d = server.mc_dir.rstrip("/")
    tmp = posixpath.join(d, ".mcmanager", "update", "server.jar.new")
    bak = posixpath.join(d, f"{jar}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    journal = Journal(ssh, server)
    was_running = False

    def _p(frac, msg):
        progress and progress(frac, msg)

    maint.pre_op_check(ssh, server, info.get("size") or (400 << 20),
                       "update the server")
    journal.write("update", "begin",
                  {"new": info.get("version"), "jar": jar, "bak": bak})

    try:
        # 1. automatic pre-update backup (issue 7)
        _p(0.05, "pre-update backup")
        log and log("[update] creating automatic pre-update backup...")
        bk.create_backup(ssh, server, log=log, profile="quick",
                         tag="pre-update")
        journal.write("update", "backed_up", {"bak": bak})

        # 2. verified download (issue 28)
        _download_verified(ssh, server, info["url"], info.get("sha256", ""),
                           info.get("sha1", ""), info.get("size", 0), tmp,
                           log=log, progress=lambda f, m: _p(0.1 + f * 0.5, m))
        journal.write("update", "downloaded", {"bak": bak, "tmp": tmp})

        # 3. graceful stop
        was_running = bool(control.status(server))
        if was_running:
            _p(0.68, "stopping server")
            log and log("[update] stopping the server...")
            control.stop(server, log)
        journal.write("update", "stopped", {"bak": bak, "tmp": tmp,
                                            "was_running": was_running})

        # 4. atomic swap (old jar is KEPT as .bak for rollback)
        _p(0.74, "swapping server jar")
        jar_path = posixpath.join(d, jar)
        res = ssh.exec(
            server,
            f"mv {shq(jar_path)} {shq(bak)} && "
            f"mv {shq(tmp)} {shq(jar_path)} && echo SWAPPED", timeout=60)
        if "SWAPPED" not in res.stdout:
            raise RuntimeError("Could not swap the jar files: "
                               + (res.stderr or "")[-200:])
        journal.write("update", "swapped", {"bak": bak, "was_running": was_running})
        log and log(f"[update] {jar} replaced; previous build kept as "
                    f"{posixpath.basename(bak)}")

        # 5. start + real health check (issue 14/23)
        _p(0.8, "starting new version")
        control.start(server, log)
        journal.write("update", "starting", {"bak": bak,
                                             "was_running": was_running})
        _p(0.88, "health check (SLP)")
        log and log("[update] waiting for the server to become ready...")
        ready = _wait_ready(ssh, server, control, hlth)

        if ready:
            journal.write("update", "done", {"bak": bak})
            journal.clear()
            server.mc_version = info.get("version", server.mc_version)
            _p(1.0, "update complete")
            log and log(f"[update] server is running "
                        f"{server.mc_version} - update complete.")
            notify and notify("update", server,
                              f"{server.name}: updated to "
                              f"{server.platform} {server.mc_version} and "
                              "healthy.")
            return {"ok": True, "rolled_back": False, "report": report,
                    "backup": bak}

        # 6. failed start -> automatic rollback (issue 27)
        log and log("[update] new version did not become ready - "
                    "ROLLING BACK automatically...")
        _p(0.93, "rolling back")
        _rollback_files(ssh, server, control, journal, jar, bak, log)
        journal.write("update", "rolled_back", {"bak": bak})
        journal.clear()
        notify and notify("update", server,
                          f"{server.name}: update to {info.get('version')} "
                          "FAILED health check - rolled back to the previous "
                          "build automatically.", ok=False)
        _p(1.0, "rolled back")
        return {"ok": False, "rolled_back": True, "report": report,
                "backup": bak}
    except Exception:
        journal.write("update", "failed", {"bak": bak})
        raise


def _rollback_files(ssh: SSHManager, server: Server, control, journal,
                    jar: str, bak: str, log=None) -> None:
    d = server.mc_dir.rstrip("/")
    jar_path = posixpath.join(d, jar)
    ssh.exec(server,
             f"if [ -f {shq(bak)} ]; then mv -f {shq(bak)} {shq(jar_path)}; fi",
             timeout=60)
    if control.status(server):
        control.stop(server, log)
    control.start(server, log)


def _wait_ready(ssh: SSHManager, server: Server, control, hlth,
                timeout: float | None = None) -> bool:
    """Poll the REAL protocol health until ready (or stopped = fast fail)."""
    timeout = WAIT_READY_S if timeout is None else timeout
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rep = hlth.deep_status(ssh, server, control)
            if rep.state == "ready":
                return True
            if rep.state == "stopped":
                return False
        except Exception:  # noqa: BLE001
            pass
        time.sleep(6)
    return False


# manual rollback entry point -------------------------------------------------
def rollback_last_update(ssh: SSHManager, server: Server, control,
                         log=None) -> str:
    """Find the newest *.bak-* next to the jar and restore it."""
    d = server.mc_dir.rstrip("/")
    jar = current_jar(ssh, server) or "server.jar"
    res = ssh.exec(
        server,
        f"ls -1t {shq(posixpath.join(d, jar + '.bak-*'))} 2>/dev/null | head -1",
        timeout=20)
    bak = res.stdout.strip()
    if not bak:
        raise RuntimeError("No rollback backup found "
                           f"({jar}.bak-*) on the server.")
    journal = Journal(ssh, server)
    journal.write("update", "manual_rollback", {"bak": bak})
    try:
        if control.status(server):
            control.stop(server, log)
        _rollback_files(ssh, server, control, journal, jar, bak, log)
        journal.clear()
        return posixpath.basename(bak)
    except Exception:
        journal.write("update", "failed", {"bak": bak})
        raise


# ================================================================= mods =====
MANIFEST = ".mcmanager/mods.json"
TRASH = ".mcmanager/trash"


def manifest_read(ssh: SSHManager, server: Server) -> dict:
    if not server.mc_dir:
        return {}
    p = posixpath.join(server.mc_dir, MANIFEST)
    res = ssh.exec(server, f"cat {shq(p)} 2>/dev/null; true", timeout=20)
    try:
        data = json.loads(res.stdout.strip() or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def manifest_write(ssh: SSHManager, server: Server, data: dict) -> None:
    p = posixpath.join(server.mc_dir, MANIFEST)
    payload = json.dumps(data, indent=1, ensure_ascii=False)
    ssh.exec(server,
             f"mkdir -p {shq(posixpath.dirname(p))} && "
             f"printf '%s' {shq(payload)} > {shq(p)}", timeout=20)


def manifest_record(ssh: SSHManager, server: Server, filename: str,
                    version: dict, project: dict) -> None:
    data = manifest_read(ssh, server)
    data[filename] = {
        "slug": project.get("slug") or project.get("id", ""),
        "project_id": project.get("project_id") or project.get("id", ""),
        "title": project.get("title", ""),
        "version_number": version.get("version_number", ""),
        "version_id": version.get("id", ""),
        "sha1": ((version.get("files") or [{}])[0].get("hashes") or {}).get("sha1", ""),
        "installed_at": time.time(),
    }
    manifest_write(ssh, server, data)


def check_mod_updates(ssh: SSHManager, server: Server) -> list[dict]:
    """Installed mods with a newer version on Modrinth."""
    data = manifest_read(ssh, server)
    out: list[dict] = []
    if not server.mc_version:
        return out
    loader = {"paper": "paper", "purpur": "purpur", "spigot": "spigot",
              "bukkit": "bukkit", "fabric": "fabric", "quilt": "quilt",
              "forge": "forge", "neoforge": "neoforge"}.get(server.platform)
    for fname, entry in data.items():
        try:
            versions = apis.modrinth_versions(
                entry.get("slug") or entry.get("project_id"),
                loaders=[loader] if loader else None,
                game_version=server.mc_version)
        except apis.ApiError:
            continue
        if not versions:
            continue
        newest = versions[0]
        if newest.get("version_number") != entry.get("version_number"):
            out.append({"filename": fname, "entry": entry,
                        "new_version": newest, "url": "",
                        "new_number": newest.get("version_number", "")})
    return out


def install_mod_version(ssh: SSHManager, server: Server, project: dict,
                        version: dict, folder: str, log=None,
                        _depth: int = 0) -> str:
    """Download+verify+install one mod version; required dependencies are
    installed recursively (issue 16).  Returns the installed filename."""
    f = apis.primary_file(version)
    if not f or not f.get("url"):
        raise RuntimeError("Modrinth returned no downloadable file.")
    sha1 = (f.get("hashes") or {}).get("sha1", "")
    dest_dir = posixpath.join(server.mc_dir, folder)
    tmp = posixpath.join(server.mc_dir, ".mcmanager", "update",
                         f["filename"])
    _download_verified(ssh, server, f["url"], "", sha1,
                       int(f.get("size") or 0), tmp, log=log)
    # keep any previous jar as .bak in trash for rollback
    target = posixpath.join(dest_dir, f["filename"])
    ssh.exec(server, f"mkdir -p {shq(posixpath.join(server.mc_dir, TRASH))} "
             f"{shq(posixpath.dirname(tmp))}", timeout=20)
    res = ssh.exec(
        server,
        f"if [ -f {shq(target)} ]; then mv {shq(target)} "
        f"{shq(posixpath.join(server.mc_dir, TRASH, f['filename'] + '.' + str(int(time.time())) + '.bak'))}; fi; "
        f"mv {shq(tmp)} {shq(target)} && echo INSTALLED", timeout=60)
    if "INSTALLED" not in res.stdout:
        raise RuntimeError("Could not move the mod into the folder.")
    manifest_record(ssh, server, f["filename"], version, project)
    log and log(f"[mods] installed {f['filename']} "
                f"({version.get('version_number', '?')})")

    # required dependencies (recursive, depth-limited)
    if _depth < 3:
        for dep in version.get("dependencies") or []:
            if dep.get("dependency_type") != "required":
                continue
            pid = dep.get("project_id")
            if not pid:
                continue
            try:
                dep_project = apis.modrinth_project(pid)
                dep_versions = apis.modrinth_versions(
                    dep_project.get("slug") or pid,
                    game_version=server.mc_version)
                if not dep_versions:
                    continue
                install_mod_version(ssh, server, dep_project, dep_versions[0],
                                    folder, log=log, _depth=_depth + 1)
                log and log(f"[mods] auto-installed required dependency "
                            f"'{dep_project.get('title')}'")
            except Exception as exc:  # noqa: BLE001 - deps best-effort
                log and log(f"[mods] dependency '{pid}' could not be "
                            f"auto-installed: {exc}")
    return f["filename"]


def rollback_mod(ssh: SSHManager, server: Server, filename: str,
                 folder: str, log=None) -> str:
    """Restore the newest .bak of a mod from the trash folder."""
    trash = posixpath.join(server.mc_dir, TRASH)
    res = ssh.exec(
        server,
        f"ls -1t {shq(trash)}/{shq(filename)}.*.bak 2>/dev/null | head -1",
        timeout=20)
    bak = res.stdout.strip()
    if not bak:
        raise RuntimeError(f"No rollback copy of {filename} found.")
    target = posixpath.join(server.mc_dir, folder, filename)
    res = ssh.exec(
        server,
        f"mv -f {shq(bak)} {shq(target)} && echo RESTORED", timeout=30)
    if "RESTORED" not in res.stdout:
        raise RuntimeError("Could not restore the previous mod file.")
    data = manifest_read(ssh, server)
    data.pop(filename, None)  # manifest entry now unknown -> refresh later
    manifest_write(ssh, server, data)
    log and log(f"[mods] rolled back {filename}")
    return filename
