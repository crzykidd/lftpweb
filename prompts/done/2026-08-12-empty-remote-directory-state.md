---
name: 2026-08-12-empty-remote-directory-state
status: completed          # pending | completed | failed
created: 2026-08-12
model: sonnet            # coding; the product decision is already made (below)
completed: 2026-08-12
result: >
  Fixed. Added `remote_file_totals` rollup in `core/reconcile.py` to distinguish "no remote
  files at all" from "remote files exist but all excluded" within the `relevant == 0` branch.
  A genuinely empty remote directory with no local copy now reads REMOTE_ONLY; once mirrored,
  DOWNLOADED. The all-excluded case is unchanged (still DOWNLOADED), regression-guarded by a
  new test. Nested empty directories follow the same rule at every depth, no special case.
  4 tests added/rewritten in tests/test_reconcile.py (15 -> 18). ruff format/check clean;
  full pytest 461 passed, no regressions. Decision recorded in docs/decisions.md. Not
  committed/pushed per instructions — tree left prepared for the orchestrating session.
---

# Task: An empty remote directory must not read as `DOWNLOADED`

An empty directory on the seedbox shows up in the Files view as `DOWNLOADED` even though it
does not exist locally at all. Reported by the user against the running dev instance.

`core/reconcile.py` (~line 173):

```python
if relevant == 0:
    # Rule 1's vacuous case ... §4.7: "vacuously DOWNLOADED", not PARTIAL.
    state = STATE_DOWNLOADED
```

`relevant` counts remote *files* under the directory that pass the completeness predicate. For
an empty directory it is 0, so `complete == relevant` holds vacuously and the directory reads
`DOWNLOADED` no matter what exists locally.

**The decision, already made by the user — implement it, do not re-litigate:** an empty remote
directory that has not been copied down reads **`REMOTE_ONLY`**. Once it exists locally it reads
`DOWNLOADED` (an empty directory that has been mirrored *is* complete). No new state is being
introduced.

## The trap — read this before touching the branch

That `relevant == 0` branch deliberately serves **two** cases that the code currently cannot
tell apart:

1. **Every child is excluded** by a `file_exclude` pattern. `DOWNLOADED` is **correct** here and
   must stay. This is the §4.7 / §3.2-rule-8 behaviour that stops a filtered release sitting
   `PARTIAL` and being auto-queued forever — one of the project's named traps
   (`prompts/startnewsession.md`, `core/patterns.py`, `docs/decisions.md` phase 4). Breaking it
   reintroduces an infinite re-queue loop.
2. **The directory is genuinely empty** on the remote. This is the bug.

Both produce `relevant_totals == 0` today, because `relevant_own` is 0 for excluded files *and*
there are no files at all in the empty case. **A fix based only on local presence will flip case
1 as well and resurrect the re-queue loop.** You need to distinguish "no remote files under here
at all" from "remote files exist but none of them count" — e.g. a second rollup over
`remote_tree` counting remote files *before* the predicate is applied, alongside the existing
`relevant_totals`/`complete_totals`/`local_present_totals`. Follow the existing `_rollup` idiom
rather than inventing a parallel mechanism.

## Before you start

- Read `DESIGN.md` §3.2 (rule 1 and rule 8) and §4.7. Note that §3.2 rule 1 is **silent** on the
  empty-remote-directory case — phase 2 already recorded that it is also silent on a directory
  with zero local presence, resolved then as `REMOTE_ONLY`; this task follows that precedent.
- Read `core/reconcile.py` in full — it is a pure function and small. Pay attention to how
  `relevant_own` / `complete_own` / `local_present_own` are built and rolled up, and to the
  existing `LOCAL_ONLY` branch above the one you are changing.
- Read `docs/decisions.md`'s phase 2 and phase 4 entries.

## Working tree check

Run `git status --porcelain` first. The tree is dirty on purpose from an in-flight local session:
dev-environment fixes (`docker/Dockerfile`, `docker-compose*.yml`, `frontend/vite.config.ts`),
the `_UNPACK_` extraction change, a Settings → Transfer tab change, and very likely a
post-processing state-persistence change touching `core/engine.py` and `core/mount_sentinel.py`.
**None of them are yours — do not revert, refactor, or tidy them.** `CHANGELOG.md`,
`standards.md`, `prompts/startnewsession.md`, `.claude/commands/release-prep.md` were dirty
before the session; leave them alone. Append to `docs/decisions.md` at the top without
disturbing existing entries. If a file you need to modify is dirty, list it and ask first.

## What to do

1. Distinguish the two cases in `core/reconcile.py` as described above, and make the genuinely
   empty remote directory with no local copy read `REMOTE_ONLY`. Everything else in that branch
   keeps its current behaviour.
2. Decide and document what a **nested** empty directory does — a remote directory containing
   only other empty directories, and an empty directory that *does* exist locally. State the
   rule you implemented in your report; it should fall out of the same predicate, not need a
   special case.
3. Keep the comment discipline of the surrounding code: the existing comment on that branch
   explains *why* it was vacuously `DOWNLOADED`. Replace it with one that explains why the two
   cases are now distinguished, citing §4.7 and §3.2, so the next reader doesn't "simplify" it
   back into one branch.
4. **Tests, in `tests/test_reconcile.py`** (15 tests today — match their style):
   - empty remote directory, no local copy → `REMOTE_ONLY`
   - empty remote directory that exists locally → `DOWNLOADED`
   - directory whose children are **all excluded**, no local copy → still `DOWNLOADED`
     (this is the regression guard for the re-queue loop — label it as such in the test name or
     docstring so nobody deletes it as redundant)
   - nested empty directories, whatever rule you chose in step 2
5. Check whether anything downstream assumed the old behaviour — in particular
   `core/autoqueue.py` (a directory flipping to `REMOTE_ONLY` becomes eligible for auto-queue)
   and `core/reconcile.py`'s own rollups. Report what you found. An empty directory becoming
   auto-queueable is **expected and acceptable**: lftp creates it locally, the next scan reads
   `DOWNLOADED`, and it converges. Flag it, don't design around it.

## Conventions to honor

- Comments explain **why**, matching the surrounding density and voice. Cite `DESIGN.md`
  sections where a decision traces to one.
- `uv run ruff format --check` **and** `uv run ruff check` (run the format check explicitly — it
  has caught files `check` alone missed four times in this project), plus the full `uv run pytest`.
- If `DESIGN.md` §3.2 should say something about this case — it currently says nothing —
  **propose the wording in your report**; do not edit `DESIGN.md` yourself.
- The dev stack and fake seedbox are **running and in use by the user**. Leave every container
  running; do not disturb `/data/pickup`. `docker compose -f docker-compose.dev.yml restart backend`
  picks up backend changes. The fake seedbox's fixture tree contains a `chmod 000` directory
  (`no-permission`) — useful nearby, but not what this task is about.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record the decision in `docs/decisions.md`, newest at top, including the two-cases distinction
   and why a local-presence-only fix was rejected.
4. **Do not commit. Do not push.** Prepare the tree, then report back to the orchestrating
   session with the file list and a proposed one-line commit message (`fix:` prefix, no
   `Co-authored-by:` trailer).
