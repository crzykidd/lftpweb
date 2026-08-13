# Open issues — raised 2026-08-12 (post-phase-9, first real-use session)

A living list, not a handoff prompt. It never moves to `done/`.

**Every item raised on 2026-08-12 is now closed.** They are kept below with their reasoning
and the commit that closed them, because the *why* is the part worth finding again — several
were closed in a way that is not obvious from the diff, and one was closed, shipped, and then
deliberately reversed the same night.

Nearly all of them came from the user actually running the app against a real seedbox. That is
the same pattern the 2026-08-12 session already recorded: nine phases of green CI while real
defects sat in plain sight on the first screen a human opened. It held again — CI was green on
every one of these before they were found.

---

## Closed

| # | Summary | Closed by |
|---|---|---|
| 1 | No `busy_timeout` on the shared connection — any `SQLITE_BUSY` failed instantly | `cd74f91` |
| 2 | **No settle gate** — a partial upload read as complete; directories never self-healed | `9b11df6`, finished in `855e7a3` |
| 3 | `verify_hash_on_disk` blessed a partial file, which could authorize a `move`-mode remote delete | `9b11df6` |
| 4 | `REMOVED_LOCAL` not in `ELIGIBLE_STATES` | `dfb74c2`, then **reversed** in `6d3bd95` — see below |
| 5 | `EXTRACTED` claimed for items containing no archives; false `extracted_at`; overwrote a real `VERIFIED` | `819b82c` |
| 6 | `state_changed_at` + trigger; Files column with relative time | `57f7ce9` |
| 7 | Local retention cleanup | `dfb74c2` |
| 8 | Manual delete-local in Files (per-item + bulk) | `dfb74c2` |
| 9 | Expand/Collapse all gave no reason when disabled | `cd74f91` |
| 10 | "Rescan now" spinner was a 1-second timer, not a completion signal | `cd74f91` |
| 11 | Per-queue scan interval (10/30/60/none) | `c8d3e8b` |
| 12 | Child files under a mirroring directory only updated every 30s | `819b82c` |
| 13 | `_sample_and_publish_progress` hardcoded `"state": "DOWNLOADING"` instead of using `itemview.py` | `819b82c` |
| 14 | Extraction had no completeness precondition | `819b82c` |
| 15 | `_FAILED_` directories accumulated forever, hidden from scans | `819b82c` |
| 16 | **rar extraction had never worked** — the image had no RAR decoder at all | `855e7a3` |

Also closed the same night: the backup `VACUUM INTO` race (`209928d`, carried over from the
previous session), and the whole pending-`DESIGN.md`-wording backlog (`855e7a3`, `6d3bd95`).

---

## The three worth reading before touching this area again

### 16 — rar extraction had never worked, for any release

Alpine's `7zip` package is built **without the RAR codec** — distros strip it because 7-Zip's
RAR decoder derives from unRAR source. `7zz i` in the image listed `zip`, `7z`, `tar`, `gzip`,
`bzip2`, `Lzh`, `Cab`, `Iso`, `SquashFS`… and no `Rar`. So every rar release failed with
`Cannot open the file as archive`, which reads exactly like a corrupt download and is not.

`docker/Dockerfile`'s own comment claimed rar support. `DESIGN.md` §6 specified it.
`core/extract.py`'s multi-volume rar machinery was dead code. **Nine phases of green CI missed
it because no test ever built a real rar** — every fixture was fake bytes (`b"volume 1"`),
exercising naming and precondition logic only.

Fixed by building `unrar` from RARLAB source in a dedicated Dockerfile stage (SHA256-pinned,
statically linked — the dynamic build compiles fine and then fails at runtime with
`Error loading shared library libstdc++.so.6`, which only building and running the image
reveals). Alpine 3.24 has no `unrar`, `unar`, `p7zip`, or `unrar-free` in main or community;
`libarchive-tools` exists but its RAR support is read-only and weak on multi-volume sets.

Licence: UnRAR permits redistributing the compiled binary with attribution and forbids only
using the *source* to build a RAR compressor. Recorded in `NOTICE`.

**The regression guard is the point.** Two hand-built real RAR4 fixtures (single-file, and a
genuine two-volume `.rar`+`.r00` split with correct `SPLIT_BEFORE`/`SPLIT_AFTER` flags),
cross-validated against a desktop 7-Zip that *does* have a RAR codec before being committed.
Nothing in this repo can create a rar, so these were constructed byte by byte. **Do not
replace them with fake bytes.**

### 4 — closed, shipped, and reversed the same night

`REMOVED_LOCAL` was added to `ELIGIBLE_STATES` in `dfb74c2` as a fix. **The framing was
wrong** — it was the orchestrating session's error, faithfully implemented.

There are exactly two ways a local copy disappears:

1. **lftpweb deleted it** — `core/local_delete.py` writes `REMOVED_BOTH` **and**
   `auto_queue_suppressed = 1`. Never re-queued. Correct, unchanged.
2. **Something else moved it** — an `*arr` importer, a human, a script. Reaches
   `REMOVED_LOCAL` through §7.3's grace period, unsuppressed.

Making case 2 eligible produced an infinite loop on a `copy` queue with auto-queue on: import
moves the files out → the remote copy still exists and the pattern still matches → re-fetched
→ imported again → repeat every scan interval. Bandwidth, seedbox load, duplicate imports.
Excluding `REMOVED_LOCAL` is exactly what §3.2 rule 3 existed to do.

The narrower worry that motivated it — a half-imported release whose stragglers can never be
fetched — **is handled by the settle gate**, which stops that release being marked
`DOWNLOADED` off a partial remote set in the first place.

`6d3bd95` made it an opt-in setting, `re_download_externally_removed`, **default off**, scoped
by *who removed the file*. It only ever matters for `copy` queues: on `move` the remote copy is
already gone, so there is nothing to re-fetch.

### 2 — the settle gate, as actually built

Fingerprints each top-level item's whole remote subtree as
`(file_count, total_bytes, max_mtime)`. Settled requires **both** `REQUIRED_SETTLE_SCANS` (2)
**and** `SETTLE_MIN_AGE_S` (60s wall clock) — the time floor exists so the per-queue scan
interval (#11) cannot shrink the window by polling faster. Unsettled items are held at
`REMOTE_ONLY`/`substate='settling'`.

Two gates, not one: auto-queue eligibility **and completion**. The completion half is what
fixes the directory case; queue-only would leave it open for manually-queued and
mid-upload-arriving items. A manual Queue click overrides eligibility, never completion.

**On by default** — the third reasoned exception to "new capabilities ship off", after
`move`-mode verification and the phase 7 scheduled backup.

Held items self-heal: `_persist` triggers post-processing when a scan releases an item from
`settling` straight to `DOWNLOADED` with no job in between. **This widened the
post-processing trigger contract** (previously job-success only) and is documented in
`DESIGN.md` §6 rather than silently diverged from.

A scan carrying a partial-scan warning must never advance the settle counter — GNU `find`
exits nonzero on one unreadable subdirectory and still prints what it found, so two identical
truncated scans would otherwise read as settled.

---

## Still open — decisions, not bugs

- **`move` mode deletes the remote copy *before* extraction runs, and this is deliberate for
  now.** `core/postprocess.py.process_item`'s order is verify → delete remote → extract →
  relocate. A failed extraction therefore happens after the remote copy is already gone. The
  local archives survive, so it is recoverable rather than data loss — but it means the
  verification gate is doing all the work, and verification proves the *bytes* arrived, not
  that they will *extract*.

  Surfaced to the user 2026-08-13, right after discovering rar extraction had never worked
  (issue 16) — under which every `move`-mode rar release would have had its remote deleted and
  then failed to unpack. **The user's call: leave it, and revisit if it becomes a problem in
  practice.** Do not reorder it as a side effect of some other task; if it is ever changed,
  that deserves its own reasoning about what the delete gate should actually require.

- **Row lifetime** — nothing deletes `item` rows. Filed as
  [issue #1](https://github.com/crzykidd/lftpweb/issues/1) with the non-obvious part: pruning
  a row does not delete history, it *hollows it out*, because `job`/`event` reach `rel_path`
  and queue name through `LEFT JOIN`s. `item_settle` has the same property.
- **No browser has seen most of this.** New and unviewed as of 2026-08-12: the delete
  confirmation panel and bulk delete, the `state_changed_at` column and its ticker, the
  settling badge, the "scanned Xs ago" readout, Settings → Transfer's settle section,
  Settings → Queues' scan-interval dropdown and re-download toggle.
- **Retention has no Settings UI** — API and dry-run preview only. Same "backend first" gap
  Settings → Transfer had before 2026-08-12.
- **`re_download_externally_removed` is site-level, not per-queue.** `auto_queue_enabled` and
  `sync_mode` are both already per-queue columns, and this only matters for `copy` queues, so
  per-queue is defensible. Needs a migration; site-level matches the retention precedent.
- **§9's "TanStack Query for REST" has never been true** — hand-rolled `usePoll`/`fetch` since
  phase 1. Flagged since phase 3b, still undecided: correct the doc or do the migration.
- **§12's file list omits every module added since phase 4** (`verify`, `extract`, `audit`,
  `itemview`, `mount_sentinel`, `settle`, `metrics`, `local_delete`, `logtail`).
- **Stale pointers in code comments** — `core/settle.py`, `core/metrics.py`, and
  `migrations/005` still say "DESIGN.md wording proposed, not applied". They have since been
  applied.
- **Unverifiable without a rar compressor**: encrypted-rar password retry is implemented but
  untested, and only old-style `.r00` multi-volume got a real fixture, not `.partNN`.
