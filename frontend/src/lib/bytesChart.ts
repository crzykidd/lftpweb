// Pure helpers for the Dashboard's bytes chart (2026-08-17,
// prompts/done/2026-08-17-bytes-chart-7d-30d-ranges-and-total.md) -- the 7d/30d ranges this
// task added mean the chart's bucket width is no longer always an hour, so the total, the
// per-bucket label, and the title all have to scale with `bucket_seconds` (`api/metrics.py`'s
// `_RANGES`) instead of assuming 3600. Kept pure and separate from `BytesChart.tsx` so the
// range/label math is Vitest-testable without rendering SVG.

import type { BytesRange, MetricsBucketOut } from '../api/types'

/** Sum of `total_bytes` across every "up" bucket -- the range total the chart's header shows
 * ("Total: 84.2 GB"). A `down` bucket's `total_bytes` is always `null` (idle-vs-down,
 * docs/decisions.md) and contributes 0, never treated as a real, present zero the way an idle
 * bucket's own `total_bytes: 0` would be.
 */
export function sumTotalBytes(buckets: MetricsBucketOut[]): number {
  return buckets.reduce((sum, b) => sum + (b.up ? (b.total_bytes ?? 0) : 0), 0)
}

/** Same sum, split by queue id -- what the legend appends to each queue's own entry ("same
 * numbers, one place," per the task prompt, not a second computation). Only buckets with
 * `up: true` contribute; `by_queue` is always `{}` on a down bucket anyway (docs/decisions.md).
 */
export function sumBytesByQueue(buckets: MetricsBucketOut[]): Record<number, number> {
  const totals: Record<number, number> = {}
  for (const b of buckets) {
    if (!b.up) continue
    for (const [qid, bytes] of Object.entries(b.by_queue)) {
      const id = Number(qid)
      totals[id] = (totals[id] ?? 0) + bytes
    }
  }
  return totals
}

/** Bucket-width-scaled label for an x-axis tick / bar tooltip (task prompt item 3) -- hourly
 * buckets (3600s, the 24h range) show a clock time exactly as the chart always has; 6-hour
 * buckets (21600s, the 7d range) show enough to place the bucket in its day (weekday + hour);
 * 1-day buckets (86400s, the 30d range) show just the date -- a clock time on a bucket that
 * spans a whole day would be false precision. Falls back to the raw `ts` on an unparsable date,
 * the same defensive shape `BytesChart`'s sibling `SpeedLineChart.formatTime` already uses.
 */
export function bucketLabel(ts: string, bucketSeconds: number): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  if (bucketSeconds >= 86400) {
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  }
  if (bucketSeconds >= 21600) {
    return d.toLocaleString([], { weekday: 'short', hour: '2-digit' })
  }
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/** The chart title names what one bar actually represents -- it tracks the selected range's
 * bucket width (task prompt item 3), not just the range label the selector button beside it
 * already shows.
 */
export function bytesChartTitle(bucketSeconds: number): string {
  if (bucketSeconds >= 86400) return 'Bytes transferred — per day'
  if (bucketSeconds >= 21600) return 'Bytes transferred — per 6 hours'
  return 'Bytes transferred — per hour'
}

/** How many days of history a bytes-chart range actually spans -- the retention-note gate
 * (task prompt item 5) compares this against the configured retention setting, not the
 * range's button label.
 */
export const BYTES_RANGE_DAYS: Record<BytesRange, number> = {
  '24h': 1,
  '7d': 7,
  '30d': 30,
}

/** Task prompt item 5's retention-honesty note: `null` unless the selected range's span
 * exceeds what's actually retained, in which case some of the range's own buckets are
 * guaranteed-empty gaps by construction (pruned by `core/metrics.py.prune_metrics`, not "lftpweb
 * wasn't running") rather than a real absence of transfers -- without this, a default
 * 7-day-retention install picking 30d sees a chart that looks broken with no explanation.
 * `retentionDays == null` (the one-time settings fetch hasn't resolved yet, or failed) means
 * "say nothing" rather than guessing at a number that might not match this install.
 */
export function retentionNoteForRange(range: BytesRange, retentionDays: number | null): string | null {
  if (retentionDays == null || BYTES_RANGE_DAYS[range] <= retentionDays) return null
  return retentionDays === 1
    ? 'Only the last 1 day is retained — older buckets are empty. Retention is configurable in Settings.'
    : `Only the last ${retentionDays} days are retained — older buckets are empty. Retention is configurable in Settings.`
}
