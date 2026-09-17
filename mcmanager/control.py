"""Server lifecycle control: start/stop/restart via GNU screen, console tail,
command sending and live monitor stats.

For ADOPTED (imported) servers the control layer automatically uses:
- the screen session the server was already running in (external_screen), or
- RCON over an SSH tunnel (enable-rcon in server.properties), or
- a polite SIGTERM (java treats it as a graceful stop) as last resort.
"""
from __future__ import annotations

import posixpath
import re
import secrets
import struct
import time

from .models import Server
from .ssh_manager import SSHManager, shq

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# ---------------------------------------------------------------- RCON -----
def _rcon_pkt(rid: int, ptype: int, payload: bytes) -> bytes:
    body = struct.pack("<ii", rid, ptype) + payload + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


def _rcon_recv(chan) -> tuple[int, int, str]:
    buf = b""
    while len(buf) < 4:
        chunk = chan.recv(4 - len(buf))
        if not chunk:
            raise RuntimeError("RCON connection closed.")
        buf += chunk
    (length,) = struct.unpack("<i", buf)
    body = b""
    while len(body) < length:
        chunk = chan.recv(length - len(body))
        if not chunk:
            raise RuntimeError("RCON connection closed.")
        body += chunk
    rid, ptype = struct.unpack("<ii", body[:8])
    return rid, ptype, body[8:-2].decode("utf-8", "replace")


class ServerControl:
    def __init__(self, ssh: SSHManager) -> None:
        self.ssh = ssh
        self.console_offsets: dict[str, int] = {}

    # ----------------------------------------------------------- channels --
    def _screen_map(self, server: Server) -> list[tuple[str, str]]:
        """All GNU screen sessions as (pid, name) pairs."""
        res = self.ssh.exec(server, "screen -ls 2>/dev/null; true", timeout=20)
        pairs: list[tuple[str, str]] = []
        for ln in res.stdout.splitlines():
            m = re.match(r"\s*(\d+)\.([^\s]+)", ln)
            if m:
                pairs.append((m.group(1), m.group(2)))
        return pairs

    def _screen_names(self, server: Server) -> list[str]:
        return [name for _pid, name in self._screen_map(server)]

    def _screen_ancestor_name(self, server: Server, java_pid: str,
                              pairs: list[tuple[str, str]]) -> str:
        """Walk the java process up the parent chain; if a GNU screen
        process is found on the way, return that session's name."""
        script = (
            f"par={java_pid}; "
            "for i in 1 2 3 4 5 6 7 8; do "
            'par=$(ps -o ppid= -p "$par" 2>/dev/null | tr -d " "); '
            '[ -n "$par" ] && [ "$par" -gt 1 ] 2>/dev/null || break; '
            'echo "$par"; done'
        )
        res = self.ssh.exec(server, script, timeout=20)
        ancestors = set(res.stdout.split())
        for spid, name in pairs:
            if spid in ancestors:
                return name
        return ""

    def _resolve_screen(self, server: Server) -> str:
        """Best-matching screen session for this server, whatever its name
        is and wherever the server was started - never a fixed path."""
        pairs = self._screen_map(server)
        names = [n for _p, n in pairs]
        for cand in (server.external_screen, server.screen_tag):
            if cand and cand in names:
                return cand
        for n in names:  # name drift: keep the app-issued prefix
            for cand in (server.external_screen, server.screen_tag):
                if cand and n.startswith(cand):
                    return n
        pid = self.mc_pid(server) if server.mc_dir else ""
        if pid:
            owner = self._screen_ancestor_name(server, pid, pairs)
            if owner:
                return owner
            if len(names) == 1:
                return names[0]  # only one screen session and our java runs
        return ""

    def mc_pid(self, server: Server) -> str:
        """PID of the java process whose cwd is the server dir ('' if none)."""
        if not server.mc_dir:
            return ""
        script = (
            f"for p in $(pgrep -x java 2>/dev/null); do "
            f"c=$(readlink /proc/$p/cwd 2>/dev/null); "
            f"[ \"$c\" = {shq(server.mc_dir)} ] && echo $p && break; done"
        )
        res = self.ssh.exec(server, script, timeout=25)
        out = res.stdout.strip()
        return out.splitlines()[0] if out else ""

    def _rcon(self, server: Server, command: str) -> str:
        if not server.rcon_port:
            raise RuntimeError("RCON is not configured on this server.")
        client = self.ssh.connect(server)
        try:
            chan = client.get_transport().open_channel(
                "direct-tcpip", ("127.0.0.1", int(server.rcon_port)),
                ("127.0.0.1", 0))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "RCON tunnel failed - is the server running with "
                "enable-rcon=true in server.properties?") from exc
        chan.settimeout(12)
        try:
            rid = 0x1CE5
            chan.sendall(_rcon_pkt(rid, 3, server.rcon_pass.encode()))
            r, _, _ = _rcon_recv(chan)
            if r == -1:
                raise RuntimeError("RCON authentication failed (rcon.password).")
            chan.sendall(_rcon_pkt(rid + 1, 2, command.encode()))
            _, _, payload = _rcon_recv(chan)
            return payload
        finally:
            try:
                chan.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- core --
    def status(self, server: Server) -> bool:
        """Running = our screen OR the adopted external screen OR a java
        process whose working directory is the server folder."""
        names = self._screen_names(server)
        if server.external_screen and server.external_screen in names:
            return True
        if server.screen_tag in names:
            return True
        if server.mc_dir and self.mc_pid(server):
            return True
        return False

    def start(self, server: Server, log=None) -> None:
        if not server.mc_dir:
            raise RuntimeError("No install folder linked - run the Setup "
                               "Wizard or 'Find existing installs' first.")
        if self.status(server):
            raise RuntimeError("The server is already running.")
        if log:
            log(f"[start] launching screen session '{server.screen_tag}' in {server.mc_dir}")
        script = (
            f"cd {shq(server.mc_dir)} && "
            "if [ -f start.sh ]; then "
            f"screen -dmS {server.screen_tag} bash start.sh; "
            "elif [ -f run.sh ]; then "
            f"screen -dmS {server.screen_tag} bash run.sh; "
            "else echo 'NO_START_SCRIPT: no start.sh or run.sh in this folder'; exit 1; fi; "
            f"sleep 1 && screen -ls | grep '\\.{server.screen_tag}[[:space:]]'"
        )
        res = self.ssh.exec(server, script, timeout=30)
        if res.exit_code != 0:
            raise RuntimeError(res.stderr.strip() or res.stdout.strip()
                               or "start.sh failed - check the directory.")
        if log:
            log("[start] session is up - attach with Console or Terminal.")

    def stop(self, server: Server, log=None) -> None:
        pid = self.mc_pid(server)
        stopped = False
        try:
            self.send_command(server, "stop")
            stopped = True
            if log:
                log("[stop] 'stop' sent to the console; waiting up to 60s for shutdown...")
        except RuntimeError as exc:
            if pid:
                self.ssh.exec(server, f"kill -TERM {pid}", timeout=15)
                stopped = True
                if log:
                    log("[stop] no screen/RCON channel - SIGTERM sent to the "
                        "java process (graceful shutdown)...")
            else:
                if log:
                    log(f"[stop] {exc}")
        if not stopped:
            return
        for _ in range(20):
            time.sleep(3)
            if not self.status(server):
                if log:
                    log("[stop] server stopped cleanly.")
                return
        for t in {server.screen_tag, server.external_screen} - {""}:
            self.ssh.exec(server, f"screen -S {shq(t)} -X quit; true", timeout=15)
        time.sleep(2)
        if self.status(server) and log:
            log("[stop] still running - stop it from the Terminal page.")

    def restart(self, server: Server, log=None) -> None:
        if self.status(server):
            self.stop(server, log)
        self.start(server, log)

    # --------------------------------------------------------------- console
    def console_init_offset(self, server: Server) -> int:
        res = self.ssh.exec(
            server,
            f"f={shq(server.mc_dir)}/logs/latest.log; "
            "if [ -f \"$f\" ]; then stat -c %s \"$f\"; else echo 0; fi",
            timeout=15,
        )
        try:
            size = int(res.stdout.strip() or "0")
        except ValueError:
            size = 0
        offset = max(0, size - 6000)
        self.console_offsets[server.id] = offset
        return offset

    def console_poll(self, server: Server) -> list[str]:
        """Return new console lines since the last poll (and update offset)."""
        offset = self.console_offsets.get(server.id, 0)
        res = self.ssh.exec(
            server,
            f"f={shq(server.mc_dir)}/logs/latest.log; "
            f"if [ -f \"$f\" ]; then tail -c +{offset + 1} \"$f\" | head -c 200000; "
            "s=$(stat -c %s \"$f\"); else s=" + str(offset) + "; fi; "
            "printf '\\n@@%s' \"$s\"",
            timeout=20,
        )
        out = res.stdout
        if "@@" not in out:
            return []
        body, _, marker = out.rpartition("@@")
        try:
            self.console_offsets[server.id] = int(marker.strip() or offset)
        except ValueError:
            pass
        lines = [ANSI_RE.sub("", ln) for ln in body.splitlines()]
        return [ln for ln in lines if ln.strip()]

    # ------------------------------------------------------------ send keys
    @staticmethod
    def _stuff_eval(target: str, command: str, with_p: bool) -> str:
        """POSIX-safe screen keystuffing: screen itself parses ^M as Enter,
        so this works under bash / dash / zsh / fish login shells alike."""
        esc = command.replace("\\", "\\\\").replace('"', '\\"')
        p = "-p 0 " if with_p else ""
        return (f"screen -S {shq(target)} {p}-X eval "
                f"'stuff \"{esc}\"' 'stuff \"^M\"'")

    def _send_screen(self, server: Server, target: str, command: str) -> bool:
        # 1) works on every login shell (screen interprets ^M)
        res = self.ssh.exec(server, self._stuff_eval(target, command, True),
                            timeout=15)
        if res.exit_code == 0:
            return True
        # 2) ANSI-C newline form (bash/zsh shells)
        safe = command.replace("'", "'\\''")
        res = self.ssh.exec(
            server, f"screen -S {shq(target)} -p 0 -X stuff $'{safe}\\n'",
            timeout=15)
        if res.exit_code == 0:
            return True
        # 3) older screen builds without -p on -X
        res = self.ssh.exec(server, self._stuff_eval(target, command, False),
                            timeout=15)
        return res.exit_code == 0

    def _send_tmux(self, server: Server, command: str) -> bool:
        res = self.ssh.exec(
            server, "tmux list-sessions -F '#S' 2>/dev/null; true", timeout=15)
        sessions = [s.strip() for s in res.stdout.splitlines() if s.strip()]
        if not sessions:
            return False
        target = ""
        for cand in (server.external_screen, server.screen_tag):
            if cand in sessions:
                target = cand
                break
        if not target and len(sessions) == 1 and self.mc_pid(server):
            target = sessions[0]
        if not target:
            return False
        res = self.ssh.exec(
            server, f"tmux send-keys -t {shq(target)} -- {shq(command)} Enter",
            timeout=15)
        return res.exit_code == 0

    def send_command(self, server: Server, command: str) -> str:
        """Deliver a console command and return the channel used.
        Order: any matching screen session (found dynamically - the server
        may have been started outside the app, with any session name) ->
        tmux -> RCON."""
        if not command:
            return ""
        target = self._resolve_screen(server)
        if target:
            if self._send_screen(server, target, command):
                if server.external_screen != target:
                    server.external_screen = target  # remember what worked
                return f"screen session '{target}'"
        if self._send_tmux(server, command):
            return "tmux session"
        if server.rcon_port:
            self._rcon(server, command)
            return f"RCON port {server.rcon_port}"
        raise RuntimeError(
            "No console channel found - no screen/tmux session matches this "
            "server and RCON is off. Accept the automatic fix (enables RCON "
            "for you) or start the server from the Control page.")

    # ---------------------------------------------------------------- rcon fix
    def enable_rcon(self, server: Server, log=None) -> str:
        """Write enable-rcon / rcon.port / rcon.password into
        server.properties so the app NEVER asks the user to edit settings
        by hand. Takes effect after the next (re)start."""
        if not server.mc_dir:
            raise RuntimeError("No install folder linked.")
        props = posixpath.join(server.mc_dir, "server.properties")
        res = self.ssh.exec(
            server, f"grep -m1 '^rcon.port=' {shq(props)} 2>/dev/null; true",
            timeout=15)
        cur = res.stdout.strip().split("=", 1)[-1].strip() if "=" in res.stdout else ""
        port = int(cur) if cur.isdigit() else (server.rcon_port or 25575)
        pwd = server.rcon_pass or ("mcm-" + secrets.token_hex(6))
        script = (
            f"f={shq(props)}; touch \"$f\"; "
            "sk() { if grep -q \"^$1=\" \"$f\" 2>/dev/null; then "
            'sed -i "s|^$1=.*|$1=$2|" "$f"; else echo "$1=$2" >> "$f"; fi; }; '
            "sk enable-rcon true; sk rcon.port "
            f"{port}; sk rcon.password {shq(pwd)}; "
            "grep -E '^(enable-rcon|rcon.port|rcon.password)=' \"$f\""
        )
        res = self.ssh.exec(server, script, timeout=20)
        if res.exit_code != 0 or "enable-rcon=true" not in res.stdout:
            detail = (res.stderr or res.stdout).strip()[-160:]
            raise RuntimeError("Could not update server.properties. " + detail)
        server.rcon_port = port
        server.rcon_pass = pwd
        if log:
            log(f"[rcon] enabled on port {port}")
        return f"RCON enabled on port {port}"

    # --------------------------------------------------------------- monitor
    def monitor(self, server: Server) -> dict:
        mc_dir_q = shq(server.mc_dir) if server.mc_dir else "''"
        res = self.ssh.exec(
            server,
            "top -bn1 | grep 'Cpu(s)' | head -1; "
            "free -b | awk '/Mem:/{print $2\":\"$3\":\"$7}'; "
            "df -B1 / | awk 'NR==2{print $2\":\"$3\":\"$4}'; "
            "cat /proc/loadavg; "
            "j=''; "
            f"for p in $(pgrep -x java 2>/dev/null); do "
            f"c=$(readlink /proc/$p/cwd 2>/dev/null); "
            f"[ \"$c\" = {mc_dir_q} ] || continue; "
            "j=$(ps -o etime=,rss= -p $p 2>/dev/null); break; done; "
            "[ -n \"$j\" ] || j=$(ps -eo etime=,rss=,comm | awk '$3==\"java\"{print $1, $2; exit}'); "
            "echo \"$j\"",
            timeout=25,
        )
        lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
        stats = {"cpu": 0.0, "ram_used": 0, "ram_total": 1,
                 "disk_used": 0, "disk_total": 1, "load": 0.0,
                 "mc_uptime": "", "mc_rss_kb": 0}
        try:
            if lines:
                idle = re.search(r"([0-9.]+)\s*id", lines[0])
                if idle:
                    stats["cpu"] = round(max(0.0, 100.0 - float(idle.group(1))), 1)
            if len(lines) > 1:
                total, used, _avail = lines[1].split(":")[:3]
                stats["ram_total"], stats["ram_used"] = int(total), int(used)
            if len(lines) > 2:
                total, used, _free = lines[2].split(":")[:3]
                stats["disk_total"], stats["disk_used"] = int(total), int(used)
            if len(lines) > 3:
                stats["load"] = float(lines[3].split()[0])
            if len(lines) > 4:
                parts = lines[4].split()
                stats["mc_uptime"] = parts[0]
                stats["mc_rss_kb"] = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            pass
        return stats
