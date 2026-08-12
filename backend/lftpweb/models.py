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


class PathQueueOut(PathQueueIn):
    id: int
    host_id: int


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
