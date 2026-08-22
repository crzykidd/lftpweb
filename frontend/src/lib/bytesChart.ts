// Pure helpers for the Dashboard's bytes chart (2026-08-17,
// prompts/done/2026-08-17-bytes-chart-7d-30d-ranges-and-total.md) -- the 7d/30d ranges this
// task added mean the chart's bucket width is no longer always an hour, so the total, the
// per-bucket label, and the title all have to scale with the selected grouping instead of
// assuming 3600. Kept pure and separate from `BytesChart.tsx` so the range/label math is
// Vitest-testable without rendering SVG.
//
// 2026-08-21 (chart grouping, prompts/done/2026-08-21-chart-grouping.md): `bucketLabel`/
// `bytesChartTitle` now key off the explicit `MetricsGroup` the server echoes back
// (`MetricsThroughputResponse.group`) rather than reverse-engineering a bucket width in seconds
// -- a `week` bucket and a `month` bucket both need their own label shape, not just "coarser
// than a day."

import type { BytesRange, MetricsBucketOut, MetricsGroup } from '../api/types'

export const METRICS_GROUP_VALUES: MetricsGroup[] = ['hour', 'day', 'week', 'month']
export function isMetricsGroup(value: unknown): value is MetricsGroup {
  return typeof value === 'string' && (METRICS_GROUP_VALUES as string[]).includes(value)
}

// The per-range default grouping (task table, from the user's own stated preference): 24h stays
// hourly (already right), 7d moves from 6-hour to daily (the one default that actually changes),
// 30d stays daily, 90d/1y move from daily to weekly. Mirrors `api/metrics.py._DEFAULT_GROUP` --
// keep the two in sync.
export const DEFAULT_GROUP_FOR_RANGE: Record<BytesRange, MetricsGroup> = {
  '24h': 'hour',
  '7d': 'day',
  '30d': 'day',
  '90d': 'week',
  '1y': 'week',
}

export interface GroupOption {
  value: MetricsGroup
  available: boolean
  // Only set when `available` is false -- shown as the disabled reason in the dropdown.
  reason?: string
}

// Not every grouping is available at every range: hourly grouping is architecturally impossible
// at 90d/1y (raw history tops out at 30 days and the daily rollup table is one-day granularity
// by construction, so there is no sub-day data that far back at any retention setting). Every
// other combination is available -- disabling a control for a real capability gap, never faking
// one by silently downgrading (same discipline as `docs/download-client-api-survey.md` §4).
// Mirrors `api/metrics.py._AVAILABLE_GROUPS` -- the server independently rejects the same
// combination with a 422, so this is a UX convenience (grey it out with a reason), never the
// only enforcement.
export function groupOptionsForRange(range: BytesRange): GroupOption[] {
  const hourlyUnavailable = range === '90d' || range === '1y'
  return METRICS_GROUP_VALUES.map((value) => {
    if (value === 'hour' && hourlyUnavailable) {
      return {
        value,
        available: false,
        reason: 'No hourly data this far back — raw history is only kept 30 days.',
      }
    }
    return { value, available: true }
  })
}

export function isGroupAvailableForRange(range: BytesRange, group: MetricsGroup): boolean {
  return groupOptionsForRange(range).find((o) => o.value === group)?.available ?? false
}

/** Resolves a possibly-stale or hand-edited stored grouping against the *current* range --
 * falls back to that range's own default rather than trusting it, the same discipline
 * `DashboardPage`'s `isBytesRange`/`isSpeedRange` already apply to the range itself. Covers two
 * cases: the stored value was valid for a previously-selected range but isn't available for the
 * one now selected (e.g. `hour` stored while viewing `24h`, then the range changes to `90d`), and
 * there is no stored value yet at all (`null`).
 */
export function resolveGroupForRange(range: BytesRange, stored: MetricsGroup | null): MetricsGroup {
  if (stored != null && isGroupAvailableForRange(range, stored)) return stored
  return DEFAULT_GROUP_FOR_RANGE[range]
}

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

/** Grouping-scaled label for an x-axis tick / bar tooltip (task prompt item 3; regrouped
 * 2026-08-21, chart grouping, to key off the explicit `MetricsGroup` rather than a bucket-width
 * number) -- `hour` buckets show a clock time exactly as the chart always has; `day` buckets
 * show just the date -- a clock time on a bucket that spans a whole day would be false
 * precision; `week` buckets are named by the first day they cover (the same convention
 * `api/metrics.py._aggregate_day_points` uses for a week/month bucket's own `ts`); `month`
 * buckets show the month and year. Falls back to the raw `ts` on an unparsable date, the same
 * defensive shape `BytesChart`'s sibling `SpeedLineChart.formatTime` already uses.
 */
export function bucketLabel(ts: string, group: MetricsGroup): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  switch (group) {
    case 'month':
      return d.toLocaleDateString([], { month: 'short', year: 'numeric' })
    case 'week':
      return `Week of ${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}`
    case 'day':
      return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
    case 'hour':
    default:
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
}

/** The chart title names what one bar actually represents -- it tracks the selected grouping
 * (task prompt item 3), not just the range label the selector button beside it already shows.
 */
export function bytesChartTitle(group: MetricsGroup): string {
  switch (group) {
    case 'month':
      return 'Bytes transferred — per month'
    case 'week':
      return 'Bytes transferred — per week'
    case 'day':
      return 'Bytes transferred — per day'
    case 'hour':
    default:
      return 'Bytes transferred — per hour'
  }
}

/** How many days of history a bytes-chart range actually spans -- the retention-note gate
 * (task prompt item 5) compares this against the configured retention setting, not the
 * range's button label.
 */
export const BYTES_RANGE_DAYS: Record<BytesRange, number> = {
  '24h': 1,
  '7d': 7,
  '30d': 30,
  '90d': 90,
  '1y': 365,
}

// 2026-08-21 (daily rollups, prompts/done/2026-08-21-daily-metric-rollups.md): 90d/1y are
// answered from `metric_daily` (api/metrics.py's `_DAILY_RANGES`), not the raw tables --
// `retentionNoteForRange`'s whole premise (the selected span outrunning the *raw*, user-
// configurable retention setting) doesn't apply to them; any gap in a 90d/1y chart is either a
// day predating this feature's rollout, or one that aged out of the daily table's own fixed
// ~13-month retention, neither of which `GET /api/settings/metrics` describes.
const DAILY_TABLE_RANGES: ReadonlySet<BytesRange> = new Set(['90d', '1y'])

/** Task prompt item 5's retention-honesty note: `null` unless the selected range's span
 * exceeds what's actually retained, in which case some of the range's own buckets are
 * guaranteed-empty gaps by construction (pruned by `core/metrics.py.prune_metrics`, not "lftpweb
 * wasn't running") rather than a real absence of transfers -- without this, a default
 * 7-day-retention install picking 30d sees a chart that looks broken with no explanation.
 * `retentionDays == null` (the one-time settings fetch hasn't resolved yet, or failed) means
 * "say nothing" rather than guessing at a number that might not match this install.
 */
export function retentionNoteForRange(range: BytesRange, retentionDays: number | null): string | null {
  if (DAILY_TABLE_RANGES.has(range)) return null
  if (retentionDays == null || BYTES_RANGE_DAYS[range] <= retentionDays) return null
  return retentionDays === 1
    ? 'Only the last 1 day is retained — older buckets are empty. Retention is configurable in Settings.'
    : `Only the last ${retentionDays} days are retained — older buckets are empty. Retention is configurable in Settings.`
}

/** The Dashboard's "total downloaded" readout label (task: "a user can have the option to just
 * see their total downloaded amount") -- `since <date>` for a real earliest day, or an honest
 * "no history yet" rather than a bare number with no context, when `metric_daily` (and today's
 * raw samples) are both still empty (a fresh install, or a queue just added).
 */
export function totalSinceLabel(sinceDay: string | null): string {
  if (!sinceDay) return 'no history yet'
  const d = new Date(`${sinceDay}T00:00:00Z`)
  if (Number.isNaN(d.getTime())) return `since ${sinceDay}`
  return `since ${d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })}`
}
