"""Vocabularies, capability declaration, the connector ABC, and baseline profiles
(docs/download-client-framework-spec.md §2, §4, §5) -- the pieces that make adding connector
#6 through #10 cheap rather than a re-litigation of what a "capability" even means.

**Two vocabularies, not one** (spec §2): `Operation` is a verb a client can be asked to
perform -- a missing one disables a *control* ("your client can't recheck"). `Field` is a fact
one normalized `Transfer` record can carry -- a missing one disables a *rule* ("your client
doesn't report seed time, so a 14-day rule is unavailable"). One flat list would serve neither
#18's settle-gate skip (almost entirely an `Operation` consumer) nor #21's ranking function
(almost entirely a `Field` consumer) well, and would force SABnzbd to declare `none` against a
wall of torrent verbs that were never about it.

**The capability declaration is three layers, only one of which lives in this module's own
data** (spec §4.1): *static* (a class attribute -- what this connector type could ever do,
before any connection exists), *probed* (refined at `test_connection` time by version
detection, persisted per-instance), and *runtime-degraded* (a capability that failed in use,
in-memory, cleared by the next successful probe). Only `static` is built here; stage 0 ships
no instance rows and no poller to drive the other two, but the *mechanism* for narrowing a
`CapabilitySet` (`narrowed_by`) and for degrading one safely from a real error
(`degrade_from_error`) is built and tested now, because it is cheaper to get the merge rule
right once here than to re-derive it once per connector later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, Literal

from .errors import CapabilityUnavailable
from .models import (
    BasePath,
    ConnectionInfo,
    RemoveOutcome,
    SpaceInfo,
    Transfer,
    TrackerInfo,
    TransferPhase,
)


# --------------------------------------------------------------------------------------------
# 1. The operation vocabulary (spec §2.1) -- a closed enum, not free strings. Ten connectors
# authored against free strings produce ten spellings of the same idea; a `StrEnum` member
# compares equal to its own string value, so this costs nothing at any call site that already
# expects a plain string (a log line, a dict key) while still being a closed, typo-proof set
# everywhere it matters.
#
# `add_transfer` is deliberately excluded, permanently (spec §2.1): lftpweb does not grab, the
# *arr does, and anything that writes the client's own configuration is out of scope.
# `remove_with_data` is deliberately absent (spec §10.1, §2.1): `remove` means *unregister,
# leave the data*, and lftpweb deletes the bytes itself over SSH as a separate step outside any
# connector (see `docs/decisions.md` for the full reasoning).
# --------------------------------------------------------------------------------------------
class Operation(StrEnum):
    TEST_CONNECTION = "test_connection"
    LIST_TRANSFERS = "list_transfers"
    LIST_HISTORY = "list_history"
    GET_TRANSFER = "get_transfer"
    LIST_TRACKERS = "list_trackers"
    LIST_FILES = "list_files"
    LIST_BASE_PATHS = "list_base_paths"
    FREE_SPACE = "free_space"
    PAUSE = "pause"
    RESUME = "resume"
    REMOVE = "remove"
    SET_LABEL = "set_label"
    RECHECK = "recheck"


# --------------------------------------------------------------------------------------------
# 2. The field vocabulary (spec §2.2) -- what one normalized `Transfer` record can carry,
# beyond the four fields that are mandatory for every connector and therefore never declared
# (`client_id`, `name`, `phase`, `raw_status` -- see `models.Transfer`'s own docstring). Every
# member's string value is deliberately identical to the `Transfer` attribute it governs
# (`Field.CONTENT_PATH == "content_path" == Transfer.content_path`'s name) -- this is what lets
# `project_transfer` below use `getattr`/`setattr` generically across every field without a
# second name-mapping table that could drift from the dataclass.
# --------------------------------------------------------------------------------------------
class Field(StrEnum):
    CONTENT_PATH = "content_path"
    SIZE_BYTES = "size_bytes"
    BYTES_DONE = "bytes_done"
    ETA_S = "eta_s"
    ERROR_MESSAGE = "error_message"
    CATEGORY = "category"
    ADDED_AT = "added_at"
    COMPLETED_AT = "completed_at"
    RATIO = "ratio"
    UPLOADED_BYTES = "uploaded_bytes"
    SEED_TIME_S = "seed_time_s"
    TRACKER_HOSTS = "tracker_hosts"


class Support(StrEnum):
    """Tri-state support level for one `Operation` or `Field` (spec §4.3):
    `docs/download-client-api-survey.md` §4.1's conclusion, generalized to both vocabularies.
    """

    NATIVE = "native"
    DERIVED = "derived"
    NONE = "none"


# Ranks `Support` for the narrowing check `narrowed_by` performs -- a layer may only move a key
# to an equal-or-lower rank, never raise it (spec §4.1: "Merging is narrowing-only").
_SUPPORT_RANK: dict[Support, int] = {Support.NONE: 0, Support.DERIVED: 1, Support.NATIVE: 2}


@dataclass(frozen=True)
class Capability:
    """One `Operation` or `Field`'s declared support level, plus an optional caveat.

    `note` exists because a `DERIVED` value's *semantics* can differ from what a caller would
    assume the field means (spec §4.3). The canonical case, restated here because it is the
    reason this field exists at all: rTorrent has no seed-time field, and deriving one from
    `d.timestamp.finished` measures *wall-clock since completion* -- a stopped torrent still
    accrues. A site rule meaning "actually seeding for 14 days" cannot be honored faithfully on
    that connector, and `note` is where the UI learns to say so where the user writes the rule,
    rather than quietly redefining it.
    """

    support: Support
    note: str | None = None


@dataclass(frozen=True)
class CapabilitySet:
    """An immutable declaration of support for every `Operation` and every `Field`.

    Two distinct ways to build one from another, matching two genuinely different situations
    (spec §4.1, §5) that must not be conflated:

    - `overridden` -- **authoring time**, unconstrained. A connector class body builds its own
      static declaration by starting from `USENET_BASELINE`/`TORRENT_BASELINE` (spec §5) and
      overriding a handful of entries either direction (a baseline says `NONE`, this connector
      actually has it `NATIVE`, or vice versa). There is no "narrowing" rule here because there
      is no prior *runtime* fact being contradicted -- it is simply how the connector's own
      static truth is written down.
    - `narrowed_by` -- **runtime layering** (spec §4.1's probed and runtime-degraded layers).
      A layer here may only lower a key's support, never raise it: a `test_connection` probe
      finding a plugin missing can turn `NATIVE` into `NONE`; it can never turn a baseline's
      `NONE` into something better, because a probe or a runtime failure is not evidence a
      capability exists that the static declaration didn't already claim.

    `supports` is the one query every caller uses -- nothing outside this module (or a test)
    should ever read `.operations`/`.fields` directly to decide whether to do something.
    """

    operations: Mapping[Operation, Capability]
    fields: Mapping[Field, Capability]

    def supports(self, key: Operation | Field, *, accept_derived: bool = False) -> bool:
        """Whether a caller may rely on `key` right now.

        `NATIVE` always answers yes. `DERIVED` answers yes only when the caller opted in via
        `accept_derived=True` -- the spec §4.3 split between "native only"
        (`caps.supports(Field.SEED_TIME_S)`) and "derived is good enough"
        (`caps.supports(Field.SEED_TIME_S, accept_derived=True)`). An undeclared key (should
        never happen for a conformant connector; the conformance suite asserts every key is
        always present) answers `False` rather than raising, the same "absent means no" default
        `NONE` itself gets.
        """
        table: Mapping[Operation | Field, Capability] = (
            self.operations if isinstance(key, Operation) else self.fields
        )
        cap = table.get(key)
        if cap is None:
            return False
        if cap.support is Support.NATIVE:
            return True
        if cap.support is Support.DERIVED:
            return accept_derived
        return False

    def overridden(
        self,
        *,
        operations: Mapping[Operation, Capability] | None = None,
        fields: Mapping[Field, Capability] | None = None,
    ) -> CapabilitySet:
        """Build a new declaration from this one, replacing the given entries freely in either
        direction (see class docstring). This is how `TORRENT_BASELINE` is built from
        `USENET_BASELINE` below, and how a real connector is expected to build its own static
        `capabilities` from whichever baseline fits it best (spec §5).
        """
        new_operations = dict(self.operations)
        new_operations.update(operations or {})
        new_fields = dict(self.fields)
        new_fields.update(fields or {})
        return CapabilitySet(
            operations=MappingProxyType(new_operations), fields=MappingProxyType(new_fields)
        )

    def narrowed_by(
        self,
        *,
        operations: Mapping[Operation, Capability] | None = None,
        fields: Mapping[Field, Capability] | None = None,
    ) -> CapabilitySet:
        """Apply a probed or runtime-degraded layer on top of this one (spec §4.1). Every key
        named in `operations`/`fields` must already be declared in this set, and its `Support`
        may only move to an equal or lower rank -- raises `ValueError` on an attempt to raise
        it, and `KeyError` on a key this set never declared in the first place (both are
        authoring bugs, never a real runtime outcome).
        """
        return CapabilitySet(
            operations=_narrow_table(self.operations, operations or {}),
            fields=_narrow_table(self.fields, fields or {}),
        )


def _narrow_table(
    base: Mapping[Any, Capability], overrides: Mapping[Any, Capability]
) -> MappingProxyType[Any, Capability]:
    result = dict(base)
    for key, new_cap in overrides.items():
        current = result.get(key)
        if current is None:
            raise KeyError(f"cannot narrow undeclared key {key!r}")
        if _SUPPORT_RANK[new_cap.support] > _SUPPORT_RANK[current.support]:
            raise ValueError(
                f"{key!r}: a narrowing layer may only lower support, not raise it "
                f"({current.support!s} -> {new_cap.support!s})"
            )
        result[key] = new_cap
    return MappingProxyType(result)


def degrade_from_error(
    capabilities: CapabilitySet, key: Operation | Field, exc: Exception, *, note: str | None = None
) -> CapabilitySet:
    """The **only** sanctioned way to apply spec §4.1's runtime-degraded layer -- structural
    enforcement of spec §4.2's "a transport failure must never degrade a capability", the
    single most important rule in this stage.

    If `exc` is not a `CapabilityUnavailable`, `capabilities` is returned completely unchanged:
    a `ClientUnreachable` or a bare `ClientError` carries no information about what the client
    supports, and must never flip a capability off (this is the rule the v0.2.4 SABnzbd
    blank-queue incident, `docs/download-client-framework-spec.md` §1, exists to generalize
    past). Only when `exc` actually is a `CapabilityUnavailable` does `key`'s support drop to
    `Support.NONE` in the returned set. This function only ever narrows -- via `narrowed_by`,
    so the same "may only lower, never raise" guarantee applies here too -- and it knows
    nothing about the database or an audit trail; the caller that owns those (stage 2's
    poller) is responsible for writing the accompanying audit event itself, exactly as
    spec §1's "a connector is handed no database handle" requires.
    """
    if not isinstance(exc, CapabilityUnavailable):
        return capabilities
    degraded = Capability(support=Support.NONE, note=note or str(exc) or None)
    if isinstance(key, Operation):
        return capabilities.narrowed_by(operations={key: degraded})
    return capabilities.narrowed_by(fields={key: degraded})


def project_transfer(transfer: Transfer, capabilities: CapabilitySet) -> Transfer:
    """Null out any `Field` this connector's own `capabilities` says it cannot populate (spec
    §2.2: "a connector must never declare a field it cannot populate... a field declared and
    returned `None` is worse than one declared absent: a consumer offers a rule that silently
    never matches").

    Every connector's raw-response parser is expected to build a `Transfer` from whatever the
    client handed back and run it through this before returning it from `list_transfers`/
    `list_history`/`get_transfer` -- turning spec §2.2's assertion from "hope every connector's
    own code remembers this" into a structural guarantee applied once, here, for all of them
    (the same move spec §1 makes for "a connector cannot write `item.state`": enforce it in the
    shape of the code, not by author discipline). The conformance suite
    (`tests/test_clients_framework.py`) checks this function against every registered
    connector's own declared `capabilities`.

    Mandatory fields (`client_id`, `name`, `phase`, `raw_status`) and `raw` have no `Field`
    entry and are never touched -- they are not subject to capability declaration at all
    (`models.Transfer`'s own docstring).
    """
    updates: dict[str, Any] = {}
    for member in Field:
        cap = capabilities.fields.get(member)
        support = cap.support if cap is not None else Support.NONE
        if support is Support.NONE and getattr(transfer, member.value) is not None:
            updates[member.value] = None
    return replace(transfer, **updates) if updates else transfer


# --------------------------------------------------------------------------------------------
# 3. Baseline profiles (spec §5) -- "the features are close to the same except ratios/etc",
# decided with the user 2026-08-22. A connector starts from one of these and calls
# `.overridden(...)` for its own handful of differences (~3 lines) instead of writing the full
# 25-entry declaration from scratch (~25 lines) -- the mechanism that keeps 7-10 connectors
# cheap. Both baselines declare **every** `Operation` and `Field` key, deliberately: a connector
# that overrides nothing still ends up with a complete declaration, which is what the
# conformance suite's "every key is declared" check depends on.
# --------------------------------------------------------------------------------------------

# "queue, history, categories, paths, free space, pause, remove. No ratio, no seed time, no
# trackers, no recheck" (spec §5). The spec states this in prose, by category, not as an
# exact per-key table -- the mapping from that prose onto the closed enum's precise members
# below (e.g. `LIST_FILES`, `SET_LABEL`, `RESUME`) is this module's own reasonable reading of
# it, not a literal transcription, and is called out as such in the stage-0 report.
USENET_BASELINE = CapabilitySet(
    operations=MappingProxyType(
        {
            Operation.TEST_CONNECTION: Capability(Support.NATIVE),
            Operation.LIST_TRANSFERS: Capability(Support.NATIVE, note="the queue"),
            Operation.LIST_HISTORY: Capability(
                Support.NATIVE, note="native on usenet clients (spec §2.1)"
            ),
            Operation.GET_TRANSFER: Capability(
                Support.DERIVED, note="filtered from list_transfers (spec §2.1)"
            ),
            Operation.LIST_TRACKERS: Capability(Support.NONE, note="usenet has no trackers"),
            Operation.LIST_FILES: Capability(Support.NATIVE),
            Operation.LIST_BASE_PATHS: Capability(Support.NATIVE, note="the completed folder"),
            Operation.FREE_SPACE: Capability(Support.NATIVE),
            Operation.PAUSE: Capability(Support.NATIVE),
            Operation.RESUME: Capability(Support.NATIVE),
            Operation.REMOVE: Capability(Support.NATIVE),
            Operation.SET_LABEL: Capability(Support.NATIVE, note="categories"),
            Operation.RECHECK: Capability(Support.NONE, note="torrent-only operation (spec §2.1)"),
        }
    ),
    fields=MappingProxyType(
        {
            Field.CONTENT_PATH: Capability(
                Support.NATIVE, note="carried by list_history (spec §2.1)"
            ),
            Field.SIZE_BYTES: Capability(Support.NATIVE),
            Field.BYTES_DONE: Capability(Support.NATIVE),
            Field.ETA_S: Capability(Support.NATIVE),
            Field.ERROR_MESSAGE: Capability(Support.NATIVE),
            Field.CATEGORY: Capability(Support.NATIVE),
            Field.ADDED_AT: Capability(Support.NATIVE),
            Field.COMPLETED_AT: Capability(Support.NATIVE),
            Field.RATIO: Capability(Support.NONE, note="no ratio (spec §5)"),
            Field.UPLOADED_BYTES: Capability(Support.NONE, note="torrent-only concept"),
            Field.SEED_TIME_S: Capability(Support.NONE, note="no seed time (spec §5)"),
            Field.TRACKER_HOSTS: Capability(Support.NONE, note="no trackers (spec §5)"),
        }
    ),
)

# "the above plus ratio, uploaded bytes, seed time, trackers, recheck" (spec §5) --
# built from `USENET_BASELINE` via `overridden`, the exact reuse mechanism spec §5 exists for.
TORRENT_BASELINE = USENET_BASELINE.overridden(
    operations={
        Operation.LIST_HISTORY: Capability(
            Support.DERIVED, note="a torrent never leaves the list (spec §2.1)"
        ),
        Operation.LIST_TRACKERS: Capability(Support.NATIVE),
        Operation.RECHECK: Capability(Support.NATIVE),
    },
    fields={
        Field.RATIO: Capability(Support.NATIVE),
        Field.UPLOADED_BYTES: Capability(Support.NATIVE),
        Field.SEED_TIME_S: Capability(Support.NATIVE),
        Field.TRACKER_HOSTS: Capability(
            Support.NATIVE, note="populated only once list_trackers has been called (spec §2.2)"
        ),
    },
)


# --------------------------------------------------------------------------------------------
# 4. The declared connection-config schema (spec §8.1) -- enough for Settings to render one
# generic form per connector, instead of ten hand-authored ones. Kept intentionally minimal in
# stage 0 (a list of field descriptors); stage 1's SABnzbd adapter is the first real consumer,
# and a settings API/UI is out of scope here entirely.
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigField:
    """One entry in a connector's declared connection-config schema (spec §8.1). `kind`
    is deliberately a small closed set -- enough for a generic form renderer to pick a widget
    (a plain text box, a checkbox, a password box for `"secret"`) without knowing anything
    about the connector that declared it.
    """

    key: str
    label: str
    kind: Literal["str", "int", "bool", "secret"]
    required: bool = True
    default: Any = None
    help_text: str | None = None


# --------------------------------------------------------------------------------------------
# 5. The connector ABC.
# --------------------------------------------------------------------------------------------
class DownloadClient(ABC):
    """One connector: a module that talks to one kind of download client
    (docs/download-client-framework-spec.md §1).

    **`__init__` takes connection config only -- never a database connection or a session
    factory** (spec §1). This is spec §4.1's "advisory only" rule enforced structurally rather
    than by discipline: a connector cannot write `item.state` because it has nothing to write
    to. A future change that wants to pass a database handle into a connector's constructor is
    this rule being violated, and should be read as such, not accepted as a convenience.

    `family` is display metadata **only** -- it groups the settings picker and picks a default
    config form, and it must **never** appear in a capability decision (spec §5.1). The moment
    something reads `if family == "torrent": has_labels`, a Deluge install without the label
    plugin is silently broken, which is precisely the class of bug `CapabilitySet` exists to
    prevent. `capabilities` is the only thing any caller may ever branch on.

    `capabilities` is a **class attribute** (spec §4.1's static layer: "renders 'if you
    configured a qBittorrent you'd get…' before any connection exists") -- typically built from
    `USENET_BASELINE`/`TORRENT_BASELINE` via `.overridden(...)`. Stage 0 ships no instance rows
    and no poller, so the probed and runtime-degraded layers (`narrowed_by`,
    `degrade_from_error`) exist and are tested, but nothing in this module composes them onto a
    live instance yet -- that is stage 1/2's job, once there is an instance row to persist a
    probed result onto in the first place.
    """

    family: ClassVar[Literal["usenet", "torrent"]]
    client_type: ClassVar[str]  # set by the `@register_client(...)` decorator, never by hand
    capabilities: ClassVar[CapabilitySet]
    config_schema: ClassVar[tuple[ConfigField, ...]] = ()

    def __init__(self, *, config: Mapping[str, Any]) -> None:
        self.config = config

    @staticmethod
    @abstractmethod
    def map_phase(raw_status: str) -> TransferPhase:
        """Map this client's own status vocabulary onto `TransferPhase` (spec §3).

        **Total**: must never raise on an unrecognized status string of any kind (including
        one this codebase has genuinely never seen), and must return `TransferPhase.UNKNOWN`
        for anything it doesn't recognize -- spec §3's "unknown never blocks anything" as a
        guarantee every connector's own mapping must uphold, not merely a convention.
        """

    @abstractmethod
    async def test_connection(self) -> ConnectionInfo:
        """Reachability, version, server identity (spec §2.1). Mandatory for every connector."""

    @abstractmethod
    async def list_transfers(self, *, active_only: bool = False) -> list[Transfer]:
        """Everything the client knows about, normalized (spec §2.1, §3). Mandatory for every
        connector. `active_only` is the fast-cadence filter spec §9.1 needs (`list_transfers
        (active_only=True)` on the ~10s poll) -- a connector for which "active" isn't a
        meaningful distinction may ignore the flag and always return everything.
        """

    @abstractmethod
    async def list_history(self) -> list[Transfer]:
        """Finished/failed work, carrying the real on-disk path (spec §2.1) -- native on usenet
        clients, derived (filtered from `list_transfers`) on torrent clients, since a torrent
        never leaves the list.
        """

    @abstractmethod
    async def get_transfer(self, client_id: str) -> Transfer | None:
        """Exact lookup by client id -- the *arr's own `downloadId` (spec §2.1, §7.1). Often
        derived (filter `list_transfers`'s own result) rather than a dedicated API call.
        """

    @abstractmethod
    async def list_trackers(self, client_id: str) -> list[TrackerInfo]:
        """One item's announce hosts (spec §2.1) -- its own operation, not a field, because it
        can be an N-call fetch (qBittorrent, rTorrent) a caller must be able to decide not to
        pay for.
        """

    @abstractmethod
    async def list_files(self, client_id: str) -> list[str]:
        """One item's file list (spec §2.1)."""

    @abstractmethod
    async def list_base_paths(self) -> list[BasePath]:
        """The client's own configured download/complete directories (spec §2.1, §8.2) -- a
        prefill, never treated as the complete or authoritative set of roots.
        """

    @abstractmethod
    async def free_space(self, path: str) -> SpaceInfo:
        """Free bytes for a path, and total where reported (spec §2.1, §12). No client reports
        quota -- quota is out of scope for this framework entirely (spec §12).
        """

    @abstractmethod
    async def pause(self, client_id: str) -> None:
        """Pause one item (spec §2.1). qBittorrent renamed this to `stop` at API v2.11 (5.0) --
        that is an internal implementation detail of the connector that calls it, never
        something this vocabulary's own name changes to track.
        """

    @abstractmethod
    async def resume(self, client_id: str) -> None:
        """Resume one item (spec §2.1)."""

    @abstractmethod
    async def remove(self, client_id: str) -> RemoveOutcome:
        """Unregister the item, **leave the data on disk** (spec §2.1, §10) -- the only removal
        verb a connector has. `remove_with_data` does not exist in this vocabulary.
        """

    @abstractmethod
    async def set_label(self, client_id: str, label: str) -> None:
        """Category/label assignment (spec §2.1)."""

    @abstractmethod
    async def recheck(self, client_id: str) -> None:
        """Re-verify data against the torrent (spec §2.1). Torrent clients only -- a usenet
        connector declares `Operation.RECHECK` as `Support.NONE` and may implement this by
        raising `CapabilityUnavailable` immediately, since no caller should ever invoke it
        without first checking `capabilities.supports(Operation.RECHECK)`.
        """
