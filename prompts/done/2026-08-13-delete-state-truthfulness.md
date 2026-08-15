---
name: 2026-08-13-delete-state-truthfulness
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  All four defects fixed. Defect 1's root cause was the second named possibility (remote
  already gone on the move queue; REMOVED_BOTH was correct, just silent and blocking) --
  fixed with a transient substate='removing' marker published before the filesystem work,
  the work itself moved off the event loop, and a new DeleteInFlight in-memory tracker
  (crash-safe by construction, wired into Engine._protected_rel_paths). Defect 2's narrowing
  is core/local_delete.reconsider_removed_state, scoped to prev_state in
  {REMOVED_LOCAL, REMOVED_BOTH} only, never touching auto_queue_suppressed. Defect 3 fixed
  both ways the prompt allowed: core/queue.py._reap_one now flushes a final accurate child
  reading at reap time, and core/mount_sentinel.resolve_vanished is a new, deliberately
  narrow (PARTIAL/LOCAL_ONLY only) fallback in the vanished-from-both-trees sweep -- narrowed
  after an initial too-broad version broke an existing, correct test
  (tests/test_ws_deltas.py's REMOTE_ONLY-vanishes-cleanly assumption). Defect 4 was a
  one-line frontend fallback. 733 backend tests pass (up from 701), both ruff gates clean,
  npm run lint and npm run build clean. DESIGN.md, docs/decisions.md, and CHANGELOG.md
  updated.
---

# Task: A deleted item must stay truthful when the delete is slow, and when the release comes back

Two defects found by the user on 2026-08-13, testing the delete work that shipped hours
earlier in `b39158e`.

## Defect 1 — `REMOVED_BOTH` shown while only the local copy is being removed

> when I remove a large directory the status shows removed both, but it is only removing the
> local? so the status might just be removing?

Two possibilities and you must determine which before fixing anything:

- **The state is written too early**, before the filesystem work completes, so a large
  directory sits at its final state while deletion is still in flight. Read
  `core/local_delete.py` and establish the actual ordering of "unlink the tree" versus
  "`_mark_subtree_removed`".
- **The remote really was already gone** (a `move` queue, or `remote_size IS NULL`), in which
  case `REMOVED_BOTH` is correct and the only problem is the absence of progress feedback.

**Either way the user's instinct is right: a large delete has no feedback at all.** Add a
transient state so the row says something honest while the work happens.

- `item.substate` is the right vehicle — it already carries `'settling'` for the settle gate,
  so there is a precedent and no migration. A new `state` value would mean touching the
  `CHECK` constraint and §9.2's visible vocabulary; **prefer `substate`** unless you find a
  strong reason otherwise, and say why if you deviate.
- It must be **impossible to get stuck**. This project's rule (see
  `prompts/startnewsession.md`'s traps list): *"a state that is merely protected is a state
  that can never be un-stuck."* Transient states are protected by a **live worker's
  existence**, never by the state string — `PostprocessPipeline.in_flight_item_ids()` is the
  model. A crashed or killed process must not leave rows reading "Removing…" forever. If you
  cannot guarantee that, say so and propose an alternative rather than shipping a wedge.
- Clear it in the same transaction that writes the final state.

## Defect 2 — a re-copied release still reads `REMOVED_BOTH`

> if I copy the same folder I already deleted on seedbox again. Status is Removed Both. while
> this is kind of true it should maybe change from "Queue" to Re-Download. Something that shows
> that this has been recopied. but won't auto download.

`REMOVED_BOTH` asserts **both** copies are gone. Once the user re-uploads, the remote exists
again and the assertion is false. The row should fall back to **`REMOVED_LOCAL`** — locally
absent, remotely present — and the **R facet should light up**, since `remote_size` is non-null
again.

**Suppression must survive.** `auto_queue_suppressed`/`suppressed_reason = 'deleted_local'`
stays set: the user deleted this deliberately and a re-upload is not consent to re-fetch it.
The point is that the UI should *show* the release is available again, not that we should go
get it. Verify the state change does not clear the flag.

**The same root cause bites from the local side too, and the user hit it minutes later:**

> after a redownload (manual) it extracted but the extracted .mkv shows Removed Both. This
> should have been updated to Extracted and shown on the local disk. The top folder shows
> extracted.

When the folder was deleted, `b39158e` marked the **whole subtree** `REMOVED_BOTH` +
suppressed — including the path that extraction later recreated. The file now exists locally,
but the row is protected from recomputation, so it can never notice. The parent is correct
(`EXTRACTED`) because post-processing writes it directly; only the children are fossilised.

So the narrowing below must handle **content returning on either side** — remote reappearing
*and* local reappearing — not just the remote case. A suppressed row whose reality has
demonstrably changed should correct its state while staying ineligible.

Check where this transition belongs. `core/engine.py._persist` protects rows with
`auto_queue_suppressed` set from state recomputation (see `_protected_rel_paths`) —
deliberately, so a stopped item is not resurrected by a rescan. That protection is what is
keeping the stale `REMOVED_BOTH`. **Narrow it rather than removing it**: a suppressed row whose
remote has genuinely reappeared may correct `REMOVED_BOTH → REMOVED_LOCAL` without becoming
auto-queue eligible, because eligibility is the flag's job, not the state's. Do not widen this
into "suppressed rows get recomputed generally" — that reinstates the bug `_protected_rel_paths`
exists to prevent.

**And the action label.** `FileTree.tsx`'s `rowAction` returns `'queue'` for any state except
`QUEUED`/`DOWNLOADING`/`LOCAL_ONLY`. For a row we previously deleted whose remote is back,
label it **"Re-Download"** — the user asked for it by name, and "Queue" reads like a fresh item
rather than one coming back. Derive it from the suppression reason plus remote presence, not
from the state string alone.

## Defect 3 — a `PARTIAL` row that leaves both trees is stuck forever

Reported by the user on 2026-08-13, on a **`move`** queue:

> the last file downloaded was a Sample file and it ended at Partial but the file is there and
> there are no active transfers. a rescan doesn't seem to fix that.

**This is the most serious of the three** — the row is permanently wrong and no amount of
rescanning corrects it.

Diagnosis (verify before fixing; do not take it on trust):

`core/mount_sentinel.py._COMPLETE_PREV_STATES` is `{"DOWNLOADED"} | _POSTPROCESS_STATES`.
**`PARTIAL` is not in it**, and `resolve_absence` returns `None` — "trust the fresh structural
reading" — for any `prev_state` outside that set. The second pass added in `56ec523` for rows
that vanish from *both* trees reuses that same gate, so a row whose `prev_state` it has no
opinion about is left untouched. There is no fresh structural reading for such a row, because
it is in neither tree. Nothing ever revisits it.

How a child reaches `PARTIAL` and then vanishes, in one scan interval:

1. `core/queue.py._publish_child_progress` persists child rows every
   `CHILD_PROGRESS_THROTTLE_TICKS` (3), so the **last** write before a job ends is frequently a
   mid-transfer `PARTIAL`, not the final `DOWNLOADED`.
2. Post-processing runs immediately on job success — verify, remote delete (`move`), extract,
   relocate to `staging_path`.
3. The child is now absent from `local_path` (relocated) *and* from the remote (deleted).
4. The next scan finds it in neither tree → second pass → `prev_state == 'PARTIAL'` → no
   opinion → left alone. Permanently.

Both halves deserve attention, and you should decide how much to fix:

- **The stuck row.** A row that has left both trees and will never be revisited must reach
  *some* defined resting state. Work out what `PARTIAL` should resolve to here and why.
  Widening `_COMPLETE_PREV_STATES` to include `PARTIAL` is the obvious move and is probably
  **wrong** — that set means "asserted all its bytes were here", which `PARTIAL` explicitly
  does not. Prefer a separate rule for "vanished from both trees with no opinion available"
  over blurring the meaning of an existing set. Whatever you choose, the comment on that set
  is unusually good about *why* its membership is what it is — extend that reasoning, do not
  quietly widen it.
- **The stale child reading.** Consider whether the throttled child-progress writer should
  flush a final, accurate reading when its job reaps, rather than leaving whatever the last
  throttled tick happened to write. That would stop the fossil forming in the first place, and
  is arguably the real fix — the stuck-row handling is the safety net. `core/queue.py._reap_one`
  is where a job ends.

Do both if they are both sound. If you judge one out of scope, say which and why.

**Test it as the user hit it**: a `move` queue with `auto_move` on, a multi-file release
including a small file, driven end to end against the fake seedbox, asserting no row is left at
`PARTIAL` once the dust settles.

## Defect 4 — a completed directory shows no size on a `move` queue

> after extracted and complete the folders no longer shows size.

`frontend/src/components/FileTree.tsx:35`:

```js
entry.is_dir ? entry.remote_size : (entry.local_size ?? entry.remote_size)
```

Files fall back to `local_size`; **directories do not.** On a `move` queue the remote copy is
deleted after verification, so a completed directory's `remote_size` is NULL and the cell goes
blank — while files inside the same tree still show sizes, because they have the fallback.

Give directories the same fallback. Both are already rollups from `core/reconcile.py`, so no
new computation is needed. Check the tooltip and the **sort comparator** (`sortBy` size, around
line 229 — `node.local_size ?? node.remote_size ?? 0`, which already falls back) so the
displayed value and the sorted value cannot disagree; a column that sorts by one number and
shows another is worse than either.

## Before you start

- `core/local_delete.py` — `delete_local`, `_mark_subtree_removed`, `_removed_state_for`.
- `core/engine.py._persist` and `_protected_rel_paths`.
- `core/settle.py` for how `substate` is set, published, and cleared.
- `core/itemview.py` — the R facet's `deleted_by_us` vs `no_remote` reasons.
- `frontend/src/components/FileTree.tsx` — `rowAction`, and the settling badge as the
  transient-state visual precedent.
- `prompts/open-issues.md`, especially the two-ways-a-local-copy-disappears distinction.

## Working tree check

`git status --porcelain`. If files you need are dirty, list them and ask.

## Tests

- A large/multi-file delete shows the transient state, and clears it — asserted at the
  published-message level, not only in the database.
- **A killed worker cannot leave a row stuck in the transient state.** This is the important
  one.
- Delete an item, re-create the remote path, scan → the row reads `REMOVED_LOCAL`, R lights,
  and `auto_queue_suppressed` is **still 1**.
- That same row is **not** picked up by auto-queue, with `re_download_externally_removed` both
  off and on.
- A suppressed `STOPPED` item is still protected from recomputation — prove the narrowing did
  not widen.
- Manual re-queue of the re-appeared item works and clears suppression, as it does today.

## Conventions to honor

- `docs/decisions.md`, newest at top.
- `CHANGELOG.md` under `### Fixed`.
- `DESIGN.md` — the user has given standing approval to update it directly. §3.2 rule 3 and
  §9.2 are the relevant sections.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`.
- `uv run pytest` with the fake seedbox up (`docker-compose.test.yml`); 701 pass today. Tear it
  down afterward and confirm with `docker ps -a`.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `fix:` message, which of the two
   causes defect 1 turned out to be, how you guaranteed the transient state cannot wedge, how
   you narrowed the suppression protection, test count, lint results, and anything not fixed.
   Never `git add -A`, never push.
