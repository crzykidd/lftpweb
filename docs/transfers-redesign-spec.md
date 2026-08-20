# Transfers redesign + download-client integration — spec

**Status: proposal, nothing here is built.** `DESIGN.md` remains the source of truth for what
lftpweb actually does today; this document describes where the Transfers/Files/History surfaces
are going and why, so the work can be staged into prompts without re-deriving the reasoning each
time. Design settled in conversation with the user on 2026-08-19.

Two related changes live here because they interlock: the download-client integration feeds
pre-arrival rows into the redesigned queue view, and the queue view is what makes them worth
having.

**They are phased, and the order is decided (2026-08-19): the UI redesign lands first, in full
(§7, phase 1). The download-client work in §4 is phase 2 and is not started.** §4 is written down
now so the phase-1 design doesn't paint it into a corner — not because it is being built next.

---

## 1. The problem

The current split between **Files**, **Transfers**, and **History** is drawn on an
*implementation* seam — `item` (a thing in the trees) versus `job` (an attempt to transfer it) —
rather than on a user-task seam. To the user those are one object: "this release." The result:

- **Files** is where the user actually lives, because it shows new content landing and per-file
  progress. But it can't show queue position and can't reorder — those exist only on Transfers.
- **Transfers** owns ordering and control, but shows one row per job with no visibility into the
  files inside an item, which is the thing the user opens Files for.
- **History** overlaps the item drawer: the drawer already shows recent transfers and audit
  events for one item, which is a per-item slice of what History shows globally. Neither is
  obviously canonical.
- **Grouping by queue is actively misleading** — see §3.1.

## 2. Navigation shape

**Transfers becomes the main section**, with two tabs:

| Tab | Answers |
|---|---|
| **Queue** | What is moving, and in what order? |
| **Files** | What exists — the full merged remote/local tree, for when detail is wanted |

**History becomes Events** — the audit-event log only. Its jobs list is dropped, because the
Queue tab's completed box now covers "what finished, in what order." Events keeps what nothing
else has: remote deletes, deletes withheld, verify outcomes, notify failures, the forensic trail.

**Per-item Events deep link.** A row on either tab gets an "Events" affordance that opens the
Events page pre-filtered to that item. This is what allows the item drawer to stop duplicating
the audit trail — one canonical place, reachable in one click from anywhere.

### Files is demoted, not removed

Files stops being the progress-watching surface. It stays because it is the only view that shows
**things with no job** — `REMOTE_ONLY` items sitting on the seedbox that were never queued
because no pattern matched or auto-queue was off. If the Queue tab only shows what entered the
pipeline, nothing shows what didn't. Files is also the only home for Delete and the only
tree-shaped view of the remote.

## 3. The Queue tab

### 3.1 One list, not one section per queue

**Grouping by queue is dropped.** This is a correctness fix, not a preference:
`core/scheduler.py` contains **zero** references to `queue_id` — admission is entirely
queue-agnostic. There is exactly **one** global line, ordered `rank DESC, queued_at ASC`.
Grouping by queue visually implies each queue has its own line and its own ordering. It does not.

This also explains the current oddity where positions inside one queue group read `#3`, `#7`,
`#11` — the *numbering* was honest; the *grouping* was the lie.

**This reverses a documented decision** (`prompts/done/2026-08-16-transfers-group-by-queue.md`),
which introduced grouping because *"per-row queue labels make the page busy."* That reasoning was
sound in a world with no filter. The name filter shipped 2026-08-19 changes it: a row needs far
less queue signal when the user can isolate a queue on demand. `docs/decisions.md` must record
the reversal and its cause rather than silently contradicting the earlier entry.

**Consolidation, not loss:** dropping grouping also drops `GroupHeader` and the per-queue
"Dismiss Queue" control (v0.2.3, `278e10f`). The filter + scoped "Dismiss list" already
supersedes it — filter to a queue, dismiss the list.

### 3.2 Two boxes, paginated

| Box | Page size | Ordering | Pagination |
|---|---|---|---|
| **Active / pending** | 20 | true admission order | client-side — the set is bounded and already fully loaded |
| **Complete** | 50 | most recently finished first | **server-side — reuse `api/history.py`'s existing paginator**, do not build a second one |

Numbered pages (`1 2 3 4 >`), SAB-style. Rows shifting between pages as work completes is
**accepted and explicitly not a problem to solve** — the user's call, and it is how SAB behaves.

### 3.3 Rows expand to per-file progress

This is the thing Files is currently used for, moved to where the ordering lives.

**The data already exists.** `core/queue.py._publish_child_progress` computes each child file's
size and state from the same filesystem walk the job already performs, persists it, and publishes
it live. The Files tree is one renderer of it. This is re-presentation, not new plumbing.

**Children must be fetched lazily on expand, never inlined into the jobs list.** Precedent and
trap: `api/history.py` deliberately carries only `has_output_tail` and fetches the blob from a
separate endpoint, because inlining per-row payloads onto a list endpoint reintroduces exactly
the cost the row cap exists to prevent. A season pack has dozens of children.

Note `pget` (single-file) jobs have no children — `JobProgress.children` is `None` there.

### 3.4 Reordering: chevrons, and the model change underneath

Wanted: **▲ up one**, **▼ down one**, **▲▲ to top**.

**Today's `rank` is a boost, not a position.** It defaults to `0`; "Move to top" sets
`rank = MAX(rank) + 1`. So the queue is two zones — a boosted zone (rank > 0, most recently
boosted first) and the natural zone (rank 0, oldest `queued_at` first). "Move to top" fits this
perfectly. **"Move up one" does not**, three ways:

1. Two adjacent rank-0 jobs can only be swapped by swapping `queued_at`, which corrupts the
   queued-wait readout. The v0.2.6 rescue backdates `queued_at` *specifically* so that readout
   stays truthful.
2. At the zone boundary, "up one" is not up one — promoting a rank-0 job means rank ≥ 1, which
   vaults it above the entire backlog at once.
3. Inside the boosted zone, rank encodes *how recently you boosted*, not *where it sits*.

**Required: a dense total order.** One position value per queued job, assigned on insert and
rewritten on reorder. Recommended shape is a fractional key (`REAL`): new jobs take `max + 1`,
and a move between two neighbours takes the midpoint — one `UPDATE`, no renumbering, occasional
rebalance. Needs a migration plus changes to the admission query and auto-queue's insert path.

**Two reasons this is riskier than it looks:**

- The admission ordering query is the most load-bearing query in the app.
- **The v0.2.6 startup-rescue ordering fix is built directly on these semantics** — it carries
  the original `queued_at` forward and deliberately leaves `rank` alone so a rescue can never
  outrank an explicit Move-to-top. Under a position model that behavior must be *re-derived and
  re-proven by test*, not merely ported.

**Therefore: the order model is its own stage, before any chevron UI.** The chevrons are trivial
once positions are writable.

**Scope of a move is global**, matching the global scheduler and the global numbering, so the
chevron and the displayed number always agree. Consequence to accept: the row visually above you
is not necessarily the one you trade places with.

### 3.5 The fast lane makes today's numbering slightly dishonest

`queuePositions` numbers every queued job `1..N` regardless of lane, but the two lanes admit from
independent pools — items under `small_item_threshold_bytes` (10 MB default) enter a fast lane
with its own concurrency cap and reserved bandwidth. So a small item at `#9` can genuinely start
before the main-lane job at `#2`.

Easy to miss in today's grouped view; in one intermixed ordered list it will read as a bug.

**Decided (2026-08-19): keep one `1..N` numbering and mark fast-lane rows** with a badge, so the
jump is explained rather than surprising. Rejected: separate per-lane numbering (`F1`, `F2`…) —
two numbering schemes in one list is more to read than the problem justifies; and a third box
for the fast lane — the page already has two.

### 3.6 Queue identity without grouping

With grouping gone, a row still needs to say which queue it belongs to — but cheaply, since
"per-row queue labels make the page busy" was a real finding.

**Proposed, in order of cost:**

1. **A short display name per queue.** New optional `path_queue` column (e.g. `short_name`),
   set in Settings → Queues, falling back to the full name when unset. `DC-Movies` → `MOV`.
   Cheap, no licensing, solves most of it.
2. **A predefined icon per queue** — a curated set (TV, TV 4K, Movie, Movie 4K, audiobook
   variants, music, software), chosen from a picker in queue setup and rendered on each row.

**Decided (2026-08-19): ship (1) first, revisit (2) later** — once the single list has been
lived with, and it's clear whether icons are still wanted. The short name alone makes a single
list readable; icons are a genuine improvement on top but carry work the short name doesn't:

- Any bundled icon set is third-party content and needs a `NOTICE` entry with its licence.
- Icons must be inlined/bundled (no external fetch) and must be legible in both light and dark
  themes.
- A fixed curated list will not cover every user's categories, so the short name remains the
  fallback and must exist anyway.

## 4. Download-client integration (SAB, ruTorrent, extensible)

### 4.1 The governing principle

**Advisory only.** A download client may:

- **skip work** — satisfy the settle gate directly,
- **withhold work** — block a transfer that would move known-bad bytes,
- **explain** — annotate rows and write audit events.

It may **never write `item.state`.** This preserves §1.3: lftp — and now SAB — is a transfer
engine, not a status API. The state machine keeps deriving truth from the filesystem.

### 4.2 The rule that must be encoded loudly

> **Absent from the client is not a verdict.** Only an *explicit* failure blocks anything.
> Silence means no information, and no information means fall back to today's behavior.

This is not hypothetical. The entire amber `dropped` state (v0.2.4) exists because SAB
spuriously returns a blank queue to Sonarr's poll, and 8 mid-download items flipped to terminal
`gone` in a single pass as a result. If "not in SAB's queue" were allowed to mean "failed, never
transfer," one blank response would blackhole a batch of good releases.

**Unreachable client ⇒ keep last known status**, never downgrade a verdict. Same shape as
`core/arrsync.py`'s per-instance capped exponential backoff (60 s → 30 min), 10 s timeout, one
warning + one audit event.

### 4.3 What the verdicts buy

**Skip the settle gate on a positive verdict.** The settle gate waits for the remote directory to
stop growing; SAB completing *is* that same fact, reported by the process doing the writing. This
replaces an inference with a direct observation, keeping the inference as fallback. Saves the
~60 s settle tax and is *more* correct — a stalled upload that happens to look still stops
reading as settled.

**Withhold on partial failure.** If SAB fails outright, nothing lands and there is nothing to
auto-queue — the block is automatic and needs no code. The case that genuinely needs an explicit
withhold is a **partial** failure: the download dies partway, or unpack fails, leaving a
half-written directory on the seedbox. The settle gate sees those bytes stop growing (they have —
permanently) and transfers garbage.

**Explain.** The real deliverable is the audit path: the user currently sees nothing arrive and
has no way to learn why.

### 4.4 Identity — already solved, for free

The *arr hands lftpweb a `downloadId` that **is** the client's own key. Evidence from a real
production bundle: `b67924d8-c0f0-4901-8941-85ddbfef6179` (a SAB `nzo_id`) and
`12682AF0C00A061448BCFA16975A5D5F01A84A61` (a torrent hash), both sitting in `arr_matched`
events. So for any *arr-tracked item, matching is an exact key lookup.

For untracked items, **SAB's history `storage` field carries the real final path on disk**, after
any rename or unpack — observed, not predicted. Two-phase identity:

- **In the client's queue:** only a name and an id exist. Nothing is on disk yet, so there is
  nothing to match — this phase is display-only.
- **In the client's history:** the true path exists, and binding is exact.

**Do not predict paths from names.** The same production bundle shows why: Sonarr grabbed
`Married.At.First.Sight.S12E15.720p.WEB.h264-BAE-xpost`, it failed on the SAB side, and the
replacement landed as `...BAE[rarbg]-xpost`. SAB also renames on unpack. A predicted path is a
guess that will sometimes be wrong, and a wrong guess leaves a phantom row that never reconciles.

### 4.5 Binding: site-level instance, category → queue mapping

**A client instance is site-level, not per-queue** — one SAB serves `dc-tv` and `dc-movies` both.
Copying the *arr integration's per-queue binding would mean configuring and polling the same SAB
twice.

**Queue attribution comes from a configured category → queue mapping**, defined when the client
is set up. Explicit, matches how the user already thinks about SAB, and — importantly — it means
a pending entry knows its queue *before any file exists*, so incoming work can be grouped and
ordered from the first moment.

### 4.6 Pending entries are NOT `item` rows

**This is the load-bearing architectural decision.** A pending entry exists before the release is
in either tree. Making it an `item` row breaks three things at once:

1. `core/engine.py._project` filters to paths present in some tree — and that filter is
   load-bearing, not tidiness (it is what stops departed rows resurrecting, and what keeps
   `diff_nodes`'s `removed` from being permanently empty). A pending row is in neither tree and
   would be dropped on every scan.
2. Every §3.2 state rule is defined in terms of remote/local presence. "On neither, but expected"
   is a genuinely new axis.
3. Auto-queue would need teaching never to touch a pending row, since there is no remote path.

**Instead: an ephemeral client-activity layer** — its own table, merged into the Queue tab *for
display*, retired once the real item appears and takes over.

- The audit path is complete: pending → client logo → *arr match → failed/complete → real item →
  transfer → import.
- `item` and the state machine are untouched. No new state, no CHECK-constraint migration, no
  risk to reconcile, and "nothing publishes a state it did not read back from the `item` table"
  survives intact.
- The **10-minute failure display** the user asked for becomes trivial — an ephemeral row with a
  TTL, rather than a permanent `item` row needing retention machinery.
- The name-instability problem disappears: a pending entry does not claim to *be* a path, only
  that the client is working on something.

**Framed as a cache:** everything in the table is re-fetchable from the client, nothing else
reads it for correctness, and truncating it is always safe. That framing is what stops it
quietly becoming a second source of truth.

### 4.7 Adapter interface

Build the pluggable abstraction properly, **ship one adapter (SAB) first**. ruTorrent and others
then land against a proven interface. Needs a normalized verdict vocabulary across very different
APIs — SAB's history status (Completed / Failed, plus `fail_message`, and queue-side
Downloading / Paused / Repairing / Extracting) versus a torrent's percent-complete / hashing /
error.

### 4.8 The polling shape appears a third time

`core/arrsync.py` already implements: poll an external service, short timeout, capped exponential
backoff per instance, one warning + one audit event on failure, keep last-known state, never let
silence become a verdict. The client poller needs the identical shape.

**Do not refactor `arrsync.py` in the same pass that introduces the new integration** — it is
battle-tested against real production incidents. Build the client poller alongside it, then
extract the shared shape afterwards, once both exist and the seams are obvious.

---

## 5. Reversals of earlier decisions

Both must be recorded in `docs/decisions.md` as reversals with their cause, not silently
contradicted:

| Reversed | Why it changed |
|---|---|
| Grouping the Transfers page by queue (`2026-08-16-transfers-group-by-queue.md`) — adopted because per-row queue labels made the page busy | The name filter (2026-08-19) removes the need for per-row queue signal; and grouping misrepresents a globally-ordered scheduler as per-queue lines |
| Per-queue "Dismiss Queue" control (v0.2.3, `278e10f`) | Superseded by the filter + scoped "Dismiss list" (2026-08-19), which does the same job without grouping |

## 6. Open questions

**Resolved:**

- ~~Fast lane vs. a single numbering~~ — decided, §3.5: one numbering, mark fast-lane rows.
- ~~Icon set~~ — decided, §3.6: short name first, icons revisited later.
- ~~Does the Events page support a per-item deep link?~~ — **yes, already.**
  `api/history.py`'s events endpoints already accept an `item_id` filter parameter. The deep link
  is frontend route/query-param wiring only; no backend work.

**Still open (phase 2 only, see below):**

1. What TTL policy retires a *successful* pending entry — immediately on binding to a real item,
   or after a short grace so the user sees the handoff? Deliberately deferred: it only matters
   once the client integration is being built, and the phase-1 redesign should be lived with
   first.

## 7. Build order

**Two phases, decided 2026-08-19: the UI redesign lands first, in full, before any
download-client work begins.** The client integration is real but it is not what makes the app
better to use today, and stacking a new external integration on top of an in-flight IA change
would make both harder to verify.

Staged so each lands verifiable on its own. **Every UI stage ships browser-unverified until the
user looks at it** — no agent in this project can render a page, which is exactly why the risky
stages are split small.

### Phase 1 — the UI redesign

| # | Stage | Depends on |
|---|---|---|
| 1 | **Queue order model** — dense position key, migration, admission query, auto-queue insert; re-prove the v0.2.6 rescue ordering by test | — |
| 2 | **Chevron reordering UI** (▲ ▼ ▲▲) | 1 |
| 3 | **Queue short display name** on `path_queue` + Settings → Queues field | — |
| 4 | **Single ungrouped Queue list** — drop grouping, two paginated boxes (20 / 50), short name on rows, fast-lane badge | 3 |
| 5 | **Row expansion to per-file progress**, children fetched lazily on expand | 4 |
| 6 | **Tabs** — Transfers as main section with Queue + Files tabs | 4 |
| 7 | **History → Events** — drop the jobs list, add the per-item deep link (frontend only) | 4, 6 |

Stage 1 carries the real architectural risk; 3 and 4 are low-risk and independently useful.
Stages 3 and 1 have no dependency on each other and can go in either order.

### Phase 2 — download clients (not started; revisit after phase 1 is in use)

| # | Stage | Depends on |
|---|---|---|
| 8 | **Download-client adapter interface + SAB adapter** — poller, verdicts, category mapping | — |
| 9 | **Ephemeral client-activity layer** + pending rows in the Queue tab + 10-minute failure display | 4, 8 |
| 10 | **Settle-gate skip** on a positive client verdict; **withhold** on partial failure | 8 |
| 11 | **Queue icon set** (optional, §3.6) | 3 |
| 12 | **ruTorrent adapter**; extract the shared polling shape from `arrsync.py` | 8 |

Stages 8 and 9 carry this phase's architectural risk.
