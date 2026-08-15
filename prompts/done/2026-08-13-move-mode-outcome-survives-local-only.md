---
name: 2026-08-13-move-mode-outcome-survives-local-only
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: |
  Fixed both halves. `outcome_survives_rescan` (core/postprocess.py) now also wins over a
  structural LOCAL_ONLY when `item.remote_deleted_at` is set, gated on that column so a
  genuinely untracked local file is unaffected. `core/engine.py._persist` also gained a second
  pass, found while testing the first fix: a rel_path that leaves *both* trees at once (a
  move-mode item auto_move relocates after its remote copy is already gone) got no node from
  `core/reconcile.py` at all and was previously never revisited -- frozen on its outcome
  forever. It now resolves every previously tracked rel_path missing from the scan's written
  set through `core/mount_sentinel.py.resolve_absence`'s existing grace-period machinery, so
  it still reaches REMOVED_LOCAL. Found and reported, not fixed: DESIGN.md/autoqueue.py say a
  move-mode item should settle on REMOVED_BOTH once both copies are gone, but
  `resolve_absence` always writes bare REMOVED_LOCAL regardless of mode -- see
  docs/decisions.md's 2026-08-13 entry. 20 new tests added (643 total, up from 623), both
  lint gates clean. DESIGN.md wording drafted, not applied -- see the session report.
---

# Task: A `move`-mode item's post-processing outcome must survive the rescan that finds it `LOCAL_ONLY`

Found by the user on 2026-08-13, the first time `move` mode was run end to end against a real
release. It downloaded, verified, deleted the remote, unrarred — and then every item read
**`LOCAL_ONLY`**, losing the `VERIFIED`/`EXTRACTED` record within one scan interval.

## Mechanism — already diagnosed

`core/postprocess.py._maybe_delete_remote` sets `item.remote_deleted_at` and, by phase 5's
explicit design decision, **deliberately does not change `item.state`** — the row is meant to
keep whatever verify/extract last set. See `prompts/startnewsession.md`'s traps list: *"A
`move`-mode delete sets `item.remote_deleted_at` but never changes `item.state`."*

But the next scan finds local present / remote absent, `core/reconcile.py` computes
`LOCAL_ONLY`, and `core/postprocess.py.outcome_survives_rescan` is:

```python
return prev_state in TERMINAL_STATES and structural_state == "DOWNLOADED"
```

`LOCAL_ONLY` is not covered, so the outcome is overwritten ~30s after it was earned. Phase 5's
intent is defeated by a rule written before `move` had ever been run.

**This is the same bug class as "post-processing states erased for four phases"**, fixed on
2026-08-12 — that fix covered the `DOWNLOADED` case only, because nobody had exercised `move`.

**The signal already exists.** `remote_deleted_at IS NOT NULL` together with a structural
`LOCAL_ONLY` means "the bytes are all here and the remote is gone because *we* deleted it" —
a refinement of the outcome in exactly the sense `DOWNLOADED` is.

## Before you start

- `core/postprocess.py` — `outcome_survives_rescan` and its docstring, which reasons carefully
  about *why* only `DOWNLOADED` qualifies. Extend that reasoning; do not discard it.
- `core/reconcile.py` — where `LOCAL_ONLY` is produced (~line 186).
- `core/engine.py._persist` — the only caller of `outcome_survives_rescan`.
- `core/mount_sentinel.py.resolve_absence` — the *other* half of the precedence rule, for
  absence. Understand the split before touching either.
- `prompts/open-issues.md` and `prompts/startnewsession.md`'s traps list.

## Working tree check

`git status --porcelain`. Other tasks are in flight around `core/postprocess.py`,
`core/local_delete.py`, and `core/extract.py`. If files you need are dirty, list them and ask.

## What to do

1. **Extend `outcome_survives_rescan` to cover `LOCAL_ONLY` when `remote_deleted_at` is set.**
   It currently takes `(prev_state, structural_state)`; it will need the deletion marker too.
   Keep it a pure function — it is unit-tested as one and that is worth preserving.

2. **Gate it on `remote_deleted_at`, not on `LOCAL_ONLY` alone.** A genuinely unmanaged local
   file, or one whose remote vanished for some other reason, should still read `LOCAL_ONLY`.
   Only "we deleted the remote ourselves after verifying" earns the outcome's survival.

3. **Do not weaken the existing rules.** They are load-bearing and were reasoned out
   deliberately:
   - `PARTIAL` still beats an outcome (rule 2 — the bytes are not all there).
   - Absence still routes to `resolve_absence` and §7.3's grace period, so a `move`-mode item
     later relocated by `_do_move`, or moved out by an importer, still reaches `REMOVED_LOCAL`
     normally. **Check this specifically**: with `auto_move` on, the item leaves `local_path`
     after extraction, and your change must not freeze it on its outcome forever.
   - Transient states are protected by `in_flight_item_ids()`, never by the state string, so a
     crashed worker cannot wedge an item. Do not add a state-string protection here.

4. **Consider whether `REMOVED_BOTH` needs the same treatment** — a `move` item that is both
   relocated and remote-deleted ends with neither copy at `local_path`. Work out what state it
   should settle on and whether the current rules get there. Report what you find even if you
   change nothing.

## Tests

- Move-mode item: verify → remote delete → extract → scan. Asserts the row still reads
  `EXTRACTED`, not `LOCAL_ONLY`. **This is the reproduction; do not ship without it.**
- The same for `VERIFIED` with extraction disabled.
- A genuine `LOCAL_ONLY` item (no `remote_deleted_at`) is unaffected.
- `PARTIAL` still beats the outcome even with `remote_deleted_at` set.
- With `auto_move` on, the item still reaches `REMOVED_LOCAL` through the grace period after
  relocation — it is **not** frozen on its outcome.
- Exercise it end to end against the fake seedbox, not only as a unit test on the pure
  function. This bug existed precisely because the unit-level rule looked right.

## Conventions to honor

- `docs/decisions.md`, newest at top.
- `CHANGELOG.md` under `### Fixed`.
- `DESIGN.md` §3.2 rule 9 and §7.3 describe this precedence and were rewritten on 2026-08-12.
  Draft updated wording and record it; the user has been approving these promptly, but do not
  apply without a nod in this task's report loop.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `uv run pytest` with the fake seedbox up.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `fix:` message, test count,
   lint results, what you found about `REMOVED_BOTH` and about `auto_move`, any `DESIGN.md`
   wording drafted, and anything not fixed. Never `git add -A`, never push.
