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
