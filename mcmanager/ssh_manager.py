"""SSH + SFTP layer built on paramiko (replaces sshj from the Android app).

v2.0 changes:
- HOST KEYS ARE VERIFIED (issue 3): the old AutoAddPolicy accepted any
  key, which silently enabled man-in-the-middle attacks.  We now use a
  TOFU store (~/.mcmanager/known_hosts): first connect records the key
  (the UI may confirm the fingerprint), any later key change REJECTS the
  connection with a clear alarm.
- `exec(..., lock=False)` lets long operations (backup tar, jar download,
  live `tail -F`) run on their own channel while normal commands keep
  flowing - the per-server lock no longer blocks progress polling.
- `exec_channel()` returns a raw channel + stdout file for streaming
  commands (live console) without occupying the lock.
- transport keepalive prevents idle drops during long operations, and a
  dropped transport is transparently re-established on the next call
  (SSH-drop resilience, issue 33).
"""
from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass

import paramiko

from .models import Server
from .secure import HOST_KEYS

CONNECT_TIMEOUT = 12


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


def shq(value: str) -> str:
    """Single-quote a value for safe POSIX shell usage."""
    return "'" + str(value).replace("'", "'\\''") + "'"


class SSHError(Exception):
    pass


class ShellChannel:
    """Interactive shell for the Terminal page."""

    def __init__(self, client: paramiko.SSHClient, on_line) -> None:
        self.channel = client.invoke_shell(term="xterm-256color", width=110, height=32)
        self._on_line = on_line
        self._alive = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        buf = b""
        while self._alive and not self.channel.closed:
            try:
                if self.channel.recv_ready():
                    buf += self.channel.recv(4096)
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._deliver(line)
                else:
                    self.channel.timeout = 0.2
                    self.channel.recv(1)
            except socket.timeout:
                continue
            except (OSError, paramiko.SSHException):
                break
        if buf:
            self._deliver(buf)

    def _deliver(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if text:
            try:
                self._on_line(text)
            except Exception:  # noqa: BLE001
                pass

    def send(self, text: str) -> None:
        if not self.channel.closed:
            self.channel.sendall((text + "\n").encode("utf-8"))

    def close(self) -> None:
        self._alive = False
        try:
            self.channel.close()
        except Exception:  # noqa: BLE001
            pass


class SSHManager:
    """Cached, per-server SSH client with per-server locking."""

    def __init__(self) -> None:
        self._clients: dict[str, paramiko.SSHClient] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock_all = threading.Lock()

    def _lock_for(self, server_id: str) -> threading.Lock:
        with self._lock_all:
            if server_id not in self._locks:
                self._locks[server_id] = threading.Lock()
            return self._locks[server_id]

    def connect(self, server: Server) -> paramiko.SSHClient:
        with self._lock_for(server.id):
            client = self._clients.get(server.id)
            if client is not None:
                transport = client.get_transport()
                if transport is not None and transport.is_active():
                    return client
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._clients.pop(server.id, None)

            client = paramiko.SSHClient()
            # TOFU host-key verification instead of AutoAddPolicy (issue 3)
            HOST_KEYS.attach(client)
            try:
                client.connect(
                    hostname=server.host, port=server.port,
                    username=server.username, password=server.password,
                    timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT,
                    auth_timeout=CONNECT_TIMEOUT,
                    allow_agent=False, look_for_keys=False,
                )
            except paramiko.AuthenticationException as exc:
                raise SSHError("Wrong username or password.") from exc
            except paramiko.BadHostKeyException as exc:
                raise SSHError(
                    f"HOST KEY CHANGED for {server.host}! This can mean a "
                    "man-in-the-middle attack, or that the machine was "
                    "reinstalled. If you are SURE it is legitimate, remove "
                    "the old key: Settings -> Manage trusted hosts."
                ) from exc
            except socket.timeout as exc:
                raise SSHError(f"Connection timed out ({server.host}:{server.port}).") from exc
            except (socket.error, OSError) as exc:
                raise SSHError(f"Cannot reach {server.host}:{server.port} - {exc}") from exc
            except paramiko.SSHException as exc:
                raise SSHError(f"SSH error: {exc}") from exc
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(25)  # survive idle NAT/firewall drops
            self._clients[server.id] = client
            return client

    def close(self, server_id: str) -> None:
        with self._lock_for(server_id):
            client = self._clients.pop(server_id, None)
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    def close_all(self) -> None:
        for sid in list(self._clients):
            self.close(sid)

    # ---- one-shot commands ------------------------------------------------
    def exec(self, server: Server, command: str, timeout: float | None = 60,
             lock: bool = True) -> ExecResult:
        """Run a command and collect stdout/stderr/exit code.

        lock=False: run on a dedicated channel WITHOUT holding the
        per-server lock - used by long-running operations (tar backup,
        jar download) so the UI can poll progress with normal exec()s.
        """
        if lock:
            client = self.connect(server)
            with self._lock_for(server.id):
                return self._exec_on(client, command, timeout)
        client = self.connect(server)  # lock-free long op on its own channel
        return self._exec_on(client, command, timeout)

    def _exec_on(self, client: paramiko.SSHClient, command: str,
                 timeout: float | None) -> ExecResult:
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = 0
        try:
            code = stdout.channel.recv_exit_status()
        except Exception:  # noqa: BLE001
            code = -1
        return ExecResult(code, out, err)

    def exec_stream(self, server: Server, command: str, on_line,
                    timeout: float | None = None) -> int:
        """Run a command, streaming every stdout line to on_line (blocking)."""
        client = self.connect(server)
        with self._lock_for(server.id):
            _stdin, stdout, _stderr = client.exec_command(command, timeout=timeout)
            for raw in iter(stdout.readline, ""):
                line = raw.rstrip("\n")
                if line:
                    on_line(line)
            code = 0
            try:
                code = stdout.channel.recv_exit_status()
            except Exception:  # noqa: BLE001
                code = -1
            return code

    def exec_channel(self, server: Server, command: str,
                     timeout: float | None = None):
        """Open a LONG-RUNNING command on its own channel (no per-server
        lock held).  Returns (stdout_file_object, channel); the caller MUST
        close the channel when done.  Used by the live console stream
        (`tail -F logs/latest.log`)."""
        client = self.connect(server)
        _stdin, stdout, _stderr = client.exec_command(command, timeout=timeout)
        return stdout, stdout.channel

    @contextmanager
    def sftp(self, server: Server):
        client = self.connect(server)
        sftp = client.open_sftp()
        try:
            yield sftp
        finally:
            try:
                sftp.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- helpers -----------------------------------------------------------
    def test(self, server: Server) -> str:
        res = self.exec(server, "echo ok", timeout=20)
        if res.exit_code == 0 and "ok" in res.stdout:
            uname = self.exec(server, "uname -sr", timeout=15).stdout.strip()
            return f"Connected - {uname or 'server reachable'}"
        raise SSHError(res.stderr.strip() or "Command failed after connecting.")

    def shell(self, server: Server, on_line) -> ShellChannel:
        return ShellChannel(self.connect(server), on_line)
