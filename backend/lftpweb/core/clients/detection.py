"""Base-path and category detection (docs/download-client-framework-spec.md §8.2 correction,
migration 028; §8.3, prompts/2026-08-22-client-base-paths-detected.md,
prompts/2026-08-23-category-binding-redesign.md) -- what a connector's own `list_base_paths` /
`list_categories` (spec §2.1) reports, so a save never has to trust the client's word alone.

**Detection proposes; it never saves.** Nothing in this module writes to
`download_client_base_path` or `download_client_category` -- `api/settings_clients.py`'s
`POST .../test` only ever returns what it found; a caller (the settings UI) decides whether and
how to turn a proposal into a saved row.

**A detection failure must never fail the connection test.** Reachability and detection are
different questions (spec §4.2's temperament, applied here): every function below resolves to
a result -- an empty list, or a per-path `BasePathState` -- never lets an exception escape to
its caller. `report_base_paths`/`report_categories` are deliberately tolerant of *any* exception
a connector's `list_base_paths`/`list_categories` can raise, not just the declared `ClientError`
taxonomy, because a connector that hasn't implemented the method yet (`NotImplementedError`, a
test double) must be just as harmless as one that raises `ClientError` for real.

Split base-path detection into two functions, not one, so each half is independently
unit-testable without a live SSH connection: `report_base_paths` needs only a `DownloadClient`
and its `CapabilitySet`; `verify_reported_paths` needs only something that duck-types
`asyncssh.SFTPClient.stat` (`tests/test_browse.py`'s own `_FakeSFTP` pattern) --
`api/settings_clients.py` is the only caller that ever has both a real connector and a real SFTP
client at once. **`report_categories` has no SSH-verification counterpart** -- a category is a
name the client itself owns, not a filesystem path lftpweb needs to independently confirm it can
see, so "detect, propose, confirm" collapses to just "detect, propose" for this one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from lftpweb.core import browse as browse_core

from .base import CapabilitySet, Operation
from .models import BasePathKind

if TYPE_CHECKING:
    import asyncssh

    from .base import DownloadClient
    from .models import BasePath


class BasePathState(StrEnum):
    """The three outcomes of checking one client-reported path over SSH (spec's own wording,
    2026-08-22) -- **deliberately not collapsed into two**. `core.browse.
    remote_directory_error`'s own docstring draws exactly this distinction: a clean "no such
    directory" answer is a verified fact, and an ambiguous failure (permission, protocol) is
    not the same fact just because both mean "cannot use this path right now."
    """

    # The client reported it, and lftpweb sees it at the same path over SSH.
    VERIFIED = "verified"
    # The client reported it, and the seedbox clearly reports it missing or not a directory --
    # the namespace mismatch, detected rather than asked about. The user supplies the SSH-
    # visible equivalent.
    NOT_FOUND = "not_found"
    # The stat failed for any other reason (permission denied, a protocol hiccup, no SSH
    # connection available at all). **Not the same as `not_found`** -- never presented as a
    # failure; the caller may accept it anyway.
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class DetectedBasePath:
    """One proposal: what the client reported, its declared role (`core.clients.models.
    BasePathKind`), and whether lftpweb can see it at the same path. `client_path` is always
    the client's own literal answer -- turning a `not_found` into a saved row is the caller's
    job (supplying the SSH-visible equivalent), never this module's.
    """

    client_path: str
    kind: BasePathKind
    state: BasePathState


async def report_base_paths(client: DownloadClient, capabilities: CapabilitySet) -> list[BasePath]:
    """The client's own answer, verbatim -- `[]` if the connector doesn't declare
    `Operation.LIST_BASE_PATHS` (accepting derived) at all, which is **not an error**: a
    connector that cannot answer this question simply contributes nothing (spec's own
    instruction). `[]` also on *any* exception `list_base_paths()` raises -- detection must
    never fail the connection test that already succeeded to get here.
    """
    if not capabilities.supports(Operation.LIST_BASE_PATHS, accept_derived=True):
        return []
    try:
        return await client.list_base_paths()
    except Exception:  # noqa: BLE001 - a detection failure must never fail the connection test
        return []


async def _verify_one(sftp: asyncssh.SFTPClient | None, base_path: BasePath) -> DetectedBasePath:
    if sftp is None:
        return DetectedBasePath(base_path.path, base_path.kind, BasePathState.UNVERIFIED)
    try:
        await browse_core.remote_directory_error(sftp, base_path.path)
        return DetectedBasePath(base_path.path, base_path.kind, BasePathState.VERIFIED)
    except browse_core.RemotePathNotFoundError:
        return DetectedBasePath(base_path.path, base_path.kind, BasePathState.NOT_FOUND)
    except Exception:  # noqa: BLE001 - ambiguous failure -- unverified, never presented as wrong
        return DetectedBasePath(base_path.path, base_path.kind, BasePathState.UNVERIFIED)


async def verify_reported_paths(
    reported: list[BasePath], sftp: asyncssh.SFTPClient | None
) -> list[DetectedBasePath]:
    """SSH-check every entry `report_base_paths` returned. `sftp is None` -- no host configured,
    credentials not decryptable, or the connection itself could not be established -- verifies
    nothing and reports every entry `unverified`, never `not_found`: not being able to look is
    not the same fact as the seedbox saying no.
    """
    return [await _verify_one(sftp, bp) for bp in reported]


async def report_categories(client: DownloadClient, capabilities: CapabilitySet) -> list[str]:
    """The client's own categories, verbatim (spec §8.3, joined 2026-08-23) -- `[]` if the
    connector doesn't declare `Operation.LIST_CATEGORIES` (accepting derived) at all, which is
    **not an error**: a connector that cannot answer this simply contributes nothing, and the
    settings UI falls back to proposing from base-path arithmetic instead. `[]` also on *any*
    exception `list_categories()` raises, mirroring `report_base_paths`'s own tolerance -- a
    category-detection failure must never fail the connection test that already succeeded to get
    here.
    """
    if not capabilities.supports(Operation.LIST_CATEGORIES, accept_derived=True):
        return []
    try:
        return await client.list_categories()
    except Exception:  # noqa: BLE001 - a detection failure must never fail the connection test
        return []
