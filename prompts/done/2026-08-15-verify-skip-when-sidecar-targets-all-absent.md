---
name: 2026-08-15-verify-skip-when-sidecar-targets-all-absent
status: done
created: 2026-08-15
model: sonnet
completed: 2026-08-15
result: >
  Implemented in core/verify.py: every sidecar-referenced file absent + other non-archive
  content present -> SKIPPED; any referenced file present (including mixed/half-deleted) or
  sidecar-and-nothing-else -> unchanged, stays CORRUPT. Deviation found during implementation
  (recorded in docs/decisions.md): "at least one non-sidecar content file exists," taken
  literally, also matched the 2026-08-14 renamed-sidecar regression test (real archive volumes
  present under a different name than the sidecar's one entry), which would have let
  archive_cleanup discard unverified archives. Fixed by excluding archive-shaped files from the
  content check via a new core/extract.py.is_archive_member() helper. All gates green: ruff
  check/format clean, backend pytest 1142 passed 0 failed, frontend lint/test/build all green
  (untouched, re-verified).
---

# Task: verification reads an upstream-extracted release as SKIPPED, not CORRUPT

Live-testing fix (user-approved rule, 2026-08-15). A release that was rar'd at origin and
extracted **upstream** (the seedbox's SABnzbd unpacks, deletes the rars, keeps the `.sfv`)
arrives locally as `movie.mkv + .sfv`. `core/verify.py` counts every sidecar-referenced
file as "missing" → `CORRUPT`, which is false — and under a `move` queue it permanently
withholds the remote delete and wedges a perfectly good item. Live case:
`National.Lampoons.Animal.House.1978.iNTERNAL.1080p.BluRay.x264-EwDp` on the ar-movies
queue.

## The approved rule — narrow on purpose

Verification is the only gate that runs **before** the irreversible remote delete
(pipeline order: verify → delete gate → extract → move), so only the provably-benign case
relaxes:

1. **Every sidecar-referenced file is absent AND at least one non-sidecar content file
   exists** → `SKIPPED`, detail along the lines of
   `"sidecar references only absent files -- archives likely extracted upstream; nothing verifiable"`.
   Rationale: zero files were verified, so `SKIPPED` (not `VERIFIED`) is the honest state,
   and it puts the release at exactly the trust level of a sidecar-less release, which is
   already `SKIPPED` today. `SKIPPED` permits the move-mode delete per the existing
   "verification must not have *failed*" rule — that is intended.
2. **Any referenced file present** → unchanged: verify the present ones, any mismatch is
   `CORRUPT`, and missing-plus-present (a half-deleted archive set) **stays `CORRUPT`** —
   by the time extraction would notice a missing volume, the remote copy is already
   deleted. Do not relax this; the pipeline-ordering question is open issue #2 (G1) and
   explicitly out of scope here.
3. Degenerate case — the item contains a sidecar and **nothing else** → stays `CORRUPT`
   (there is no content the sidecar could have been vouching for; something is wrong).

Completeness context worth encoding in a comment: post-processing only runs after the
local-vs-remote completeness gate, so "missing vs the sfv" at verify time always means
the **remote** lacked those files too — it is an upstream-anomaly signal, never a
partial-transfer signal.

## Before you start

- Read `backend/lftpweb/core/verify.py` fully — its docstrings carry two prior
  false-`CORRUPT` incidents and the sidecar-search asymmetry; extend that narrative, don't
  contradict it. Read `core/postprocess.py`'s delete-gate ordering enough to confirm the
  claim above. Read the `.sfv` and `.md5` paths both — the rule applies to each.
- `docs/arr-integration-spec.md` is background only; this fix is verification, not *arr.

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt.

## What to do

1. Implement the rule in `core/verify.py`, for both sidecar formats, keeping the
   mismatch/missing accounting in the detail strings honest (say how many entries were
   absent and that nothing was checked).
2. Tests in the existing verify test module: the Animal House shape (sfv listing N rars,
   none present, one mkv present → `SKIPPED` with the upstream-extraction detail); mixed
   presence (some rars present and passing, some absent → `CORRUPT`); present-and-corrupt
   still `CORRUPT`; sidecar-only directory → `CORRUPT`; and an md5-flavored twin of the
   headline case. Confirm the existing e2e postprocess tests still pass unchanged — the
   rule must not alter any case where referenced files exist.
3. Docs, same commit: `docs/decisions.md` entry (newest at top) with the pipeline-ordering
   rationale and the pointer to open issue #2 for the broader reordering question;
   `CHANGELOG.md` under Unreleased/Fixed; a traps-list line in
   `prompts/startnewsession.md` ("missing-vs-sfv at verify time means the remote lacked it
   too") and a row in the arr build-run table (this fix rode the same live-test session).

## Conventions to honor

- `fix:` prefix. No behavior change outside the all-absent case.
- The two existing anchored-regex security-control patterns and the real-RAR fixtures are
  untouchable (see startnewsession.md's standing rules).

## Verification gates — run each separately and read its exit code

1. `uv run ruff check backend`
2. `uv run ruff format --check backend`
3. `uv run pytest` — note skip counts honestly.
4. `cd frontend && npm run lint && npm test && npm run build` (untouched; prove it).

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `fix:` message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
