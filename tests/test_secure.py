"""Security tests (issues 2, 3): credential vault + TOFU host keys."""
from __future__ import annotations

import pytest

from mcmanager import secure
from mcmanager.secure import (CredentialVault, VaultError, sudo_stdin,
                              SCOPED_SUDOERS_HINT)


@pytest.fixture
def vault(tmp_path):
    return CredentialVault(key_file=tmp_path / ".vault.key")


def test_vault_roundtrip(vault):
    token = vault.encrypt("hunter2")
    assert token.startswith("enc:v1:")
    assert vault.decrypt(token) == "hunter2"
    assert not vault.is_encrypted("hunter2")
    assert vault.is_encrypted(token)


def test_vault_unique_ciphertexts(vault):
    assert vault.encrypt("abc") != vault.encrypt("abc")  # random nonce


def test_vault_detects_tampering(vault, tmp_path, monkeypatch):
    token = vault.encrypt("hunter2")
    # flip a character inside the ciphertext part
    parts = token.split(":")
    ct = parts[2]
    flipped = ("A" if ct[0] != "A" else "B") + ct[1:]
    broken = ":".join([parts[0], parts[1], flipped, parts[3], parts[4]])
    with pytest.raises(VaultError):
        vault.decrypt(broken)


def test_vault_wrong_key_fails(tmp_path):
    v1 = CredentialVault(key_file=tmp_path / "k1")
    token = v1.encrypt("secret")
    # simulate a lost/regenerated key
    (tmp_path / "k2").write_bytes(b"\x01" * 32)
    v2 = CredentialVault(key_file=tmp_path / "k2")
    with pytest.raises(VaultError):
        v2.decrypt(token)


def test_vault_key_file_permissions(tmp_path):
    import os
    v = CredentialVault(key_file=tmp_path / "k3")
    v.encrypt("x")
    assert (os.stat(tmp_path / "k3").st_mode & 0o777) == 0o600


def test_plaintext_passthrough_for_migration(vault):
    """Legacy plaintext values decrypt to themselves (re-saved encrypted)."""
    assert vault.decrypt("legacy-plain") == "legacy-plain"


def test_sudo_stdin_pipes_password():
    cmd = sudo_stdin("pa'ss", "apt-get install -y x")
    assert "sudo -S" in cmd
    assert "printf" in cmd
    assert "'pa'\\''s'" in cmd or "pa'\\''ss" in cmd
    # password must never appear unquoted
    assert "pa'ss" not in cmd.replace("'\\''", "@").replace("'", "")


def test_scoped_sudoers_hint_mentions_no_nopasswd_all():
    assert "NOPASSWD: ALL" not in SCOPED_SUDOERS_HINT
    assert "NOPASSWD" in SCOPED_SUDOERS_HINT  # scoped variant documented


# ===================================================================
# v2.0.1 host-key store IO resilience (the Windows PermissionError crash)
# ===================================================================
import os
import sys

import paramiko

from mcmanager.secure import HostKeyStore


@pytest.fixture
def kh_path(tmp_path):
    return tmp_path / "known_hosts"


@pytest.fixture(scope="module")
def rsa_key():
    return paramiko.RSAKey.generate(2048)


def _client():
    return paramiko.SSHClient()  # never connected - offline safe


def _chmod_writable(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def test_attach_with_missing_file_does_not_raise(kh_path):
    """v2.0.0 regression: fresh install -> FileNotFoundError escaped attach()
    and killed EVERY connect task. attach must install the policy anyway."""
    store = HostKeyStore(path=kh_path)
    client = _client()
    store.attach(client)  # must not raise
    assert type(client._policy).__name__ == "TOFU"
    assert store.persistent  # file simply not created yet - still usable
    assert store.entries() == []


def test_attach_with_unreadable_file_does_not_raise(kh_path, rsa_key):
    """THE reported crash: known_hosts exists but cannot be opened
    (PermissionError on Windows). connect() must survive."""
    store = HostKeyStore(path=kh_path)
    store._tofu_policy().missing_host_key(None, "srv1", rsa_key)  # create+trust
    assert kh_path.exists()
    os.chmod(kh_path, 0o000)  # unreadable for a non-root user
    try:
        client = _client()
        store2 = HostKeyStore(path=kh_path)
        store2.attach(client)  # must not raise (v2.0.0 died here)
        assert not store2.persistent
        assert store2.last_error
        assert type(client._policy).__name__ == "TOFU"
    finally:
        _chmod_writable(kh_path)


def test_attach_seeds_client_from_memory_when_file_locked(
        kh_path, rsa_key, monkeypatch):
    """Locked file (simulated): the client still gets the trusted keys from
    our in-memory copy, so key-CHANGE detection keeps working."""
    import builtins
    store = HostKeyStore(path=kh_path)
    store._tofu_policy().missing_host_key(None, "srv1", rsa_key)

    real_open = builtins.open

    def locked_open(file, *a, **kw):
        if str(file).endswith("known_hosts"):
            raise PermissionError(13, "Permission denied")
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", locked_open)
    client = _client()
    store.attach(client)          # must not raise
    # in-memory store still trusted the host -> seeded into the client
    assert list(client.get_host_keys().keys()) == ["srv1"]


def test_tofu_first_use_persists_three_arg_add(kh_path, rsa_key):
    """v2.0.0 bug: policy called HostKeys.add with 2 args -> TypeError on
    first use. Also verifies persistence + fingerprint helpers."""
    store = HostKeyStore(path=kh_path)
    store._tofu_policy().missing_host_key(None, "srv1", rsa_key)
    assert kh_path.exists()
    text = kh_path.read_text()
    assert "srv1" in text and "ssh-rsa" in text
    if sys.platform != "win32":
        assert (os.stat(kh_path).st_mode & 0o777) == 0o600
    # a fresh store (app restart) sees the entry
    store2 = HostKeyStore(path=kh_path)
    assert store2.entries() == [("srv1", "ssh-rsa")]
    assert store2.fingerprint_of("srv1").startswith("SHA256:")
    assert store2.fingerprint_of("unknown") == ""


def test_tofu_persist_failure_never_breaks_connection(kh_path, rsa_key):
    """Unwritable store: missing_host_key must trust in memory and move on."""
    store = HostKeyStore(path=kh_path)
    store._tofu_policy().missing_host_key(None, "srv1", rsa_key)  # writable
    os.chmod(kh_path, 0o444)  # read-only file (write -> PermissionError)
    try:
        # must not raise even though saving fails
        store._tofu_policy().missing_host_key(None, "srv2", rsa_key)
        # self-heal: _save clears the read-only flag -> file was updated
        assert "srv2" in kh_path.read_text()
        assert store.persistent  # healed, not degraded
    finally:
        _chmod_writable(kh_path)


def test_save_does_not_wipe_other_hosts(kh_path, rsa_key):
    """v2.0.0 bug: the store never LOADED the file before saving, so every
    save wiped all previously trusted hosts."""
    HostKeyStore(path=kh_path)._tofu_policy().missing_host_key(
        None, "hostA", rsa_key)
    # simulate app restart, trust a second server
    HostKeyStore(path=kh_path)._tofu_policy().missing_host_key(
        None, "hostB", rsa_key)
    hosts = {h for h, _ in HostKeyStore(path=kh_path).entries()}
    assert hosts == {"hostA", "hostB"}


def test_known_hosts_path_is_a_directory(kh_path, rsa_key):
    """Broken machine state: a directory where the file should be. Nothing
    may raise; the store degrades to session-only."""
    kh_path.mkdir()
    store = HostKeyStore(path=kh_path)
    client = _client()
    store.attach(client)                                  # no raise
    store._tofu_policy().missing_host_key(None, "s", rsa_key)  # no raise
    assert not store.persistent
    assert store.last_error
    assert (host for host, _ in store.entries())  # in-memory entry survives
    assert ("s", "ssh-rsa") in store.entries()


def test_forget_removes_host(kh_path, rsa_key):
    store = HostKeyStore(path=kh_path)
    store._tofu_policy().missing_host_key(None, "srv1", rsa_key)
    store.forget("srv1")
    assert HostKeyStore(path=kh_path).entries() == []


# ===================================================================
# v2.0.2 first-use confirmation: single-flight + session answer memory
# ===================================================================
import threading
import time
from types import SimpleNamespace

from mcmanager.secure import HostKeyChanged


def test_ask_single_flight_shares_one_dialog(kh_path):
    """Watchdog + user click connect at once -> ONE dialog, shared answer
    (v2.0.1 stacked independent asks and mislabelled failures)."""
    store = HostKeyStore(path=kh_path)
    dialog_open = threading.Event()
    calls = []

    def slow_callback(host, fp):
        calls.append(host)          # dialog opens...
        dialog_open.wait(5)         # ...user reads it...
        return True                 # ...and clicks Yes

    store.first_use_callback = slow_callback
    results = []

    def worker():
        results.append(store.ask("srv1", "SHA256:x"))

    t1 = threading.Thread(target=worker)
    t1.start()
    for _ in range(200):            # wait until the leader's dialog is up
        if calls:
            break
        time.sleep(0.01)
    t2 = threading.Thread(target=worker)   # second concurrent connect
    t2.start()
    time.sleep(0.2)                 # t2 must WAIT, not open another dialog
    assert calls == ["srv1"]        # still exactly one dialog
    dialog_open.set()               # user answers
    t1.join(5)
    t2.join(5)
    assert results == [True, True]  # both connects proceed with the answer


def test_ask_answer_remembered_no_second_dialog(kh_path):
    store = HostKeyStore(path=kh_path)
    calls = []

    def cb(host, fp):
        calls.append(host)
        return True

    store.first_use_callback = cb
    assert store.ask("srv1", "fp") is True
    assert store.ask("srv1", "fp") is True      # from memory
    assert calls == ["srv1"]                    # dialog shown exactly once


def test_ask_decline_is_remembered_and_blocks_network(kh_path):
    store = HostKeyStore(path=kh_path)
    store.first_use_callback = lambda h, f: False
    assert store.ask("srv1", "fp") is False
    assert store.declined("srv1") is True
    assert store.declined("other") is False
    # a later ask must NOT show another dialog (would spam + hammer server)
    def must_not_ask(h, f):
        raise AssertionError("declined host re-asked!")

    store.first_use_callback = must_not_ask
    assert store.ask("srv1", "fp") is False


def test_ask_callback_crash_is_refusal_not_hang(kh_path):
    store = HostKeyStore(path=kh_path)
    store.first_use_callback = lambda h, f: 1 / 0
    assert store.ask("srv1", "fp") is False
    assert store.declined("srv1")


def test_policy_refusal_message_tells_how_to_retry(kh_path, rsa_key):
    store = HostKeyStore(path=kh_path)
    store.first_use_callback = lambda h, f: False
    with pytest.raises(HostKeyChanged) as ei:
        store._tofu_policy().missing_host_key(None, "srv9", rsa_key)
    assert "restart" in str(ei.value)
    assert "srv9" in str(ei.value)
    # the refusal must also be remembered -> next attempt is instant
    with pytest.raises(HostKeyChanged):
        store._tofu_policy().missing_host_key(None, "srv9", rsa_key)


def test_ssh_connect_refuses_declined_host_without_tcp(kh_path, monkeypatch):
    """ssh_manager must check `declined` BEFORE the TCP handshake so a
    declined host cannot trigger fail2ban with endless connection
    attempts (the WinError 10054 the user saw)."""
    from mcmanager import ssh_manager
    from mcmanager.ssh_manager import SSHManager, SSHError

    host = "declined-test.invalid"
    monkeypatch.setattr(ssh_manager.HOST_KEYS, "_answers", {host: False})

    server = SimpleNamespace(id="x", host=host, port=22,
                             username="u", password="p")

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("network layer touched for declined host!")

    monkeypatch.setattr(ssh_manager.paramiko, "SSHClient", Boom)
    with pytest.raises(SSHError) as ei:
        SSHManager().connect(server)
    assert "declined" in str(ei.value)


# ===================================================================
# v2.0.1 vault IO resilience
# ===================================================================
def test_vault_ephemeral_when_key_unreadable(tmp_path):
    """Locked/ACL-broken vault key: the app must keep running with a
    session key instead of crashing every decrypt (v2.0.0 would raise)."""
    kf = tmp_path / ".vault.key"
    v = CredentialVault(key_file=kf)
    v.encrypt("secret")            # creates + persists the key
    os.chmod(kf, 0o000)
    try:
        v2 = CredentialVault(key_file=kf)
        token = v2.encrypt("x")    # must not raise
        assert v2.ephemeral and v2.last_error
        assert v2.decrypt(token) == "x"   # session roundtrip still works
    finally:
        _chmod_writable(kf)


def test_vault_ephemeral_when_dir_readonly(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    os.chmod(d, 0o555)
    try:
        v = CredentialVault(key_file=d / ".vault.key")
        token = v.encrypt("hello")
        assert v.ephemeral and v.last_error
        assert v.decrypt(token) == "hello"
    finally:
        os.chmod(d, 0o755)
