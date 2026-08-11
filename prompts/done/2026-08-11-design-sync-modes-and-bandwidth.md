---
name: 2026-08-11-design-sync-modes-and-bandwidth
status: completed
created: 2026-08-11
model: opus
completed: 2026-08-11
result: Partially executed by a spawned agent (stopped mid-run when planning was still moving); superseded and finished in-session with three corrections plus later UI, ops, and container decisions.
---

# Task: Revise DESIGN.md — sync modes, bandwidth model, path queues, and SeedSync framing

`DESIGN.md` was written before a design review that settled five things and closed every open
question in §13. Fold those decisions into the doc so it stays the single architectural source
of truth. This is a **documentation revision only** — no application code exists yet and none
should be written.

Decisions 3 and 4 are the substantial ones: 3 adds remote deletion (irreversible, so the
safety reasoning matters more than the feature), and 4 reshapes the core config entity, which
ripples through the schema and most of the UI sections.

## Before you start

- Read `DESIGN.md` end to end. Its sections are numbered and referenced elsewhere
  (`CLAUDE.md` cites §1.3; `docs/decisions.md` cites §1.3, §4.4, §4.5), so numbering is a
  small public API — see "Conventions to honor".
- Read `CLAUDE.md` for the project's standing rules.
- Read `docs/decisions.md` — the existing 2026-08-11 entry "lftp is a transfer engine, not a
  status API" is the decision this doc hangs off. Don't contradict it.
- The user is `crzykidd`. Their seedbox workflow is described under decision 3 below; it is
  load-bearing for the whole sync-modes design, so read it carefully before writing.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files
this plan needs to modify (`DESIGN.md`, `docs/decisions.md`). If any have uncommitted
changes, list them and ask the user before touching them. Surface unrelated dirty files
once as awareness; don't block. This file (the handoff prompt itself) is exempt.

## What to do

### Decision 1 — SeedSync is prior art, not lineage

lftpweb is a **fresh codebase with all fresh code**. SeedSync was studied; nothing is derived
from it.

- Retitle §1.2 (currently "Why not just run SeedSync") to make the relationship explicit —
  something like "Prior art: what SeedSync teaches us". Add a sentence stating plainly that no
  SeedSync code is used or adapted, and that the references exist to justify design choices,
  overwhelmingly choices to do things *differently*.
- **Keep all the technical substance.** The issue numbers, the maintainer quote from fork
  issue #294, the shared-blast-radius / kill-race / directory-ETA consequences — these are the
  evidence for §1.3 and must survive.
- Make sure §4.4 reads correctly under this framing: `.lftp-pget-status` and the
  `xfer:use-temp-file` / `*.lftp` suffix are **lftp's** on-disk conventions, not SeedSync's.
  We'd have to handle them no matter what inspired the project.
- Sweep the rest of the doc for any phrasing that implies inheritance rather than comparison
  (the §2.1 departures table is fine — it's explicitly a comparison).

### Decision 2 — Bandwidth and concurrency (rewrite and expand §4.5)

§4.5 currently treats the global bandwidth cap as a regrettable approximation. That framing is
wrong; the real model is a three-level hierarchy and the binding constraint isn't bandwidth at
all. Rewrite it to cover:

**The hierarchy.** Present it as a stacked diagram like this (adapt formatting as you see fit):

```
global cap                     10 MB/s
├─ job 1  (one queued item, usually a directory)      cap 5 MB/s
│   ├─ mirror --parallel=4        → 4 files at once
│   └─ mirror --use-pget-n=4      → 4 connections per file   = 16 connections
└─ job 2                                               cap 5 MB/s
```

- A **job** is one queued item — one lftp process. For a seedbox that's nearly always a release
  directory, so "2 streams" means 2 directories in flight.
- `--parallel` and `--use-pget-n` are the knobs *inside* a directory job. That's why they
  belong at the job level rather than forming a separate concurrency tier.

**Why per-job caps compose with multi-connection transfers.** `net:limit-total-rate` is
**process-wide** — the sum across every connection that lftp process opens. One number per job
therefore bounds the entire subtree beneath it regardless of how many files or pget chunks it
splits into. Contrast `net:limit-rate`, the per-connection knob, which does *not* compose;
state explicitly that we use `net:limit-total-rate` and avoid `net:limit-rate` for this purpose.

**Allocation rule.** Per-job cap = `global_limit ÷ (currently active jobs + 1)`, computed at
spawn time — **not** `÷ max_concurrent`. If only one item is queued it gets the full global
cap. This matters because "one big download at a time" is the common case and is exactly where
dividing by max wastes the most.

**Residual gap, stated honestly.** A job already running cannot be re-shaped when another
finishes, because `lftp -c` gives no control channel. Record the possible fix as a **Phase 3
experiment**: hold the lftp process's stdin open as a pipe, run the transfer with `&`, and send
`set net:limit-total-rate <n>` mid-flight to retune it live — preserving one-process-per-job
while regaining live control. Mark it clearly as **unverified**; it must be tested against a
running transfer before the design depends on it, with static division as the fallback. Do not
present it as a settled part of the design.

**The connection ceiling is the constraint that actually bites.** 2 jobs × 4 parallel × 4
chunks = 32 concurrent SFTP sessions, and many seedboxes cap well below that and start refusing
connections. So:
- `net:connection-limit` is a first-class setting, not an advanced afterthought.
- The Settings UI must compute and display the **worst-case concurrent connection count** live
  as the user adjusts jobs / parallel / pget-n, because these three multiply silently. Add this
  to §8.1.
- Per-pair defaults still apply, so `tv/` and `movies/` can be tuned differently.

Because §4.5 is no longer a straight admission of inferiority, revise the §13 risk row that
points at it accordingly (see Decision 4).

### Decision 3 — Sync modes (new section, insert as §7)

This is the largest addition. Three modes, configured **per path-pair**:

| Mode | Behavior |
|---|---|
| `copy` | Download; never touch the remote. Local deletes do not propagate. **Default.** |
| `move` | Download, verify, then delete the remote. |
| `sync` | Copy, plus propagate *local* deletes back to the remote. |

**Deployment assumption that makes remote deletion safe — document this prominently.** The
user's torrent client hardlinks completed files into a separate pickup directory, and lftpweb
syncs from that pickup directory. The seeding torrent's data keeps its own hardlink, so
deleting our copy in the pickup dir never removes the seed's data. This dissolves the usual
"deleting the source breaks the seed and costs tracker ratio" hazard **for this deployment
shape**. Consequences to write down:

- **No torrent-client integration is needed** for delete safety, and none is planned.
- **No minimum-file-age gate** is needed. Don't add one — file age is a poor proxy and would
  only add friction here.
- **Add an explicit warning** for anyone who points lftpweb at a live torrent *data* directory
  instead of a hardlink pickup directory: in that configuration `move` and `sync` will destroy
  seeding torrents. This belongs in the doc and later in the Settings UI next to the mode
  selector.

**The `sync` trigger, and what makes it different from what you'd assume.** The user's
Sonarr/Radarr import **moves** files out of the local sync directory, followed by a cleanup job
that deletes leftovers. So a local file disappearing is the **normal, expected** end state of
every successful import — and that disappearance is precisely the intended trigger for
propagating a remote delete in `sync` mode.

This inverts the usual safety intuition and you must design around it:

- **Deletes are routine, not anomalous.** A count-based circuit breaker ("more than N deletes
  in a cycle is suspicious") will false-positive on every bulk import. Do not lean on one. If
  you keep a breaker at all, make it rate-based and generously sized, and describe it as a
  backstop rather than a safeguard.
- **The mount/sentinel health gate carries the entire safety load.** Specify it properly:
  lftpweb writes a sentinel file (e.g. `.lftpweb-mount-ok`) at the local root after its first
  successful scan. Before *any* delete propagation, the local root must exist, be readable, and
  contain that sentinel. If a volume fails to mount, every item looks deleted — without this
  gate lftpweb would cheerfully wipe the seedbox. Say that consequence out loud in the doc; it
  is the reason the gate exists.

**Remaining rails:**
- Only propagate a delete for an item with a `DOWNLOADED` record. Absence of something never
  fetched means nothing.
- Grace period: absence must persist across several scans (default ~10 minutes) so a transient
  state can't trigger a delete.
- Dry-run mode that logs what *would* be deleted without acting.
- Full audit trail in the `event` table. A remote delete is irreversible; the minimum bar is
  being able to reconstruct exactly what was deleted and why.

**Deletion mechanism.** Remote deletes always go through our own asyncssh path (§5), gated on
verification — **never** lftp's `mirror --Remove-source-files`. Rationale to record: it keeps
verification as the gate, keeps every delete auditable, and uses one code path for both `move`
and `sync`.

### Decision 4 — Host is configured once; work is organized into named path queues

This replaces the current `path_pair` model and settles §13 open question A.

**Shape:** one **host** (connection details set once — address, port, user, key/password,
protocol, connection tuning), and under it **N named path queues**, each mapping one remote
path to one local path. The UI groups everything by queue: browsing, active transfers, and
history are all filterable and groupable by which queue an item belongs to.

- Rename the entity: `path_pair` → `path_queue`, displayed in the UI simply as a **Queue**
  with a user-supplied **name** (e.g. "TV", "Movies", "Music") plus its remote → local mapping.
- **Naming collision — resolve it deliberately.** "Queue" now means the user-facing path
  grouping, while §4 already uses "queue" for the pending-transfer queue. Rename the latter
  consistently to the **job queue** (`core/queue.py` → the job queue; `TransferQueue` stays a
  reasonable class name but the prose must not call it "the queue" unqualified). Sweep the
  whole doc so no sentence leaves the reader guessing which is meant.
- **Move connection settings out of the queue and up to the host.** Credentials, address,
  port, protocol, and host-level connection tuning (`net:connection-limit`, socket buffer,
  timeouts, retry) are configured once. Per-queue settings are the ones that legitimately vary
  per path: `sync_mode`, auto-queue enable, include/exclude patterns, post-processing toggles,
  staging path, and bandwidth/parallelism overrides.
- **Design for one host, leave a seam for many.** v1 targets a single seedbox — don't build
  multi-host UI or routing. But model the host as its own record (`host` table, or a settings
  block with a stable id referenced by `path_queue.host_id`) rather than inlining its fields
  into global settings, so adding a second host later is a schema addition rather than a
  migration of every queue.
- Update §3.1 (schema), §5, §6, §8 (Settings and Files views), §8.1 (which knobs are
  host-level vs queue-level), and any other section that says "path pair" or "per-pair".

**Also settled from the same review:**
- **Question C — confirmed:** no Sonarr/Radarr/*arr integration, in v1 or in the near roadmap.
  Remove any language implying it's planned. (The `sync` mode of Decision 3 is what serves the
  *arr workflow, and it does so without talking to those services at all — worth one sentence
  making that explicit, since a reader will expect an integration here.)
- **Question D — answered:** History ships as its own **second page**, with results **grouped
  by queue**. Promote it from "polish" to a real deliverable in §8 and place it appropriately
  in the §11 build order (it needs the `job`/`event` tables, so it can land any time after the
  transfer engine; don't leave it stranded in the last phase).

### Decision 5 — Ripple the above through the rest of the doc

- **§3.2 (file states).** The current `DELETED_REMOTE` name is ambiguous now that both "gone
  locally" and "gone from both sides" are real states. Rename for clarity — suggested:
  `REMOVED_LOCAL` (was downloaded, absent locally, remote still present) and `REMOVED_BOTH`
  (terminal, retained as history). Use better names if you find them, but the two cases must be
  unambiguous. Spell out that the *same* detection drives different actions by mode: in `copy`
  it means "do not re-queue this"; in `sync` it means "propagate the delete". Keep the existing
  numbered rules 1–4 and extend them rather than replacing.
- **§3.1 (schema).** `path_queue` (renamed per Decision 4) gains `sync_mode`. Add whatever the
  delete audit and grace period need (e.g. a `first_missing_at` on `item`, delete records in
  `event`). Note that the old `path_pair` carried `auto_delete_remote` — reconcile that with
  `sync_mode` rather than leaving two overlapping switches; `sync_mode` should subsume it.
- **§5 (remote scanning).** Note that this component also owns remote deletion, and that
  deletes are gated on verification.
- **§6 (post-processing).** Verification is now load-bearing whenever `sync_mode != copy` —
  it's the gate on an irreversible remote delete, not optional garnish. Say so.
- **§11 (build order).** Add sync modes as a phase. It depends on the transfer engine and on
  verification, so it lands after Phase 5 (post-processing); renumber the remainder.
- **§12 (verification).** Add test coverage for the new logic: the mount-sentinel gate
  (simulate an unmounted root and assert zero deletes propagate), the grace period, mode
  behavior differences, and `REMOVED_LOCAL` not being re-queued in `copy` mode.
- **§13 (risks).** Delete the seeding risk if present — it's dissolved by the hardlink pickup
  dir. Revise the §4.5 bandwidth row to match the new framing. Add: (a) the mount-gate is a
  single point of failure for an irreversible operation; (b) routine deletes mean anomaly
  detection can't be a safeguard; (c) the misconfiguration hazard of pointing at a live torrent
  data dir.
- **§13 open questions.** All four are now answered — **delete the entire open-questions
  block.** A → host-once + named path queues (Decision 4). B → all three sync modes ship in v1
  (Decision 3). C → no *arr integration. D → History is its own page, grouped by queue. If any
  genuinely new open question surfaced while you were writing, list it in their place; don't
  keep the answered ones around.

## Conventions to honor

- **Section numbering is a small public API.** `CLAUDE.md` and `docs/decisions.md` cite §1.2,
  §1.3, §4.4, and §4.5 — all of which sit *before* the §7 insertion point, so they stay valid.
  After inserting the new §7, renumber everything below it and **fix every internal
  cross-reference in the doc**. Grep for `§` when you're done and verify each one still points
  where it means to.
- Match the existing voice: direct, specific, willing to name trade-offs and call out what's
  unverified. No marketing tone. Tables and short diagrams where they earn their place.
- Don't restate decisions already captured in `docs/decisions.md` — reference them.
- Keep the doc's "Status: draft, pending review" header accurate.

## When done

1. Add a `docs/decisions.md` entry (newest at top) for the sync-mode / seed-safety decision:
   the hardlink pickup directory is what makes remote deletion safe, which is why there's no
   torrent-client integration and no minimum-age gate; the Sonarr move-on-import flow is what
   makes local deletes routine, which is why the mount sentinel — not anomaly detection —
   carries the safety load. Record the rejected alternatives (torrent-client API gating,
   minimum-age gating, count-based circuit breaker) and why each was dropped.
2. Update this file's frontmatter: set `status` (completed/failed), `completed` (2026-08-11),
   and `result` (one line).
3. `git mv` this file into `prompts/done/` (on success) or `prompts/failed/` (on failure).
   Create the subdir if it doesn't exist yet.
4. Hand off ONE commit covering this prompt file, `DESIGN.md`, `docs/decisions.md`, and the
   prompt move (the prompt is **not** pre-committed — it bundles in here). Present the file
   list and a one-line message summarising the changes.
   - **You are a spawned agent:** do **not** commit. Prepare the working tree, then report the
     file list + proposed message back to the orchestrating session, which surfaces the `y/n`
     to the user.
   - Never `git add -A`, never push, never auto-commit.
   - This project does not adopt `code-checkin-and-pr`, so no branch or prefix rules are
     imposed — but the repo follows its prefixes voluntarily, so use a `docs:` prefix and no
     `Co-authored-by:` trailer. Current branch is `dev`.
