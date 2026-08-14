# Open issues — 2026-08-12 / 2026-08-13 / 2026-08-14 session

A living list, not a handoff prompt. It never moves to `done/`.

**Everything raised on 2026-08-12 and 2026-08-13 is closed.** Sixteen commits, tests
**489 → 701**. Closed items are kept below with the commit and the reasoning, because the
*why* is what is worth finding again — one fix was shipped and deliberately reversed the same
night, and the reasoning for that reversal is more valuable than the diff.

**Everything raised on 2026-08-14 is closed too**, across ten commits (`a75dc38`…`b4de50a`).
Tests **701 → 967 backend / 189 frontend**. The queue that existed at the start of that session
is empty.

Almost every item came from the user running the app against a real seedbox and reporting what
looked wrong. **CI was green before each one was found.** That is the pattern of this project
and it held across all three sessions.

**The 2026-08-14 session's own hard lesson is recorded in the `bytes_start` section below.**
A second application (an old `seedsync` container, autostarted by an unrelated update) was
writing into the same download directory, and produced symptoms that were confidently
misdiagnosed as three separate application bugs before anyone thought to check `ps`. Read that
section before trusting any live-evidence conclusion in this file.

---

## Queued, written, not yet run

**Nothing.** The queue is empty as of 2026-08-14. Every prompt written during the
2026-08-12/13/14 sessions has been executed and lives in `prompts/done/`.

## Decisions waiting on the user

- **Rename the CI job?** `"Frontend lint + typecheck"` now also runs `npm test`, but that exact
  string is a **live required status check** on `main` (verified via `gh api`). Renaming the job
  without updating branch protection in the same motion blocks every future PR. Rename both
  together, or leave it.
- ~~**`docker-compose.yml` pins `ghcr.io/crzykidd/lftpweb:0.0.1`**~~ — **resolved 2026-08-13**:
  it now uses `:latest`, the tag the publish matrix actually pushes on every merge to `main`
  and every published release, with the comment corrected (it still claimed no GitHub remote
  existed). A copied compose file now resolves to the newest published build instead of a
  version tag that does not exist yet.
- **A `dev` → `main` PR, and the first release.** `dev` is far ahead of `main`; nothing has been
  tagged, and `release-prep`/`release-cut` have never been run. **Planned for 2026-08-14
  afternoon**, after the user click-tests the night's work.
- ~~**Should "Folder prefix during transfer" default ON?**~~ — **decided 2026-08-14: yes.** The
  fourth deliberate exception to "every new capability ships off", after `move`-mode forced
  verification, the phase 7 scheduled backup, and the settle gate. Same reasoning as the settle
  gate's own flip: it fixes a *reproduced* defect rather than adding a preference, so an existing
  install silently keeps running with that defect live unless it defaults on. Flipped hours after
  it shipped off in `342f96c`. Existing installs will notice — see `CHANGELOG.md`'s `### Changed`
  entry.
- **Two `DESIGN.md` §4.3 wordings are drafted and unapplied**, both in `docs/decisions.md`
  awaiting approval: (1) exit 0 means lftp reported no error, not that every byte arrived —
  completion is confirmed from the filesystem; (2) the new `LOCAL_FS_ERROR` class and its place
  in the transient set. The repo's rule is that a build revealing `DESIGN.md` is wrong gets the
  doc corrected rather than quietly diverged from, so these should land or be rejected, not
  linger.

## Still open — read these first

### A terminal removed row has no UI path to an individual reset

2026-08-14, found while fixing the All-scope reset preview
(`prompts/done/2026-08-14-reset-all-preview-undercounts.md`). `POST /api/items/{item_id}/reset`
(`api/jobs.py.reset_item`) works fine given any real `item.id` — it does not check whether the
row is currently published. The gap is entirely client-side: the Files-page **Selected** scope
(`QueueResetControls.tsx`) can only ever select rows it can see, and `core/engine.py` stops
publishing a row once it lands on `REMOVED_LOCAL`/`REMOVED_BOTH` with nothing left in either
tree (`a4a626d`) — so a single already-removed-but-still-tracked item has no checkbox to select
and no way for a user to learn its `item_id` from the UI at all.

**All** now reaches these rows (`reset_queue_targets`), and **Pattern** already did
(`reset_pattern_matches` reads the `item` table directly, not the published tree) — so both
multi-target scopes can reset one of these rows today. Only the single-item scope cannot target
one deliberately, on its own, without going through a broader scope that also touches everything
else matching it. Not fixed here — the task that found it was scoped to the All-scope preview
defect, and giving the Files page a "removed items" view or picker is a real feature, not a
one-line fix.

### ~~No frontend test runner~~ — **closed `129cfcf`**

Vitest + happy-dom, `npm test`, wired into the CI Frontend job. 118 tests covering the pure
logic: `sortTree`'s sibling-preserving invariant asserted on tree structure, the collapse
preference's default-plus-exceptions model including newly-arrived directories, every branch of
`resetWarning`, `storage.ts`'s throwing paths, `format.ts` edge cases, and popover placement.

**No component rendering is tested** — mounting `FileTree` would mean mocking the API client,
the WebSocket live model, `@tanstack/react-virtual`, and a portal-based hover card, for logic
the unit tests already cover directly. That is a deliberate boundary, recorded in
`README.md`'s Known gaps.

### Almost none of the new UI has been seen by anyone

New on 2026-08-13 and unviewed: R/L/V/E lifecycle icons, inline progress bars, column sorting,
the persisted collapse preference, the "Missing only" filter, the per-row info icon, and the
generalised item drawer with its both-sides panel, lifecycle chronology, and history.

**No agent can see any of it** — there is no browser in the environment agents run in. Every
UI claim in this repo means "builds, type-checks, lints, and the endpoints it calls were
verified over real HTTP." Never "renders correctly."

The specific thing to look at first: **progress-bar text contrast at around 50% fill**, where
the label straddles filled and unfilled background. That is the case that looks fine in a
mockup and reads badly in practice.

### A cleaned-up archive rests in a different state depending on sync mode

After archive cleanup deletes the volumes:

- **`copy` queue** — the remote rars still exist, so the node stays in the tree and the counts
  predicate marks it **`EXCLUDED`**. Correct.
- **`move` queue** — both copies are gone, so the row goes through §7.3's grace period and
  lands at **`REMOVED_LOCAL`** after ~10 minutes. Semantically wrong: nothing went *missing*,
  we deleted them deliberately as part of extraction.

Functionally harmless today (archive volumes are children, and auto-queue only considers
top-level items), but it means the same event produces two different readings. A cleaned-up
archive should rest at `EXCLUDED` in both modes.

**Blocks a UI decision.** The user floated collapsing archive volumes into one summary row —
*"14 archive volumes · removed after extraction"*, expandable, so the screen stays clean but
the provenance of the `.mkv` is still visible. Worth doing, but not until there is one
consistent resting state to summarise. Their own note: *"Not sure on this."*

### `AuthSettingsIn` has the silent-reset shape, with security stakes

The 2026-08-13 settings audit fixed the replace-on-PUT bug for `PostprocessSettings` and
`RetentionSettings` by merging on `model_fields_set`. It found `AuthSettingsIn.proxy_header`
and `proxy_trusted_cidrs` have **the same defaulted-field shape** and left them alone as out of
scope.

That one matters more than the others: silently resetting a trusted-CIDR list is a security
control quietly turning itself off. Not known to be reachable — nobody has checked whether the
Auth tab omits those fields on save — but it is the same pattern that *was* reachable twice.

`SettleSettingsIn`/`AutoQueueSettingsIn` are single-field so cannot partially omit;
`BackupSettingsIn`/`TransferSettingsIn`/`MetricsSettingsIn` have no defaults and are not
vulnerable.

### ~~A job spawned with `bytes_start` reading ~18 GB against a directory confirmed empty~~ — **not a bug; closed 2026-08-14**

**There was no defect. A second application was writing into the same directory.** The user's
old `seedsync` container had autostarted after an unrelated update and was downloading into
`/mnt/fs02-media/working/box-ar-tv` alongside lftpweb. `item.local_size` was reporting real
bytes that really were on disk; the directory was not empty, it only appeared so because nobody
knew seedsync was running.

The evidence is clean, because the same job shape ran on both sides of the discovery:

| Job | Environment | `bytes_start` |
|---|---|---|
| 45 | seedsync running | 1,531,314,176 |
| 46 | seedsync running | 1,703,575,552 |
| **47** | **seedsync destroyed** | **0** |

The earlier writeup here traced every writer of `item.local_size` and correctly concluded none
of them could latch a stale value — that analysis was right, and the reason nothing explained it
is that nothing in this codebase was responsible. The "leading hypothesis" recorded at the time
(an inflated `scan_interval_s`, or a scan racing a slow mount) was **wrong**; it is preserved
here only as an example of a plausible theory built on contaminated evidence.

**The lesson worth keeping.** Two hours of that session went into confident, detailed, wrong
diagnoses — the settle gate releasing early, lftp "lying" about exit 0, this. Each fit the
available evidence. The tell that was missed for a long time: files were actively being written
while `ps aux | grep [l]ftp` showed **no lftp process in the container at all**. When live
behaviour contradicts code that reads correctly, check for a second actor before theorising
about the code. Cheap discriminators: is the process that should be doing the work in `ps`? Does
a running job's recorded `pid` exist? Is disk usage larger than one copy of the data?

A media importer counts as a second actor too — Sonarr was separately confirmed the same night
importing completed episodes mid-`mirror` and deleting the release folder while lftp was still
transferring into it. That one **was** a real gap, and it is what
`prompts/done/2026-08-14-in-flight-folder-prefix.md` closed.

### The folder prefix and the settle gate's stuck-item recovery don't compose

2026-08-14, found by auditing every caller that builds a local path after two prefix bugs in one
morning. **Not data loss, and self-recovering when auto-queue is on** — recorded because the
failure shape is non-obvious and the obvious fix is worse than the problem.

`core/engine.py`'s `unstuck` path (the settle gate's own stuck-item follow-up) rescues an item
whose job succeeded while the remote was still settling: a later scan computes it structurally
`DOWNLOADED` and fires post-processing directly. **With "folder prefix during transfer" enabled,
that can never fire.** `scan_local(extra_dir_prefixes=...)` deliberately hides the in-flight
`.downloading-<name>/` tree, so the item has no visible local bytes and computes `REMOTE_ONLY`
however complete the copy on disk actually is.

Same root cause as the two bugs fixed the same morning (the PARTIAL/REMOTE_ONLY flip and delete
failing on a stopped transfer): **something assumed an item's logical path and its physical path
are the same thing.** Three defects, one assumption.

**Recovery, and why it is left alone.** With auto-queue on the item recovers by itself: it is
`REMOTE_ONLY`, eligible, settled and unsuppressed, so it re-queues, lftp resumes into the
existing prefixed directory, finds everything present, exits 0, and *this* time `settled and
complete` both hold — so `_reap_one` renames and completes it. That is exactly the path
`testfolder10`'s job 44 took. With auto-queue **off**, nothing un-sticks it and a human has to
click Queue on an item that already has a full copy on disk.

The tempting fix — also renaming from the settle-release path — adds a **second owner of the
prefix rename**, and every bug in this feature so far came from two places disagreeing about a
path. One owner (`core/queue.py._reap_one`) plus re-queue as the recovery is the smaller blast
radius. Revisit only if this is actually hit.

**Audited and clear at the same time:** `core/postprocess.py` (its trigger shares the rename's
`settled and complete` gate, and a failed rename sets `complete = False`), retention deletion (it
routes through `delete_local`, which resolves the physical root), and the orphan-temp sweeper
(age-gated at 2 days; a live transfer refreshes mtime constantly).

### Smaller, and genuinely optional

- **Row lifetime** — nothing deletes `item` rows. [Issue #1](https://github.com/crzykidd/lftpweb/issues/1).
  The non-obvious part: pruning does not delete history, it *hollows it out*, because
  `job`/`event` reach `rel_path` and queue name through `LEFT JOIN`s.
- **Retention has no Settings UI** — API and dry-run preview only.
- **`re_download_externally_removed` is site-level, not per-queue**, though
  `auto_queue_enabled` and `sync_mode` are both already per-queue columns and it only matters
  for `copy` queues. Needs a migration.
- **`move` deletes the remote *before* extraction runs.** A failed extraction therefore happens
  after the remote copy is gone. Local archives survive, so it is recoverable. **The user's
  explicit call on 2026-08-13: leave it, revisit if it bites.** Do not reorder it as a side
  effect of unrelated work.
- **Encrypted-rar password retry is implemented but untestable** — no compressor exists
  anywhere to build an encrypted fixture. Real-archive rar coverage is old-style `.r00` only,
  not `.partNN`.
- **`DESIGN.md` still has no section of its own** for local delete / retention, and none for
  per-file live child progress.

---

## Closed — 2026-08-12

| # | Summary | Commit |
|---|---|---|
| 1 | No `busy_timeout` on the shared connection | `cd74f91` |
| 2 | **No settle gate** — a partial upload read as complete | `9b11df6`, `855e7a3` |
| 3 | `verify_hash_on_disk` blessed a partial file, gating a `move` delete | `9b11df6` |
| 4 | `REMOVED_LOCAL` eligibility | `dfb74c2`, **reversed** in `6d3bd95` |
| 5 | `EXTRACTED` claimed for items with no archives | `819b82c` |
| 6 | `state_changed_at` + trigger, shown on Files rows | `57f7ce9` |
| 7 | Local retention cleanup | `dfb74c2` |
| 8 | Manual delete-local in Files | `dfb74c2` |
| 9 | Expand/Collapse all gave no reason when disabled | `cd74f91` |
| 10 | "Rescan now" spinner was a 1-second timer | `cd74f91` |
| 11 | Per-queue scan interval | `c8d3e8b` |
| 12 | Child files under a mirroring directory only updated every 30s | `819b82c` |
| 13 | `_sample_and_publish_progress` hardcoded `"state": "DOWNLOADING"` | `819b82c` |
| 14 | Extraction had no completeness precondition | `819b82c` |
| 15 | `_FAILED_` directories accumulated forever | `819b82c` |
| 16 | **rar extraction had never worked** — the image had no RAR decoder | `855e7a3` |

Plus the backup `VACUUM INTO` race (`209928d`) and the pending `DESIGN.md` wording backlog.

## Closed — 2026-08-13

| Summary | Commit |
|---|---|
| The four post-processing toggles' site-wide/per-queue AND (raised as "Awaiting a decision from the user," now resolved and removed from this file) replaced with inherit-or-override; migration 015, and `db.py.migrate()` now disables `PRAGMA foreign_keys` for the whole batch of pending migrations after the table-rebuild this needed turned out to cascade-delete `item`/`pattern` — see `docs/decisions.md` | `prompts/done/2026-08-13-postprocess-inherit-or-override.md` |
| `re_download_externally_removed` could queue doomed jobs on a `move` queue, and `resolve_absence` never wrote `REMOVED_BOTH` — the same underlying question, closed together; see `docs/decisions.md` and `DESIGN.md` §3.2 rule 3 | `prompts/2026-08-13-vanished-rows-should-leave-the-tree.md` |
| A vanished-both row (`56ec523`'s own fix) stayed *published* in the Files tree forever once it reached a terminal removed state, instead of leaving it once `_project` stopped being handed it — the sweep must keep *writing* the row (unchanged) but stop *publishing* it once terminal | `prompts/2026-08-13-vanished-rows-should-leave-the-tree.md` |
| Archive volumes optionally deleted after extraction, without breaking completeness | `4533617` |
| Delete marks the whole subtree; `REMOVED_LOCAL` vs `REMOVED_BOTH` chosen per row | `b39158e` |
| A `move`-mode outcome survives the rescan that finds it `LOCAL_ONLY`; items that leave both trees no longer freeze | `56ec523` |
| Files row revamp — lifecycle icons, inline progress, sorting, persisted collapse | `8a54475` |
| Item detail drawer, reachable from Files — `local_mtime`, lifecycle chronology, bounded history | `de85753` |
| DESIGN.md backlog applied; three long-standing untruths corrected | `cad5891` |
| Delete/removal state honest under a slow delete, a returning release, and a stuck `PARTIAL` child | `7dc045f` |
| Per-queue archive cleanup, site-setting readout, settings-merge fix for silent resets | `0781352` |
| Header sorting, facet filter, settle countdown, queue position, dashboard timeframe | `38efaaa` |
| Vanished rows stop being published once terminal; `REMOVED_BOTH` gap closed; Files columns drag-resizable; clipped labels shortened | `a4a626d` |
| Empty Files tree says what it means instead of "Nothing scanned yet" | `f1f4009` |
| **Delete now stops an active transfer instead of refusing** — and the `.lftp` temp file of a mid-transfer loose file is removed too | `21c41b0` |
| Both-sides remote/local hover card, sharing one formatter with the item drawer | `f4a4205` |
| Settle display split into "still arriving" (with the climbing byte count) and "waiting to settle" | `1d651ed` |
| **SSH key can be pasted, encrypted at rest**, decrypted in memory for asyncssh and materialised to tmpfs per-job only where lftp needs a file | `6359569` |

Three of those came from a second round of live testing after the first push, and two found
bugs nobody was looking for: `shutil.rmtree` was blocking the event loop for the whole duration
of a large delete, and **every save from Settings → Post-processing had always silently reset
`failed_retention_enabled`/`failed_retention_days`**, because those fields have no frontend
form entry at all and the PUT replaced rather than merged.

---

## Closed — 2026-08-14

| Summary | Commit |
|---|---|
| **`lftp` exiting 0 was treated as proof a transfer completed** — a live incident left one file 500 MB short as a `.lftp` temp file and the item still reached `DOWNLOADED`; a filesystem completeness check (exclusion-aware, so a `file_exclude`d file can't hold an item `PARTIAL` forever) now gates `DOWNLOADED`, `output_tail` is retained on every success instead of nulled, and the Transfers page surfaces a recently-succeeded job instead of it vanishing on reap. `bytes_total` is also now frozen at job spawn rather than drifting with a later scan. The `bytes_start` anomaly this same incident surfaced is a separate, unreproduced defect — see "Still open" above. | `prompts/done/2026-08-14-exit-zero-is-not-completion.md` |

---

## The four worth reading before touching this code

### rar extraction had never worked, for any release

Alpine's `7zip` is built **without the RAR codec** — distros strip it because 7-Zip's RAR
decoder derives from unRAR source. `7zz i` listed `zip`, `7z`, `tar`, `gzip`… and no `Rar`. So
every rar release failed with `Cannot open the file as archive`, which reads exactly like a
corrupt download and is not.

The Dockerfile comment claimed rar support. `DESIGN.md` §6 specified it. `core/extract.py`'s
multi-volume machinery was dead code. It survived nine phases because **no test ever built a
real rar** — every fixture was fake bytes.

Fixed with `unrar` built from RARLAB source (SHA256-pinned, statically linked — the dynamic
build compiles and then fails at runtime, which only building and running the image reveals).
Guarded by two hand-built real RAR4 fixtures, cross-validated against a desktop 7-Zip that does
have a RAR codec. **Do not replace them with fake bytes.**

### A "bug" that was not one

`REMOVED_LOCAL` being excluded from `ELIGIBLE_STATES` was reported as a bug by the orchestrating
session, implemented faithfully, and reversed hours later when it turned out to cause an
infinite re-download loop: an `*arr` import moves files out → the remote copy still exists and
the pattern still matches → re-fetched → imported again → forever.

Two ways a local copy disappears, and they are not the same:

1. **lftpweb deleted it** — `REMOVED_BOTH`/`REMOVED_LOCAL` **and** `auto_queue_suppressed = 1`.
   Never re-queued, under either setting.
2. **Something else moved it** — an importer, a human, a script. Unsuppressed.

`6d3bd95` made case 2 an opt-in setting, default off. **The lesson: an exclusion that looks
like an oversight may be the entire safety mechanism.**

### State and reality are different things

`item.state` is one enum carrying five orthogonal facts. That is the root cause of most of this
session's bugs: `LOCAL_ONLY` clobbering `EXTRACTED`, `REMOVED_BOTH` overloaded, a deleted item
unable to say "gone locally, still on the seedbox", `DOWNLOADED` rows claiming absent files.

The 2026-08-13 answer is **not** to rewrite the state machine — it drives auto-queue and
post-processing and has real coverage. It is to add a truthful display projection beside it:
`core/itemview.py` computes R/L/V/E facets from persisted columns.

**Presence icons (R, L) read the world and may go dark. Milestone icons (V, E) read timestamps
and stay lit.** Collapse that distinction back into one notion and the bug class returns.

### The settle gate

Fingerprints each top-level item's remote subtree as `(file_count, total_bytes, max_mtime)`.
Settled needs **both** `REQUIRED_SETTLE_SCANS` (2) **and** `SETTLE_MIN_AGE_S` (60s wall clock)
— the time floor exists so a fast per-queue scan interval cannot shrink the window.

Two gates: auto-queue eligibility **and completion**. The completion half is what stops a
directory reading `DOWNLOADED` off a partial upload and being extracted, moved, and imported at
one-third of a season. A manual Queue click overrides eligibility, never completion.

**On by default** — the third reasoned exception to "new capabilities ship off."

A scan carrying a partial-scan warning must never advance the counter: GNU `find` exits nonzero
on one unreadable subdirectory and still prints what it found, so two identical truncated scans
would otherwise read as settled.
