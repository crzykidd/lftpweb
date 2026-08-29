---
name: 2026-08-29-settle-verify-under-existing-toggle
status: completed        # pending | completed | failed
created: 2026-08-29
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-29
result: Folded the client-shortened settle under client_skip_enabled (5s window, 2.5s tick),
  deleted the old trust-only path outright, and — per a mid-task scope change from the user
  ("yes, make it on by default since it verifies") — flipped client_skip_enabled's own default
  to True. Gates green: pytest 2014 passed/49 skipped (net -13, fully accounted for), ruff check
  and format clean, frontend lint/tsc/vitest all clean (839/839 frontend tests unchanged).
---

# Task: put the re-fingerprint verify behind the existing client-skip toggle, at 5s

The client-shortened settle mechanism built in `prompts/done/2026-08-24-client-shortened-settle.md`
is **sitting uncommitted in the working tree**. It works and its gates are green, but it ships
**ON by default** as a *second* mechanism alongside the older trust-only skip. The user has
rejected both of those properties. This task reworks it — it does not rebuild it.

The user's own words: *"There is a toggle already for Skip the wait on a download clients own
verdict. This setting should be the one that still does the 5s verify."*

**One toggle, one meaning: skipping the wait means verifying that nothing moved.** After this
task there must be no code path anywhere that queues on a download client's word alone.

## Before you start

Read, in this order:

1. `git diff` and `git status --porcelain` — the uncommitted mechanism you are reworking. Do not
   revert it, do not start over; it is good work with the wrong switch on it.
2. `core/settle.py` — `SettleSettings.client_skip_enabled` (~line 444), `CLIENT_COMPLETION_HOLD_S`
   and `client_completion_ready` (~583-650, the trust-only path you are deleting), and
   `CLIENT_RECHECK_INTERVAL_S` (~677, the constant you are changing).
3. `core/autoqueue.py` — the `on_scan` registration branch (~725-760, currently gated only on
   `settle_settings.enabled`) and the old `client_completion_ready` branch (~774-830, being
   deleted). Also `RECHECK_TICK_S` (~407) and `advance_pending_rechecks` (~945).
4. `frontend/src/pages/settings/TransferTab.tsx` around line 287 — the toggle's own label and
   help text.
5. `CLAUDE.md` — commit rules; gates in the **foreground**, from the repo root.

## What to do

### 1. Gate the mechanism on the existing toggle

The pending-recheck registration in `on_scan` must require `settle_settings.client_skip_enabled`
in addition to `settle_settings.enabled`. With the toggle off — which stays the **default** —
behavior is exactly today's ordinary settle gate, no registration, no ticker work, no I/O.

Do **not** add a new settings field. The whole point is that the existing toggle gains a better
meaning rather than the product gaining a second switch.

### 2. `CLIENT_RECHECK_INTERVAL_S = 5.0`

Changed from 10.0 on the user's explicit instruction. Update the constant's docstring: it can no
longer justify itself as "the same number `CLIENT_COMPLETION_HOLD_S` already settled on," because
that constant is being deleted in step 3. Say what 5s actually buys and what it risks.

**`RECHECK_TICK_S` now needs a deliberate decision, not a leftover.** It is currently `5.0`, equal
to the new window — so the second fingerprint lands somewhere between 5s and 10s after the first,
and a user who asked for a 5s verify would routinely get 10s. Pick a tick that makes the real
observed window close to 5s, and write down in the constant's docstring why that number, naming
the tick-vs-window interaction explicitly. Do not leave the two equal by accident.

### 3. Delete the trust-only path

`CLIENT_COMPLETION_HOLD_S`, `client_completion_ready`, and the `on_scan` branch that queues on a
terminal verdict once it is old enough, all go. The toggle they served now means "verify."

When the verify **cannot** run — no remote scan wired, unreachable host, partial scan, item gone
from the remote — the item falls back to the **ordinary settle gate**, never to trusting the
verdict. `advance_pending_rechecks` already does exactly this; confirm it still does after the
deletion and that no other path grew a shortcut.

Remove or rewrite the tests that covered the deleted path — `tests/test_settle_client_skip.py`
and the `client_completion_ready` region of `tests/test_settle.py` (~line 619). A test asserting
deleted behavior must not be quietly weakened into asserting nothing; delete it outright and say
so in your report. **Do not delete a test that is actually covering the new mechanism.**

### 4. Update the toggle's copy

The label at `TransferTab.tsx:287` says "Skip the wait on a download client's own verdict." Its
meaning has changed from *trust* to *verify*. Update the label and/or its help text so a user
reading the settings page learns that enabling it re-checks the filesystem rather than taking the
client's word — the honest-naming instinct this repo has applied before (finding #17's unclaimed
pile, the debris/unclaimed display rename). Keep the field name `client_skip_enabled` unchanged;
this is a copy change, not a rename through the codebase.

Touching a frontend file means the frontend gates apply — see below.

### 5. Docs, in the same change set

- `DESIGN.md` §3.3 and §17.8 — the mechanism is opt-in, and the trust-only path is gone.
- `docs/download-client-framework-spec.md` §14 (and the §9 prose mention).
- `docs/decisions.md`, newest at top — one entry: the toggle gained a stronger meaning rather than
  the product gaining a second switch; 5s chosen over 10s on the user's instruction; the
  trust-only fallback rejected because it reintroduces blind trust under a switch whose whole
  point is verification. Name the rejected alternatives.
- `README.md` — the "two overlapping mechanisms" known gap this work previously had to document
  is **now closed**. Remove it rather than leaving a stale gap.
- `CHANGELOG.md` Unreleased — rewrite the existing entry: this is no longer a default-on behavior
  change for existing installs. It is an opt-in toggle that got safer and faster.

## Tests

`tests/test_client_shortened_settle.py` exists and covers the mechanism. Extend it:

- With `client_skip_enabled` **off**, a finished client verdict registers **no** pending recheck
  and performs **no** remote I/O. Name it so it is obvious it protects the default.
- With it **on**, the existing convergence tests still pass at the 5s window.
- The fallback tests (changed fingerprint, unfetchable fingerprint) still fall back to the
  ordinary gate — these are the invariant tests; make sure they survive the rework intact.

## Conventions to honor

- Match the surrounding docstring style — `core/settle.py` explains *why* at length, including
  which earlier decision a line reverses and what caused the reversal. This task reverses a
  decision made hours ago; write that down plainly rather than making it look like it was always
  this way.
- Gates, each its own **foreground** command from the repo root, reading each exit code:
  `uv run pytest` (~2.5 min, generous timeout), `uv run ruff check .`, `uv run ruff format
  --check .`. Then, because step 4 touches the frontend: `npm --prefix frontend run lint`,
  `npx tsc -b --noEmit` (from `frontend/`; there is no `typecheck` script), `npm --prefix
  frontend test`. `ruff check` passing is not `ruff format --check` passing.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record non-obvious decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the working tree, then report back to the orchestrating session:
   the file list, a one-line commit message, and the final test counts. The orchestrating session
   surfaces the `y/n` to the user. Never `git add -A`, never push.
