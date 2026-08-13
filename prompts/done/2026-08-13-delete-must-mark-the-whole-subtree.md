---
name: 2026-08-13-delete-must-mark-the-whole-subtree
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >-
  Fixed. delete_local() now marks the target's whole subtree (_subtree_rows, matched in
  Python, never SQL LIKE) in the same transaction as the filesystem delete, and chooses
  REMOVED_LOCAL vs REMOVED_BOTH per row from item.remote_size (_removed_state_for) instead
  of a hardcoded REMOVED_BOTH. Retention shares the primitive and is fixed by the same
  change. WS item_delta now publishes the whole subtree. 12 new tests + 1 new e2e file
  against the fake seedbox; 623 passed (was 611), both ruff gates clean. Not committed --
  left for the orchestrating session to review and commit.
---

# Task: A local delete must mark the whole subtree, not just the row that was clicked

Found by the user on 2026-08-13, testing the delete feature that shipped hours earlier in
`dfb74c2`. They deleted a top-level directory from the Files list: the directory row correctly
showed `REMOVED_BOTH` ("Removed 20s ago"), **and every file inside it went on showing
`DOWNLOADED` even though the files were gone.**

## Mechanism — already diagnosed, build on it

`core/local_delete.py.delete_local` finishes with:

```sql
UPDATE item SET state = 'REMOVED_BOTH', auto_queue_suppressed = 1,
                suppressed_reason = 'deleted_local' WHERE id = ?
```

`WHERE id = ?` — **one row.** The descendant `item` rows are untouched, so the next scan finds
them locally absent but still present remotely, and they enter §7.3's absence grace period
(`core/mount_sentinel.py.resolve_absence`, `DEFAULT_GRACE_S = 600.0`). During those ten
minutes `resolve_absence` deliberately holds their previous state — which is `DOWNLOADED`.

That grace period exists for absences that might be *lies*: an NFS mount that flapped, an
importer mid-move. **It has no business applying to a deletion this codebase performed itself
and has a record of.**

Two distinct defects follow:

1. **The visible one.** Files inside a deleted directory read `DOWNLOADED` for ten minutes
   after being deleted. Simply wrong, and it undermines confidence in a destructive action.
2. **The consistency one.** When the grace period does elapse, descendants land at
   `REMOVED_LOCAL` **unsuppressed** — not `REMOVED_BOTH` + `deleted_local` like their parent.
   That breaks the invariant `6d3bd95` depends on: *lftpweb's own deletions are never
   re-fetched, under either value of `re_download_externally_removed`.* Today nothing actually
   re-downloads, because `core/autoqueue.py` only ever considers **top-level** items and the
   parent is suppressed — so state your findings precisely rather than overstating the
   severity. But the rows are wrong, they contradict their own parent, and the guarantee holds
   by accident rather than by construction.

**Retention shares the primitive, so it has the identical bug.** Fixing `delete_local` fixes
both; confirm that rather than assuming it.

## Before you start

- `core/local_delete.py` in full — the guards, the dry-run path, and the update above.
- `core/mount_sentinel.py` — `resolve_absence`, `DEFAULT_GRACE_S`, and the states the grace
  clock can start from.
- `core/engine.py._persist` — how absence resolution and `_protected_rel_paths` interact.
- `core/itemview.py` and `core/engine.py.diff_nodes` — how removals reach the UI.
- `prompts/open-issues.md` § "4" for why the suppression marker matters.

## Working tree check

`git status --porcelain`. The archive-cleanup task (`prompts/2026-08-13-delete-archives-after-
extract.md`) also works in `core/local_delete.py` and may have landed just before you. Read the
current file, not the version described above, and if it is dirty list it and ask.

## What to do

0. **First, fix the state itself — `REMOVED_BOTH` is the wrong state when a remote copy
   still exists.** The user raised this directly: after a delete, the Files list no longer
   reflects what is on disk. `dfb74c2` set `REMOVED_BOTH` for *every* delete and its own report
   flagged this as "knowingly overloaded to also mean local-only delete, remote untouched,
   diverging slightly from DESIGN.md §3.2's literal wording." That overload is now causing a
   visible wrong answer, so undo it rather than extend it.

   Choose the state from what is actually true:

   - **A remote copy still exists** (the normal `copy`-mode case) → **`REMOVED_LOCAL`**. True,
     and strictly more informative than `REMOTE_ONLY`: it says "this was downloaded and is now
     locally gone," not "this was never here."
   - **No remote copy** (a `LOCAL_ONLY` item, or a `move` queue whose remote was already
     deleted) → **`REMOVED_BOTH`**. Both copies really are gone.

   **There is no tension with not re-downloading.** `auto_queue_suppressed = 1` +
   `suppressed_reason = 'deleted_local'` is what prevents the re-fetch; the state does not have
   to lie to achieve it. Keeping the *policy* in the flag and the *truth* in the state is the
   same separation `6d3bd95` established — read `prompts/open-issues.md` § "4".

   This also makes the Files list behave correctly through machinery that already exists:
   `core/engine.py._project` filters out paths in neither tree, so a `REMOVED_LOCAL` item with
   a live remote stays visible (and manually re-queueable), while a genuinely-gone
   `REMOVED_BOTH` item drops out of the list.

   Determine remote presence from persisted state, not a live scan — `item.remote_size` is what
   `FileTree.tsx`'s delete dialog already uses for the same distinction. Confirm that is sound
   rather than assuming it.

1. **Mark the whole subtree in the same transaction** as the target row: every `item` in the
   same queue whose `rel_path` is the target or lies beneath it, each getting the state chosen
   per step 0 (evaluated per row — a directory can contain a mix, e.g. an `EXCLUDED` child that
   never had a remote counterpart), plus `auto_queue_suppressed = 1` and
   `suppressed_reason = 'deleted_local'` on all of them.

   **Get the path matching right.** A `LIKE 'target%'` prefix match is wrong — it also matches
   a sibling named `target-extra`. Match the exact path *or* the path plus a `/` separator, and
   test that a sibling with a shared prefix is not swept in. Watch for SQL `LIKE` metacharacters
   (`%`, `_`) in real release names — `_` is extremely common in scene naming and matches any
   single character. Either escape them or don't use `LIKE`.

2. **Do it in one transaction with the target row**, so a crash mid-delete cannot leave a
   parent marked and its children not. The files are already gone at that point; the database
   must not disagree with the filesystem in a way that resolves differently on restart.

3. **Only rows under the deleted path**, and only in that queue. Two queues can hold the same
   `rel_path`.

4. **Do not change the grace period or `resolve_absence`.** The grace period is correct for
   what it is for. This bug is that a known deletion was being routed through a mechanism meant
   for unexplained absence. Fix it by recording what we know, not by weakening the fallback.

5. **Check the dry-run path agrees.** `preview_retention` / the dry-run mode must report the
   same set of items a real run would affect. If a real run now marks a subtree, the preview
   should reflect that — a preview that undercounts is worse than no preview.

6. **Check what the UI does with a subtree of removals at once.** The WebSocket publishes
   removals via `diff_nodes`; confirm a directory delete produces a coherent update rather than
   a stale subtree hanging around. You cannot see the UI, so verify at the message level and
   say that is what you did.

## Tests

- Delete a directory item with several descendants; assert **every** descendant row is
  suppressed with `deleted_local` immediately, with no scan in between and no grace period
  elapsed.
- **State correctness per row**: a deleted item whose remote copy still exists reads
  `REMOVED_LOCAL`; one with no remote copy reads `REMOVED_BOTH`. Include a directory holding a
  mix of both.
- **The Files list reflects disk**: after deleting a directory, a scan pass publishes the
  descendants as locally gone — none still reads `DOWNLOADED`. This is the user-visible
  symptom that started this task; assert it end to end rather than only at the row level.
- A `REMOVED_LOCAL`-after-delete item is **not** picked up by auto-queue, with
  `re_download_externally_removed` **both off and on** — the suppression flag, not the state
  name, must be what stops it.
- **Manual re-queue of a deleted item works** and re-downloads it. The user named this as
  required behaviour: "I should still see that it exists on the remote host and it won't get
  autodownloaded again... but I should have an option to manually queue it again."

  All three parts already work and this task must not break them — assert them rather than
  assuming: `core/queue.py.enqueue_item` clears `auto_queue_suppressed`/`suppressed_reason`
  and resets `attempt`; `FileTree.tsx`'s `rowAction` offers Queue for any state except
  `QUEUED`/`DOWNLOADING`/`LOCAL_ONLY`, so a `REMOVED_LOCAL` row gets the button; and
  `canDeleteLocal` correctly withholds Delete from a row with no local content. Add a test
  that a deleted item can be manually re-queued, transfers again, and comes back with its
  suppression cleared — end to end, not just at the `enqueue_item` call.
- A sibling directory sharing a name prefix (`Release` vs `Release-Extra`) is **not** touched.
- A release name containing `_` and `%` is handled correctly.
- The same `rel_path` in a different queue is not touched.
- Retention deletion marks subtrees too.
- A single-file item still behaves exactly as before.
- Dry-run reports the same set the real run marks.

## Conventions to honor

- `docs/decisions.md`, newest at top.
- `CHANGELOG.md` under `### Fixed`. Note the feature and the fix landed the same day and never
  shipped in a release — describe the net behaviour, not the detour, as `6d3bd95` did.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `uv run pytest` with the fake seedbox up.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `fix:` message, test count,
   lint results, how you matched subtree paths and why that is safe, whether retention was
   fixed by the same change, and anything not fixed. Never `git add -A`, never push.
