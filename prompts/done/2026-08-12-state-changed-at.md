---
name: 2026-08-12-state-changed-at
status: done
created: 2026-08-12
model: sonnet
completed: 2026-08-12
result: >
  Added migration 006 (item.state_changed_at, two triggers: AFTER INSERT for new rows since
  SQLite refuses a non-constant ALTER TABLE ADD COLUMN DEFAULT on a populated table, AFTER
  UPDATE OF state guarded on IS NOT for actual changes), plumbed it through ITEM_VIEW_COLUMNS
  / item_view / models.py / types.ts, and rendered it on the Files tree as a per-state
  relative-time label (Intl.RelativeTimeFormat, one shared per-tree ticker, absolute time on
  hover). 8 new backend tests covering trigger firing/non-firing/re-entrancy plus the
  backfill; all 547 backend tests, both ruff gates, and frontend lint/build pass.
---

# Task: Record when each item last changed state, and show it in Files

The user wants the Files page to show when a row last changed — "if it moved to
`DOWNLOADED`, when did that happen; if it moved to remote, when did that happen" — as
relative time ("3 min ago"), with the absolute time on hover.

One timestamp meaning "when did this row last change state", not per-state columns. The
label comes free from the state the row already carries: "Downloaded 3 min ago",
"Remote 2 hr ago".

## Before you start

- Read `DESIGN.md` §3.2 (the state rules) and §9.2 (the Files page).
- Read `core/itemview.py` — the single projection shared by `GET /api/files`, the
  `queue_delta`, the connect-time `snapshot()`, and the queue's item publishing. Anything
  you add reaches all of them at once through this one file.
- Read `prompts/startnewsession.md`'s "Traps worth knowing", especially the publish
  invariant (reconcile → persist → read back → diff → publish).
- Read `backend/lftpweb/migrations/` — follow the existing `NNN_description.sql`
  convention and the transaction/rollback shape `db.py.migrate()` expects.

## Working tree check

Run `git status --porcelain`. Other agents recently worked in `core/queue.py`,
`core/progress.py`, `core/itemview.py`, `core/extract.py`, `core/postprocess.py`,
`db.py`, `core/engine.py`, `api/`, `FileTree.tsx`, and `FilesPage.tsx`. If any file you
need is dirty, list it and ask. This prompt file is exempt.

## Use migration number 006

`005` is the metrics migration. Other queued tasks are reserved `007` and `008`. Use
**`006`** and nothing else, so parallel work does not collide.

## Why a trigger, not writer discipline

`item.state` is written from **three** modules — `core/engine.py._persist` (two
`INSERT ... ON CONFLICT DO UPDATE` statements), `core/queue.py` (QUEUED, DOWNLOADING,
DOWNLOADED, STOPPED, FAILED), and `core/postprocess.py` (`_set_item_state` plus the
verify/extract branches). Requiring every writer to also stamp a timestamp guarantees one
gets missed, and a timestamp that is silently wrong is worse than none. This fragmentation
is exactly what `DESIGN.md` §3.2 rule 9 — still an unapproved proposed wording, see
`docs/decisions.md` — exists to address.

**Enforce it in the schema:**

```sql
ALTER TABLE item ADD COLUMN state_changed_at TEXT;

CREATE TRIGGER item_state_changed_at
AFTER UPDATE OF state ON item
WHEN NEW.state IS NOT OLD.state
BEGIN
  UPDATE item SET state_changed_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
```

Properties to **verify with tests**, not assume:

- It fires on the `ON CONFLICT DO UPDATE` branch of `_persist`'s upserts — that is where
  most transitions actually land.
- It does **not** fire when the 30s rescan rewrites the same state (`IS NOT` guard), so
  the clock does not churn.
- It cannot re-enter: the trigger's own `UPDATE` touches `state_changed_at`, not `state`.
- New rows get a value. Add `DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))` on the column
  so a first-sighted `REMOTE_ONLY` item is stamped at insert — that is the user's "when
  did it become remote" case. Note SQLite's `ALTER TABLE ADD COLUMN` restrictions on
  defaults and handle it accordingly (a plain `ALTER` plus an `UPDATE` backfill, or a
  table rebuild — your call, but say which and why).

**Backfill existing rows** from `COALESCE(extracted_at, verified_at, downloaded_at,
first_seen_at)` and label it in the migration comment as the approximation it is:
everything already in the database gets a guess, everything from the migration forward is
exact.

## Plumbing

`ITEM_VIEW_COLUMNS` is currently
`id, rel_path, is_dir, remote_size, local_size, remote_mtime, state` — **no timestamps
reach the frontend at all.** Add `state_changed_at` there and it arrives via all three
paths at once. Then `models.py` (~line 224) and `frontend/src/api/types.ts` (~line 170).

## Frontend

- Relative time via **`Intl.RelativeTimeFormat`** — built in, no new dependency. This
  project has deliberately avoided adding frontend dependencies (see the TanStack Query
  note in `docs/decisions.md`); do not add one here.
- Absolute time in a `title` on hover, rendered in **local** time. (History's date filters
  are UTC-only, a documented phase 6 limitation — relative time on this page sidesteps
  that question entirely rather than inheriting it.)
- **`FileTree.tsx` is virtualized.** Use **one** shared ticker at the page/tree level that
  bumps a counter the rows read — never a `setInterval` per row. With thousands of rows a
  per-row timer is a real cost.
- Label the column by state: "Downloaded 3 min ago", "Remote 2 hr ago", etc. Handle a null
  `state_changed_at` gracefully (possible for rows the backfill could not date).

## One thing to NOT do

Do not repoint anything else at `state_changed_at`. In particular, the planned local
retention feature must key on `downloaded_at`, not this column — "when did it complete"
and "when did it last move" are different questions, and a `DOWNLOADED` item that dips to
`PARTIAL` and back would otherwise earn a fresh retention lease it has not earned. Leave
a comment on the column saying so, so the next person does not wire it up wrongly.

## Conventions to honor

- `docs/decisions.md`, newest at top: why a trigger rather than writer discipline, and how
  you handled the `ALTER TABLE ... DEFAULT` restriction.
- `CHANGELOG.md` under `## [Unreleased]` → `### Added`.
- Test the **migration** itself the way `tests/test_db.py` already tests migrations:
  build a database at 005, run `migrate()`, and assert both the backfill and the trigger's
  live behaviour.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` and `npm run build` in `frontend/`.
- `uv run pytest` with the fake seedbox up; tear it down afterward, confirm with
  `docker ps -a`.
- **You cannot see the UI.** No browser here.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` it into `prompts/done/` (or `prompts/failed/`).
3. Record decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `feat:` message, test count, lint results, and anything found but not fixed. Never
   `git add -A`, never push.
