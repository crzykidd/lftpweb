---
name: 2026-08-24-client-shortened-settle
status: completed        # pending | completed | failed
created: 2026-08-24
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-29
result: Shipped the client-shortened settle (default on) — AutoQueue's own ~5s background ticker re-fingerprints a finished item's remote subtree twice, 10s apart, and queues it only on a match; widened SEEDING into the finished-transfer phase set alongside COMPLETED (the real defect blocking rTorrent); left the old client_skip_enabled time-hold untouched as a named, unconsolidated overlap.
---

# Task: client-shortened settle — verify with a 10s re-fingerprint instead of waiting 60s

When a download client reports a release finished, lftpweb still waits out the full settle gate
(`REQUIRED_SETTLE_SCANS` × `SETTLE_MIN_AGE_S`, ≥60s) before queuing. This task shortens that to
roughly ten seconds **by actually verifying on the filesystem** — fingerprint the item's remote
subtree, wait ~10s, fingerprint again, and queue if nothing moved.

The user's own framing: *"Once a client marks complete it should be 100% done with all work. So
we should be good, but putting a short check allows us to verify."*

## Before you start

Read, in this order:

1. `core/settle.py`'s **module docstring** — the fingerprint `(file_count, total_bytes,
   max_mtime)`, why all three parts are load-bearing, and why `REQUIRED_SETTLE_SCANS` and
   `SETTLE_MIN_AGE_S` are *both* independently required. You are reusing this fingerprint, not
   inventing a second notion of "quiet."
2. `core/settle.py` lines ~516-620 — `find_client_completion`, `CLIENT_COMPLETION_HOLD_S`,
   `client_completion_ready`. This is the existing mechanism you are superseding.
3. `core/clientsync.py.completed_transfers()` and `core/autoqueue.py.on_scan` — the candidate
   source and the one caller.
4. `core/clients/rtorrent.py._classify_token` and `_RTORRENT_STATUS_MAP`.
5. `DESIGN.md` §3.3 (the settle gate) and §17.
6. `docs/download-client-framework-spec.md` §13.6 and §14.
7. `CLAUDE.md` — commit rules; gates in the **foreground**, from the repo root.

### The defect this fixes

`rtorrent.py._classify_token` maps a finished torrent that is actively seeding — the normal case
— to `SEEDING`, not `COMPLETED`:

```
if complete:
    return "seeding" if is_active else "completed"
```

`clientsync.completed_transfers()` filters strictly to `TransferPhase.COMPLETED`. So the existing
settle-gate skip is **structurally unreachable for an ordinary seeding rTorrent torrent**; it can
only fire for one that finished and stopped. `tests/test_clients_rtorrent.py:457-458` asserts
exactly this behaviour today.

### 🔴 The wrong fix — do not make it

**Do not change `_classify_token` or `_RTORRENT_STATUS_MAP`.** `SEEDING` is the *correct* phase
for a complete, actively-seeding torrent, and it is read that way by the Preflight box, the disk
review scan and everything else. Making `complete + is_active` map to `COMPLETED` would make the
client lie about its own state everywhere in the product to fix one gate.

The fix belongs in **what the gate accepts**, not in what the connector reports. Leave
`tests/test_clients_rtorrent.py:457-458` asserting what it asserts now — that mapping is right.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this plan
needs to modify. If any have uncommitted changes, list them and ask the user before touching
them. Surface unrelated dirty files once as awareness; don't block. This file is exempt.

## What to do

Files likely in scope: `backend/lftpweb/core/settle.py`, `backend/lftpweb/core/clientsync.py`,
`backend/lftpweb/core/autoqueue.py`, `backend/lftpweb/models.py`, `tests/test_settle.py`,
`tests/test_clientsync.py`, `tests/test_autoqueue.py`, `tests/fake_rtorrent.py`, plus
`DESIGN.md` §3.3, `docs/download-client-framework-spec.md` and `CHANGELOG.md`.

### 1. "Finished" means the download is done, not `phase == COMPLETED`

Widen the candidate filter to accept **`TransferPhase.SEEDING` as well as
`TransferPhase.COMPLETED`**. Rename `completed_transfers()` to something honest
(`finished_transfers()` or similar) and update `find_client_completion` — which re-checks the
phase itself as belt-and-suspenders — to match.

Widen to **those two phases only.** `VERIFYING` must stay excluded: rTorrent's `hashing`
overrides every other flag, so a torrent re-checking its data correctly reports `VERIFYING` and
must not satisfy this gate. `DOWNLOADING`, `QUEUED`, `PAUSED`, `FAILED` and `UNKNOWN` are all
unchanged and all still mean "no."

This changes nothing for SABnzbd — usenet has no seeding, so `SEEDING` never appears from it.

The existing rule this widens is documented as "never let a queue-side status satisfy the gate."
That rule survives: `SEEDING` is not queue-side, it is *finished and uploading*. Record the
reasoning in `docs/decisions.md` rather than silently relaxing a stated rule — and keep the
existing docstrings' convention of naming what changed and why, not rewriting them to look like
they always said this.

**No connector-specific branch anywhere.** `if client_type == "rtorrent"` must not appear; §17
rule 6 has held across this whole subsystem and holds here.

### 2. Replace the time-hold with a real re-fingerprint

`client_completion_ready` currently compares `now - transfer.completed_at` against
`CLIENT_COMPLETION_HOLD_S` and nothing else — its own docstring notes that a completion already
older than the hold "satisfies it immediately, with no added wait at all." In the common case
where the poller notices minutes later, **that means lftpweb verifies nothing** and starts
transferring purely on the strength of a status string that spec §13.4/§13.6 both flag as
doc-derived and unverified.

Replace that with the check the user asked for:

1. A client reports the item finished (step 1) and the ordinary settle gate has not already
   passed it.
2. Fingerprint that item's remote subtree — the **same** `(file_count, total_bytes, max_mtime)`
   `core/settle.py` already computes. Reuse the existing function; do not write a second one.
3. Wait ~`CLIENT_RECHECK_INTERVAL_S` (10s).
4. Fingerprint again. **Equal → settled**, queue it. **Different → fall back to the ordinary
   settle gate**, exactly as it runs today. Never shortcut on a changed fingerprint.

This is the point of the task: the shortened path is safe because it *observes stability*, not
because it trusts the client's vocabulary. Say that in the docstring.

**Do not sleep inside the scan pass.** A 10s sleep in `on_scan` would stall the scan loop for
every queue. Implement the wait as a short-cadence ticker (~5s) that advances pending rechecks:
no fingerprint yet → take one; fingerprint taken ≥10s ago → take the second and compare. It must
converge in ~10–15s and must never block a scan pass.

Pending-recheck state may be held **in memory**. A restart mid-recheck simply falls back to the
ordinary settle gate, which is the safe direction. Write that down as a deliberate choice, not an
oversight. (The "nothing may publish a state it did not read back from a table" invariant governs
published *item state*; this is internal gate bookkeeping and never published directly.)

Name the constants the way this module already names `REQUIRED_SETTLE_SCANS` and
`SETTLE_MIN_AGE_S` — "a decision, not an accident," with the reasoning inline.

### 3. Default on

This ships **on**, and the reasoning must be recorded because it is an exception to this
project's "every new capability ships off" rule — the fifth, after `move`-mode verification, the
scheduled backup, and `SettleSettings.enabled` itself (`docs/decisions.md` records those
together; add this one alongside them).

The reason it earns the exception, stated plainly: the existing `client_skip_enabled` ships off
because it *trusts* an unverified status mapping. This one **verifies on the filesystem**, so
that objection does not apply to it. A wrong or missing client verdict costs nothing — the
ordinary 60s gate runs, exactly as today.

`CHANGELOG.md` must state that existing installs will see transfers start **sooner** than
before, the same way `SettleSettings.enabled`'s own default flip stated it plainly rather than
leaving someone to notice.

### 4. Leave the old skip in place, and name the overlap

The user explicitly chose "default on" over "replace the existing skip entirely," so
**`client_skip_enabled` and its pure time-hold stay, unchanged and still off by default** — the
escape hatch for someone who wants no recheck at all.

Order of evaluation: the client-shortened settle runs first; if it settles the item, done. The
old skip remains available for anyone who has switched it on.

That leaves two overlapping mechanisms for one job. **Name that as a gap** — in
`docs/decisions.md` and in `README.md`'s "Known gaps" — rather than quietly consolidating them.
Consolidation is a follow-up someone should decide deliberately, not a side effect of this task.

### 5. Tests — fixture first, and this is not optional

This repo has been burned twice by a self-authored fixture encoding the same assumption as the
code it tests (`IMPORT_EVENT_TYPES = {3}`; SABnzbd's `mode=version` accepting an invalid API
key), with a green suite throughout both times.

**So: change `tests/fake_rtorrent.py` first to produce a complete + actively-seeding torrent,
write the test asserting the gate accepts it, and WATCH IT FAIL before you touch the gate.**
Report in your final summary that you saw it fail and what the failure said. A fixture edited
only to match new code proves nothing.

Cover at minimum:

- a complete + active (`SEEDING`) rTorrent torrent now satisfies the finished-transfer filter
- a `VERIFYING` (hashing) torrent does **not**, even when `complete` is set
- an unchanged fingerprint across the recheck queues the item in ~10s
- a **changed** fingerprint falls back to the ordinary settle gate and does not shortcut
- the recheck never blocks the scan pass (assert the scan pass returns without waiting out the
  interval)
- an absent, unparseable or missing client verdict falls back to the ordinary gate
- SABnzbd behaviour is unchanged by the phase widening
- the existing settle-gate tests all still pass unmodified

## Conventions to honor

- Match the surrounding docstring style — these modules explain *why* at length, including which
  earlier decision a line reverses and what caused the reversal. Preserve that; do not rewrite
  history to look like it was always right.
- Doc updates ship in the **same commit** as the code: `DESIGN.md` §3.3, the relevant spec
  sections, `CHANGELOG.md`, and `README.md`'s "Known gaps" for the overlap in step 4.
- `docs/decisions.md`, newest at top: the `SEEDING` widening, the default-on exception, the
  in-memory pending state, and the named overlap.
- Gates, each its own **foreground** command from the repo root, reading each exit code:
  `uv run pytest` (~3.5 min, generous timeout), `uv run ruff check`, `uv run ruff format
  --check`. `ruff check` passing is not `ruff format --check` passing.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record the decisions above in `docs/decisions.md`.
4. **Do not commit.** Prepare the working tree, then report back to the orchestrating session:
   the file list, a one-line `feat:`-prefixed commit message, the final test counts, and
   confirmation that you saw the fixture-first test fail before the fix. Never `git add -A`,
   never push.
