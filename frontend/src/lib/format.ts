const UNITS = ['B', 'KB', 'MB', 'GB', 'TB']

export function formatBytes(bytes: number): string {
  if (bytes <= 0) return '0 B'
  const exp = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), UNITS.length - 1)
  const value = bytes / 1024 ** exp
  return `${value.toFixed(exp === 0 ? 0 : 1)} ${UNITS[exp]}`
}

export function formatRate(bytesPerSecond: number): string {
  return `${formatBytes(bytesPerSecond)}/s`
}

/** `eta_s` from `core/progress.py` -- `null` when speed is 0 or the total is unknown. */
export function formatEta(etaSeconds: number | null | undefined): string {
  if (etaSeconds == null || !Number.isFinite(etaSeconds)) return '—'
  const total = Math.max(Math.round(etaSeconds), 0)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m`
  return `${s}s`
}

export function formatPercent(done: number | null, total: number | null): string {
  if (done == null || total == null || total <= 0) return '—'
  return `${Math.min(Math.round((done / total) * 100), 100)}%`
}

// DESIGN.md §4.5/§9.3's Settings -> Transfer fields are stored on the wire as bytes(/s) --
// nobody should have to type `10000000`. Decimal MB (1,000,000 B), not binary MiB: it's what
// makes `core/queue.py.TransferSettings`'s own round defaults (10_000_000 bps, 1_000_000 B
// floors) come back as clean numbers ("10", not "9.5367...") rather than an arbitrary choice.
const MB_BYTES = 1_000_000

export function bytesToMB(bytes: number): number {
  return bytes / MB_BYTES
}

export function mbToBytes(mb: number): number {
  return Math.round(mb * MB_BYTES)
}

/** "scanned 12s ago" style reading for a `scan_complete`/snapshot timestamp (Files page,
 * DESIGN.md §9.2). Deliberately recomputed only on render, never on a ticking timer -- the
 * Files page is WebSocket-driven (DESIGN.md §9), and a client-side interval here would be
 * exactly the kind of client-side refresh loop that page is built to avoid; each queue
 * already re-renders at least every `scan_interval_s` (default 30s) as its own `scan_complete`
 * arrives, which is fresh enough for a relative reading measured in seconds-to-minutes.
 */
export function formatRelativeTime(iso: string): string {
  const deltaMs = Date.now() - new Date(iso).getTime()
  const deltaS = Math.max(Math.round(deltaMs / 1000), 0)
  if (deltaS < 5) return 'just now'
  if (deltaS < 60) return `${deltaS}s ago`
  const deltaM = Math.round(deltaS / 60)
  if (deltaM < 60) return `${deltaM}m ago`
  const deltaH = Math.round(deltaM / 60)
  if (deltaH < 24) return `${deltaH}h ago`
  const deltaD = Math.round(deltaH / 24)
  return `${deltaD}d ago`
}
