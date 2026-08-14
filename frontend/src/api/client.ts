import type {
  ApiKeyCreatedOut,
  ApiKeyIn,
  ApiKeyOut,
  AuthSessionOut,
  AuthSettingsIn,
  AuthSettingsOut,
  AutoQueueSettingsIn,
  AutoQueueSettingsOut,
  ChangePasswordIn,
  BackupInfoOut,
  BackupListResponse,
  BackupSettingsIn,
  BackupSettingsOut,
  DeleteItemResponse,
  FilesResponse,
  HealthResponse,
  HistoryClearResponse,
  HistoryEventsFilter,
  HistoryEventsResponse,
  HistoryJobOutputOut,
  HistoryJobsFilter,
  HistoryJobsResponse,
  HostIn,
  HostOut,
  HostTestRequest,
  JobOut,
  JobsResponse,
  LoginIn,
  LogFilesResponse,
  LogTailResponse,
  MetricsRange,
  MetricsSettingsIn,
  MetricsSettingsOut,
  MetricsThroughputResponse,
  PathQueueIn,
  PathQueueOut,
  PatternIn,
  PatternOut,
  PatternPreviewRequest,
  PatternPreviewResponse,
  PostprocessSettingsIn,
  PostprocessSettingsOut,
  QueueAutoQueueStatus,
  QueueResetRequest,
  ResetItemResponse,
  ResetPatternPreviewRequest,
  ResetPatternPreviewResponse,
  ResetSummaryResponse,
  SettleSettingsIn,
  SettleSettingsOut,
  StatsResponse,
  TestConnectionResponse,
  TransferSettingsIn,
  TransferSettingsOut,
} from './types'

// The CSRF token issued at login (DESIGN.md §8) — held in memory only, never localStorage
// (nothing durable needs to survive a page reload; `hooks/useAuth.tsx` re-fetches it from
// `GET /api/auth/session` on mount instead). `setCsrfToken` is called by that hook whenever
// a login/session response carries one.
let csrfToken: string | null = null

export function setCsrfToken(token: string | null): void {
  csrfToken = token
}

const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) {
    throw new Error(`${path} responded ${res.status}`)
  }
  return (await res.json()) as T
}

async function sendJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  // Attached whenever we have one, regardless of mode — a no-op in `none`/`proxy` mode (the
  // backend only checks it for a password-mode session, middleware.py) and required for
  // every mutating call once a password-mode session exists.
  if (csrfToken && MUTATING_METHODS.has(method)) headers['X-CSRF-Token'] = csrfToken

  const res = await fetch(path, {
    method,
    headers: Object.keys(headers).length ? headers : undefined,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`${method} ${path} responded ${res.status}${detail ? `: ${detail}` : ''}`)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export function getHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>('/api/health')
}

export function getStats(): Promise<StatsResponse> {
  return getJson<StatsResponse>('/api/stats')
}

// --- Settings -> Connection -------------------------------------------------------------

export function getHost(): Promise<HostOut | null> {
  return getJson<HostOut | null>('/api/settings/host')
}

export function putHost(body: HostIn): Promise<HostOut> {
  return sendJson<HostOut>('/api/settings/host', 'PUT', body)
}

export function testHost(body?: HostTestRequest): Promise<TestConnectionResponse> {
  return sendJson<TestConnectionResponse>('/api/settings/host/test', 'POST', body ?? {})
}

// --- Settings -> Queues ------------------------------------------------------------------

export function listQueues(): Promise<PathQueueOut[]> {
  return getJson<PathQueueOut[]>('/api/settings/queues')
}

export function createQueue(body: PathQueueIn): Promise<PathQueueOut> {
  return sendJson<PathQueueOut>('/api/settings/queues', 'POST', body)
}

export function updateQueue(id: number, body: PathQueueIn): Promise<PathQueueOut> {
  return sendJson<PathQueueOut>(`/api/settings/queues/${id}`, 'PUT', body)
}

export function deleteQueue(id: number): Promise<void> {
  return sendJson<void>(`/api/settings/queues/${id}`, 'DELETE')
}

export function getAutoQueueStatus(queueId: number): Promise<QueueAutoQueueStatus> {
  return getJson<QueueAutoQueueStatus>(`/api/settings/queues/${queueId}/autoqueue-status`)
}

// --- Settings -> Queues -> Patterns (phase 4, DESIGN.md §3.1 `pattern`, §4.7) -----------

export function listPatterns(queueId?: number): Promise<PatternOut[]> {
  const qs = queueId != null ? `?queue_id=${queueId}` : ''
  return getJson<PatternOut[]>(`/api/settings/patterns${qs}`)
}

export function createPattern(body: PatternIn): Promise<PatternOut> {
  return sendJson<PatternOut>('/api/settings/patterns', 'POST', body)
}

export function updatePattern(id: number, body: PatternIn): Promise<PatternOut> {
  return sendJson<PatternOut>(`/api/settings/patterns/${id}`, 'PUT', body)
}

export function deletePattern(id: number): Promise<void> {
  return sendJson<void>(`/api/settings/patterns/${id}`, 'DELETE')
}

/** The live "what would this match" preview (DESIGN.md §4.7, §9.2) -- evaluates an *unsaved*
 * pattern set against the queue's current remote tree.
 */
export function previewPatterns(
  queueId: number,
  body: PatternPreviewRequest,
): Promise<PatternPreviewResponse> {
  return sendJson<PatternPreviewResponse>(
    `/api/settings/queues/${queueId}/pattern-preview`,
    'POST',
    body,
  )
}

// --- Settings -> Post-processing (phase 5, DESIGN.md §6) --------------------------------

export function getPostprocessSettings(): Promise<PostprocessSettingsOut> {
  return getJson<PostprocessSettingsOut>('/api/settings/postprocess')
}

export function putPostprocessSettings(
  body: PostprocessSettingsIn,
): Promise<PostprocessSettingsOut> {
  return sendJson<PostprocessSettingsOut>('/api/settings/postprocess', 'PUT', body)
}

// --- Settings -> the settle gate (prompts/open-issues.md #2, `core/settle.py`) ---------

export function getSettleSettings(): Promise<SettleSettingsOut> {
  return getJson<SettleSettingsOut>('/api/settings/settle')
}

export function putSettleSettings(body: SettleSettingsIn): Promise<SettleSettingsOut> {
  return sendJson<SettleSettingsOut>('/api/settings/settle', 'PUT', body)
}

// --- Settings -> auto-queue (`core/autoqueue.py.AutoQueueSettings`) ---------------------

export function getAutoQueueSettings(): Promise<AutoQueueSettingsOut> {
  return getJson<AutoQueueSettingsOut>('/api/settings/autoqueue')
}

export function putAutoQueueSettings(body: AutoQueueSettingsIn): Promise<AutoQueueSettingsOut> {
  return sendJson<AutoQueueSettingsOut>('/api/settings/autoqueue', 'PUT', body)
}

// --- Files ---------------------------------------------------------------------------------

export function getFiles(): Promise<FilesResponse> {
  return getJson<FilesResponse>('/api/files')
}

export function rescanFiles(): Promise<{ triggered: boolean }> {
  return sendJson<{ triggered: boolean }>('/api/files/rescan', 'POST')
}

// --- Jobs / transfer engine (DESIGN.md §4, §9.2 Transfers) -------------------------------

export function getJobs(): Promise<JobsResponse> {
  return getJson<JobsResponse>('/api/jobs')
}

/** Manual queue (§4.7): always wins over auto-queue suppression. `startNow` requests the
 * "start now at max bandwidth" admission path (§4.5) at the moment of queueing.
 */
export function queueItem(itemId: number, startNow = false): Promise<JobOut> {
  return sendJson<JobOut>('/api/jobs', 'POST', { item_id: itemId, start_now: startNow })
}

export function stopJob(jobId: number): Promise<void> {
  return sendJson<void>(`/api/jobs/${jobId}/stop`, 'POST')
}

export function moveJobToTop(jobId: number): Promise<void> {
  return sendJson<void>(`/api/jobs/${jobId}/move-to-top`, 'POST')
}

/** Dismiss a terminal (`failed`/`cancelled`) job from the Transfers page (2026-08-13,
 * prompts/done/2026-08-13-dismiss-terminal-jobs.md) -- the row's own record stays in History;
 * this only stops it showing here. Rejects (non-2xx, `sendJson` throws) for a `queued`/
 * `running` job -- see `core/queue.py.dismiss_job`.
 */
export function dismissJob(jobId: number): Promise<void> {
  return sendJson<void>(`/api/jobs/${jobId}/dismiss`, 'POST')
}

export function startJobNow(jobId: number): Promise<{ applied: boolean }> {
  return sendJson<{ applied: boolean }>(`/api/jobs/${jobId}/start-now`, 'POST')
}

export function retryItem(itemId: number): Promise<JobOut> {
  return sendJson<JobOut>(`/api/items/${itemId}/retry`, 'POST')
}

/** Stop-by-item (DESIGN.md §9.2's Files-page Stop action) -- the Files page only knows the
 * item, never the job id an item may currently be running under.
 */
export function stopItem(itemId: number): Promise<{ applied: boolean }> {
  return sendJson<{ applied: boolean }>(`/api/items/${itemId}/stop`, 'POST')
}

/** Delete-by-item (DESIGN.md §9.2's Files-page "Delete local"; prompts/open-issues.md
 * "7 + 8"). A withheld guard responds non-2xx, so `sendJson` throws -- this rejects exactly
 * the way `queueItem`/`stopItem` already do on failure, which is what lets `FileTree.tsx`'s
 * existing `Promise.allSettled` bulk-action reporting cover Delete with no new mechanism.
 */
export function deleteItem(itemId: number): Promise<DeleteItemResponse> {
  return sendJson<DeleteItemResponse>(`/api/items/${itemId}/delete`, 'POST')
}

// --- Reset item tracking (2026-08-13, prompts/2026-08-13-reset-item-tracking.md) -----------
//
// A different, more dangerous action than Delete above: this forgets an item's row (and its
// item_settle/deleted_archive bookkeeping) outright rather than removing bytes, so a
// suppressed or failed path can be reused. Also unrelated to Clear History (api/history.py) --
// see api/types.ts's own comment on why the two must never be confused.

/** Selected-item(s) scope -- one row, or a bulk selection resolved to one call per item
 * (`Promise.allSettled`, the identical shape `FileTree.tsx` already uses for bulk Delete).
 * A withheld guard responds non-2xx, so `sendJson` throws exactly like `deleteItem`.
 */
export function resetItem(itemId: number): Promise<ResetItemResponse> {
  return sendJson<ResetItemResponse>(`/api/items/${itemId}/reset`, 'POST')
}

/** Whole-queue scope -- the clean-slate case, and the most destructive action in the app.
 * `confirm_name` must equal the queue's own name exactly; the server checks this too (defense
 * in depth), so a mismatch is a 400. Never all-or-nothing: an item mid-transfer is withheld
 * (named in the response's `withheld`) while the rest of the queue still resets.
 */
export function resetQueue(
  queueId: number,
  body: QueueResetRequest,
): Promise<ResetSummaryResponse> {
  return sendJson<ResetSummaryResponse>(`/api/queues/${queueId}/reset-all`, 'POST', body)
}

/** The purge-by-pattern scope's own safety mechanism -- every top-level item `body.pattern`
 * would reset, single-queue only, with enough per-item data to compute the same real-numbers
 * warning the other two scopes show. Never resets anything itself.
 */
export function previewResetByPattern(
  queueId: number,
  body: ResetPatternPreviewRequest,
): Promise<ResetPatternPreviewResponse> {
  return sendJson<ResetPatternPreviewResponse>(
    `/api/queues/${queueId}/reset-preview`,
    'POST',
    body,
  )
}

/** Executes the purge-by-pattern scope reviewed via `previewResetByPattern` above -- same
 * pattern, same single-queue scope, same evaluator server-side.
 */
export function resetByPattern(
  queueId: number,
  body: ResetPatternPreviewRequest,
): Promise<ResetSummaryResponse> {
  return sendJson<ResetSummaryResponse>(`/api/queues/${queueId}/reset-by-pattern`, 'POST', body)
}

// --- Settings -> Transfer (phase 3a API, phase-9-follow-up UI -- DESIGN.md §4.5/§9.3) -----

export function getTransferSettings(): Promise<TransferSettingsOut> {
  return getJson<TransferSettingsOut>('/api/settings/transfer')
}

export function putTransferSettings(body: TransferSettingsIn): Promise<TransferSettingsOut> {
  return sendJson<TransferSettingsOut>('/api/settings/transfer', 'PUT', body)
}

// --- History (phase 6, DESIGN.md §9.2 History page) ---------------------------------------

function queryString(params: object): string {
  const usp = new URLSearchParams()
  for (const [key, value] of Object.entries(params) as [string, string | number | undefined][]) {
    if (value !== undefined) usp.set(key, String(value))
  }
  const qs = usp.toString()
  return qs ? `?${qs}` : ''
}

/** Completed/failed/cancelled jobs (DESIGN.md §9.2) -- this is where a `succeeded` job's own
 * record lives; the Transfers page (`getJobs`) deliberately never shows it. Server-capped
 * and paginated (`total`/`limit`/`offset`) -- never assume `jobs.length` is everything.
 */
export function getHistoryJobs(filter: HistoryJobsFilter = {}): Promise<HistoryJobsResponse> {
  return getJson<HistoryJobsResponse>(`/api/history/jobs${queryString(filter)}`)
}

/** The on-demand fetch for a job's captured lftp output (~4KB, DESIGN.md §9.2) -- never
 * shipped inline in `getHistoryJobs`'s list payload; see `HistoryJobOut.has_output_tail`.
 */
export function getHistoryJobOutput(jobId: number): Promise<HistoryJobOutputOut> {
  return getJson<HistoryJobOutputOut>(`/api/history/jobs/${jobId}/output`)
}

/** The `event` table (DESIGN.md §3.1/§7.3/§7.4) -- every remote delete, every delete
 * withheld with its gating precondition, and every verify/extract/move outcome. Also
 * server-capped and paginated.
 */
export function getHistoryEvents(filter: HistoryEventsFilter = {}): Promise<HistoryEventsResponse> {
  return getJson<HistoryEventsResponse>(`/api/history/events${queryString(filter)}`)
}

// --- History: clearing (2026-08-13, prompts/2026-08-13-clear-history.md) ------------------
//
// A *different* action from `dismissJob` above: dismiss only hides a row from Transfers and
// leaves History untouched; these delete the row from History outright, and it's irreversible
// -- every caller must confirm first (DESIGN.md's own instruction; see HistoryJobsSection /
// HistoryEventsSection for the confirmation panel). Bulk clears run server-side as one
// request (not a `Promise.allSettled` loop over ids) -- there's nothing per-row that can fail
// independently the way a stop-then-delete race can, so one `DELETE ... WHERE` is simpler and
// is what `api/history.py`'s own docstring documents as the choice made here.

/** Clear one job record from History. Rejects (throws) for a `queued`/`running` job -- an
 * active transfer is not history and the server enforces this itself (409), not just this
 * button being hidden.
 */
export function clearHistoryJob(jobId: number): Promise<HistoryClearResponse> {
  return sendJson<HistoryClearResponse>(`/api/history/jobs/${jobId}`, 'DELETE')
}

/** Clear every job matching `filter` -- the same filter shape `getHistoryJobs` takes. No
 * filter at all clears every terminal job ("clear all"); `state` alone is "clear by outcome".
 * Never reaches an active job regardless of filter -- see the server-side docstring.
 */
export function clearHistoryJobs(
  filter: Omit<HistoryJobsFilter, 'limit' | 'offset'> = {},
): Promise<HistoryClearResponse> {
  return sendJson<HistoryClearResponse>(`/api/history/jobs${queryString(filter)}`, 'DELETE')
}

/** Clear one event record from History -- no "active" concept the way jobs have, so this
 * always either deletes the row or 404s.
 */
export function clearHistoryEvent(eventId: number): Promise<HistoryClearResponse> {
  return sendJson<HistoryClearResponse>(`/api/history/events/${eventId}`, 'DELETE')
}

/** Clear every event matching `filter` -- the same filter shape `getHistoryEvents` takes.
 * No category is protected: the delete-audit kinds (`remote_delete` etc.) clear the same as
 * any other event kind (docs/decisions.md).
 */
export function clearHistoryEvents(
  filter: Omit<HistoryEventsFilter, 'limit' | 'offset'> = {},
): Promise<HistoryClearResponse> {
  return sendJson<HistoryClearResponse>(`/api/history/events${queryString(filter)}`, 'DELETE')
}

// --- Settings -> Logs (phase 7, DESIGN.md §10.1) -----------------------------------------

export function getLogFiles(): Promise<LogFilesResponse> {
  return getJson<LogFilesResponse>('/api/logs/files')
}

export function getLogTail(lines: number, level?: string): Promise<LogTailResponse> {
  return getJson<LogTailResponse>(`/api/logs/tail${queryString({ lines, level })}`)
}

export function logDownloadUrl(filename: string): string {
  return `/api/logs/${encodeURIComponent(filename)}/download`
}

// --- Settings -> Backup (phase 7, DESIGN.md §10.2) ---------------------------------------

export function getBackupSettings(): Promise<BackupSettingsOut> {
  return getJson<BackupSettingsOut>('/api/settings/backup')
}

export function putBackupSettings(body: BackupSettingsIn): Promise<BackupSettingsOut> {
  return sendJson<BackupSettingsOut>('/api/settings/backup', 'PUT', body)
}

export function listBackups(): Promise<BackupListResponse> {
  return getJson<BackupListResponse>('/api/settings/backup/list')
}

/** DESIGN.md §10.2's "Backup now" -- always takes one immediately, then prunes to the
 * configured keep count.
 */
export function backupNow(): Promise<BackupInfoOut> {
  return sendJson<BackupInfoOut>('/api/settings/backup/now', 'POST')
}

export function backupDownloadUrl(filename: string): string {
  return `/api/settings/backup/${encodeURIComponent(filename)}/download`
}

// --- Metrics / Dashboard (this task -- DESIGN.md new section proposed) -------------------

export function getMetricsSettings(): Promise<MetricsSettingsOut> {
  return getJson<MetricsSettingsOut>('/api/settings/metrics')
}

export function putMetricsSettings(body: MetricsSettingsIn): Promise<MetricsSettingsOut> {
  return sendJson<MetricsSettingsOut>('/api/settings/metrics', 'PUT', body)
}

/** Both Dashboard charts (DESIGN.md new section proposed) -- omit `queueId` for the
 * all-queues breakdown + site total (bytes/hour bar chart, "All queues" speed line); pass it
 * for one queue's own series (speed line with a queue selected). Server-side bucketed
 * (core/metrics.py) -- never raw rows to aggregate here.
 */
export function getThroughput(
  range: MetricsRange,
  queueId?: number,
): Promise<MetricsThroughputResponse> {
  return getJson<MetricsThroughputResponse>(
    `/api/metrics/throughput${queryString({ range, queue_id: queueId })}`,
  )
}

// --- Auth (phase 8, DESIGN.md §8) ---------------------------------------------------------

/** Always reachable with no credentials — see `middleware.py.PUBLIC_API_PATHS` — because a
 * browser that isn't authenticated yet is exactly who needs to call this to find out.
 */
export function getAuthSession(): Promise<AuthSessionOut> {
  return getJson<AuthSessionOut>('/api/auth/session')
}

export function login(body: LoginIn): Promise<AuthSessionOut> {
  return sendJson<AuthSessionOut>('/api/auth/login', 'POST', body)
}

export function logout(): Promise<AuthSessionOut> {
  return sendJson<AuthSessionOut>('/api/auth/logout', 'POST')
}

export function getAuthSettings(): Promise<AuthSettingsOut> {
  return getJson<AuthSettingsOut>('/api/settings/auth')
}

export function putAuthSettings(body: AuthSettingsIn): Promise<AuthSettingsOut> {
  return sendJson<AuthSettingsOut>('/api/settings/auth', 'PUT', body)
}

export function changePassword(body: ChangePasswordIn): Promise<void> {
  return sendJson<void>('/api/settings/auth/password', 'POST', body)
}

export function listApiKeys(): Promise<ApiKeyOut[]> {
  return getJson<ApiKeyOut[]>('/api/settings/auth/api-keys')
}

/** The plaintext `key` on the returned object is shown exactly once — DESIGN.md §8. */
export function createApiKey(body: ApiKeyIn): Promise<ApiKeyCreatedOut> {
  return sendJson<ApiKeyCreatedOut>('/api/settings/auth/api-keys', 'POST', body)
}

export function deleteApiKey(id: number): Promise<void> {
  return sendJson<void>(`/api/settings/auth/api-keys/${id}`, 'DELETE')
}
