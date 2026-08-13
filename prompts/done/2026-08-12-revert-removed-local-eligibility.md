---
name: 2026-08-12-revert-removed-local-eligibility
status: done
created: 2026-08-12
model: sonnet
completed: 2026-08-12
result: >
  Reverted core/autoqueue.py.ELIGIBLE_STATES to ("REMOTE_ONLY", "PARTIAL") and added
  a site-level setting, AutoQueueSettings.re_download_externally_removed (default
  False), that puts REMOVED_LOCAL back in for anyone who wants it. lftpweb's own
  deletions (REMOVED_BOTH + auto_queue_suppressed) stay excluded unconditionally
  under either setting value. Surfaced in Settings -> Queues; delete confirmation in
  FileTree.tsx now states whether the remote copy survives. Corrected DESIGN.md
  (§3.2 rule 3, §4.6, §4.7, §13, §14 for the eligibility revert; §6 and §3.3 for two
  unrelated same-day staleness problems from commit 855e7a3). docs/decisions.md and
  CHANGELOG.md updated. 587 tests passing (584 + 3 new), both ruff gates and
  npm lint/build clean.
---

# Task: Put `REMOVED_LOCAL` back outside `ELIGIBLE_STATES`, and say so in the delete dialog

Earlier today `REMOVED_LOCAL` was added to `core/autoqueue.py.ELIGIBLE_STATES` as a fix for
"issue 4". **That framing was wrong** — it was the orchestrating session's mistake, not the
implementing agent's, and the implementation is careful and well-reasoned on top of a bad
premise. Revert the eligibility change; keep everything else.

## Why it is wrong

There are exactly two ways an item's local copy goes away:

1. **lftpweb deleted it** — `core/local_delete.py.delete_local` writes
   `state = 'REMOVED_BOTH'` **and** `auto_queue_suppressed = 1` in the same write. Never
   re-queued. **This is already correct and must stay exactly as it is.**
2. **Something else moved it** — an `*arr` importer, a human, a script. The item reaches
   `REMOVED_LOCAL` through §7.3's grace period with the suppression flag clear.

With `REMOVED_LOCAL` eligible, case 2 becomes an infinite loop on a `copy`-mode queue with
auto-queue on: import moves the files out → `REMOVED_LOCAL` → the remote copy still exists
and the pattern still matches → re-queued → downloaded again → imported again → repeat every
scan interval, forever. Bandwidth, seedbox load, and duplicate imports.

Excluding `REMOVED_LOCAL` is exactly what `DESIGN.md` §3.2 rule 3 existed to do. **The
user's live queue is `copy` mode and auto-queue is the feature they are actively building
patterns around**, so this is live exposure, not theory.

The narrower worry that motivated the original change — a half-imported release whose
straggler files arrive later and can never be fetched — **is now handled by the settle
gate**, which stops that release being marked `DOWNLOADED` off a partial remote set in the
first place. The motivation is gone; the cost is not.

## Before you start

- Read `core/autoqueue.py` in full, especially the module docstring's numbered point 3 and
  the long comment above `ELIGIBLE_STATES` — both argue for the change you are reverting.
- Read `core/local_delete.py` — specifically that it writes `REMOVED_BOTH` + suppression.
- Read `DESIGN.md` §3.2 rule 3, §4.6, §4.7 — these were rewritten today to document the
  behaviour you are reverting.
- Read `docs/decisions.md`'s entries for the deletion cluster and for the DESIGN.md wording
  pass.

## Working tree check

`git status --porcelain`. Several agents landed today and some changes may still be
uncommitted. If files you need are dirty, list them and ask.

## What to do

1. **Make it a setting, defaulting to the safe behaviour — do not just hardcode the
   revert.** The user's call, and it is the better design: the current behaviour is not
   *always* wrong, it is wrong as a default and wrong to be unchangeable.

   Add a site-level setting — working name **"Re-download items removed outside lftpweb"**,
   `re_download_externally_removed`, **default `False`**. Store it as JSON in the `setting`
   table alongside the other `*Settings` dataclasses; **no migration.**

   - `False` (default): `ELIGIBLE_STATES` is `("REMOTE_ONLY", "PARTIAL")`, as it was before
     today. An item something outside lftpweb removed is left alone.
   - `True`: `REMOVED_LOCAL` is eligible again, for anyone who genuinely wants a local
     deletion to be re-fetched.

   **Name and scope it by *who removed the file*, not by the state name.** lftpweb's own
   deletions (`core/local_delete.py`, retention) are `REMOVED_BOTH` + `auto_queue_suppressed`
   and must be re-fetched **never**, under either setting — that is not a behaviour anyone
   should be able to switch on. The setting governs only the externally-removed case. Make
   that unmistakable in the setting's help text and in the code comment; "locally deleted" is
   ambiguous about the agent of deletion, which is the exact confusion that produced this bug.

   Replace the comment above `ELIGIBLE_STATES` and the docstring's point 3 with the *correct*
   reasoning — do not just delete the text. Name the two-paths distinction explicitly.

   **The concrete case that decided the default** (worth putting in the comment): copy mode,
   Sonarr/Radarr importing locally on one schedule, and a cleanup script pruning the seedbox
   on a different one. Between the import and the remote cleanup, the same release re-fetches
   on every scan and the importer is handed duplicates repeatedly.

   **This only bites `copy`-mode queues.** On a `move` queue the remote copy is deleted after
   a verified download, so there is nothing left to re-fetch and the loop cannot occur — the
   item simply reaches `REMOVED_BOTH`. Say so in the help text, so a `move` user is not left
   wondering whether the toggle affects them. It also means defaulting site-wide to `False`
   costs `move` users nothing at all.

   Surface the toggle in Settings wherever auto-queue behaviour is configured, since that is
   the only thing it affects. Note in your report whether it would be better as a *per-queue*
   column than site-level — auto-queue **and `sync_mode`** are both per-queue, and since this
   only matters for `copy` queues that is a real argument for per-queue. But it needs a
   migration, and site-level matches the retention-settings precedent. Do not build the
   per-queue version; just give an opinion.
2. **Keep everything else from the deletion cluster.** `suppressed_reason = 'deleted_local'`
   stays — it is still what distinguishes our deletions from anyone else's, it is in
   migration 008's `CHECK` constraint, and a future prune will want it. Do not revert the
   migration.
3. **Fix the tests that assert the reverted behaviour** rather than deleting them. The
   deletion task added/rewrote tests in `tests/test_autoqueue.py` for `REMOVED_LOCAL`
   eligibility. Invert them: an unsuppressed `REMOVED_LOCAL` item must **not** be picked up,
   and there should be an explicit test named for the regression — an importer moving a
   completed release out must not cause a re-download on the next scan.
4. **Add the remote-still-exists warning to the delete confirmation** (the user asked for
   this directly). `FileTree.tsx`'s delete confirmation panel already shows count and total
   bytes. Add whether the selected items still have a remote copy:
   - Remote copy exists → say the local files will be removed, the remote copy stays, and
     the item will **not** be re-fetched (because lftpweb suppresses its own deletions).
     Tell the user what will happen; do not warn them off a safe action.
   - No remote copy (`LOCAL_ONLY`) → the item is gone entirely once deleted.
   Keep it factual and short. `remote_size` on the item view already tells you which case
   applies; confirm that rather than assuming.
5. **Correct `DESIGN.md` — three separate staleness problems, all from today.** Unusually
   for this project you *are* editing `DESIGN.md` directly: the user approved applying
   wordings today, and several sections were written against behaviour that has since
   changed underneath them, all within commit `855e7a3`. Leaving them makes the doc
   confidently wrong, which is worse than the gaps it just closed.

   a. **§3.2 rule 3, §4.6, §4.7** (and possibly §13/§14) were rewritten to document
      `REMOVED_LOCAL` eligibility — the thing you are reverting. Restore the original
      intent, keep the improved prose, and document the new setting from step 1.

   b. **§6's "the trigger is the job-success transition, and only that one"** is now false.
      The settle follow-up added a **second, narrow trigger**: `core/engine.py._persist`
      calls `PostprocessPipeline.trigger()` when its own scan pass releases a `rel_path`
      from `REMOTE_ONLY`/`substate='settling'` straight to `DOWNLOADED` with no fresh job in
      between — the fix for items that would otherwise sit held forever with auto-queue off.
      Replacement wording is drafted in `docs/decisions.md`; apply it. Document *both*
      triggers and the guard that makes the second safe (it fires only on that exact
      prev-state/prev-substate pair, and `_process_item` independently re-checks
      `item.state == 'DOWNLOADED'`).

   c. **§3.3's "Off by default"** is now false — the settle gate ships **on**, with a
      `SETTLE_MIN_AGE_S = 60.0` wall-clock floor required *in addition to*
      `REQUIRED_SETTLE_SCANS`. Update the section to describe both conditions and the new
      default, and record it as the third reasoned exception to the defaults-off rule
      (alongside `move`-mode verification and the phase 7 scheduled backup).

   Read the relevant `docs/decisions.md` entries before writing any of these — several
   drafts touch the same sections and you want one coherent result, not stacked revisions.
   Note the reversal in `docs/decisions.md` rather than silently rewriting history.

## Conventions to honor

- `docs/decisions.md`, newest at top: record that this reverses a same-day change, why the
  original premise was wrong, and what is deliberately kept.
- `CHANGELOG.md` — the eligibility change may already have a `### Fixed` bullet from the
  deletion task. **Correct or remove that bullet** rather than adding a second one
  contradicting it; neither shipped in a release, so the changelog should describe the net
  result, not the detour.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`.
- `uv run pytest` with the fake seedbox up.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `fix:` message, test count,
   lint results, what you kept from the deletion cluster, the exact dialog wording you
   chose, and anything not fixed. Never `git add -A`, never push.
