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

## Still open — read these first

### 🔴 `re_download_externally_removed` can queue doomed jobs on a `move` queue

Found by the documentation sweep on 2026-08-13, and it contradicts something the orchestrating
session asserted to the user earlier that evening.

The claim was: the setting is a no-op for `move` queues, because the remote copy is already
gone so there is nothing to re-fetch. **That is true of the intent and false of the code.**
`core/autoqueue.py`'s eligibility query selects on `state` and `auto_queue_suppressed` and
**never consults the current remote tree**. So a `move`-mode row sitting at bare, unsuppressed
`REMOVED_LOCAL` becomes eligible the moment that setting is turned on, and produces a transfer
job against a remote that no longer exists.

The setting defaults **off**, so nothing is broken today. Recorded in `DESIGN.md` §3.2 rule 3,
`README.md`'s Known gaps, and the changelog.

### `resolve_absence` never writes `REMOVED_BOTH`

`core/mount_sentinel.py.resolve_absence` always writes the literal `"REMOVED_LOCAL"`, taking
neither `sync_mode` nor `remote_deleted_at` as input. So a fully-completed `move`-mode item
that leaves both trees lands at `REMOVED_LOCAL`, not the `REMOVED_BOTH` that `DESIGN.md` and
`core/autoqueue.py`'s comments both describe.

Latent until `56ec523` — before that, such items never reached the function at all. Widening it
is a real design decision (it must also decide whether to set `auto_queue_suppressed`), so it
was documented rather than guessed at. **This and the item above are the same underlying
question**: what a completed `move` item's terminal state should be, and whether suppression
belongs with it. Answer them together.

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
| Archive volumes optionally deleted after extraction, without breaking completeness | `4533617` |
| Delete marks the whole subtree; `REMOVED_LOCAL` vs `REMOVED_BOTH` chosen per row | `b39158e` |
| A `move`-mode outcome survives the rescan that finds it `LOCAL_ONLY`; items that leave both trees no longer freeze | `56ec523` |
| Files row revamp — lifecycle icons, inline progress, sorting, persisted collapse | `8a54475` |
| Item detail drawer, reachable from Files — `local_mtime`, lifecycle chronology, bounded history | `de85753` |
| DESIGN.md backlog applied; three long-standing untruths corrected | `cad5891` |

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
