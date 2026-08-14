---
name: 2026-08-14-hide-queue-when-there-is-no-remote-copy
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: >
  rowAction now gates on the general hasRemoteCopy(node) fact instead of the single LOCAL_ONLY
  state string, hiding Queue for REMOVED_BOTH children and move-mode parents whose remote was
  deleted on purpose, while redownload and manual-queueing-ignores-suppression both still work.
  Confirmed remote_size is never null for a row with a genuine remote copy (reconcile.py sets it
  the instant remote_entry is seen; the only explicit NULL write is the vanished-row path), so no
  new sentinel was needed. Bulk queue already routed through rowAction; the item drawer has no
  Queue affordance. rowAction test coverage added (FileTree.test.ts); full frontend
  lint/test/build and backend pytest/ruff/compose-config all green.
---

# Task: Hide the Queue button on a row with no remote copy to fetch

**Reported live 2026-08-14.** After a `move`-mode release completed and its remote copy was
deleted, the Files page still offered **Queue** on the parent folder and on every removed child.
There is nothing to queue — clicking it would spawn a job against a remote path that no longer
exists.

**The user's call: hide the button, not disable it with a reason** — consistent with how
`LOCAL_ONLY` already behaves.

## The fix is one line, and the helper already exists

`frontend/src/components/FileTree.tsx`'s `rowAction`:

```js
if (node.id == null) return null
if (node.state === 'QUEUED' || node.state === 'DOWNLOADING') return 'stop'
if (node.state === 'LOCAL_ONLY') return null          // "nothing remote to fetch"
if (node.suppressed_reason === 'deleted_local' && hasRemoteCopy(node)) return 'redownload'
return 'queue'                                         // everything else falls here
```

Its own docstring already states the right principle — *"the one exception is a node with nothing
remote to fetch at all (`LOCAL_ONLY`), where there is nothing a 'Queue' action could mean"* — but
implements it by testing **one state string** rather than the fact. So a `REMOVED_BOTH` child, and
a `move`-mode parent whose remote this codebase deleted itself (`remote_deleted_at` set,
`remote_size` null, state `VERIFIED`/`EXTRACTED`), both fall through to `'queue'`.

`hasRemoteCopy(node)` (`remote_size != null`) is defined a few lines below and currently used only
by the `redownload` branch. Gate on it instead: it covers `LOCAL_ONLY`, `REMOVED_BOTH`, and the
move-deleted parent in one test, driven by presence rather than by an enumerated list of state
strings — the same correction this codebase has arrived at repeatedly (`core/itemview.py`'s R/L/V/E
facets read the world, never the state string).

Replace the `LOCAL_ONLY` special case with the general one; do not keep both. Update the docstring
so it describes the fact rather than the state.

## Watch for

- **`redownload` must still work.** Its own branch already requires `hasRemoteCopy`, so ordering
  the new gate before it is safe — but confirm, because that is the case where a row we deleted
  locally has had its remote copy come back, and it must not become unreachable.
- **A never-yet-scanned row.** If `remote_size` can legitimately be null for something that *does*
  have a remote copy (a fresh row before its first size is recorded), gating on it would hide a
  button that should be there. Check `core/reconcile.py`/`core/itemview.py` for whether that state
  is reachable, and say what you found. If it is, use a signal that cannot be confused with
  "not measured yet".
- Bulk actions and the item drawer may offer Queue by a separate path — check and make them
  consistent rather than fixing only the row button.

## Testing

Extend `frontend/src/components/FileTree.test.ts`. `rowAction` is a pure function, so test it
directly rather than mounting anything:

- `REMOVED_BOTH`, no remote → no button.
- A `move`-mode parent, `remote_size` null with `remote_deleted_at` set → no button.
- `LOCAL_ONLY` → still no button (unchanged behaviour, now via the general rule).
- `deleted_local` + remote present → still `redownload`.
- `STOPPED`/`FAILED` with a remote copy → still `queue`. **Manual queueing must stay unfiltered by
  suppression** — that is a documented rule (`rowAction`'s own docstring, DESIGN.md §4.7) and this
  task must not narrow it.
- `QUEUED`/`DOWNLOADING` → still `stop`.

Run `npm run lint`, `npm test`, `npm run build`; `uv run pytest` to confirm nothing backend moved
(none should — if a backend test fails, stop and report rather than adapting it); `ruff check` and
`ruff format --check`; `docker compose config --quiet` on all three compose files.

## Conventions to honor

- Record the decision in `docs/decisions.md` (newest at top), including why hidden rather than
  disabled-with-a-reason, since `cd74f91` established the opposite convention elsewhere and a
  future reader will wonder.
- `CHANGELOG.md` entry.
- **You cannot see the UI** — no browser exists here.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
