---
name: 2026-08-11-phase5-postprocessing-and-move
status: done
created: 2026-08-11
model: sonnet
completed: 2026-08-12
result: |
  core/verify.py, core/extract.py, core/postprocess.py, core/audit.py, and
  RemoteConnectionPool.delete_path (core/remote.py) built and verified. `move` accepted in
  IMPLEMENTED_SYNC_MODES (`sync` still rejected); inline misconfiguration warning + required
  confirmation checkbox added at the Settings -> Queues mode selector; per-queue
  verify/extract/move toggles and a working Settings -> Post-processing page added. 244
  backend tests pass (0 skipped, fake seedbox up), including a real end-to-end move-mode
  transfer -> verify -> asyncssh delete -> confirmed-by-rescan test. Both ruff gates and
  npm build/lint clean; all three docker-compose files validate. Fake-seedbox containers
  torn down and confirmed removed.

  Not committed -- prepared on the working tree per the handoff-prompt workflow, proposed
  commit message left for the user.

  FLAG FOR THE USER, READ FIRST: the user's one live queue has sync_mode stored as 'move'
  from before phase 4's guard existed. It was inert until this phase; `move` is now
  implemented, so that row is live -- the next completed, verified download on that queue
  will have its remote copy deleted. The row was deliberately left untouched (not reset to
  'copy', not disabled) per this phase's explicit instruction; it is the user's call. See
  docs/decisions.md's phase 5 entry (point 0) and prompts/startnewsession.md's "Where we
  are" for the full writeup.
---

# Task: Phase 5 — post-processing and `move` mode

Everything that happens *after* a transfer completes: verification, archive extraction, the
staging→final move, and the first feature that deletes data on a machine we don't own.

**Done when:** a completed item verifies, extracts, and lands in its final home; and a `move`
queue deletes the remote copy **only** after verification, with an audit trail.

## Before you start

- **Read `DESIGN.md` §6** (post-processing), **§7 in full** (sync modes — especially §7.1 on why
  deletion is safe here and §7.4 on the deletion mechanism), §3.1 (`item` lifecycle columns,
  `event`), §3.2, §13 phase 5, §15.
- Read `prompts/startnewsession.md`, especially "what real hardware taught us".
- Read `docs/decisions.md` — phase 4 added a long entry; several constraints carry over.
- Phases 1–4 are committed. Auto-queue, patterns, the mount sentinel, and `EXCLUDED` all exist.

## Working tree check

`git status --porcelain` first. Anything dirty: list it and ask. This file is exempt.

## This phase deletes data. Read this twice.

`move` is the first feature that removes files from the user's seedbox. It is irreversible and
it runs unattended.

- **`move` and `sync` are currently rejected by the API** (`IMPLEMENTED_SYNC_MODES` in
  `api/settings.py`) and disabled in the UI, because they did nothing. This phase implements
  `move` — so add `"move"` to that set and re-enable it in the selector. **`sync` stays
  rejected and disabled**; it is not scheduled (§7).
- **Do NOT modify the user's live data or config.** Their one queue has `sync_mode` stored as
  `move` from before the guard existed. Implementing `move` will make that row *live*. **Leave
  the row alone and say so prominently in your report** — it is the user's call whether to keep
  it, and they are asleep. Do not "helpfully" reset it either; just flag it.
- Deletion happens through **our own asyncssh path** (§7.4), never lftp's
  `--Remove-source-files`. That keeps verification as the gate, keeps every delete auditable,
  and gives one code path.
- **Verification gates deletion.** An item that fails or skips verification is never deleted.

## What to do

### 1. `core/postprocess.py` — the pipeline

Triggered on transition to `DOWNLOADED`, run in a thread pool, one item at a time by default.
Every step independently toggleable **globally and per queue, all defaulting off** except where
noted.

**Verify** — `.sfv` / `.md5` sidecars when present; otherwise optional hash-on-disk. Result
`VERIFIED` or `CORRUPT`. This is load-bearing now: it is the gate on an irreversible remote
delete, not optional garnish.

**Extract** — rar/rar5/zip/7z/tar/gz/bz2/xz via `7zz` (the image's only archive tool — see
`NOTICE` and `docs/decisions.md`; there is no `unrar`). Multi-part rar sets extract from the
first volume only. Optional password list. Target: in place, or a configured directory. Failures
record `EXTRACT_FAILED` and never abort the pipeline for other items.

**Move** — staging → final destination. `os.rename` fast path, cross-device copy+fsync+unlink
fallback. Note the user's downloads live on **NFS**, so cross-device is the likely path, not the
exception; make sure a partial copy can't be mistaken for a complete one.

### 2. `move` mode

On verified completion, delete the remote copy via §7.4's asyncssh path. Every delete — **and
every delete withheld, with the failing precondition** — writes an `event` row naming the item,
the queue, the mode, and what gated it. A remote delete is irreversible; the minimum bar is
reconstructing exactly what happened and why.

Set `item.remote_deleted_at` and move the item to its terminal state (§3.2's `REMOVED_BOTH`
naming exists for the `sync` case; for `move`, pick whatever is consistent and record the call).

### 3. The misconfiguration warning

§7.1's warning must appear **inline at the mode selector**, not only in docs: pointing a `move`
queue at a live torrent *data* directory rather than a hardlink pickup directory will destroy
seeding torrents. Switching a queue to `move` requires explicit confirmation.

## Verify before reporting — actually run these

Fake seedbox: `docker/test-seedbox/gen_key.sh` then `docker compose -f docker-compose.test.yml up -d`
(`seeduser`/`testpass123`, ports 2222/2223). **Tear it down afterwards** and confirm with
`docker ps -a`.

1. `uv run pytest` passes. New tests must include:
   - **`move` deletes the remote only after verification** — a `CORRUPT` or unverified item
     leaves the remote intact, and the withheld delete is recorded;
   - the delete goes through the asyncssh path, not lftp;
   - extraction of a real archive you construct (7zz handles zip/7z natively — use a format you
     can create in the test);
   - staging→final move across a simulated cross-device boundary (mock or monkeypatch
     `os.rename` to raise `EXDEV`) leaves no partial file at the destination on failure;
   - every post-processing step is off by default.
2. **End-to-end against the fake seedbox**: a `move` queue transfers an item, verifies it, and
   the file is **gone from the seedbox afterwards** — confirm by scanning again, not by
   assuming. Report exactly what you observed.
3. `npm run build` and `npm run lint` clean.
4. **Both lint gates repo-wide, exactly as CI runs them** — `check` alone is not enough and has
   broken the build before:
   ```
   uvx ruff@0.8.4 check  --config ruff.toml .
   uvx ruff@0.8.4 format --config ruff.toml --check .
   ```
5. `docker compose config --quiet` clean on all three files.

State plainly anything you could not verify.

## Surfacing decisions

The user is asleep and asked that **every decision made without them be documented**. Record each
in `docs/decisions.md` (newest at top) with rejected alternatives, and repeat them in your report.
If `DESIGN.md` is wrong or silent, make the smallest reasonable call, record it, and **do not edit
`DESIGN.md`**.

Given this phase deletes data, be conservative: when a choice is between "delete" and "don't
delete and report", choose the latter and record it.

## When done

1. `docs/decisions.md` entries.
2. Update `prompts/startnewsession.md` (phase table, "Where we are").
3. Frontmatter: `status`, `completed`, `result`.
4. `git mv` this file to `prompts/done/` (or `prompts/failed/`).
5. **Do NOT commit.** Report the file list and a proposed one-line commit message (`feat:`
   prefix, no `Co-authored-by:`; branch `dev`).
