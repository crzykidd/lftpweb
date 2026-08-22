---
name: 2026-08-22-client-base-paths-detected
status: completed        # pending | completed | failed
created: 2026-08-22
model: sonnet            # coding
completed: 2026-08-22
result: >
  Base paths now detected from the client and SSH-verified (verified/not_found/unverified kept
  distinct), never saved on the client's word alone; save-on-test added for enabled instances
  (§3a). Backend: migration 028, core/clients/detection.py, BasePath.kind (BasePathKind enum
  replacing label), settings_clients.py create/update/test rewritten. Frontend:
  lib/clientBasePathDetection.ts + ClientsTab.tsx detect/accept/translate UI. Spec §8.2/§2.1
  rewritten with the correction recorded. All gates green: 1811 backend tests, 705 frontend
  tests, ruff + build + lint clean. See docs/decisions.md 2026-08-22 entry for the full
  reasoning.
---

# Task: Base paths are detected from the client and SSH-verified, not typed in

Replace "the user types base paths, the client's answer is a prefill" with **detect → verify →
confirm**. The client already reports its own directories *and* their roles; the only thing it
cannot know is whether lftpweb sees them at the same path over SSH.

## The correction this implements

`docs/download-client-framework-spec.md` §8.2 currently says base paths are user-configured and
`list_base_paths` is a prefill only. **That reasoning was wrong** (settled with the user
2026-08-22) and this task fixes both the code and the spec:

- The stated justification was "rTorrent's `directory.default` will never mention the completed
  folder it hardlinks into." True, but irrelevant — **that folder is the queue's `remote_path`,
  which lftpweb already knows.** It never needed declaring.
- `SabnzbdClient.list_base_paths` already returns `complete_dir` and `download_dir` labelled
  `complete`/`incomplete`. The **role is already known** because the connector knows which config
  key it read each path from. Asking the user to classify them pushes a question outward that the
  API already answers.
- **The real reason user input is ever needed is path-namespace translation**, which §8.2 never
  named. lftpweb reaches the seedbox over SSH; a containerised client reports paths in its own
  filesystem view (`/complete` vs `/home/user/downloads/complete`). This repo already solved the
  identical problem for the *arr with `path_queue.arr_visible_path` (migration 018) — **mirror
  that design, including its naming logic.**

## Before you start

Read:

1. **`docs/download-client-framework-spec.md`** §8.2 (the section you are rewriting), §2.1
   (`list_base_paths`), §10.2 (base paths are the delete containment boundary — this is why the
   field matters), §11 (the scan's roots), §13.4 (guess #7: whether SAB's reported `complete_dir`
   is even valid over SSH is *unverified* — this task turns that from a guess into something the
   UI states).
2. **`backend/lftpweb/migrations/018_arr_integration.sql`** — `path_queue.arr_visible_path` and
   its comment. The foreign-view/native-view split you are mirroring.
3. **`backend/lftpweb/core/browse.py`** — `remote_directory_error(sftp, path)` raises
   `RemotePathNotFoundError` iff the seedbox clearly reports the path missing or not a directory,
   and deliberately lets ambiguous failures propagate as themselves. **Respect that distinction:
   "not found" is a verified mismatch; a permission error is not.**
4. **`backend/lftpweb/api/settings_clients.py`**, **`core/clients/models.py`** (`BasePath`),
   **`core/clients/sabnzbd.py`** (`list_base_paths`), **`frontend/src/pages/settings/ClientsTab.tsx`**.
5. How an existing endpoint obtains an SFTP client for the configured host — `api/browse.py` is
   the shortest example. Reuse it; do not open your own connection.

## Working tree check

`git status --porcelain` — should be clean at `7ebccb5`. This prompt file is exempt.

## What to do

### 1. `BasePath` gains a real role

Replace the free-text `label` with a closed `BasePathKind` enum:

- **`content`** — finished content lands here; lftpweb syncs from it; commonly shared between
  clients and overlapping a queue's `remote_path`. SAB's `complete_dir`.
- **`working`** — the client's own working/seeding storage. SAB's `download_dir` (incomplete);
  rTorrent's `directory.default` when that connector lands.
- **`unknown`** — a connector that reports a path but cannot say what it is for.

The distinction is **not cosmetic**: it decides what deleting there means. Removing a file in a
`content` root that is hardlinked from a seeding torrent frees nothing (spec §10.5); removing it
in the `working` root frees the space and kills the seed. Put that in the enum's docstring.

Update `sabnzbd.py` to return the enum instead of `"complete"`/`"incomplete"` strings. Keep the
existing UNVERIFIED marker — the *mapping* is now typed, the *source keys* are still doc-derived.

### 2. Migration `028_client_base_path_detection.sql`

Additive columns on `download_client_base_path`:

- `kind` — the enum value; default `unknown` so an existing row (there are none in practice, but
  do not rely on that) is honest rather than mislabelled.
- `client_path` — **what the client reported, when it differs from `path`. NULL = no translation
  needed.** Exactly `arr_visible_path`'s semantics, inverted to match this direction: `path` stays
  the SSH-visible path lftpweb actually scans and deletes within; `client_path` records the
  client's own view for display and diagnosis.
- `source` — `detected` or `manual`, so the UI can show which is which and a re-detect can leave
  manual entries alone.

Follow 018's commenting style: say *why* each column exists.

### 3. Test-connection also detects and verifies paths

Extend `POST /api/settings/clients/{id}/test`:

1. If the connector declares `Operation.LIST_BASE_PATHS` (check via `capabilities.supports(...)`,
   accepting derived), call it. **A connector that does not declare it simply detects nothing** —
   that is not an error and must not fail the test.
2. For each reported path, verify it over SSH with `core/browse.py.remote_directory_error`.
3. Return each as one of three states, and **do not collapse them**:
   - `verified` — the client reported it and lftpweb can see it at the same path.
   - `not_found` — the client reported it and it does not exist over SSH. **This is the namespace
     mismatch, detected rather than asked about.** The user supplies the SSH-visible equivalent.
   - `unverified` — the stat failed for any other reason (permission, protocol). **Not the same as
     `not_found`** — `remote_directory_error`'s own docstring makes exactly this distinction, and
     collapsing it would tell a user their path is wrong when lftpweb simply could not look.

**Detection proposes; it never saves.** Same rule as the category→queue inference already in the
UI. The response carries the proposal; the user confirms.

**Do not let a detection failure fail the connection test.** Reachability and detection are
different questions — spec §4.2's temperament. Report both outcomes separately.

### 3a. Test on save — and refuse to save an **enabled** instance whose test fails

**The user's requirement, 2026-08-22: "we should test at save and not save if enabled and test
failed — this is how the arr clients handle it."** Sonarr/Radarr's own Download Clients page tests
the submitted settings on save and refuses to accept a broken enabled client; lftpweb's Clients
page should behave the same way, because a saved-but-broken client is a config the user believes is
working and isn't.

Applies to both `POST /api/settings/clients` and `PUT /api/settings/clients/{id}`:

- **`enabled: true` → test the *submitted* config before persisting.** On failure, return a 4xx
  carrying the real error message and error class, and **persist nothing** — not the instance, not
  a partial row.
- **`enabled: false` → never test.** Saving a disabled instance always works.
- **On success, persist the probed capabilities and version from that same test** (spec §4.1's
  probed layer). The user should not have to save and then click Test to get a populated capability
  readout — one round trip already produced it.

**The test must run against the submitted payload, not a stored row.** On create there is no row
yet, so the connector is constructed from the request body. Do not persist-then-test-then-rollback;
build the connector from the submitted config, test, and only write on success.

**`enabled: false` is the deliberate escape hatch, and the UI must make it obvious.** If a client
is temporarily unreachable, a hard refusal with no way out would lock the user out of editing their
own settings — including fixing the typo that broke it. Disabling always saves, so the recovery
path is "uncheck Enabled, save, fix, re-enable." Say so in the error the UI surfaces rather than
leaving the user to discover it. **Do not invent a force/save-anyway flag** — that decision has not
been made, and the disable path already covers the need.

Keep `POST /api/settings/clients/{id}/test` as it is, for re-testing something already saved.

### 4. UI: confirm what was found

In `ClientsTab.tsx`, base paths become a **detected list you confirm**, not a field you fill:

- After a test, show detected paths with their kind and state.
- `verified` → one click to accept.
- `not_found` → show what the client reported and prompt for the SSH-visible path, with the
  existing **Browse…** picker to find it. Make the reason explicit — *"SABnzbd reports `/complete`,
  which doesn't exist over SSH. Which path is it here?"* — not a bare validation error.
- `unverified` → say lftpweb could not check, and allow accepting anyway. Never present it as a
  failure.
- **Manual add stays** as the escape hatch for a path the API does not expose.
- Re-running detection must not clobber `manual` rows or a translation the user already supplied.

**No `if client_type === ...` anywhere** (spec §4.4/§5.1), same as the rest of this page.

### 5. Rewrite spec §8.2

Replace it with detect → verify → confirm. State plainly that the earlier "user-configured, client
is a prefill" reasoning was wrong and why, in the same style §11.1c corrects an earlier reading —
this project records reversals with their cause rather than silently contradicting them
(`docs/decisions.md` convention). Keep the one thing that *was* right: **the SSH-visible path is
authoritative for scanning and deletion**, because it is the containment boundary (§10.2). What
changed is where it comes from, not which one wins.

Also update §2.1's `list_base_paths` row — it currently reads "a prefill, not the source of truth."

## Conventions to honor

- `from __future__ import annotations`; match the surrounding docstring density; cite spec sections.
- Backend and frontend tests for: the three verification states kept distinct; detection proposing
  rather than saving; re-detect preserving manual rows and translations; a connector that does not
  declare `LIST_BASE_PATHS` detecting nothing without erroring; a detection failure not failing the
  connection test.
- Backend tests for save-on-test (§3a): an enabled instance with a failing test is **rejected and
  persists nothing** (assert the row count is unchanged, not merely that the response was 4xx); a
  disabled instance with a failing test **saves fine**; a successful save **populates capabilities
  and version** without a separate Test click; and an update that disables a previously-enabled
  broken instance succeeds (the escape hatch actually works).
- Record decisions in `docs/decisions.md`, newest at top.
- Doc updates ship in the same commit as the code.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate.** Every gate Bash call MUST pass an explicit timeout of at least
600000 ms. Three agents on this feature have already backgrounded `pytest`, received no completion
notification, and stalled for ~25 minutes each. **Run backend gates from the REPO ROOT, never
`backend/`** — from `backend/` pytest collects zero tests and exits 0, which looks like a pass.
**If you `cd` into `frontend/`, `cd` back before running backend gates** — the working directory
persists between calls and this has already produced a false "no tests ran" result in this session.

Run each separately, read each exit code:

1. `uv run pytest` (~4.5 min)
2. `uv run ruff check .`
3. `uv run ruff format --check .`
4. Frontend: `npm run build`, `npm run lint`, `npm test` (from `frontend/`)

## When done

1. Update frontmatter; `git mv` to `prompts/done/`.
2. Record decisions in `docs/decisions.md`.
3. **Do not commit.** Report: files, every gate's exit code, backend and frontend test counts, a
   proposed one-line message, and anything in the spec found wrong or underspecified.

Never `git add -A`, never push. Branch is `dev`.
