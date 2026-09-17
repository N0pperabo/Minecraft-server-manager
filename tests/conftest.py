"""Test fixtures: a scripted FakeSSH manager + FakeControl.

The FakeSSH.exec() matches handler rules (substring of the command) in
registration order and returns canned ExecResults, so whole flows
(backup, update, restore) can be exercised without a real server.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcmanager.models import Server
from mcmanager.ssh_manager import ExecResult


class FakeChan:
    """Scripted channel: recv() serves bytes from a buffer, then EOF."""

    def __init__(self, script: bytes = b""):
        self.buf = script
        self.closed = False
        self.timeout = 5

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n):
        if not self.buf:
            return b""
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def sendall(self, data):
        pass

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, ssh):
        self.ssh = ssh

    def open_channel(self, kind, addr, src):
        return self.ssh.open_channel(kind, addr)


class FakeClient:
    def __init__(self, ssh):
        self.ssh = ssh

    def get_transport(self):
        return FakeTransport(self.ssh)


class _FakeStdout:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, n):
        out, self._data = self._data[:n], self._data[n:]
        return out


class FakeSSH:
    """Drop-in SSHManager for tests.  Rules match by substring in command
    order; unmatched commands fall through to a tiny in-memory filesystem
    that understands the file commands the app actually issues (printf >,
    cat, rm, ls, mkdir, grep on known files)."""

    def __init__(self):
        self.rules: list[tuple[str, object]] = []
        self.calls: list[str] = []
        self.channels: list[FakeChan] = []
        self.fs: dict[str, str] = {}   # in-memory remote files

    # -- scripting ---------------------------------------------------------
    def on(self, pattern: str, stdout: str = "", code: int = 0,
           stderr: str = ""):
        self.rules.append((pattern, ExecResult(code, stdout, stderr)))

    def on_fn(self, pattern: str, fn):
        self.rules.append((pattern, fn))

    def add_channel(self, script: bytes = b"") -> FakeChan:
        chan = FakeChan(script)
        self.channels.append(chan)
        return chan

    def open_channel(self, kind, addr):
        if self.channels:
            return self.channels.pop(0)
        raise RuntimeError("no scripted channel")

    # -- mini filesystem ------------------------------------------------------
    import re as _re

    _WRITE = _re.compile(r"printf '%s' '(.*)' > '([^']+)'")
    _CAT = _re.compile(r"cat '([^']+)'")
    _RM = _re.compile(r"rm -f '([^']+)'")
    _LS = _re.compile(r"ls -1t '([^']+)'")

    def _fs_exec(self, command: str):
        wrote = self._WRITE.search(command)
        if wrote:
            self.fs[wrote.group(2)] = wrote.group(1)
            return ExecResult(0, "", "")
        if "mkdir -p" in command:
            return ExecResult(0, "", "")
        cat = self._CAT.search(command)
        if cat:
            return ExecResult(0, self.fs.get(cat.group(1), ""), "")
        rm = self._RM.search(command)
        if rm:
            self.fs.pop(rm.group(1), None)
            return ExecResult(0, "", "")
        ls = self._LS.search(command)
        if ls:
            import fnmatch
            hits = sorted(p for p in self.fs if fnmatch.fnmatch(p, ls.group(1)))
            return ExecResult(0, "\n".join(hits[:1]), "")
        return None

    # -- SSHManager API ------------------------------------------------------
    def connect(self, server):
        return FakeClient(self)

    def exec(self, server, command, timeout=None, lock=True):
        self.calls.append(command)
        for pat, res in self.rules:
            if pat in command:
                if callable(res):
                    return res(command)
                return res
        fs_result = self._fs_exec(command)
        if fs_result is not None:
            return fs_result
        return ExecResult(0, "", "")

    def exec_channel(self, server, command, timeout=None):
        return _FakeStdout(b""), FakeChan()

    def close(self, server_id):
        pass

    def close_all(self):
        pass


class FakeControl:
    def __init__(self, running: bool = False):
        self.running = running
        self.starts = 0
        self.stops = 0

    def status(self, server) -> bool:
        return self.running

    def start(self, server, log=None):
        self.starts += 1
        self.running = True

    def stop(self, server, log=None):
        self.stops += 1
        self.running = False

    def mc_pid(self, server) -> str:
        return "4242" if self.running else ""

    def restart(self, server, log=None):
        self.stop(server, log)
        self.start(server, log)


def make_server(**kw) -> Server:
    base = dict(id="test01", name="TestBox", host="203.0.113.10",
                port=22, username="root", password="s3cret",
                mc_dir="/opt/mc", mc_version="26.2", platform="paper",
                ram_mb=4096, mc_port=25565)
    base.update(kw)
    return Server(**base)


@pytest.fixture
def fake_ssh() -> FakeSSH:
    return FakeSSH()


@pytest.fixture
def fake_control() -> FakeControl:
    return FakeControl(running=False)


@pytest.fixture
def server() -> Server:
    return make_server()
