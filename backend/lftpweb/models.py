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
    nodes: list[FileNode] = Field(default_factory=list)


class FilesResponse(BaseModel):
    queues: list[QueueFiles]
