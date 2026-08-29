---
name: 2026-08-24-disk-review-visibility-backend
status: completed          # pending | completed | failed
created: 2026-08-24
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-24
result: >
  Excluded claims are retained instead of dropped (attribution=excluded, shown in seeding_estate);
  a new excluded_content pile covers unclaimed content under an excluded path; broken_seeds
  retired in favor of a per-claim `torrents` array (client-reported figures + file_count/
  size_on_disk/missing_on_disk); a `clients` roster carries per-instance reachability and field
  capabilities. is_authorized_delete_target and resolve_category_exclusion_paths untouched;
  invariant covered by a dedicated test. DESIGN.md §17, spec §11, and docs/decisions.md updated.
  48/48 disk-review tests and the full 2065-test backend suite pass; ruff check/format clean.
---

# Task: make the disk review scan report everything it found, per client

The disk review page currently shows almost nothing. Content in an excluded category is
deleted from consideration before any pile logic runs, and migration 032 makes **every newly
observed category default to excluded** — so a real seedbox reads as nearly empty. This task
makes the scan report what is actually there, grouped by the client that reported it.

**This is a visibility change only. It does not touch delete logic, delete containment, or
what could ever become a delete target.** Stage 5 is not being built and is not being
prepared for here.

## Before you start

Read, in this order:

1. `core/disk_review.py`'s **module docstring** — the two load-bearing invariants (Set A is a
   union across every contributor; claiming is by inode, not path). Neither changes here.
2. `DESIGN.md` §17 — the connector framework as it actually exists.
3. `docs/download-client-framework-spec.md` §11 — the governing spec for this feature. §11.1a,
   §11.1b, §11.1d and §11.4 are the relevant parts.
4. `prompts/test-findings-2026-08-23.md`, findings **#16** and **#17** — the two-lftpwebs-share-
   one-seedbox shape, and the correction that fail-closed means "never act without a human
   looking at it," not "never display."
5. `CLAUDE.md` — commit rules, gate rules. Run `uv run pytest` and `ruff` from the **repo root**,
   **always in the foreground** with a generous explicit timeout. Never background a gate.

### The governing principle for this whole task

> **Exclusion is a delete-safety boundary, not a visibility boundary.**

Today those are the same flag, and finding #17 already established the lesson this violates:
*content that exists and is never surfaced is indistinguishable from content that is not there.*
This is that same lesson, applied a third time, to the excluded-category path.

### 🔴 The hard invariant — read this twice

**`is_authorized_delete_target(path, base_paths, excluded_paths)` must keep receiving the full
resolved excluded-path set, and must keep returning `False` for anything under it.** Its
signature, its behaviour, and `_resolve_client_exclusions`'s resolution of an excluded category
into paths all stay exactly as they are. `resolve_category_exclusion_paths` stays.

What changes is *only* which pile a disk entry is reported in. Nothing about what may ever be
deleted moves. If a change you are considering would make an excluded path's content eligible
for the `debris` list, you have misread this task — excluded content must be **visible and
inert**, never debris.

Add a test that asserts this directly: an excluded path whose content is now reported in the
new `excluded_content` pile is *still* `is_authorized_delete_target(...) is False`.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files
this plan needs to modify. If any of those files have uncommitted changes, list them
and ask the user before touching them. Surface unrelated dirty files once as
awareness; don't block. This file (the handoff prompt itself) is exempt.

## What to do

Files in scope: `backend/lftpweb/core/disk_review.py`, `backend/lftpweb/api/disk_review.py`,
`backend/lftpweb/models.py`, `tests/test_disk_review.py`, plus `DESIGN.md` §17 and
`docs/download-client-framework-spec.md` §11.

### 1. Stop dropping excluded claims; tag them instead

In `reconcile()`, `claims = [c for c in claims if not _category_excluded(c)]` (line ~389) goes
away. An excluded claim stays a claim, so the files under it are **claimed** — which means they
land in the seeding estate and can never reach the debris or unclaimed branches anyway.

Because the claim is retained, the machinery that folded an excluded claim's own `content_path`
into the hard-exclusion set (`excluded = {...} | {_norm(c.content_path) for c in claims if
_category_excluded(c)}`, line ~366) becomes unnecessary — that existed *only* to stop those
files falling through to the unclaimed pile once the claim was dropped. Remove it, and say so in
the docstring. This is a simplification; do not replace it with something equivalent.

Each claim gains an **attribution state**, one of:

- `bound` — its category maps to a queue (`download_client_category.queue_id IS NOT NULL`)
- `excluded` — `download_client_category.excluded = 1`
- `undecided` — a category row with neither, or no category reported at all

`run_scan` reads all three states from `download_client_category` (it already reads the excluded
subset; widen that query) and passes a per-client category→state map into `reconcile`.
`reconcile` **does not interpret** these states — it only copies the claim's state onto the rows
it emits. Keep `excluded_categories_by_client` as the input `reconcile` uses for anything
behavioural, so the behavioural and the display concerns stay separately visible in the
signature.

`debris_ambiguous_roots` and the finding-#17 unclaimed pile are **unchanged**. An *unclaimed*
file under an rTorrent root still cannot be resolved to a category, so it still fails closed
into `unclaimed`. Do not try to simplify this away — it is a different problem from the one this
task fixes.

### 2. Route excluded-path content into its own pile, never into debris

Entries under a **manually** excluded path (`download_client_excluded_path`, and any
category-resolved path with no claim currently covering it) have no claim behind them. With the
display filter gone, they would fall through to `debris` — which would propose another lftpweb
instance's data. That must not happen.

Give the per-entry loop this explicit order, and write the order into the docstring as the thing
that makes the change safe:

1. claimed → `seeding_estate` (carrying the claim's attribution state)
2. else under an excluded path → **new `excluded_content` pile**, never selectable, never debris
3. else ambiguous-root unclaimed → `unclaimed` (unchanged)
4. else eligible → `debris` (unchanged)

`excluded_content` rows carry `root`, `rel_path`, `abs_path`, `size`, `excluded_path` (which
excluded root matched), and `link_paths` computed the same link-aware way the other piles use,
so its size figure is honest rather than a naive sum.

### 3. Widen the claim shape with the client's own per-torrent figures

`ClientClaim` gains, all optional and all `None` when the connector doesn't report them:
`size_bytes`, `uploaded_bytes`, `ratio`, `seed_time_s`, `added_at`, `raw_status`, `phase`.

`reconcile()` must not interpret any of them — it passes them through. They come straight off
the `Transfer` record `run_scan` already has in hand.

**Capability honesty is mandatory here.** `USENET_BASELINE` declares `RATIO`, `UPLOADED_BYTES`
and `SEED_TIME_S` as `Support.NONE`, so every SABnzbd row will have `None` for all three. That
is correct. **Never substitute `0`, `0.0`, or an empty string** — a fabricated `0.00` ratio
sitting beside a real one is exactly the "guess dressed up as a fact" `SpaceInfo`'s own docstring
warns against.

### 4. Emit a torrents array, not per-file duplication

The response gains a `torrents` array: **one entry per claim**, carrying `client_id`,
`transfer_id`, `transfer_name`, `content_path`, `category`, `attribution`, the figures from step
3, plus two disk-derived values `reconcile` computes:

- `file_count` — how many disk entries resolved to this claim
- `size_on_disk` — link-aware bytes, computed with the same grouping rule `freed_bytes` uses

Seeding-estate file rows carry a `claim_key` (`client_id` + `transfer_id`) so the frontend can
join them to their torrent.

**Do not denormalize the per-torrent figures onto every file row.** `reconcile()` stays per-file
because inode accounting is inherently per-file (spec §11.1b), but a 40-file season pack must not
carry its ratio and seed time forty times over the wire.

### 5. Surface the two things currently dropped silently

**a. Transfers with no `content_path`.** `run_scan` line ~974 does `if not
transfer.content_path: continue` — a transfer the client reports but cannot give a path for
vanishes entirely. Emit it as a torrent row with `content_path: null`, `file_count: null`,
`size_on_disk: null`. It has no path, so it must **never** participate in claiming — do not let
it into the claim-matching loop. Its client-reported figures are still real and still shown.

**b. Broken seeds fold into the torrents array.** A `BrokenSeed` is just a claim whose path was
walked and found empty — i.e. a torrent with `file_count: 0`. Represent it as a torrent row with
a `missing_on_disk: true` marker and retire `DiskReviewBrokenSeedOut` and the separate
`broken_seeds` response field. Keep the existing rule that a claim whose root was never walked is
**not** reported as broken (absent information is not a verdict) — such a claim gets
`file_count: null`, not `0`.

Broken/excluded interaction: with claims retained, an excluded claim can now be reported missing
too. That is correct — it is visibility, which is the point.

### 6. Report the clients themselves

The response gains a `clients` array so the page can section by client: `client_id`, `name`,
`client_type`, `reachable` (did it report this pass), `failure_reason`, and the client's
**declared field capabilities** read from `download_client.capabilities_json`.

The capabilities are what lets the frontend decide which columns a section renders. **Never
expose or branch on `client_type` for that decision** — `client_type` is display metadata only
(§17 rule 6: no `if client_type == "rtorrent"` exists anywhere in this subsystem, and none may be
introduced). Keep `client_failures` working; it may be folded into this array if that reads
better, as long as a failing client still appears with its reason.

### 7. Tests

Extend `tests/test_disk_review.py`. At minimum, each of these as its own named test:

- Excluded-category content appears in `seeding_estate` tagged `attribution: excluded`, and is
  **absent from `debris`**.
- Manually-excluded-path content appears in `excluded_content`, and is **absent from both
  `debris` and `unclaimed`**.
- `is_authorized_delete_target` still returns `False` for both of the above, **after** they
  became visible. This is the invariant test; name it so it is obvious what it protects.
- A transfer with no `content_path` is reported as a torrent and claims nothing.
- A claim whose root was walked and found empty reports `missing_on_disk: true, file_count: 0`;
  a claim whose root was never walked reports `file_count: null` and is **not** marked missing.
- A SABnzbd-shaped claim reports `None` for ratio/uploaded/seed time — never `0`.
- `size_on_disk` for a torrent with a hardlinked file counts those bytes once.
- The existing finding-#17 unclaimed-pile tests still pass unchanged.

## Conventions to honor

- Match the surrounding docstring style — this module explains *why* at length, including which
  earlier decision a line reverses and what caused the reversal. Preserve that convention;
  do not rewrite history to look like it was always right.
- Doc updates ship in the **same commit** as the code: `DESIGN.md` §17 and
  `docs/download-client-framework-spec.md` §11 both need the new pile, the new response shape,
  and the visibility-vs-containment split written down.
- Record the exclusion-is-not-invisibility decision in `docs/decisions.md`, newest at top, naming
  the rejected alternative (keeping the display filter and adding a separate "show excluded"
  toggle) and why it was rejected: a toggle defaulting off reproduces the exact bug being fixed.
- Gates, each as its own **foreground** command from the repo root, reading each exit code:
  `uv run pytest` (~3.5 min, use a generous timeout), `uv run ruff check`, `uv run ruff format
  --check`. `ruff check` passing is not `ruff format --check` passing.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record non-obvious decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the working tree, then report back to the orchestrating session:
   the file list, a one-line `feat:`-prefixed commit message, and the final test counts. The
   orchestrating session surfaces the `y/n` to the user. Never `git add -A`, never push.
