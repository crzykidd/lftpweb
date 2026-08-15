---
name: 2026-08-13-settle-progress-visibility
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Shipped. Migration 013 added item_settle.first_observed_at/last_changed_at. The Files page
  now shows "Arriving · 3.4 GB" / "Still arriving — 3.4 GB, changed 12s ago — watching for 3m"
  while settle_matched_scans == 1 (no confirmed match yet), and the existing "Waiting 1/2 ·
  35s" countdown unchanged once matched_scans >= 2. A same-shaped fix at core/settle.py's
  counter itself (starting matched_scans at 0 instead of 1 on a real change) was tried first,
  found to silently turn "2 consecutive unchanged scans" into 3, was caught by
  tests/test_settle_gate_e2e.py, and reverted in favor of the new last_changed_at field
  instead -- matched_scans/REQUIRED_SETTLE_SCANS/is_settled are all untouched. 767 tests
  passing (was 760), both ruff gates clean, oxlint clean, tsc+vite build clean. Full account in
  docs/decisions.md.
---

# Task: Show that a still-arriving item is actually arriving

User report, 2026-08-13, copying a large directory onto the seedbox:

> copying a large physical directory on remote host. files land, the scan validate process
> works as it keeps saying change so doesn't start. however the counter stays at 1/2 … that
> gives the user the ability to see how many scans we have done waiting for it to complete

The settle countdown is pinned near `1/2` for the whole copy, so it conveys nothing about
progress. The user proposed incrementing the denominator (`2/3`, `3/4`…); **that was discussed
and rejected together — the requirement is not growing, it is always 2 consecutive unchanged
scans, and a climbing denominator would state something false.** Agreed direction:

- **Keep the fraction honest at `n/2`.**
- **While the fingerprint is still changing, show that it is changing and how it is
  progressing** — the byte count climbing is the real progress signal.
- **Once it stops changing**, the existing `Waiting 1/2 · 35s` countdown becomes meaningful and
  stays as-is.

## What to build

### Migration 013

`item_settle` currently holds `queue_id`, `rel_path`, `file_count`, `total_bytes`, `max_mtime`,
`matched_scans`, and `updated_at` (repurposed by `855e7a3` as the first-matched timestamp that
backs `SETTLE_MIN_AGE_S`). Add what is missing to answer "how long have we been watching, and
when did it last move":

- `first_observed_at` — when this item first entered the settle tracker.
- `last_changed_at` — set whenever the fingerprint differs from the stored one.

Check nothing has claimed 013 before you start. Existing rows get NULL; handle that as "unknown"
in the display rather than inventing a time.

### The two display states

Both are the same `substate = 'settling'` — the distinction is whether the last scan matched.

- **Still arriving** (`matched_scans == 0`, i.e. the fingerprint changed on the most recent
  scan): something like `Still arriving · 3.4 GB · changed 12s ago`. The size is
  `item_settle.total_bytes`, **already stored** as part of the fingerprint — watching it climb
  is what tells the user the seedbox copy is progressing. Include how long we have been
  watching if it fits; the user asked for that directly.
- **Settling** (`matched_scans >= 1`): the existing `Waiting n/2 · Xs` countdown, unchanged.

Keep both short — this sits in a fixed-width cell that has already been trimmed once for
clipping (`a4a626d`). Put the fuller phrasing in the chip's `title`, as the current label
already does.

### Projection

Surface the new fields through `core/itemview.py` **gated on `substate == 'settling'`, exactly
as the existing settle fields are.** That gating is load-bearing, not tidiness: exposing settle
fields unconditionally made every top-level row compare as changed on every scan to
`diff_nodes`'s whole-dict equality check, which reintroduced full-tree WebSocket traffic and was
caught by `tests/test_ws_deltas.py`. Read that decision entry before touching the projection,
and do not widen the gate.

## Before you start

- `core/settle.py` — the fingerprint, `advance_settle`, `is_settled`, `REQUIRED_SETTLE_SCANS`,
  `SETTLE_MIN_AGE_S`.
- `core/engine.py._persist` — where settle records are advanced.
- `core/itemview.py` — the settle fields and their `substate` gate.
- `frontend/src/lib/format.ts` — `settleWaitShortLabel`, added in `a4a626d`.
- `frontend/src/components/StateChip.tsx` — the label/title split.
- `tests/test_ws_deltas.py` — what it asserts about payload size.

## Working tree check

`git status --porcelain`. A task may be in flight in `FileTree.tsx`/`format.ts` (the both-sides
hover card). If those are dirty, stop and ask rather than racing — that file has taken five
significant changes today.

## Tests

- A fingerprint that changes across scans keeps `matched_scans` at 0 and updates
  `last_changed_at`; one that holds still advances the counter and leaves `last_changed_at`
  alone.
- `first_observed_at` is set once and never moved by later scans.
- The new fields are absent from the projection when `substate != 'settling'` — **and
  `tests/test_ws_deltas.py`'s payload/tree-size assertions still hold.** This is the regression
  that has already happened once; guard it explicitly.
- NULL `first_observed_at`/`last_changed_at` on pre-migration rows render as unknown, not as
  1970 or a crash.

## Conventions to honor

- `docs/decisions.md`, newest at top — record why the denominator stays at 2, since the user
  proposed otherwise and a future reader will wonder.
- `CHANGELOG.md`; `DESIGN.md` §3.3 (standing approval to edit directly).
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`; `uv run pytest` with the fake seedbox up.
- **There is no frontend test runner** — do not add one. Write the label logic as a pure
  function and cover the backend side properly.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `feat:` message, the exact
   labels you chose for both states, test count, lint results, and anything not fixed. Never
   `git add -A`, never push.
