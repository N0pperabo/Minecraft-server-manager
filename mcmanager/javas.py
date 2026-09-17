"""Java runtime management (v2.0 - fixes issues 1 and 15).

The v1 installer had two bugs:
- `required_java("26.2")` returned 21: the old comparison
  `(major, minor) > (1, 20)` treated the new YEAR-BASED Minecraft
  versions (26.1, 26.2, ... introduced in 2026) like ancient 1.x ones.
- it only looked at the DEFAULT `java` on PATH: a perfectly installed
  Java 25 under /usr/lib/jvm was invisible, so the installer re-installed
  OpenJDK 21 (or failed).

Now:
- full version table for BOTH naming schemes (1.x and 26.x-year-based,
  plus YYwWW snapshots): 26.x -> Java 25, 1.20.5+ -> 21, 1.18-1.20.4 ->
  17, 1.17 -> 16, 1.12-1.16 -> 8.
- `discover_javas()` finds EVERY JVM on the machine (PATH, /usr/lib/jvm,
  update-alternatives, IntelliJ ~/.jdks, sdkman, our managed dir) with
  one remote call and picks the best match for the required major.
- `ensure_java()` installs a missing runtime from the Adoptium/Temurin
  API into ~/.mcmanager/jdk/<major> WITHOUT root (no sudo, no
  NOPASSWD); the package manager is only a fallback and then runs
  through `sudo -S` with the password piped (issue 4).
- start.sh gets the absolute java path baked in, so the runtime the
  app picked is the runtime the server actually runs.
"""
from __future__ import annotations

import re

from .models import Server
from .ssh_manager import SSHManager, shq
from .secure import sudo_stdin, SCOPED_SUDOERS_HINT

# ---------------------------------------------------------------- versions --
_SNAPSHOT_RE = re.compile(r"^(\d{2})w\d{2}[a-z]$", re.IGNORECASE)


def required_java(mc_version: str) -> int:
    """Java major required for a Minecraft version.

    Handles the classic scheme (1.21.4), the 2026 year-based scheme
    (26.1, 26.2, ...) and snapshot ids (26w13a, 25w46a, 1.21.4-rc1).
    """
    v = (mc_version or "").strip().lower()
    if not v:
        return 21
    # snapshot "26w13a" -> year 2026 -> Java 25 ; "25w46a" -> 2025 -> 21
    m = _SNAPSHOT_RE.match(v)
    if m:
        year = 2000 + int(m.group(1))
        return 25 if year >= 2026 else 21
    parts = v.split("rc")[0].split("pre")[0].split("exp")[0].split(".")
    try:
        major = int(re.sub(r"[^0-9].*$", "", parts[0]) or "0")
    except ValueError:
        return 21
    if major <= 0:
        return 21
    if major == 1:  # classic 1.x.y scheme
        minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        patch = int(re.sub(r"\D.*$", "", parts[2] or "0") or 0) \
            if len(parts) > 2 else 0
        if (minor, patch) >= (20, 5):
            return 21
        if minor >= 18:
            return 17
        if minor == 17:
            return 16
        return 8
    # year-based scheme without the "1." prefix (26.1, 26.2, 27.x ...)
    if major >= 26:
        return 25
    if major >= 21:  # defensive: unexpected future scheme
        return 21
    return 21


def java_for_server(server: Server) -> int:
    """Required Java major, honoring the per-server override (issue 31)."""
    if server.java_major >= 8:
        return server.java_major
    return required_java(server.mc_version)


# --------------------------------------------------------------- discovery --
# one remote script -> lines "JAVA|<path>|<major>|<raw version string>"
_DISCOVER = r"""
for j in \
  $(command -a java 2>/dev/null) \
  /usr/lib/jvm/*/bin/java \
  /usr/lib/jvm/*/*/bin/java \
  $HOME/.jdks/*/bin/java \
  $HOME/.sdkman/candidates/java/*/bin/java \
  $HOME/.mcmanager/jdk/*/bin/java \
  /opt/java/*/bin/java /opt/jdk/*/bin/java ; do
  [ -x "$j" ] || continue
  raw=$("$j" -version 2>&1 | head -1)
  case "$raw" in *version*) ;; *) continue;; esac
  maj=$(printf '%s' "$raw" | grep -oE '"([0-9]+)\.' | head -1 | tr -d '".')
  [ -n "$maj" ] || continue
  case "$maj" in 1*)  # legacy 1.8.0_x naming -> major 8
     sub=$(printf '%s' "$raw" | grep -oE '"1\.([0-9]+)\.' | head -1 | cut -d. -f2)
     [ -n "$sub" ] && maj=$sub;;
  esac
  echo "JAVA|$j|$maj|$raw"
done | sort -u
"""


def discover_javas(ssh: SSHManager, server: Server) -> list[dict]:
    """Every usable JVM on the remote machine: [{path, major, raw}]."""
    res = ssh.exec(server, _DISCOVER, timeout=40)
    out: list[dict] = []
    seen: set[str] = set()
    for line in res.stdout.splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4 or parts[0] != "JAVA":
            continue
        path, major, raw = parts[1].strip(), parts[2].strip(), parts[3].strip()
        try:
            major_i = int(major)
        except ValueError:
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "major": major_i, "raw": raw})
    out.sort(key=lambda j: j["major"], reverse=True)
    return out


def pick_java(javas: list[dict], required_major: int) -> dict | None:
    """Best JVM for the requirement: the NEWEST runtime that still
    satisfies it (Minecraft requires 'Java N or newer').  Sorts by major
    itself so callers do not have to."""
    ok = sorted((j for j in javas if j["major"] >= required_major),
                key=lambda j: j["major"], reverse=True)
    return ok[0] if ok else None


# --------------------------------------------------------------- installs ---
TEMURIN_API = ("https://api.adoptium.net/v3/binary/latest/{major}/ga/linux/"
               "{arch}/jdk/hotspot/normal/eclipse")


def _arch(ssh: SSHManager, server: Server) -> str:
    res = ssh.exec(server, "uname -m", timeout=15)
    machine = res.stdout.strip()
    return "aarch64" if machine in ("aarch64", "arm64") else "x64"


def install_temurin(ssh: SSHManager, server: Server, major: int,
                    log=None) -> str:
    """Install an Eclipse Temurin JDK into ~/.mcmanager/jdk/<major> - no
    root, no sudoers changes.  Returns the absolute java path."""
    arch = _arch(ssh, server)
    url = TEMURIN_API.format(major=major, arch=arch)
    base = f"$HOME/.mcmanager/jdk/jdk{major}"
    if log:
        log(f"[java] downloading Temurin JDK {major} ({arch}) - no root needed...")
    script = (
        f"mkdir -p {base} && cd {base} && "
        f"curl -fL --retry 3 -o jdk.tar.gz {shq(url)} && "
        "tar -xzf jdk.tar.gz -C . --strip-components=1 && rm -f jdk.tar.gz && "
        "[ -x bin/java ] && echo OK"
    )
    res = ssh.exec(server, script, timeout=900)
    if res.exit_code != 0 or "OK" not in res.stdout:
        tail = (res.stderr or res.stdout).strip()[-400:]
        raise RuntimeError(f"Temurin JDK {major} download failed. {tail}")
    res2 = ssh.exec(server, f"cd {base} && printf '%s/bin/java' \"$PWD\"",
                    timeout=15)
    java_path = res2.stdout.strip()
    if not java_path.startswith("/"):
        raise RuntimeError("Could not resolve the downloaded java path.")
    if log:
        log(f"[java] Temurin JDK {major} installed at "
            f"{java_path.rsplit('/bin', 1)[0]}")
    return java_path


def install_via_package_manager(ssh: SSHManager, server: Server,
                                probe_pkg: str, major: int, log=None) -> None:
    """Fallback: distro package manager. Uses sudo -S with the password
    piped - NOPASSWD entries are NOT required (issue 4)."""
    if probe_pkg == "apt-get":
        script = f"apt-get update -y && apt-get install -y openjdk-{major}-jre-headless"
    elif probe_pkg in ("dnf", "yum"):
        script = f"{probe_pkg} install -y java-{major}-openjdk-headless"
    elif probe_pkg == "apk":
        script = f"apk add openjdk{major}-jre"
    else:
        raise RuntimeError("No known package manager found.")
    if log:
        log(f"[java] trying {probe_pkg} (sudo password is piped, no "
            "NOPASSWD needed)...")
    res = ssh.exec(server, sudo_stdin(server.password, script), timeout=900)
    if res.exit_code != 0:
        tail = (res.stderr or res.stdout).replace(server.password, "***")
        tail = tail.strip()[-600:]
        raise RuntimeError(
            "Package-manager Java install failed. The app will NOT ask for "
            "'NOPASSWD: ALL'. Two safe options:\n"
            "1) let the app install a user-local Temurin JDK (no root), or\n"
            f"2) {SCOPED_SUDOERS_HINT}\nDetails:\n{tail}")


def ensure_java(ssh: SSHManager, server: Server, mc_version: str,
                probe_pkg: str = "", log=None) -> str:
    """Return an absolute java path suitable for the server version.

    Order: per-server override -> best installed JVM -> Temurin user
    install -> distro package.  Never touches the system default java.
    """
    if server.java_path and _java_works(ssh, server, server.java_path):
        return server.java_path

    required = java_for_server(server)
    javas = discover_javas(ssh, server)
    if log:
        found = ", ".join(f"Java {j['major']}" for j in javas[:6]) or "none"
        log(f"[java] MC {mc_version or '?'} needs Java {required} - "
            f"installed: {found}")
    best = pick_java(javas, required)
    if best is not None:
        if log:
            log(f"[java] using {best['path']} ({best['raw']})")
        server.java_path = best["path"]
        return best["path"]

    try:
        path = install_temurin(ssh, server, required, log)
        server.java_path = path
        return path
    except Exception as exc:  # noqa: BLE001 - fall back to distro packages
        if log:
            log(f"[java] Temurin unavailable ({str(exc)[:120]}) - "
                "trying the distro package manager...")
    if probe_pkg:
        install_via_package_manager(ssh, server, probe_pkg, required, log)
        javas = discover_javas(ssh, server)
        best = pick_java(javas, required)
        if best is not None:
            server.java_path = best["path"]
            return best["path"]
    raise RuntimeError(
        f"No Java {required}+ runtime could be provisioned for MC "
        f"{mc_version}. Install Temurin JDK {required} on the server or "
        "set a custom java path in the server's policy settings.")


def _java_works(ssh: SSHManager, server: Server, path: str) -> bool:
    res = ssh.exec(server, f"{shq(path)} -version 2>&1 | head -1", timeout=20)
    return res.exit_code == 0 and "version" in res.stdout
