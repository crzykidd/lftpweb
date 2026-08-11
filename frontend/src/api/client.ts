import type { HealthResponse, StatsResponse } from './types'

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) {
    throw new Error(`${path} responded ${res.status}`)
  }
  return (await res.json()) as T
}

export function getHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>('/api/health')
}

export function getStats(): Promise<StatsResponse> {
  return getJson<StatsResponse>('/api/stats')
}
