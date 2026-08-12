import type {
  BackupInfoOut,
  BackupListResponse,
  BackupSettingsIn,
  BackupSettingsOut,
  FilesResponse,
  HealthResponse,
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
  LogFilesResponse,
  LogTailResponse,
  PathQueueIn,
  PathQueueOut,
  PatternIn,
  PatternOut,
  PatternPreviewRequest,
  PatternPreviewResponse,
  PostprocessSettingsIn,
  PostprocessSettingsOut,
  QueueAutoQueueStatus,
  StatsResponse,
  TestConnectionResponse,
} from './types'

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) {
    throw new Error(`${path} responded ${res.status}`)
  }
  return (await res.json()) as T
}

async function sendJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
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
