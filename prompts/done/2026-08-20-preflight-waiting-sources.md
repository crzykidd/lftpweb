---
name: 2026-08-20-preflight-waiting-sources
status: completed        # pending | completed | failed
created: 2026-08-20
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-20
result: Settle-gated releases added as Preflight's second source (core/autoqueue.py, no reshape
  of core/preflight.py needed); mount-gated queues surfaced as a banner (PreflightResponse.
  gated_queues); settle wins over *arr on a cross-source tie via a new _merge_preflight_rows
  helper in api/jobs.py. 1566/1544 backend and 573/573 frontend tests pass (0 skipped); all
  gates green. Browser-unverified.
---

# Task: Preflight also holds non-*arr items in a wait state, and warns when a queue is mount-gated

**Follow-up to the Preflight box.** That task built the box with an *arr-sourced projection and a
**source-agnostic row model** (see its report and `docs/decisions.md` for where the source boundary
sits). This task adds the second source through that boundary — it should not need a refactor.

User's framing, 2026-08-20:

> *"we should list items in preflight that are in a wait state. They will hit there naturally if
> they are from arr, but non arr items should show up there in the waiting state and remote -
> xxgig. That is a natural place for them in preflight."*

Preflight is the **waiting room**: things lftpweb knows about but has no work to do on yet,
whatever the reason.

## What goes in — and what deliberately does not

### IN: settle-gated releases (rows)

`core/settle.py`'s gate holds a release that is **still being uploaded to the seedbox** until its
remote fingerprint (file count, total bytes, newest mtime) holds still across two consecutive scans
**and** at least 60 s of wall clock. On by default.

During that hold, lftpweb has seen the release, knows its **remote size**, and is deliberately not
transferring it. That is the definition of preflight — and today those items appear **nowhere on
the Queue tab**, which is exactly how "nothing is happening and I do not know why" becomes
possible.

Show the remote size on the row, per the user's `remote — 22 GB` shape.

### IN: mount-gated queues (a BANNER, not rows) — decided with the user

When a queue's local root fails the sentinel check, `core/autoqueue.py` skips **the entire
queue's** auto-queue pass — nothing enters the queue at all. That is a wait state, and arguably the
most useful thing this box can tell anyone, because today the only signals are a log line and an
`AutoQueue.gated` API field almost nobody reads.

But it is a different *shape*: not "these items are waiting" but "**this whole queue is blocked**".
**Decided: a banner on the Preflight box** — one line per gated queue, naming the queue and the
reason — **not one row per affected item.** Fifty identical rows would bury the single fact that
matters.

`AutoQueue.gated` already carries the reason string; use it rather than composing a second one.

### OUT — and this matters, do not include them

- **Auto-queue-suppressed items** (`auto_queue_suppressed`) — a stopped or deliberately-excluded
  item is **not waiting**; nothing is coming. Listing it in a waiting room implies otherwise.
- **`REMOTE_ONLY` items matching no pattern** — the single most common row in the system. They are
  not waiting, they are simply not wanted. Including them would make Preflight a second Files tree.

If you find another genuine wait state while reading the code, **name it in your report** rather
than adding it unilaterally.

## Properties that must survive — inherited from the first Preflight task

- **Projection, not a store.** Nothing accumulates: a settle-gated item stops being projected the
  moment the gate releases it (and it becomes real queued work) or its remote disappears. No TTL,
  no cleanup sweep, none of `core/pipeline_flight.py`'s "nothing blocks forever" machinery needed.
- **Rows are inert** — no job, no bytes in flight, no queue position. No chevrons, no Dismiss, no
  Start now, no Stop, no `#N`.
- **No duplicate at handover.** The instant a settle-gated item is released and gets a job, its
  preflight row must go and the Active row take over. A release visible twice in one view is the
  failure to avoid — the same guarantee the *arr source already had to make.
- **The box scales to content**: height follows the row count up to the 5-row default; zero rows
  collapses to a single "Nothing in preflight" line. It sits above the real work.

## Before you start

- The Preflight prompt in `prompts/done/2026-08-20-preflight-box.md` and its `docs/decisions.md`
  entry — **the source boundary is the thing you are building against.** If adding this source
  requires reshaping it, say so in your report; that is a signal the boundary was drawn wrong.
- `core/settle.py` — the gate, `item_settle`, and how a hold is recorded and released.
- `core/autoqueue.py` — `AutoQueue.gated`, and `core/mount_sentinel.py.check()`'s docstring on why
  the gate is blanket-per-queue.
- `docs/transfers-redesign-spec.md` §4.6 — why preflight entries are not `item` rows.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt.

## What to do

1. Add the settle-gated source behind the existing source boundary, carrying the remote size.
2. Add the mount-gated banner to the box, one line per gated queue with its reason.
3. Make sure the two sources coexist: an *arr-sourced row and a settle-sourced row for the **same
   release** must not both appear. Decide which wins and why — the settle-gated one has strictly
   more information (it is actually on the seedbox), so it is the likely answer.
4. Row ordering across mixed sources: pick something defensible and say what it is.

## Tests

The settle-gated projection; the no-duplicate guarantee both at handover to a real job **and**
across the two sources; the mount-gate banner appearing per gated queue; and explicitly that
**suppressed** and **pattern-unmatched `REMOTE_ONLY`** items do **not** appear.

## Docs

`CHANGELOG.md` under `[Unreleased]`; `docs/concepts.md` (the Preflight entry will exist — extend
it, since "why is my release in Preflight and not downloading?" now has two answers, and the mount
banner is the one people will actually need); `DESIGN.md` §9.2; `docs/decisions.md` for the
banner-not-rows choice and the cross-source precedence.

## Conventions to honor

- **Never background a verification gate.** Foreground, with the Bash tool's `timeout` set to
  600000 ms for pytest (~3.5 min), reading each exit code. A spawned agent receives no background
  completion notification and will stall forever — a written rule in `CLAUDE.md`, and it has cost
  two manual nudges today already.
- From the **repo root** (not `backend/` — running from there collects zero tests and looks like a
  pass): `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** Say plainly what a human should check first.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
