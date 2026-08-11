"""Pydantic models for API shapes. Only what this phase's endpoints return — the domain
is not modeled speculatively ahead of the phases that need it.
"""

from __future__ import annotations

from pydantic import BaseModel


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
