---
name: 2026-08-16-path-browse-dialog
status: completed
created: 2026-08-16
model: sonnet
completed: 2026-08-16
result: Browse dialog for Queues' path fields (issue #4) shipped, plus two mid-run additions -- save-time path validation (hard local, best-effort remote) and mount-gate audit events -- all backend/frontend gates green.
---

# Task: Browse dialog for Settings path fields (issue #4)

GitHub issue #4: "When selecting file paths in settings we have access to both the remote
host and the local host. We should add a browse dialog that allows path selection based on
both sides." Add a directory-browse dialog to the Settings path inputs, backed by two new
list-directory endpoints — one for the container's local filesystem, one for the seedbox over
the already-pooled SSH connection. Design settled with the user 2026-08-16 (this prompt is the
record); build it, don't re-litigate it.

## Before you start

- Read `DESIGN.md` first (required by `CLAUDE.md`) — especially §2 (API/WS shape), §9.2
  (Settings pages), §11 (privilege/identity model), §8 (auth: everything under `/api/` is
  default-deny via `middleware.py.AuthMiddleware`; confirm your new routes are NOT added to
  the public allowlist).
- Read `docs/decisions.md` back through the 2026-08-15 entries — match its conventions for
  recording your own decisions.
- Look at these before writing code, they carry the conventions to match:
  - `backend/lftpweb/api/settings_queues.py` and `api/settings_arr.py` — router/pydantic
    conventions, input length caps (the S3 audit standard: cap all string inputs).
  - `backend/lftpweb/core/remote.py` — `RemoteConnectionPool` (`get_connection`,
    `start_sftp_client` usage in `_run_fallback`), `HostConfig` loading
    (`core/engine.load_host_config`), `credentials_need_reentry`.
  - `frontend/src/pages/settings/QueuesTab.tsx` — form conventions, and the
    disabled-with-hint pure-predicate pattern (`arrDeleteCompletedDisabled`) with its tests in
    `QueuesTab.test.ts`.
  - `frontend/src/components/FileTree.tsx`'s delete-confirmation dialog — the modal styling
    to match.
  - Frontend pure logic goes in `frontend/src/lib/` with Vitest tests — the repo's settled
    pattern (`lib/fileTree.ts`, `lib/transferPanel.ts`).

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this plan
needs to modify. If any of those files have uncommitted changes, list them and ask before
touching them. Surface unrelated dirty files once as awareness; don't block. This file (the
handoff prompt itself) is exempt — it's expected to be modified by "When done" below.

## Scope — which fields get a Browse button (settled)

| Field | Tab | Side |
|---|---|---|
| `remote_path` | Settings → Queues | remote |
| `local_path` | Settings → Queues | local |
| `staging_path` (labelled "Final destination") | Settings → Queues | local |

**Deliberately excluded** (record in a code comment where the buttons are wired, and in
`docs/decisions.md`): `arr_visible_path` — it is the path *as the *arr's own host sees it*;
neither this container nor the seedbox can list it, so a browser there would be actively
misleading. `key_path` (Settings → Connection) — a file, not a directory, and the pasted-key
alternative is already preferred.

## What to do

### 1. Backend — two list-directory endpoints (new module, e.g. `api/browse.py`)

Both return the same response shape, roughly:
`{ "path": "<resolved absolute dir actually listed>", "parent": "<absolute parent or null at
/>", "entries": [{"name": "..."}...], "truncated": false, "fallback_from": "<original request,
only when the resolver walked up>" }`. Entries are **directories only**, sorted by name; a
symlink that resolves to a directory counts as one. Register the router in `main.py` the same
way the other settings routers are registered.

- `GET /api/browse/local?path=...`
  - Resolve server-side: empty or non-absolute input (including `~`, which is meaningless in
    the container — the app user has no real home) → fall back to `/`. A path that doesn't
    exist, isn't a directory, or can't be listed (PermissionError) → walk up to the **nearest
    listable ancestor** and return that, with `fallback_from` set. `/` itself failing is the
    only 500-worthy case; bad input must never 500.
  - This deliberately exposes the container's whole filesystem tree to any authenticated
    user — that is the feature (volumes can be mounted anywhere). Auth-gating comes free from
    the default-deny middleware; assert in a test that the route 401s unauthenticated in
    `password` mode (see phase 8's route-enumeration test for the pattern — check whether it
    auto-covers new routes, and if it does, say so instead of duplicating it).
  - Cap `path` input length like every other string input (S3 standard). Cap `entries` at a
    server-side maximum (500, mirroring `MAX_LIMIT` precedent) with `truncated: true`.
- `GET /api/browse/remote?path=...`
  - No host configured, or `credentials_need_reentry` → a clean 409 with a message the dialog
    can show verbatim (match the wording style of existing 409s). Connection/listing failure →
    surfaced as an error the dialog shows, never a 500 traceback.
  - Use the engine's existing pool (`app.state.engine.pool` — the same seam
    `PostprocessPipeline` and `ArrSyncScheduler` already use) and `conn.start_sftp_client()`.
  - `~` and relative paths resolve against the SSH user's home (SFTP `realpath`). Same
    nearest-existing-ancestor walk-up as local; ultimate fallback is the home directory, then
    `/`.
  - Directories-only via SFTP attrs; treat symlink-to-dir as a directory.

### 2. Frontend — one shared dialog

- A `PathBrowseDialog` component (modal, styled like the delete confirmation) + a small
  `Browse…` button rendered beside each in-scope input. Props: `side: 'local' | 'remote'`,
  `initialPath` (the field's current text), `onSelect(path)`.
- Behavior (settled with the user):
  - The dialog opens at the **field's current value**, resolved by the endpoint — so a
    half-typed `~/downloads/rtor` opens at `~/downloads` (remote) via the ancestor walk-up.
    When `fallback_from` is set, show a small one-line note ("showing nearest existing
    directory").
  - Navigation: click a subdirectory to descend, an up-entry / breadcrumb to ascend, a
    path readout of where you are. **Select** writes the endpoint-resolved **absolute** path
    into the field (never the `~` form — the stored value feeds `find`/lftp/`rm -rf --` and
    must be unambiguous). A hand-typed path the user never browses stays untouched.
  - Remote-side Browse renders disabled-with-hint when no host is configured — reuse
    whatever host-status reading Settings already has access to (see `CredentialsBanner`/
    ConnectionTab's data source) rather than a new poll; follow the
    `arrDeleteCompletedDisabled` pure-predicate pattern so the rule is unit-testable.
  - Errors from the remote endpoint render inside the dialog with the server's message; the
    dialog stays open so the user can retry or cancel.
- Keep navigation/resolution display logic as pure functions in a new `lib/` module (e.g.
  `lib/pathBrowse.ts`) with Vitest coverage; the component stays thin.

### 3. Not in scope (name it, don't build it)

No "create directory" affordance, no file selection, no multi-select. Say so in the
`docs/decisions.md` entry.

### 4. Tests

- Backend: new test module for both endpoints. Local: build real trees under `tmp_path` —
  exact-path open, ancestor walk-up (nonexistent tail, file-not-dir, permission-denied dir),
  non-absolute → `/`, truncation cap, input-length cap. Remote: follow the existing convention
  for tests that need the fake seedbox (see how `tests/` marks/skips seedbox-dependent tests)
  — cover `~`/relative resolution against the SSH home, walk-up, and the no-host 409 (that one
  needs no seedbox).
- Frontend: Vitest for the `lib/pathBrowse.ts` pure functions and the disabled-with-hint
  predicate.

### 5. Docs — same commit

- `CHANGELOG.md` `[Unreleased]` → **Added**: the browse dialog (reference issue #4). Also add
  under **Fixed** the entry that commit `4ecf5dc` (the *arr `gone`-commit `REMOVED_BOTH`
  resurrection fix, already on `dev`) should have carried — it was missed at the time; one
  line, matching the existing entry style.
- `DESIGN.md`: if §9.2/§12 enumerate the Settings capabilities/API surface where this
  belongs, update them in the honest, minimal way the repo's prior sessions have (look at how
  §16/*arr additions were handled). If it genuinely fits nowhere, say so in your report
  instead of forcing it in.
- `docs/decisions.md`: newest-at-top entry — scope exclusions (`arr_visible_path`,
  `key_path`), tilde/absolute-path policy, ancestor walk-up, the local-endpoint
  exposes-container-FS statement, and any rejected alternatives you actually considered.
- `prompts/startnewsession.md`: add a row **T** to the build-run table (after row S), same
  style, same commit.

### 6. Verify — each gate separately, read each exit code

`uv run ruff check backend tests` · `uv run ruff format --check backend tests` ·
`uv run pytest` (full suite) · in `frontend/`: `npm test -- --run`, `npm run lint`,
`npm run build`. All must pass. Do not conflate `ruff check` with `ruff format --check`.

## Conventions to honor

- Comment style: constraints the code can't show, matching the density of the files you touch.
- Conventional-commit prefix (`feat:`), no `Co-authored-by:` trailers.
- Never `git add -A`, never auto-commit, never push.

## Scope addition (2026-08-16, mid-run)

The user added two pieces mid-run, after backend research for the browse dialog was already
underway (both reuse the same `core/browse.py` machinery and touch the same
`api/settings_queues.py`/`core/autoqueue.py` files), prompted by a real incident hit the same
day: a mistyped `local_path` in Settings → Queues saved silently, and the only symptom anywhere
was a WARNING log line once auto-queue's mount gate later refused to act.

1. **Save-time path validation on `POST`/`PUT /api/settings/queues`.** `local_path` (always)
   and `staging_path` (when set) are hard-validated — must be a real, readable directory on the
   container's own filesystem, or the save is rejected (4xx) naming the field, the path, and
   what's wrong (missing / not a directory / unreadable). Never auto-creates the directory.
   `remote_path` is best-effort: checked over the pooled SFTP connection only when a host is
   configured, reachable, and its credentials decrypt; otherwise the save is allowed (a seedbox
   outage must never lock the user out of editing settings) — this asymmetry, and why, belongs
   in `docs/decisions.md`. Frontend: the server's validation message surfaces inline at the
   queue form.
2. **Mount-gate audit events in `core/autoqueue.py.on_scan`.** The existing gating-transition
   guard (the one that already logs a WARNING exactly once per gating episode) also writes an
   `event` row via `core/audit.py.record_event` — `level="warning"`, `kind="autoqueue_gated"` —
   reusing the existing `self.gated` dict as the sole debounce (no second mechanism, no event
   per scan pass). Recovery writes a `level="info"`, `kind="autoqueue_ungated"` event, but only
   when the gate was actually blocking the queue a moment ago — the `auto_queue_enabled=False`
   early-return branch's pop stays silent, since turning auto-queue off is not a gate recovery.

Both are covered by their own tests and folded into the same `docs/decisions.md` entry, the same
CHANGELOG `[Unreleased]` additions, and this task's row **T** in `prompts/startnewsession.md`,
rather than as separate handoffs.

## When done

1. Update this file's frontmatter: set `status` (completed/failed), `completed` (the date),
   and `result` (one line).
2. `git mv` this file into `prompts/done/` (on success) or `prompts/failed/` (on failure).
3. Record the non-obvious decisions in `docs/decisions.md` (see §5 above).
4. Hand off ONE commit covering this prompt file, the files this session modified, and the
   prompt move. **You are a spawned agent: do not commit.** Prepare the working tree, then
   report the file list + proposed `feat:` message (reference issue #4) back to the
   orchestrating session, which surfaces the `y/n` to the user.
