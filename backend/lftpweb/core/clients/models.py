"""The normalized record shapes a connector hands back (docs/download-client-framework-spec.md
§2.2, §3, §7.1) -- the client-agnostic vocabulary every caller reads, regardless of which of
the 7-10 expected connectors produced it.

Same "narrow projection + `raw`" shape `core/arrclient.py`'s `QueueRecord`/`HistoryEvent`
already use: a handful of typed, normalized fields for logic to read, plus the untouched
response dict alongside for a future caller (or a test, or a support-bundle capture, spec
§13.3) to reach past the projection without a client change. `raw` is never interpreted here or
by any caller outside the connector that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


# Spec §3's normalized phase vocabulary, exactly nine values. `unknown` is the safe default a
# connector's `map_phase` must fall back to for any status string this codebase has never seen
# -- spec §4.2's "absent is not a verdict" encoded in the type rather than left to a comment,
# and `unknown` must never block anything a caller does (the same way an unreachable *arr
# never downgrades a verdict, `core/arrclient.py`'s module docstring).
class TransferPhase(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    VERIFYING = "verifying"
    EXTRACTING = "extracting"
    SEEDING = "seeding"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Transfer:
    """One normalized transfer/history record (spec §2.2) -- the shape `list_transfers`,
    `list_history`, and `get_transfer` all return.

    `client_id`, `name`, `phase`, `raw_status`, and `raw` are mandatory for every connector,
    **never declared** in a capability set (spec §2.2: "Mandatory for every connector, never
    declared, no exceptions") -- they always exist because a record cannot exist without them.
    Every field below that line is one entry in the closed `Field` vocabulary
    (`core.clients.base.Field`), optional and defaulting to `None`: a connector declares
    support for it (or doesn't) via its `CapabilitySet`, and `core.clients.base.
    project_transfer` is the one place that declaration is actually enforced against a real
    record, so a connector cannot accidentally return a value for a field it never declared.

    `raw_status` is the client's own word (`"Downloading"`, `"seeding"`, whatever the wire
    format actually says) preserved alongside the normalized `phase` -- display shows
    `raw_status`; logic reads `phase` (spec §3).
    """

    client_id: str
    name: str
    phase: TransferPhase
    raw_status: str
    raw: dict[str, Any] = field(repr=False)

    # -- Everything below is `Field`-governed: optional, `None` by default, and never set by a
    # connector that hasn't declared support for it (spec §2.2, enforced by `project_transfer`).
    content_path: str | None = None
    size_bytes: int | None = None
    bytes_done: int | None = None
    eta_s: int | None = None
    error_message: str | None = None
    category: str | None = None
    added_at: str | None = None
    completed_at: str | None = None
    # rTorrent reports per-mille -- a connector must divide by 1000 before this field is ever
    # populated (spec §2.2's own warning: "a rule comparing raw `d.ratio` against `1.0` treats
    # every torrent as wildly over-seeded"). This module does no such conversion itself; it is
    # exactly the kind of connector-owned unit fix `core/arrclient.py`'s own docstrings warn
    # against ever doing at the wrong layer.
    ratio: float | None = None
    uploaded_bytes: int | None = None
    seed_time_s: int | None = None
    # Hostnames only, never full announce URLs (spec §7.3 -- announce URLs embed per-user
    # passkeys). Populated only when `list_trackers` has actually been called for this item
    # (spec §2.2); `None` otherwise, never an empty tuple standing in for "not fetched".
    tracker_hosts: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ConnectionInfo:
    """`test_connection`'s result (spec §2.1) -- reachability, version, server identity. A
    connector raises rather than returning on failure (`ClientUnreachable`/`ClientError`,
    `core.clients.errors`); this type only ever represents success.
    """

    version: str | None = None
    server_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class TrackerInfo:
    """One entry from `list_trackers` (spec §2.1) -- **hostname only, never the full announce
    URL** (spec §7.3, §10.2's "capture mechanism... a new and very plausible way to leak one").
    A connector must extract just the host before this record is ever constructed; there is no
    field here to hold the full URL by design, not merely by convention -- there is nowhere to
    put one even by mistake.
    """

    host: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class SpaceInfo:
    """`free_space`'s result (spec §2.1, §12) -- free bytes always; `total_bytes` optional and
    `None` for every surveyed client except Transmission, the only one reporting it (spec §12).
    Never estimated when a connector's own API doesn't supply it -- absent means `None`, not a
    guess dressed up as a fact (the same discipline spec §4.4 states for capability-gated
    fields, applied here to a value with no capability gate of its own).
    """

    free_bytes: int
    total_bytes: int | None = None


@dataclass(frozen=True)
class RemoveOutcome:
    """The result of `remove` (spec §2.1, §10) -- unregister the item, **leave the data on
    disk**. `remove_with_data` does not exist in this vocabulary (spec §10.1), so this type has
    no field for "and the data too" -- lftpweb deletes the bytes itself, over SSH, as a
    separate step outside any connector (spec §10.2). `detail` carries the client's own wording
    for an audit event or an in-app banner.
    """

    succeeded: bool
    detail: str | None = None


@dataclass(frozen=True)
class BasePath:
    """One entry from `list_base_paths` (spec §2.1, §8.2) -- **a prefill, not the source of
    truth**. The user's own configured base paths (spec §8.2) are what the disk-review scan
    (spec §11) actually trusts; rTorrent's own `directory.default` will never mention the
    completed folder it hardlinks into (spec §1.1), so a client's own answer here is
    necessarily incomplete and no caller may treat this list as exhaustive.
    """

    path: str
    label: str | None = None


# A run of exactly 40 hex characters is a SHA-1 infohash's length -- the only id shape this
# codebase normalizes. SAB's `nzo_id` is a UUID with hyphens (`b67924d8-c0f0-4901-8941-
# 85ddbfef6179`, docs/transfers-redesign-spec.md §4.4) and can never satisfy this check, so it
# always passes through `normalize_client_id` untouched, exactly as spec §7.1 requires.
_INFOHASH_LENGTH = 40
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def normalize_client_id(client_id: str) -> str:
    """The one place a client id is case-normalized for storage and comparison (spec §7.1).

    Lowercases a hex infohash; leaves every other id form untouched. **Real production
    evidence for why this exists at all**: the *arr hands infohashes over uppercase --
    `12682AF0C00A061448BCFA16975A5D5F01A84A61`, observed in `arr_matched` events
    (docs/transfers-redesign-spec.md §4.4) -- while download clients vary in what case *they*
    report. Comparing case-insensitively by normalizing both sides to lowercase avoids a class
    of phantom-row bugs from a case mismatch that means nothing about the underlying identity.

    Nothing else is touched. A SAB `nzo_id` (a hyphenated UUID) and any future client's own id
    shape pass through completely unchanged -- this function's whole job is recognizing "this
    looks like a hex infohash," never reformatting an id it doesn't recognize.
    """
    if len(client_id) == _INFOHASH_LENGTH and all(c in _HEX_DIGITS for c in client_id):
        return client_id.lower()
    return client_id
