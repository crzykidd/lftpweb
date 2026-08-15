---
name: 2026-08-12-ws-publishes-persisted-state
status: completed        # pending | completed | failed
created: 2026-08-12
model: opus              # touches the §2/§9 publish contract, plus a rename across every call site
completed: 2026-08-12
result: >
  The `item` table is now the single authority for item state. `Engine.scan_queue` persists
  first, reads back via `_project` (the widened `_refresh_item_ids` query), and diffs and
  publishes that projection; `snapshot()` re-reads the database and is now `async`, so the
  reload path agrees too. `ReconciledNode.state` renamed to `structural_state` across
  `core/reconcile.py`, `core/engine.py`, `tests/test_reconcile.py` and
  `tests/test_settings_api.py`. New `core/itemview.py` holds the one projection: `api/files.py`,
  `core/queue.py._publish_item_state` and `core/postprocess.py._publish` all collapsed onto it
  (four hand-written copies of the same dict became one). Delta-size property preserved and
  extended — the new invariant test runs at 20 and 5000 nodes and asserts wire state == `item`
  state for every published node. 489 tests pass (486 + 3), ruff format and check clean,
  verified live over `ws://localhost:8087/api/ws`.
---

# Task: Make the database the single authority for item state, and publish a projection of it

The WebSocket publishes a state the database disagrees with. `core/engine.py.scan_queue`:

```python
nodes = reconcile(...)                          # purely structural: REMOTE_ONLY / PARTIAL / DOWNLOADED / ...
changed, removed = diff_nodes(old_nodes, nodes) # <-- diffed BEFORE any override is applied
self.models[q.id] = nodes                       # <-- the in-memory model is the structural one
await self._persist(q.id, nodes)                # <-- _persist decides the REAL state
...publish("queue_delta", changed=...)          # <-- so the wire carries the structural reading
```

`_persist` is where state is actually decided: job-lifecycle protection (`QUEUED`/`DOWNLOADING`/
`STOPPED`/`FAILED`), the post-processing precedence rule, and §7.3's `REMOVED_LOCAL` grace period.
None of it reaches the wire. `snapshot()` serializes from `self.models` too, so a reload
re-sends the structural reading as well. Pre-existing since phase 4: a `REMOVED_LOCAL` item, or
one held `DOWNLOADED` through the grace window, has been published as `REMOTE_ONLY` — Queue button
and all — ever since. `api/files.py` already reads the database (the phase 3 fix), so **the REST
view and the WebSocket view of the same item can disagree today.**

## The approach — decided by the user, implement this one

Do **not** simply patch the overridden states back onto the in-memory model. That works and is
less code, but it leaves two places computing what an item's state is, kept in agreement by
remembering to — which is precisely how this bug arose. Instead:

**The `item` table is the single authority for item state. The in-memory model is a cache *of*
it. Nothing publishes a value it did not read back.**

Three parts:

1. **Publish a projection of the database.** `_refresh_item_ids` already runs
   `SELECT id, rel_path FROM item WHERE queue_id = ?` after every persist, returning one row per
   node — the whole tree, every scan. Widen that single query to also select the display columns
   (`state`, `is_dir`, `remote_size`, `local_size`, `remote_mtime`) and build the published nodes
   from its rows. **Every field `serialize_node` emits is already an `item` column** — verify this
   yourself before relying on it. No new query, no extra round trip, no asymptotic change
   (`reconcile` is already O(tree)).

2. **Rename `ReconciledNode.state` to `structural_state`.** This is the part that prevents
   recurrence, not cosmetics: the field is a *candidate* reading that merely looks authoritative,
   so `nodes[x].state` reads as "the state" at every call site. After the rename, publishing the
   wrong thing requires explicitly asking for the structural value. Update every call site —
   `core/reconcile.py`, `core/engine.py`, tests, and anything else the grep finds. **Be careful in
   `core/reconcile.py`: an empty-remote-directory fix landed there today** (`remote_file_totals`);
   read it before touching the file and preserve it exactly.

3. **Collapse the duplicate view.** `serialize_node`'s docstring says the Files page renders purely
   from the stream, never from `GET /api/files` — so that endpoint is a second implementation of
   the same projection. Point `api/files.py` at the same projection function so one code path
   produces item views regardless of whether they leave over HTTP or the socket. If that turns out
   to be more invasive than it looks, say so and stop rather than half-doing it.

## The trap this must not break

**Never publish a full node list except on WebSocket connect.** A named regression in this project
(`docs/decisions.md` phase 3b; the trap list in `prompts/startnewsession.md`): every update after
the initial `snapshot` must be a delta proportional to what changed, never to tree size — proven
against 20-node and 5,000-node trees. The tempting shortcut here is to re-send more rows so the
wire agrees with the database. Do not. Keep the existing delta-size assertions passing and extend
them.

The genuinely hard case: **an item whose effective state changes while its structural node does
not.** A job starting moves `DOWNLOADED` → `DOWNLOADING` with identical bytes on both sides; a
grace period expiring moves an item to `REMOVED_LOCAL` with no new scan data. Diffing projections
rather than structural nodes makes these visible to the diff *for the first time* — which is
correct, but decide deliberately whether the scan path should emit them, given `core/queue.py`
already pushes `item_delta` for lifecycle transitions. **Work out which transitions each path
covers, and do not create a second mechanism where one exists.** Report your reasoning.

## Before you start

- **Read `DESIGN.md` §2, §3.2, §9 and `core/engine.py` in full** — especially `diff_nodes`,
  `_persist`, `_protected_rel_paths`, `_previous_states`, `_refresh_item_ids`, `serialize_node`.
- Read `core/mount_sentinel.py.resolve_absence`, `core/postprocess.py`'s state ownership
  (`OWNED_STATES`/`outcome_survives_rescan`), `api/files.py`, and
  `frontend/src/hooks/useLiveModel.ts`.
- Read `docs/decisions.md`'s newest entries — this bug is recorded as point 7 of the
  post-processing state-persistence entry, which is where it was found.

## Tests

- Extend `tests/test_ws_deltas.py` and assert the invariant directly: **for every published node,
  the state on the wire equals the state in the `item` table.** Cover a protected job-lifecycle
  state, a post-processing state, a grace-period `REMOVED_LOCAL`, and the plain structural case.
- Keep and extend the delta-size assertions (the phase 3b property).
- Add a case proving the connect-time `snapshot()` agrees with the database too — the reload path
  is how this bug is actually visible.

## Explicitly out of scope

- Changing any state *rule*. This changes only which already-decided state reaches the wire.
- Redesigning the WebSocket message shape. If you believe the payload must change, stop and report.
- Frontend changes beyond whatever the rename forces (ideally none — the wire shape is unchanged).

## Conventions to honor

- Comments explain **why**, matching the surrounding density and voice. Cite `DESIGN.md` sections.
- `uv run ruff format --check` **and** `uv run ruff check` (run the format check explicitly — it
  has caught files `check` alone missed four times here), plus the full `uv run pytest`
  (**486 passing**; no regressions).
- Frontend gates if the rename reaches it: `npm run build` and `npm run lint` clean.
- **No browser exists here.** You *can* connect to the live WebSocket
  (`ws://localhost:8087/api/ws`) against the running dev stack — do that and report the actual
  messages observed, including a reconnect snapshot. Never claim the Files page renders correctly.
- The dev stack and fake seedbox are **running and in use by the user**. Leave every container
  running; do not disturb `/data/pickup`.
- Propose `DESIGN.md` §2/§9 wording stating the invariant (what is published is the persisted
  state, not the structural one) in your report — **do not edit `DESIGN.md` yourself.**

## Working tree check

Run `git status --porcelain` first. The tree is dirty on purpose with many unrelated completed
changes: dev-environment fixes, `_UNPACK_` extraction, Settings → Transfer, post-processing state
persistence, the empty-directory reconcile fix, a logging change, a metrics/Dashboard feature
(new `core/metrics.py`, `api/metrics.py`, migration 005, Dashboard page), and a Files
expand/collapse change. **None are yours — do not revert, refactor, or tidy them.**
`CHANGELOG.md`, `standards.md`, `prompts/startnewsession.md`, `.claude/commands/release-prep.md`
were dirty before the session; leave them alone. Append to `docs/decisions.md` at the top. Several
files you must touch (`core/engine.py`, `core/reconcile.py`) are already dirty from today's work —
**read those changes first and build on them**; stop and ask only if they genuinely conflict.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record the decision in `docs/decisions.md`, newest at top — the single-authority rule, the
   rename's rationale, and your answer on scan-path vs `item_delta` overlap.
4. **Do not commit. Do not push.** Prepare the tree, then report back with the file list and a
   proposed one-line commit message (`fix:` prefix, no `Co-authored-by:` trailer).
