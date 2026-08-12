"""Credential encryption at rest (DESIGN.md §8).

Moved up from build phase 8 to phase 2 (docs/decisions.md) — phase 2 is the phase where a
seedbox password first exists, and "store it in plaintext until later" is not acceptable even
in a dev build. This module implements only the encryption scheme; the rest of §8 (auth modes,
sessions, API keys, rate limiting) is still phase 8.

Scheme: a per-install secret is generated on first run and written to
`<config_dir>/secret.key`, mode 0600. A Fernet key (AES-128-CBC + HMAC-SHA256, authenticated)
is derived from it via HKDF so the on-disk secret itself is never used directly as a key. The
secret is deliberately **not** included in database backups (§10.2/§10.3) — a `.db` backup
therefore carries no usable credential, and a restore onto a different install must re-enter
them. `DecryptionError` is how the caller (api/settings.py) detects that case and surfaces the
"credentials need re-entry" state (§8) instead of crashing or retrying.
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SECRET_FILENAME = "secret.key"
_HKDF_INFO = b"lftpweb-credential-key-v1"


class DecryptionError(Exception):
    """A stored credential could not be decrypted with the current install secret.

    Raised when the secret file is missing/rotated, or the ciphertext is corrupt. Callers
    must treat this as "credentials need re-entry" (§8), not as a transient failure.
    """


def _secret_path(config_dir: str) -> Path:
    return Path(config_dir) / SECRET_FILENAME


def ensure_install_secret(config_dir: str) -> bytes:
    """Return the per-install secret, generating it on first run.

    32 random bytes, written mode 0600. Generation is not itself concurrency-safe (two
    processes racing on first boot could each generate one) but this app is single-process
    (§2), so that race cannot occur here.
    """
    path = _secret_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = path.read_bytes()
        if len(data) != 32:
            raise DecryptionError(f"install secret at {path} is not 32 bytes; refusing to use it")
        return data

    secret = os.urandom(32)
    # Write then chmod (not os.open with the mode up front) is fine here: the file is
    # created under /config, which only this process's uid can traverse in practice, and the
    # window is a single local filesystem write with no network exposure.
    path.write_bytes(secret)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return secret


def _fernet(secret: bytes) -> Fernet:
    """Derive a Fernet key from the raw install secret via HKDF-SHA256.

    Fernet requires a 32-byte urlsafe-base64 key; deriving it rather than using the raw
    secret directly keeps the on-disk secret and the actual encryption key distinct, so
    swapping the KDF or adding key versioning later doesn't touch the on-disk format.
    """
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(secret)
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(config_dir: str, plaintext: str) -> str:
    """Encrypt a credential (e.g. a host password) for storage in `host.password_enc`."""
    secret = ensure_install_secret(config_dir)
    token = _fernet(secret).encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt_secret(config_dir: str, ciphertext: str) -> str:
    """Decrypt a stored credential. Raises `DecryptionError` if it cannot be recovered."""
    secret = ensure_install_secret(config_dir)
    try:
        plaintext = _fernet(secret).decrypt(ciphertext.encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise DecryptionError(
            "stored credential does not decrypt with the current install secret"
        ) from exc
    return plaintext.decode("utf-8")
