---
name: 2026-08-12-fix-backup-vacuum-race
status: completed          # pending | completed | failed
created: 2026-08-12
model: sonnet            # coding task — the direction is already decided
completed: 2026-08-12
result: >
  Fixed. Wrote a deterministic regression test first (an open, uncommitted transaction on the
  app connection, then create_backup()), watched it fail with the exact reported error, then
  gave VACUUM INTO a dedicated connection with a 30s busy_timeout. create_backup's signature
  is unchanged -- it asks the connection for its own database file via PRAGMA database_list
  instead of trusting db_path(config_dir) alone (see docs/decisions.md). Verified the
  pre-migration backup in db.py.migrate() is safe: it runs before any migration's write lock
  is held. Full suite: 490 passed (489 + the new regression test). Both ruff gates clean.
---

# Task: Give `VACUUM INTO` its own connection so backups stop racing writers

`core/backup.py.create_backup` runs `VACUUM INTO` on the **shared application
connection**, and SQLite cannot `VACUUM` inside a transaction. Every writer in the app
holds a transaction between its `execute` and its `commit`, so any backup that lands in
that window dies with:

```
sqlite3.OperationalError: cannot VACUUM from within a transaction
```

This is **shipped in `:dev`** (images were published from `fe80aaf`) and CI catches it on
`fe80aaf` as `tests/test_backup_api.py::test_backup_now_creates_and_lists_and_downloads`.
It **passes locally** — the failure is timing-dependent, so *a green local run proves
nothing about this bug*. Only the deterministic reproduction below does.

Why it matters now: the race has existed since phase 7, but writes used to be
event-driven (scans, transfers), so the window was narrow and nobody hit it. The
2026-08-12 metrics sampler writes a `metric_heartbeat` row **every 30 seconds
unconditionally, including when completely idle**, which turned it from rare into
routine. Scheduled backups **default ON** (daily, keep 7), so a real instance quietly
starts failing its nightly backup.

## Before you start

- Read `DESIGN.md` §10.2 (why `VACUUM INTO` and never a file copy — that decision is
  **not** up for revisiting; the fix is about *which connection* runs it, nothing else).
- Read `backend/lftpweb/core/backup.py` in full, including its module docstring — it
  documents the three callers and the encryption-secret guarantee.
- Read `backend/lftpweb/db.py` — `db_path(config_dir)`, `connect(config_dir)` and the
  pragmas it applies, and `migrate()`'s pre-migration backup call site.
- Read `CLAUDE.md` for the per-session rules (commit prefixes, no `Co-authored-by:`,
  docs ship in the same commit).

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this
plan needs to modify. If any have uncommitted changes, list them and ask before touching
them. Surface unrelated dirty files once as awareness; don't block. This file (the
handoff prompt) is exempt.

## The reproduction — write this test FIRST, watch it fail

This is the whole point of the task. Reproduced deterministically already:

```
in_transaction: True
BACKUP FAILED: OperationalError cannot VACUUM from within a transaction
```

Shape: open the app connection, `INSERT` a row and **do not commit**, assert the
connection is in a transaction, then call `create_backup(...)`. On today's code it raises
every single time. Add it to `tests/test_backup.py` as a named regression test whose
docstring says what it guards, and confirm it fails before your fix and passes after.

## What to do

1. **Write the failing regression test** (above). Do not proceed until you have watched
   it fail on unmodified code — a fix for a bug you never reproduced is a guess.

2. **Give `VACUUM INTO` a dedicated connection.** Open a fresh `aiosqlite` connection to
   the database file, run the `VACUUM INTO`, close it. WAL mode makes a second connection
   safe. Set a `busy_timeout` on it (something like 30s) so a concurrent checkpoint or
   writer produces a wait rather than an instant `SQLITE_BUSY`; check what
   `db.py.connect()` already does about `busy_timeout` and stay consistent with it.

   **Committing first and then vacuuming is NOT sufficient** and must not be what you
   build — another coroutine can open a transaction between the commit and the `VACUUM`.
   Isolation of the connection is the fix; ordering is not.

3. **Decide how `create_backup` learns which file to back up, and prove it.** Its current
   signature is `create_backup(db, config_dir, *, reason=...)`, and there are four call
   sites (`api/backup.py`, `db.py.migrate()`, `BackupScheduler.run_if_due`, and
   `tests/test_backup.py`). Two workable options:

   - Use `db_path(config_dir)` and **verify every existing call site and test passes a
     `config_dir` whose database is the same one the `db` connection is attached to.**
   - Ask the connection itself (`PRAGMA database_list`, a read that is safe inside a
     transaction) for the `main` database's file path, falling back to
     `db_path(config_dir)`. This preserves the "back up *this* connection's database"
     contract exactly, at the cost of a little indirection.

   Pick one, say why in `docs/decisions.md`, and make it hold for **all four** call
   sites. If you drop the now-unused `db` parameter, update every caller — don't leave a
   parameter that lies about what the function reads.

4. **Check the pre-migration backup specifically.** `db.py.migrate()` takes a backup
   before the first pending migration runs. The brief's assessment is that it is
   *probably* safe (it fires after a `commit()` and before the background loops start) —
   **verify that rather than assuming it**, and make sure your change doesn't introduce a
   new problem there: a second connection opened while a migration holds a write lock is
   a different situation from one opened at idle. Note what you found.

5. **Confirm the CI-failing test now passes:**
   `tests/test_backup_api.py::test_backup_now_creates_and_lists_and_downloads`.

6. **Re-verify the guarantees the existing tests encode** — especially
   `test_backup_never_contains_the_encryption_secret` and
   `test_create_backup_produces_a_valid_openable_database`. A dedicated connection must
   not change what ends up in the backup file.

7. **Run the full suite**: `uv run pytest` from the repo root. The fake seedbox
   (`docker-compose.test.yml`) should be up so nothing skips — 489 tests pass today; you
   should land at 490+ with your regression test. Tear the fake-seedbox containers down
   afterward and confirm with `docker ps -a`.

8. **Lint gates, both of them.** `format --check` has caught files that `check` alone
   missed three separate times in this project's history — run both.

## Conventions to honor

- Match `core/backup.py`'s existing commenting style: it explains *why* a choice was made
  and names the alternative that was rejected. Your new connection deserves a comment
  saying it exists because `VACUUM` cannot run inside another coroutine's transaction —
  otherwise someone "simplifies" it back to the shared connection in six months.
- `docs/decisions.md`, newest at top, with the rejected alternative (commit-then-vacuum)
  named explicitly.
- `CHANGELOG.md` — add this under `## [Unreleased]` → `### Fixed`.
- `prompts/startnewsession.md` — **delete the `### 🔴 KNOWN BUG, unfixed, shipped in
  `:dev` — fix this first` section** and replace it with a short note that it was fixed,
  when, and that `:dev` images built before the fix still carry it. Also add a one-line
  entry to the "Traps worth knowing" list: *`VACUUM` cannot run on a connection anyone
  else might have a transaction open on.*
- Scope discipline: fix this race and nothing else. If you spot other bugs, **write them
  down in your report** — the user is actively collecting a bug list for the next round of
  prompts — but do not fix them here.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (or `prompts/failed/`).
3. Record the non-obvious decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the working tree, then report back to the orchestrating
   session with the file list and a proposed one-line `fix:` message. The orchestrator
   surfaces the `y/n` to the user. Never `git add -A`, never push, never auto-commit.
5. In your report, state plainly: whether you watched the regression test fail before the
   fix, the final test count, and anything you found but deliberately did not fix.
