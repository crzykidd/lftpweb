---
name: 2026-08-12-settle-gate-followups
status: done
created: 2026-08-12
model: sonnet
completed: 2026-08-12
result: >
  All three follow-ups shipped. (1) A stuck settling item now self-heals: core/engine.py._persist
  recognizes an item its own settle gate just released straight to DOWNLOADED and triggers
  post-processing for it -- a second, narrow entry point alongside core/queue.py._reap_one's
  job-success trigger, proven end to end against the real fake seedbox with auto-queue off.
  (2) Settling now requires both REQUIRED_SETTLE_SCANS (2) matches and SETTLE_MIN_AGE_S (60.0,
  a named constant) of wall-clock time since the fingerprint was first observed -- persisted by
  repurposing item_settle.updated_at, no migration. (3) SettleSettings.enabled now defaults
  True (third reasoned exception to "ships off"), and Settings -> Transfer gained a "Settle
  gate" section (toggle + read-only scan-count/time-floor readout). 584 tests pass, both lint
  gates clean, npm run lint/build clean. DESIGN.md wording drafted (not applied) for a §6
  correction -- see docs/decisions.md's 2026-08-12 entry for full reasoning, the test-suite
  blast radius from flipping the default and how it was resolved, and rejected alternatives.
  Not committed -- left for the orchestrating session per this prompt's own instruction.
---

# Task: Finish the settle gate — unstick held items, add a time floor, turn it on

Three follow-ups to the settle gate shipped in `9b11df6`, all decided by the user on
2026-08-12 after reading how it works.

## Before you start

- Read `backend/lftpweb/core/settle.py` in full, plus its `docs/decisions.md` entry.
- Read the settle-gate call sites: `core/engine.py._persist`, `core/queue.py._reap_one`,
  `core/autoqueue.py`.
- Read `prompts/done/2026-08-12-settle-gate.md` — the task that built it, including the
  "found but not fixed" section that item 1 below comes from.
- Read `prompts/open-issues.md` § "2 — the settle gate".

## Working tree check

`git status --porcelain`. Recent agents landed deletion/retention (migration 008) and the
per-queue scan interval (migration 009) may also be in flight. If files you need are
dirty, list them and ask.

## 1. An item held at `settling` can sit forever

Found and recorded by the agent that built the gate. If a job finishes while its item is
still unsettled, the item is held at `REMOTE_ONLY`/`substate='settling'` and only reaches
`DOWNLOADED` again by being **re-queued** — via auto-queue or a manual click. With
auto-queue off and nobody clicking, it sits indefinitely with the bytes already on disk.

**The user has asked for this to be fixed.**

The original agent considered a scan-driven re-trigger and rejected it as out of scope
because it cuts against `core/postprocess.py`'s stated "only job success triggers
post-processing" design. That reasoning was about scope, not correctness — but the design
tension is real, so:

- Work out the cleanest way for an item whose bytes are complete and whose fingerprint has
  **now** settled to reach `DOWNLOADED` without a redundant re-transfer. The scan pass
  already recomputes state and already knows the settle status; that is the natural place.
- If that means post-processing can be triggered by something other than job success,
  **say so explicitly** and draft the `DESIGN.md` wording rather than quietly widening the
  contract. Do not edit `DESIGN.md`; there are already six proposed wordings awaiting the
  user's approval.
- Guard against the obvious failure mode: this must not re-trigger post-processing for an
  item that has already been through it. The existing state-precedence rules
  (`core/postprocess.py.outcome_survives_rescan`) and
  `PostprocessPipeline.in_flight_item_ids()` are the tools; a crashed worker must not be
  able to wedge an item, and a completed one must not be reprocessed on every scan.
- Test the actual stuck scenario end to end: job completes while unsettled, remote goes
  quiet, and the item reaches `DOWNLOADED` on its own with auto-queue **off**.

## 2. The settle window must have a time floor, not just a scan count

Currently the gate requires N unchanged scans (`REQUIRED_SETTLE_SCANS = 2`). With the
global 30s interval that is ~60s of quiet. **But the per-queue scan interval is landing**
(migration 009), and a queue set to 10s silently reduces the settle window to ~20s — the
gate gets weaker exactly where the user asked for faster polling. The user identified this
directly: they think in terms of *time between checks*, not scan count.

Require **both**:

- N consecutive unchanged fingerprints (keep the existing counter), **and**
- a minimum wall-clock age since the fingerprint was first observed unchanged.

Pick the floor and justify it. Something in the 60–90s range matches today's effective
behaviour, so enabling this changes nothing for a queue left at 30s while making a 10s
queue safe. Make it a named constant with a comment explaining why both conditions exist —
the next person will otherwise "simplify" one of them away.

The user's real-world case for this: their seedbox normally hardlinks (atomic, settles
immediately), but files also arrive by plain copy, and seedboxes are slow because of
shared disk.

## 3. Turn the gate ON by default, and give it a UI

It shipped **off** under this project's "new capabilities default off" rule. The user has
since described the settle behaviour as how the system *should* work and confirmed that
non-atomic copies are a real path on their setup. **Change the default to on.**

- `SettleSettings.enabled` defaults `True`.
- **The changelog must say plainly** that this changes behaviour for existing installs:
  transfers will start up to one settle window later than before, and this is deliberate.
  This is the third reasoned exception to the defaults-off rule (after `move`-mode
  verification and the phase 7 scheduled backup) — record it in `docs/decisions.md`
  alongside them, with the reasoning, so the rule keeps its meaning.
- **Build the Settings UI.** `GET/PUT /api/settings/settle` exists with no page behind it.
  Settings → Transfer is the natural home (it was itself a "backend with no UI" gap until
  earlier today — do not add a second one). Surface the enable toggle, the required scan
  count, and the time floor, with a short explanation of what the gate does: an item is
  not treated as complete until the remote side has stopped changing.

## Conventions to honor

- `docs/decisions.md`, newest at top.
- `CHANGELOG.md` — item 3 belongs under `### Changed`, not `### Added`; it changes existing
  behaviour.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`.
- `uv run pytest` with the fake seedbox up. Existing settle tests assert the old default
  and the scan-count-only rule — **update them to match the new behaviour rather than
  weakening what they check.**
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. Record decisions in `docs/decisions.md`.
4. **Do not commit.** Report back: file list, proposed one-line message, test count, lint
   results, the time floor you chose and why, how you solved the stuck-item problem and
   whether it required widening the post-processing trigger contract, any `DESIGN.md`
   wording you drafted, and anything not fixed. Never `git add -A`, never push.
