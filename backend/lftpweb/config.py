"""Env-var-driven settings. See DESIGN.md §11 / §11.2 for the container defaults."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, all overridable via `LFTPWEB_*` env vars."""

    model_config = SettingsConfigDict(env_prefix="LFTPWEB_")

    config_dir: str = "/config"
    # Not 8080: chosen to avoid collisions with other dev stacks on the shared build host
    # (see docs/decisions.md). Override with LFTPWEB_PORT if it collides elsewhere too.
    port: int = 8087
    log_level: str = "INFO"

    # Where the built SPA lives (Dockerfile copies frontend/dist here). Deliberately an
    # absolute container path rather than "next to this package" — the runtime image
    # installs lftpweb non-editable into site-packages, so a path relative to
    # `Path(__file__).parent` would depend on the exact Python version's site-packages
    # layout. Absent outside the container (e.g. local `pytest`), which is fine: no static
    # mount is registered and only the API is served.
    static_dir: str = "/app/static"

    # Used to build the version-link URL in the nav (§9.1). Empty means "no link" —
    # the GitHub repo does not exist yet.
    repo_url: str = ""

    # DESIGN.md §5: remote scan cadence. Phase 2 uses one combined interval for remote +
    # local scanning rather than §5's separate 30s/10s cadences — see docs/decisions.md.
    scan_interval_s: float = 30.0

    # DESIGN.md §4.2/§11.1: per-job rc files (credentials + settings) and their known_hosts
    # pin, mode 0600, live on a tmpfs, unlinked when the job exits. `/run/lftpweb` in the
    # container; overridable for local dev, where bare `/run` isn't writable by a non-root uid.
    run_dir: str = "/run/lftpweb"

    # DESIGN.md §4.5: the transfer engine's scheduling pass and progress sampler cadence.
    # "~1 Hz" per §4.4.
    transfer_tick_s: float = 1.0


settings = Settings()
