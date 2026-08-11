// Mirrors backend/lftpweb/models.py. Phase 1 only — extend as later phases add endpoints.

export interface HealthResponse {
  status: string
  version: string
  db: boolean
  uptime_s: number
  repo_url: string
}

export interface StatsResponse {
  current_speed_bps: number
  allocated_bps: number
  ceiling_bps: number
  queued_count: number
  queued_bytes: number
  transferred_24h_bytes: number
}
