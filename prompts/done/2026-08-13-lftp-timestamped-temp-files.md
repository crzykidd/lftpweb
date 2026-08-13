---
name: 2026-08-13-lftp-timestamped-temp-files
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Root cause fixed: enqueue_item is now idempotent against an existing active job, and
  TransferQueue._admit/_spawn_decision independently refuse to run two processes for the same
  item regardless of how many job rows exist. autoqueue.py's "no active job" eligibility is now
  enforced by its query (NOT EXISTS), not just claimed by its docstring. local_scan.py recognises
  both .lftp and .lftp~<timestamp>~ as the same temp-file concept (TEMP_FILE_RE, LocalEntry.is_temp);
  reconcile.py refuses to call a still-temp entry complete regardless of reported size, closing the
  false-DOWNLOADED-triggers-remote-delete path on move queues. local_delete.py cleans up every temp
  variant on delete. Added an off-by-default orphan-temp-file sweep (local_scan.sweep_orphan_temp_files,
  wired into RetentionScheduler, Settings API only, no frontend yet). FileTree.tsx's bulk Queue
  button now filters to queueable rows. Empirically verified against the fake seedbox (SIGKILL mid
  -transfer, then a fresh job): resume works correctly, measured in bytes -- was never broken. No
  lftp setting controls the ~timestamp~ variant; the concurrent-writer race is internal to lftp.
  857 tests pass (was 828, +29), both lint gates clean, frontend build clean.
---

# Task: an item can be queued twice and run two concurrent lftp processes

User report, 2026-08-13, downloading a large directory of mkvs:

> I see these showing up. `S.W.A.T.S06E21.2017.1080p.NF.WEB-DL.DDP5.1.H264-HHWEB.mkv.lftp~20260813154311~`

and then, decisively:

> it should only have 2 processes running but i had 4 like the job was queued 2 times

## 0. The root cause — fix this first

**`core/queue.py.enqueue_item` has no guard against an existing active job.** It fetches the
item, inserts a new `job` row, and sets the item to `QUEUED` — unconditionally. There is no
check for a `queued`/`running` job on that item.

So a second Queue / Re-Download / Retry click on an item that is already transferring creates a
**second job row**, which the scheduler admits and spawns as a **second concurrent lftp process
against the same remote and local paths**. A double-click does it; so does clicking Queue on an
item auto-queue has just picked up.

Consequences:

- Two processes writing the same tree. The `~timestamp~` temp name is lftp avoiding the first
  process's `.lftp` file — **the naming is the symptom, not the disease.** For a `pget` on a
  loose top-level file, two processes would both rename onto the same final name.
- `max_concurrent_transfers` is silently exceeded, and the scheduler's bandwidth allocation
  (§4.5) is computed against a job count that is wrong.
- Doubled bandwidth against the seedbox for no benefit.

**`core/autoqueue.py`'s eligibility query has the same omission**, at `:209-211`:

```sql
SELECT id, rel_path, is_dir FROM item WHERE queue_id = ?
AND instr(rel_path, '/') = 0 AND auto_queue_suppressed = 0
AND state IN (...)
```

Its module docstring claims "only a top-level item with **no active job** … is eligible", but
nothing in the query says so — it relies entirely on the *state* not being `QUEUED`/
`DOWNLOADING`. That indirection holds today, but it is a claim the code does not enforce, and
the docstring asserting otherwise is exactly how it stops holding later.

### What to build

- **Make `enqueue_item` refuse or de-duplicate.** DESIGN.md §4.7's "manual queue always wins"
  means it beats *suppression and the settle gate* — not that it spawns duplicates. Decide
  between returning the existing job's id (idempotent) and raising a clear error, and say why.
  Idempotent is probably kinder to a double-click.
- **Defend at the spawn layer too.** `core/queue.py`'s admission path should refuse to spawn a
  second process for an item that already has one running, regardless of how two job rows came
  to exist. A guard at only one layer is one refactor away from being no guard.
- **Make auto-queue's active-job exclusion explicit in its query**, so the docstring becomes
  true rather than aspirational.
- **Check every caller of `enqueue_item`** — the Files page's Queue and bulk Queue,
  Re-Download, and the Transfers page's Retry — and make sure the UI cannot offer an action
  that creates a duplicate. `FileTree.tsx`'s `rowAction` already returns `'stop'` for
  `QUEUED`/`DOWNLOADING`, so the Files row is probably safe; bulk actions and Retry need
  checking. **Fix the server regardless** — the UI not offering it is not a guarantee.
- **Reap the orphans this has already created.** The user has `~timestamp~` files on disk right
  now from duplicate processes; see part 1 below.

### Tests

- Two `enqueue_item` calls for one item produce **one** job, or the second is rejected —
  whichever you chose.
- The spawn layer refuses a second process for an item that already has one, even if two job
  rows are inserted directly.
- Auto-queue skips an item with an active job, asserted against the query rather than via
  state alone (insert an eligible-state item *with* a running job and confirm it is not
  picked up).
- An end-to-end check against the fake seedbox that only one lftp process exists per item.

## The remaining problems, downstream of that one

### 1. We do not recognise the name

`core/local_scan.py:22` defines `TEMP_FILE_SUFFIX = ".lftp"`, and line ~209 maps a temp file
to its final name only when the name **ends with** that suffix.
`foo.mkv.lftp~20260813154311~` ends with `~`, so it is not recognised as a temp file at all.
It therefore:

- appears as its own `LOCAL_ONLY` row in the Files tree (what the user saw);
- **counts toward its parent directory's `local_size` rollup** — and this is the dangerous one:
  an orphaned 4 GB temp file can push a directory to `local >= remote`, making it read
  `DOWNLOADED` while genuinely incomplete. That triggers post-processing, and on a `move` queue
  that means verify → **delete the remote copy** → extract. **Test this case explicitly.**
- is missed by the `.lftp` cleanup added in `21c41b0`, so deleting an item leaves it behind;
- is never reaped by anything.

`_local_size_for` (~line 128) also only probes `path.name + ".lftp"`, so resume accounting
misses these too.

### 2. Why does lftp create them at all — and is resume actually working?

`core/lftp.py:258-259` sets `xfer:use-temp-file yes` and `xfer:temp-file-name "*.lftp"`, and
nothing about clobber or auto-rename. lftp appears to pick a `~timestamp~` variant when the
plain `.lftp` name is already taken.

**But `-c` (continue) is supposed to resume *into* the existing temp file.** If lftp is instead
starting a fresh timestamped file on each attempt, then every retry re-downloads from zero —
which for a multi-gigabyte release is a serious, silent waste and would explain several large
orphans accumulating. The user has been interrupting transfers during testing, which is exactly
how the plain `.lftp` file gets orphaned in the first place.

**Determine this empirically before changing anything.** Do not reason about lftp's rules from
documentation alone — this project has been bitten repeatedly by lftp behaving differently from
its docs (`net:reconnect-interval-base` rejecting `5s`; a leading blank line corrupting
quote-stripping; `pget:save-status` defaulting to 10s; `mirror`'s target being the parent
directory). Reproduce it against the fake seedbox:

- Start a transfer, kill it partway, confirm what is left on disk.
- Start it again. Does lftp resume into the existing `.lftp` file, or create a
  `~timestamp~` one and start from zero? **Measure bytes transferred**, do not infer from
  file names.
- Try with an orphaned `.lftp` present but no live job.

Report what you find plainly. If resume *is* broken, that is a bigger finding than the naming
and should be called out as such.

## What to fix

1. **Recognise the variant** wherever `.lftp` is recognised today —
   `core/local_scan.py` (`TEMP_FILE_SUFFIX`, the scan loop, `_local_size_for`) and
   `core/local_delete.py`'s cleanup. Match a *pattern*, not a fixed suffix, and put the pattern
   in one place both modules import. Do not hardcode `~` handling in two files.
2. **Make sure such a file cannot inflate completeness.** A temp file's bytes should be
   attributed to the final name it is a temp file *for*, exactly as `.lftp` is today — never
   counted as an extra sibling. Include the "orphaned temp file makes a directory read
   `DOWNLOADED`" case as a test, since that is the path to an unwanted remote delete.
3. **Reap orphans.** An orphaned temp file whose final name already exists, or whose item has
   no active job, is dead weight. Decide where cleanup belongs — the existing `_FAILED_` sweep
   (`core/extract.py.sweep_failed_dirs`) is the nearest precedent — and whether it defaults on.
   Deletion defaults **off** in this project unless there is an explicit reason; a temp file
   lftp itself abandoned is a reasonable candidate for an exception, but argue it rather than
   assuming.
4. **Consider preventing the variant.** If the empirical work shows a setting
   (`xfer:clobber`, `xfer:auto-rename`, or similar) makes lftp reuse the plain `.lftp` name and
   resume properly, that is a better fix than accommodating the variant. **Verify any setting
   against the real lftp binary** — `tests/test_lftp_settings_accepted.py` exists precisely
   because asserting the rc file *contains* a string only proves what we wrote, not that lftp
   accepted it.

## Before you start

- `core/local_scan.py` in full — the temp-file and sidecar conventions, and `_local_size_for`.
- `core/lftp.py.build_rc_text` and `build_transfer_command`.
- `core/local_delete.py`'s `.lftp` handling (added `21c41b0`).
- `core/reconcile.py` — how `local_size` rolls up, and §3.2 rule 2.
- `prompts/startnewsession.md`'s traps list, particularly the sparse-file/sidecar entries and
  the several "lftp does not behave as documented" findings.

## Working tree check

`git status --porcelain`. If files you need are dirty, list them and ask.

## Tests

- A `~timestamp~` temp file does **not** appear as its own node.
- Its bytes are attributed to the final name, not counted as an extra file.
- **An orphaned temp file cannot make a directory read `DOWNLOADED` when it is not.** This is
  the one that protects against an unwanted `move`-mode remote delete.
- Deleting an item removes `~timestamp~` temps along with everything else.
- Whatever reaping you add, plus its guard conditions.
- If you change an lftp setting, feed it to the real binary the way
  `tests/test_lftp_settings_accepted.py` does.
- A real interrupted-then-resumed transfer against the fake seedbox, asserting on **bytes
  transferred** for the second attempt — this is the resume question, and it is the reason the
  task exists.

## Conventions to honor

- `docs/decisions.md`, newest at top — especially whatever you learn about lftp's actual
  behaviour, which is the durable part.
- `CHANGELOG.md`; `DESIGN.md` §4.4/§6 (standing approval to edit directly).
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build` if you touch the frontend; `uv run pytest` with the fake
  seedbox up (828 pass today).
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line message, **what you found about
   resume** (the headline), whether a setting prevents the variant, where you put reaping and
   its default, test count, lint results, and anything not fixed. Never `git add -A`, never push.
