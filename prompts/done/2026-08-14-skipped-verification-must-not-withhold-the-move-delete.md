---
name: 2026-08-14-skipped-verification-must-not-withhold-the-move-delete
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: >
  core/postprocess.py._maybe_delete_remote now withholds only on CORRUPT (and the defensive
  None case); SKIPPED proceeds to delete, recorded as kind="remote_delete" with a distinct
  completeness-only message at level="warning". _finalize_download_prefix's rename event no
  longer hardcodes "downloaded, verified, and extracted" -- it now names the real
  verify_state/extract_state. DESIGN.md §7.3, CHANGELOG.md, and docs/decisions.md updated in
  the same commit; tests updated and new coverage added in tests/test_postprocess.py. Full
  suite (1037 tests), ruff check, and ruff format --check all pass.
---

# Task: A `move`-mode delete is gated on verification *passing*, not on verification *running*

`core/postprocess.py._maybe_delete_remote` currently withholds the remote delete whenever
`verify_state != "VERIFIED"`. That folds two different things into one: verification that
**failed** (`CORRUPT` — real evidence of a bad download) and verification that **did not
apply** (`SKIPPED` — no `.sfv`/`.md5` sidecar in the release and hash-on-disk verification
disabled, so there was nothing to check against).

The user's rule, stated directly: **we require verification to pass where it applies; we do
not require that it ran.** Only `CORRUPT` is a failure. `SKIPPED` must not withhold the
delete.

## Why this is safe now, and why it wasn't obviously safe before

Confirmed live on the user's production instance (`https://lftpweb.crzynet.com`, events
160–167 and 145–146) on 2026-08-15T01:34Z and 01:40Z. Two `ar-tv` WEB-DL releases downloaded
correctly and had their remote copies withheld:

```
verify   SKIPPED: no .sfv/.md5 sidecar found and hash-on-disk verification is disabled
delete   WITHHELD -- verification produced no usable result for this move-mode item
```

while a sidecar-bearing rar release on queue 1 in the same log went `VERIFIED: 12 file(s)
matched sidecar checksum` → `remote_delete`. The machinery works; the gate is simply too
strict.

**By the time `_maybe_delete_remote` runs, the item has already cleared three independent
checks:**

1. lftp exited 0 under `cmd:fail-exit true`.
2. The settle gate — the remote fingerprint was stable for `REQUIRED_SETTLE_SCANS` (2) *and*
   `SETTLE_MIN_AGE_S` (60s).
3. **A filesystem completeness check** — `core/queue.py:842` only triggers post-processing on
   `settled and complete`, where `complete` (`core/queue.py:966`) is
   `not evidence and local_bytes >= remote_total`: no leftover `.lftp`/temp files anywhere in
   the tree, and local bytes at least matching the remote total.

Check 3 landed in `0460111` on **2026-08-14**, in response to the incident where lftp exited 0
while a file sat 500 MB short as a `.lftp` temp file. DESIGN.md §7.3's strict "verified or
nothing" rule was written at **phase 5**, long before it. Truncation — the main risk the strict
gate existed to catch — is now caught upstream by better machinery, and the gate was never
re-examined when its primary justification moved.

**The residual risk, to be stated honestly in the docs and not glossed:** a release whose bytes
arrived intact in *count* but wrong in *content* will now have its remote copy deleted. Over
SFTP that requires corruption surviving both TCP checksums and SSH's per-packet MAC. It is not
zero. It is a different order of likelihood from truncation. The user has decided to accept it.

## Before you start

- **Read `DESIGN.md` §7.3 and §7.4** (the delete gate) and `core/postprocess.py`'s module
  docstring — the "every delete and every withheld delete writes an event; there is no silent
  path here" invariant is load-bearing and must survive this change intact.
- Read `docs/decisions.md`'s phase 5 entry on why verification is forced on for `move`
  regardless of both toggle layers. **That stays true** — `verify_effective` must keep
  `or sync_mode == "move"`. This task changes what is done with the *result*, not whether
  verification runs.
- Read `prompts/open-issues.md`'s "The settle gate" section for the surrounding evidence chain.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this plan
needs to modify. If any have uncommitted changes, list them and ask before touching them.
Surface unrelated dirty files once as awareness; don't block. This file is exempt.

## What to do

### 1. The gate itself — `core/postprocess.py._maybe_delete_remote`

Withhold **only** when `verify_state == "CORRUPT"`. Proceed to delete on `VERIFIED` and on
`SKIPPED`.

Keep the defensive `None` case withholding — for a `move` queue `verify_effective` is forced
true so `verify_state` is always set, and a `None` arriving here means a code path changed
underneath this function rather than a release without a sidecar. Say that in a comment so a
future reader doesn't "simplify" the two back together.

Rewrite the withheld message so it names the actual failure rather than the old catch-all
"produced no usable result" wording, which will no longer be reachable for `SKIPPED`.

### 2. Keep History able to tell the two kinds of delete apart

A delete made on completeness evidence alone must not log as an ordinary `remote_delete`
indistinguishable from a checksum-backed one. Keep `kind="remote_delete"` (History filters and
`docs/` reference that kind; don't fragment it), but make the message state which evidence
backed it — something like:

- checksum-backed: `... deleted verified remote copy <path>` (unchanged)
- completeness-only: `... deleted remote copy <path> on completeness evidence alone (no
  .sfv/.md5 sidecar; hash-on-disk verification disabled)`

Pick the final wording to match the surrounding house style; the requirement is that a human
reading History can tell at a glance which deletes had a checksum behind them.

Consider `level="warning"` for the completeness-only path if that reads better against
`api/history.py`'s level filter — your call, but say which you chose and why in
`docs/decisions.md`.

### 3. Fix the dishonest rename message — `core/postprocess.py:1007`

`_finalize_download_prefix` hardcodes:

> `renamed <src> -> <dst> now that it has been downloaded, verified, and extracted (folder
> prefix during transfer)`

Both `ar-tv` items above got that message while their own events, in the same second, recorded
`verify SKIPPED` and `extract "no archives found"`. It claims two things that did not happen.
This project's diagnostic posture rests on event messages being literally true — a future
session reading that line would reasonably conclude the item was verified.

Make the wording reflect the actual `verify_state` / `extract_state`. This will mean threading
those values into `_finalize_download_prefix` (or building the message at the call site);
choose whichever is the smaller change against the existing structure.

### 4. Tests

`tests/test_postprocess.py`, `tests/test_postprocess_e2e.py`, and `tests/test_history_api.py`
reference `remote_delete_withheld` / `SKIPPED` and will be pinning the old rule. Update them,
and **add** coverage for the new behaviour rather than only editing the old assertions:

- `SKIPPED` + `move` → delete **proceeds**, `remote_deleted_at` set, event message names
  completeness-only evidence.
- `CORRUPT` + `move` → delete still withheld, event still written.
- `VERIFIED` + `move` → unchanged, message unchanged.
- The rename message reflects a `SKIPPED` verify and a no-archives extract truthfully.

### 5. Docs — all in the same commit as the code

- **`DESIGN.md` §7.3/§7.4** — correct the rule to "verification must not have failed", with the
  three-check evidence chain and the accepted residual risk named. The repo rule is that a build
  revealing DESIGN.md is wrong gets the doc corrected, never quietly diverged from.
- **`CHANGELOG.md`** — this changes behaviour for any existing install with a `move` queue:
  releases without a sidecar will now have their remote copy deleted where previously they were
  kept. That is exactly the kind of change an existing user must not discover by surprise.
- **`docs/decisions.md`**, newest at top — the reasoning above, the rejected alternative (a
  settings toggle for "delete without checksum evidence": rejected because defaulting it off
  reproduces the reported complaint and defaulting it on is unconditional-with-extra-surface),
  and the residual-risk acceptance.

## Conventions to honor

- `uv run ruff check` **and** `uv run ruff format --check` are two different gates — run both
  separately and read each exit code. This exact failure mode has bitten this project repeatedly.
- Run the backend suite (`uv run pytest`). Report the count.
- Do not touch the user's production instance or its settings.
- Do not reorder the pipeline. `move` deleting the remote before extraction runs is a **separate
  open question** (`prompts/open-issues.md`, "Smaller, and genuinely optional") that the user has
  not decided. Explicitly out of scope here — do not fix it as a side effect.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/`.
3. Record the non-obvious decisions in `docs/decisions.md`, newest at top.
4. **You are a spawned agent: do not commit.** Prepare the working tree, then report the file
   list and a proposed one-line `fix:` commit message back to the orchestrating session, which
   surfaces the `y/n`. Never `git add -A`, never push, never auto-commit.
