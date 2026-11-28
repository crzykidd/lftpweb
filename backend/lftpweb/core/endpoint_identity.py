"""Endpoint identity: when may a stored secret be reused? (audit batch C4, finding F6.)

lftpweb's secrets are write-only: the UI never has the plaintext, so an edit that leaves the
secret field blank means "keep the stored one". Without a check, someone who can only *write*
settings could change an endpoint's URL or address to a server they run, leave the secret blank,
and lftpweb would send the stored secret there -- recovering a secret they were never meant to
read. The rule here: a stored secret is only ever sent to the endpoint it was saved for. If the
endpoint's identity changes, the caller must supply the secret again.

**Non-breaking by construction.** Identity is *normalised*, so an edit that does not move the
endpoint -- a trailing slash, the case of the hostname, an explicit default port, surrounding
whitespace -- compares equal and keeps working with the secret left blank. Stored configs are
never rewritten; they are only compared against what is submitted.

Pure functions, no I/O.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}

# A hashable, comparable tuple. Opaque on purpose: callers only ever compare two of them.
Identity = tuple[Any, ...]


def url_identity(value: str | None, extra_path: str | None = None) -> Identity:
    """Identity of an HTTP(S) endpoint: scheme, lowercased host, port with the scheme default
    filled in, any credentials embedded in the URL, and the path with no trailing slash.

    The path is part of identity because a different path on the same host can be a different
    service (a reverse proxy mounting SABnzbd under `/sabnzbd` and something else under `/other`).
    `extra_path` (rTorrent's `rpc_path`) is appended before normalising, since the secret is
    sent to `<base_url><rpc_path>`. A value that cannot be parsed as a URL falls back to its
    stripped, trailing-slash-free text, so the comparison stays deterministic and a change still
    counts as a change.
    """
    raw = (value or "").strip()
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return ("raw", raw.rstrip("/"), _norm_path(extra_path))
    if not parts.scheme or not host:
        return ("raw", raw.rstrip("/"), _norm_path(extra_path))
    scheme = parts.scheme.lower()
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    path = _norm_path(parts.path) + _norm_path(extra_path)
    return (
        "url",
        scheme,
        host.lower(),
        port,
        parts.username or "",
        parts.password or "",
        path,
        parts.query,
    )


def _norm_path(path: str | None) -> str:
    stripped = (path or "").strip().strip("/")
    return f"/{stripped}" if stripped else ""


def ssh_identity(address: str | None, port: int | None, username: str | None) -> Identity:
    """Identity of an SSH endpoint: lowercased address, port, username (case-sensitive, as on
    the remote). Whitespace around the address and username is not significant.
    """
    return (
        "ssh",
        (address or "").strip().lower(),
        int(port) if port is not None else 22,
        (username or "").strip(),
    )
