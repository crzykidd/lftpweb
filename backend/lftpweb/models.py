"""Pydantic models for API shapes. Only what this phase's endpoints return — the domain
is not modeled speculatively ahead of the phases that need it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

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
    # 2026-08-20 (`prompts/2026-08-20-queue-pause.md`): whether admission is paused
    # (`core/queue.py.TransferQueue.paused`). The header bar and the Queue tab's own banner both
    # read this -- "a queue that silently does nothing is a support question waiting to happen"
    # is the task's own reasoning for surfacing it here alongside `scheduler_alive`.
    queue_paused: bool = False
    # 2026-08-21 (`prompts/2026-08-21-pause-for-duration.md`): the absolute ISO-8601 UTC
    # deadline a timed pause (1/10/30/60 minute dropdown) resumes at, or `None` for an
    # indefinite pause (or no pause at all) -- `core/queue.py.TransferQueue.paused_until`. The
    # UI reads this to show "resumes at HH:MM" rather than a bare "paused" that would otherwise
    # look identical to an indefinite one.
    queue_paused_until: str | None = None
    # 2026-08-16 (docs/decisions.md): also not in §12's literal shape, same reasoning as
    # `repo_url` above -- `config.Settings.build_sha`/`.build_channel` are baked at image
    # *build* time (docker/Dockerfile's `runtime` stage, .github/workflows/publish.yml), and
    # this endpoint is already how the nav's version readout learns runtime-only facts about
    # itself. `None` for every build that never baked them (local dev, compose dev stack, a
    # manual `docker build` with no `--build-arg`) -- the frontend badge degrades to today's
    # plain `v<version>` rendering in that case, never a lie about the channel.
    build_sha: str | None = None
    build_channel: str | None = None


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
    # Sonarr/Radarr integration (migration 018, docs/arr-integration-spec.md "Data model" /
    # "API surface"). Binding is per-queue, one instance at most -- `None` (the default, and
    # every existing queue's value after the migration) means "no integration": no icons, no
    # matching, no *arr behavior at all for this queue. Full-replace fields like the rest of
    # this model (not the four post-processing toggles' merge-on-absence shape) -- Settings ->
    # Queues' edit form always submits the complete queue state, same reasoning
    # `update_queue`'s own docstring already gives for every other plain field here.
    arr_instance_id: int | None = None
    # Default off, per-queue, and only meaningful when `arr_instance_id` is set --
    # `api/settings_queues.py` rejects `True` with no bound instance (spec: "the Settings UI's
    # 'Delete when imported' checkbox, disabled with a hint unless an instance is selected").
    arr_delete_completed: bool = False
    # This queue's `local_path`, translated into the bound *arr's own namespace (spec "Path
    # namespaces") -- `None` means "same namespace, no translation," never an empty-string
    # sentinel. Optional even when an instance is bound: it is only read by phase B's notify
    # push, not by matching.
    arr_visible_path: str | None = Field(default=None, max_length=MAX_PATH_LEN)
    # Migration 024 (docs/transfers-redesign-spec.md §3.6, phase 1 stage 3,
    # prompts/done/2026-08-19-queue-short-display-name.md). `None` (the default, and every
    # existing queue's value after the migration -- nullable with **no backfill**) means "no
    # short name set": every read falls back to the full `name` via
    # `api/settings_queues.py.resolve_queue_display_name`, the one place that fallback is
    # computed. An explicit value is a per-queue display hint for the compact per-row label
    # stage 4 renders once Transfers drops its per-queue grouping (`DC-Movies` -> `MOV`) --
    # deliberately **not** an identifier: no uniqueness constraint, two queues may share one.
    # Trimmed and empty-after-trim-normalized-to-`None` at save time
    # (`api/settings_queues.py._normalized_short_name`), and length-capped there too
    # (`MAX_SHORT_NAME_LEN`) rather than via this field's own `Field(max_length=...)` -- unlike
    # every other capped field on this model, the cap here must apply to the *trimmed* value,
    # and an over-length value must not be rejected before trimming has a chance to fix it.
    short_name: str | None = None


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


class DeleteItemRequest(BaseModel):
    """`POST /api/items/{item_id}/delete`'s optional body (2026-08-16, the manual delete
    dialog's independent Local/Source scopes,
    `prompts/2026-08-16-manual-delete-local-and-remote.md`). Omitted entirely (no body at all)
    means exactly today's pre-existing behavior -- `local=True, source=False`, a local-only
    delete -- so every caller that predates this task (including every existing test that calls
    `delete_item` with no body) is unaffected. `source=True` is the first *manual* remote-delete
    path in the API; the endpoint itself raises 400 if both are `False` -- "at least one" is a
    cross-field rule a plain check in the handler expresses more legibly than a Pydantic
    validator would.
    """

    local: bool = True
    source: bool = False


class DeleteItemResponse(BaseModel):
    """`POST /api/items/{item_id}/delete` -- the first delete endpoint in this API (DESIGN.md
    §9.2's Files-page "Delete local"). A withheld guard raises `HTTPException` instead of
    returning `deleted=False` here, so the existing `Promise.allSettled` bulk-action shape
    (`FileTree.tsx`, phase 9) reports it as a per-item failure without any new frontend
    plumbing.

    `deleted`/`reason`/`bytes_freed` describe the **local** scope exactly as before this task
    (`True`/`"deleted"`/bytes when `local=True` was requested and succeeded; unchanged shape for
    every caller that predates the source scope). `source_deleted`/`source_reason` are `None`
    when `source` was not requested, and otherwise describe that independent outcome -- a
    combined request where local succeeds but source then fails is reported as `deleted=True`
    with `source_deleted=False` (200, not 409): the local side effect already happened and
    cannot be un-happened, so the response says exactly what did and did not occur rather than
    a single flag flattening the two into a misleading pass/fail. The endpoint only raises 409
    when *nothing* requested actually succeeded -- see `api/jobs.py.delete_item`'s own docstring.
    """

    deleted: bool
    reason: str
    bytes_freed: int | None = None
    source_deleted: bool | None = None
    source_reason: str | None = None


# --- Manual pipeline resolution (2026-08-20, docs/transfers-redesign-spec.md §3.2's
# pipeline-completion rule, migration 025) ------------------------------------------------------
#
# The escape hatch for a genuinely wedged row in the Queue tab's Active/pending box. Every
# blocking condition has a bounded automatic exit (`core/pipeline_flight.py`), but automatic exits
# are necessary rather than sufficient, and a box that can silently accumulate rows nothing is
# working on stops being trustworthy.


class ResolveItemRequest(BaseModel):
    """`POST /api/items/{item_id}/resolve`'s body. `outcome` is `'complete'`/`'failed'` to file
    the row out of Active with that outcome, or **`null` to undo** a resolution set by mistake --
    the row then goes straight back through the normal predicate as if it had never been touched.

    **This is a CLASSIFICATION ONLY and that is not negotiable.** It moves a row between two
    boxes on a page; it is evidence of nothing. See migration 025's own comment for the explicit
    list (it must never advance the `move`-mode delete ladder, never be read as a confirmed *arr
    import, never trigger notify/cleanup/retention/post-processing, never alter auto-queue's
    eligibility). DESIGN.md §7.3 makes a source delete wait for a *confirmed* import held across
    two consecutive poller passes precisely because that delete is irreversible; a user clicking a
    button on a hunch is not that evidence.
    """

    outcome: Literal["complete", "failed"] | None = None


class ResolveItemResponse(BaseModel):
    """Echoes what is now stored on the item, so the caller never has to guess whether an undo
    landed. `manual_outcome` is `null` after an undo.
    """

    item_id: int
    manual_outcome: str | None
    manual_outcome_at: str | None


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


# --- Settings -> Queues path-browse dialog (GitHub issue #4,
# prompts/done/2026-08-16-path-browse-dialog.md) -- `api/browse.py`. Directories only, one
# shared shape for the local and remote endpoints alike.


class BrowseEntryOut(BaseModel):
    name: str


class BrowseResponse(BaseModel):
    path: str
    parent: str | None
    entries: list[BrowseEntryOut]
    truncated: bool
    # Set only when the endpoint had to walk up from what was actually requested (a nonexistent
    # tail, a file instead of a directory, permission denied) -- `core/browse.py`'s own
    # docstring has the full algorithm. `None` means `path` above is exactly what was asked for.
    fallback_from: str | None = None


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
    # Sonarr/Radarr integration (migration 018, docs/arr-integration-spec.md): the Files page's
    # *arr icon reads this directly. A facet, not a lifecycle state -- passed through verbatim
    # from `item.arr_status`/`item.arr_status_at` (`core/itemview.py.item_view`), never derived
    # from `state`. `arr_download_id` is deliberately absent here too -- see that column's own
    # comment in migration 018 ("not published in the item projection").
    arr_status: str | None = None
    arr_status_at: str | None = None
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


# "Start now" (§4.5) as a menu, not a single button (2026-08-19,
# prompts/done/2026-08-19-start-now-bandwidth-fractions.md): `POST /api/jobs/{id}/start-now`'s
# optional body. `rate_percent` omitted (no body at all -- every caller before this task) or
# `100` both mean Max, byte-for-byte the only behavior this action had before. Anything outside
# the five menu options is a 422 for free, via `Literal` -- no hand-written validation needed.
class StartNowRequest(BaseModel):
    rate_percent: Literal[10, 25, 50, 75, 100] | None = None


# The chevron reorder actions (2026-08-19, docs/transfers-redesign-spec.md §3.4 stage 2,
# `prompts/2026-08-19-queue-reorder-chevrons.md`) -- `POST /api/jobs/{id}/move`'s required body.
# One endpoint, one request shape, rather than three near-identical routes; anything outside the
# three directions is a 422 for free via `Literal`, the same pattern `StartNowRequest` above uses
# for its own five-option menu.
MoveDirection = Literal["up", "down", "top"]


class MoveJobRequest(BaseModel):
    direction: MoveDirection


# The Transfers -> Queue tab's Pause dropdown menu's four offered durations (2026-08-21,
# `prompts/2026-08-21-pause-for-duration.md`) -- minutes, converted to seconds before reaching
# `TransferQueue.pause`'s `duration_s`. Anything outside this set is a 422 for free via
# `Literal`, the same pattern `StartNowRequest.rate_percent` already uses for its own menu.
PauseDurationMinutes = Literal[1, 10, 30, 60]


# The Transfers -> Queue tab's Pause control (2026-08-20, `prompts/2026-08-20-queue-pause.md`):
# `POST /api/queue/pause`'s body -- "pause after current" (`stop_running=False`, the default)
# leaves running jobs alone; "pause now" (`stop_running=True`) also SIGTERMs and requeues them
# (`core/queue.py.TransferQueue.pause`). `POST /api/queue/unpause` takes no body.
#
# `duration_minutes` (2026-08-21, `prompts/2026-08-21-pause-for-duration.md`): `None` (the
# default, unchanged from before this task) is an indefinite pause -- the dropdown of durations
# extends this control, it does not replace "pause until I say otherwise". Combines with either
# `stop_running` value; the two are independent axes of the same call.
class QueuePauseRequest(BaseModel):
    stop_running: bool = False
    duration_minutes: PauseDurationMinutes | None = None


class QueueBandwidthRequest(BaseModel):
    """`POST /api/queue/bandwidth` -- the Transfers -> Queue tab's bandwidth slider (2026-08-21,
    `prompts/done/2026-08-21-bandwidth-from-the-queue-page.md`). Sets the **site-wide throttle**
    (`TransferSettings.throttle_bandwidth_bps`) -- the limit the scheduler actually allocates
    against, bounded above by Settings -> Transfer's `max_bandwidth_bps` ceiling (2026-08-21,
    `prompts/done/2026-08-21-bandwidth-ceiling-and-autocommit.md`). Still site-wide, still never a
    per-queue limit (DESIGN.md §4.5: one site, one set of transfer knobs).

    **It no longer writes the ceiling.** Until 2026-08-21 this endpoint and Settings -> Transfer
    edited the same single number, which made the slider's own upper bound unstateable: capping
    it at the value it edits is a ratchet (see that prompt, and `docs/decisions.md`).

    `apply_to_running` defaults to `False`, the safe option: write the number, interrupt
    nothing. `True` additionally stops and re-queues every in-flight transfer so the scheduler
    re-admits it against the new limit -- see `core/queue.py.TransferQueue.set_site_bandwidth`
    for why that is the only way a running job's allocation can change, and for what it does
    (and pointedly does not do) while the queue is paused.

    The lower bound is enforced in `set_site_bandwidth` against the *current*
    `min_share_floor_bps`, not here, since it depends on another setting's value; `gt=0` is the
    cheap half that can be checked without a DB read. The *upper* bound is not a validation at
    all -- a value above the ceiling is clamped to it and the applied value comes back in the
    response.
    """

    effective_bandwidth_bps: int = Field(..., gt=0)
    apply_to_running: bool = False


class QueueBandwidthResponse(BaseModel):
    """What the slider's apply actually did -- `core/queue.py.BandwidthChangeOutcome`, one for
    one. `effective_bandwidth_bps` is the throttle **as applied** (clamped to the ceiling if the
    request exceeded it), which is what the banner announces and what the UI echoes optimistically
    until its next settings poll. `interrupted` is how many running transfers were stopped and
    re-queued (always `0` for a future-items-only change, and for an apply-to-in-progress with
    nothing running); `skipped_because_paused` marks the case where the setting was written but
    the queue's pause was deliberately left untouched -- the banner must then say nothing was
    restarted rather than report a restart that did not happen.
    """

    effective_bandwidth_bps: int
    interrupted: int
    skipped_because_paused: bool


class JobOut(BaseModel):
    id: int
    item_id: int
    queue_id: int
    queue_name: str
    # The queue's short display name (migration 024, `path_queue.short_name`) -- `null` when
    # unset, same fallback-to-`queue_name` convention `api/settings_queues.py.
    # resolve_queue_display_name` / `lib/queueDisplayName.ts` already both define. Added
    # 2026-08-19 (docs/transfers-redesign-spec.md §3.6, phase 1 stage 4a) so a Transfers row can
    # show a queue badge without grouping rows by queue.
    queue_short_name: str | None
    rel_path: str
    is_dir: bool
    kind: str
    state: str
    lane: str
    # Vestigial for ordering as of migration 023 (`queue_position`, below, is the ordering key
    # now) -- kept in the API response unchanged since it's still a real, still-written column
    # (docs/transfers-redesign-spec.md §3.4, migration 023's own comment on why `rank` wasn't
    # dropped). Not read by the frontend for sorting.
    rank: float
    attempt: int
    queued_at: str
    started_at: str | None
    finished_at: str | None
    pid: int | None
    rate_limit_bps: int | None
    # 2026-08-19 (prompts/done/2026-08-19-start-now-bandwidth-fractions.md): widened from a
    # plain `forced_full_rate: bool` -- `None` means never force-started, `1.0` means Max
    # (byte-identical to the old `True`), and the new menu's `0.1`/`0.25`/`0.5`/`0.75` fill the
    # gap between. See `core/queue.py.resolve_forced_rate_fraction` for how a row resolves to
    # this.
    forced_rate_fraction: float | None
    bytes_start: int
    bytes_done: int
    bytes_total: int | None
    speed_bps: float | None = None
    eta_s: float | None = None
    exit_code: int | None = None
    error_class: str | None = None
    # DESIGN.md §9.2: "Failed rows show the error class and the captured lftp output tail."
    # `null` on a row from `GET /api/jobs/complete` (2026-08-19, docs/transfers-redesign-spec.md
    # §3.2, phase 1 stage 4b) -- that endpoint is paginated but *unbounded* in total row count,
    # so it never inlines this blob (~4KB/row), the identical trap `api/history.py`'s own
    # docstring names for its own list endpoint. `GET /api/jobs` (the Active/pending box) stays
    # bounded by construction (`core/queue.py.list_jobs`'s own docstring) and keeps inlining it
    # unchanged. `has_output_tail` below is the one signal a row's expand panel needs to decide
    # whether to fetch it on demand -- true from either endpoint, so the panel doesn't need to
    # know which box a row came from.
    output_tail: str | None = None
    # Mirrors `HistoryJobOut.has_output_tail` -- always populated (from `output_tail is not
    # None` server-side), regardless of whether `output_tail` itself was inlined. The Transfers
    # row's expand panel (`TransfersPage.tsx.RowDetailPanel`) fetches on demand via the existing
    # `GET /api/history/jobs/{id}/output` (same `job` table, same id) exactly when this is `true`
    # and `output_tail` came back `null` -- one on-demand fetch path shared with History's own,
    # rather than a second endpoint that does the same thing.
    has_output_tail: bool = False
    # 2026-08-15 (prompts/2026-08-15-transfers-single-line-rows-with-detail.md): the item-level
    # facts the Transfers row's new expand panel needs for its Processing/*arr groups. Inlined
    # here rather than fetched separately -- `list_jobs()`'s row set is bounded by construction
    # (one row per active/recently-terminal item, `core/queue.py.list_jobs`'s own docstring), so
    # joining these onto every row costs nothing the endpoint doesn't already pay for `rel_path`/
    # `queue_name`. All three below mirror `item.verified_at`/`item.extracted_at`/
    # `item.remote_deleted_at` (migration 001) -- the same milestones `ItemDrawer.tsx`'s
    # `lifecycleChronology` already reads off `FileNode`.
    verified_at: str | None = None
    extracted_at: str | None = None
    remote_deleted_at: str | None = None
    # `item.arr_status`/`item.arr_status_at` (migration 018, docs/arr-integration-spec.md) --
    # same facet the Files page's row icon already reads off the item projection.
    arr_status: str | None = None
    arr_status_at: str | None = None
    # The bound instance's own display name, resolved via `path_queue.arr_instance_id ->
    # arr_instance.name` -- `null` whenever this job's queue has no bound *arr instance, the one
    # signal the panel's *arr group gates its own visibility on (`lib/transferPanel.ts.
    # hasArrGroup`). Never `arr_download_id` -- that column stays server-side only, same as the
    # item projection's own convention (`lib/fileTree.ts`'s comment on why).
    arr_instance_name: str | None = None
    # The bound instance's `kind` ('sonarr'/'radarr', migration 018's CHECK constraint) -- added
    # 2026-08-16 (prompts/2026-08-16-arr-chip-on-row-lines.md) alongside `arr_instance_name` for
    # the Transfers/History row chip's brand-logo choice. `arr_instance_name` is free-text (the
    # user can rename an instance to anything), so it can't drive which logo to draw; `kind` is
    # the one field that reliably says "this is a Sonarr instance" vs. "this is a Radarr
    # instance". `null` under the same condition `arr_instance_name` is null.
    arr_instance_kind: str | None = None
    # **Which box this row belongs in** (2026-08-20, docs/transfers-redesign-spec.md §3.2's
    # pipeline-completion rule) -- `true` = Active/pending, `false` = Complete. Computed
    # server-side by `core/pipeline_flight.py`, the *same* expression `GET /api/jobs/complete`
    # filters its listing and its `total` on, and shipped as a field rather than re-derived on
    # the client on purpose: the Active box is client-side over `GET /api/jobs` and the Complete
    # box is a server-side paginated query, so a second encoding of this rule would drift and put
    # a row in both boxes or neither. The frontend's own rule is exactly
    # `job.state === 'queued' || job.state === 'running' || job.pipeline_in_flight`, and the first
    # two disjuncts are already inside this flag -- they're kept client-side only so the box still
    # renders sensibly against a response from an older server.
    pipeline_in_flight: bool = False
    # What the row is waiting on, when it is in flight and its own state chip doesn't already say
    # ('verifying' | 'extracting' | 'processing' | 'awaiting_import' | 'deleting_source', or
    # `null`). Derived from the same clauses as `pipeline_in_flight` in one `CASE`
    # (`core/pipeline_flight.waiting_reason_expr`), so the label and the box can never disagree.
    # `null` for a `queued`/`running` row -- the state chip already says "QUEUED"/"DOWNLOADING".
    # An unrecognized value degrades to the raw string on the row, the same tolerance
    # `arr_instance_kind` above documents.
    pipeline_waiting_reason: str | None = None
    # The manual escape hatch (migration 025) -- `'complete'`/`'failed'` once a human has resolved
    # this item out of the Active box, `null` otherwise. **A classification only**: nothing but
    # `core/pipeline_flight.py` reads it, and migration 025's own comment lists what it must never
    # be mistaken for. Surfaced here so the row can *show* it was manually resolved rather than
    # looking like a normal completion -- otherwise the audit trail says one thing and the UI
    # another.
    manual_outcome: str | None = None
    manual_outcome_at: str | None = None


class JobsResponse(BaseModel):
    jobs: list[JobOut]


# --- Preflight (docs/transfers-redesign-spec.md §4, prefigured; this task's own handoff prompt,
# prompts/done/2026-08-20-preflight-box.md, plus its follow-up
# prompts/2026-08-20-preflight-waiting-sources.md) -- the Queue tab's third, small box: things
# lftpweb already knows about but has no work to do on yet. **Source-agnostic by construction**:
# the *arr poller (`core/arrsync.py`) and the settle gate's own eligibility check
# (`core/autoqueue.py.AutoQueue`) are the two sources wired up, and `source`/`source_label`/
# `source_kind` name *which* one a given row came from rather than any field here assuming it's
# always the *arr -- see `core/preflight.py.PreflightRow` for the projection this response wraps
# (no table, no migration, nothing persisted) and its own docstring for the full reasoning on why
# nothing source-specific belongs at this layer. `gated_queues` (below) is a different shape
# entirely -- a whole queue blocked, not an item waiting -- and deliberately lives on this
# response rather than as rows, per the user's own decision (`docs/decisions.md`). --------------


class PreflightRowOut(BaseModel):
    """One Preflight box row. Deliberately thin -- no `id`, no `queue_position`, no
    `bytes_done`: there is no `item` and no `job` behind this row yet, and the handoff prompt's
    own "the rows are inert, and the box is what makes that structural" is exactly why nothing
    here invites a per-row control (chevrons, Dismiss, Start now, Stop) that would need one.
    """

    # `'arr'` or `'settle'` today (`core/preflight.py.PreflightSource`); widened, not replaced,
    # if a further source ever lands. The frontend's own *arr-specific rendering (the brand-logo
    # chip) is gated on this, never inferred from `source_kind` alone.
    source: str
    queue_id: int
    # The bound queue's own display identity (2026-08-21, "the columns moved around" fix) -- so
    # this row can show the same queue tag every other row on the page shows
    # (`lib/queueDisplayName.ts.queueDisplayName(queue_short_name, queue_name)`). Mirrors
    # `core/preflight.py.PreflightRow`'s own two fields of the same name field for field.
    queue_name: str
    queue_short_name: str | None
    title: str
    # Free-form, source-owned display text for "what state is this in" -- an *arr row's own
    # `trackedDownloadState` (e.g. `"downloading"`), verbatim; `null` when the source didn't
    # report one. Display-only advisory text, never a state this codebase's own state machine
    # reads or reasons about.
    status_label: str | None
    # The upstream's own display name (an *arr instance's configured name, e.g. `"Sonarr"`) and,
    # when the source has one, a brand/variant hint for the row's chip (`'sonarr'`/`'radarr'` for
    # *arr; `null` for a source with no logo of its own).
    source_label: str
    source_kind: str | None
    # A known total size and, when the source can compute one, how much is left to arrive --
    # both `null` when the source has neither (**never a request to enrich one that lacks it**,
    # the handoff prompt's own instruction). An *arr row's own `size`/`sizeleft`, when its
    # response happened to carry them.
    size_bytes: int | None
    size_remaining_bytes: int | None
    # How many seconds until this row's own source expects its wait to clear (2026-08-21,
    # "we missed the remaining time") -- an *arr row's own `timeleft`
    # (`core/arrsync.py._parse_timeleft`), rendered through the same `formatEta`/`transferLineValue`
    # shape the Transfers row already uses for its own ETA. `null` when the source has no
    # meaningful estimate this pass (frequently true -- a paused/stalled download client item, or
    # a settle-gated row, whose own remaining figure is `size_bytes` above, not a time) -- never a
    # fabricated or zero figure.
    remaining_s: float | None
    # The download client actually fetching this release (2026-08-21, user's own words:
    # "tooltip maybe we should show the arr details ... Downloading from '<download client
    # name>' from arr") -- an *arr row's own `downloadClient`; `null` for a settle row (no
    # separate download client in that source's own model) or an *arr row whose response didn't
    # carry one. Display-only provenance for a chip tooltip.
    download_client: str | None
    # A generic "how far along has this row's own wait gotten" detail for the chip's own hover
    # tooltip (2026-08-21, "the settling chip should have a mouseover that shows time details")
    # -- `core/preflight.py.PreflightRow.wait_scans`/`wait_since`'s own docstring has the full
    # reasoning. `wait_since` is already an ISO-8601 string on this side (`core/settle.py.
    # SettleProgress.first_matched_at`), so no further conversion happens at this layer. `null`
    # for an *arr row (its own wait isn't bound by scan count) or a settle row with no
    # `item_settle` history yet -- both fields together, never one alone.
    wait_scans: int | None
    wait_since: str | None


class PreflightGatedQueueOut(BaseModel):
    """One entry in the Preflight box's mount-gate banner (this task,
    prompts/2026-08-20-preflight-waiting-sources.md, decided with the user) -- **a banner line,
    not a row**: `core/autoqueue.py.AutoQueue.gated` blocks a queue's *entire* auto-queue pass at
    once, so the useful fact is "this queue is blocked and why," never one row per affected item
    (fifty identical rows would bury the single fact that matters). `reason` is
    `AutoQueue.gated`'s own string, verbatim -- never recomposed here, so the banner and the
    existing Settings -> Queues status readout (`QueueAutoQueueStatus.gated_reason`) can never
    say different things about the same gating episode.
    """

    queue_name: str
    reason: str


class PreflightResponse(BaseModel):
    """`GET /api/queue/preflight`. `source_configured=False` (with `rows` always empty in that
    case) means "no row source is configured at all" (no bound, enabled *arr instance anywhere,
    **and** no queue has the settle gate + auto-queue both live) -- the frontend hides the box's
    row list for that case rather than showing an empty "Nothing in preflight" that would be
    meaningless for a user with nothing configured. `gated_queues` is independent of
    `source_configured` and can be non-empty even when it's `False` -- the mount gate can block a
    queue's auto-queue pass whether or not either row source happens to be configured, so the
    frontend shows the box whenever *either* `source_configured` or `gated_queues` has something
    to say (`components/PreflightBox.tsx`).
    """

    source_configured: bool
    rows: list[PreflightRowOut]
    gated_queues: list[PreflightGatedQueueOut] = []


class CompleteJobsResponse(BaseModel):
    """`GET /api/jobs/complete` (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage
    4b) -- the Queue tab's **Complete** box: terminal (`succeeded`/`failed`/`cancelled`), not
    dismissed, one row per item (the same "most recent job wins" rule `core/queue.py.list_jobs`
    already applies -- see `TransferQueue.list_complete_jobs`'s own docstring), newest-finished
    first, **server-side paginated**. Same `total`/`limit`/`offset` shape
    `api/history.py.HistoryJobsResponse` already established -- reused deliberately rather than
    inventing a second pagination idiom (`total` is the full filtered count, ignoring the page,
    so the frontend can render numbered pages -- `lib/pagination.ts` -- without a second
    unbounded query).
    """

    jobs: list[JobOut]
    total: int
    limit: int
    offset: int


class DismissAllRequest(BaseModel):
    """`POST /api/jobs/dismiss-all`'s optional JSON body (2026-08-17, the Transfers page's
    per-queue-group "Dismiss Queue" control, `prompts/2026-08-17-transfers-dismiss-per-queue.md`)
    -- scopes the bulk dismiss to one queue's own terminal jobs instead of every queue. Omitted
    entirely (no body at all), or `queue_id: null`, means exactly today's pre-existing behavior
    -- every queue -- so every caller that predates this task (including every existing test
    that calls `dismiss_all_jobs` with no body) is unaffected. The same "optional body, omitted
    means unchanged" shape `DeleteItemRequest` above already set (2026-08-16).

    `job_ids` (2026-08-19, the Transfers page's name filter and its own "Dismiss list" button,
    `prompts/2026-08-19-transfers-name-filter.md`) scopes the same bulk dismiss to an explicit
    set of job ids instead of a whole queue -- "Dismiss list" sends the ids of exactly the
    terminal rows the filter currently matches (`lib/transferPanel.ts.dismissableJobIds`) as
    **one** request, never a client-side loop over each row's own `/dismiss` call (see
    `TransferQueue.dismiss_all_terminal`'s own docstring). Omitted entirely, or `job_ids: null`,
    means exactly today's pre-existing behavior -- the same "optional field, omitted means
    unchanged" shape `queue_id` above already set, so every caller that predates this task is
    unaffected. `job_ids: []` is a real, deliberate "match nothing" input (an empty filter
    result), not "no filter" -- it dismisses zero rows, never every row; see
    `TransferQueue.dismiss_all_terminal`'s own comment for why that distinction has to be made
    explicitly rather than falling out of an `if job_ids:` truthiness check.

    `name_filter` (2026-08-19, phase 1 stage 4b) supersedes `job_ids` for "Dismiss list" now
    that the Complete box (`CompleteJobsResponse` above) is server-paginated: the filter can
    match far more rows than are loaded on the current page, so an explicit id list can only
    express "dismiss this one page's worth", not what the button promises ("dismiss everything
    the filter matches"). `name_filter` carries the *same* text the Complete box's own listing
    query is filtering on, so the server dismisses exactly what the box is currently showing,
    across every page -- `TransferQueue.dismiss_all_terminal`'s own comment has the matching SQL,
    built from the identical predicate `TransferQueue.list_complete_jobs` uses (never two
    versions of "what counts as a match" that could drift apart), same case-insensitive
    substring-over-`rel_path` semantics as the client-side `filterTransferJobs`. An empty string
    is a real value here (matches every `rel_path`, same as an empty client-side filter matching
    every row) -- it is `None`, not `""`, that means "no filter given", the same "unset means
    unchanged" convention every other optional field on this model already uses.

    `outcome` (2026-08-20, follow-up to phase 1 stage 4b from the user's browser review,
    `prompts/2026-08-20-transfers-dismiss-menu-and-counts.md`) narrows the same bulk dismiss to
    one terminal outcome -- the Complete box's own "Dismiss" menu, "all, downloaded, failed (or
    whatever the completed status are)" in the user's own words. One of the three states
    `TransferQueue.dismiss_job`'s own guard (and `lib/transferPanel.ts.isDismissable` on the
    frontend) already allows: `succeeded`/`failed`/`cancelled`. `None` (the default, and every
    call before this task) means no outcome restriction -- unchanged behavior for every existing
    caller.

    **Decided by the user (2026-08-20): `outcome` and `name_filter` COMPOSE rather than being
    mutually exclusive.** Both are *narrowings* of the same dismissable set, not alternative
    scopes -- "dismiss the failed ones matching `Married`" is a coherent request the Complete
    box's own header now has to express (the outcome menu lives inside the same box the name
    filter already narrows), so a request naming both is valid and dismisses their intersection.
    See `TransferQueue.dismiss_all_terminal`'s own docstring for the SQL and
    `docs/decisions.md` for the fuller reasoning, including why `queue_id` was *not* given the
    same treatment.

    **`job_ids` and `queue_id` stay mutually exclusive with everything, including each other.**
    `job_ids` already names exactly which rows to dismiss -- composing a narrowing alongside an
    explicit id list is meaningless (and `outcome`/`name_filter` are exactly that: narrowings of
    an implicit set, not restrictions on an explicit one). `queue_id` is arguably also a
    narrowing (dropping it into the composing group would be consistent), but it has had no
    caller since per-queue grouping was dropped in phase 1 stage 4a -- kept mutually exclusive
    the way it always was, since widening it costs real validator complexity for a scope nothing
    sends. The restructured validator (`_scopes_are_coherent` below) checks this in two tiers
    -- `job_ids`/`queue_id` against each other, then `job_ids`/`queue_id` against
    `outcome`/`name_filter` -- rather than one flat "name more than one of four" rule, which
    would have wrongly rejected `outcome` + `name_filter` together.
    """

    queue_id: int | None = None
    job_ids: list[int] | None = None
    name_filter: str | None = None
    outcome: Literal["succeeded", "failed", "cancelled"] | None = None

    @model_validator(mode="after")
    def _scopes_are_coherent(self) -> "DismissAllRequest":
        exclusive_scopes = sum(scope is not None for scope in (self.queue_id, self.job_ids))
        if exclusive_scopes > 1:
            raise ValueError("queue_id and job_ids are mutually exclusive -- pass at most one")
        narrowings_given = sum(scope is not None for scope in (self.outcome, self.name_filter))
        if exclusive_scopes > 0 and narrowings_given > 0:
            raise ValueError(
                "queue_id/job_ids are mutually exclusive with outcome/name_filter -- job_ids "
                "already names exactly which rows to dismiss, and queue_id has no caller that "
                "composes with a narrowing"
            )
        return self


class DismissAllResponse(BaseModel):
    """`POST /api/jobs/dismiss-all` (2026-08-15) -- the bulk counterpart to `POST
    /api/jobs/{id}/dismiss`. `dismissed` is the actual row count the bulk `UPDATE` affected
    (`cursor.rowcount`), the same "report the real number, not a guess" convention
    `HistoryClearResponse.deleted` already uses.
    """

    dismissed: int


class ItemEventOut(BaseModel):
    """One `event` row, scoped to a single item (2026-08-15) -- the Transfers panel's on-demand
    "processing story" fetch. Deliberately leaner than `api/history.py.HistoryEventOut`: this
    endpoint is already scoped to one known item (the caller supplies `item_id` in the URL), so
    there is no need to resolve or carry `queue_id`/`queue_name`/`rel_path` a second time.
    """

    id: int
    ts: str
    level: str
    kind: str
    message: str
    job_id: int | None


class ItemEventsResponse(BaseModel):
    events: list[ItemEventOut]


class ItemChildrenResponse(BaseModel):
    """`GET /api/items/{id}/children` (2026-08-20, docs/transfers-redesign-spec.md §3.3, phase 1
    stage 5) -- the Transfers row's on-demand per-file expansion, "the thing Files is currently
    used for, moved to where the ordering lives" (the spec's own words).

    **Fetched only when a row expands, never inlined into the jobs list** -- the exact trap
    `api/history.py`'s own module docstring names for `output_tail`: a season pack has dozens of
    children, and the Active/Complete boxes are bounded by row count, not by how large any one
    row's own subtree is. `api/jobs.py.item_children` is the fetch this response belongs to; see
    its docstring for the query and the cap.

    `children` is `FileNode` -- the exact same `core/itemview.py.item_view` projection every
    other consumer of the `item` table reads through (`GET /api/files`, the WebSocket, this
    endpoint), never a second shape invented for this one panel. `total` is the true descendant
    file count regardless of the cap, so a capped response can still say "showing N of total"
    honestly -- the same `total`/`limit`/`offset` shape `HistoryJobsResponse`/
    `CompleteJobsResponse` already use, reused rather than a fourth paging idiom.
    """

    children: list[FileNode] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class TransferSettingsIn(BaseModel):
    """The twelve fields Settings -> Transfer owns and writes. **`throttle_bandwidth_bps` is
    deliberately absent** (2026-08-21,
    `prompts/done/2026-08-21-bandwidth-ceiling-and-autocommit.md`): the Queue tab's slider owns the
    throttle through `POST /api/queue/bandwidth`, and this PUT writes the whole object, so
    including it would let a stale Settings form silently undo a throttle set minutes later on
    another page. `api/jobs.py.put_transfer_settings` carries the stored throttle forward
    untouched (clamping it if this PUT lowered the ceiling beneath it).
    """

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


class TransferSettingsOut(TransferSettingsIn):
    """...plus the one read-only number the two-value bandwidth model adds: the limit actually
    in force (`core/queue.py.TransferSettings.effective_bandwidth_bps`). Equal to
    `max_bandwidth_bps` when no throttle is set, which is the default and the upgrade path.

    Exposed as the *resolved* value rather than the raw nullable throttle so every consumer --
    the Queue slider's position, the "Start now" fraction check, Settings -> Transfer's own
    "a throttle is in force" note, the support bundle -- reads one number and cannot disagree
    about what `None` means. "Is a throttle set?" stays answerable: it is
    `effective_bandwidth_bps < max_bandwidth_bps`.
    """

    effective_bandwidth_bps: int


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
    # `item.arr_status`/`item.arr_status_at` plus the bound instance's `name`/`kind` (2026-08-16,
    # prompts/2026-08-16-arr-chip-on-row-lines.md) -- the same two scalar columns `JobOut` above
    # already carries for the Transfers row chip, joined here via the same `path_queue.
    # arr_instance_id -> arr_instance` `LEFT JOIN` so the History job row can draw the identical
    # chip. Two scalar columns on an already-paginated list, not a blob -- the phase-6
    # unbounded-list trap this module's own docstring warns about does not apply to a handful of
    # short strings per row. `null` whenever this job's queue has no bound *arr instance, or the
    # poller never matched this item.
    arr_status: str | None = None
    arr_status_at: str | None = None
    arr_instance_name: str | None = None
    arr_instance_kind: str | None = None


class HistoryQueueSummaryOut(BaseModel):
    """One queue's honest aggregate over the *whole filtered set*, not just the loaded page
    (2026-08-16, prompts/2026-08-16-history-jobs-group-collapse.md) -- History's jobs list is
    `LIMIT`/`OFFSET` paginated, so a client-side sum over `HistoryJobsResponse.jobs` would be
    wrong the moment more rows match the filter than are loaded. This is a single bounded
    `GROUP BY` (one row per queue, api/history.py's `_queue_summaries`), never a per-row blob,
    and it honors the exact same filter as the `jobs` list it rides alongside -- same
    `_jobs_where_clause` call, so the two can never drift apart. History's job domain is
    terminal-only (`succeeded`/`failed`/`cancelled` -- see `_TERMINAL_JOB_STATES`), so unlike
    the Transfers page's queue-group header (`lib/transferPanel.ts.QueueGroupCounts`) there is
    no `active`/`queued` bucket here to count.
    """

    queue_id: int
    queue_name: str
    succeeded: int
    failed: int
    cancelled: int
    total_bytes_done: int


class HistoryJobsResponse(BaseModel):
    jobs: list[HistoryJobOut]
    total: int  # count matching the filter, ignoring limit/offset -- what "load more" needs
    limit: int
    offset: int
    # Inlined rather than a second `GET /api/history/jobs/summary` endpoint (2026-08-16): the
    # frontend already refetches this list on every filter change, so a second request would
    # just be a second round trip for data derived from the identical filter -- see the
    # module docstring in api/history.py for the fuller comparison.
    queue_summaries: list[HistoryQueueSummaryOut]


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
    # 2026-08-21 (daily rollups, prompts/done/2026-08-21-daily-metric-rollups.md): fraction
    # (0.0-1.0) of a full day's expected heartbeats actually observed -- only set for a
    # daily-granularity bucket sourced from `metric_daily` (the 90d/1y ranges at `group=day`),
    # `None` for every bucket sourced from the raw tables (1h/12h/24h/7d/30d at `group=hour` or
    # `group=day`), where `up` alone is already exact at that bucket's own width. Lets the UI
    # distinguish a genuinely quiet day (`up: true`, `coverage` near 1.0, `total_bytes: 0`) from
    # a day lftpweb was mostly down (`coverage` well under 1.0) -- both would otherwise look
    # identical.
    #
    # 2026-08-21 (chart grouping, prompts/done/2026-08-21-chart-grouping.md): for a `week`/`month`
    # bucket (any range), this means something different -- the fraction of *days* in the bucket
    # that were `up`, not a heartbeat-density average (`api/metrics.py._aggregate_day_points`'s
    # docstring has the full reasoning: raw-table days only ever carry a boolean up/down at that
    # granularity, so a day-count fraction is the one definition that means the same thing
    # regardless of which table the underlying days came from).
    coverage: float | None = None


class MetricsThroughputResponse(BaseModel):
    range: str
    # 2026-08-21 (chart grouping): the bucket width actually used, decoupled from `range` (which
    # only says how far back) -- "hour"/"day"/"week"/"month" for the bytes-chart-groupable ranges
    # (24h/7d/30d/90d/1y, api/metrics.py._GROUPABLE_RANGES); `None` for the speed chart's own
    # untouched fixed-width ranges (1h/12h), whose single bucket width `bucket_seconds` alone
    # already fully describes.
    group: str | None = None
    bucket_seconds: int
    buckets: list[MetricsBucketOut]


class MetricsTotalOut(BaseModel):
    """The Dashboard's "total downloaded" readout (task: "a user can have the option to just see
    their total downloaded amount"). `since_day` is the earliest UTC calendar day
    (`'YYYY-MM-DD'`) this total actually covers -- `None` when there is no rolled-up history yet
    (a fresh install) -- so the UI can say "since <date>" rather than implying an unbounded
    history.
    """

    total_bytes: int
    since_day: str | None


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


# --- Settings -> Integrations (migration 018, docs/arr-integration-spec.md) -------------

ArrKind = Literal["sonarr", "radarr"]


class ArrInstanceIn(BaseModel):
    """A create/update request for one Sonarr/Radarr instance. `api_key` is plaintext here --
    the only place it ever appears in a request body -- and is encrypted at rest
    (`core/crypto.py`) before it touches the database, the identical convention `HostIn.password`
    uses; it is never included in any response. Omitting it on an update keeps the stored key
    (same "unchanged must not mean cleared" rule `settings_host.py.put_host` follows).
    """

    name: str = Field(max_length=MAX_NAME_LEN)
    kind: ArrKind
    base_url: str = Field(max_length=MAX_NAME_LEN)
    api_key: str | None = Field(default=None, max_length=MAX_SECRET_LEN)
    enabled: bool = False
    notify_on_complete: bool = False


class ArrInstanceOut(BaseModel):
    id: int
    name: str
    kind: ArrKind
    base_url: str
    # Never the key itself (DESIGN.md §9.2's "must never round-trip the stored secret back to
    # the browser") -- whether one is on file, mirroring `HostOut.has_password`.
    has_api_key: bool
    enabled: bool
    notify_on_complete: bool
    created_at: str
    updated_at: str


class ArrTestResponse(BaseModel):
    """`POST /api/settings/arr/{id}/test` -- the `GET /api/v3/system/status` round trip
    (docs/arr-integration-spec.md "API surface"), the Settings UI's Test button. Same shape as
    `TestConnectionResponse` plus the instance's own reported version, which only this endpoint
    (not the generic `ok`/`error_class`/`message` triple) has anything to say about.
    """

    ok: bool
    error_class: str | None
    message: str
    version: str | None = None


class ArrPollSettingsOut(BaseModel):
    """`GET`/`PUT /api/settings/arr/poll-interval` (2026-08-21, issue #16,
    `prompts/done/2026-08-21-arr-poll-cadence.md`) -- `core/arrsync.py.ArrSettings` exposed on
    the *arr settings surface for the first time; before this it was DB-only, a default that got
    written down rather than ever a user choice. One field, same "narrow settings dataclass,
    its own `Out`" shape every other site-level settings endpoint in this codebase uses
    (`BackupSettingsOut`, `SettleSettingsOut`, ...).
    """

    poll_interval_s: float


class ArrPollSettingsIn(ArrPollSettingsOut):
    """`PUT` body. `api/settings_arr.py.put_arr_poll_settings` validates this server-side against
    `ArrSyncScheduler.MIN_POLL_INTERVAL_S`/`core/arrsync.py.MAX_POLL_INTERVAL_S` -- the same
    "validate server-side, not only in the browser" rule this task's own handoff prompt states
    explicitly, mirroring `api/backup.py.put_backup_settings`'s inline range check rather than a
    pydantic field constraint, so the error message can name the actual bound.
    """


# --- Download clients (docs/download-client-framework-spec.md, stage 1b of #18) ----------------
#
# `api/settings_clients.py` -- instance CRUD, `client-types` (the registry's declared config
# schemas), and test-connection. Mirrors `ArrInstanceIn`/`ArrInstanceOut`/`ArrTestResponse`
# above closely (this task's own handoff prompt: "the shape to mirror"), widened for what the
# framework spec adds that *arr integration never needed: a connector-declared config schema
# (spec §8.1) instead of a fixed `base_url`/`api_key` pair, multiple base paths (spec §8.2), and
# a category -> queue mapping (spec §8.3).


class ClientConfigFieldOut(BaseModel):
    """One entry in a connector's declared connection-config schema
    (`core.clients.base.ConfigField`, spec §8.1) -- projected to the wire so stage 1b-ii's
    Settings page can render one generic form for every registered connector without knowing
    anything about the connector that declared it.
    """

    key: str
    label: str
    kind: Literal["str", "int", "bool", "secret"]
    required: bool = True
    default: Any = None
    help_text: str | None = None


class ClientTypeOut(BaseModel):
    """One entry in `GET /api/settings/client-types` -- one registered connector
    (`core.clients.registered_clients()`, spec §6). `family` is display grouping only, never a
    behavioural branch (spec §5.1) -- it groups the settings picker, nothing more.
    """

    client_type: str
    family: Literal["usenet", "torrent"]
    config_schema: list[ClientConfigFieldOut]


class DownloadClientBasePathIn(BaseModel):
    """One base path to save (migration 028, spec §8.2 correction, 2026-08-22). `path` is the
    SSH-visible path -- the only one `core.browse.remote_directory_error` validates and the
    only one the §10.2 containment check and §11 scan ever read; `kind`/`client_path`/`source`
    are provenance the settings UI carries so a re-detect can tell a manual row and an already-
    resolved translation apart from a fresh proposal, never inputs this API validates against
    each other.
    """

    path: str = Field(max_length=MAX_PATH_LEN)
    # The role this path plays (`core.clients.models.BasePathKind`) -- decides deletion
    # semantics (spec §10.5): freeing a `content` root that is hardlinked from a seeding
    # torrent frees nothing; freeing a `working` root frees the space and kills the seed.
    # `unknown` is the honest default for a manually-added path no connector ever classified.
    kind: Literal["content", "working", "unknown"] = "unknown"
    # This path as the *client itself* sees it, when it differs from `path` -- mirrors
    # `path_queue.arr_visible_path` (migration 018), inverted: `path` is the SSH-visible/
    # authoritative side here, `client_path` is the foreign view kept only for display and
    # diagnosis. `None` = no translation needed -- the client and lftpweb agree.
    client_path: str | None = Field(default=None, max_length=MAX_PATH_LEN)
    # Whether this row came from detection (the client's own `list_base_paths` answer,
    # SSH-verified) or was typed by hand via the manual-add escape hatch. Lets a re-detect
    # leave manual rows -- and any translation the user already supplied for a detected one --
    # alone rather than clobbering them.
    source: Literal["detected", "manual"] = "manual"


class DownloadClientBasePathOut(DownloadClientBasePathIn):
    id: int


class DownloadClientCategoryIn(BaseModel):
    category: str = Field(max_length=MAX_NAME_LEN)
    # `None` = configured but not yet bound to a queue (spec §8.3) -- distinct from the mapping
    # not existing at all.
    queue_id: int | None = None


class DownloadClientCategoryOut(BaseModel):
    id: int
    category: str
    queue_id: int | None = None


class DownloadClientIn(BaseModel):
    """A create/update request for one download-client instance.

    `config` carries **every** key the connector's own declared `config_schema` names --
    secret and non-secret alike (spec §8.1: "each connector declares its own connection-form
    schema"), unlike `ArrInstanceIn`'s fixed `base_url`/`api_key` pair. `api/settings_clients.py`
    splits it against the registered connector's schema at request time: non-secret values go to
    `config_json` verbatim, secret values are encrypted into `secret_enc` (`core/crypto.py`,
    identical mechanism to `ArrInstanceIn.api_key`). A secret key **absent** from this dict on an
    update keeps whatever is already stored for that key -- the same "unchanged must not mean
    cleared" rule `ArrInstanceIn.api_key`/`HostIn.password` already follow, generalized from one
    named field to however many secret keys a given connector's schema declares.
    """

    name: str = Field(max_length=MAX_NAME_LEN)
    client_type: str = Field(max_length=MAX_NAME_LEN)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False
    base_paths: list[DownloadClientBasePathIn] = Field(default_factory=list)
    categories: list[DownloadClientCategoryIn] = Field(default_factory=list)


class DownloadClientOut(BaseModel):
    id: int
    name: str
    client_type: str
    # Non-secret config only -- never the secret sub-values, in any form (mirrors
    # `ArrInstanceOut.has_api_key`, generalized to "does this instance have a secret on file" for
    # a schema that may name more than one secret key).
    config: dict[str, Any]
    has_secret: bool
    enabled: bool
    # The probed capability layer (spec §4.1), as `core.clients.base.CapabilitySet` projects to
    # JSON in `api/settings_clients.py`. `None` = never successfully probed.
    capabilities: dict[str, Any] | None = None
    capabilities_probed_at: str | None = None
    version: str | None = None
    base_paths: list[DownloadClientBasePathOut]
    categories: list[DownloadClientCategoryOut]
    created_at: str
    updated_at: str


class DetectedBasePathOut(BaseModel):
    """One entry in `DownloadClientTestResponse.detected_base_paths` (spec §8.2 correction,
    migration 028) -- what the connector's own `list_base_paths` reported, and whether lftpweb
    can see it at the same path over SSH. **Detection proposes; it never saves** -- turning one
    of these into a saved `DownloadClientBasePathIn` (accepting it, or supplying the SSH-visible
    equivalent for a `not_found` one) is a separate, explicit save the settings UI performs.
    """

    client_path: str
    kind: Literal["content", "working", "unknown"]
    # `verified` -- lftpweb sees it at the same path. `not_found` -- the seedbox clearly
    # reports it missing or not a directory: the namespace mismatch, detected rather than asked
    # about. `unverified` -- the stat failed for any other reason (permission, protocol, no SSH
    # connection to try at all). **`not_found` and `unverified` are deliberately distinct** --
    # collapsing them would tell a user their path is wrong when lftpweb simply could not look
    # (`core.browse.remote_directory_error`'s own docstring draws this exact line).
    state: Literal["verified", "not_found", "unverified"]


class DownloadClientTestResponse(BaseModel):
    """`POST /api/settings/clients/{id}/test` -- the `ArrTestResponse` shape, widened with the
    resolved capability set (spec §4.1) test-connection persists. `capabilities` reflects
    whatever is now on file after this call -- unchanged from before the call for
    `ClientUnreachable`/`ClientError` (spec §4.2: a transport failure changes no capability),
    narrowed by exactly one degraded key for `CapabilityUnavailable`, and reset to the
    connector's static declaration on a fresh success (layer 3 is cleared by the next successful
    probe, spec §4.1).

    `detected_base_paths` is only ever populated on a fresh success (spec §8.2 correction) --
    `[]` on any failed test (detection never runs against a connector that couldn't even be
    reached) and `[]` for a connector that doesn't declare `list_base_paths`, which is not an
    error either.
    """

    ok: bool
    error_class: str | None
    message: str
    version: str | None = None
    capabilities: dict[str, Any] | None = None
    detected_base_paths: list[DetectedBasePathOut] = Field(default_factory=list)


# --- Support bundle (Settings -> Logs, 2026-08-17) ---------------------------------------
#
# `POST /api/support-bundle` (`api/support_bundle.py`, `core/supportbundle.py`): a downloadable
# diagnostic zip -- see that module's docstring for the full bundle shape. lftpweb's own logs
# are always included (the dialog shows that checkbox checked and disabled), so there is no
# field here to turn them off; every other part defaults on, matching the dialog's own
# default-all-checked state -- a client that sends `{}` gets everything except *arr logs (which
# need an instance id to name, so there is no "all" default for those).

MAX_ARR_INSTANCE_IDS = 64  # generous: no real install binds anywhere near this many instances


class SupportBundleRequest(BaseModel):
    include_environment: bool = True
    include_settings: bool = True
    include_events: bool = True
    include_jobs: bool = True
    # Enabled *arr instance ids whose own Sonarr/Radarr log files should be fetched and
    # bundled -- empty (the default) fetches none. One checkbox per enabled instance on the
    # frontend, all pre-checked; unlike the fixed boolean parts above this list is inherently
    # variable-length, so "select everything" is expressed as "name every id", not a bool.
    arr_instance_ids: list[int] = Field(default_factory=list, max_length=MAX_ARR_INSTANCE_IDS)
