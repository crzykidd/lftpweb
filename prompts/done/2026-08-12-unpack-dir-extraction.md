---
name: 2026-08-12-unpack-dir-extraction
status: done
created: 2026-08-12
model: sonnet            # coding task, no open design questions
completed: 2026-08-12
result: |
  Extraction now stages into a `_UNPACK_<name>` sibling directory and merges into the final
  directory only once every archive under the item has extracted cleanly (`core/extract.py`);
  failure renames staging to `_FAILED_<name>` and leaves it as evidence. `move_tree`
  (`core/postprocess.py`) grew a `merge=True` mode, reused for the merge-into-an-existing-
  directory case. `core/local_scan.py` now hides both prefixes at any depth, directories only,
  built on the just-landed mount-sentinel filter. Fixed a latent crash for the loose top-level
  file case (`root` being a file, not a directory) while building this.

  Tests: extended tests/test_postprocess.py's existing extract section (tests/test_extract.py
  named in the prompt doesn't exist -- extract.py's tests have lived there since phase 5),
  tests/test_local_scan.py, and tests/test_postprocess_e2e.py (new real-seedbox extraction
  e2e). Both ruff gates clean; full suite (386 tests) green with the fake seedbox up.

  Report-only findings confirmed real, not fixed here: (1) a rescan can silently revert
  EXTRACTING/EXTRACTED/VERIFYING/VERIFIED/CORRUPT/EXTRACT_FAILED back to a freshly-computed
  structural state -- core/engine.py._persist's protection covers only active-job/suppressed
  items; (2) move-mode's delete-before-extract ordering looks incidental, not deliberately
  reasoned -- DESIGN.md's numbered §6 pipeline never mentions the delete at all. Both flagged
  in docs/decisions.md and DESIGN.md §6 gaps (staging convention, delete/extract ordering) noted
  as undocumented, not corrected in-session per the working-tree constraint.
---

# Task: Extract into `_UNPACK_` instead of in place, then rename into position

Extraction is the only step in the post-processing pipeline that writes files under their
**final** names, incomplete, in a directory Sonarr/Radarr may be watching. Downloads are
already safe — `core/lftp.py` sets `xfer:use-temp-file yes` with `xfer:temp-file-name
"*.lftp"`, so in-flight files carry an extension the *arrs ignore. Extraction has no such
protection: unpack a large rar set into `/downloads/Some.Release/` and there is a growing,
importable-looking `.mkv` sitting there for the whole extraction.

Fix it with the convention the *arrs already skip: extract into a sibling `_UNPACK_<name>`
directory, then rename into position on success. The rename is same-filesystem and therefore
atomic — an *arr sees nothing, then sees a complete release, never an intermediate state.

## Before you start

- **Read `DESIGN.md` §6** (post-processing) and **§3.2** (the state rules) first.
- Read `core/postprocess.py`'s module docstring, `core/extract.py`'s module docstring, and
  `docs/decisions.md`'s phase 5 entry. The pipeline order today is verify → (move-mode remote
  delete) → extract → move-to-final; this task changes only the extract step's *destination*
  and adds a rename, not the order.
- `core/extract.py.extract_item` already takes `target_dir` (`None` = in place, which is what
  `_do_extract` passes today unless the site-wide `PostprocessSettings.extract_target_dir` is
  set). The plumbing you need mostly exists.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this plan
needs to modify. If any have uncommitted changes, list them and ask before touching them.
Surface unrelated dirty files once as awareness; don't block. This file is exempt.

**Known already-dirty, expected, do not revert:** `backend/lftpweb/core/local_scan.py` and
`tests/test_local_scan.py` carry a just-landed fix that filters the mount sentinel out of the
scan — you will be extending exactly that mechanism in step 4, so build on it rather than
around it. `frontend/vite.config.ts` and `docker-compose.test.yml` carry unrelated dev-env
fixes. `CHANGELOG.md`, `docs/decisions.md`, `standards.md`, `prompts/startnewsession.md`, and
`.claude/commands/release-prep.md` were dirty before this session began — leave them alone
apart from the `docs/decisions.md` entry this task requires.

## What to do

1. **Add the `_UNPACK_` staging directory to `core/extract.py`.** Extract into a sibling of
   the item's own directory (`<parent>/_UNPACK_<item-name>/`), not a child of it — a child
   would be inside the tree the reconciler walks and inside anything a later move relocates.
   Define the prefixes as module constants (`UNPACK_PREFIX = "_UNPACK_"`,
   `FAILED_PREFIX = "_FAILED_"`), not string literals at the call sites.

2. **Rename into position on success.** After every archive under the item extracts cleanly,
   move the `_UNPACK_` directory's contents into the item's real directory, then remove the
   now-empty staging dir. Reuse `core/postprocess.py.move_tree` rather than reimplementing
   the `EXDEV`/atomic-rename logic — it already handles the cross-device case correctly and
   is the one place that reasoning lives. Merging into an *existing* directory (the item dir
   already holds the rars) is the case to get right; `move_tree` may need a merge mode, and
   if so add it there with a test rather than open-coding a walk in `extract.py`.

3. **On failure, rename `_UNPACK_<name>` to `_FAILED_<name>` and leave it.** Do not delete
   it — it is the evidence for diagnosing a bad archive, and the prefix keeps the *arrs off
   it. The item still goes `EXTRACT_FAILED` with the existing error class and audit event;
   this only changes what is left on disk. If a `_FAILED_` dir from a previous attempt is in
   the way, replace it rather than erroring.

4. **Hide both prefixes from the scan, exactly like the mount sentinel.** `core/local_scan.py`
   just grew a filter for `.lftpweb-mount-ok` (already in the working tree — read it first and
   match its shape and comment style). `_UNPACK_`/`_FAILED_` directories are lftpweb's own
   bookkeeping and exist only locally, so left in the walk they reconcile to `LOCAL_ONLY`
   nodes and clutter the Files tree. Unlike the sentinel these can appear at **any** depth
   (an item can be nested), so match on the prefix anywhere, directories only.

5. **Default it on, no new setting.** Extracting in place has no upside worth a toggle, and
   the whole point is that the *arr-visible window closes for everyone. Do **not** add a
   per-queue column or a `PostprocessSettings` field. The existing site-wide
   `extract_target_dir` keeps working and keeps taking precedence when set — when it is,
   `_UNPACK_` staging still applies, inside that target.

6. **Tests.** Unit-test in `tests/test_extract.py` (extend it; match its `binary="7z"` /
   `LFTPWEB_7Z_BIN` convention so it runs against the dev host's real 7-Zip): a successful
   extraction leaves no `_UNPACK_` dir and the files in the item dir; a failed one leaves
   `_FAILED_<name>` and no `_UNPACK_`; nothing lands under a final name mid-extraction. Add
   the `scan_local` filter cases to `tests/test_local_scan.py`. Extend the phase 5 e2e
   (`tests/test_postprocess_e2e.py`) so the pipeline is exercised end to end, not just the
   unit seam.

## Questions to answer while you are in there — report, do not fix

- **Does a rescan walk an `EXTRACTED` item back to a structural state?** `core/engine.py._persist`
  only protects items with a `queued`/`running` job or `auto_queue_suppressed` from having
  `state` recomputed; `EXTRACTED` appears to be neither. If that regression is real, **report
  it, do not fix it** — it is its own task with its own state-machine reasoning, and quietly
  widening `protected` inside an extraction change is how §3.2 gets diverged from by accident.
- Extraction runs *after* the move-mode remote delete. Verification gates the delete so nothing
  unverified is lost, but a failed extract leaves no remote copy. Note whether the ordering
  looks deliberate in the phase 5 reasoning or incidental.

## Conventions to honor

- Comments explain **why**, matching the density and voice of the surrounding modules — this
  codebase's comments carry the non-obvious reasoning, not restatements of the code.
- Cite `DESIGN.md` sections as `§6` where a decision traces to one.
- Both Python gates must be clean: `uv run ruff format --check` **and** `uv run ruff check`.
  Run `format --check` explicitly — it has caught files `check` alone missed four times in
  this project now.
- Full suite before reporting: `uv run pytest` (bring the fake seedbox up with
  `docker compose -f docker-compose.test.yml up -d` for the e2e tests; note that compose file
  now bind-mounts `private_data/seedbox-dropbox` at `/data/dropbox` — do not disturb
  `/data/pickup`, several tests assert on its exact contents). Tear the containers down when
  finished, and leave any files you drop in the dropbox cleaned up.
- If the build reveals `DESIGN.md` §6 is wrong or underspecified about extraction, **say so in
  your report** — the doc gets corrected, it is not quietly diverged from.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record non-obvious decisions in `docs/decisions.md`, newest at top — especially the
   sibling-not-child choice, whatever you concluded about merging into an existing directory,
   and anything you rejected.
4. **Do not commit. Do not push.** The user is iterating locally and the working tree already
   carries unrelated in-progress changes. Prepare the tree, then report back to the
   orchestrating session with the file list and a proposed one-line commit message
   (`feat:`/`fix:` prefix, no `Co-authored-by:` trailer) for them to decide on later.
