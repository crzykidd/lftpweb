---
name: 2026-08-23-withhold-and-cadence
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: |
  Cadence split corrected to cheap-vs-expensive, read per-connector off
  `Operation.LIST_HISTORY`'s own NATIVE/DERIVED capability declaration (no client_type branch).
  A terminal SABnzbd verdict now reaches `completed_transfers()`/`failed_transfers()` within one
  FAST_INTERVAL_S tick instead of being stranded behind SLOW_INTERVAL_S; rTorrent's already-
  expensive full listing is never called twice per tick. Withhold gate added to
  `core/autoqueue.py.on_scan` as a third gate (mount/settle/withhold), self-lifting by always
  checking `find_client_completion` before `find_client_failure`, ships off
  (`WithholdSettings.enabled`, default False) pending live confirmation of spec §13.4 guess #2.
  All gates green: pytest, ruff check, ruff format --check. docs/decisions.md and spec §9.1/§14
  updated. No frontend/API surface added for `AutoQueue.withheld` -- named as an open gap.
---

# Task: Stage 3 — withhold on an explicit failure verdict, and fix the poll cadence split

Two related pieces: correct which cadence carries terminal verdicts (found wrong during stage 2b),
then use an **explicit** failure verdict to withhold a transfer that would move known-bad bytes.

## Part 1 — the cadence split is wrong (fix this first, it is small)

`docs/download-client-framework-spec.md` §9.1 splits polling into a fast pass
(`list_transfers(active_only=True)`) and a slow full-estate pass. Stage 2b discovered the fast pass
**structurally cannot carry a terminal verdict**: a finished SABnzbd item leaves the queue and
appears only in history, so `active_only=True` never sees a `COMPLETED`. The settle-gate skip
therefore reads a cache refreshed on the slow cadence — largely defeating §4.3's whole point, which
was replacing a ~60 s wait with a direct observation.

**The split was drawn along the wrong axis.** It should separate *cheap* from *expensive*, not
*active* from *everything*:

- **Fast (~10 s): whatever is cheap and carries verdicts.** For SABnzbd that is queue **and**
  history — two calls, both trivial, and history is where every terminal verdict lives.
- **Slow (minutes): what is genuinely expensive.** The full torrent estate — hundreds of rows, and
  `list_trackers` is an N-call fetch on rTorrent (spec §2.1).

Make it a **per-connector property** rather than a rule the poller hard-codes: a connector knows
what is cheap for it, and the poller should not branch on client type (spec §4.4/§5.1's rule
applies here too — no `if client_type == …` in the scheduler). Update §9.1 to match, marking it as
a correction with its cause, the way §11.1c and §8.2 record theirs.

## Part 2 — withhold on explicit failure

`docs/download-client-framework-spec.md` §4.3:

> **A client failing outright needs no code** — nothing lands, so there is nothing to auto-queue,
> and the block is automatic. **The case that genuinely needs an explicit withhold is a *partial*
> failure**: the download dies partway, or unpack fails, leaving a half-written directory on the
> seedbox. The settle gate sees those bytes stop growing — they have, permanently — and transfers
> garbage.

So: an **explicit terminal failure verdict** for a release blocks auto-queueing it, and says why.

### The rules this must not break

- **§4.2 outranks everything here.** Only an *explicit* failure blocks. Silence, `UNKNOWN`, an
  unreachable client, a release the client never heard of — none of those block anything, ever. A
  blank SABnzbd queue response (the v0.2.4 production incident) must never read as failure.
- **Ship it OFF by default**, same reasoning as stage 2b: `Failed`→`FAILED` is §13.4 guess #2,
  doc-derived and unverified. Consider carefully which default is safer here and say so in your
  report — withholding wrongly means a good release **silently never arrives**, which is arguably
  worse than transferring garbage, because nothing surfaces it. That asymmetry is the argument for
  off-by-default *and* for making an active withhold highly visible.
- **A withhold must be liftable and must lift itself.** If the client later reports success for the
  same release — a re-grab, a repair, a manual retry — the withhold clears on the next pass. A
  permanent block from a transient verdict is the failure mode to design against.
- **A withhold must never be silent.** One audit event when it engages, one when it lifts, and the
  reason visible in the UI. A user seeing nothing arrive and having no way to learn why is exactly
  what §4.3 calls "the real deliverable."
- **It must not write `item.state`** (spec §4.1). Withholding is a decision about whether to
  *queue*, taken where auto-queue decisions already live — not a state the item carries.

### Where it goes

`core/autoqueue.py` already gates queueing on the settle gate and the mount sentinel; this is a
third gate of the same kind. Follow the existing shape rather than inventing a parallel mechanism —
and note `auto_queue_suppressed` already exists for a *different* purpose (§4.6, the stop path).
**Do not reuse it**; a withhold is not a stop, and conflating them will strand items exactly the
way v0.2.6's `REMOTE_GONE` defect did.

## Tests

- Explicit terminal `FAILED` + exact path match + setting on → withheld, event written.
- Setting off → no withhold, byte-identical to today. Name the test that proves it.
- `UNKNOWN`, blank queue, unreachable client, unknown release → **never** withheld.
- A withhold **lifts** when the client later reports the same release completed.
- A near-miss path does not match (component-boundary rule, same as stage 2b).
- The withhold does not touch `item.state` or `auto_queue_suppressed`.
- Cadence: a connector's cheap calls run on the fast tick and its expensive ones do not; the
  scheduler contains no client-type branch.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — every gate Bash call MUST pass an explicit timeout of at least
600000 ms. Five agents on this feature have stalled on exactly this. **Run backend gates from the
REPO ROOT**; if you `cd`, `cd` back.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. Frontend if touched: `npm run build`, `npm run lint`, `npm test`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md`, update
the spec (§9.1's correction, §14's staging table).
**Do not commit or push.** Report: files, every exit code, test counts, a proposed one-line `feat:`
message, **which default you chose for the withhold and the argument for it**, and anything in the
spec found wrong or underspecified.
