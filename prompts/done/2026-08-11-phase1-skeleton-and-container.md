---
name: 2026-08-11-phase1-skeleton-and-container
status: completed
created: 2026-08-11
model: sonnet
completed: 2026-08-11
result: FastAPI+SQLite backend, React/Vite/Tailwind shell, and container/compose all built and verified; two DESIGN.md gaps found and worked around (cap_drop/PUID conflict, health repo_url), ports moved to 8087/5187 for the host.
---

# Task: Phase 1 — skeleton + container

Build the runnable shell of lftpweb: a FastAPI backend with a migrated SQLite database and a
health endpoint, a React SPA shell with the real navigation chrome, and the container and
compose files to run it. **No sync, scan, or transfer logic** — those are phases 2 and 3.

**Done when:** `docker compose -f docker-compose.dev.yml up --build` starts the container, the
UI loads in a browser, and `/api/health` returns 200 with version `0.0.1`.

## Before you start

- **Read `DESIGN.md` first**, at minimum §2 (architecture), §3.1 (schema), §9 (frontend), §10
  (operations), §11 (container), §12 (files to create), §13 (build order, phase 1).
- Read `prompts/startnewsession.md` for project context and the traps list.
- Read `CLAUDE.md` for the operating rules you must honor.
- This is a **greenfield repo** — only docs exist. You are creating the first code.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this plan
needs to modify. If any have uncommitted changes, list them and ask before touching them.
Surface unrelated dirty files once as awareness; don't block. This file is exempt.

## Decisions already made — do not re-litigate

- **Migrations: hand-rolled, not Alembic.** Numbered SQL files in
  `backend/lftpweb/migrations/NNN_description.sql`, applied in order by a small runner in
  `db.py`, tracked in a `schema_version` table. Rationale: the schema in §3.1 is raw SQL with no
  ORM, Alembic without SQLAlchemy models is friction for no gain, and §10.2's
  backup-before-migration hook is trivial to wire into our own runner. Record this in
  `docs/decisions.md`.
- **No SQLAlchemy.** `aiosqlite` with explicit SQL. Pydantic models for API shapes only.
- **Dependencies via `pyproject.toml`** (PEP 621), installed with `uv` in the Docker builder
  stage. `uv` is available locally too.
- **Phase 1 dependency set stays minimal:** `fastapi`, `uvicorn[standard]`, `aiosqlite`,
  `pydantic`, `pydantic-settings`. `asyncssh` arrives in phase 2 — don't add it yet.

## What to do

### 1. Backend skeleton

- `backend/lftpweb/__init__.py` — `__version__ = "0.0.1"`, bare, no `v` prefix. This is the
  single source of truth for the version (§12).
- `config.py` — `pydantic-settings`, env-var driven, with sane container defaults:
  `LFTPWEB_CONFIG_DIR=/config`, `LFTPWEB_PORT=8080`, `LFTPWEB_LOG_LEVEL=INFO`,
  `LFTPWEB_REPO_URL` (empty by default — used for the version link, §9.1).
- `db.py` — `aiosqlite` connection management, WAL mode on, foreign keys on, plus the migration
  runner described above.
- `models.py` — Pydantic models for whatever the API returns in this phase. Don't model the
  whole domain speculatively.
- `logsetup.py` — rotating file handler to `<config>/logs/lftpweb.log`, 5 MB × 5 files, plus
  console. §10.1. The credential redactor belongs here as a logging filter; a stub that's wired
  in and tested with one case is enough for now, since there are no credentials yet.
- `main.py` — FastAPI app factory, lifespan that opens the DB and runs migrations, static-file
  mount serving the built SPA with an SPA fallback so client-side routes deep-link correctly.

### 2. Schema — all of §3.1, in migration `001`

Create every table now (`setting`, `host`, `path_queue`, `pattern`, `item`, `job`, `event`),
matching §3.1 including the columns added for later phases (`auto_queue_suppressed`,
`suppressed_reason`, `lane`, `rank`, `forced_full_rate`, `first_missing_at`,
`remote_deleted_at`). Later phases fill them; getting the shape right once beats five
migrations that each add a column.

§3.1 is written as annotated pseudo-SQL — you must turn it into real DDL: pick sensible types,
add `NOT NULL` and defaults where obvious, add the `UNIQUE(queue_id, rel_path)` constraint on
`item`, and index what will obviously be queried (`item.queue_id`, `item.state`, `job.state`,
`job.item_id`, `event.ts`). **If you find a genuine problem with the schema as designed, stop
and report it rather than silently improving it** — see "Surfacing design decisions" below.

### 3. API

- `api/health.py` — `GET /api/health` → `{status, version, db, uptime_s}`. `db` reflects a real
  query, not a constant. This is also the container `HEALTHCHECK` target.
- `api/stats.py` — `GET /api/stats` → the header-bar shape from §9.1
  (`current_speed_bps`, `allocated_bps`, `ceiling_bps`, `queued_count`, `queued_bytes`,
  `transferred_24h_bytes`), returning zeros in this phase. Wiring the real endpoint now means
  phase 3 fills in values rather than reshaping the UI.
- Leave `files` / `jobs` / `settings` / `auth` / `ws` out of this phase.

### 4. Frontend shell

Vite + React + TypeScript + Tailwind in `frontend/`. Build the **chrome from §9.1**, with pages
as empty placeholders:

- **Left nav**: Files · Transfers · History · Settings (React Router).
- **Top tabs**, rendered only where a section has more than one page — Settings has
  Connection · Queues · Transfer · Post-processing · Logs · Backup · Auth, all empty shells.
- **Stats header** bound to `/api/stats`: current speed, **allocated vs. ceiling**, queued count
  and bytes, 24 h transferred. Zeros are fine; the layout must be real.
- **Theme: dark / light / system, defaulting to system.** Tailwind class strategy on `<html>`,
  choice persisted to `localStorage`, and it must actually follow the OS when set to system.
- **Version bottom-left of the nav**, from `/api/health`, rendered as `v0.0.1`. It links to
  `<repo_url>/releases/tag/v<version>` when `LFTPWEB_REPO_URL` is set, and renders as plain
  text when it isn't — the GitHub repo does not exist yet and the UI must not show a dead link.

Keep it clean and legible. This is the shell every later phase builds into, so structure it
deliberately (a layout component, a router, an api client, a theme provider) rather than
cramming it into `App.tsx`.

### 5. Container and compose

Per §11 and §11.2.

- `docker/Dockerfile` — multi-stage: `node:22-alpine` builds the SPA → `python:3.13-alpine`
  builder installs deps with `uv` → `python:3.13-alpine` runtime with `lftp`,
  `openssh-client`, `7zip`, `su-exec`, `tini`. **No compiler in the final image.** Install
  `lftp`/`7zip` now even though nothing uses them yet — proving they're available on Alpine is
  part of this phase's value.
- `docker/entrypoint.sh` — `PUID`/`PGID`/`UMASK`; **chown `/config` only, never the data
  volumes**; a chown failure on a data volume is a **warning, not fatal**; verify writability of
  each configured path and report a clear error naming the path and effective uid/gid; then
  `exec su-exec` to drop privileges. §11.2 — read it, this is the part most likely to be got
  wrong.
- `docker-compose.yml` (production) — image, NFS-style bind mounts, dedicated uid/gid,
  `read_only: true`, `cap_drop: ALL`, `no-new-privileges`, tmpfs `/run`, restart policy,
  healthcheck.
- `docker-compose.dev.yml` — builds locally, bind-mounts source for hot reload (uvicorn
  `--reload` + Vite dev server), the developer's own uid/gid, scratch dirs under
  `private_data/`, DEBUG logging.

### 6. Tests

`tests/`, pytest. Keep it proportionate to a skeleton:

- Migration from an empty database to head succeeds, and is idempotent on re-run.
- `/api/health` returns 200, the version matches `__version__`, and `db` reports healthy.
- `/api/stats` returns the documented shape.
- The log redactor turns `sftp://user:secret@host` into `sftp://user:***@host`.

## Verify before reporting — actually run these

Do not report success on anything you have not executed. Docker 29.6, Docker Compose v5.3,
Node 24, Python 3.12 and `uv` are all available.

1. `pytest` passes.
2. The frontend builds (`npm run build`) with no TypeScript errors.
3. `docker compose -f docker-compose.dev.yml build` succeeds.
4. The container starts and `curl -fsS localhost:<port>/api/health` returns 200 with
   `"version": "0.0.1"`.
5. `docker compose config --quiet` is clean for **both** compose files.
6. The SPA is served and its client-side routes deep-link (e.g. `/settings/logs` returns the app
   shell, not a 404).

Report exactly what you ran and what it printed. **If something cannot be verified, say so
plainly rather than implying it works** — an unverified claim here costs more than a gap.

## Surfacing design decisions

The user explicitly asked for major design decisions found during the build to be surfaced
rather than absorbed. If you hit something where `DESIGN.md` is wrong, ambiguous, or silent on
a choice that will be hard to reverse:

- Make the smallest reasonable call to keep moving,
- and **report it prominently in your final summary**, with the alternatives and what you chose.

Do not edit `DESIGN.md` yourself — it gets corrected deliberately, in conversation with the
user. Small factual corrections are the exception; flag those too.

## Conventions to honor

- Match `DESIGN.md`'s module layout from §12 exactly. If a module isn't needed yet, don't create
  an empty file for it.
- Type-annotate the Python. Keep functions small enough to test.
- Comment the non-obvious only — the entrypoint's NFS handling deserves comments; a FastAPI
  route does not.
- No secrets, no credentials, nothing under `private_data/` committed.

## When done

1. Record in `docs/decisions.md` (newest at top): the hand-rolled-migrations choice with its
   rejected alternative (Alembic), plus anything non-obvious you had to decide.
2. Update **`prompts/startnewsession.md`** — the "Where we are" table and status line — so a
   fresh session knows phase 1 is done and what's next. This is required, not optional.
3. Update this file's frontmatter: `status`, `completed` (2026-08-11), `result` (one line).
4. `git mv` this file into `prompts/done/` (on success) or `prompts/failed/` (on failure).
5. **You are a spawned agent: do NOT commit.** Prepare the working tree, then report back the
   file list and a proposed one-line commit message (`feat:` prefix, no `Co-authored-by:`
   trailer; current branch is `dev`) so the orchestrating session can surface the `y/n`.
   Never `git add -A`, never push.
