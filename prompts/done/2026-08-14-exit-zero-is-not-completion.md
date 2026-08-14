---
name: 2026-08-14-exit-zero-is-not-completion
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: |
  Steps 1-4 fixed and tested (897 backend + 123 frontend tests green, both ruff gates and
  compose configs clean). Step 1's completeness check turned out to need to be
  exclusion-aware (item.remote_size is a raw rollup, not the completeness-relevant total) --
  found via the existing test_autoqueue_e2e.py file_exclude e2e test, fixed with
  TransferQueue._relevant_remote_total. Step 5 (bytes_start ~18GB anomaly) not reproduced;
  recorded in prompts/open-issues.md with the leading hypothesis. Proposed DESIGN.md §4.3
  wording drafted in docs/decisions.md, not applied -- awaiting user approval.
---

# Task: Stop treating lftp's exit 0 as proof a transfer completed, and keep the evidence when it lies

`core/queue.py._reap_one` treats lftp's exit 0 as proof that every byte arrived. It isn't. On a
live instance a job exited 0 having left one file **500 MB short** as a `.lftp` temp file, and
the item was marked `DOWNLOADED` and handed to post-processing anyway. Fix the false assumption,
stop destroying the lftp output that would have explained it, and make a running-then-completed
transfer visible on the Transfers page instead of vanishing.

## The evidence (2026-08-13/14, production-test, queue `ar-tv`, `sync_mode = move`)

Real timeline, reconstructed from the live instance — cite it in tests and comments:

| Time (UTC) | What |
|---|---|
| 03:59:44 | `testfolder10` first observed on the remote (both dirs were empty beforehand) |
| 04:05:19 | Remote settle fingerprint reaches its final `(22 files, 38841560420 bytes)` and holds |
| 04:06:21 | Auto-queue queues it; **job 43** spawns (`bytes_start = 18002714542` — see defect 5) |
| 04:13:30 | **job 43 exits 0**, logged `job 43 succeeded: testfolder10 (38841560420 bytes)`, leaving `S.W.A.T.S06E22….mkv.lftp` + its `.lftp-pget-status` sidecar on disk |
| 04:16:55 | Post-processing runs: `verify` → **CORRUPT**, "only 38340697110 of 38841560420 expected bytes"; `remote_delete_withheld` |
| 04:17:00–04:17:55 | **job 44** re-queued by auto-queue, finishes E22 in 55s |
| 04:21:11 | `verify` → VERIFIED (22 files, byte totals match); `remote_delete` proceeds correctly |

**The settle gate was NOT at fault** — the remote reached its final size a full minute before
job 43 spawned. Do not "fix" the settle gate as part of this task.

## Before you start

- Read `DESIGN.md` §1.3, §4.3, §4.4, §6, §7.3 and `CLAUDE.md`. Read `prompts/open-issues.md`.
- **`DESIGN.md` §4.3's "no inference" rule is what created this bug.** It says to trust lftp's
  exit code rather than infer state. That rule is right about not *guessing* progress; it is
  wrong to conclude that exit 0 proves every byte arrived. Surface this as a proposed §4.3
  wording change — do not silently diverge from the doc (see "Design change" below).
- Do not reintroduce `jobs -v` parsing (§1.2). The completeness check here is a **filesystem**
  check, which is exactly what §1.3 already mandates as the source of truth.
- `core/local_scan.py` already knows how to measure an item's on-disk bytes including the
  `.lftp` temp suffix and `.lftp-pget-status` sidecar math. **Reuse it. Do not reimplement.**

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this plan
needs to modify. If any have uncommitted changes, list them and ask the user before touching
them. Surface unrelated dirty files once as awareness; don't block. Known at authoring time:
`docker-compose.yml`, `prompts/open-issues.md`, `prompts/startnewsession.md` are modified and
are **unrelated** to this task. This prompt file itself is exempt.

## What to do

### 1. Exit 0 must not mean DOWNLOADED (`core/queue.py._reap_one`) — the core fix

`_reap_one`'s success branch (around line 646) currently comments: *"`cmd:fail-exit true` makes
exit 0 mean the whole transfer succeeded, so the item is DOWNLOADED now, not 'probably.'"*
That is false and the comment must go.

On exit 0, **before** taking the `DOWNLOADED` branch, verify completeness on disk:

- No lftp temp file remains anywhere under the item. **A temp file is not just `*.lftp`** —
  lftp falls back to `<final>.lftp~<timestamp>~` when the plain name already exists, and
  `core/local_scan.py` already handles both via `TEMP_FILE_RE` / `is_temp_name()` /
  `find_temp_variants()`. Use those helpers; a check that only matches `*.lftp` misses the
  variant and reintroduces this bug for exactly the retry case that is most likely to hit it.
  A leftover `.lftp-pget-status` sidecar counts too.
- Local bytes for the item are `>=` the known remote total (`item.remote_size`). Measure with
  `core/local_scan.py`'s existing `effective_file_size` / `scan_local` sidecar math.

If either fails, treat the job as **incomplete**, not successful:

- Do **not** set `DOWNLOADED`. Do **not** trigger post-processing.
- Set the item to `PARTIAL` so auto-queue's existing eligibility picks it up again (job 44
  proves the re-queue path already works and finishes the job).
- Write an `audit.record_event` row — new kind `incomplete_on_exit_zero`, level `warning` —
  naming the expected vs. actual byte counts and the leftover temp files. This is the row that
  would have explained the whole incident at a glance.
- Preserve `output_tail` for this case (see step 2).

Keep this distinct from the settle completion gate that already exists in the same branch —
they answer different questions ("has the remote stopped changing?" vs. "did we actually get
it all?") and both must apply.

### 2. Stop destroying the evidence (`core/queue.py`, ~line 622)

The success path currently writes `output_tail = NULL`. On the one job whose success was in
doubt, lftp's own account had already been captured and was then deliberately thrown away;
both jobs show `has_output_tail: false`.

Retain `output_tail` when the completeness check in step 1 fails. Retaining it for *every*
success is the simplest correct option — 4 KB per job (`lftp.OUTPUT_TAIL_BYTES`) against a
`job` table that History already paginates — but if you keep the null-on-clean-success
behaviour, the incomplete case **must** keep it. Justify whichever you choose in
`docs/decisions.md`.

### 3. A completed transfer must be visible (`core/queue.py.list_jobs`)

`list_jobs()` selects only `queued`/`running` plus the most recent `failed`/`cancelled`, so a
`succeeded` job never appears on Transfers. Seven minutes of real transfer looked, from the UI,
like nothing running and 0 B/s in the header — which is why this took a long live debugging
session to even characterise.

Surface recently-succeeded jobs on the Transfers page, bounded so the row set stays bounded by
construction (that boundedness is why `JobOut` can inline `output_tail` at all — see
`api/history.py`'s docstring for the contrast). Suggested: the item's most recent `succeeded`
job, dismissible via the existing `dismissed_at` mechanism, exactly like terminal jobs today.
Match `HistoryJobsSection`'s existing visual treatment rather than inventing a new one.

### 4. `bytes_done` must not exceed `bytes_total`

The live API returned `bytes_total: 31812118603` with `bytes_done: 38841560420` for both jobs —
the two fields use different denominators, so any progress bar renders past 100%. Note the DB
column `job.bytes_total` was `NULL` for these rows while the API response carried a computed
value; find where that number is derived and make the pair consistent. Add a regression test.

### 5. Investigate `bytes_start` on a clean directory

Job 43 spawned with `bytes_start = 18002714542` against a local directory the user confirms was
**empty**. It is written once at spawn from `item["local_size"] or 0` (`core/queue.py:1278` and
:1305), so `item.local_size` held ~18 GB for an empty directory.

This is a genuine, unexplained defect and it corrupts throughput reporting: `core/metrics.py:179`
computes `contribution = max(job.bytes_done - job.bytes_start, 0)`, so this transfer was
under-counted by 18 GB on the Dashboard.

Find where `item.local_size` is written for a top-level directory (`core/engine.py._persist` /
`core/reconcile.py`) and determine whether it can hold a remote rollup, a stale value, or a
mis-scoped sum. **If you cannot reproduce it, say so plainly and leave it open** — record the
finding in `prompts/open-issues.md` rather than inventing a fix for a mechanism you could not
demonstrate. Do not let this block steps 1–4.

## Design change to surface, not to apply silently

`DESIGN.md` §4.3's "no inference" rule needs a clause: exit 0 means *lftp reported no error*,
not *every byte arrived*; completion is confirmed from the filesystem (§1.3's own principle),
and an item may only reach `DOWNLOADED` once that check passes. Draft the exact wording, put it
in `docs/decisions.md`, and **ask the user before editing `DESIGN.md`** — this repo corrects the
doc rather than diverging from it, but the user approves the wording.

## Testing

- A regression test for step 1 that builds the real shape: remote total N bytes, local tree
  short by one file that is present only as `<name>.lftp`, process exits 0 → assert the item
  does **not** reach `DOWNLOADED`, post-processing is **not** triggered, the event row is
  written, and `output_tail` survives.
- A test asserting a `move`-mode item in that state never reaches the delete gate.
- Tests for steps 3 and 4. Frontend tests go in the Vitest suite added in `129cfcf`.
- Run `uv run pytest` with the fake seedbox up (`docker-compose.test.yml`, run
  `docker/test-seedbox/gen_key.sh` first), both lint gates (`ruff check` **and**
  `ruff format --check` — `check` alone has missed files three times in this project's history),
  `npm run lint`, `npm test`, `npm run build`, and `docker compose config --quiet` on all three
  compose files. Tear down the seedbox containers afterward and confirm via `docker ps -a`.

## Conventions to honor

- Doc updates ship in the same commit as the code. Update `README.md`'s "Known gaps" and
  `prompts/open-issues.md` (close what this fixes; the `docker-compose.yml` `:latest` item is
  already closed and unrelated).
- Non-obvious decisions go in `docs/decisions.md`, newest at top, with rejected alternatives.
- `CHANGELOG.md` gets an entry — this changes behaviour an existing install will notice
  (items that used to reach `DOWNLOADED` on a short transfer now go `PARTIAL` and re-queue).
- **You cannot see the UI.** No browser exists in this environment. Any claim about step 3 means
  "builds, type-checks, lints, and its endpoints answered over HTTP" — never "renders correctly."

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record non-obvious decisions in `docs/decisions.md`.
4. Prepare ONE commit covering this prompt file, the modified files, and the prompt move.
   **Do not commit** — report the file list and a proposed `fix:` one-liner back to the
   orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push. Do not
   stage the three unrelated dirty files listed above.
