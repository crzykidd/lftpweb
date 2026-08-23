---
name: 2026-08-23-preflight-phase-allowlist
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  core/clientsync.py's Preflight phase filter replaced with a named allowlist
  (_PREFLIGHT_PHASES: QUEUED, DOWNLOADING, PAUSED, VERIFYING, EXTRACTING), fixing #12
  (SEEDING no longer floods Preflight) and #4 (PAUSED now appears) in one change. Added a
  guard test asserting the allowlist plus its four named exclusions cover the full
  TransferPhase enum, plus direct-unit and rTorrent-fixture end-to-end coverage. All three
  gates green: pytest 1966 passed, ruff check clean, ruff format clean.
---

# Task: Preflight's client phase filter becomes an allowlist

Fix findings **#12** and **#4** from `prompts/test-findings-2026-08-23.md` — they are the same
filter wrong in both directions at once.

## The bug

`core/clientsync.py`'s Preflight projection excludes by denylist:

```python
if transfer.phase in (TransferPhase.COMPLETED, TransferPhase.FAILED):
    continue
```

`TransferPhase.SEEDING` is not on it. An rTorrent seeding torrent maps to `SEEDING` (not
`COMPLETED`) and rTorrent reports it as *active*, so `active_only=True` returns it too. **Every
seeding torrent becomes a Preflight row** — observed live, 2026-08-23.

That filter's own comment states the assumption that broke it: *"every connector's
`active_only=True` contract already excludes terminal transfers."* True for **SAB**, where finished
work leaves the queue for history. **False for rTorrent**, where finished work stays in the list and
seeds indefinitely.

Simultaneously, finding **#4** reports a **paused, 60%-complete torrent appearing nowhere** in
Preflight — the same filter failing in the opposite direction.

## What to do

**Replace the denylist with an allowlist.** Over a closed nine-value enum, enumerate what Preflight
*means* so a phase nobody considered is excluded by default rather than admitted by default. Adding
`SEEDING` to the denylist is the same mistake one phase later and is not the fix.

Preflight's definition, from `core/preflight.py`'s own docstring: *"something lftpweb already knows
about but has no work to do on yet"* — i.e. **work that is coming**.

Proposed allowlist. **These are recommendations from the findings file, not settled decisions —
weigh each and record your reasoning:**

| Phase | Proposed | Why |
|---|---|---|
| `QUEUED`, `DOWNLOADING` | include | work plainly coming |
| `VERIFYING`, `EXTRACTING` | include | post-download steps before it lands |
| `PAUSED` | include | known-but-not-arriving; **this is finding #4's fix** — a paused incomplete item is the single most useful thing Preflight can show, because nothing else in lftpweb would tell you it is stuck |
| `SEEDING` | **exclude** | nothing is coming. This is the estate, and it belongs to Disk review's second pile (spec §11.1d) — a routing error, not missing functionality |
| `COMPLETED` | **exclude here** | handled by retirement-on-handover, not by this filter. Do not change handover behaviour in this task |
| `FAILED` | exclude | nothing coming; stage 3's withhold is that surface |
| `UNKNOWN` | **exclude** | §4.2 says unknown never blocks; it should not *populate* either. A row asserting nothing helps nobody |

**Write the allowlist as a named module-level constant with a comment explaining the
denylist-to-allowlist reasoning**, so the next person adding a `TransferPhase` member sees
immediately that they must decide rather than inherit.

**Also fix the stale comment.** The "`active_only=True` already excludes terminal transfers" claim
is false for torrent clients and actively misleading — it is what made the bug look impossible.
Replace it with the real reason the filter exists.

## Scope

`core/clientsync.py` and its tests. **Do not** change the merge/precedence logic (§9.2), the
retirement-on-handover behaviour, the category attribution, or anything in the frontend. Those are
separate findings with their own fixes queued.

## Tests

- A `SEEDING` transfer produces **no** Preflight row. (Finding #12, asserted directly.)
- A `PAUSED`, partially-complete transfer **does** produce one. (Finding #4, asserted directly.)
- `QUEUED`/`DOWNLOADING`/`VERIFYING`/`EXTRACTING` all produce rows.
- `FAILED`/`COMPLETED`/`UNKNOWN` produce none.
- **A test that fails if a new `TransferPhase` member is added without a decision** — e.g. assert
  the allowlist plus the explicit exclusions covers every enum member exactly. This is the guard
  that stops finding #12 recurring, and it is the most valuable test in this task.
- An rTorrent-shaped fixture (seeding torrents returned by `active_only=True`) produces only the
  incoming rows — the live scenario, end to end through `tests/fake_rtorrent.py`.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — every gate Bash call MUST pass an explicit timeout of at least
600000 ms. Six agents on this feature have stalled on exactly this. **Run backend gates from the
REPO ROOT**; if you `cd`, `cd` back.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record the decision in `docs/decisions.md`, and
mark findings #12 and #4 as fixed in `prompts/test-findings-2026-08-23.md` (leave the findings
text; append the resolution).
**Do not commit or push.** Report: files, exit codes, test count, a proposed one-line `fix:`
message, your `PAUSED` and `UNKNOWN` decisions with reasoning, and anything else you found.
