# Open issues — 2026-08-12 / 2026-08-13 / 2026-08-14 sessions

> **2026-08-14: `v0.1.0` shipped.** The first tagged release, a beta. Everything below that is
> not struck through is post-release work. The three items most worth a fresh session's
> attention are **issue #2** (`move` deletes the remote before extraction runs — now exercised
> on every release, not just checksummed ones), **`AuthSettingsIn`'s silent-reset shape** (a
> trusted-CIDR list that can quietly empty itself), and **row lifetime / issue #1**.

A living list, not a handoff prompt. It never moves to `done/`.

**Everything raised on 2026-08-12 and 2026-08-13 is closed.** Sixteen commits, tests
**489 → 701**. Closed items are kept below with the commit and the reasoning, because the
*why* is what is worth finding again — one fix was shipped and deliberately reversed the same
night, and the reasoning for that reversal is more valuable than the diff.

**Everything raised on 2026-08-14 is closed too**, across **thirty commits**
(`a75dc38`…`61f1f1a`). Tests **701 → 1036 backend / 266 frontend**. The queue is empty.

That day split in two: an overnight unattended run through a pre-written queue, then an afternoon
of the user click-testing a real seedbox. The afternoon produced **eight more defects, none
reachable by CI** — and **five of them shared one root cause**, the logical-vs-physical path
assumption, fixed at source in `0e93fab`.

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
- ~~**A `dev` → `main` PR, and the first release.**~~ — **done 2026-08-14: `v0.1.0`, a beta.**
  PR #3 merged, tag `v0.1.0` cut from `main`, images published. `release-prep`/`release-cut`
  have now both been exercised end to end. See `prompts/startnewsession.md`'s branch section for
  the two things that tripped the first run (the PR-body character limit, and `/release-prep`
  being forbidden from touching `DESIGN.md`).
- **Should the GitHub release be flagged as a prerelease?** `v0.1.0` was published with
  `prerelease=false`. It *is* a beta — `README.md` says so and the changelog says so — but `0.x`
  already signals pre-1.0 under semver, so this is defensible either way. Raised and not decided;
  it is a one-line flip if wanted, and the same question will recur at `0.2.0`.
- ~~**Should "Folder prefix during transfer" default ON?**~~ — **decided 2026-08-14: yes.** The
  fourth deliberate exception to "every new capability ships off", after `move`-mode forced
  verification, the phase 7 scheduled backup, and the settle gate. Same reasoning as the settle
  gate's own flip: it fixes a *reproduced* defect rather than adding a preference, so an existing
  install silently keeps running with that defect live unless it defaults on. Flipped hours after
  it shipped off in `342f96c`. Existing installs will notice — see `CHANGELOG.md`'s `### Changed`
  entry.
- ~~**Two `DESIGN.md` §4.3 wordings are drafted and unapplied.**~~ — **both were already applied
  on 2026-08-14**; this entry was stale and is kept only as a caution. Verified 2026-08-15 by
  reading `DESIGN.md` §4.3 against the drafted text in `docs/decisions.md`: the exit-0 rewrite
  is at §4.3's first bullet, `LOCAL_FS_ERROR` is in both the classify list and the transient
  retry list, all verbatim. `docs/decisions.md` marks each draft **APPLIED 2026-08-14** at the
  point of the draft itself — that is the authoritative record; this file simply never got the
  matching edit.

  **The caution, which is the reason this entry survives at all:** a drafted-wording entry here
  is not evidence the wording is still pending, and it cost a session's worth of "this must land
  before the release" advice that was already untrue. Check `DESIGN.md` itself before repeating
  any claim from this list about what the design doc does or doesn't say.

## Still open — read these first

### Admission holds the 2nd job because a lone 1st job grabs the whole ceiling (allocation ≠ actual throughput)

2026-08-15, user report against the real seedbox — the pattern that finds every real bug in this
project. Config: **Max Bandwidth 50 MB/s, Max Concurrent Jobs 2**, files-in-parallel 2,
connections-per-file 20. Two folders land a few seconds apart. The first starts; the second
**queues and never auto-starts** until the first finishes, even though a slot is free.

**Root cause — `core/scheduler.py.admit()`.** Admission gates on `if ready > 0 and headroom > 0`,
where `headroom = ceiling − small_lane_reserve − Σ(running allocations)`. A job admitted *alone*
gets `share = headroom / ready` with `ready == 1` — i.e. **essentially the entire ceiling**
(~49 MB/s of 50). That allocation is written to lftp as `net:limit-total-rate`
(`core/lftp.py`), a **ceiling, not a reservation**, and allocations are never re-shaped for a
running job (the §4.5 invariant). So when the 2nd folder lands, `headroom = 50 − reserve − 49 ≈ 0`
and it's parked — despite the free slot.

**Why the user is right that there's real headroom.** A single transfer to their seedbox tops out
at ~15 MB/s (per-transfer/SSH limit; 20 connections don't beat it), so the 49 MB/s ceiling on job
A is **inert** — never reached. The link actually has ~35 MB/s idle, but the scheduler accounts
for *allocated* bandwidth, not *measured* throughput (deliberately — §4.5's "no live control
channel"), so it can't see it. **No setting fixes this**: raising or lowering Max Bandwidth doesn't
help, because a lone first job always takes ~all of B whatever B is. The only current escape is
**"Start now"** on the parked item (runs it, ignoring the cap).

**The real fix (scheduler).** Allocate each job an equal **per-slot slice** —
`(ceiling − reserve) ÷ max_concurrent_transfers` — instead of `headroom / ready`, so a lone job
takes at most 1/N and leaves headroom for the other slots. For this user (~22 MB/s cap vs 15
actual) the cap is inert and both folders simply run. **Tradeoff to decide, not assume:** a *solo*
transfer that genuinely *can* saturate the link would be capped to 1/N when running alone — so this
likely wants to be a **toggle** ("split bandwidth evenly across job slots" vs today's greedy first
job), not a silent change for everyone. Small, well-contained change with a pure-function test
(`admit()` is already table-tested against every §4.5 worked example — add the staggered-arrival
case).

**Cheaper interim mitigation (UX) — the user's own suggestion.** The header
(`StatsHeader.tsx`) already shows **Allocated X / ceiling** and **Queued N**, and its docstring
even calls allocated-vs-ceiling "the honest answer to why the next item hasn't started." But a user
seeing **Speed 15 · Allocated 49/50 · Queued 1** doesn't connect the low speed to the full
allocation. Add a plain-language hint (a `title=` tooltip, or an inline note shown only when
`queued_count > 0` and `allocated_bps` is near `ceiling_bps` while `current_speed_bps` is well
below it): "Transfers are admitted against *allocated* bandwidth, not current speed — a running job
reserves up to the ceiling even if it isn't using it all, so the next item waits until bandwidth
frees. Use *Start now* to override." Ship this first; its wording will depend on whether the
scheduler toggle above lands, so do them together or the note before the fix.

### Post-`v0.1.0` audit — the deferred items (`docs/audit-v0.1.0.md`)

A full audit landed 2026-08-14 (`docs/audit-v0.1.0.md`): findings **S1–S4** (security),
**G1–G3** (settings/gating), **P1–P5** (partitioning). An overnight run on the same day closed
the fixable ones — **S1** (SPA path-traversal file read, `01efac4`), **S3+S4** (input caps +
security headers, `0a4593a`), **S2** (extraction containment, `65b0618`), **P2** (`api/settings.py`
split, `90df1ea`), **P3** (`core/local_delete.py` split, `d480885`), and **P1 in part** (pure logic
→ `lib/fileTree.ts`, `0cb294f`). The full audit doc has every finding's detail and the fix commit.
**What's left, deliberately, for a session with the user in the loop:**

- **G1 — `move`-mode delete runs before extraction.** This is the same thing as **issue #2**
  below (a no-sidecar release verifies `SKIPPED`, its remote is deleted, then extraction may
  fail — the only re-fetchable source already gone). It's a *design call*, not a mechanical fix:
  either delete after a successful extract, or make the delete gate also require extraction not to
  have failed (mirroring the download-prefix `release_ok` rule). Don't resolve it unilaterally.
- **G2 — `net:connection-limit` has no write path.** §4.5 calls it "first-class, host-level," but
  it lives only in the `host.connection_overrides` JSON blob with no UI/endpoint. Needs a
  migration (promote to a real column) + a Settings field. A scoped feature, not a one-liner.
- **Rest of P1 — the `FileTree.tsx` component extractions.** The pure logic is out; the `Row`,
  hover-card, and column-resize *components* remain (still ~1765 lines). Left deliberately: they
  have no unit coverage, and a prop/closure slip only shows when rendered — wants a browser to
  verify, which the coding environment doesn't have.
- **P4/P5 — `core/queue.py` (1881) and `core/engine.py` (1621) splits.** The deepest stateful
  code (the transfer god-object; the scan→persist→publish invariant). The audit itself said do
  these under review, not unattended. Each is a handoff-prompt-sized task on its own.

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

### Some of the UI has now been seen — and six screens are photographed

**2026-08-14: the first visual review this project has ever had.** The user click-tested a real
instance and captured six screenshots, now in `docs/images/` and described in
`docs/screenshots.md`. An agent *reviewed those images* (they can be read, even though no browser
exists to drive), which is how several of the afternoon's defects were characterised.

Confirmed by eye and working: the grey `Extracted` chip vs the emerald one, the `Missing · Xm`
countdown, per-file speed and ETA, the inline progress chip, the item drawer's prefixed-path note,
and the corrected `7zz`/`unrar` extraction label.

**Confirmed by eye 2026-08-14 (second pass)** — the user click-tested each and reported no
defects: the **Speed column** at its widened 128px `defaultWidth`, the **column resize handles**
on each column's left edge, the **unified reset control's** three scopes (All / Pattern /
Selected), the **effective-lftp-settings `<details>` panel**, **Settings → Queues**, and
**Docs → How it works**. The three Files-page items had been reasoned from CSS only until now.

**Progress-bar text contrast at ~50% fill is settled** — the user's call on 2026-08-14 is that
it's fine. It had been flagged unverified since 2026-08-13. Not a task any more; don't re-raise it
without a new observation.

**The "never viewed" list is now empty.** Every screen shipped through 2026-08-14 has been looked
at by a human at least once. That is a first for this project, and it is a floor, not a
guarantee — "viewed once, no obvious defect" is weaker than tested, and anything shipped after
this date starts unviewed again unless someone says otherwise.

**No agent can drive a browser.** Every UI claim in this repo still means "builds, type-checks,
lints, and the endpoints it calls were verified over real HTTP" unless a human or a screenshot
proves otherwise.

### ~~A cleaned-up archive rests in a different state depending on sync mode~~ — **closed 2026-08-14**

`prompts/2026-08-14-extracted-archives-rest-as-extracted.md`, executed end to end
(`docs/decisions.md` has the full reasoning). The live trigger was worse than "different state
depending on mode" by the time this was picked up: a `move` queue's cleaned-up volumes were
running the ten-minute removal-grace clock and showing an alarming `Missing · 9m` countdown for
files deleted on purpose, nine seconds after extraction succeeded.

Fixed at the one place a deleted archive volume could ever reach that clock —
`core/engine.py._persist`'s "vanished from both trees" sweep, reachable only on `move` queues
(a `copy` queue's surviving remote volume already read `EXCLUDED` correctly via `reconcile()`'s
own predicate, before `_persist` ever saw it). Both sync modes now rest at `EXCLUDED`
identically, never through the grace clock. The Files page tells this `EXCLUDED` apart from an
ordinary pattern-excluded file via a new `deleted_archive_at` wire field and renders a
greyed-out `Extracted` chip instead of the misleading `Excluded` — a display projection, per
this project's own established pattern (`core/itemview.py`'s R/L/V/E facets); no new `state`
value, `EXCLUDED` not overloaded with new meaning.

**The summary row this was blocking is still not built.** The user floated collapsing archive
volumes into one row — *"14 archive volumes · removed after extraction"*, expandable, so the
screen stays clean but the provenance of the `.mkv` is still visible. This task unblocked its
stated prerequisite (one consistent resting state to summarize) but did not build the row
itself, per the task's own instruction to stop rather than force it if it doesn't fall out
cleanly against `FileTree.tsx`'s virtualization, sorting, and persisted collapse preference —
it doesn't: every row in that pipeline is a real `item` row with its own `id`/`rel_path`, which
multi-select, sorting, and the collapse map all assume, and a synthetic grouping row with none
of that is a structural change, not an additive one. Still open, now blocked by that real
complexity rather than by "no consistent state to summarize." Their own note at the time:
*"Not sure on this."*

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

### ~~The folder prefix and the settle gate's stuck-item recovery don't compose~~ — **closed 2026-08-14**

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

**Closed by `prompts/done/2026-08-14-map-the-download-prefix-not-filter-it.md`, not by the
"tempting fix" this section rejected.** The root cause named above — the reconciler being blind
to its own in-flight working directory — is what got fixed, not worked around a second time:
`scan_local` now **maps** a prefixed directory onto its logical rel_path instead of filtering it
out of the walk, so the item genuinely computes structurally `DOWNLOADED` once its bytes are all
present, whether or not auto-queue is on to paper over it. Reproduced directly against the real
fake seedbox and proven to fire the `unstuck` path itself, not just inferred:
`tests/test_download_prefix_e2e.py::test_engine_scan_unsticks_a_settled_item_whose_bytes_are_
still_prefixed`. See `docs/decisions.md`'s entry for that task for the full reasoning and every
consumer checked.

### Smaller, and genuinely optional

- **Row lifetime** — nothing deletes `item` rows. [Issue #1](https://github.com/crzykidd/lftpweb/issues/1).
  The non-obvious part: pruning does not delete history, it *hollows it out*, because
  `job`/`event` reach `rel_path` and queue name through `LEFT JOIN`s.
- **Retention has no Settings UI** — API and dry-run preview only.
- **`re_download_externally_removed` is site-level, not per-queue**, though
  `auto_queue_enabled` and `sync_mode` are both already per-queue columns and it only matters
  for `copy` queues. Needs a migration.
- **`move` deletes the remote *before* extraction runs.**
  [Issue #2](https://github.com/crzykidd/lftpweb/issues/2) — filed 2026-08-15 with the full
  reasoning, the proposed fix, and the tradeoff. A failed extraction happens after the remote
  copy is gone; local archives survive, so it is recoverable. The user's call on 2026-08-13 was
  "leave it, revisit if it bites." **It bit on 2026-08-14.** Deferred again on 2026-08-15 —
  deliberately, to an issue rather than a prompt.

  **`6883db3` widened its exposure**: a sidecar-less release used to be withheld at the delete
  gate and never reached this window at all; now it deletes like any other, so the gap between
  "remote gone" and "archives proven extractable" is exercised on every release. Not a
  regression from that commit, but the reason this is worth more than it was when first
  deferred. Do not reorder it as a side effect of unrelated work.
- **Encrypted-rar password retry is implemented but untestable** — no compressor exists
  anywhere to build an encrypted fixture. Real-archive rar coverage is old-style `.r00` only,
  not `.partNN`. **The user generated real `.partNN.rar` sets on 2026-08-14** (visible in that
  day's History screenshot) — capturing one small set as a committed fixture would close the
  new-style half of this gap.
- **The `archive_cleanup` event message is a wall of text** — it lists every removed volume path
  inline, and on the History page a twelve-volume release dominates the panel (visible in
  `docs/images/history-audit-trail.png`). Wants a count plus an expand.
- **A `move` queue can run 56 concurrent SFTP sessions without warning.** Settings → Transfer's
  own readout says so plainly (`2 jobs × 2 parallel × 14 pget-n`), but the "over the limit"
  warning beside it can never fire, because `net:connection-limit` has no write path (see
  `README.md`'s Known gaps). The readout is honest; nothing enforces anything.
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
| **A `move`-mode delete was withheld on `SKIPPED` verification, not only on `CORRUPT`** — two `ar-tv` WEB-DL releases downloaded correctly and their remote copies were never cleaned up, because with no `.sfv`/`.md5` sidecar and hash-on-disk off, verification returns `SKIPPED` and the gate required `VERIFIED`. The rule is now "verification must not have *failed*". Found by the user using the app; diagnosed in one call to `GET /api/history/events`, which had already recorded the reason. `core/postprocess.py`'s rename gate had used the correct rule all along, on the *more* dangerous side of the decision. | `6883db3` |
| **Both filename-guard regexes anchored with `\Z` instead of `$`**, after five CodeQL `py/path-injection`/weak-hash alerts on PR #3 were verified as false positives and dismissed. `$` also matches before a trailing newline, so `"lftpweb.log\n"` passed a pattern documented as anchored at both ends. Unexploitable, but those patterns are what the dismissals rest on. +18 parametrized tests at the regex itself. | `b06cafe` |
| **Two stale trackers** claimed `DESIGN.md` wordings were still pending after they had shipped — and the claim was repeated as pre-release advice before anyone opened the doc. Both corrected and kept as cautions. | `3281e48` |
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
