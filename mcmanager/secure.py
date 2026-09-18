"""Security hardening layer (v2.0).

Three concerns, previously reported as issues:

1.  SSH passwords were stored as PLAINTEXT in servers.json.
    -> `CredentialVault` encrypts every secret at rest (password + RCON
       password).  The key lives in ~/.mcmanager/.vault.key (0600) and the
       ciphertext is authenticated (encrypt-then-MAC), so tampering with
       servers.json is detected instead of silently producing garbage.
       No third-party crypto dependency is required: the cipher is a
       SHA-256-HMAC based CTR keystream (same construction as HKDF/Salsa
       style stream modes), which is secure for this threat model
       (protect secrets on disk from casual reading / backup leakage).

2.  `paramiko.AutoAddPolicy()` accepted ANY host key - a man-in-the-middle
    could impersonate the VPS on first connect or any connect.
    -> `HostKeyStore` implements TOFU (trust-on-first-use) with a
       persistent known_hosts file: the first connection records the key
       (and the UI can show the fingerprint for confirmation); every later
       connection REJECTS the server if the key changed (MITM alarm).

3.  The installer suggested `NOPASSWD: ALL` sudo for the SSH user.
    -> `sudo_stdin()` runs privileged commands with the password piped to
       `sudo -S` (no passwordless root at all) and `SCOPED_SUDOERS_HINT`
       documents a minimal, command-scoped sudoers alternative for
       unattended installs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import stat
import threading
from pathlib import Path

CONFIG_DIR = Path.home() / ".mcmanager"
VAULT_KEY_FILE = CONFIG_DIR / ".vault.key"
KNOWN_HOSTS_FILE = CONFIG_DIR / "known_hosts"
VAULT_PREFIX = "enc:v1:"

_lock = threading.Lock()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str, pad_to: int | None = None) -> bytes:
    """Decode url-safe base64; pad_to (byte length) is checked only when
    the expected size is known (nonce/tag) - ciphertext has variable
    length and must not be length-checked against the base64 string."""
    text = text.strip()
    text += "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(text.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("corrupted vault entry (bad base64)") from exc
    if pad_to is not None and len(raw) != pad_to:
        raise ValueError("corrupted vault entry (bad length)")
    return raw


class VaultError(Exception):
    """Raised when a stored secret fails authentication (tampered file?)."""


# ============================================================ credentials ===
class CredentialVault:
    """Authenticated encryption for secrets at rest in servers.json.

    Layout of an encrypted value:  enc:v1:<nonce>:<ciphertext>:<tag>
      nonce  16 random bytes
      ct     plaintext XOR keystream(key, nonce)
      tag    HMAC-SHA256(key, nonce || ct)[:16]
    Keystream block i = HMAC-SHA256(key, nonce || uint64be(i)).
    """

    NONCE_LEN = 16
    TAG_LEN = 16
    BLOCK = 32

    def __init__(self, key_file: Path = VAULT_KEY_FILE) -> None:
        self.key_file = key_file
        self._key: bytes | None = None
        # v2.0.1: set when the key file cannot be read or written (locked by
        # another process, broken ACL, read-only media...).  The vault then
        # uses a random SESSION-ONLY key so the app keeps running instead of
        # crashing every task; the cost is that stored passwords cannot be
        # decrypted and must be re-entered.
        self.ephemeral = False
        self.last_error = ""

    # -- key management -----------------------------------------------------
    def _load_key(self) -> bytes:
        with _lock:
            if self._key is not None:
                return self._key
            key = self._read_or_create_key()
            if key is None:
                # disk unusable - degrade to an ephemeral key, never crash
                self.ephemeral = True
                self.last_error = (
                    f"Cannot use vault key file {self.key_file} - saved "
                    "passwords cannot be unlocked and must be re-entered. "
                    "Close other instances of the app or fix the file's "
                    "permissions, then restart.")
                self._key = secrets.token_bytes(32)
            else:
                self._key = key
            return self._key

    def _read_or_create_key(self) -> "bytes | None":
        """Return the 32-byte key, or None when the file is unusable."""
        try:
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            if self.key_file.exists():
                data = self.key_file.read_bytes().strip()
                if len(data) == 32:
                    return data
                if len(data) == 64:  # hex fallback
                    try:
                        raw = bytes.fromhex(data.decode())
                        if len(raw) == 32:
                            return raw
                    except ValueError:
                        pass
            # first run (or lost key): generate a fresh random key.  Loss of
            # the key only means saved passwords must be re-entered.
            key = secrets.token_bytes(32)
            self._write_key(key)
            return key
        except OSError:
            return None

    def _write_key(self, key: bytes) -> None:
        fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        try:  # best-effort on filesystems without full chmod support
            os.chmod(self.key_file, 0o600)
        except OSError:
            pass

    # -- cipher --------------------------------------------------------------
    def _keystream(self, key: bytes, nonce: bytes, length: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            out += hmac.new(key, nonce + counter.to_bytes(8, "big"),
                            hashlib.sha256).digest()
            counter += 1
        return bytes(out[:length])

    # -- public API ----------------------------------------------------------
    @staticmethod
    def is_encrypted(value: str) -> bool:
        return isinstance(value, str) and value.startswith(VAULT_PREFIX)

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        if self.is_encrypted(plaintext):
            return plaintext
        key = self._load_key()
        nonce = secrets.token_bytes(self.NONCE_LEN)
        pt = plaintext.encode("utf-8")
        ct = bytes(a ^ b for a, b in zip(pt, self._keystream(key, nonce, len(pt))))
        tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:self.TAG_LEN]
        return (f"{VAULT_PREFIX}{_b64(nonce)}:{_b64(ct)}:{_b64(tag)}")

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        if not self.is_encrypted(token):
            return token  # legacy plaintext value; will be re-saved encrypted
        try:
            _, _, nonce_s, ct_s, tag_s = token.split(":")
            nonce = _unb64(nonce_s, self.NONCE_LEN)
            ct = _unb64(ct_s)   # variable length - no fixed size check
            tag = _unb64(tag_s, self.TAG_LEN)
        except ValueError as exc:
            raise VaultError("Malformed stored credential.") from exc
        key = self._load_key()
        good = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:self.TAG_LEN]
        if not hmac.compare_digest(good, tag):
            raise VaultError(
                "Stored password failed integrity check - servers.json was "
                "modified or the vault key is missing. Re-enter the password "
                "in the server editor.")
        pt = bytes(a ^ b for a, b in zip(ct, self._keystream(key, nonce, len(ct))))
        return pt.decode("utf-8", errors="replace")


VAULT = CredentialVault()


# ============================================================== host keys ===
class HostKeyChanged(Exception):
    """The server presented a DIFFERENT host key than the trusted one.

    This is what a man-in-the-middle attack looks like - connecting is
    refused instead of silently accepting the new key (the old
    AutoAddPolicy behavior).  The message tells the user how to proceed
    if the change is legitimate (VPS reinstall)."""


def _fingerprint(key) -> str:
    fp = ":".join(f"{b:02x}" for b in
                  hashlib.sha256(key.asbytes()).digest()[:16])
    return f"SHA256:{fp}"


class HostKeyStore:
    """TOFU store of trusted SSH host keys (~/.mcmanager/known_hosts).

    Uses paramiko's own HostKeys container so key matching/mismatching
    follows the exact same rules as OpenSSH clients.  `first_use_callback`
    (if set) can show the fingerprint to the user before trusting a new
    host; returning False aborts the connection.

    v2.0.1: every disk access is fault-tolerant.  A missing, unreadable,
    locked or read-only known_hosts must NEVER kill a connection (the
    v2.0.0 bug: `attach()` let the load error escape, so EVERY server
    task died with PermissionError and the app was unusable).  On any
    file problem the store degrades to an in-memory TOFU for the current
    session and records a human-readable reason in `last_error`, which
    the UI shows once.
    """

    def __init__(self, path: Path = KNOWN_HOSTS_FILE,
                 first_use_callback=None) -> None:
        self.path = path
        self.first_use_callback = first_use_callback  # (hostname, fingerprint) -> bool
        self._inner = None
        # RLock, NOT Lock: callers hold _tlock around _store()/_save(), and
        # those helpers take _tlock again themselves - a plain Lock
        # self-deadlocks the calling thread (v2.0.0 bug: first-use connect
        # and Settings->Forget hung forever).
        self._tlock = threading.RLock()
        self.last_error = ""   # set once when the disk store is unusable
        self.persistent = True  # False while the file cannot be used
        # first-use confirmation state (v2.0.2): ONE dialog per host even
        # when the watchdog and a user click connect at the same time, and
        # the answer is remembered for the whole session so a declined host
        # cannot hammer the server with retry handshakes (fail2ban!).
        self._ask_lock = threading.Lock()
        self._pending: "dict[str, threading.Event]" = {}
        self._answers: "dict[str, bool]" = {}

    # -- first-use confirmation (v2.0.2) --------------------------------------
    def ask(self, hostname: str, fingerprint: str) -> bool:
        """Ask the user to trust `hostname` - exactly ONCE per session.

        Single-flight: while one thread is showing the dialog, every other
        thread asking about the SAME host waits and shares the answer
        instead of stacking more dialogs.  The answer (yes or no) is then
        remembered: a second connect to the same host never re-asks within
        this session, and a DECLINED host is refused instantly without
        touching the network.
        """
        with self._ask_lock:
            if hostname in self._answers:
                return self._answers[hostname]
            ev = self._pending.get(hostname)
            if ev is None:
                ev = threading.Event()
                self._pending[hostname] = ev
                leader = True
            else:
                leader = False
        if not leader:
            ev.wait(timeout=600)  # leader always sets ev (finally)
            with self._ask_lock:
                return self._answers.get(hostname, False)
        try:
            try:
                ok = bool(self.first_use_callback(hostname, fingerprint))
            except Exception:  # noqa: BLE001 - a broken callback must not hang
                ok = False
            with self._ask_lock:
                self._answers[hostname] = ok
            return ok
        finally:
            ev.set()
            with self._ask_lock:
                self._pending.pop(hostname, None)

    def declined(self, hostname: str) -> bool:
        """True when the user already REFUSED this host in this session.
        ssh_manager checks this BEFORE opening a TCP connection so a
        declined host cannot trigger fail2ban with endless handshakes."""
        with self._ask_lock:
            return self._answers.get(hostname, None) is False

    # -- status -------------------------------------------------------------
    def _degrade(self, message: str) -> None:
        """The disk store is unusable: keep going in memory, remember why.

        In-memory TOFU still protects the CURRENT session: any key change
        after the first connect is refused.  Only persistence across
        restarts is lost until the file problem is fixed."""
        self.persistent = False
        if not self.last_error:
            self.last_error = message

    def _ensure_parent(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._degrade(f"cannot create folder {self.path.parent} ({exc})")

    def _store(self):
        with self._tlock:
            if self._inner is None:
                import paramiko
                hk = paramiko.HostKeys()  # empty container: WE control the IO
                try:
                    hk.load(str(self.path))  # tolerates junk lines
                except FileNotFoundError:
                    pass  # first run: nothing trusted yet (paramiko raises)
                except OSError as exc:
                    self._degrade(
                        f"cannot read {self.path} ({exc}). Host keys are "
                        "remembered for this session only - try deleting "
                        "that file and restart.")
                self._inner = hk
            return self._inner

    # -- policy plumbing ------------------------------------------------------
    def attach(self, client) -> None:
        """Wire a paramiko.SSHClient to this store: load known keys, install
        the TOFU policy for unknown ones, and let paramiko itself raise
        BadHostKeyException on mismatch.

        v2.0.1: NEVER raises on host-key file problems.  A missing, locked
        or unreadable known_hosts must not kill the connection (this was
        the v2.0.0 startup crash: PermissionError for every server)."""
        self._ensure_parent()
        store = self._store()
        try:
            client.load_host_keys(str(self.path))
        except FileNotFoundError:
            pass  # fresh install: no trusted hosts yet
        except OSError:
            # file unreadable (locked, wrong ACL, a directory...): seed the
            # client from our in-memory copy so key-CHANGE detection keeps
            # working for every host we already trust.
            try:
                dest = client.get_host_keys()
                for host in list(store.keys()):
                    for ktype, key in dict(store.lookup(host) or {}).items():
                        dest.add(host, ktype, key)
            except Exception:  # noqa: BLE001 - seeding is best-effort
                pass
        client.set_missing_host_key_policy(self._tofu_policy())

    def _tofu_policy(self):
        store = self

        class TOFU:
            def missing_host_key(self, client, hostname, key):
                display = _fingerprint(key)
                if store.first_use_callback is not None:
                    # single-flight ask: concurrent connects share ONE
                    # dialog; a declined host is refused instantly
                    if not store.ask(hostname, display):
                        raise HostKeyChanged(
                            f"Host key for {hostname} was NOT accepted - "
                            "connection refused. If this was a mistake, "
                            "restart MC Manager to be asked again.")
                # trust on first use: persist so later MITM attempts fail
                # hard.  Persisting must never break the connection itself -
                # on failure we degrade to session-only trust.
                try:
                    with store._tlock:
                        s = store._store()
                        # paramiko's HostKeys.add is (hostname, keytype, key)
                        s.add(hostname, key.get_name(), key)
                        store._save()
                except Exception:  # noqa: BLE001
                    pass
        return TOFU()

    def _save(self) -> None:
        """Atomically persist the store.

        v2.0.1: writes to a temp file then os.replace() (no half-written
        known_hosts if anything fails mid-write), clears a stale read-only
        flag first (a Windows read-only attribute or a posix 0444 would
        otherwise fail the swap), and degrades gracefully when the path is
        unusable (e.g. a directory sits where the file should be)."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self._ensure_parent()
            try:
                # os.replace refuses to clobber a read-only target on Windows
                os.chmod(self.path, stat.S_IREAD | stat.S_IWRITE)
            except OSError:
                pass  # target missing: fine
            self._store().save(str(tmp))
            os.replace(tmp, self.path)  # atomic on posix AND Windows
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self._degrade(
                f"cannot write {self.path} ({exc}). Host keys are remembered "
                "for this session only - try deleting that file and restart.")

    # -- helpers ---------------------------------------------------------------
    def fingerprint_of(self, hostname: str) -> str:
        try:
            trusted = self._store().lookup(hostname) or {}
            for key in list(trusted.values()):
                return _fingerprint(key)
        except Exception:  # noqa: BLE001
            pass
        return ""

    def entries(self) -> "list[tuple[str, str]]":
        """(hostname, key-type) pairs currently trusted - for the UI."""
        out: "list[tuple[str, str]]" = []
        try:
            store = self._store()
            for host in list(store.keys()):
                for ktype in dict(store.lookup(host) or {}):
                    out.append((host, ktype))
        except Exception:  # noqa: BLE001
            pass
        return out

    def forget(self, hostname: str) -> None:
        """Drop a trusted key (used after a legitimate server reinstall)."""
        with self._tlock:
            hk = self._store()
            if hostname in hk:
                del hk[hostname]
                self._save()


HOST_KEYS = HostKeyStore()


# ================================================================== sudo ====
def sudo_stdin(password: str, command: str) -> str:
    """Build a shell snippet that pipes `password` into `sudo -S`.

    This makes NOPASSWD sudoers entries unnecessary: the elevation lasts
    for exactly one command and is authenticated by the same SSH password
    the app already holds in memory for the session.
    """
    esc = password.replace("'", "'\\''")
    return f"printf '%s\\n' '{esc}' | sudo -S -p '' -- {command}"


SCOPED_SUDOERS_HINT = (
    "Unattended installs only (optional!): give the SSH user passwordless "
    "access to JUST the package manager instead of everything:\n"
    "  youruser ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/dnf, /usr/bin/apk\n"
    "Never give this app unrestricted passwordless root - it is not needed."
)
