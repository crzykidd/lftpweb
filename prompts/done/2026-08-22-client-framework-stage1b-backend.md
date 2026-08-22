---
name: 2026-08-22-client-framework-stage1b-backend
status: completed        # pending | completed | failed
created: 2026-08-22
model: sonnet            # coding
completed: 2026-08-22
result: >
  Migration 027 (download_client, download_client_base_path, download_client_category),
  api/settings_clients.py (list/create/update/delete, client-types, test-connection with the
  probed capability layer and redacted capture), main.py wire-up, and
  tests/test_settings_clients_api.py (20 new tests). Full backend suite: 1794 passed. Found and
  fixed a real credential leak (httpx's own default request logging bypassing
  core/clients/capture.py's redaction) via core/logsetup.py's existing third-party-floor
  mechanism -- see docs/decisions.md. One targeted addition to tests/fake_sabnzbd.py
  (echo_key_in_version_body) so the capture test could prove the key-in-body half of its own
  assertion. No frontend, no poller, no README changes, per scope.
---

# Task: Stage 1b (backend) — the download-client instance row, its API, and test-connection

Give the stage 1a SABnzbd connector somewhere to live: a `download_client` table, base-path and
category→queue configuration, a CRUD API, and a test-connection endpoint that **writes a redacted
capture of the real responses to the log**.

**Stage 1b is being executed in two halves. This is the backend.** The Settings page, the generic
connector form, and the README write-up are stage 1b-ii — build none of them here.

**Why the backend goes first:** once this deploys to the test system, test-connection against the
user's real SABnzbd produces genuine wire captures in the log. `docs/download-client-framework-
spec.md` §13.4 is a list of **twelve unverified guesses** the stage 1a connector is built on, and
those captures are how they get corrected — **before** a UI is built on top of possibly-wrong
mappings.

## Before you start

Read, in this order:

1. **`docs/download-client-framework-spec.md`** — §4.1 (the three declaration layers; **this task
   builds the *probed* layer**), §7.1 (identity), §8.1 (instance row, connector-declared config
   schema), §8.2 (**base paths are user-configured, browsed, validated on save**), §8.3 (site-level
   instance, category→queue), §13.3 (capture-first), **§13.4 (the correction list this task's
   captures are meant to resolve)**.
2. **`backend/lftpweb/core/clients/`** — all of stage 0 and 1a. `base.py`'s `ConfigField`,
   `CapabilitySet`, `degrade_from_error`; `sabnzbd.py`; `capture.py`.
3. **`backend/lftpweb/api/settings_arr.py`** (219 lines) — **the shape to mirror closely**: list /
   create / update / delete / `POST .../{id}/test`, the encrypted-secret handling, the
   `_instance_out_from_row` projection, `_now_iso`. Do not invent a different shape.
4. **`backend/lftpweb/migrations/018_arr_integration.sql`** — the table style to follow, including
   the inline comments explaining each column.
5. **`backend/lftpweb/core/browse.py`** — `remote_directory_error` is the save-time validator this
   task must call for base paths. Read its docstring: it deliberately gives a real answer rather
   than a graceful fallback, because the whole point is catching a typo at save.
6. **`backend/lftpweb/core/crypto.py`** — how the seedbox password and *arr API key are encrypted
   at rest. The client secret uses the identical mechanism.

## Working tree check

`git status --porcelain` first; the tree should be clean at `50f02f7`. Surface anything unexpected
before editing. This prompt file is exempt.

## What to do

### 1. Migration `027_download_clients.sql`

Follow `018_arr_integration.sql`'s commenting style. Three tables:

- **`download_client`** — `id`, `name`, `client_type` (the registry key, e.g. `sabnzbd`),
  `config_json` (the connector-declared schema's non-secret values, spec §8.1), `secret_enc`
  (encrypted via `core/crypto.py`), `enabled` **defaulting to 0** per project rule,
  `capabilities_json` + `capabilities_probed_at` + `version` (spec §4.1's *probed* layer),
  `created_at`, `updated_at`.
- **`download_client_base_path`** — `id`, `client_id` FK, `path`. **Multiple per instance** (spec
  §8.2 — a seedbox routinely spreads content across several). This is what §10.2's delete
  containment check and §11's scan roots are read from, so it is a security boundary, not a
  convenience field.
- **`download_client_category`** — `client_id` FK, `category`, `queue_id` FK to `path_queue`
  (spec §8.3). `ON DELETE` behaviour should match how `path_queue.arr_instance_id` handles a
  deleted parent — check what 018 chose and be consistent.

**Additive only.** Nothing destructive, nothing altering existing rows.

### 2. `api/settings_clients.py`

Mirror `settings_arr.py`. Endpoints:

- `GET /api/settings/clients` — list. **Never returns the secret**, in any form.
- `GET /api/settings/client-types` — the registry's available connectors, each with its
  `client_type`, `family` (display grouping only — spec §5.1, never a behavioural branch), and its
  **declared `ConfigField` schema** so 1b-ii can render one generic form for all of them.
- `POST` / `PUT` / `DELETE /api/settings/clients[/{id}]` — CRUD, secret encrypted on write, and
  **an unchanged-secret update must not require re-sending it** (check how `settings_arr.py`
  handles this and do the same).
- `POST /api/settings/clients/{id}/test` — see below.
- Base paths and category mappings managed as part of the instance payload.

**Base-path validation on save is mandatory** (spec §8.2): every submitted base path goes through
`core/browse.py.remote_directory_error`, and a bad one is a 4xx with the real reason — never
silently accepted. A wrong base path is a wrong safety boundary for §10.2's containment check.

### 3. Test-connection, the probed capability layer, and the capture

`POST /api/settings/clients/{id}/test` must:

1. Construct the connector from the registry and call `test_connection()`.
2. Return reachability, the client's reported version, and the **resolved capability set** — the
   static declaration narrowed by whatever the probe learned (spec §4.1). Persist it to
   `capabilities_json`/`capabilities_probed_at`/`version`.
3. **Write a redacted capture of the raw responses to the log** via `core/clients/capture.py`
   (spec §13.3). This is the point of the whole task — see the note at the top.

Three rules that must not be got wrong:

- **Only `CapabilityUnavailable` may degrade a capability.** Route every degradation through
  `base.degrade_from_error`; never write a capability change from an `except ClientUnreachable`
  or a bare `ClientError` branch. Spec §4.2, and stage 0 has direct tests for the helper — do not
  bypass it.
- **A failed test must not wipe a previously probed capability set.** An unreachable client tells
  us nothing about what it supports; the last known set stands (spec §4.2, §9.2 constraint 3).
- **The capture redacts at the point of capture.** Never log a raw response and redact later.
  Verify by test that an API key present in a request URL does not reach the log.

### 4. Wire-up

Register the router in `main.py` alongside the other settings routers. Import
`core.clients` where needed so connectors register. **No poller, no scheduler changes, no
Preflight source** — that is stage 2.

### 5. Tests

- Migration applies cleanly on a fresh DB and is additive.
- Full CRUD round-trip, including that the secret is never returned and an update without the
  secret preserves it.
- Base-path save-time validation rejects a bad path with a real reason.
- Test-connection against `tests/fake_sabnzbd.py`: success path persists capabilities/version;
  **failure path leaves a previously persisted set intact**; a `CapabilityUnavailable` degrades
  exactly one key and a `ClientUnreachable` degrades nothing.
- **The capture writes no secret.** Assert on log content, with the key present in both a URL and
  a body.
- `GET /api/settings/client-types` returns the registry's declared config schemas.

## Conventions to honor

- `from __future__ import annotations`; match `settings_arr.py`'s and `arrclient.py`'s docstring
  density — say *why*, cite spec sections.
- No new runtime dependency.
- Record non-obvious decisions in `docs/decisions.md`, newest at top.
- **Do not touch `core/arrsync.py`** (spec §9's explicit instruction) and do not modify the stage
  0/1a modules except where genuinely required — if you must, say so in the report.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate.** Foreground, with an explicit timeout of at least 600000 ms. The
previous agent on this feature backgrounded `pytest`, received no completion notification, and
stalled indefinitely — do not repeat it. **Run from the REPO ROOT, never `backend/`** (from
`backend/` pytest collects zero tests and exits 0, which looks exactly like a pass).

Run each separately, read each exit code:

1. `uv run pytest` (~4.5 min)
2. `uv run ruff check .`
3. `uv run ruff format --check .`

## When done

1. Update frontmatter (`status`, `completed`, `result`); `git mv` to `prompts/done/`.
2. Record decisions in `docs/decisions.md`.
3. **Do not commit.** Report: files, three exit codes, backend test count, a proposed one-line
   `feat:` message, anything in the spec found wrong or underspecified, and — specifically —
   **what a user must do in the API to get a real capture out of a live SABnzbd**, since that is
   the next step after this lands.

Never `git add -A`, never push. Branch is `dev`.
