---
name: 2026-08-30-sab-global-pause
status: completed        # pending | completed | failed
created: 2026-08-30
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-30
result: SABnzbd's global pause (queue["paused"]) now overrides QUEUED/DOWNLOADING queue slots to PAUSED, leaves post-download phases untouched, and clientsync.py derives a "Paused (...)" status_label for any PAUSED transfer, connector-agnostic by construction.
---

# Task: surface SABnzbd's global pause

The user: *"I notice we don't pick up the pause status on sab... so when we refresh we should show
sab as paused for the item."*

`core/clients/sabnzbd.py.list_transfers` maps `queue["slots"]` and **never reads the top-level
`queue["paused"]` boolean**. SABnzbd's main pause button pauses the whole queue without changing
any individual slot's `status`, so every slot keeps reporting `Downloading`/`Queued` and lftpweb
faithfully shows a stopped queue as still downloading.

**Scope: global pause only.** Do NOT touch the history post-processing mapping or
`_HISTORY_PHASE_MAP` — a separate question about unpacking is open and waiting on live API
captures from the user's own instance. Widening the phase maps on a guess is exactly what this
file's own `UNVERIFIED` comments warn about.

## Before you start

Read, in this order:

1. `core/clients/sabnzbd.py` — `_QUEUE_PHASE_MAP` and its **"doc-derived, UNVERIFIED against a
   live SABnzbd"** header comment, `_map_phase`, `_transfer_from_queue_slot`, `_get_queue`, and
   `list_transfers` (~line 494).
2. `core/clients/models.py.Transfer` — especially that `raw_status` is the client's own string,
   **verbatim**, and is mandatory for every connector.
3. `core/clientsync.py` ~line 866 — `status_label=transfer.raw_status`, which is what the
   Preflight row actually displays.
4. `CLAUDE.md` — commit rules; gates in the **foreground**, from the repo root.

## What to do

### 1. Read the flag

`_get_queue`'s payload carries a top-level `paused` boolean alongside `slots`. Thread it into
`list_transfers` and apply it when building each queue-slot `Transfer`.

### 2. Apply it only to the download side

**A global pause stops downloading. It does not stop post-processing.** An item that is
`Extracting`, `Verifying`, `Repairing`, `Moving` or `Running` keeps doing that while the queue is
paused, so its phase must NOT become `PAUSED`.

Override to `TransferPhase.PAUSED` only where the slot's own mapped phase is a download-side one
(`QUEUED`, `DOWNLOADING`). A slot already reporting `PAUSED` individually stays `PAUSED`. Anything
else is left exactly as mapped.

Write this reasoning into the code, not just the commit — it is the non-obvious half of the task,
and a later reader will otherwise "simplify" it into a blanket override.

### 3. Make the user actually see it

`raw_status` is contractually the client's own string, verbatim — **do not rewrite it** to say
"Paused". But `core/clientsync.py` sets `status_label=transfer.raw_status`, so a paused item would
still read "Downloading" on the Preflight row, which is the whole complaint.

Resolve this in `core/clientsync.py`, not in the connector: when a transfer's `phase` is `PAUSED`
but its `raw_status` doesn't already say so, the displayed `status_label` should say paused. Keep
it a display-level derivation — the phase is the fact, `raw_status` stays verbatim for the drawer
and for debugging, and no other connector's behavior changes. Pick the exact wording and justify
it in a comment.

**This is connector-agnostic by construction** — it keys off `TransferPhase.PAUSED`, never off
`client_type` (spec §4.4/§5.1 forbid client-name branching). rTorrent's own paused torrents get
the same treatment for free, which is correct.

## Tests

Extend `tests/test_sabnzbd_connector.py` (or the existing SAB connector test module) and the
clientsync tests:

- Global pause true + a slot reporting `Downloading` → phase `PAUSED`, `raw_status` still
  `"Downloading"` verbatim.
- Global pause true + a slot reporting `Extracting` → phase still `EXTRACTING`, **not** paused.
  Name this test so its purpose is obvious; it is the one that protects the nuance.
- Global pause false → every phase exactly as today (a straight regression guard).
- A slot individually `Paused` with global pause false → still `PAUSED`.
- Missing/absent `paused` key → treated as not paused, never a raise. This connector's own
  "tolerant reading" rule.
- clientsync: a `PAUSED` transfer's `status_label` reads as paused; a non-paused one is unchanged.

The repo has a `fake_sabnzbd_server` fixture — use it rather than hand-rolling a second fake.

## Conventions to honor

- Match the surrounding docstring style. This module is scrupulous about marking what is
  doc-derived versus measured: **the top-level `paused` flag is doc-derived unless you verify it,
  so label it that way.** Do not quietly present a guess as a measured fact.
- Doc updates ship in the same change set: `docs/download-client-framework-spec.md` where the SAB
  connector's phase mapping is described, and `docs/decisions.md` newest at top — record the
  download-side-only rule and the rejected alternative (a blanket override of every phase).
- Gates, each its own **foreground** command from the repo root, reading each exit code:
  `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`. Add the frontend gates
  only if you touch a frontend file.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record non-obvious decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the working tree, then report back to the orchestrating session:
   the file list, a one-line commit message, and the final test counts. The orchestrating session
   surfaces the `y/n` to the user. Never `git add -A`, never push.
