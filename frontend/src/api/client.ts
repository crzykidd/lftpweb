import type {
  FilesResponse,
  HealthResponse,
  HostIn,
  HostOut,
  HostTestRequest,
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
