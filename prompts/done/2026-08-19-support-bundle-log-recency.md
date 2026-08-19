---
name: 2026-08-19-support-bundle-log-recency
status: completed          # pending | completed | failed
created: 2026-08-19
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-19
result: Confirmed the cause (filename/rotation-suffix sort interleaves series arbitrarily; arrclient already passed lastWriteTime through unused) and re-sorted the *arr log fetch by lastWriteTime descending across every series, unknown-timestamp files last; TRUNCATED.txt now shows a timestamp per file, fetched and skipped alike.
---

# Task: the support bundle's *arr log fetch must spend its budget on the newest files, not the biggest stale ones

The support bundle fetches each selected Sonarr/Radarr instance's own log files "newest-first, up
to a per-instance size cap" (20 MB). In a real production bundle that ordering demonstrably failed
and cost a live diagnosis its decisive evidence. Fix the ordering so the budget buys the most
recent logs.

## The evidence this task exists for

Bundle `private_data/debug_logs/lftpweb-support-0.2.6-20260819T205145Z.zip`, taken 2026-08-19 to
diagnose two `REMOTE_GONE` jobs from that same afternoon. Its `arr-Sonarr/TRUNCATED.txt` reads:

> 6 of 12 log file(s) not fetched: the ~20 MB per-instance budget was exhausted after 6 file(s)
> were attempted. Skipped, oldest first: sonarr.1.txt, sonarr.trace.1.txt, sonarr.2.txt,
> sonarr.trace.2.txt, sonarr.3.txt, sonarr.4.txt

What the ~20 MB actually bought — first line of each fetched file:

| File | Size | Newest entry it contains |
|---|---|---|
| `sonarr.txt` | 7.6 MB | 2026-08-19 (current — correct) |
| `sonarr.0.txt` | 3.7 MB | 2026-08-19 (correct) |
| `sonarr.debug.txt` | 5.5 MB | **2026-08-10** |
| `sonarr.trace.0.txt` | 10.4 MB | **2026-08-10** |
| `sonarr.trace.txt` | 4.1 MB | **2026-08-10** |

Three files totalling ~20 MB, every entry **nine days stale**, from a debug/trace session that had
since been switched off — and they consumed the budget ahead of files that covered the incident.
The investigation could not confirm what Sonarr did during the 18:15–18:17Z window because the
logs covering it were among the six dropped.

**The likely cause** (verify it — do not assume): Sonarr exposes several independent rotation
*series* (`sonarr.*`, `sonarr.debug.*`, `sonarr.trace.*`). A "newest-first" sort by filename or by
rotation index orders correctly *within* a series but interleaves them arbitrarily, so a dormant
series whose newest file is ancient still sorts near the front. The `lastWriteTime` the Sonarr API
returns per log file is the field that actually answers "how recent is this."

## Before you start

- `backend/lftpweb/core/supportbundle.py` — the bundle builder; the *arr log fetch and its budget
  accounting live here.
- `backend/lftpweb/core/arrclient.py` — `log_files()` / `download_log_file()`. Check what fields
  `log_files()` actually returns today and what the *arr API offers (`lastWriteTime`,
  `filename`, `id`). If the client currently discards the timestamp, that is part of the fix.
- `docs/concepts.md`'s "What's in a support bundle" section — user-facing description of this
  behavior; update it if the described ordering changes.
- `backend/tests/test_support_bundle_api.py` and `backend/tests/fake_arr.py` — the existing
  coverage and the fake *arr's log endpoints, which you will need to extend.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this plan
needs to modify. If any have uncommitted changes, list them and ask the user before touching them.
Surface unrelated dirty files once; don't block. This prompt file is exempt.

**Note:** another task may be in flight on `frontend/` and on `backend/lftpweb/api/jobs.py` /
`models.py` / `core/queue.py`. This task should not need any of those files. If you find yourself
wanting to edit one, stop and ask.

## What to do

1. **Confirm the cause before fixing it.** Read `arrclient.log_files()` and the current sort in
   `supportbundle.py`. Establish what the fetch is actually ordering by today. Write down what you
   found — if it turns out to already sort by timestamp and the real cause is something else, say
   so and fix *that* instead. Do not fix the hypothesis.
2. **Sort candidate log files by last-modified, descending, across every series** before spending
   the budget. Files the *arr reports without a usable timestamp sort last rather than first — an
   unknown age must never outrank a known-recent file.
3. **Keep the budget itself unchanged** (20 MB per instance). This task is about what the budget
   buys, not how big it is. If you believe the cap needs raising, say so in your report and leave
   it alone.
4. **Make `TRUNCATED.txt` genuinely informative.** It currently lists skipped filenames "oldest
   first," which was misleading here — the skipped files were *newer* than three that were
   fetched. Include each file's last-modified timestamp for both the fetched and the skipped set,
   so a future reader can tell at a glance whether the budget was well spent.
5. **Tests.** Extend `fake_arr.py` so its log-file listing can return several series with
   controllable `lastWriteTime`s, including the exact shape this bundle hit: a large, stale
   `trace` series alongside a small, current main series. Then assert in
   `test_support_bundle_api.py` that the current files are fetched and the stale ones are the
   ones dropped — a test that fails against today's code. Also cover the missing-timestamp
   case.
6. **Docs.** Update `docs/concepts.md`'s support-bundle section if it describes the ordering.
   Add a `CHANGELOG.md` entry under `[Unreleased]`. Record the decision in `docs/decisions.md`
   (newest at top) — specifically *why* ordering by recency across series matters, with the
   evidence above, so this isn't re-derived later.

## Conventions to honor

- **Run the gates in the FOREGROUND with a generous timeout and read each exit code.** From the
  **repo root** (not `backend/` — `testpaths` lives in the root `pyproject.toml` and `tests/` is a
  sibling of `backend/`; running from `backend/` collects zero tests and looks like a pass):
  `uv run pytest`, `uv run ruff check`, `uv run ruff format --check` — three separate gates;
  `ruff check` passing is not `ruff format --check` passing. Do not background the test run; a
  subagent never receives the completion notification and will stall forever.
- Report backend test counts before and after, and confirm 0 skipped.
- Conventional-Commit prefix (`fix:`). No `Co-authored-by:` trailer.
- Doc updates ship in the same commit as the code they describe.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (or `prompts/failed/`).
3. Record non-obvious decisions in `docs/decisions.md`.
4. **You are a spawned agent: do NOT commit.** Prepare the tree, then report the file list and a
   proposed one-line commit message back to the orchestrating session. Never `git add -A`, never
   push.
