---
name: 2026-08-12-transfer-settings-tab
status: completed        # pending | completed | failed
created: 2026-08-12
model: sonnet            # coding; the one open design question is flagged to surface, not decide
completed: 2026-08-12
result: >
  Built Settings -> Transfer (TransferTab.tsx) with the required §9.3 live connection-count
  readout and a full DESIGN.md §4.5 admission-formula preview; added queue_name to JobOut and
  the Transfers page. net:connection-limit confirmed unreachable from any UI today -- surfaced
  read-only via a new HostOut.net_connection_limit field (core/remote.py.parse_connection_limit),
  not promoted to a real column. Frontend build/lint clean; backend ruff format/check clean;
  386 pytest tests pass; GET/PUT /api/settings/transfer and GET /api/settings/host,
  /api/jobs verified over real HTTP against the running dev stack. Full reasoning in
  docs/decisions.md (2026-08-12, top entry). Not committed/pushed per instructions.
---

# Task: Build Settings → Transfer, and show the queue on the Transfers page

Two related §9.2/§9.3 UI gaps, both against backends that already exist.

**Part A.** `Settings → Transfer` (`pages/settings/TransferTab.tsx`) still renders
`PagePlaceholder` even though `GET`/`PUT /api/settings/transfer` have been complete and tested
since phase 3a (they live in `api/jobs.py`, not `api/settings.py`). Every site-level bandwidth
and concurrency knob is reachable today only by hand-crafting an HTTP request. This is the
largest single hole in the UI — see `README.md`'s "Known gaps".

**Part B.** The Transfers page doesn't show which queue a row belongs to. `JobOut` carries
`queue_id` but no `queue_name`, so the information isn't even on the wire.

## Before you start

- **Read `DESIGN.md` §4.5 and §9.3 in full before writing any code.** §9.3's live
  connection-count readout is marked "required, not a nice-to-have" and is the whole reason
  this tab is not just a list of number inputs. Read §9.2 for the Transfers page.
- Read `core/queue.py`'s `TransferSettings` dataclass — it is the exact contract, twelve
  fields, and its `effective_small_lane_reserve_bps()` docstring documents a trap the UI has to
  surface (below).
- Match the existing settings tabs' structure and idiom — `PostProcessingTab.tsx`, `QueuesTab.tsx`,
  and `BackupTab.tsx` are the models to follow for form/save/error shape. **Do not introduce
  TanStack Query**; this project hand-rolls its fetch client and poll hook on purpose
  (`docs/decisions.md`, phase 3b). Follow the local convention.

## Working tree check

Run `git status --porcelain` first and cross-reference. Expect several dirty files from an
in-flight local session: dev-environment fixes (`docker/Dockerfile`, `docker-compose.dev.yml`,
`docker-compose.test.yml`, `frontend/vite.config.ts`), a mount-sentinel scan filter
(`backend/lftpweb/core/local_scan.py`, `tests/test_local_scan.py`), and an extraction change
(`core/extract.py`, `core/postprocess.py`). **None of them are yours — do not revert, refactor,
or "tidy" them.** `CHANGELOG.md`, `standards.md`, `prompts/startnewsession.md`, and
`.claude/commands/release-prep.md` were dirty before the session; leave them alone.
`docs/decisions.md` is shared — append your entry at the top without disturbing existing ones.

If any file **Part A or B needs to modify** is dirty, list it and ask before touching it.

## Part A — the Transfer settings form

1. **All twelve fields, grouped so the groups mean something** — not one flat list:
   - *Bandwidth*: `max_bandwidth_bps`, `min_share_floor_bps`
   - *Concurrency*: `max_concurrent_transfers` (jobs at once),
     `mirror_parallel_transfer_count` (files at once within one job), `mirror_use_pget_n` and
     `pget_default_n` (connections per file)
   - *Fast lane*: `small_item_threshold_bytes`, `small_lane_concurrency`, `small_lane_reserve_bps`
   - *Retry*: `max_attempts`, `retry_backoff_base_s`
   - *Escape hatch*: `extra_lftp_settings` (free text, injected verbatim into every job's rc file)

   Bandwidth and size fields are stored in **bytes per second** and **bytes**. Do not make the
   user type `10000000` — accept and display sensible units (MB/s, MB) and convert at the edge,
   round-tripping without drift.

2. **The live connection-count readout (§9.3) — required.** As the user changes max concurrent
   jobs, `mirror_parallel_transfer_count`, and `mirror_use_pget_n`, compute and display the
   worst case continuously:

   ```
   2 jobs × 4 parallel × 4 pget-n = 32 concurrent SFTP sessions   ⚠ over net:connection-limit (16)
   ```

   These three numbers multiply *silently* — nothing in lftp's output tells the user they just
   asked for 32 sessions, and seedboxes refuse connections well below what the inputs will
   happily accept. Warn when the worst case exceeds the host's `net:connection-limit`, and show
   the resulting per-job bandwidth cap next to it. **This is the point of the tab; a form
   without it has not completed this task.**

3. **Surface the derived fast-lane reserve.** `small_lane_reserve_bps` is nullable: `null` means
   "derived — 10% of the ceiling, min 1 MB/s", and `effective_small_lane_reserve_bps()` then
   clamps it to at most half the ceiling. That clamp is load-bearing: without it, any ceiling at
   or below 1 MB/s yields a reserve ≥ the ceiling, so the main lane admits nothing, ever, and
   jobs sit queued with no error and no log line. The UI must show the **effective** value when
   the field is left on "derived", so that clamp is visible rather than invisible.

4. **Validation matches the backend's.** Don't invent client-side rules the API doesn't enforce,
   and don't let the form submit values the API will reject. Check `TransferSettingsIn` in
   `api/jobs.py` for what is actually validated, and report any field where the two disagree.

## Part B — show the queue on the Transfers page

5. Add `queue_name` to `JobOut` (`models.py`) and populate it in `core/queue.py.list_jobs()`.
   A join onto `path_queue` is fine here: `list_jobs()`'s row set is bounded by construction
   (its own docstring explains why) — this is **not** the unbounded-endpoint case that
   `api/history.py` deliberately avoids inlining blobs for. Do not copy this pattern to History
   without the same reasoning.

6. Render it on `TransfersPage.tsx`. A queue column or a per-row label both work; pick whichever
   fits the existing three-word visible vocabulary (§9.2) without crowding the row, and say why
   in your report. If multiple queues are active the user must be able to tell rows apart at a
   glance — that is the whole point.

7. Update any test that asserts on `JobOut`'s shape.

## Surface, do not decide

- **`net:connection-limit` is not a first-class setting, and §4.5 says it should be.** Today it
  is dug out of a JSON `connection_overrides` blob on the `host` row
  (`core/queue.py._connection_limit`), while §4.5 says it is "a **first-class setting**,
  host-level, not an advanced afterthought". Part A's warning has to read it from somewhere, so
  you will hit this. **Read it from where it currently lives and report the divergence** —
  including whether Settings → Connection exposes any way to set it at all. Promoting it to a
  real column is a schema change with its own migration and is explicitly **not** in scope here.
  If it is unreachable from the UI entirely, say so plainly; the warning then renders only when
  a limit happens to be configured, and that limitation belongs in your report and in
  `README.md`'s "Known gaps".

## Conventions to honor

- Comments explain **why**, matching the surrounding density and voice. Cite `DESIGN.md`
  sections (`§4.5`, `§9.3`) where a decision traces to one.
- Frontend gates: `npm run build` and `npm run lint` both clean.
- Backend gates if you touch Python: `uv run ruff format --check` **and** `uv run ruff check`
  (run the format check explicitly — it has caught files `check` alone missed four times in this
  project), plus `uv run pytest`.
- **No browser exists in this environment.** You can build, type-check, and lint, and you can
  exercise every endpoint over real HTTP against the running dev stack at `http://localhost:8087`
  — do that for both `GET` and `PUT /api/settings/transfer`. Do **not** claim the UI renders
  correctly; say exactly what you verified and what you did not.
- The dev stack and fake seedbox are **running and in use by the user** (`lftpweb-backend-1`,
  `lftpweb-frontend-1`, `lftpweb-test-seedbox-gnu`, `lftpweb-test-seedbox-busybox`). Leave them
  running. `docker compose -f docker-compose.dev.yml restart backend` to pick up backend changes;
  the frontend hot-reloads. Do not disturb `/data/pickup` on the seedboxes.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record non-obvious decisions in `docs/decisions.md`, newest at top — the unit-conversion
   choice, the queue-label placement, and anything rejected.
4. **Do not commit. Do not push.** The user is iterating locally with a working tree that
   already carries unrelated in-progress changes. Prepare the tree, then report back to the
   orchestrating session with the file list and a proposed one-line commit message
   (`feat:` prefix, no `Co-authored-by:` trailer).
