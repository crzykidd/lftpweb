# Open issues — 2026-08-12 / 2026-08-13 session

A living list, not a handoff prompt. It never moves to `done/`.

**Everything raised on 2026-08-12 and 2026-08-13 is closed.** Sixteen commits, tests
**489 → 701**. Closed items are kept below with the commit and the reasoning, because the
*why* is what is worth finding again — one fix was shipped and deliberately reversed the same
night, and the reasoning for that reversal is more valuable than the diff.

Almost every item came from the user running the app against a real seedbox and reporting what
looked wrong. **CI was green before each one was found.** That is the pattern of this project
and it held all night.

---

## Awaiting a decision from the user

### Post-processing settings: site-wide *and* per-queue, for four toggles

Raised by the user 2026-08-13: *"if we have these settings per queue then why have some of them
here?"* Scanned and written up; **not built — this changes behaviour and is the user's call.**

Only **four** settings are duplicated, and they are ANDed
(`core/postprocess.py.process_item`): `verify_enabled`/`auto_verify`,
`extract_enabled`/`auto_extract`, `move_enabled`/`auto_move`,
`delete_archives_after_extract`/`auto_delete_archives`.

Everything else site-wide is genuine *configuration* with no per-queue twin —
`verify_hash_on_disk`, `extract_target_dir`, `extract_passwords`, `concurrency`,
`failed_retention_enabled`/`_days`. Nobody is confused by those and they should stay put.

Three options:

1. **Status quo** — keep the AND, rely on the "System setting: off" readout added in `0781352`.
   No work, but it papers over the confusion instead of removing it.
2. **Site toggle becomes the default for new queues, not an AND gate.** The queue owns the
   decision; the site value is the template applied at creation. "Toggle on but nothing
   happens" becomes structurally impossible. **The migration can preserve behaviour exactly** —
   set each queue's toggle to `(site AND queue)` as it currently resolves, then stop ANDing, so
   no existing install changes.
3. **Drop the site toggles entirely.** Simplest model; costs a global kill switch.

**Recommendation: 2.** Keeps set-policy-once convenience, makes a queue's behaviour readable
from one screen, migration is behaviour-preserving. The only loss is a global off switch, which
is better expressed as one explicit "pause post-processing" control than as four ANDs that each
fail silently — the user has been bitten by exactly that twice.

Under all three, `sync_mode == 'move'` still forces verification on regardless of both layers,
because it gates an irreversible remote delete.

## Still open — read these first

### No frontend test runner exists — at all

No vitest, no jest, nothing, anywhere in the tree. The 2026-08-13 Files revamp added sorting,
a persisted collapse preference, and progress arithmetic — all written as pure, isolable
functions specifically so they *could* be tested, and all with **zero automated coverage**.

Every backend behaviour this session has tests. None of the frontend logic does. Adding a
runner is a bigger infra decision than any single task should make unasked, which is why three
separate agents declined to. It now needs a decision.

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
