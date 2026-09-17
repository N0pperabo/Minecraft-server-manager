"""Detect existing Minecraft server installations ANYWHERE on the remote
Linux machine - no fixed path - and adopt them so Control, Console,
Terminal, Files, Monitor and plugin/mod installs all work on that folder.

Two passes (both path-independent):

1) FAST pass (~1s): find every running `java` process, read its working
   directory from /proc/PID/cwd and walk the parent chain to discover the
   GNU screen session it lives in. This finds running servers no matter
   where they are on disk AND gives us the session name so the app can
   attach to it automatically.

2) DEEP pass: `find /` across the ENTIRE filesystem (virtual dirs like
   /proc, /sys, /dev are pruned, plus /var/lib/docker, caches...), looking
   for Minecraft marker files (server.properties, eula.txt, bukkit.yml...)
   and every known server-jar naming scheme: Paper, Purpur, Folia,
   Pufferfish, Airplane, Spigot, CraftBukkit, Bukkit, Fabric, Quilt, Forge,
   NeoForge, Vanilla, Mohist, Arclight, Banner, CatServer, Magma,
   Cardboard, Sponge, BungeeCord, Waterfall, Velocity...
"""
from __future__ import annotations

import re

from .models import Server
from .ssh_manager import SSHManager, shq

# ------------------------------------------------------------------ find ---
# prune virtual / irrelevant trees, then look for MC markers + server jars
_PRUNE = (
    "\\( -path /proc -o -path /sys -o -path /dev -o -path /run -o -path /snap"
    " -o -path /boot -o -path /etc -o -path /var/lib/docker -o -path /var/cache"
    " -o -path /var/log -o -path /var/tmp -o -path /tmp -o -path /usr/lib"
    " -o -path /usr/lib32 -o -path /usr/lib64 -o -path /usr/share"
    " -o -path /usr/src -o -path /usr/include -o -path /usr/bin"
    " -o -path /usr/sbin -o -name node_modules -o -name .cache"
    " -o -name .git -o -name .npm -o -name .nvm \\) -prune"
)
_JARS = (
    "-iname 'server.jar' -o -iname '*paper*.jar' -o -iname '*purpur*.jar'"
    " -o -iname '*folia*.jar' -o -iname '*pufferfish*.jar' -o -iname '*airplane*.jar'"
    " -o -iname '*spigot*.jar' -o -iname '*craftbukkit*.jar' -o -iname '*bukkit*.jar'"
    " -o -iname '*fabric*.jar' -o -iname '*quilt*.jar' -o -iname '*neoforge*.jar'"
    " -o -iname '*forge*.jar' -o -iname 'minecraft_server*.jar'"
    " -o -iname '*mohist*.jar' -o -iname '*arclight*.jar' -o -iname '*banner*.jar'"
    " -o -iname '*catserver*.jar' -o -iname '*magma*.jar' -o -iname '*cardboard*.jar'"
    " -o -iname '*sponge*.jar' -o -iname '*bungeecord*.jar' -o -iname '*bungee*.jar'"
    " -o -iname '*waterfall*.jar' -o -iname '*velocity*.jar'"
)
_MARKERS = (
    "-iname 'server.properties' -o -iname 'eula.txt' -o -iname 'bukkit.yml'"
    " -o -iname 'spigot.yml' -o -iname 'paper.yml' -o -iname 'paper-global.yml'"
    " -o -iname 'user_jvm_args.txt' -o -iname 'spongeboot.txt' -o -iname 'ops.json'"
    " -o -iname 'whitelist.json' -o -iname 'banned-players.json'"
    " -o -iname 'velocity.toml'"
)
FIND_CMD = (
    "nice -n 19 timeout 200 find / " + _PRUNE +
    " -o -type f \\( " + _JARS + " -o " + _MARKERS +
    " \\) -print 2>/dev/null | head -150"
)

# jar basename -> platform (specific names first)
JAR_PLATFORMS = [
    ("paper", "paper"), ("purpur", "purpur"), ("folia", "folia"),
    ("pufferfish", "pufferfish"), ("airplane", "paper"),
    ("spigot", "spigot"), ("craftbukkit", "bukkit"), ("bukkit", "bukkit"),
    ("fabric", "fabric"), ("quilt", "quilt"), ("neoforge", "neoforge"),
    ("forge", "forge"), ("mohist", "mohist"), ("arclight", "arclight"),
    ("banner", "banner"), ("catserver", "catserver"), ("magma", "magma"),
    ("cardboard", "bukkit"), ("sponge", "sponge"),
    ("bungeecord", "bungeecord"), ("bungee", "bungeecord"),
    ("waterfall", "waterfall"), ("velocity", "velocity"),
    ("minecraft_server", "vanilla"), ("server", "vanilla"),
]
MC_JAR_RE = re.compile(
    r"(?i)(paper|purpur|folia|pufferfish|airplane|spigot|craftbukkit|bukkit|"
    r"fabric|quilt|neoforge|forge|mohist|arclight|banner|catserver|magma|"
    r"cardboard|sponge|bungee|velocity|waterfall|minecraft_server|server)"
    r"[^/]*\.jar$")

VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")

# ------------------------------------------------------- pass 1: processes --
PROC_SCRIPT = (
    "pgrep -x java 2>/dev/null | while IFS= read -r p; do "
    'cwd=$(readlink "/proc/$p/cwd" 2>/dev/null); '
    '[ -n "$cwd" ] || continue; '
    'args=$(tr "\\0" " " < "/proc/$p/cmdline" 2>/dev/null); '
    "jar=$(printf '%s' \"$args\" | grep -oE '[A-Za-z0-9_./@-]+\\.jar' | tail -1); "
    "par=$p; scr=''; "
    "for i in 1 2 3 4 5 6 7 8; do "
    'par=$(ps -o ppid= -p "$par" 2>/dev/null | tr -d " "); '
    '[ -n "$par" ] && [ "$par" != "0" ] && [ "$par" != "1" ] || break; '
    'if ps -o comm= -p "$par" 2>/dev/null | grep -q "^screen$"; then scr=$par; break; fi; '
    "done; "
    "nm=''; "
    'if [ -n "$scr" ]; then '
    'nm=$(ls /var/run/screen/S-$(id -u) 2>/dev/null | grep "^${scr}\\." '
    "| head -1 | cut -d. -f2-); "
    "fi; "
    'echo "PROC|$p|$jar|$cwd|$nm"; '
    "done"
)


def scan_processes(ssh: SSHManager, server: Server) -> list[dict]:
    """Running MC-server java processes: dir, jar, pid, screen session."""
    res = ssh.exec(server, PROC_SCRIPT, timeout=40)
    out: list[dict] = []
    for line in res.stdout.splitlines():
        parts = line.split("|", 4)
        if len(parts) == 5 and parts[0] == "PROC":
            out.append({
                "pid": parts[1].strip(),
                "jar": parts[2].strip(),
                "cwd": parts[3].strip(),
                "screen_name": parts[4].strip(),
            })
    return out


# ----------------------------------------------------- pass 2: whole disk ---
def deep_scan_dirs(ssh: SSHManager, server: Server) -> list[str]:
    """Run find across the whole filesystem, return unique parent dirs."""
    res = ssh.exec(server, FIND_CMD, timeout=260)
    dirs: list[str] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line.startswith("/"):
            continue
        d = line.rsplit("/", 1)[0] or "/"
        if d not in dirs:
            dirs.append(d)
    return dirs


# ------------------------------------------------------------ classify -----
CLASSIFY_HEAD = (
    "while IFS= read -r d; do "
    '[ -d "$d" ] || continue; '
    'cd "$d" 2>/dev/null || continue; '
    'echo "===D|$d"; '
    "ver=; tok=; "
    'echo "J|$(ls -1 *.jar 2>/dev/null | head -8 | tr "\\n" ";")"; '
    "m=0; "
    "for f in server.properties eula.txt logs world world_nether world_the_end"
    " mods plugins bukkit.yml spigot.yml paper.yml config user_jvm_args.txt"
    " run.sh start.sh .fabric velocity.toml"
    " libraries/net/minecraftforge libraries/net/neoforged spongeboot.txt"
    " permissions.yml ops.json whitelist.json; do "
    '[ -e "$f" ] && m=$((m+1)); done; '
    'echo "M|$m"; '
    "if [ -f logs/latest.log ]; then "
    'log=$(head -c 40000 logs/latest.log); '
    "tok=$(printf '%s' \"$log\" | grep -m1 -oiE"
    " 'paper|purpur|folia|spigot|bukkit|fabric|quilt|neoforge|forge|mohist|arclight'"
    " || true); "
    'echo "T|$(printf \'%s\' "$tok" | tr "[:upper:]" "[:lower:]")"; '
    "ver=$(printf '%s' \"$log\" | grep -m1 -oE"
    " 'Starting minecraft server version [0-9.]+' | grep -oE '[0-9.]+' | head -1); "
    "if [ -z \"$ver\" ]; then ver=$(printf '%s' \"$log\" | grep -m1 -oE"
    " 'Loading Minecraft [0-9.]+' | grep -oE '[0-9.]+' | head -1); fi; "
    "if [ -z \"$ver\" ]; then ver=$(printf '%s' \"$log\" | grep -m1 -oiE"
    " 'for (MC|Minecraft) [0-9.]+' | grep -oE '[0-9.]+' | head -1); fi; "
    'echo "V|$ver"; '
    "fi; "
    "if [ -z \"$ver\" ]; then "
    "jj=$(ls -1 *.jar 2>/dev/null | grep -iv installer | head -1); "
    "if [ -n \"$jj\" ]; then ver=$(unzip -p \"$jj\" version.json 2>/dev/null "
    "| head -c 200 | grep -oE '[0-9]+([.][0-9]+)+' | head -1); fi; fi; "
    "if [ -n \"$ver\" ]; then echo \"V|$ver\"; fi; "
    "if [ -d .fabric ] || [ -f fabric-server-launch.jar ]; then echo 'T|fabric'; fi; "
    "if [ -d libraries/net/minecraftforge ] || [ -f user_jvm_args.txt ]; "
    "then echo 'T|forge'; fi; "
    "if [ -d libraries/net/neoforged ]; then echo 'T|neoforge'; fi; "
    "if [ -f server.properties ]; then "
    'echo "SP|yes"; '
    'echo "P|$(grep -m1 \'^server-port=\' server.properties | cut -d= -f2'
    ' | tr -d \'[:space:]\')"; '
    'echo "RC|$(grep -m1 \'^enable-rcon=\' server.properties | cut -d= -f2'
    ' | tr -d \'[:space:]\')"; '
    'echo "RP|$(grep -m1 \'^rcon.port=\' server.properties | cut -d= -f2'
    ' | tr -d \'[:space:]\')"; '
    'echo "RW|$(grep -m1 \'^rcon.password=\' server.properties | cut -d= -f2'
    ' | tr -d \'[:space:]\')"; '
    "fi; "
    "if [ -f eula.txt ] && grep -qi 'eula=true' eula.txt; then echo 'E|yes';"
    " else echo 'E|no'; fi; "
    "[ -f start.sh ] && echo 'S|yes'; "
    "[ -f run.sh ] && echo 'R|yes'; "
    "true; "
    "done <<'__MCM_DIRS__'\n"
)


def classify_dirs(ssh: SSHManager, server: Server, dirs: list[str]) -> list[dict]:
    """Classify many directories with ONE ssh exec (fast, no round-trips)."""
    if not dirs:
        return []
    script = CLASSIFY_HEAD + "\n".join(dirs[:30]) + "\n__MCM_DIRS__"
    res = ssh.exec(server, script, timeout=120)
    results: list[dict] = []
    cur: dict | None = None

    def blank(d: str) -> dict:
        return {"dir": d, "jars": [], "tokens": [], "version": "", "port": "25565",
                "eula_ok": False, "has_start": False, "has_run": False,
                "sp": False, "rcon": False, "rcon_port": 0, "rcon_pass": ""}

    for line in res.stdout.splitlines():
        if line.startswith("===D|"):
            cur = blank(line[5:].strip())
            results.append(cur)
            continue
        if cur is None or "|" not in line or line.startswith("==="):
            continue
        key, _, val = line.partition("|")
        key, val = key.strip(), val.strip()
        if key == "J" and val:
            cur["jars"] = [j for j in val.split(";") if j]
        elif key == "T" and val and val not in cur["tokens"]:
            cur["tokens"].append(val)
        elif key == "V" and val and not cur["version"]:
            cur["version"] = val
        elif key == "P" and val.isdigit():
            cur["port"] = val
        elif key == "M":
            cur["markers"] = int(val or "0")
        elif key == "SP":
            cur["sp"] = val == "yes"
        elif key == "E":
            cur["eula_ok"] = val == "yes"
        elif key == "S":
            cur["has_start"] = val == "yes"
        elif key == "R":
            cur["has_run"] = val == "yes"
        elif key == "RC":
            cur["rcon"] = val.lower() == "true"
        elif key == "RP" and val.isdigit():
            cur["rcon_port"] = int(val)
        elif key == "RW":
            cur["rcon_pass"] = val
    return [r for r in results if _finalize(r)]


def _finalize(info: dict) -> bool:
    """Resolve platform / version; return True if this looks like an MC server."""
    jar_list = info["jars"]
    platform = ""
    for prefix, plat in JAR_PLATFORMS:
        for j in jar_list:
            if j.lower().startswith(prefix) and "installer" not in j.lower():
                platform = plat
                break
        if platform:
            break
    if not platform:  # log-based (renamed jars), then structural hints
        if info["tokens"]:
            platform = info["tokens"][0]
        elif info.get("has_run") and any("forge" in j.lower() or "neoforge" in j.lower()
                                         for j in jar_list):
            platform = "forge"
    if not platform and info.get("sp"):
        platform = "vanilla"  # server.properties exists => definitely an MC server
    if not platform and jar_list and int(info.get("markers") or 0) >= 3:
        platform = "vanilla"  # renamed jar + several MC-structural files
    if not platform:
        return False
    if not info["version"]:
        for j in jar_list:  # paper-1.21.4-232.jar -> 1.21.4
            m = VERSION_RE.search(j)
            if m and not m.group(1).startswith("0."):
                info["version"] = m.group(1)
                break
    info["platform"] = platform
    return True


# --------------------------------------------------------------- top level --
def run_detection(ssh: SSHManager, server: Server) -> list[dict]:
    """Search the machine (processes + whole filesystem) and classify every
    directory that looks like an MC server. Running servers come first."""
    found: dict[str, dict] = {}
    order: list[str] = []

    # pass 1 - running java processes (fast, path-independent)
    try:
        procs = scan_processes(ssh, server)
    except Exception:  # noqa: BLE001
        procs = []
    for p in procs:
        d = p["cwd"]
        if not d or d in found:
            continue
        cands = classify_dirs(ssh, server, [d])
        info = cands[0] if cands else None
        if info is None:
            if not MC_JAR_RE.search(p["jar"] or ""):
                continue  # unrelated java app (jenkins, elastic...)
            info = {"dir": d, "jars": [p["jar"].rsplit("/", 1)[-1]],
                    "tokens": [], "version": "", "port": "25565",
                    "eula_ok": False, "has_start": False, "has_run": False,
                    "sp": False, "rcon": False, "rcon_port": 0, "rcon_pass": ""}
            if not _finalize(info):
                continue
        info["running"] = True
        info["pid"] = p["pid"]
        info["screen_name"] = p["screen_name"]
        found[d] = info
        order.append(d)

    # pass 2 - the entire filesystem
    try:
        dirs = deep_scan_dirs(ssh, server)
    except Exception:  # noqa: BLE001
        dirs = []
    pending = [d for d in dirs if d not in found]
    if pending:
        for info in classify_dirs(ssh, server, pending):
            d = info["dir"]
            if d in found:
                continue
            info["running"] = False
            info["pid"] = ""
            info["screen_name"] = ""
            found[d] = info
            order.append(d)

    cands = [found[d] for d in order]
    cands.sort(key=lambda c: (not c.get("running"), c["dir"]))
    return cands[:14]


# ------------------------------------------------------------------ adopt ---
def refresh_server_info(ssh: SSHManager, server: Server) -> dict | None:
    """Re-check the linked folder and refresh platform + the RUNNING
    Minecraft version (1.21.11, 26.2, snapshots...) automatically - the
    app figures the version out by itself from logs / jar metadata, the
    user never types it. Also re-hooks the screen session when the server
    is running and none is stored yet."""
    if not server.mc_dir:
        return None
    infos = classify_dirs(ssh, server, [server.mc_dir])
    if not infos:
        return None
    info = infos[0]
    if info.get("platform"):
        server.platform = info["platform"]
    if info.get("version"):
        server.mc_version = info["version"]
    if not server.external_screen:
        try:
            for p in scan_processes(ssh, server):
                if p.get("cwd") == server.mc_dir and p.get("screen_name"):
                    server.external_screen = p["screen_name"]
                    info["screen_name"] = p["screen_name"]
                    break
        except Exception:  # noqa: BLE001
            pass
    return info


def _main_jar(jars: list[str], platform: str) -> str:
    """Pick the jar start.sh should launch."""
    prefer = {"paper": "paper", "purpur": "purpur", "folia": "folia",
              "pufferfish": "pufferfish", "spigot": "spigot",
              "bukkit": "craftbukkit", "fabric": "fabric", "quilt": "quilt",
              "neoforge": "neoforge", "forge": "forge", "mohist": "mohist",
              "arclight": "arclight", "banner": "banner",
              "catserver": "catserver", "magma": "magma",
              "sponge": "sponge", "bungeecord": "bungee",
              "waterfall": "waterfall", "velocity": "velocity",
              "vanilla": ("minecraft_server", "server")}.get(platform, ())
    if isinstance(prefer, str):
        prefer = (prefer,)
    for p in prefer:
        for j in jars:
            if j.lower().startswith(p) and "installer" not in j.lower():
                return j
    for j in jars:  # anything that is not an installer / sources jar
        if "installer" not in j.lower() and "sources" not in j.lower():
            return j
    return jars[0] if jars else ""


def adopt(ssh: SSHManager, server: Server, cand: dict, ram_mb: int) -> str:
    """Point the saved server at an existing install, hook the running
    screen session (if any) and set up start.sh / eula when missing."""
    server.platform = cand.get("platform") or server.platform
    server.mc_version = cand.get("version") or server.mc_version
    server.mc_dir = cand["dir"]
    server.ram_mb = max(1024, ram_mb)
    d = shq(cand["dir"])
    notes: list[str] = []

    # screen session of a server that is already running
    scr = (cand.get("screen_name") or "").strip()
    if cand.get("running") and scr:
        server.external_screen = scr
        notes.append(f"Attached to the running screen session '{scr}' - "
                     "console commands will be sent there.")
    else:
        server.external_screen = ""

    # RCON fallback channel
    if cand.get("rcon") and cand.get("rcon_port"):
        server.rcon_port = int(cand["rcon_port"])
        server.rcon_pass = cand.get("rcon_pass", "")
        if server.rcon_pass:
            notes.append(f"RCON detected on port {server.rcon_port} - "
                         "can also send commands through it.")
        else:
            notes.append("RCON is enabled but rcon.password is empty - "
                         "set it in server.properties for command sending.")
    else:
        server.rcon_port = 0
        server.rcon_pass = ""

    # eula
    if not cand.get("eula_ok"):
        ssh.exec(server, f"cd {d} && echo 'eula=true' > eula.txt", timeout=20)
        notes.append("eula.txt was missing/not accepted - created it.")

    # start.sh
    jars: list[str] = cand.get("jars") or []
    if cand.get("has_start"):
        notes.append("Existing start.sh will be used as-is.")
    elif cand.get("platform") == "forge" and cand.get("has_run"):
        ssh.exec(
            server,
            f"cd {d} && printf '#!/usr/bin/env bash\\ncd \"$(dirname \"$0\")\"\\n"
            "exec bash run.sh\\n' > start.sh && chmod +x start.sh",
            timeout=20,
        )
        notes.append("start.sh created to launch Forge's run.sh.")
    elif jars:
        jar = _main_jar(jars, cand.get("platform", ""))
        ssh.exec(
            server,
            f"cd {d} && printf '#!/usr/bin/env bash\\ncd \"$(dirname \"$0\")\"\\n"
            f"exec java -Xms512M -Xmx{server.ram_mb}M -jar {shq(jar)} nogui\\n' "
            "> start.sh && chmod +x start.sh",
            timeout=20,
        )
        notes.append(f"start.sh created for {jar} with {server.ram_mb} MB RAM.")
    elif server.external_screen:
        notes.append("No start script needed - server is already running.")
    else:
        notes.append("No server jar found in this folder - use the Setup "
                     "Wizard if you need to (re)install one.")

    if cand.get("platform") == "forge" and not cand.get("has_start") and jars:
        jvm = f"{d}/user_jvm_args.txt"
        ssh.exec(
            server,
            f"grep -q '^-Xmx' {jvm} 2>/dev/null || echo '-Xmx{server.ram_mb}M' >> {jvm}",
            timeout=15,
        )

    if not notes:
        notes.append("Installation linked.")
    return " ".join(notes)
