---
name: 2026-08-17-interrupted-job-popout-explains-itself
status: completed
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: Backend writes an explanation into INTERRUPTED jobs' output_tail; frontend's failed-row panel renders a static empty state (via a new pure failedJobPanelContent helper) instead of staying blank when has_output_tail is false. All six gates green.
---

# Task: An INTERRUPTED job's History popout explains itself instead of rendering blank

Live find on the test instance (2026-08-17, Fresh.Off.the.Boat.S04E16, jobs 265/266): a
container restart at 18:21:49Z cut a running 2.4 GB mirror; startup recovery marked the
job `failed / INTERRUPTED`, a fresh job resumed two seconds later and completed, and the
whole *arr ladder ran clean. The History page told that story badly in two ways this
task fixes — deliberately **without** any attempt-grouping, "resumed" annotation, state
reclassification, or count changes (considered with the user and rejected as
disproportionate; the rows stay exactly as they are, they just explain themselves when
expanded).

1. **A failed job with no captured output expands to a completely blank panel.**
   `HistoryJobsSection.tsx`'s expand handler only fetches output when
   `job.has_output_tail` is true, and the panel's "No output was captured for this
   job." message only renders once `output` is non-null — i.e. after a fetch that never
   happens in exactly this case. The explanatory empty state is unreachable for the one
   failure class that most needs it.
2. **An INTERRUPTED job records no reason at all.** `core/queue.py`'s startup recovery
   (`_recover_orphaned_jobs` or whatever the `state = 'running'` sweep method is named
   — find it by the `"clearing %d job(s) left 'running' by a previous run"` log line,
   ~line 334) sets `state='failed', error_class='INTERRUPTED', finished_at=now` and
   nothing else, so even a fixed popout would have nothing to say about *why*.

## Before you start

- Read `CLAUDE.md`; skim `DESIGN.md` §9.2 (History) and §4.6 (stop/interrupt
  semantics — partial bytes are kept, `-c` resumes from them; the docstring right above
  the recovery UPDATE says this in the code's own words — reuse its framing).
- Read before editing:
  - `backend/lftpweb/core/queue.py` — the startup-recovery sweep (~lines 325–355) and
    how `output_tail` is written elsewhere in the module (the reap path), so the new
    write matches the existing column usage and any length conventions.
  - `frontend/src/components/HistoryJobsSection.tsx` — the `handleToggle` fetch guard
    (~line 107) and the `expanded && job.state === 'failed'` panel (~lines 182–195).
  - `backend/lftpweb/api/history.py` — `GET /api/history/jobs/{id}/output` and
    `has_output_tail`, to confirm the new backend-written tail flows through the
    existing endpoint with zero API changes (it should — it's just the column).
- The **coordination note**: another task from this same session
  (`prompts/2026-08-17-bulk-delete-per-entry-scopes.md`, Files-page bulk delete) may
  have landed just before this one. Its files don't overlap yours except `CHANGELOG.md`
  and `docs/decisions.md` — append alongside its entries, don't disturb them.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files
this plan needs to modify. If any of those files have uncommitted changes, list them
and ask the user before touching them. Surface unrelated dirty files once as
awareness; don't block. This file (the handoff prompt itself) is exempt — it's
expected to be modified by "When done" below.

## What to do

1. **Backend — startup recovery writes the reason as the job's `output_tail`.** In the
   same UPDATE (or an adjacent one in the same transaction) that marks orphaned jobs
   `INTERRUPTED`, set `output_tail` to a short, human-facing explanation along the
   lines of: "Transfer interrupted by an application restart or crash — the process
   did not exit on its own. Partial bytes on disk are retained; the next attempt for
   this item resumes from them." Only write it when `output_tail` is NULL/empty —
   never overwrite genuinely captured lftp output (a job can conceivably have output
   from before the restart… in practice the tail is only written at reap, but guard it
   anyway; state the guard's reason in a comment). This flows through the existing
   `has_output_tail` flag and output endpoint untouched.
2. **Frontend — the failed-job expand panel earns a real empty state.** When a failed
   row expands and `job.has_output_tail` is false, render the error class plus
   "No output was captured for this job." (the existing wording, now actually
   reachable) without fetching. Keep the fetch path for `has_output_tail: true` rows
   byte-for-byte as it is. Note: after fix 1, *future* interruptions will have a tail
   and take the fetch path — this empty state still matters for every pre-fix
   interrupted row already in users' databases (like the live one that prompted this)
   and any other tail-less failure.
3. **Tests:**
   - Backend: extend the existing startup-recovery coverage (find it —
     `tests/test_queue*.py` or wherever the orphaned-job sweep is pinned) to assert
     the interrupted job's `output_tail` is set to the explanation, `has_output_tail`
     reads true through `api/history.py`'s list, and a job that somehow already has a
     tail keeps it.
   - Frontend: if the empty-state branch is reasonably testable as a pure decision
     (e.g. factor a tiny helper into `lib/` deciding fetch-vs-static-empty-state),
     cover it in Vitest; if it would take rendering the component, match how the
     existing `HistoryJobsSection` behavior is (or isn't) tested and don't build new
     test infrastructure for it — say which way you went.
4. **Docs, same commit:**
   - `CHANGELOG.md` — `### Fixed` entries under Unreleased (create the section only if
     absent; append after any entries already there): expanding a failed transfer with
     no captured output now explains itself instead of showing an empty panel, and a
     transfer interrupted by a restart now records why it failed and that the next
     attempt resumes from the partial bytes.
   - `docs/decisions.md` — one entry (2026-08-17, newest at top): the
     considered-and-rejected alternatives (attempt grouping under the succeeding row —
     pagination/filtering make the pairing unreliable; a "resumed by job N"
     server-side annotation — disproportionate machinery for a cosmetic win; both per
     the user), and why the fix is "the row explains itself" rather than any
     reclassification.

## Conventions to honor

- Gates, each run separately, exit codes read: backend `uv run ruff check`,
  `uv run ruff format --check`, `uv run pytest`; frontend `npm run lint`, `npm test`,
  `npm run build`. All green before handing off.
- Comment style: dated, incident-naming, constraint-stating — match the file you're in.
- No browser here — UI claims are "builds, lints, tests green", never "renders
  correctly"; the changed panel ships unviewed and say so.
- Conventional-Commit prefix `fix:`; no `Co-authored-by:` trailers.

## When done

1. Update this file's frontmatter: set `status` (completed/failed), `completed` (the
   date), and `result` (one line).
2. `git mv` this file into `prompts/done/` (on success) or `prompts/failed/` (on
   failure).
3. Record the decisions entry (step 4 above) in `docs/decisions.md`.
4. Hand off ONE commit covering this prompt file, the files this session modified, and
   the prompt move (the prompt is **not** pre-committed — it bundles in here). Present
   the file list and a one-line message summarising the changes.
   - **You are a spawned agent:** do **not** commit. Prepare the working tree, then
     report the file list + proposed message back to the orchestrating session, which
     surfaces the `y/n` to the user.
   Never `git add -A`, never push, never auto-commit. Branch is `dev`.
