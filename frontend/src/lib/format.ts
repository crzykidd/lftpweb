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

/** The one place `done`/`total` becomes a percentage (0-100, clamped, or `null` when it
 * wouldn't mean anything -- no total, a non-positive total, or no `done` reading yet). Guards
 * the exact inputs that would otherwise produce `NaN` or a negative width: `total <= 0`
 * (division by zero or a nonsensical negative denominator) and `done == null`/`total == null`
 * (nothing to compare). `formatPercent` and `FileTree.tsx`'s inline progress bar both build on
 * this rather than each rolling their own -- "reuse formatPercent rather than writing a second
 * percentage" (prompts/2026-08-13-lifecycle-icons.md), factored so the bar's numeric fraction
 * and the text's rounded label can never disagree.
 */
export function percentValue(done: number | null, total: number | null): number | null {
  if (done == null || total == null || total <= 0) return null
  return Math.min(Math.round((done / total) * 100), 100)
}

export function formatPercent(done: number | null, total: number | null): string {
  const value = percentValue(done, total)
  return value == null ? '—' : `${value}%`
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

// migration 006's `item.state_changed_at` -- the Files tree's per-row "when did this last
// move" reading (DESIGN.md §9.2). Unlike `formatRelativeTime` above, this one is built on
// `Intl.RelativeTimeFormat` (no new dependency -- this project has deliberately avoided
// adding frontend ones, docs/decisions.md) rather than a hand-rolled bucket chain, because the
// Files tree can hold thousands of rows and needs the day/hour/minute/second boundaries
// (including "yesterday"/"now") got right in one place instead of re-derived per caller.
const RELATIVE_UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ['day', 86400],
  ['hour', 3600],
  ['minute', 60],
]
const relativeTimeFormatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto', style: 'narrow' })

/** "3m ago" / "yesterday" style reading of an ISO timestamp, always relative to `Date.now()`
 * at call time -- this function owns no timer of its own. `FileTree.tsx` drives freshness with
 * one shared ticker per tree (a bumped counter that forces a re-render), never a per-row
 * `setInterval`, which is the expensive mistake to avoid at the row counts this page can hit.
 */
export function formatRelativeTimeIntl(iso: string): string {
  const elapsedS = Math.max(Math.round((Date.now() - new Date(iso).getTime()) / 1000), 0)
  for (const [unit, secondsPerUnit] of RELATIVE_UNITS) {
    if (elapsedS >= secondsPerUnit) {
      return relativeTimeFormatter.format(-Math.round(elapsedS / secondsPerUnit), unit)
    }
  }
  return relativeTimeFormatter.format(-elapsedS, 'second')
}

// A verb-phrase reading of each §3.2 state for the "Downloaded 3m ago" / "Remote 2h ago"
// column -- distinct from `StateChip.tsx`'s STYLES map (which colors the state's own name
// verbatim) because a chip and a sentence want different words for the same value.
const STATE_AGE_LABELS: Record<string, string> = {
  REMOTE_ONLY: 'Remote',
  QUEUED: 'Queued',
  DOWNLOADING: 'Downloading',
  PARTIAL: 'Partial',
  STOPPED: 'Stopped',
  DOWNLOADED: 'Downloaded',
  EXCLUDED: 'Excluded',
  VERIFYING: 'Verifying',
  VERIFIED: 'Verified',
  CORRUPT: 'Corrupt',
  EXTRACTING: 'Extracting',
  EXTRACTED: 'Extracted',
  EXTRACT_FAILED: 'Extract failed',
  FAILED: 'Failed',
  LOCAL_ONLY: 'Local',
  // 'Removed locally' (2026-08-13, prompts/2026-08-13-resizable-file-columns.md audit)
  // combined with a relative-time reading ("Removed locally yesterday") ran past this
  // column's width more often than any other label here -- shortened to 'Deleted', still
  // distinct from REMOVED_BOTH's 'Removed' below (this row's remote copy survives; that one's
  // doesn't). The fuller distinction stays available regardless: the Status chip in the
  // neighboring column always shows the raw state, `REMOVED_LOCAL`/`REMOVED_BOTH` verbatim,
  // uncut -- this column is a second, shorter reading of the same fact, not the only one.
  REMOVED_LOCAL: 'Deleted',
  REMOVED_BOTH: 'Removed',
}

/** "Downloaded 3m ago" / "Remote 2h ago" -- the label a Files row's age column actually shows.
 * `stateChangedAt` is `null` when migration 006's backfill genuinely couldn't date a
 * pre-existing row (DESIGN.md §9.2); shown as the bare state label rather than something
 * fabricated.
 */
export function stateAgeLabel(state: string, stateChangedAt: string | null): string {
  const label = STATE_AGE_LABELS[state] ?? state
  if (stateChangedAt == null) return label
  return `${label} ${formatRelativeTimeIntl(stateChangedAt)}`
}

// The settle gate's wait, spelled out (2026-08-13, prompts/2026-08-13-files-ux-pass.md item 3)
// -- replaces the previous 6px dot (`h-1.5 w-1.5`, effectively invisible in `FileTree.tsx`)
// with a readable countdown, "Waiting for changes -- 1 of 2 scans, 35s of 60s". Shared between
// `FileTree.tsx`'s Status-column chip label (which substitutes it wholesale for the normal
// state text while a top-level item is settling) and `LifecycleIcons.tsx`'s R-icon tooltip --
// one function, so the two can never disagree about what "waiting" means for the same row. The
// node/settings shapes it needs (`settle_matched_scans`/`_first_matched_at`, `required_scans`/
// `min_age_s`) are structural enough not to warrant importing `api/types.ts`'s named types here
// -- a minimal inline shape keeps this file free of a dependency on the wire-shape module.
interface SettleProgressNode {
  settle_matched_scans: number | null
  settle_first_matched_at: string | null
}
interface SettleConstants {
  required_scans: number
  min_age_s: number
}

/** `settle` is `null` before `getSettleSettings()`'s one site-wide fetch resolves
 * (`FileTree.tsx` fetches it once, not per row) or if it failed; `node.settle_matched_scans`/
 * `_first_matched_at` are `null` whenever `core/itemview.py.item_view` didn't have both a join
 * match and `substate === 'settling'` for this row. Either way this degrades to the bare label
 * rather than showing a stale or fabricated count -- never blocks the row from rendering.
 */
export function settleWaitLabel(node: SettleProgressNode, settle: SettleConstants | null): string {
  if (settle == null || node.settle_matched_scans == null || node.settle_first_matched_at == null) {
    return 'Waiting for changes'
  }
  const elapsedS = Math.max(
    0,
    Math.floor((Date.now() - new Date(node.settle_first_matched_at).getTime()) / 1000),
  )
  return (
    `Waiting for changes -- ${node.settle_matched_scans} of ${settle.required_scans} scans, ` +
    `${elapsedS}s of ${Math.round(settle.min_age_s)}s`
  )
}

/** The Status chip's own in-cell text (2026-08-13, prompts/2026-08-13-resizable-file-
 * columns.md) -- `settleWaitLabel` above is the complete sentence, right for a hover tooltip,
 * but it was being shown verbatim *in the chip itself*, inside a column a few characters wide,
 * where it just ran off the edge. This is the short form the chip actually renders; the full
 * sentence survives on hover (`FileTree.tsx`'s `Row` passes `settleWaitLabel`'s result as the
 * chip's own `title`), so nothing is lost, just not force-fit into the cell. Keeps a verb
 * ("Waiting") rather than a bare `1/2 · 35s` -- a number pair with no verb reads as data, not
 * status, which was the one thing to avoid per the task's own bar.
 */
export function settleWaitShortLabel(node: SettleProgressNode, settle: SettleConstants | null): string {
  if (settle == null || node.settle_matched_scans == null || node.settle_first_matched_at == null) {
    return 'Waiting…'
  }
  const elapsedS = Math.max(
    0,
    Math.floor((Date.now() - new Date(node.settle_first_matched_at).getTime()) / 1000),
  )
  return `Waiting ${node.settle_matched_scans}/${settle.required_scans} · ${elapsedS}s`
}
