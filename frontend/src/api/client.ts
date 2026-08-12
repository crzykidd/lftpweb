import type {
  FilesResponse,
  HealthResponse,
  HostIn,
  HostOut,
  HostTestRequest,
  JobOut,
  JobsResponse,
  PathQueueIn,
  PathQueueOut,
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
