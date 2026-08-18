# Sonarr / Radarr integration — spec

Status: **approved 2026-08-15, build dispatched** (three handoff prompts:
`2026-08-15-arr-integration-{backend,notify-cleanup,ui-and-docs}.md`). The relevant parts
get folded into `DESIGN.md` (new §16) in the final build commit, per the doc rules.

## The user story

A queue's downloads are driven by Sonarr (or Radarr) sending grabs to a torrent client on the
seedbox. lftpweb is the piece that lands those bytes locally. The integration closes the loop:

1. A new remote item appears. lftpweb asks the queue's bound *arr instance* "is this in your
   download queue?" If yes, the Files row gets a Sonarr/Radarr icon — visible proof that this
   item is *arr-driven and being watched through the pipeline.
2. lftpweb downloads, verifies, extracts, renames into final position — the existing
   post-processing pipeline, unchanged.
3. Optionally, lftpweb then tells the *arr "your files are here, import now" via its command
   API (`DownloadedEpisodesScan` / `DownloadedMoviesScan`).
4. lftpweb watches the *arr's queue + history until the item has actually been **imported**.
   Then, per a per-queue setting:
   - **Delete completed** ON → clean up the local copy (the *arr has its own copy/hardlink in
     the library now) and suppress re-download.
   - **Delete completed** OFF → the icon flips to a "imported ✓" state and the files stay.

## Assumed topology (worth stating because it constrains the design)

- The *arr talks to a download client **on the seedbox**. Its queue is therefore already
  populated with these releases, with a seedbox-side `outputPath`, before lftpweb ever sees
  them. This is what makes step 1's matching possible at all.
- The *arr may or may not have a Remote Path Mapping configured (seedbox path → local synced
  path). If it does, Sonarr's own Completed Download Handling (CDH) will try to import on its
  own once files appear locally — and lftpweb's `.downloading-<name>` transfer prefix already
  protects CDH from importing a partial or unverified release (that is literally what the
  prefix was built for; the *arr's periodic import retry succeeds only after our rename).
  If it doesn't, the push-scan command in step 3 is what triggers import.
- Both paths are supported; the push is a toggle. Neither is required for the icons.

## Scope

- **Sonarr and Radarr, v3 API.** The two APIs are shape-identical for everything we touch
  (queue, history, command, system/status); only command names and media nouns differ. One
  client, a `kind` field. Lidarr/Readarr/Whisparr are out of scope but nothing precludes them.
- **Binding is per queue** (user decision, 2026-08-15): one queue ↔ at most one *arr
  instance. One instance may be bound to many queues. A queue with no instance gets no icons
  and no *arr behavior at all. This kills the hard matching problem (which instance owns this
  item?) by construction.

## Data model — migration `018_arr_integration.sql`

```sql
CREATE TABLE arr_instance (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,                 -- display name, e.g. "Sonarr", "Radarr 4K"
    kind        TEXT NOT NULL CHECK (kind IN ('sonarr','radarr')),
    base_url    TEXT NOT NULL,                 -- e.g. https://sonarr.crzynet.com
    api_key_enc TEXT NOT NULL,                 -- encrypted at rest via core/crypto.py,
                                               -- exactly like the seedbox password
    enabled     INTEGER NOT NULL DEFAULT 0,    -- defaults OFF, per project rule
    notify_on_complete INTEGER NOT NULL DEFAULT 0,  -- push the scan command after postprocess
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

ALTER TABLE path_queue ADD COLUMN arr_instance_id INTEGER
    REFERENCES arr_instance (id) ON DELETE SET NULL;   -- NULL = no integration
ALTER TABLE path_queue ADD COLUMN arr_delete_completed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE path_queue ADD COLUMN arr_visible_path TEXT;
    -- this queue's local_path AS THE BOUND *ARR SEES IT (its container/host namespace).
    -- NULL = same namespace, no translation. See "Path namespaces" below.

ALTER TABLE item ADD COLUMN arr_status TEXT;           -- NULL | detected | notified
                                                       -- | imported | cleaned | dropped | gone
ALTER TABLE item ADD COLUMN arr_status_at TEXT;        -- when arr_status last changed
ALTER TABLE item ADD COLUMN arr_download_id TEXT;      -- the *arr queue record's downloadId
                                                       -- (infohash), recorded at match time;
                                                       -- makes history lookup exact. Not
                                                       -- published in the item projection.
```

`dropped` (2026-08-18, `prompts/2026-08-18-arr-gone-grace-and-recheck.md`) is a value, not a
migration -- `arr_status` is plain `TEXT` with no `CHECK` constraint (unlike `arr_instance.kind`
above), so a new value needs no schema change at all.

No new rows inserted; every existing install behaves identically after migration (icons need
an instance created + enabled + bound, three explicit acts).

`arr_status` is a **facet, not a lifecycle state** — it never touches `item.state` and the
reconciler never writes it. This follows the established "presence icons read the world;
milestone icons read timestamps" split, and keeps the delicate §3.2 state machine untouched.
It joins `ITEM_VIEW_COLUMNS` in `core/itemview.py` so the WebSocket, the snapshot, and
`GET /api/files` all carry it through the one projection (the publish invariant).

## The association lifecycle

```
(no status) ──match──▶ detected ──postprocess OK + notify──▶ notified
                          │                                     │
                          └────────────── import seen ──────────┤
                                                                ▼
                                            delete_completed?  imported
                                              ON → cleanup → cleaned
                                              OFF → stays imported (green icon)

   queue record vanished, no import event, confirmed over 2 passes ──▶ dropped (amber, rechecking)
     dropped ──same downloadId reappears──▶ detected
     dropped ──import event surfaces──▶ imported (rung 4 + cleanup proceed normally)
     dropped ──neither, for DROPPED_GONE_GRACE_S (6h)──▶ gone (red; nothing deleted, ever)
```

**`dropped` (2026-08-18, production incident, support bundle
`lftpweb-support-0.2.3-20260818T013532Z`)** sits between the two-pass quiescence guard and the
old, terminal `gone`. SABnzbd sometimes returns a blank/empty queue to Sonarr's own poll; this
codebase's poller runs slower than that blip (once a minute), so both of the guard's observation
passes can land inside the same blank window -- 8 real items committed straight to `gone` in a
single pass while lftpweb was still actively downloading them (their verify/rename events ran
minutes later, proving no import could have existed yet). `gone` is deliberately terminal (a
settled row refuses to re-match on an identical `downloadId`, below) and the stranded-source-
delete sweep only retries `imported`/`cleaned` rows, so all 8 rows sat with a permanent red dot,
a parked rung-4 source delete, and no cleanup -- even though the *arr imported every one of them
normally an hour later.

The two-pass guard confirming "record gone, no import evidence" now commits `dropped` instead of
`gone`, and the row is re-checked **every subsequent poller pass** -- not gated behind another
two-pass observation, since `dropped` itself already *is* the held-for-confirmation state:

- The identical `downloadId` reappearing in the queue is direct evidence the disappearance was a
  blip, not a removal, and sends the row straight back to `detected`. This is the deliberate
  *opposite* of the `gone`/`cleaned` matching rule below (a settled row refuses to re-match on
  that same identical `downloadId`) -- `dropped` is not settled yet, so it gets the opposite
  treatment. See `docs/decisions.md`, 2026-08-18.
- A history import event surfacing while `dropped` promotes straight to `imported` through the
  normal two-pass-guard's own commit path -- rung 4's deferred source delete and cleanup then
  proceed exactly as they would for any other import.
- Only once `arr_status_at` is older than `DROPPED_GONE_GRACE_S` (a deliberate, named module
  constant defaulting to **6 hours** -- `core/arrsync.py`, `docs/concepts.md`; a settings knob is
  a named future option, not built now) with neither signal does the row finally commit `gone`.

**Retroactive self-heal.** A row that already committed the old, direct `gone` before this
shipped -- the production 8, and any like them -- is healed automatically:
`core/arrsync.py._heal_stranded_gone_rows` re-queries `import_events` by the item's own stored
`arr_download_id` for every `gone` row still carrying a stranded rung-4 delete debt
(`remote_delete_pending IS NOT NULL AND remote_deleted_at IS NULL`) -- the query alone is the
self-heal, no migration or state massaging needed. An import event found promotes the row to
`imported` through the normal path (rung-4 delete + cleanup then proceed normally); bounded by
`MAX_GONE_HEAL_ATTEMPTS`, growing-delay backoff (same shape as the rung-4 retry sweep), so a
genuinely-gone row is not queried forever -- one `arr_gone_heal_giving_up` event on giving up. A
row with no recorded `arr_download_id` (an old title-fallback match) still counts each check as
an attempt, since no exact lookup is possible for it.

- **detected** — a record in the bound instance's `/api/v3/queue` matched this item.
- **notified** — the scan command was POSTed (only if `notify_on_complete`; skipped state
  otherwise).
- **imported** — the *arr's history shows an import event for this release AND the queue
  record is gone. **Both are required before anything destructive happens.** Queue
  disappearance alone is ambiguous — the user may have removed the item, or the grab may
  have failed — so it maps to `gone`, which is terminal-but-safe: icon dims, no cleanup,
  ever. Never delete on ambiguity.

  **"Imported" must mean the *arr is FULLY done, not merely started** (user decision,
  2026-08-15). A 40 GB season pack copied over the network takes real time, and the *arr
  imports a multi-file release file by file — one history import event lands *after each
  file's copy completes*, so a history event is a trailing per-file indicator, never a
  whole-release one. The whole-release signal is the **queue record's own lifecycle**: the
  record stays present with `trackedDownloadState: importing` for the entire import pass
  and is removed only when the *arr has dealt with the whole release. Three layered
  requirements before `imported` is set:

  1. The queue record is **gone** (or reports `trackedDownloadState: imported`) — the *arr
     itself says the release is finished. An item whose record shows `importing` is never
     `imported`, no matter what history says; the icon's hover can surface "importing…" as
     a courtesy.
  2. ≥1 history import event for the release (by `downloadId` when known) — proves the
     disappearance was an import, not a removal.
  3. **Both signals must hold on two consecutive poller passes** (≥60s apart) — a
     settle-gate-style quiescence guard, same philosophy as the transfer settle gate:
     defense against API races, restarts mid-import, and any *arr version whose
     queue-removal timing differs from the above. Cleanup is the irreversible step, so it
     gets the same "unchanged for two observations" treatment the remote fingerprint does.
- **cleaned** — local copy removed by us because the *arr is done with it.

Every transition writes an `event` row (`core/audit.py`): `arr_matched`, `arr_notified`,
`arr_notify_failed`, `arr_imported`, `arr_cleanup`, `arr_cleanup_withheld` (with the reason
in the message, same discipline as the delete gate — the audit trail must be able to answer
"why wasn't this cleaned up?" in one HTTP call).

## Path namespaces — three views of the same directory

This feature spans **three** path namespaces, and confusing them is its most likely bug
class (the logical-vs-physical lesson, one level up):

1. **The seedbox namespace** — where the download client writes
   (`/home/user/torrents/complete/...`). lftpweb knows it as the queue's `remote_path`.
2. **lftpweb's namespace** — the queue's `local_path` inside the lftpweb container
   (`/downloads/...`).
3. **The *arr's namespace** — what Sonarr/Radarr sees the synced directory as, in *its*
   container/host (`/data/torrents/...`). The *arr's own Remote Path Mappings translate
   namespace 1 → 3 already (the user runs these today); lftpweb never sees or manages
   those mappings.

Rules that follow:

- **Matching never compares full paths across namespaces** — it keys on the release
  **basename**, which is identical in all three. See below.
- **Any path lftpweb *sends to* the *arr must be in namespace 3.**
  `path_queue.arr_visible_path` is the translation: the notify path is the item's final
  physical path with the queue's `local_path` prefix replaced by `arr_visible_path`.
  `NULL` means "same namespace" (shared mount, identical paths) and sends our path
  unchanged. Note the substitution must be applied to the item's **post-move** location —
  if the queue's Move step relocates to `staging_path`, `arr_visible_path` describes where
  *that* lands in the *arr's view, and the Settings UI help text must say so.
- **Any path received *from* the *arr** (queue `outputPath`, history `droppedPath`/
  `importedPath`) is namespace 3 (or occasionally 1, if the user's *arr mapping is absent) —
  never trusted as a local path, never touched on disk, used only for basename matching.

## Matching — how a queue record maps to an item

The bound instance's queue records carry `outputPath` (the download path as the *arr sees
it — post its own remote path mapping, so namespace 3, e.g.
`/data/torrents/complete/Show.S01E05.1080p-GRP/`) and `title`. Match, in order:

1. **Basename of `outputPath` == item name** (the top-level entry name lftpweb scans). This
   is the release directory name and is exact in the normal case.
2. Fallback: `title` normalized (case-fold, `.`/`_`/space equivalence) == item name
   normalized. Covers single-file releases where `outputPath` includes the filename, and
   clients that rename.

Match against the item's **logical** name — never the physical `.downloading-<name>` path
(the five-defects trap; `scan_local` already publishes logical names, so matching at the
item-table level gets this for free, but any future code touching disk here must go through
`core/local_delete._physical_local_root`).

Only top-level items are candidates (an *arr queue record is a release, and a release is a
top-level item by §4.7's narrow definition). `downloadId` (infohash) is recorded on the item
association when present — it makes the later history lookup exact instead of name-based.

## The poller — `core/arrsync.py`

A background loop in the `BackupScheduler` / `Engine` shape (`_task`/`start()`/`stop()`),
**not** wired into the scan pass — scan cadence is per-queue and variable, and *arr polling
wants its own clock. Default every **60s** (site setting, `ArrSettings` in `setting` like
every other settings dataclass):

1. For each enabled instance with ≥1 bound queue: `GET /api/v3/queue` (one page walk).
2. Match records → items in bound queues; set `detected` on new matches.
3. For items in `detected`/`notified`: check queue presence; on disappearance, query
   `/api/v3/history` (by `downloadId` when known, else by `sourceTitle`) for an import
   event → candidate `imported` or `gone`. `imported` is only committed after the
   two-consecutive-passes quiescence guard (see the lifecycle section) confirms the *arr
   is fully done — a record still showing `trackedDownloadState: importing` always means
   "not yet".
4. Immediately on committing `imported`: on a `move`-mode queue, if `core/postprocess.py`
   deferred this item's remote delete (`item.remote_delete_pending` set — rung 4 of the delete
   ladder, `prompts/done/2026-08-16-move-delete-gate-ladder.md`), perform it now, before
   anything else this pass touches the item. Never on `gone`.
5. For `imported` items on a `arr_delete_completed` queue: run cleanup (below) — after step 4,
   so a `move`-mode item's remote copy is already gone before local cleanup ever runs.

An unreachable instance logs once at WARNING, writes one event row, and backs off
(exponential, capped); it must never block or slow the loop for other instances, and never
touches the scan/transfer engine at all. All HTTP via **httpx** (currently a dev-only
dependency — promoted to runtime; async client, 10s timeout, `X-Api-Key` header).

## Notify — "your files are here, import now"

Fires from the tail of `PostprocessPipeline.process_item`, **after the whole pipeline
succeeds** — after verification, extraction, the `.downloading-` → real-name rename, *and*
the Move-to-final relocation if the queue does one. The path sent is the item's **final
physical location at that moment** (post-move), translated into the *arr's namespace via
`arr_visible_path` (see "Path namespaces"), because that's the only path the *arr can
import from:

- Sonarr: `POST /api/v3/command {"name": "DownloadedEpisodesScan", "path": <final path>, "importMode": "Copy"}`
- Radarr: same with `"DownloadedMoviesScan"`.

`importMode: Copy` deliberately (the *arr's own hardlink-vs-copy setting still governs the
actual mechanics): a `Move` import would rip files out from under lftpweb mid-tracking. The
existing `REMOVED_LOCAL` grace machinery would cope, but Copy keeps "who deletes what" in
exactly one place — our cleanup step. If the *arr imports by hardlink, our later cleanup
costs nothing and the library copy survives, which is the ideal path.

Notify failure is non-fatal: event row + retry on the next poller tick (bounded retries),
because CDH may import anyway.

## Cleanup — "delete completed"

Per-queue toggle (`arr_delete_completed`), default **OFF**, only meaningful when an instance
is bound. When an item reaches `imported` on such a queue:

1. Set `auto_queue_suppressed = 1` **before** touching disk. On a `copy` queue the remote
   copy still exists (seedbox keeps seeding — untouched, that's `sync_mode`'s business, not
   ours), so without suppression the per-queue re-download toggle could re-queue the release
   forever — the exact `ELIGIBLE_STATES` loop `prompts/open-issues.md` documents. Belt and
   braces on top of `REMOVED_LOCAL`'s default exclusion.
2. Delete the local tree via the existing local-deletion machinery (`core/local_delete.py`,
   resolving through `_physical_local_root` — never a second resolver).
3. `arr_status = cleaned`, event row. The item then ages into `REMOVED_LOCAL` through the
   normal grace machinery, exactly as if a human had deleted it.

**The cleaned item stays visible through the existing ~10-minute removal grace window**
(user decision, 2026-08-15) — someone watching the queue sees the row move
downloaded → processed → (countdown) → gone, rather than vanish the instant cleanup runs.
This reuses the grace machinery and its countdown chip as-is — **no new timer** — with one
presentational override: the chip normally reads "Missing · Xm" because an unexplained
absence means a decision is pending, but here the absence is deliberate and audited, so a
row with `arr_status = cleaned` renders the countdown as processed ("Processed · Xm" with
the *arr mark), not as an alarm. Same clock, different words.

Cleanup is **withheld** (with an `arr_cleanup_withheld` event naming why) when: the item is
not `imported`; verification for the item had failed; or a job for the item is active. On a
`move`-mode queue this composes with the delete ladder (`core/postprocess.py`/`core/arrsync.py`,
`prompts/done/2026-08-16-move-delete-gate-ladder.md`), not before it: an *arr-tracked item's
remote copy is deliberately still there when it reaches `imported` (the pipeline defers rung 4
to this poller rather than deleting at `DOWNLOADED` time), so `_commit_terminal` performs that
deferred delete itself, in the same pass, **before** the `arr_delete_completed` sweep below ever
runs — "import green → delete source → delete local," never the reverse. The release is fully
gone once the *arr has it, which is the point.

## API surface

- `GET/POST /api/settings/arr` + `PUT/DELETE /api/settings/arr/{id}` — instance CRUD
  (api_key write-only, never echoed — same convention as the seedbox password).
- `POST /api/settings/arr/{id}/test` — `GET /api/v3/system/status` round-trip; returns
  version + reachability. The Settings UI's "Test" button.
- `path_queue` PATCH gains `arr_instance_id`, `arr_delete_completed`, `arr_visible_path`
  (existing queues endpoint, `settings_queues.py`).
- `arr_status`/`arr_status_at` ride the existing item projection — no new item endpoint.

## UI

- **Settings → Integrations** (new tab): instance list, add/edit form (name, kind, URL, API
  key, enabled, notify-on-complete), Test button.
- **Settings → Queues**: an "*arr instance" dropdown per queue, the "Delete when imported"
  checkbox (disabled with a hint unless an instance is selected), and the "Path as seen by
  the *arr" field (`arr_visible_path`, optional, with FieldHelp explaining the namespace
  translation and that it describes the *post-move* location for a queue that relocates).
- **Files row + Transfers row + History job row** — one shared chip, `ArrRowChip`
  (`LifecycleIcons.tsx`), driven purely by `arr_status` from the WS stream / REST projection.
  Introduced for the Transfers/History row line first (2026-08-16,
  prompts/2026-08-16-arr-chip-on-row-lines.md), then adopted by the Files tree the same day
  (prompts/2026-08-16-files-brand-logo-icons.md — user feedback: "one visual language
  everywhere," after the Files page had shipped a day earlier with its own, separate generic
  mark). Renders the **real** Sonarr/Radarr brand logo (user decision — recognition is the
  point), in the mark's own brand colour (Sonarr blue `#2596be`, Radarr gold `#ffcb3d`), never
  tinted by status; the outcome rides a small overlay badge instead, **multi-faceted** (original
  Files decision, 2026-08-15, carried into this shared chip) — "the *arr processed it" and "the
  *arr merely dropped it" must be visually distinct states, never one dimmed glyph:

  | `arr_status` | Overlay | Meaning at a glance |
  |---|---|---|
  | `null` | *(no chip at all)* | not *arr-tracked |
  | `detected` / `notified` | none | Sonarr/Radarr queued this; it's being watched through the pipeline |
  | `imported` | green ✓ badge | the *arr confirmed import; files kept (delete-completed off) |
  | `dropped` | amber pending dot | left the *arr's queue moments ago with no import evidence yet — rechecked every pass; not yet actionable (2026-08-18) |
  | `gone` | red dot badge | stayed unconfirmed past the `dropped` grace window — likely a failed/removed grab, may need attention |
  | `cleaned` | green ✓ badge + (Files only) "Processed · Xm" countdown | imported and locally cleaned up; visible through the ~10m removal grace, then leaves via the normal `REMOVED_LOCAL` flow |

  `cleaned` shares the `imported` row's green ✓ (decision, 2026-08-16, first live Radarr
  run): with "Delete when imported" on, `imported` is a seconds-long transient — cleanup
  runs on the very next poller beat — so a green check that only lived on `imported` would
  flash and vanish before it was ever seen. The hover text still says "imported" vs.
  "imported and cleaned up locally" so the two states stay tellable apart despite sharing
  a chip.

  An unrecognized/future instance `kind` falls back to a text chip of the instance name, in
  the same green/red/neutral status colours (`ArrTextChip`), rather than rendering nothing for
  a tracked item. Transfers/History read `kind` straight off the wire — `core/queue.py.
  list_jobs()` and `api/history.py.list_history_jobs()` both join `arr_instance.kind` alongside
  `arr_instance.name` (`JobOut`/`HistoryJobOut.arr_instance_kind`; `HistoryJobOut` also gained
  the `arr_status`/`arr_status_at`/`arr_instance_name` fields the Transfers panel already had).
  The Files tree has no such per-item field — `FilesPage.tsx` resolves both `name` and `kind`
  itself, from each queue's own `path_queue.arr_instance_id` against `GET /api/settings/arr`,
  and threads them down through `FileTree`/`Row` as props, the same "fetched once, passed down"
  shape it already used for the instance name alone. Logo path data: simple-icons dataset
  (CC0), itself citing Sonarr's/Radarr's own repositories — see `NOTICE`.

  Hover card names the instance and the timestamp (`arr_status_at`). Files' text/state
  filters gain an "*arr-tracked" facet, and `gone` is filterable on its own since it's the
  actionable one. (As always: an agent cannot see this render; it ships unviewed until the
  user opens it.)

  A second, generic "linked to an external system" mark (`ArrIcon`, Lucide `link-2` — this
  codebase's original choice not to ship third-party brand logos, before the brand-logo chip
  above existed) still exists for exactly one remaining spot: the Transfers/History **job
  detail drawer**'s own "*arr" section (icon + a full sentence, not a row-line chip) — `gone`
  reads **amber** there, the one place the two specs still differ, since that surface never
  needed brand recognition or an overlay badge, just the plain mark beside its own text.

## Defaults & safety (the standing rule: everything OFF)

Three independent, escalating opt-ins, each defaulting off:

1. Instance `enabled` — off → nothing polls, nothing matches, icons never appear.
2. Instance `notify_on_complete` — off → we never POST commands; CDH may still import.
3. Queue `arr_delete_completed` — off → we never delete anything. The only destructive
   switch, per-queue, and gated behind a confirmed import event even when on.

## Failure modes & interactions worth naming now

- **The `.downloading-` prefix is load-bearing for CDH** — if a future change ever made the
  prefix optional-off *and* the user relies on CDH, partial imports come back. The prefix
  default stays on.
- **History lookup by name is fuzzy; by `downloadId` it is exact.** Record `downloadId` at
  match time; fall back to `sourceTitle` matching only when absent. A wrong-positive here
  triggers deletion, so the matcher must prefer precision over recall — an unmatched import
  just means the icon never turns green.
- **A season pack is one queue record and one item** — fine. But an *arr can grab the same
  episode twice (upgrade): a second record matching an already-`cleaned` item name must
  start a *fresh* association, not resurrect the old one. Keyed on (item id, downloadId)
  when deciding "new match".
- **`GET /api/v3/queue` pagination** — walk all pages; a busy instance can exceed one page.
- **Sonarr and Radarr numeric `eventType` codes and the exact `trackedDownloadState`
  vocabulary must be verified against a live instance during the build, not trusted from
  memory** — same lesson as `test_lftp_settings_accepted.py`: assert against the real
  program, not the docs. The fully-done detection leans on `importing` vs record-removal
  timing, so the fake-*arr test fixture must model a **slow multi-file import** (record
  present in `importing` with per-file history events accreting) and the test must prove
  cleanup does not fire until the record is gone and the quiescence guard has passed.
- **The seedbox seed vs. `move` mode** (§7.1) — unchanged by this feature, but a user wiring
  an *arr in will usually be seeding; the existing `move` warning covers it.

## Build plan (when approved — handoff prompts, Sonnet)

1. **Backend foundation**: migration 018, `core/arrclient.py` (httpx, one class, `kind`
   switch), `core/arrsync.py` poller with match + import detection, settings dataclass, API
   CRUD + test endpoint, event rows. Tests against a fake *arr (a small FastAPI fixture app
   speaking the three endpoints — same philosophy as the fake seedbox: real HTTP, not mocks).
2. **Notify + cleanup**: the postprocess tail hook, the cleanup path with suppression,
   withheld events, the per-queue columns on the queues API.
3. **UI**: Integrations tab, Queues additions, Files icon + filter. Browser-verified session.

## Resolved decisions (user, 2026-08-15)

1. **CDH is live today** — the user's Sonarr/Radarr already see the download path via their
   own Remote Path Mappings, so imports happen without lftpweb pushing anything. The
   `notify_on_complete` push is still specced (it accelerates import and covers installs
   without a mapping) and gains the `arr_visible_path` substitution so the pushed path is
   valid in the *arr's namespace even when the two containers mount the directory
   differently.
2. **One instance per queue stands.** Sonarr + Sonarr-4K setups use distinct processing
   paths in practice (they'd confuse each other otherwise), so many-to-many binding is not
   built.
3. **The icon is multi-faceted** — "the *arr processed it" (`imported`, green ✓) and "the
   *arr removed it without importing" (`gone`, amber ⚠ at the time of this decision — the
   row-line chip all three surfaces now share reads `gone` as a red dot instead, per the "UI"
   section above; amber survives only on the job-detail drawer's plain mark) are distinct
   visual states, and `gone` is independently filterable since it's the one that usually needs
   a human.
