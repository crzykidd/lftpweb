// Mirrors backend/lftpweb/models.py — extended as each phase adds endpoints.

export interface HealthResponse {
  status: string
  version: string
  db: boolean
  uptime_s: number
  repo_url: string
  // Phase 7, DESIGN.md §10.3. `null` = no host configured yet (distinct from `false`, "a
  // host exists but the pooled connection last failed").
  host_reachable: boolean | null
  scheduler_alive: boolean
  // 2026-08-16 (docs/decisions.md): baked at image build time, `null` for every build that
  // never baked them (local dev, compose dev stack, a manual `docker build` with no
  // `--build-arg`) -- see `lib/versionBadge.ts` for how the nav's version readout uses them.
  build_sha: string | null
  build_channel: 'dev' | 'release' | null
}

export interface StatsResponse {
  current_speed_bps: number
  allocated_bps: number
  ceiling_bps: number
  queued_count: number
  queued_bytes: number
  transferred_24h_bytes: number
}

// --- Settings -> Connection (phase 2, DESIGN.md §3.1 `host`, §9.2) ---------------------

export type AuthMethod = 'key' | 'agent' | 'password'
export type KnownHostsPolicy = 'accept-and-pin' | 'strict' | 'insecure'

export interface HostOut {
  id: number
  name: string
  address: string
  port: number
  username: string
  auth_method: AuthMethod
  key_path: string | null
  has_password: boolean
  // migration 014 (DESIGN.md §8): whether a pasted key is currently stored, mirroring
  // `has_password` -- never the key itself.
  has_ssh_key: boolean
  // Which of `key_path` / a pasted key is actually in use for `auth_method === 'key'` -- the
  // pasted-wins-over-path rule is decided once, server-side (`api/settings.py`), so this is
  // read, never re-derived. `null` when `auth_method !== 'key'` or neither is set.
  active_key_source: 'pasted' | 'path' | null
  known_hosts_policy: KnownHostsPolicy
  credentials_need_reentry: boolean
  // Read-only (DESIGN.md §4.5/§9.3, docs/decisions.md 2026-08-12): whatever currently sits
  // in `net:connection-limit` inside the host's `connection_overrides` JSON blob, or null if
  // unset. There is no field on `HostIn` to set it -- Settings → Connection has no UI for
  // it -- so this is `null` on every install that hasn't hand-edited the database.
  net_connection_limit: number | null
}

// Mirrors HostIn — password/ssh_key are plaintext here only, and only ever sent, never
// received back (§9.2: neither must ever round-trip the stored secret to the browser).
export interface HostIn {
  name: string
  address: string
  port: number
  username: string
  auth_method: AuthMethod
  key_path: string | null
  password: string | null
  // migration 014: an *additional* way to satisfy `auth_method === 'key'`, alongside
  // `key_path` -- not a replacement. Wins over `key_path` when both are set.
  ssh_key: string | null
  known_hosts_policy: KnownHostsPolicy
}

export interface HostTestRequest {
  name?: string | null
  address?: string | null
  port?: number | null
  username?: string | null
  auth_method?: AuthMethod | null
  key_path?: string | null
  password?: string | null
  ssh_key?: string | null
  known_hosts_policy?: KnownHostsPolicy | null
}

export interface TestConnectionResponse {
  ok: boolean
  error_class: string | null
  message: string
}

// --- Settings -> Queues (phase 2, DESIGN.md §3.1 `path_queue`) -------------------------

export type SyncMode = 'copy' | 'move' | 'sync'

export interface PathQueueIn {
  name: string
  remote_path: string
  local_path: string
  staging_path: string | null
  enabled: boolean
  sync_mode: SyncMode
  // Phase 4 (DESIGN.md §4.7). Both default off/false -- enabling auto-queue is an explicit
  // user action; omitting these on create must not auto-enable anything.
  auto_queue_enabled: boolean
  auto_queue_patterns_only: boolean
  // Phase 5 (DESIGN.md §6); nullable-for-inherit as of 2026-08-13
  // (prompts/2026-08-13-postprocess-inherit-or-override.md). `null` means "inherit the
  // matching Settings -> Post-processing site-wide flag" -- the default, and what every
  // existing queue's row was set to by migration 015. `true`/`false` is an explicit per-queue
  // override, independent of the site-wide flag in either direction; the backend no longer
  // ANDs it with the site-wide flag. The backend forces auto_verify to an explicit `true`
  // whenever sync_mode is 'move' regardless of what's sent here -- the UI mirrors that by
  // disabling (not hiding) the checkbox rather than relying on the server alone.
  auto_verify: boolean | null
  auto_extract: boolean | null
  auto_move: boolean | null
  // Migration 012 (2026-08-13); nullable-for-inherit alongside the three above as of the task
  // cited above. Archive cleanup (Settings -> Post-processing -> Extract) shipped site-only
  // and was the odd one out; this is its per-queue half.
  auto_delete_archives: boolean | null
  // Migration 009 (prompts/done/2026-08-12-per-queue-scan-interval.md). `null` -- the default,
  // and what an existing queue already has -- means "use the site-wide default (30s)"; `0`
  // means on-demand only (no timer; "Rescan now" and auto-queue-driving passes still work when
  // something else forces a scan); any positive number is a literal per-queue interval in
  // seconds. The backend rejects a negative value with a 400.
  scan_interval_s: number | null
  // Migration 017 ("folder prefix during transfer", core/download_prefix.py). Both
  // nullable-for-inherit, resolved independently -- `null` means "inherit the matching
  // Settings -> Transfer field," an explicit value is this queue's own override. Directory
  // items only; see Settings -> Transfer's own section for why.
  download_prefix_enabled: boolean | null
  download_prefix: string | null
  // Sonarr/Radarr integration (migration 018, docs/arr-integration-spec.md "Data model" /
  // "API surface"). Binding is per-queue, one instance at most -- `null` (the default, and
  // every existing queue's value after the migration) means "no integration": no icons, no
  // matching, no *arr behavior at all for this queue. Full-replace fields, like the rest of
  // this interface (not the four post-processing toggles' merge-on-absence shape) -- Settings
  // -> Queues' edit form always submits the complete queue state.
  arr_instance_id: number | null
  // Default off, per-queue, and only meaningful when `arr_instance_id` is set -- the backend
  // rejects `true` with no bound instance (`api/settings_queues.py._validate_arr_binding`).
  arr_delete_completed: boolean
  // This queue's `local_path`, translated into the bound *arr's own namespace (spec "Path
  // namespaces") -- `null` means "same namespace, no translation," never an empty-string
  // sentinel.
  arr_visible_path: string | null
}

export interface PathQueueOut extends PathQueueIn {
  id: number
  host_id: number
}

// --- Settings -> Integrations (migration 018, docs/arr-integration-spec.md) -------------
//
// Mirrors `backend/lftpweb/models.py`'s `ArrInstanceIn`/`ArrInstanceOut`/`ArrTestResponse`.
// Sonarr and Radarr, v3 API, one client with a `kind` switch (spec "Scope"). Binding an
// instance to a queue happens on `PathQueueIn.arr_instance_id` above, not here -- this is
// only the instance CRUD + connectivity test.

export type ArrKind = 'sonarr' | 'radarr'

/** A create/update request body. `api_key` is plaintext here -- the only place it ever
 * appears -- and is encrypted at rest server-side before touching the database, the same
 * convention `HostIn.password` uses. Omitting it on an update keeps the stored key (the
 * identical "unchanged must not mean cleared" rule `settings_host.py.put_host` follows) --
 * `IntegrationsTab.tsx` never pre-fills this field with a real value, only a placeholder.
 */
export interface ArrInstanceIn {
  name: string
  kind: ArrKind
  base_url: string
  api_key?: string | null
  enabled: boolean
  notify_on_complete: boolean
}

export interface ArrInstanceOut {
  id: number
  name: string
  kind: ArrKind
  base_url: string
  // Never the key itself (DESIGN.md §9.2's "must never round-trip the stored secret back to
  // the browser") -- whether one is on file, mirroring `HostOut.has_password`.
  has_api_key: boolean
  enabled: boolean
  notify_on_complete: boolean
  created_at: string
  updated_at: string
}

/** `POST /api/settings/arr/{id}/test` -- the `GET /api/v3/system/status` round trip, the
 * Settings UI's Test button. Never a non-2xx for a reachable-but-erroring instance; the
 * failure is reported in `message`/`error_class`, the same "test tells you what's wrong,
 * doesn't throw" shape `TestConnectionResponse` already uses for the seedbox.
 */
export interface ArrTestResponse {
  ok: boolean
  error_class: string | null
  message: string
  version: string | null
}

// --- Settings -> Post-processing (phase 5, DESIGN.md §6) -------------------------------

export interface PostprocessSettingsOut {
  verify_enabled: boolean
  verify_hash_on_disk: boolean
  extract_enabled: boolean
  extract_target_dir: string | null
  extract_passwords: string[]
  // Off by default -- deletes an item's spent archive volumes once they've extracted
  // successfully (2026-08-13). See core/local_delete.py.delete_extracted_archives.
  delete_archives_after_extract: boolean
  move_enabled: boolean
  concurrency: number
}

export type PostprocessSettingsIn = PostprocessSettingsOut

// --- Settings -> the settle gate (prompts/open-issues.md #2, `core/settle.py`) ---------

export interface SettleSettingsOut {
  enabled: boolean
  // Read-only -- core/settle.py.REQUIRED_SETTLE_SCANS / SETTLE_MIN_AGE_S. Not settable from
  // this API; surfaced only so the Settings page can explain what the gate requires without
  // hardcoding numbers that could drift from the backend's own constants.
  required_scans: number
  min_age_s: number
}

// Only `enabled` is writable -- `required_scans`/`min_age_s` are informational.
export interface SettleSettingsIn {
  enabled: boolean
}

// --- Settings -> the removal grace period (core/mount_sentinel.py, DESIGN.md §7.3) -----
//
// GET-only -- no `...In` counterpart. `DEFAULT_GRACE_S` isn't a per-install setting this
// phase (core/mount_sentinel.py's own comment); surfaced only so the Files page's removal-
// grace countdown (2026-08-14, prompts/2026-08-14-removal-grace-countdown.md) reads the real
// window instead of a second, hand-maintained 600 that could drift from the backend's own
// constant -- the same reasoning as SettleSettingsOut's required_scans/min_age_s above.

export interface RemovalGraceSettingsOut {
  grace_s: number
  /** The states the grace clock can actually run for -- `core/mount_sentinel.py.COMPLETE_STATES`,
   * shipped rather than duplicated here so a new post-processing state added on the Python side
   * can't silently stop being eligible in the UI. `lib/format.ts.REMOVAL_GRACE_ELIGIBLE_STATES`
   * is a bootstrap default for the render before this fetch resolves, not a second source of
   * truth; `tests/test_settings_api.py` pins the equality against the live set.
   */
  eligible_states: string[]
}

// --- Settings -> "folder prefix during transfer" (core/download_prefix.py) -------------
//
// Site-wide default; a queue's own `download_prefix_enabled`/`download_prefix` (PathQueueOut,
// above) can independently override either half. Off by default -- unlike the settle gate,
// this one was not given the "ships on" exception (see core/download_prefix.py's docstring):
// it changes where in-flight bytes physically live, which an install with a transfer already
// running when it upgrades would notice immediately.

export interface DownloadPrefixSettingsOut {
  enabled: boolean
  prefix: string
}

export type DownloadPrefixSettingsIn = DownloadPrefixSettingsOut

// --- Settings -> auto-queue (`core/autoqueue.py.AutoQueueSettings`) ---------------------
//
// Site-level, default false. Governs only whether an item something *outside* lftpweb removed
// (an `*arr` importer, a human, a script) is eligible to be re-fetched by auto-queue --
// lftpweb's own deletions (Files-page delete, retention) are never re-fetched regardless of
// this setting. Only matters for `copy`-mode queues; `move` deletes the remote copy on
// completion, so there is nothing left to re-fetch either way.

export interface AutoQueueSettingsOut {
  re_download_externally_removed: boolean
}

export type AutoQueueSettingsIn = AutoQueueSettingsOut

// --- Settings -> Queues -> Patterns (phase 4, DESIGN.md §3.1 `pattern`, §4.7) -----------

export type PatternKind = 'select' | 'skip' | 'file_exclude'

export interface PatternIn {
  queue_id: number | null // null = global, applies to every queue
  kind: PatternKind
  expr: string
  enabled: boolean
}

export interface PatternOut extends PatternIn {
  id: number
}

export interface PatternPreviewRequest {
  patterns: PatternIn[]
  patterns_only: boolean
}

export interface PatternPreviewItem {
  rel_path: string
  is_dir: boolean
  matched: boolean
}

export interface PatternPreviewFile {
  rel_path: string
  excluded: boolean
}

export interface PatternPreviewResponse {
  items: PatternPreviewItem[]
  sample_item: string | null
  sample_files: PatternPreviewFile[]
}

export interface QueueAutoQueueStatus {
  mount_ok: boolean
  gated_reason: string | null
}

// --- Settings -> Queues path-browse dialog (GitHub issue #4,
// prompts/done/2026-08-16-path-browse-dialog.md) -- api/browse.py. One shared response shape
// for both the local (container filesystem) and remote (seedbox over SFTP) endpoints.

export interface BrowseEntry {
  name: string
}

export interface BrowseResponse {
  path: string
  parent: string | null
  entries: BrowseEntry[]
  truncated: boolean
  // Set only when the endpoint had to walk up from what was actually requested -- see
  // core/browse.py's own docstring. `null` means `path` is exactly what was asked for.
  fallback_from: string | null
}

// --- Files (phase 2, DESIGN.md §9.2) ----------------------------------------------------

// Lifecycle facets (2026-08-13, prompts/2026-08-13-lifecycle-icons.md,
// core/itemview.py._lifecycle_facets) -- R(emote)/L(ocal)/V(erified)/E(xtracted), derived
// server-side from the same persisted row `state` comes from, so there is exactly one place
// that decides what a fact means. `level` drives color; `reason` plus this row's own raw
// size/timestamp fields (below) is what `FileTree.tsx` builds a tooltip sentence from --
// deliberately not a pre-formatted string, the same split `stateAgeLabel` already uses for
// `state`/`state_changed_at`.
export type FacetLevel = 'green' | 'amber' | 'red' | 'dim'

export interface LifecycleFacet {
  level: FacetLevel
  reason: string
}

export interface LifecycleFacets {
  remote: LifecycleFacet
  local: LifecycleFacet
  verified: LifecycleFacet
  extracted: LifecycleFacet
}

export interface FileNode {
  id: number | null
  rel_path: string
  is_dir: boolean
  state: string
  // The settle gate (prompts/open-issues.md #2): 'settling' for a top-level REMOTE_ONLY item
  // whose remote fingerprint hasn't held still for 2 consecutive scans yet. 'removing'
  // (2026-08-13, prompts/2026-08-13-delete-state-truthfulness.md) for the whole subtree of an
  // in-progress delete (core/local_delete.py.delete_local). null otherwise.
  substate: string | null
  // item.suppressed_reason (2026-08-13, prompts/2026-08-13-delete-state-truthfulness.md), null
  // unless auto_queue_suppressed is set server-side. Drives the "Re-Download" action label
  // below -- a self-delete ('deleted_local') reads differently from every other suppression
  // reason and from an unsuppressed REMOVED_LOCAL/REMOVED_BOTH row.
  suppressed_reason: string | null
  remote_size: number | null
  local_size: number | null
  remote_mtime: number | null
  // The local-side counterpart to `remote_mtime` (migration 011, 2026-08-13,
  // prompts/2026-08-13-files-detail-inspector.md) -- the item drawer's "modified date, both
  // sides" reading. Files only, null for a directory, mirroring `remote_mtime`'s own convention
  // (core/reconcile.py -- see that module for why the local side deliberately stays consistent
  // rather than inventing a directory rule of its own).
  local_mtime: number | null
  // When `state` last actually changed value (migration 006), stamped by that migration's own
  // triggers. null only for a row the migration's backfill genuinely couldn't date -- render
  // gracefully rather than assuming a value. Not the same question as "when did it complete"
  // (downloaded_at, the planned local-retention feature's key) -- a DOWNLOADED item that dips
  // to PARTIAL and back moves this without earning a fresh retention lease.
  state_changed_at: string | null
  // When this row was first ever seen (migration 001) -- the first entry in the item drawer's
  // lifecycle chronology (2026-08-13). Existed server-side since phase 2; new to the wire only.
  first_seen_at: string | null
  // The settle gate's countdown (2026-08-13, prompts/2026-08-13-files-ux-pass.md item 3):
  // item_settle.matched_scans/updated_at (core/settle.py.SettleRecord), joined in only for
  // top-level rows and only while substate === 'settling' -- null the rest of the time,
  // including for a non-top-level row (item_settle has no row for one at all) or before this
  // item's first scan. See core/itemview.py.item_view's own docstring for why this is gated
  // on substate rather than passed through whenever the join happens to have a row (an
  // ungated read would make this climb forever on a row nothing else about is changing,
  // which would defeat the WebSocket delta's "only publish what changed" property).
  settle_matched_scans: number | null
  settle_first_matched_at: string | null
  // The settle gate's *other* display state (2026-08-13,
  // prompts/2026-08-13-settle-progress-visibility.md, migration 013): a top-level item that
  // hasn't been confirmed unchanged even once yet (settle_matched_scans === 1 -- a first-ever
  // sighting, or the fingerprint changed on the most recent scan and reset the count; see
  // lib/format.ts.isStillArriving) has nothing useful to say via the countdown above.
  // settle_total_bytes (item_settle.total_bytes, already computed as part of the fingerprint)
  // is what a "still arriving" reading watches climb; settle_first_observed_at/
  // settle_last_changed_at answer "how long have we watched this" / "when did it last move."
  // Gated on substate === 'settling' exactly like the two fields above, and null for the same
  // reasons those two are, plus one more: a pre-migration item_settle row that hasn't changed
  // since carries null for these two timestamps specifically (core/settle.py.SettleRecord) --
  // render that as "unknown," never a fabricated time.
  settle_total_bytes: number | null
  settle_first_observed_at: string | null
  settle_last_changed_at: string | null
  // Milestone/audit timestamps (2026-08-13) -- raw material for a lifecycle icon's tooltip.
  // `downloaded_at` already existed server-side (§7.3's retention key); the other four are new
  // to the wire only.
  downloaded_at: string | null
  verified_at: string | null
  extracted_at: string | null
  first_missing_at: string | null
  remote_deleted_at: string | null
  // "Folder prefix during transfer" (core/download_prefix.py): the exact prefix string this
  // item's local root is *currently* written under, null when nothing is in flight under a
  // prefixed name. Never part of rel_path -- purely the item drawer's "where does this
  // actually live right now" answer.
  pending_download_prefix: string | null
  // `deleted_archive.deleted_at` (2026-08-14, prompts/2026-08-14-extracted-archives-rest-as-
  // extracted.md), joined the same optional way as the settle fields above. null unless this
  // rel_path is a spent archive volume `core/local_delete.py.delete_extracted_archives` removed
  // after a successful extraction -- the row's own `state` already reads `EXCLUDED` for this
  // reason (never through the removal-grace clock). `lib/format.ts.isDeletedArchiveVolume` is
  // the one place that turns this into the chip substitution.
  deleted_archive_at: string | null
  // Sonarr/Radarr integration (migration 018, docs/arr-integration-spec.md): the Files page's
  // *arr icon reads `arr_status` directly. A facet, not a lifecycle state -- passed through
  // verbatim from `item.arr_status`/`item.arr_status_at` (`core/itemview.py.item_view`), null
  // for every item on a queue with no bound instance (or one the poller hasn't matched yet).
  // `null | 'detected' | 'notified' | 'imported' | 'cleaned' | 'gone'`, kept as `string | null`
  // here (not a literal union) since this type mirrors the wire shape and the backend's own
  // `item.arr_status` column is a plain `TEXT`, not a `CHECK`-constrained enum -- see
  // `lib/fileTree.ts.arrIconVariant` for the one place that switches on the known values and
  // degrades an unrecognized one to the neutral icon rather than rendering nothing.
  arr_status: string | null
  arr_status_at: string | null
  facets: LifecycleFacets
}

// `POST /api/items/{item_id}/delete`'s optional body (2026-08-16, the delete dialog's
// independent Local/Source scopes, prompts/2026-08-16-manual-delete-local-and-remote.md).
// Omitted entirely means exactly the pre-existing behavior (`local=True, source=False`);
// `client.ts.deleteItem` always sends both explicitly instead, so every call site says what it
// means rather than relying on the backend's own default.
export interface DeleteItemRequest {
  local: boolean
  source: boolean
}

// `POST /api/items/{item_id}/delete` (prompts/open-issues.md "7 + 8" -- the first delete
// endpoint in this API). A request that accomplishes *nothing at all* is a non-2xx response
// (client.ts's `sendJson` throws) -- this shape describes every request that succeeded at
// least partially. `deleted`/`reason`/`bytes_freed` describe the **local** scope, unchanged
// from before the Source scope existed; `source_deleted`/`source_reason` are `null` when
// `source` was not requested, and otherwise describe that independent outcome -- see
// `api/jobs.py.DeleteItemResponse`'s own docstring for why a combined request can report
// `deleted: true` alongside `source_deleted: false` rather than throwing.
export interface DeleteItemResponse {
  deleted: boolean
  reason: string
  bytes_freed: number | null
  source_deleted: boolean | null
  source_reason: string | null
}

// --- Reset item tracking (2026-08-13, prompts/2026-08-13-reset-item-tracking.md) -----------
//
// Distinct from Delete (above, removes bytes) and from Clear History (a few pixels away on
// the History page, which removes job/event rows and never touches an item at all) -- this
// forgets an item's tracking outright, so a suppressed or failed path can be reused.

export interface ResetItemResponse {
  reset: boolean
  reason: string
  affected_rel_paths: string[]
}

export interface QueueResetRequest {
  /** Must equal the queue's own `queue_name` exactly -- the whole-queue scope's typed
   * confirmation, checked again server-side as defense in depth. */
  confirm_name: string
}

export interface ResetSummaryResponse {
  reset_top_level: number
  withheld: { rel_path: string; reason: string }[]
  affected_count: number
}

export interface ResetPatternPreviewRequest {
  pattern: string
}

export interface ResetPatternPreviewItem {
  rel_path: string
  is_dir: boolean
  remote_size: number | null
  local_size: number | null
}

export interface ResetPatternPreviewResponse {
  items: ResetPatternPreviewItem[]
}

export interface QueueFiles {
  queue_id: number
  queue_name: string
  scanned_at: string | null
  error: string | null
  // A *soft* note (DESIGN.md §5) -- set when the last scan skipped one or more unreadable
  // remote subtrees (core/remote.py's scan-abort fix, phase 3b) rather than failing
  // outright. Distinct from `error`, which means the whole scan failed and the tree shown
  // is stale.
  warning: string | null
  // DESIGN.md §7.3's mount sentinel, required starting phase 4. `null`/absent before this
  // queue has ever scanned or on the WebSocket's own queue shape (which doesn't carry this
  // field -- see hooks/useLiveModel.ts); `false` means auto-queue is currently gated off for
  // it regardless of its own toggle. Optional so the REST (`GET /api/files`) and WS-derived
  // shapes can share this one interface without the WS side fabricating a value.
  mount_ok?: boolean | null
  nodes: FileNode[]
}

export interface FilesResponse {
  queues: QueueFiles[]
}

// --- Jobs / transfer engine (phase 3a API, phase 3b UI -- DESIGN.md §4, §9.2 Transfers) ---

export type JobKind = 'mirror' | 'pget'
export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type Lane = 'main' | 'small'

export interface JobOut {
  id: number
  item_id: number
  queue_id: number
  queue_name: string
  rel_path: string
  is_dir: boolean
  kind: JobKind
  state: JobState
  lane: Lane
  rank: number
  attempt: number
  queued_at: string
  started_at: string | null
  finished_at: string | null
  pid: number | null
  // The allocation this job was admitted with (DESIGN.md §4.5/§9.1) -- fixed for its
  // lifetime, distinct from `speed_bps` (what it's *actually* pulling right now). Under
  // admission control a job can hold its full allocation while pulling far less of it.
  rate_limit_bps: number | null
  // "Start now" as a menu, not a single button (2026-08-19,
  // prompts/done/2026-08-19-start-now-bandwidth-fractions.md) -- widened from a plain
  // `forced_full_rate: boolean`. `null` means never force-started; `1.0` means Max
  // (byte-identical to the old `true`); `0.1`/`0.25`/`0.5`/`0.75` are the new menu options.
  forced_rate_fraction: number | null
  bytes_start: number
  bytes_done: number
  bytes_total: number | null
  speed_bps: number | null
  eta_s: number | null
  exit_code: number | null
  error_class: string | null
  output_tail: string | null
  // 2026-08-15 (prompts/2026-08-15-transfers-single-line-rows-with-detail.md): the item-level
  // facts the Transfers row's expand panel needs -- see `api/jobs.py._job_out`/`core/queue.py.
  // list_jobs`'s own comments for the join these ride on. Mirrors `FileNode.verified_at`/
  // `extracted_at`/`remote_deleted_at`/`arr_status`/`arr_status_at` (`lib/fileTree.ts`) exactly,
  // plus `arr_instance_name` -- resolved server-side here (unlike the Files page, which resolves
  // it client-side from `GET /api/settings/arr` -- see `lib/fileTree.ts`'s own comment on why
  // that page does it differently) since `JobOut`'s row set is already bounded.
  verified_at: string | null
  extracted_at: string | null
  remote_deleted_at: string | null
  arr_status: string | null
  arr_status_at: string | null
  // `null` whenever this job's queue has no bound *arr instance -- the signal
  // `lib/transferPanel.ts.hasArrGroup` gates the panel's *arr group on.
  arr_instance_name: string | null
  // The bound instance's `kind` ('sonarr' | 'radarr', migration 018's CHECK constraint) --
  // added 2026-08-16 (prompts/2026-08-16-arr-chip-on-row-lines.md) for the row chip's
  // brand-logo choice (`components/LifecycleIcons.tsx.ArrRowChip`). `arr_instance_name` is
  // free text the user can rename to anything, so it can't drive which logo to draw; `kind` is
  // the one field that reliably says which. `null` under the same condition
  // `arr_instance_name` is null. Kept as `string | null` (not a literal union), same reasoning
  // `FileNode.arr_status`'s own comment gives -- an unrecognized value degrades to a text chip
  // rather than being rejected at the type level.
  arr_instance_kind: string | null
}

export interface JobsResponse {
  jobs: JobOut[]
}

/** `POST /api/jobs/dismiss-all` (2026-08-15) -- the bulk counterpart to `dismissJob`. */
export interface DismissAllResponse {
  dismissed: number
}

/** `GET /api/items/{id}/events` (2026-08-15) -- the Transfers panel's on-demand "processing
 * story" fetch, one `event` row per entry, newest first, server-capped.
 */
export interface ItemEventOut {
  id: number
  ts: string
  level: string
  kind: string
  message: string
  job_id: number | null
}

export interface ItemEventsResponse {
  events: ItemEventOut[]
}

export interface QueueItemRequest {
  item_id: number
  start_now: boolean
}

// --- Settings -> Transfer (phase 3a API, DESIGN.md §4.5/§9.2/§9.3) -----------------------
//
// Mirrors `core/queue.py.TransferSettings` exactly -- twelve fields, one site-wide set
// (DESIGN.md §4.5: "a queue governs what and where, never how fast"). Bandwidth/size fields
// are `_bps`/`_bytes` on the wire; TransferTab.tsx converts to/from MB(/s) at the edge, this
// type stays in the backend's native units so a round-trip through the API never drifts.
export interface TransferSettingsOut {
  max_bandwidth_bps: number
  max_concurrent_transfers: number
  small_item_threshold_bytes: number
  small_lane_concurrency: number
  // null = derived (10% of the ceiling, min 1 MB/s, capped at half the ceiling -- see
  // `effective_small_lane_reserve_bps()`'s docstring in core/queue.py). TransferTab.tsx must
  // compute and show that effective value itself; the server doesn't send it separately.
  small_lane_reserve_bps: number | null
  min_share_floor_bps: number
  mirror_parallel_transfer_count: number
  mirror_use_pget_n: number
  pget_default_n: number
  max_attempts: number
  retry_backoff_base_s: number
  extra_lftp_settings: string
}

export type TransferSettingsIn = TransferSettingsOut

// --- Settings -> Transfer's "effective lftp settings" readout (2026-08-14,
// prompts/2026-08-14-show-effective-lftp-settings.md) -------------------------------------
//
// Read-only and credential-free by construction -- see `core/lftp.py.effective_tuning_settings`
// and `api/jobs.py.get_effective_lftp_settings`'s own module comment for why. Re-exported from
// `lib/effectiveLftpSettings.ts` rather than duplicated -- that module's collision-detection
// pure functions are typed against these same shapes.
export type { EffectiveLftpJobKind, EffectiveLftpSetting } from '../lib/effectiveLftpSettings'

export interface EffectiveLftpSettingsOut {
  kinds: import('../lib/effectiveLftpSettings').EffectiveLftpJobKind[]
  bandwidth_note: string
}

// --- History (phase 6, DESIGN.md §9.2 History page) ---------------------------------------

/** Deliberately not `JobOut` -- that shape carries `output_tail` inline because the
 * Transfers page's row set is bounded by construction. History has no such bound (a busy
 * install accumulates thousands of terminal jobs), so `output_tail` (~4KB/row) is fetched
 * on demand via `getHistoryJobOutput` instead of shipped in every list row -- see
 * `has_output_tail`.
 */
export interface HistoryJobOut {
  id: number
  item_id: number
  queue_id: number
  queue_name: string
  rel_path: string
  is_dir: boolean
  kind: JobKind
  state: 'succeeded' | 'failed' | 'cancelled'
  attempt: number
  queued_at: string
  started_at: string | null
  finished_at: string | null
  bytes_total: number | null
  bytes_done: number
  exit_code: number | null
  error_class: string | null
  has_output_tail: boolean
  // Migration 016 (2026-08-13) -- when this job was dismissed from the Transfers page, or
  // `null` if it never was. History shows every terminal job either way (dismissal only ever
  // hides a Transfers row); this just answers "did I dismiss this."
  dismissed_at: string | null
  // The same *arr facts `JobOut` carries (2026-08-16, prompts/2026-08-16-arr-chip-on-row-lines.md)
  // -- `item.arr_status`/`item.arr_status_at` plus the bound instance's `name`/`kind`, joined by
  // `api/history.py.list_history_jobs` the identical way `core/queue.py.list_jobs()` does, so
  // `HistoryJobsSection.tsx` can render the same `ArrRowChip`. `null` whenever this job's queue
  // has no bound *arr instance.
  arr_status: string | null
  arr_status_at: string | null
  arr_instance_name: string | null
  arr_instance_kind: string | null
}

/** One queue's honest aggregate over the whole filtered set, not just the loaded page
 * (2026-08-16, prompts/2026-08-16-history-jobs-group-collapse.md) -- `HistoryJobsResponse.jobs`
 * is one `LIMIT`/`OFFSET` page, so a client-side sum over it would be wrong whenever more rows
 * match the filter than are loaded. `backend/lftpweb/api/history.py._queue_summaries` computes
 * this with a bounded `GROUP BY` against the exact same filter as the `jobs` list beside it.
 * History's job domain is terminal-only, so unlike the Transfers page's `QueueGroupCounts`
 * (`lib/transferPanel.ts`) there is no `active`/`queued` bucket here.
 */
export interface HistoryQueueSummaryOut {
  queue_id: number
  queue_name: string
  succeeded: number
  failed: number
  cancelled: number
  total_bytes_done: number
}

export interface HistoryJobsResponse {
  jobs: HistoryJobOut[]
  total: number
  limit: number
  offset: number
  queue_summaries: HistoryQueueSummaryOut[]
}

export interface HistoryJobOutputOut {
  job_id: number
  error_class: string | null
  output_tail: string | null
}

export interface HistoryEventOut {
  id: number
  ts: string
  level: 'debug' | 'info' | 'warning' | 'error'
  kind: string
  message: string
  item_id: number | null
  job_id: number | null
  queue_id: number | null
  queue_name: string | null
  rel_path: string | null
}

export interface HistoryEventsResponse {
  events: HistoryEventOut[]
  total: number
  limit: number
  offset: number
}

export interface HistoryJobsFilter {
  // One item's own transfer history (2026-08-13, prompts/2026-08-13-files-detail-inspector.md)
  // -- the item drawer's bounded "load on open" fetch. Mirrors `HistoryEventsFilter.item_id`.
  item_id?: number
  queue_id?: number
  state?: 'succeeded' | 'failed' | 'cancelled'
  error_class?: string
  since?: string
  until?: string
  limit?: number
  offset?: number
}

export interface HistoryEventsFilter {
  kind?: string
  level?: 'debug' | 'info' | 'warning' | 'error'
  item_id?: number
  queue_id?: number
  since?: string
  until?: string
  limit?: number
  offset?: number
}

/** The response shape for every `DELETE` under `/api/history/*` (2026-08-13,
 * prompts/2026-08-13-clear-history.md) -- one row, a filtered batch, or everything. `deleted`
 * is the actual row count the server removed, not the pre-delete `total` the confirmation
 * prompt showed -- the two can differ if something else changed the rows in between.
 */
export interface HistoryClearResponse {
  deleted: number
}

// --- Settings -> Backup (phase 7, DESIGN.md §10.2) --------------------------------------

export interface BackupSettingsOut {
  interval_days: number
  keep_count: number
}

export type BackupSettingsIn = BackupSettingsOut

export interface BackupInfoOut {
  filename: string
  size_bytes: number
  created_at: string
}

export interface BackupListResponse {
  backups: BackupInfoOut[]
}

// --- Metrics / Dashboard (this task -- DESIGN.md new section proposed, docs/decisions.md) --

export interface MetricsSettingsOut {
  retention_days: number
}

export type MetricsSettingsIn = MetricsSettingsOut

// 2026-08-17 (prompts/done/2026-08-17-bytes-chart-7d-30d-ranges-and-total.md): the two Dashboard
// charts' range selectors are independent and no longer offer the same option list -- the
// speed chart (Chart 2) keeps its original 1h/12h/24h window (fine-grained speed over a month
// would average away exactly the spikes it exists to show); the bytes chart (Chart 1, see
// api/metrics.py's `_RANGES` comment) gained 7d/30d instead of 12h, since a speed-chart-style
// short window says little about total bytes moved. `MetricsRange` stays the
// union both narrower types feed into `MetricsThroughputResponse.range`/`getThroughput`, which
// don't otherwise care which selector a given range came from.
export type SpeedRange = '1h' | '12h' | '24h'
export type BytesRange = '24h' | '7d' | '30d'
export type MetricsRange = SpeedRange | BytesRange

export interface MetricsBucketOut {
  ts: string
  // `false` = no heartbeat fell in this bucket at all -- lftpweb wasn't running. Render as a
  // gap, never a zero (docs/decisions.md's idle-vs-down decision).
  up: boolean
  total_bytes: number | null
  // JSON object keys are always strings on the wire -- queue_id -> bytes moved this bucket.
  by_queue: Record<string, number>
}

export interface MetricsThroughputResponse {
  range: MetricsRange
  bucket_seconds: number
  buckets: MetricsBucketOut[]
}

// --- Settings -> Logs (phase 7, DESIGN.md §10.1) -----------------------------------------

export interface LogFileOut {
  name: string
  size_bytes: number
  modified_at: string
  is_current: boolean
}

export interface LogFilesResponse {
  files: LogFileOut[]
}

export interface LogTailResponse {
  lines: string[]
  // True when the bounded read hit its byte cap before satisfying `lines` -- a level filter
  // may be under-showing what's actually in the file (core/logtail.py never re-scans further
  // back just to satisfy a filter). See core/logtail.py's module docstring.
  truncated: boolean
}

export type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'

// --- Support bundle (Settings -> Logs, 2026-08-17) --------------------------------------
//
// `POST /api/support-bundle` -- mirrors `backend/lftpweb/models.py`'s `SupportBundleRequest`.
// lftpweb's own logs are always included server-side regardless of what's sent here (the
// dialog shows that checkbox checked and disabled), so there is no field for them.

export interface SupportBundleRequest {
  include_environment: boolean
  include_settings: boolean
  include_events: boolean
  include_jobs: boolean
  arr_instance_ids: number[]
}

// --- Auth (phase 8, DESIGN.md §8) -------------------------------------------------------

export type AuthMode = 'none' | 'password' | 'proxy'

export interface AuthSettingsOut {
  mode: AuthMode
  proxy_header: string
  proxy_trusted_cidrs: string[]
  has_user: boolean
  username: string | null
}

// Mirrors AuthSettingsIn — `username`/`new_password` are only consulted when `mode ===
// 'password'`, and are required together the first time a user is created (the backend
// refuses to store `mode: 'password'` with nobody able to log in — DESIGN.md §8).
export interface AuthSettingsIn {
  mode: AuthMode
  proxy_header: string
  proxy_trusted_cidrs: string[]
  username?: string | null
  new_password?: string | null
}

export interface ChangePasswordIn {
  current_password: string
  new_password: string
}

export interface LoginIn {
  username: string
  password: string
}

/** GET /api/auth/session — "whoami," always reachable unauthenticated so the SPA can decide
 * whether to render the login form at all (see `hooks/useAuth.tsx`).
 */
export interface AuthSessionOut {
  mode: AuthMode
  authenticated: boolean
  username: string | null
  // Present only when authenticated via a password-mode session — attached as
  // `X-CSRF-Token` on every mutating request afterwards.
  csrf_token: string | null
}

export interface ApiKeyOut {
  id: number
  name: string
  created_at: string
  last_used_at: string | null
}

export interface ApiKeyIn {
  name: string
}

// Plaintext `key` — present only in the create response, shown once, never again.
export interface ApiKeyCreatedOut extends ApiKeyOut {
  key: string
}
