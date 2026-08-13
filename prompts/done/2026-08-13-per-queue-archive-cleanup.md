---
name: 2026-08-13-per-queue-archive-cleanup
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: |
  Per-queue `auto_delete_archives` (migration 012), ANDed with the site-wide flag exactly like
  verify/extract/move. Settings -> Queues now shows a "system setting" readout next to all four
  post-processing toggles, with the move-mode-verification exception worded correctly. The
  literal save-before-load race was found not reachable (Save isn't in the DOM until loading
  settles); a related, actually-reachable gap (a failed initial load leaving Save clickable from
  blank defaults) was fixed instead, plus a backend merge-on-PUT fix
  (`api/settings.py.put_postprocess_settings`/`put_retention_settings`) for fields a request
  genuinely omits -- which also fixes a real, non-hypothetical instance already in production:
  `failed_retention_enabled`/`_days` have no frontend field at all, so every save from Settings
  -> Post-processing was already discarding them. `delete_extracted_archives`'s no-archives
  branch now logs at debug (found unreachable from its one current caller; kept as defensive
  coverage). 738 tests passing (+5), both lint gates and the frontend build clean.
---

# Task: Give archive cleanup a per-queue toggle, and show the site half of every toggle

User request, 2026-08-13, after archive cleanup silently did nothing because the site-wide
setting had been switched off without them realising:

> Do we want to make this an override on each queue? Or at least show "System setting" in the
> queues?

## Why this is a real inconsistency, not a preference

Every other post-processing step is gated on **two** layers — a site-wide setting **AND** the
queue's own column (`core/postprocess.py.process_item`):

```python
verify_effective = (settings.verify_enabled and bool(queue["auto_verify"])) or sync_mode == "move"
```

`auto_verify`, `auto_extract`, `auto_move` all work this way. **Archive cleanup shipped
site-only** (`4533617`) and is therefore the odd one out — and it is the most destructive of
the four, since it deletes files with no remote copy left on a `move` queue.

## 1. Per-queue `auto_delete_archives`

- **Migration 012** (011 is `local_mtime`; check nothing else has claimed 012 before you
  start). A new `path_queue` column, `DEFAULT 0`, so every existing queue keeps behaving
  exactly as it does today — this project's rule is that a new capability changes nothing for
  an existing install until someone opts in.
- AND it with the site-wide `PostprocessSettings.delete_archives_after_extract`, in the same
  shape as the other three. Do not invent a tri-state; match the existing pattern.
- Settings → Queues gets the toggle alongside the existing verify/extract/move ones.

## 2. Show what the site-wide half resolves to — for all four

The user asked for this for archive cleanup; it applies equally to the other three and is
arguably worse there because they have been shipping this way for longer. **A per-queue toggle
can be on while the feature is globally off, and nothing in the Queues tab says so.**

Next to each per-queue toggle, show the site-wide value and the effective result. Wording is
yours, but "System setting: off — this queue's toggle has no effect" is the shape. Link to the
site setting if that is cheap.

Note the exception in whatever you write: **`move`-mode queues force verification on**
regardless of either toggle, because it is the sole gate on an irreversible remote delete. A
readout claiming verification is off for a `move` queue would be wrong.

## 3. Harden the silent-reset footgun

`models.py:168` gives `delete_archives_after_extract` a default of `False`, so **any PUT to
that endpoint omitting the field silently turns it off.** The frontend does send it, but a
save fired before the GET populates the form would write defaults over real settings. The
retention work hit exactly this class of bug already (see its `docs/decisions.md` entry:
"without them… every unrelated settings save would silently reset it").

Work out whether this is reachable in practice — a save-before-load race on the
Post-processing page — and if it is, fix it. Options: make the form unsubmittable until
loaded, or make the API merge rather than replace. **Say which you chose and why.** A settings
page that can silently disable a destructive-action toggle is worth a few minutes.

Check the other `*Settings` endpoints for the same shape while you are there. Report what you
find; do not fix them all unless the fix is uniform and obvious.

## 4. The silent path in archive cleanup

`core/local_delete.py.delete_extracted_archives` writes an `event` on every withhold — not a
directory, mount gate failed, path resolution failed, nothing deleted — **except one**:

```python
if not archive_heads:
    return ArchiveCleanupResult(deleted_rel_paths=(), bytes_freed=0)
```

No event, no log line. When the user was diagnosing why cleanup had not run, this was the one
branch that would have left no trace. Give it a debug-level log line at minimum, or an event
if that is not too noisy for the common no-archives case — most items have no archives, so
think about volume before writing an `event` row per scan.

## Before you start

- `core/postprocess.py.process_item` — the two-layer gating pattern.
- `core/local_delete.py.delete_extracted_archives`.
- `backend/lftpweb/migrations/` — the `path_queue` columns phase 4 and 5 added are your model.
- `api/settings.py`, `models.py`, `frontend/src/pages/settings/QueuesTab.tsx` and
  `PostProcessingTab.tsx`.

## Working tree check

`git status --porcelain`. Two other tasks may be in flight around `FileTree.tsx`,
`core/local_delete.py`, and `core/postprocess.py`. If files you need are dirty, list them and
ask rather than racing.

## Tests

- The AND gating: cleanup runs only when both layers are on, all four combinations.
- Migration 012 leaves every existing queue with the feature off.
- A PUT omitting `delete_archives_after_extract` does not silently disable it (or, if you chose
  the frontend fix, that the form cannot submit before it has loaded).
- `move`-mode verification still forced on regardless of toggles — do not regress it.

## Conventions to honor

- `docs/decisions.md`, newest at top. `CHANGELOG.md`. `DESIGN.md` §6/§7.3 — the user has given
  standing approval to edit it directly.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`; `uv run pytest` with the fake seedbox up.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line message, whether the
   silent-reset race is actually reachable and how you fixed it, what you found in the other
   settings endpoints, test count, lint results, and anything not fixed. Never `git add -A`,
   never push.
