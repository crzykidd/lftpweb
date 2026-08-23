---
name: 2026-08-23-path-attribution-and-category-escape-hatch
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  Path-based attribution shipped as primary (component-boundary match against every enabled
  queue's remote_path, no config needed), category mapping demoted to the fallback for a
  transfer with no content_path yet, path wins on disagreement (logged). Manual "Add category"
  escape hatch restored (source: 'client'|'manual', migration 030), blank/duplicate rejected
  visibly. Detected categories now persist per instance (migration 030,
  detected_categories_json/_at) and survive a reload without re-testing. Unattributed-clients
  banner widened with per-category breakdown + a distinct no-category count. Corrected mid-task
  on live evidence that path attribution does not remove the category mapping's necessity for
  rTorrent (seeding-directory content_path, spec 1.1) -- only for SABnzbd. Left an explicit open
  question (spec 8.3 round-4 correction, docs/decisions.md): an uncategorised rTorrent item with
  a non-matching path has no attribution route at all. All gates green: pytest 2019 passed,
  ruff check/format clean, frontend build/lint/779->783 vitest tests clean.
---

# Task: Attribute by path first; make the category mapping optional again

The category→queue mapping was made **load-bearing** for attribution, and it should never have
been. This task demotes it to an optional override, restores the ability to add one by hand, and
stops the settings form going blank after a reload.

**Read findings #2, #10, #11 and #14 in `prompts/test-findings-2026-08-23.md` first** — this is the
fourth round on the same control, and the earlier rounds' reasoning matters.

## The core mistake

`core/clientsync.py` attributes a client's transfer to a queue **only** through the configured
category mapping, and drops anything else before ever looking at the path:

```python
if not transfer.category:
    continue  # unattributable -- no category reported at all
queue = category_map.get(transfer.category)
if queue is None:
    continue
```

But the filesystem already answers the question. SABnzbd's history reports
`storage = /home/crzykidd/downloads/complete/ar-tv/Show.S01`; the queue's `remote_path` **is**
`/home/crzykidd/downloads/complete/ar-tv`. That is a direct match requiring **no configuration at
all**. Same for rTorrent's `content_path`.

The user's own words, twice: *"the category is ar-tv and the dir for that is ar-tv"*, and *"this
makes zero sense to me"*. They are right — they were made to configure something already knowable.

## What to build

### 1. Path-based attribution becomes primary

Attribute a transfer to a queue by matching its `content_path` against every queue's `remote_path`,
**component-boundary containment or equality** — never a bare prefix (`/complete/ar-tv` must not
match `/complete/ar-tv-extra`). The same rule stage 2b already uses for the settle-gate skip; reuse
that helper rather than writing a second one.

**Order of attribution:**

1. `content_path` matches a queue's `remote_path` → that queue. No configuration needed.
2. Otherwise, the configured category mapping, if one exists.
3. Otherwise unattributable, as today.

**Path wins when both are present and disagree** — the path is where the bytes actually are, and a
stale mapping should not override observed reality. Log/note the disagreement rather than silently
preferring one; a mismatch is a signal the user's config is wrong.

**A transfer with no `content_path` yet** — still in the client's queue, nothing on disk — is
exactly and only where the category mapping is still needed. That is now its whole job.

### 2. Manual add returns as an escape hatch

`rtorrent.list_categories` declares itself `DERIVED` because it can only report labels **currently
in use**. So a category that will exist later (`ar-movies`, before any movie is grabbed) can never
be detected — and the redesign removed the only way to enter it. The capability declaration named
this limitation and the UI ignored it.

- Restore an **"Add category"** control, clearly secondary to the detected list.
- A hand-added row is marked as such (`source: manual`, mirroring base paths' own manual escape
  hatch, which exists for exactly this reason).
- **Keep the redesign's core win**: a manually-added row must not be silently droppable. If its name
  is blank it cannot be saved — reject it visibly rather than filtering it out at submit time, which
  is the defect (#11b) that started this whole thread.

### 3. Detected categories survive a reload

Today they live only in `testResults[editingId]` — in memory for the session — so re-opening a saved
instance shows an empty form and reads as data loss. The previous round chose to reword the hint
instead of persisting; **on the user's evidence that was the wrong call.**

Persist the last detected category list alongside the instance (a column, or reuse the existing
capabilities/probe JSON — your judgement), stamped with when it was detected, and render it with
its age. Re-Test refreshes it.

### 4. Make the control's reduced role obvious

Once path attribution works, most setups need this control **not at all**. The section should say
so: something like *"Most downloads are matched automatically by their folder. Use this only for
categories whose downloads land outside a queue's folder, or to bind a category before its first
download."*

## Tests

- **A transfer whose `content_path` sits under a queue's `remote_path` is attributed with no
  category mapping configured at all.** This is the headline behaviour — assert it directly.
- Component-boundary: `/complete/ar-tv` does not match `/complete/ar-tv-extra`.
- A transfer with **no** `content_path` and a mapped category is attributed by category.
- A transfer with no path and no mapping is unattributable (unchanged).
- Path and mapping disagreeing → path wins, and the disagreement is visible.
- A manually-added category persists across save/reload; a blank one is **rejected, not silently
  dropped**.
- Detected categories survive a reload without re-testing.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — explicit timeout of at least 600000 ms on every gate Bash call.
**Run backend gates from the REPO ROOT**; use a subshell `( cd frontend && … )`.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. `npm run build`, `npm run lint`, `npm test`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md`, update
spec §8.3 (attribution is no longer category-only — record this as a correction with its cause, the
way §8.2 and §11.1c record theirs), and append a resolution note under findings #10/#11/#14.
**Do not commit or push.** Report: files, every exit code, both test counts, a proposed one-line
message, how you persisted the detected categories, and **what you could not verify without a real
browser**.
