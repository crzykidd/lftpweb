---
name: 2026-08-11-phase7-operations
status: done
created: 2026-08-11
model: sonnet
completed: 2026-08-11
result: >
  core/backup.py (VACUUM INTO backups, settings, retention, BackupScheduler), the
  pre-migration backup hook in db.py.migrate(), core/logtail.py (bounded tail),
  api/backup.py + api/logs.py, extended /api/health, and filled-in Settings -> Logs /
  Settings -> Backup pages. 33 new tests, 304 passed with the fake seedbox up (0 skipped).
  Both lint gates clean, npm build/lint clean, all three compose files validate. Not
  committed per instruction -- see the final report for the proposed commit message and
  file list. Not browser-tested (no browser available).
---

# Task: Phase 7 — operations: log viewer and database backup

The two things that make this survivable to run: being able to read the log without a shell, and
being able to get the database back.

**Done when:** Settings → Logs shows the rotating app log with level filtering and a download,
and Settings → Backup can take, list, download, and schedule `VACUUM INTO` backups — including
one taken automatically before any schema migration.

## Before you start

- **Read `DESIGN.md` §10 in full** (§10.1 logging, §10.2 backup, §10.3 health), §8 (why the
  encryption key is *not* in the backup), §13 phase 7.
- Read `prompts/startnewsession.md` and `docs/decisions.md`.
- Phases 1–6 are committed. `logsetup.py` already writes a rotating file log with a credential
  redactor and a polling-noise filter. `db.py` already has the hand-rolled migration runner.

## Working tree check

`git status --porcelain` first. Anything dirty: list it and ask. This file is exempt.

## What to do

### 1. Backup — `core/backup.py`

- **`VACUUM INTO`, never a file copy.** It is atomic and WAL-safe; copying the file while WAL is
  active can capture a torn database. This is the whole reason §10.2 specifies it.
- Target `<config>/backups/lftpweb-YYYYMMDD-HHMMSS.db`. **Daily by default, keep 7**, both
  configurable.
- **A backup immediately before any schema migration.** This is the one that actually saves you
  — migrations are the failure mode that loses everything, not a random Tuesday. Wire it into
  `db.py`'s `migrate()`, which was written with this hook in mind. Note migrations already run
  inside a transaction with rollback (phase 1 review); the pre-migration backup is the second
  net, not a replacement.
- **The encryption key is deliberately excluded** (§8, §10.2). A `.db` backup therefore contains
  no usable secret and is safe to download — the trade is that restoring to a fresh install
  needs the seedbox password re-entered. `core/crypto.py`'s secret must never be copied into a
  backup; if `VACUUM INTO` can't reach it, assert that in a test rather than assuming.
- Manual **Backup now** and **download**; retention prunes oldest beyond the keep count.

### 2. Log viewer — `api/logs.py` + Settings → Logs

- List the rotated files, tail the current one, filter by level, download.
- **Never stream the whole file into memory** — tail a bounded number of lines or bytes.
- Everything already passes the credential redactor on the way *in* (`logsetup.py`), which is
  the right place. Do not add redaction on the way out and call it defence in depth; verify the
  existing filter covers what this endpoint can expose, and say so.

### 3. Health

§10.3: `/api/health` should report DB reachability, host reachability, and whether the scheduler
loop is alive. It currently reports version/db/uptime/repo_url. Extend it without breaking the
existing shape — the container `HEALTHCHECK` and the UI's version link both depend on it.

## Verify before reporting — actually run these

1. `uv run pytest` passes. Tests must include:
   - **a backup is taken before a migration runs**, and it contains the pre-migration schema;
   - `VACUUM INTO` produces a valid, openable database (open it and query it, don't just stat
     the file);
   - **the backup contains no copy of the encryption secret**;
   - retention prunes to the keep count, oldest first;
   - the log tail endpoint is bounded and doesn't read the whole file.
2. **Exercise the pre-migration backup for real**: create a database at migration N, add a
   migration, run it, and confirm the backup file exists and opens.
3. `npm run build` and `npm run lint` clean.
4. **Both lint gates repo-wide, exactly as CI runs them** — `check` alone missed an unformatted
   file in phase 6 and has broken the build before:
   ```
   uvx ruff@0.8.4 check  --config ruff.toml .
   uvx ruff@0.8.4 format --config ruff.toml --check .
   ```
5. `docker compose config --quiet` clean on all three compose files. If you start any container,
   tear it down and confirm with `docker ps -a`.

State plainly anything you could not verify. No browser is available — do not imply the UI was
click-tested.

## Surfacing decisions

The user is asleep and asked that **every decision made without them be documented**. Record each
in `docs/decisions.md` (newest at top) with rejected alternatives, and repeat them in your report.
If `DESIGN.md` is wrong or silent, make the smallest reasonable call, record it, and **do not edit
`DESIGN.md`**.

## When done

1. `docs/decisions.md` entries.
2. Update `prompts/startnewsession.md` (phase table, "Where we are").
3. Frontmatter: `status`, `completed`, `result`.
4. `git mv` this file to `prompts/done/` (or `prompts/failed/`).
5. **Do NOT commit.** Report the file list and a proposed one-line commit message (`feat:`
   prefix, no `Co-authored-by:`; branch `dev`).
