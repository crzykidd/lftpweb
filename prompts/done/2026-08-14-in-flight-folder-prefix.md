---
name: 2026-08-14-in-flight-folder-prefix
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: |
  Built "folder prefix during transfer": core/download_prefix.py (site-wide
  DownloadPrefixSettings + per-queue inherit-or-override resolution + validation), migration 017
  (path_queue.download_prefix_enabled/download_prefix nullable columns;
  item.pending_download_prefix), a mirror_rename_target flag on core/lftp.py's
  build_transfer_command/JobSpec, the rename step in core/queue.py._reap_one
  (_finalize_download_prefix) gated on settled-and-complete (before postprocess.trigger, not
  "after verify" -- see docs/decisions.md for why), matching resume logic
  (_resolve_download_prefix_for_spawn, prefers the item's own recorded prefix over current
  settings), a configurable extra_dir_prefixes filter in core/local_scan.py.scan_local,
  core/engine.py.Engine._active_download_prefixes (resolved prefix unioned with every distinct
  pending prefix on record, so a stale prefix never orphans a scan), Settings -> Transfer's
  site-wide toggle+prefix field, Settings -> Queues' per-queue override, and the item drawer's
  new physical-location panel. Directory items only, per the prompt's scope limit. Ships off by
  default; flagged in the session report that the user may want it defaulted on given it fixes a
  live, reproduced data-loss bug. Reverses part of phase 5's staging_path decision -- named
  explicitly in docs/decisions.md, with the new evidence that changed it. Verification: uv run
  pytest (961 passed, including two new files -- tests/test_download_prefix.py, 32 unit tests,
  and tests/test_download_prefix_e2e.py, 3 tests against the real fake seedbox including a
  stop-mid-transfer-then-resume-into-the-prefixed-directory case), ruff check and ruff format
  --check clean, npm run lint / npm test (145 passed) / npm run build clean, docker compose
  config --quiet clean on all three compose files.
---

# Task: Download a directory item into a prefixed folder so importers can't see it mid-transfer

A `mirror` job renames each file to its **final name** as that file completes, so any importer
watching the download directory can grab a half-finished release. Live proof, 2026-08-13/14:
Sonarr imported the episodes that had finished, then its post-import cleanup deleted the folder
while lftp was still transferring the last two, and lftp died on `rename(…): No such file or
directory` for both.

Add a setting — the user's own words: **"Folder prefix during transfer"** — an on/off toggle plus
the prefix string, defaulting to the example `.downloading-`, configurable **site-wide and
per-queue**.

## Why a dot prefix specifically, and why not `_UNPACK_`

The user researched the importer behaviour; this is settled, do not re-derive it:

- **`_UNPACK_` will not protect us.** Sonarr skips it because Completed Download Handling waits
  for *the download client's API* to report Completed. lftpweb is not a download client Sonarr
  knows about, so there is no API for it to consult and that protection does not apply.
- **`_FAILED_` likewise** — Sonarr ignores it and relies on the download client to clean it up.
- **A leading dot is purely path-based.** Sonarr skips hidden folders regardless of client
  integration, and so do Radarr, Plex and Jellyfin. That is why it is the default.

The prefix is **configurable rather than hardcoded** precisely because other tools use other
conventions — that is the reason this is a setting and not a constant.

## The decision this reverses — read before designing

Phase 5 considered making downloads land somewhere other than `local_path` and **rejected it**.
From `docs/decisions.md`:

> making that true would mean the reconciler comparing remote vs. local at a *different* root
> during a transfer than after one completes

That cost is real and unchanged: resume-from-partial, `core/progress.py`'s sampler,
`core/queue.py._completeness_on_disk` (2026-08-14), and `item.local_size` all key off the item's
local path, and all must follow it during transfer and switch when it lands.

**This is new evidence, not a re-litigation.** Phase 5 weighed that cost against *staging
semantics*; nobody had yet watched an importer delete a folder out from under a running mirror.
Say so explicitly in `docs/decisions.md` — name the phase 5 entry and explain what changed.

## Scope limit that removes most of the risk

**Only directory items need this.** A single-file (`pget`) item is complete the moment lftp
renames it off `.lftp` — there is no window where an importer can see a partial release, because
the release *is* that one file. The race exists only for a multi-file `mirror` where siblings are
still in flight.

Apply the prefix to directory items only, and say so in the setting's help text. If you conclude
single files need it too, stop and report why rather than widening scope on your own.

## Before you start

- Read `CLAUDE.md`, `DESIGN.md` §3.2, §4.4, §4.7, §6, §7.
- Read `core/queue.py._spawn_decision` (how `local_root` and lftp's target are computed — note
  `mirror`'s target is the item's **parent**, `pget`'s is the exact file path; this asymmetry is
  documented and load-bearing), `core/local_scan.py`, `core/reconcile.py`, `core/progress.py`,
  `core/postprocess.py`, `core/extract.py` (the `_UNPACK_` sibling-staging pattern this parallels).
- Read `prompts/done/2026-08-14-exit-zero-is-not-completion.md` — `_completeness_on_disk` walks the
  item's local root and must walk the right one.

## Working tree check

Run `git status --porcelain` first. Several queued tasks touch `core/queue.py`,
`components/FileTree.tsx`, `pages/FilesPage.tsx` and the Settings tabs. If a file this plan needs
is dirty, list it and ask before editing. This prompt file is exempt.

## What to do

### 1. The setting, following this project's established shape

- **Site-wide**: a dataclass persisted as JSON in `setting`, the same pattern
  `TransferSettings`/`PostprocessSettings`/`SettleSettings` use — an `enabled` flag and a `prefix`
  string.
- **Per-queue**: a nullable column via migration, following the **inherit-or-override** model
  established in `3500b3f` (per-queue `NULL` means "inherit the site setting", not "off"). Do not
  reintroduce the AND-of-two-toggles shape that commit deliberately removed.
- **Default off.** This project ships every new capability off (`prompts/startnewsession.md`'s
  safety rule) and this one changes where in-flight bytes live, which an existing install with a
  transfer in progress would notice. **Flag to the user in your report** that they may want it
  defaulted on, as they did for the settle gate — that is their call, not yours.
- Validate the prefix server-side: non-empty when enabled, no `/`, no path traversal, and it must
  not collide with `_UNPACK_`/`_FAILED_`/`.lftpweb-mount-ok`.

### 2. The download path, and the rename

When enabled for a directory item, lftp writes into `<local_path>/<prefix><name>/` and the
completed item is renamed to `<local_path>/<name>/`.

**When the rename happens is the design question — work it out and justify it.** The user's
instinct was "after verify is complete". Handle every combination that actually exists: verify
enabled or not, extraction enabled or not, `copy` vs `move`, and a `move` queue's remote delete
which is gated on verification. A coherent rule is "rename as the last step before the item is
published complete, after the completeness check and after verify when verify runs" — but you must
confirm that against `core/postprocess.py`'s real ordering rather than assuming it.

Interactions to resolve explicitly, not discover later:

- **`_UNPACK_` staging is a *sibling* of the item directory** (deliberately, so it is outside the
  tree the reconciler walks and outside anything a later move relocates). If the item lives in a
  prefixed directory during transfer, work out where that sibling lands and whether extraction
  runs before or after the rename.
- **`core/local_scan.py` must filter the prefixed directory** exactly as it filters
  `_UNPACK_`/`_FAILED_`/`.lftpweb-mount-ok`, or the reconciler will see phantom `LOCAL_ONLY`
  nodes for in-flight content. Since the prefix is configurable, that filter can no longer be a
  module constant — thread the active prefix through, and make sure a *stale* prefix (changed
  while a transfer was in flight) does not orphan a directory forever.
- **Resume from partial** must find an existing partial in the prefixed directory.
- **The rename must be atomic** — same filesystem, `os.rename`. If it can ever cross a filesystem
  boundary, reuse `core/postprocess.py.move_tree`'s existing EXDEV handling rather than writing a
  second copy-then-rename.

### 3. How the prefix is displayed — logical name in the tree, physical path in the detail

The user's rule: **the Files list shows the real release name without the prefix; the drawer and
History show the actual path, including the prefixed directory.**

This is not just a display preference — the first half is a *requirement*:

- **`item.rel_path` must never contain the prefix.** It is what the reconciler matches against the
  remote tree (`core/reconcile.py`), what `item_settle` is keyed by, and what auto-queue's
  patterns evaluate. Putting a local-only prefix into it would break remote↔local matching
  outright. So the Files tree needs **no special-casing at all** — it already renders `rel_path`,
  and `rel_path` is already the logical name.
- **The prefixed directory is a physical detail**, derived from the item's location plus the
  active prefix, never persisted into the identity of the row.

**There is direct precedent for this split — follow it rather than inventing a new pattern.**
`core/local_scan.py` already reports a still-temp `.lftp` file under its *final, stripped* name so
it can be matched against its remote counterpart, while `find_temp_files` returns the *real*
on-disk path for callers that need to name what is actually there (its own docstring explains
exactly this, and `_completeness_on_disk`'s audit message is the caller). Same principle, one
level up: match and display by the logical name, report the physical path where the physical truth
is what matters.

Concretely:

- **Files tree** — unchanged. Real name, no prefix, no conditional rendering.
- **Item drawer** (`ItemDrawer.tsx`, which already shows `local_mtime` and a lifecycle chronology)
  — show the actual local path, and while the item is in flight make it visible that it currently
  lives in the prefixed directory. This is the one place a user should be able to answer "where is
  this file *right now*."
- **History / audit events** — the physical path, which is already what happens: lftp's own
  `output_tail` is recorded verbatim, and tonight's failures quoted the full
  `.../xpost/S06E21….mkv.lftp` paths. Do not rewrite or strip paths in error messages to make them
  look tidier; a failure message naming a path the user cannot find on disk is worse than an ugly
  one.
- Any **new** audit event this task adds (e.g. the rename on completion) should name both: the
  logical item and the physical directories it moved between.

### 4. UI for the setting

Site-wide control in Settings → Transfer, per-queue override in Settings → Queues, matching how
the other inherit-or-override settings already render. Show `.downloading-` as the example and say
in the help text why a dot matters (importers skip hidden folders) and that it applies to
directory items only. Use the `FieldHelp` component (`dfff677`).

## Testing

- Prefix resolution: site on/off, per-queue override, per-queue `NULL` inherits, invalid prefixes
  rejected server-side.
- A directory item downloads into the prefixed folder and is renamed on completion; a single-file
  item is unaffected.
- `scan_local` does not surface the prefixed directory's contents as `LOCAL_ONLY`.
- Resume finds an existing partial inside the prefixed directory.
- `_completeness_on_disk` walks the prefixed root while the job is live.
- A `move` queue still verifies before deleting the remote, with the rename in the right place.
- An end-to-end test against the fake seedbox, in the shape of `tests/test_postprocess_e2e.py`.

Run `uv run pytest` with the fake seedbox up, `ruff check` **and** `ruff format --check`,
`npm run lint`, `npm test`, `npm run build`, `docker compose config --quiet` on all three compose
files.

## Conventions to honor

- Non-obvious decisions in `docs/decisions.md`, newest at top, with rejected alternatives — and
  the phase 5 reversal named explicitly.
- `CHANGELOG.md` entry. `README.md` if it describes where downloads land.
- If `DESIGN.md` needs a clause for this, **draft it in `docs/decisions.md` and ask** — do not edit
  `DESIGN.md`.
- **You cannot see the UI** — no browser exists here. Claims mean "builds, type-checks, lints,
  endpoints verified over HTTP", never "renders correctly."

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
