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

    # -- key management -----------------------------------------------------
    def _load_key(self) -> bytes:
        with _lock:
            if self._key is not None:
                return self._key
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            if self.key_file.exists():
                data = self.key_file.read_bytes().strip()
                if len(data) == 32:
                    self._key = data
                    return self._key
                if len(data) == 64:  # hex fallback
                    try:
                        self._key = bytes.fromhex(data.decode())
                        if len(self._key) == 32:
                            return self._key
                    except ValueError:
                        pass
            # first run (or lost key): generate a fresh random key.  Loss of
            # the key only means saved passwords must be re-entered.
            self._key = secrets.token_bytes(32)
            self._write_key(self._key)
            return self._key

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


class HostKeyStore:
    """TOFU store of trusted SSH host keys (~/.mcmanager/known_hosts).

    Uses paramiko's own HostKeys container so key matching/mismatching
    follows the exact same rules as OpenSSH clients.  `first_use_callback`
    (if set) can show the fingerprint to the user before trusting a new
    host; returning False aborts the connection.
    """

    def __init__(self, path: Path = KNOWN_HOSTS_FILE,
                 first_use_callback=None) -> None:
        self.path = path
        self.first_use_callback = first_use_callback  # (hostname, fingerprint) -> bool
        self._inner = None
        self._tlock = threading.Lock()

    def _store(self):
        with self._tlock:
            if self._inner is None:
                import paramiko
                self.path.parent.mkdir(parents=True, exist_ok=True)
                hk = paramiko.HostKeys(str(self.path))
                self._inner = hk
            return self._inner

    # -- policy plumbing ------------------------------------------------------
    def attach(self, client) -> None:
        """Wire a paramiko.SSHClient to this store: load known keys, install
        the TOFU policy for unknown ones, and let paramiko itself raise
        BadHostKeyException on mismatch."""
        client.load_host_keys(str(self.path))
        client.set_missing_host_key_policy(self._tofu_policy())

    def _tofu_policy(self):
        store = self

        class TOFU:
            def missing_host_key(self, client, hostname, key):
                fp = ":".join(f"{b:02x}" for b in
                              hashlib.sha256(key.asbytes()).digest()[:16])
                display = f"SHA256:{fp}"
                if store.first_use_callback is not None:
                    try:
                        ok = bool(store.first_use_callback(hostname, display))
                    except Exception:  # noqa: BLE001 - callback must not kill connect
                        ok = False
                    if not ok:
                        raise HostKeyChanged(
                            f"Host key for {hostname} was NOT accepted by the "
                            "user - connection refused.")
                # trust on first use: persist so later MITM attempts fail hard
                with store._tlock:
                    store._store().add(hostname, key)
                    store._save()
        return TOFU()

    def _save(self) -> None:
        try:
            self._store().save(str(self.path))
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- helpers ---------------------------------------------------------------
    def fingerprint_of(self, hostname: str) -> str:
        for hostkeys in self._store().lookup(hostname) or []:
            key = hostkeys["key"] if "key" in hostkeys.keys() else None
            if key is not None:
                fp = ":".join(f"{b:02x}" for b in
                              hashlib.sha256(key.asbytes()).digest()[:16])
                return f"SHA256:{fp}"
        return ""

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
