from __future__ import annotations

import stat

import pytest

from lftpweb.core.crypto import (
    DecryptionError,
    decrypt_secret,
    encrypt_secret,
    ensure_install_secret,
)


def test_ensure_install_secret_generates_and_persists(tmp_path):
    secret1 = ensure_install_secret(str(tmp_path))
    secret2 = ensure_install_secret(str(tmp_path))
    assert secret1 == secret2
    assert len(secret1) == 32


def test_install_secret_file_is_mode_0600(tmp_path):
    ensure_install_secret(str(tmp_path))
    mode = (tmp_path / "secret.key").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_encrypt_decrypt_round_trip(tmp_path):
    token = encrypt_secret(str(tmp_path), "hunter2")
    assert token != "hunter2"
    assert decrypt_secret(str(tmp_path), token) == "hunter2"


def test_decrypt_with_different_install_secret_fails(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    token = encrypt_secret(str(dir_a), "topsecret")
    with pytest.raises(DecryptionError):
        decrypt_secret(str(dir_b), token)


def test_decrypt_corrupt_ciphertext_raises_decryption_error(tmp_path):
    ensure_install_secret(str(tmp_path))
    with pytest.raises(DecryptionError):
        decrypt_secret(str(tmp_path), "not-a-real-token")
