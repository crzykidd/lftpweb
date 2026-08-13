---
name: 2026-08-13-vanished-rows-should-leave-the-tree
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Split "write" from "publish" in core/engine.py._persist's vanished-from-both-trees sweep:
  the sweep still writes a fresh state every pass (56ec523's fix, unchanged) but only re-enters
  `written` (and therefore gets published) while holding a non-terminal, content-asserting
  state during the grace period. A row that resolves to REMOVED_LOCAL/REMOVED_BOTH with nothing
  in either tree now leaves the published tree exactly once (queue_delta's `removed`, absent
  from a fresh snapshot()) while the History page (reads `item` directly) is unaffected. Also
  closed the documented REMOVED_BOTH gap (prompts/open-issues.md): the vanished-sweep's call to
  resolve_absence now remaps a resolved REMOVED_LOCAL to REMOVED_BOTH, left unsuppressed (same
  choice resolve_vanished already made), since it already knows the remote is gone too -- this
  closes the re_download_externally_removed doomed-job issue. 751 tests passing (+3 new,
  1 renamed/updated), both ruff gates clean. Not committed -- prepared for the orchestrating
  session to commit as `fix: stop publishing vanished rows once they reach a terminal removed
  state`.
---

# Task: A row that has left both trees for good must stop being published

Regression found by the user on 2026-08-13, testing `35ac8e8` on a real `move` queue:

> in move mode it deleted the upstream, shows local only. but then when I delete local via CLI
> the files list shows them in the tree still as Extracted for the directory and removed_local
> on the mkv — shouldn't these go away

They should.

## Mechanism — verified, build on it

`core/engine.py:938`, inside the vanished-from-both-trees sweep added by `56ec523`:

```python
written.add(rel_path)
```

`written` is precisely the set handed to `_project(q.id, written)` as *"what this pass is
entitled to publish"*. So every row the sweep resolves is now also **published**, and since
nothing ever deletes an `item` row, it is published forever.

Before `56ec523`, a vanished path simply was not in `written` — filtered out of the projection,
reported by `diff_nodes` as `removed`, gone from the tree. That behaviour was correct and is
described in `_project`'s own docstring, which also states the design intent plainly:
`REMOVED_BOTH` is kept **"as history"**. Terminal removed rows are History-page content, not
Files-tree content.

**The sweep conflated two different needs**: *resolving* a vanished row's state so it does not
freeze (correct, keep it) and *publishing* it (wrong once it is finished). Separate them.

## The rule to implement

- **While the grace period is still running** — the row is holding a content-asserting state
  (`DOWNLOADED`, `VERIFIED`, `EXTRACTED`, `CORRUPT`, `EXTRACT_FAILED`) pending §7.3's decision —
  **keep publishing it.** The content may come back (an importer mid-move, a flapping mount),
  and showing it is honest.
- **Once it reaches a terminal removed state** (`REMOVED_LOCAL`/`REMOVED_BOTH`) **and is in
  neither tree, stop publishing it.** It is history. `diff_nodes` should report it `removed`
  and the Files tree should lose it.

Derive that from what the sweep actually decided on this pass, not by re-deriving state
elsewhere. Note the asymmetry: a `REMOVED_LOCAL` row whose **remote still exists** is in the
remote tree, so it is in `written` by the ordinary path and keeps being published — correctly.
That is the manual-delete case the user relies on to see "deleted locally, still on the
seedbox, Re-Download available." **Do not break it.** Only rows in *neither* tree are affected.

## Watch the interaction with the `REMOVED_BOTH` gap

`core/mount_sentinel.py.resolve_absence` always writes the literal `"REMOVED_LOCAL"`, taking
neither `sync_mode` nor `remote_deleted_at` as input — so the `move`-mode item in the user's
report lands at `REMOVED_LOCAL` when `REMOVED_BOTH` is what both `DESIGN.md` and
`core/autoqueue.py`'s comments describe. This is a known, documented gap (see
`prompts/open-issues.md`).

**Fixing it is in scope for this task if it is clean**, because "which terminal removed state"
and "should it still be published" are the same question asked twice, and the user is hitting
both at once. If you take it: `REMOVED_BOTH` must not become auto-queue eligible, and you must
decide whether `auto_queue_suppressed` should be set (it currently is not, for absences nobody
asked for). If you judge it too entangled, leave it, say so, and make sure the publishing rule
handles `REMOVED_LOCAL`-in-neither-tree correctly regardless — the user's symptom must be fixed
either way.

## Before you start

- `core/engine.py._persist` (the vanished sweep, ~lines 898–975) and `_project` (~line 977).
- `core/mount_sentinel.py` — `resolve_absence`, `resolve_vanished`, `_COMPLETE_PREV_STATES`.
- `core/engine.py.diff_nodes` — how `removed` is computed.
- `prompts/done/2026-08-13-delete-state-truthfulness.md` — the task that introduced the sweep,
  and its reasoning for why the rows must be *written*.
- `prompts/open-issues.md`.

## Working tree check

`git status --porcelain`. Another task may be in flight in `frontend/src/components/FileTree.tsx`
(resizable columns) — that is not yours and should not overlap, since this is backend. If
anything you need is dirty, list it and ask.

## Tests

- **The user's exact scenario**, end to end against the fake seedbox: a `move` queue completes,
  the remote is deleted, then the local files are removed **outside lftpweb** (simulating their
  CLI `rm`). After the grace period the rows must leave the published tree — asserted on the
  `queue_delta`'s `removed` list and on a fresh `snapshot()`, not just on the database.
- **During** the grace period the row is still published, holding its outcome state.
- **The manual-delete case still works**: delete locally while the remote survives → the row
  stays visible, `REMOVED_LOCAL`, R lit, action reads "Re-Download". This is the regression
  risk; guard it explicitly.
- A row that leaves both trees and later **comes back** (remote re-uploaded) is published again
  and reads correctly — the `reconsider_removed_state` path from `7dc045f`.
- `diff_nodes` payload stays proportional to what changed; do not reintroduce full-tree
  publishes (`tests/test_ws_deltas.py` guards this — it has already caught two regressions this
  session).

## Conventions to honor

- `docs/decisions.md`, newest at top — this reverses part of a same-day change, so record why
  the sweep still writes but no longer publishes.
- `CHANGELOG.md` under `### Fixed`; describe the net behaviour, not the detour.
- `DESIGN.md` §3.2 rule 3/rule 6 and §7.3 — standing approval to edit directly.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `uv run pytest` with the fake seedbox up (currently running — leave it up).
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `fix:` message, whether you also
   fixed the `REMOVED_BOTH` gap and what you decided about suppression, test count, lint
   results, and anything not fixed. Never `git add -A`, never push.
