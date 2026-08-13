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
  known_hosts_policy: KnownHostsPolicy
  credentials_need_reentry: boolean
  // Read-only (DESIGN.md §4.5/§9.3, docs/decisions.md 2026-08-12): whatever currently sits
  // in `net:connection-limit` inside the host's `connection_overrides` JSON blob, or null if
  // unset. There is no field on `HostIn` to set it -- Settings → Connection has no UI for
  // it -- so this is `null` on every install that hasn't hand-edited the database.
  net_connection_limit: number | null
}

// Mirrors HostIn — password is plaintext here only, and only ever sent, never received back
// (§9.2: the password field must never round-trip the stored secret to the browser).
export interface HostIn {
  name: string
  address: string
  port: number
  username: string
  auth_method: AuthMethod
  key_path: string | null
  password: string | null
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
  // Phase 5 (DESIGN.md §6). All three default off. The backend forces auto_verify to true
  // whenever sync_mode is 'move' regardless of what's sent here -- the UI mirrors that by
  // disabling (not hiding) the checkbox rather than relying on the server alone.
  auto_verify: boolean
  auto_extract: boolean
  auto_move: boolean
  // Migration 009 (prompts/done/2026-08-12-per-queue-scan-interval.md). `null` -- the default,
  // and what an existing queue already has -- means "use the site-wide default (30s)"; `0`
  // means on-demand only (no timer; "Rescan now" and auto-queue-driving passes still work when
  // something else forces a scan); any positive number is a literal per-queue interval in
  // seconds. The backend rejects a negative value with a 400.
  scan_interval_s: number | null
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

export interface FileNode {
  id: number | null
  rel_path: string
  is_dir: boolean
  state: string
  // The settle gate (prompts/open-issues.md #2): 'settling' for a top-level REMOTE_ONLY item
  // whose remote fingerprint hasn't held still for 2 consecutive scans yet, null otherwise.
  substate: string | null
  remote_size: number | null
  local_size: number | null
  remote_mtime: number | null
  // When `state` last actually changed value (migration 006), stamped by that migration's own
  // triggers. null only for a row the migration's backfill genuinely couldn't date -- render
  // gracefully rather than assuming a value. Not the same question as "when did it complete"
  // (downloaded_at, the planned local-retention feature's key) -- a DOWNLOADED item that dips
  // to PARTIAL and back moves this without earning a fresh retention lease.
  state_changed_at: string | null
}

// `POST /api/items/{item_id}/delete` (prompts/open-issues.md "7 + 8" -- the first delete
// endpoint in this API). A withheld guard is a non-2xx response (client.ts's `sendJson`
// throws), not `deleted: false` -- this shape only ever describes a successful delete.
export interface DeleteItemResponse {
  deleted: boolean
  reason: string
  bytes_freed: number | null
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
