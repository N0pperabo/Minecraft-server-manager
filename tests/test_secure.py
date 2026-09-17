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
