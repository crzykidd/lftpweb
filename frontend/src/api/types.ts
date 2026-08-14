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
}

export interface PathQueueOut extends PathQueueIn {
  id: number
  host_id: number
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
  facets: LifecycleFacets
}

// `POST /api/items/{item_id}/delete` (prompts/open-issues.md "7 + 8" -- the first delete
// endpoint in this API). A withheld guard is a non-2xx response (client.ts's `sendJson`
// throws), not `deleted: false` -- this shape only ever describes a successful delete.
export interface DeleteItemResponse {
  deleted: boolean
  reason: string
  bytes_freed: number | null
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
  forced_full_rate: boolean
  bytes_start: number
  bytes_done: number
  bytes_total: number | null
  speed_bps: number | null
  eta_s: number | null
  exit_code: number | null
  error_class: string | null
  output_tail: string | null
}

export interface JobsResponse {
  jobs: JobOut[]
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
}

export interface HistoryJobsResponse {
  jobs: HistoryJobOut[]
  total: number
  limit: number
  offset: number
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

export type MetricsRange = '1h' | '12h' | '24h'

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
