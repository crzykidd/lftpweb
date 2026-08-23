---
name: 2026-08-23-disk-review-scan
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: |
  core/disk_review.py (reconcile/freed_bytes pure, run_scan I/O shell), extended
  core/remote.py (scan_with_inodes) and remote_agent/scan_fs.py (--inodes), new
  api/disk_review.py + models.py response shapes, registered in main.py, new
  Transfers -> Disk review frontend tab. All gates green: 1960 backend tests
  passed, ruff check/format clean, frontend build/lint/vitest (708 tests) green.
  See docs/decisions.md 2026-08-23 entry for the two notable findings (BusyBox
  fallback can supply inodes so no unavailable-degrade was needed; inode-only
  claim attribution needed a second index). Not yet run against the real
  seedbox -- deferred to a live-verification pass before stage 5.
---

# Task: Stage 4 — the disk review scan (review-only, deletes nothing)

> *"Client shows all this on disk… what is in the base folders for the client that don't exist in
> the UI that could be cleaned up with a review option."* — the user, 2026-08-22

Walk the clients' configured base paths over SSH, reconcile against what the clients claim and
what lftpweb itself is using, and present two labelled piles. **This task deletes nothing and
proposes nothing for automatic deletion.** Stage 5 is the delete path; it is not in scope here.

## Before you start

**Read `docs/download-client-framework-spec.md` §11 in full — it is the design, including four
subsections (§11.1a–§11.1d) that each exist because the obvious implementation is wrong.** Also
§1.1 (the reference workflow), §10.5 (hardlinks are the normal case), §8.2 (base paths).

Then:
- **`core/remote.py`** — `_run_primary` drives GNU `find -printf` already; `_run_fallback` is the
  BusyBox path. `interpret_primary_scan_result` treats a nonzero exit *with* stdout as a partial
  success, never a hard failure.
- **`core/pipeline_flight.py`** — its predicate and `in_flight_item_ids()`. **Reuse them. Do not
  write a second definition of "busy."**
- **`core/mount_sentinel.py`** — a failed check must skip a queue entirely rather than reading as
  "everything is orphaned."
- **`core/clientsync.py`** — the poller's cache, where client claims come from.

## The reconciliation

Three sets over the configured base paths:

| Set | What |
|---|---|
| **A** | What **every configured client instance** claims — `content_path` across their transfers, **plus every inode reachable under those paths** |
| **B** | What is on disk under the base paths, via SSH — path, size, **inode, link count, mtime** |
| **C** | What lftpweb itself is using — items, queued/running jobs, pipelines in flight |

- **`B − A − C` → debris.** Unclaimed by any client, unused by lftpweb. The review list.
- **`A − B` → broken seeds.** A client item whose data is gone.

## The four things that are wrong if done obviously

### 1. Set A is a union across clients (§11.1a)

SAB and rTorrent **share the TV completed folder** in the reference workflow. A scan evaluating
one client's claims against a shared folder sees *the other client's entire estate as orphaned*.
With deletion eventually on the other end of this list, **this is the most dangerous mistake
available in the feature.**

- The scan is **per-base-path**, and set A is the union of claims from every client writing there.
- **If any client contributing to a base path is unreachable, disabled, or has not reported
  successfully this pass, propose no debris for that path at all.** §4.2 applied to the scan: a
  client that did not answer has not told us its releases are unclaimed — it has told us nothing.

### 2. Claiming is by inode, not path (§11.1b)

**rTorrent's `content_path` is its seeding directory, not the completed-folder hardlink** it
creates on completion. That hardlink is invisible to rTorrent's API. Path-matching alone flags
**every rTorrent release in the completed folder as debris** — the same catastrophe as #1 by a
different route.

- The SSH walk collects **inode (`%i`) and link count (`%n`)**, not just path and size.
- A file is claimed if **any** link to its inode is claimed.
- A candidate is proposed only when **every** link to its inode is also a candidate. A file with
  `nlink > 1` and an unaccounted-for link is never proposed.
- **Check the BusyBox/fallback scan path for parity.** If it cannot supply inodes, the scan must
  declare itself unavailable there rather than silently degrading to path-only matching — which
  would reintroduce exactly the catastrophe above.

### 3. Debris and the seeding estate are different piles (§11.1d)

rTorrent's downloads directory is **claimed** by live torrents accumulating until the user cleans
them by hand. That is not debris — it is #21's rules-and-ranking problem. Show both, **label them
distinctly, and make only debris selectable.**

### 4. Freed space is link-aware (§10.5)

"7 selected — 312 GB" is a lie if half is still linked from a seeding torrent. Count a file's bytes
only when the selection removes its **last** link. The inode map already built makes this
computable — one walk, both answers.

## Other guards

- **Age floor.** A release written minutes ago that has not yet appeared in a client's list must
  never be proposed. Same instinct as §7.3's removal grace period.
- **Containment.** Only paths under configured base paths are walked or proposed. Ever.
- **Shared save paths** (§10.4): if several torrents share a path, none is a candidate unless all
  are unclaimed.
- **Mount sentinel**: a failed check skips that queue/path entirely.
- **Review-only.** No deletion, no selection persisted as an instruction, no scheduled sweep. A
  manual trigger only — the walk is potentially large and must never ride a page load (§11.3).

## Scope

Backend: the scan itself, its API endpoint, and tests. Frontend: a review page showing the two
piles with per-candidate size and a **link-aware** running total. Selection is allowed; **acting on
a selection is not built in this task.**

## Tests

The reconciliation is pure set math over three inputs — test it exhaustively without SSH:

- Union-across-clients: a two-client shared folder where each client's items are claimed by the
  other's absence — **the §11.1a catastrophe, asserted directly.**
- An unreachable contributing client → **no debris proposed for that path.**
- Inode claiming: a hardlinked completed copy whose only claim is via the seeding path's inode is
  **not** debris — **the §11.1b catastrophe, asserted directly.**
- `nlink > 1` with an unaccounted link → never proposed.
- Link-aware freed-space: selecting one of two links reports **zero** bytes reclaimed.
- Age floor, containment, shared-path, mount-sentinel guards.
- The C set uses `pipeline_flight`'s real predicate — an in-flight item is never debris.
- Broken seeds (`A − B`) surface as their own labelled pile.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — every gate Bash call MUST pass an explicit timeout of at least
600000 ms. Five agents on this feature have stalled on exactly this. **Run backend gates from the
REPO ROOT**; if you `cd`, `cd` back.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. Frontend: `npm run build`, `npm run lint`, `npm test`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md`, update
the spec's §14 staging table.
**Do not commit or push.** Report: files, every exit code, test counts, a proposed one-line `feat:`
message, **whether the BusyBox fallback can supply inodes and what you did about it**, and anything
in the spec found wrong or underspecified.
