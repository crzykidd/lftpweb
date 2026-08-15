"""Pydantic models for API shapes. Only what this phase's endpoints return — the domain
is not modeled speculatively ahead of the phases that need it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Input length caps (audit S3, docs/audit-v0.1.0.md). Chosen deliberately *generous* -- large
# enough that no legitimate value is ever rejected, small enough that an attacker can't hand the
# server a multi-megabyte body to chew on (the concrete worry being an argon2-hashed password
# field on the unauthenticated login path). These bound only absurd inputs, never real ones.
MAX_SECRET_LEN = 4096  # a password / passphrase -- real ones are tiny; this only stops a DoS body
MAX_KEY_LEN = (
    65536  # a pasted PEM private key: a few KB in practice, 64 KB is far past any real key
)
MAX_NAME_LEN = 1024  # a display name, username, hostname/address, header name, glob/pattern expr
MAX_PATH_LEN = 4096  # a filesystem/remote path (PATH_MAX on Linux is 4096)
MIN_PORT = 1
MAX_PORT = 65535


class HealthResponse(BaseModel):
    status: str
    version: str
    db: bool
    uptime_s: float
    # Not in DESIGN.md §12's literal {status, version, db, uptime_s} shape. Added because
    # the nav's version link (§9.1) needs LFTPWEB_REPO_URL at *runtime* (it's a container
    # env var, set after the SPA is already built into static files) and /api/health is
    # already the request the UI makes to render the version — see docs/decisions.md.
    repo_url: str
    # Phase 7, DESIGN.md §10.3: "reports DB reachability, host reachability, and whether the
    # scheduler loop is alive." Added, not replacing anything above -- the container
    # HEALTHCHECK only checks the HTTP status code (docker/Dockerfile), never this body, so
    # widening the shape can't change container restart behavior. `None` = no host configured
    # yet (a fresh install), distinct from `False` (a host is configured but unreachable).
    host_reachable: bool | None = None
    scheduler_alive: bool = True


class StatsResponse(BaseModel):
    """The header-bar shape from DESIGN.md §9.1. Zeros in this phase; wired for real once
    the scheduler (phase 3) has numbers to report.
    """

    current_speed_bps: int
    allocated_bps: int
    ceiling_bps: int
    queued_count: int
    queued_bytes: int
    transferred_24h_bytes: int


# --- Settings -> Connection (DESIGN.md §3.1 `host`, §8, §9.2) --------------------------

AuthMethod = Literal["key", "agent", "password"]
KnownHostsPolicy = Literal["accept-and-pin", "strict", "insecure"]


class HostIn(BaseModel):
    """A create/update request for the (single, v1) seedbox host. `password` and `ssh_key` are
    plaintext here — the only place either ever appears in a request body — and are encrypted
    at rest (`core/crypto.py`) before they touch the database; neither is ever included in any
    response (DESIGN.md §9.2: "must never round-trip the stored secret back to the browser").

    `ssh_key` (migration 014, DESIGN.md §8) is an *additional* way to satisfy `auth_method =
    'key'`, alongside `key_path` — not a replacement. When both are set on save, the pasted key
    wins (`api/settings.py.put_host`); `key_path` keeps working untouched for anyone already
    mounting a key file.
    """

    name: str = Field(max_length=MAX_NAME_LEN)
    address: str = Field(max_length=MAX_NAME_LEN)
    port: int = Field(default=22, ge=MIN_PORT, le=MAX_PORT)
    username: str = Field(max_length=MAX_NAME_LEN)
    auth_method: AuthMethod
    key_path: str | None = Field(default=None, max_length=MAX_PATH_LEN)
    password: str | None = Field(default=None, max_length=MAX_SECRET_LEN)
    ssh_key: str | None = Field(default=None, max_length=MAX_KEY_LEN)
    known_hosts_policy: KnownHostsPolicy = "accept-and-pin"


class HostOut(BaseModel):
    id: int
    name: str
    address: str
    port: int
    username: str
    auth_method: AuthMethod
    key_path: str | None
    has_password: bool
    # migration 014: whether a key is currently stored, mirroring `has_password` — never the
    # key itself (DESIGN.md §9.2).
    has_ssh_key: bool = False
    # Which of `key_path` / a pasted key is actually in use for `auth_method = 'key'` — the
    # coexistence rule (pasted wins) lives in exactly one place, `api/settings.py`, so the UI
    # never has to re-derive it. `None` when `auth_method != 'key'` or neither is set.
    active_key_source: Literal["pasted", "path"] | None = None
    known_hosts_policy: KnownHostsPolicy
    credentials_need_reentry: bool = False
    # Read-only: DESIGN.md §4.5/§9.3 calls `net:connection-limit` "a first-class setting,
    # host-level" -- it isn't (see core/remote.py.parse_connection_limit and
    # docs/decisions.md, 2026-08-12). This surfaces whatever is currently in the
    # `connection_overrides` JSON blob, if anything; there is no `HostIn` field to set it.
    net_connection_limit: int | None = None


class HostTestRequest(BaseModel):
    """*Test connection* against either the saved host (omit every field) or a form the
    user hasn't saved yet (fill in what should override the stored row) — so the UI can
    test before committing. `password = None` means "use the currently stored password";
    `ssh_key = None` means the same for a pasted key.
    """

    name: str | None = Field(default=None, max_length=MAX_NAME_LEN)
    address: str | None = Field(default=None, max_length=MAX_NAME_LEN)
    port: int | None = Field(default=None, ge=MIN_PORT, le=MAX_PORT)
    username: str | None = Field(default=None, max_length=MAX_NAME_LEN)
    auth_method: AuthMethod | None = None
    key_path: str | None = Field(default=None, max_length=MAX_PATH_LEN)
    password: str | None = Field(default=None, max_length=MAX_SECRET_LEN)
    ssh_key: str | None = Field(default=None, max_length=MAX_KEY_LEN)
    known_hosts_policy: KnownHostsPolicy | None = None


class TestConnectionResponse(BaseModel):
    ok: bool
    error_class: str | None
    message: str


# --- Settings -> Queues (DESIGN.md §3.1 `path_queue`) -----------------------------------

SyncMode = Literal["copy", "move", "sync"]


class PathQueueIn(BaseModel):
    name: str = Field(max_length=MAX_NAME_LEN)
    remote_path: str = Field(max_length=MAX_PATH_LEN)
    local_path: str = Field(max_length=MAX_PATH_LEN)
    staging_path: str | None = Field(default=None, max_length=MAX_PATH_LEN)
    enabled: bool = True
    sync_mode: SyncMode = "copy"
    # DESIGN.md §4.7, phase 4. Both default off/false -- enabling auto-queue is an explicit
    # user action; a queue created without specifying these fields must not auto-enable
    # itself (this phase's non-negotiable, docs/decisions.md).
    auto_queue_enabled: bool = False
    auto_queue_patterns_only: bool = False
    # DESIGN.md §6, phase 5; nullable-for-inherit as of 2026-08-13
    # (`prompts/2026-08-13-postprocess-inherit-or-override.md`). `None` (the default, and what
    # a freshly-created queue gets, same as migration 015 sets for every existing queue) means
    # "inherit the matching `PostprocessSettings` site-wide flag" -- `core/postprocess.py`'s
    # `_effective()`. An explicit `True`/`False` is a per-queue override, independent of the
    # site-wide flag in either direction; it is no longer ANDed with it, which could only ever
    # narrow "on" toward "off." `auto_verify`/`auto_extract` are existing DB columns (migration
    # 001) that had no API/UI field until phase 5; `auto_move` is new there (migration 003).
    # `api/settings.py._effective_auto_verify` forces `auto_verify` to an explicit `True`
    # whenever `sync_mode == 'move'` regardless of what's sent here (DESIGN.md §6: "forced on
    # and cannot be turned off in the UI") -- it is the sole gate on an irreversible delete, and
    # forcing an explicit override (not leaving it on inherit) means a later site-wide change
    # can never silently turn it off for a `move` queue.
    auto_verify: bool | None = None
    auto_extract: bool | None = None
    auto_move: bool | None = None
    # Migration 012 (prompts/2026-08-13-per-queue-archive-cleanup.md); nullable-for-inherit
    # alongside the three above as of the 2026-08-13 task cited above.
    # `PostprocessSettings.delete_archives_after_extract` shipped site-only (migration 010) and
    # was the odd one out; this brings it in line with the other three's "toggleable globally
    # and per path queue" shape (DESIGN.md §6).
    auto_delete_archives: bool | None = None
    # Migration 009 (prompts/done/2026-08-12-per-queue-scan-interval.md). `None` (the default,
    # and what an existing queue's row already has -- `ADD COLUMN` with no `DEFAULT` leaves it
    # NULL) means "use the site-wide `scan_interval_s` default (`config.py`, currently 30s)" --
    # the same "a new capability changes nothing for an existing install" rule every other field
    # on this model already follows. `0` means on-demand only (no timer at all -- still
    # scannable via "Rescan now" / `POST /api/files/rescan`); any positive number is a literal
    # per-queue interval in seconds. `api/settings.py._reject_invalid_scan_interval` rejects a
    # negative value with a 400 before it ever reaches the DB's own `CHECK`.
    scan_interval_s: float | None = None
    # Migration 017 ("folder prefix during transfer", `core/download_prefix.py`). Both
    # nullable-for-inherit, resolved independently (`3500b3f`'s shape, never an AND): `None`
    # means "inherit the matching Settings -> Transfer field," an explicit value is this
    # queue's own override. `download_prefix_enabled` defaults `None` (inherit) same as every
    # other new-capability field on this model; `download_prefix` defaults `None` (inherit the
    # site-wide prefix string) rather than the literal default prefix, so a queue that has never
    # touched this setting tracks the site default if it's ever changed, instead of freezing
    # whatever the default happened to be when the queue was created.
    download_prefix_enabled: bool | None = None
    download_prefix: str | None = None


class PathQueueOut(PathQueueIn):
    id: int
    host_id: int


# --- Settings -> Post-processing (DESIGN.md §6, phase 5) --------------------------------


class PostprocessSettingsOut(BaseModel):
    verify_enabled: bool
    verify_hash_on_disk: bool
    extract_enabled: bool
    extract_target_dir: str | None
    extract_passwords: list[str]
    # `_FAILED_` staging directory retention (core/extract.py) -- fix, 2026-08-12
    # (docs/decisions.md). Off by default; see core/postprocess.py.PostprocessSettings. Field
    # defaults (unlike every other field on this model) so a PUT body from before this fix
    # existed keeps defaulting off rather than 422ing on a field it doesn't know about.
    failed_retention_enabled: bool = False
    failed_retention_days: float = 14.0
    # Delete an item's spent archive volumes after a successful extraction (2026-08-13,
    # docs/decisions.md). Off by default -- see core/postprocess.py.PostprocessSettings. Same
    # "field defaults so an old PUT body doesn't 422" reasoning as failed_retention_enabled
    # above.
    delete_archives_after_extract: bool = False
    move_enabled: bool
    concurrency: int


class PostprocessSettingsIn(PostprocessSettingsOut):
    """`PUT /api/settings/postprocess`'s body. **Any field this pair's own defaults cover
    (`failed_retention_enabled`/`_days`, `delete_archives_after_extract`) that is genuinely
    absent from the request JSON is merged with the previously-stored value, not silently reset
    to the default above** (`api/settings.py.put_postprocess_settings`, fix,
    2026-08-13/`prompts/2026-08-13-per-queue-archive-cleanup.md` -- found because
    `failed_retention_enabled`/`_days` have no frontend field at all yet, so every save from
    Settings -> Post-processing was already discarding them on every single request, not just a
    hypothetical race). Every other field is required (no default), so FastAPI 422s before the
    handler ever runs if one is missing -- the merge only ever has work to do for the pair
    above, via `body.model_fields_set` (pydantic v2: which keys the *request* actually carried,
    not which fields merely have a value).
    """


# --- Settings -> the settle gate (prompts/open-issues.md #2, `core/settle.py`) ----------


class SettleSettingsOut(BaseModel):
    # Defaults **on** as of prompts/2026-08-12-settle-gate-followups.md -- see
    # core/settle.py.SettleSettings's own docstring for the full reasoning, and
    # CHANGELOG.md's `### Changed` entry: this is a behavior change for existing installs,
    # not a new-install-only default.
    enabled: bool = True
    # Read-only, informational -- not accepted on `SettleSettingsIn`. `core/settle.py`'s
    # `REQUIRED_SETTLE_SCANS`/`SETTLE_MIN_AGE_S` are named constants, not per-install settings
    # (see that module's own comment on why both are load-bearing and neither is meant to be
    # tuned away independently); surfaced here so Settings -> Transfer can explain what the
    # gate actually requires without hardcoding numbers in the frontend that could drift out
    # of sync with the code. The endpoint always fills these from the real constants, never
    # from a stored value -- the defaults here are only for the OpenAPI schema.
    required_scans: int = 2
    min_age_s: float = 60.0


class SettleSettingsIn(BaseModel):
    enabled: bool = True


# --- Settings -> the removal grace period (`core/mount_sentinel.py`, DESIGN.md §7.3) -----


class RemovalGraceSettingsOut(BaseModel):
    """Read-only, GET-only -- there is no `In` counterpart. `core/mount_sentinel.py.
    DEFAULT_GRACE_S` is "not user-configurable this phase" by that module's own comment, so
    unlike `SettleSettingsOut`'s `enabled` this has nothing to accept; the endpoint always
    fills `grace_s` from the real constant, never from a stored value -- the default here is
    only for the OpenAPI schema. Exists so the Files page's removal-grace countdown
    (2026-08-14, prompts/2026-08-14-removal-grace-countdown.md) can read the real grace window
    instead of a second, hand-maintained 600 baked into the frontend, the same drift risk
    `SettleSettingsOut.required_scans`/`min_age_s` already exists to avoid for the settle gate.
    """

    grace_s: float = 600.0
    # The states the grace clock can actually be running for -- `core/mount_sentinel.py.
    # COMPLETE_STATES` (`DOWNLOADED` plus the post-processing outcomes), shipped for the same
    # reason `grace_s` is: the frontend's countdown has to know which rows are eligible, and a
    # hand-maintained copy of that set in TypeScript is a set that drifts the moment a new
    # post-processing state is added on the Python side. Sent sorted so the response is stable.
    eligible_states: list[str] = []


# --- Settings -> "folder prefix during transfer" (`core/download_prefix.py`) -----------


class DownloadPrefixSettingsOut(BaseModel):
    # Off by default -- this project's rule for every new capability
    # (`prompts/startnewsession.md`); see `core/download_prefix.py.DownloadPrefixSettings`'s
    # own docstring for why this one wasn't given the settle gate's "ships on" exception.
    enabled: bool = False
    prefix: str = ".downloading-"  # matches core/download_prefix.py.DEFAULT_PREFIX


class DownloadPrefixSettingsIn(DownloadPrefixSettingsOut):
    pass


# --- Settings -> auto-queue (`core/autoqueue.py.AutoQueueSettings`) ---------------------


class AutoQueueSettingsOut(BaseModel):
    # Default False -- see core/autoqueue.py.AutoQueueSettings's own docstring for why this is
    # the *correct* default, not merely the cautious one: on for a copy-mode queue means an
    # item something outside lftpweb removed (an importer, a human, a script) is re-fetched on
    # the next scan its pattern still matches, forever, until the remote copy is also gone.
    re_download_externally_removed: bool = False


class AutoQueueSettingsIn(AutoQueueSettingsOut):
    pass


# --- Settings -> local retention (prompts/open-issues.md "7 + 8", `core/local_delete.py`) -----


class RetentionSettingsOut(BaseModel):
    # Defaults off -- non-negotiable (core/local_delete.py.RetentionSettings's own docstring):
    # this deletes the user's own data, and deletion is not where this project makes its one
    # "ships on" exception (scheduled backups).
    enabled: bool = False
    retention_days: float = 30.0


class RetentionSettingsIn(RetentionSettingsOut):
    """`PUT /api/settings/retention`'s body. Both fields above default, so `api/settings.py.
    put_retention_settings` merges over the previously-stored settings for any field genuinely
    absent from the request JSON rather than resetting it -- the same fix, for the same reason,
    as `PostprocessSettingsIn`'s own docstring."""


class OrphanTempCleanupSettingsOut(BaseModel):
    """`core/local_delete.py.OrphanTempCleanupSettings` (2026-08-13,
    prompts/2026-08-13-lftp-timestamped-temp-files.md). Defaults off -- same non-negotiable
    reason `RetentionSettingsOut` above has: this deletes files from disk, even though what it
    deletes is accidental byte waste with no diagnostic value.
    """

    enabled: bool = False
    max_age_days: float = 2.0


class OrphanTempCleanupSettingsIn(OrphanTempCleanupSettingsOut):
    """`PUT /api/settings/orphan-temp-cleanup`'s body -- both fields default, so
    `api/settings.py.put_orphan_temp_cleanup_settings` merges over the previously-stored
    settings for any field genuinely absent from the request JSON, the same fix (and the same
    reason) as `RetentionSettingsIn`'s own docstring."""


class RetentionPreviewRequest(BaseModel):
    """Mirrors `PatternPreviewRequest`'s idiom: preview against a not-yet-saved value.
    `retention_days=None` previews the currently saved setting instead.
    """

    retention_days: float | None = None


class RetentionPreviewItem(BaseModel):
    item_id: int
    queue_id: int
    queue_name: str
    rel_path: str
    local_size: int | None
    downloaded_at: str | None


class RetentionPreviewResponse(BaseModel):
    retention_days: float
    count: int
    total_bytes: int
    items: list[RetentionPreviewItem]


class DeleteItemResponse(BaseModel):
    """`POST /api/items/{item_id}/delete` -- the first delete endpoint in this API (DESIGN.md
    §9.2's Files-page "Delete local"). A withheld guard raises `HTTPException` instead of
    returning `deleted=False` here, so the existing `Promise.allSettled` bulk-action shape
    (`FileTree.tsx`, phase 9) reports it as a per-item failure without any new frontend
    plumbing.
    """

    deleted: bool
    reason: str
    bytes_freed: int | None = None


# --- Reset item tracking (2026-08-13, prompts/2026-08-13-reset-item-tracking.md) -----------
#
# Distinct from Delete (above, removes bytes) and from Clear History (`api/history.py`, removes
# job/event rows and never touches `item`) -- this forgets an item's tracking outright, so a
# suppressed or failed path can be reused. See `core/local_delete.py`'s own "Reset item
# tracking" section for the full reasoning; these are just its wire shapes.


class ResetItemResponse(BaseModel):
    """`POST /api/items/{item_id}/reset` -- the selected-item(s) scope (single row, or one call
    per selected row for a bulk reset, mirroring `DeleteItemResponse`'s own `Promise.allSettled`
    shape). A withheld guard raises `HTTPException` (409) rather than `reset=False`, for the
    identical frontend reason `DeleteItemResponse` documents.
    """

    reset: bool
    reason: str
    affected_rel_paths: list[str] = Field(default_factory=list)


class QueueResetRequest(BaseModel):
    """`POST /api/queues/{queue_id}/reset-all`'s body -- the whole-queue scope. `confirm_name`
    must equal the queue's own `name` exactly (case-sensitive); this is defense in depth
    alongside the frontend's own typed-confirmation UI, not a replacement for it -- the most
    destructive action in the app gets two independent places asking "are you sure," not one.
    """

    confirm_name: str


class ResetSummaryResponse(BaseModel):
    """The outcome of a multi-target reset (whole-queue or purge-by-pattern) -- never
    all-or-nothing: `withheld` names every target a guard refused and why, while everything
    else in scope still reset. `affected_count` is every row actually forgotten across
    `item`/`item_settle`/`deleted_archive`, at every depth -- `reset_top_level` is just the
    count of root items (selected/matched top-level entries) that succeeded.
    """

    reset_top_level: int
    withheld: list[dict[str, str]] = Field(default_factory=list)
    affected_count: int


class ResetPatternPreviewRequest(BaseModel):
    pattern: str


class ResetPatternPreviewItem(BaseModel):
    rel_path: str
    is_dir: bool
    remote_size: int | None
    local_size: int | None


class ResetPatternPreviewResponse(BaseModel):
    """The purge-by-pattern scope's own safety mechanism (per the task this shipped from): see
    every top-level item a pattern would reset, with enough per-item data
    (`remote_size`/`local_size`) for the frontend to compute the same real-numbers warning the
    other two scopes already show, before anything is confirmed.
    """

    items: list[ResetPatternPreviewItem] = Field(default_factory=list)


# --- Settings -> Queues -> Patterns (DESIGN.md §3.1 `pattern`, §4.7) --------------------

PatternKind = Literal["select", "skip", "file_exclude"]


class PatternIn(BaseModel):
    queue_id: int | None = None  # None = global, applies to every queue (§4.7)
    kind: PatternKind
    expr: str = Field(max_length=MAX_NAME_LEN)
    enabled: bool = True


class PatternOut(PatternIn):
    id: int


class PatternPreviewRequest(BaseModel):
    """The Settings → Queues live "what would this match" preview (DESIGN.md §4.7, §9.2) --
    evaluates an *unsaved* pattern set against the queue's current remote tree, so a mistake
    is visible before it's saved rather than discovered afterward.
    """

    patterns: list[PatternIn] = Field(default_factory=list)
    patterns_only: bool = False


class PatternPreviewItem(BaseModel):
    rel_path: str
    is_dir: bool
    matched: bool  # would auto-queue pick this item up


class PatternPreviewFile(BaseModel):
    rel_path: str
    excluded: bool


class PatternPreviewResponse(BaseModel):
    items: list[PatternPreviewItem]
    # Per DESIGN.md §9.2: "within a sampled item, which files would be excluded." One
    # top-level directory's files, chosen automatically -- `None` if the queue has no
    # directory item to sample yet.
    sample_item: str | None = None
    sample_files: list[PatternPreviewFile] = Field(default_factory=list)


class QueueAutoQueueStatus(BaseModel):
    """Runtime status for the Settings → Queues pattern editor (DESIGN.md §7.3's mount gate,
    required starting phase 4) -- distinct from the persisted `auto_queue_enabled` toggle,
    which is just config. `mount_ok=False` means the gate is currently blocking every
    auto-queue action for this queue, regardless of the toggle.
    """

    mount_ok: bool
    gated_reason: str | None = None


# --- Files (DESIGN.md §9.2) --------------------------------------------------------------


class LifecycleFacet(BaseModel):
    """One R/L/V/E reading (2026-08-13, `core/itemview.py._lifecycle_facets`). `level` is the
    color a renderer needs (`"green" | "amber" | "red" | "dim"`); `reason` is the short code a
    tooltip is built from, alongside this row's own raw size/timestamp fields -- see
    `core/itemview.py`'s module docstring for why the classification lives there and the
    wording lives in the frontend.
    """

    level: Literal["green", "amber", "red", "dim"]
    reason: str


class LifecycleFacets(BaseModel):
    remote: LifecycleFacet
    local: LifecycleFacet
    verified: LifecycleFacet
    extracted: LifecycleFacet


class FileNode(BaseModel):
    id: int | None = None  # the persisted `item` row's id -- what POST /api/jobs takes (§4.7)
    rel_path: str
    is_dir: bool
    state: str
    # The settle gate (prompts/open-issues.md #2, `core/settle.py`, migration 007): `'settling'`
    # for a top-level item held at REMOTE_ONLY while its remote fingerprint hasn't held still
    # for 2 consecutive scans yet. `'removing'` (2026-08-13,
    # prompts/2026-08-13-delete-state-truthfulness.md) for the whole subtree of an in-progress
    # `core/local_delete.py.delete_local()` call. `None` otherwise. Deliberately not a new
    # `state` value -- avoids touching the state CHECK constraint or DESIGN.md §9.2's
    # three-word vocabulary.
    substate: str | None = None
    # `item.suppressed_reason` (2026-08-13, prompts/2026-08-13-delete-state-truthfulness.md),
    # `None` unless `auto_queue_suppressed` is set. `FileTree.tsx`'s "Re-Download" action label
    # needs this to tell a self-delete (`'deleted_local'`) apart from every other suppression
    # reason and from a `REMOVED_LOCAL`/`REMOVED_BOTH` row that isn't suppressed at all (the
    # latter reachable via `core/mount_sentinel.py.resolve_vanished`, which never suppresses).
    suppressed_reason: str | None = None
    remote_size: int | None
    local_size: int | None
    remote_mtime: float | None
    # The local-side counterpart to `remote_mtime` (migration 011, 2026-08-13,
    # prompts/2026-08-13-files-detail-inspector.md) -- the item drawer's "modified date, both
    # sides" reading. Files only, `None` for a directory, mirroring `remote_mtime`'s own
    # convention (`core/reconcile.py`).
    local_mtime: float | None
    # When `state` last actually changed value (migration 006), stamped by that migration's
    # own triggers -- not writer discipline (see `core/itemview.py.item_view`). `None` only
    # for a row the migration's backfill genuinely couldn't date. Deliberately NOT the key for
    # the planned local-retention feature, which must use `downloaded_at` instead: "when did
    # it complete" and "when did it last move" are different questions.
    state_changed_at: str | None = None
    # When this row was first ever seen (migration 001) -- the first entry in the item drawer's
    # lifecycle chronology (2026-08-13, prompts/2026-08-13-files-detail-inspector.md). Existed
    # on the row since phase 2; new to the wire only with this task.
    first_seen_at: str | None = None
    # The settle gate's countdown (2026-08-13, prompts/2026-08-13-files-ux-pass.md item 3):
    # `item_settle.matched_scans`/`updated_at` (`core/settle.py.SettleRecord`), joined in only
    # by the two `item_view` callers that need it for the Files page's "1 of 2 scans, 35s of
    # 60s" readout (`core/itemview.py._optional`'s own docstring has the full reasoning for why
    # this is optional rather than universal). `None` for a non-top-level row -- `item_settle`
    # has no row for one at all -- or before this item's first scan.
    settle_matched_scans: int | None = None
    settle_first_matched_at: str | None = None
    # The settle gate's *other* display state (2026-08-13,
    # prompts/2026-08-13-settle-progress-visibility.md, migration 013): a top-level item that
    # hasn't been confirmed unchanged even once yet (`settle_matched_scans == 1` -- a
    # first-ever sighting, or the fingerprint changed on the most recent scan and reset the
    # count; the frontend's `lib/format.ts.isStillArriving` draws this line, deliberately not
    # distinguishing the two) has nothing useful to say via the countdown above.
    # `settle_total_bytes` (`item_settle.total_bytes`, already computed as part of the
    # fingerprint) is what a "still arriving" reading watches climb; `settle_first_observed_at`/
    # `settle_last_changed_at` answer "how long have we watched this" / "when did it last move."
    # Gated on `substate == "settling"` exactly like the two fields above -- see
    # `core/itemview.py.item_view`'s own docstring. `None` for the same reasons those two are,
    # plus one more: a pre-migration-013 `item_settle` row that hasn't changed since carries
    # `NULL` for these two timestamps specifically (`core/settle.py.SettleRecord`).
    settle_total_bytes: int | None = None
    settle_first_observed_at: str | None = None
    settle_last_changed_at: str | None = None
    # Milestone/audit timestamps (2026-08-13, prompts/2026-08-13-lifecycle-icons.md) -- passed
    # through verbatim from the `item` row (`core/itemview.py.item_view`), the raw material a
    # lifecycle icon's tooltip is built from. `downloaded_at` already existed on the row before
    # this task (§7.3's retention key); the other four are new to the wire, not new to the
    # database.
    downloaded_at: str | None = None
    verified_at: str | None = None
    extracted_at: str | None = None
    first_missing_at: str | None = None
    remote_deleted_at: str | None = None
    # "Folder prefix during transfer" (2026-08-14, `core/download_prefix.py`): the exact prefix
    # string this item's local root is *currently* written under, `None` when nothing is in
    # flight under a prefixed name. Never part of `rel_path` -- see that module's docstring --
    # this is purely the item drawer's "where does this actually live right now" answer.
    pending_download_prefix: str | None = None
    # `deleted_archive.deleted_at` (2026-08-14,
    # prompts/2026-08-14-extracted-archives-rest-as-extracted.md), joined in the same optional
    # way as the settle fields above (`core/itemview.py.item_view`'s own docstring). `None`
    # unless this rel_path is a spent archive volume `core/local_delete.py.
    # delete_extracted_archives` removed after a successful extraction -- the row's own `state`
    # already reads `EXCLUDED` for this reason (never through §7.3's grace clock), and this is
    # what lets the Files page tell that apart from an ordinary pattern-`EXCLUDED` file and
    # render a greyed-out "Extracted" chip instead of "Excluded".
    deleted_archive_at: str | None = None
    facets: LifecycleFacets


class QueueFiles(BaseModel):
    queue_id: int
    queue_name: str
    scanned_at: str | None
    error: str | None = None
    # A *soft* note (DESIGN.md §5) — set when the last scan skipped one or more unreadable
    # remote subtrees (core/remote.py's scan-abort fix, phase 3b) rather than failing
    # outright. Distinct from `error`, which means the whole scan failed.
    warning: str | None = None
    # DESIGN.md §7.3's mount sentinel, required starting phase 4 (docs/decisions.md).
    # `None` before this queue has ever scanned; `False` means auto-queue (and, later,
    # delete propagation) is currently gated off for this queue regardless of its toggles.
    mount_ok: bool | None = None
    nodes: list[FileNode] = Field(default_factory=list)


class FilesResponse(BaseModel):
    queues: list[QueueFiles]


# --- Jobs / transfer engine (DESIGN.md §4, §9.2 Transfers) ------------------------------


class QueueItemRequest(BaseModel):
    item_id: int
    start_now: bool = False  # "Start now at max bandwidth" (§4.5), applied at admission


class JobOut(BaseModel):
    id: int
    item_id: int
    queue_id: int
    queue_name: str
    rel_path: str
    is_dir: bool
    kind: str
    state: str
    lane: str
    rank: float
    attempt: int
    queued_at: str
    started_at: str | None
    finished_at: str | None
    pid: int | None
    rate_limit_bps: int | None
    forced_full_rate: bool
    bytes_start: int
    bytes_done: int
    bytes_total: int | None
    speed_bps: float | None = None
    eta_s: float | None = None
    exit_code: int | None = None
    error_class: str | None = None
    # DESIGN.md §9.2: "Failed rows show the error class and the captured lftp output tail."
    output_tail: str | None = None


class JobsResponse(BaseModel):
    jobs: list[JobOut]


class TransferSettingsOut(BaseModel):
    max_bandwidth_bps: int
    max_concurrent_transfers: int
    small_item_threshold_bytes: int
    small_lane_concurrency: int
    small_lane_reserve_bps: int | None
    min_share_floor_bps: int
    mirror_parallel_transfer_count: int
    mirror_use_pget_n: int
    pget_default_n: int
    max_attempts: int
    retry_backoff_base_s: float
    extra_lftp_settings: str


class TransferSettingsIn(TransferSettingsOut):
    pass


# --- Settings -> Transfer's "effective lftp settings" readout (2026-08-14,
# prompts/2026-08-14-show-effective-lftp-settings.md) -------------------------------------
#
# Read-only, credential-free by construction: every field here is built from
# `core/lftp.py.effective_tuning_settings` / `build_transfer_command`, which never see the two
# credential-bearing rc lines (`sftp:connect-program`, `open -u ...`) -- see that module's own
# docstring for why the split is structural, not a filter applied to rendered text.


class EffectiveLftpSetting(BaseModel):
    key: str
    value: str
    why: str
    # True when a TransferSettings-derived number drives this line's value or presence; False
    # when lftpweb always writes this exact value regardless of any setting.
    configurable: bool


class EffectiveLftpJobKind(BaseModel):
    kind: Literal["mirror", "pget"]
    # The transfer command's argv, with illustrative (not real) paths -- built by
    # `core/lftp.py.build_transfer_command` itself, so `-c`, `--parallel`, `--use-pget-n` stay
    # in lockstep with what a real job actually runs.
    argv: str
    argv_why: str
    rc_settings: list[EffectiveLftpSetting]


class EffectiveLftpSettingsOut(BaseModel):
    kinds: list[EffectiveLftpJobKind]
    # A per-job bandwidth cap (net:limit-total-rate) is always set on a real job, but the
    # number is computed at admission time (DESIGN.md §4.5) from how many jobs are currently
    # sharing the ceiling -- not a fixed value this static endpoint can state without
    # re-deriving scheduler admission math outside core/scheduler.py. Left as prose rather than
    # a fabricated number; see the live connection-count readout above it on this page for what
    # a job admitted right now would actually get.
    bandwidth_note: str


# --- History (DESIGN.md §9.2 History page, phase 6) -------------------------------------
#
# Deliberately a *separate* shape from JobOut/JobsResponse (api/jobs.py), even though both
# ultimately read the `job` table. JobOut carries `output_tail` inline because the Transfers
# page's row set is bounded by construction (only the active set plus one terminal row per
# item -- core/queue.py.list_jobs's own docstring). History has no such bound -- a busy
# install accumulates thousands of terminal jobs -- so shipping ~4KB of output per row in a
# paginated list response would make the row cap pointless. `has_output_tail` says whether
# there's anything to fetch; `GET /api/history/jobs/{id}/output` fetches it.


class HistoryJobOut(BaseModel):
    id: int
    item_id: int
    queue_id: int
    queue_name: str
    rel_path: str
    is_dir: bool
    kind: str
    state: str  # 'succeeded' | 'failed' | 'cancelled' -- this endpoint's whole domain
    attempt: int
    queued_at: str
    started_at: str | None
    finished_at: str | None
    bytes_total: int | None
    bytes_done: int
    exit_code: int | None
    error_class: str | None
    has_output_tail: bool
    # Migration 016 (2026-08-13, prompts/done/2026-08-13-dismiss-terminal-jobs.md): when this
    # job was dismissed from the Transfers page, or `None` if it never was / is still active.
    # History shows every terminal job regardless -- dismissal only ever hides a row from
    # Transfers -- but surfacing it here answers "did I dismiss this, or did it just age off
    # the other page" without making the row set itself conditional on the answer.
    dismissed_at: str | None


class HistoryJobsResponse(BaseModel):
    jobs: list[HistoryJobOut]
    total: int  # count matching the filter, ignoring limit/offset -- what "load more" needs
    limit: int
    offset: int


class HistoryJobOutputOut(BaseModel):
    """The on-demand payload for a single failed (or any terminal) job's captured output --
    phase 3a stores up to ~4KB per failed job precisely so this can show *why*, not a red dot.
    """

    job_id: int
    error_class: str | None
    output_tail: str | None


class HistoryEventOut(BaseModel):
    id: int
    ts: str
    level: str
    kind: str
    message: str
    item_id: int | None
    job_id: int | None
    # Resolved via a join against item/path_queue so the delete audit is legible without a
    # second round trip -- "what was deleted, from which queue" (DESIGN.md §7.3) shouldn't
    # require the UI to cross-reference item ids by hand. `None` when the item (or its queue)
    # no longer exists -- `event.item_id` is ON DELETE SET NULL, so a deleted queue's old
    # audit rows survive with the identifying context gone, which is the correct trade for an
    # audit trail (the record outlives the thing it describes) rather than cascading it away.
    queue_id: int | None
    queue_name: str | None
    rel_path: str | None


class HistoryEventsResponse(BaseModel):
    events: list[HistoryEventOut]
    total: int
    limit: int
    offset: int


class HistoryClearResponse(BaseModel):
    """The response for every `DELETE` under `/api/history/*` (2026-08-13,
    prompts/2026-08-13-clear-history.md) -- one row, a filtered batch, or everything, all the
    same shape. `deleted` is the actual row count the `DELETE` affected (`cursor.rowcount`),
    which is the number to show the user, not the pre-delete `total` the confirmation prompt
    used -- the two can differ if something else changed the underlying rows in between.
    """

    deleted: int


# --- Settings -> Backup (DESIGN.md §10.2, phase 7) --------------------------------------


class BackupSettingsOut(BaseModel):
    interval_days: float
    keep_count: int


class BackupSettingsIn(BackupSettingsOut):
    pass


class BackupInfoOut(BaseModel):
    filename: str
    size_bytes: int
    created_at: str


class BackupListResponse(BaseModel):
    backups: list[BackupInfoOut]


# --- Metrics / Dashboard (DESIGN.md — new section proposed, see docs/decisions.md) -------


class MetricsSettingsOut(BaseModel):
    retention_days: int


class MetricsSettingsIn(MetricsSettingsOut):
    pass


class MetricsBucketOut(BaseModel):
    ts: str  # bucket start, UTC ISO-8601
    up: bool  # False = no heartbeat fell in this bucket -- lftpweb wasn't running (a gap)
    total_bytes: int | None  # None when up is False; sum of by_queue (incl. 0) otherwise
    by_queue: dict[int, int]  # queue_id -> bytes moved in this bucket; an omitted queue moved 0


class MetricsThroughputResponse(BaseModel):
    range: str
    bucket_seconds: int
    buckets: list[MetricsBucketOut]


# --- Auth (DESIGN.md §8, phase 8) --------------------------------------------------------

AuthMode = Literal["none", "password", "proxy"]


class AuthSettingsIn(BaseModel):
    mode: AuthMode
    proxy_header: str = Field(default="Remote-User", max_length=MAX_NAME_LEN)
    proxy_trusted_cidrs: list[str] = Field(default_factory=list, max_length=256)
    # Only meaningful when `mode == "password"`: creates the single local user (if none
    # exists yet) or changes username/password atomically with the mode switch. This is what
    # keeps "switch to password mode" from ever being separable from "someone can actually
    # log in" -- a client can never store `mode: "password"` with nobody able to authenticate
    # (see api/auth.py.put_auth_settings and core/auth.py's module docstring).
    username: str | None = Field(default=None, max_length=MAX_NAME_LEN)
    new_password: str | None = Field(default=None, max_length=MAX_SECRET_LEN)


class AuthSettingsOut(BaseModel):
    mode: AuthMode
    proxy_header: str
    proxy_trusted_cidrs: list[str]
    has_user: bool
    username: str | None = None


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=MAX_SECRET_LEN)
    new_password: str = Field(max_length=MAX_SECRET_LEN)


class LoginIn(BaseModel):
    username: str = Field(max_length=MAX_NAME_LEN)
    password: str = Field(max_length=MAX_SECRET_LEN)


class AuthSessionOut(BaseModel):
    """`GET /api/auth/session` -- "whoami," always reachable unauthenticated (it's the one
    endpoint the login page itself needs before any session exists) so the frontend can
    decide whether to render the login form at all.
    """

    mode: AuthMode
    authenticated: bool
    username: str | None = None
    # Present only when `authenticated` via a password-mode session -- what the frontend
    # attaches as `X-CSRF-Token` on every mutating request (DESIGN.md §8).
    csrf_token: str | None = None


class ApiKeyOut(BaseModel):
    id: int
    name: str
    created_at: str
    last_used_at: str | None


class ApiKeyIn(BaseModel):
    name: str = Field(max_length=MAX_NAME_LEN)


class ApiKeyCreatedOut(ApiKeyOut):
    # Plaintext -- shown exactly once, in the response to the create call, and never again
    # (DESIGN.md §8: "show the plaintext once at creation and never again").
    key: str


# --- Settings -> Logs (DESIGN.md §10.1, phase 7) ----------------------------------------


class LogFileOut(BaseModel):
    name: str
    size_bytes: int
    modified_at: str
    is_current: bool


class LogFilesResponse(BaseModel):
    files: list[LogFileOut]


class LogTailResponse(BaseModel):
    lines: list[str]
    # True when the byte cap (`core/logtail.py`'s bounded reverse read) was reached before
    # `lines` worth of matching content was found -- i.e. a level filter may be under-showing
    # what's actually in the file, because this endpoint never reads the whole thing to find
    # out. See core/logtail.py's module docstring.
    truncated: bool
