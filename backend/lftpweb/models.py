"""Pydantic models for API shapes. Only what this phase's endpoints return — the domain
is not modeled speculatively ahead of the phases that need it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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
    """A create/update request for the (single, v1) seedbox host. `password` is plaintext
    here — the only place it ever appears in a request body — and is encrypted at rest
    (`core/crypto.py`) before it touches the database; it is never included in any response
    (DESIGN.md §9.2: "must never round-trip the stored secret back to the browser").
    """

    name: str
    address: str
    port: int = 22
    username: str
    auth_method: AuthMethod
    key_path: str | None = None
    password: str | None = None
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
    test before committing. `password = None` means "use the currently stored password."
    """

    name: str | None = None
    address: str | None = None
    port: int | None = None
    username: str | None = None
    auth_method: AuthMethod | None = None
    key_path: str | None = None
    password: str | None = None
    known_hosts_policy: KnownHostsPolicy | None = None


class TestConnectionResponse(BaseModel):
    ok: bool
    error_class: str | None
    message: str


# --- Settings -> Queues (DESIGN.md §3.1 `path_queue`) -----------------------------------

SyncMode = Literal["copy", "move", "sync"]


class PathQueueIn(BaseModel):
    name: str
    remote_path: str
    local_path: str
    staging_path: str | None = None
    enabled: bool = True
    sync_mode: SyncMode = "copy"
    # DESIGN.md §4.7, phase 4. Both default off/false -- enabling auto-queue is an explicit
    # user action; a queue created without specifying these fields must not auto-enable
    # itself (this phase's non-negotiable, docs/decisions.md).
    auto_queue_enabled: bool = False
    auto_queue_patterns_only: bool = False
    # DESIGN.md §6, phase 5. All three default off -- see docs/decisions.md's "every
    # post-processing step defaults off" non-negotiable. `auto_verify`/`auto_extract` are
    # existing DB columns (migration 001) that had no API/UI field until this phase;
    # `auto_move` is new (migration 003). `api/settings.py` forces `auto_verify` to `True`
    # whenever `sync_mode == 'move'` regardless of what's sent here (DESIGN.md §6: "forced on
    # and cannot be turned off in the UI") -- it is the sole gate on an irreversible delete.
    auto_verify: bool = False
    auto_extract: bool = False
    auto_move: bool = False


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
    move_enabled: bool
    concurrency: int


class PostprocessSettingsIn(PostprocessSettingsOut):
    pass


# --- Settings -> Queues -> Patterns (DESIGN.md §3.1 `pattern`, §4.7) --------------------

PatternKind = Literal["select", "skip", "file_exclude"]


class PatternIn(BaseModel):
    queue_id: int | None = None  # None = global, applies to every queue (§4.7)
    kind: PatternKind
    expr: str
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


class FileNode(BaseModel):
    id: int | None = None  # the persisted `item` row's id -- what POST /api/jobs takes (§4.7)
    rel_path: str
    is_dir: bool
    state: str
    remote_size: int | None
    local_size: int | None
    remote_mtime: float | None


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
    proxy_header: str = "Remote-User"
    proxy_trusted_cidrs: list[str] = Field(default_factory=list)
    # Only meaningful when `mode == "password"`: creates the single local user (if none
    # exists yet) or changes username/password atomically with the mode switch. This is what
    # keeps "switch to password mode" from ever being separable from "someone can actually
    # log in" -- a client can never store `mode: "password"` with nobody able to authenticate
    # (see api/auth.py.put_auth_settings and core/auth.py's module docstring).
    username: str | None = None
    new_password: str | None = None


class AuthSettingsOut(BaseModel):
    mode: AuthMode
    proxy_header: str
    proxy_trusted_cidrs: list[str]
    has_user: bool
    username: str | None = None


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class LoginIn(BaseModel):
    username: str
    password: str


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
    name: str


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
