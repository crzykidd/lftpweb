"""Env-var-driven settings. See DESIGN.md §11 / §11.2 for the container defaults."""

from __future__ import annotations

from pydantic import field_validator
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

    # DESIGN.md §4.5: the transfer engine's scheduling/reap/admit loop cadence -- "~1 Hz" per
    # §4.4. As of 2026-08-16 (§4.4), progress sampling (job and per-file speed alike) runs on a
    # derived, slower cadence -- `core/queue.py.PROGRESS_SAMPLE_TICKS` ticks of this value
    # (~5s at the default) -- not every tick.
    transfer_tick_s: float = 1.0

    # DESIGN.md §8, phase 8. Day-to-day auth configuration (mode, proxy header, trusted
    # CIDRs) lives in the `setting` table like every other *Settings dataclass and is edited
    # from Settings -> Auth (core/auth.py.AuthSettings) -- NOT here. This env var is `None`
    # (unset) by default and is deliberately the one piece of auth config that lives outside
    # the database: it is the lockout-recovery lever. Setting `LFTPWEB_AUTH_MODE=none` (or
    # any valid mode) and restarting the container overrides whatever is stored, with no
    # database access required -- see README.md's "Locked out?" section and
    # docs/decisions.md. Unset (the default) means "use whatever Settings -> Auth has
    # stored," which itself defaults to `none` for a fresh install.
    auth_mode: str | None = None

    # 2026-08-16 (docs/decisions.md): baked at image *build* time, not runtime -- unlike
    # every other setting here, these come from `docker build --build-arg` in
    # .github/workflows/publish.yml, landed as `ENV` in docker/Dockerfile's `runtime` stage,
    # so a container has no git tree to ask and still knows what it is. `build_sha` is the
    # short (7-char) commit SHA; `build_channel` is `"dev"` (pushes to `dev`) or `"release"`
    # (pushes to `main` / a published GitHub Release). Both are `None` for every path that
    # never baked them: local `uv run`, `docker-compose.dev.yml`'s `dev`/`frontend-dev`
    # targets (the ARGs are declared only in `runtime`), and a manual `docker build` with no
    # `--build-arg`. That last case still sets the env var (Docker bakes an unset ARG's
    # empty-string default into `ENV` regardless), so the validator below normalizes an
    # empty string back to `None` rather than pydantic-settings treating "" as a real value
    # distinct from "unset".
    build_sha: str | None = None
    build_channel: str | None = None

    @field_validator("build_sha", "build_channel", mode="before")
    @classmethod
    def _blank_build_field_is_unset(cls, value: str | None) -> str | None:
        return value or None


settings = Settings()
