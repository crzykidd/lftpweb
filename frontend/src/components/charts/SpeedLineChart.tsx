import { useMemo } from 'react'
import type { MetricsBucketOut } from '../../api/types'
import { formatRate } from '../../lib/format'
import './chartTheme.css'

const WIDTH = 760
const HEIGHT = 220
const PAD_LEFT = 56
const PAD_RIGHT = 8
const PAD_TOP = 16
const PAD_BOTTOM = 26
const PLOT_W = WIDTH - PAD_LEFT - PAD_RIGHT
const PLOT_H = HEIGHT - PAD_TOP - PAD_BOTTOM
const Y_GRIDLINES = 4

function formatTime(ts: string): string {
  const d = new Date(ts)
  return Number.isNaN(d.getTime())
    ? ts
    : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

interface SpeedPoint {
  ts: string
  up: boolean
  speedBps: number | null // null when `up` is false -- a gap, never a zero
}

interface SpeedLineChartProps {
  buckets: MetricsBucketOut[]
  bucketSeconds: number
  seriesLabel: string
  colorVar: string
}

/** Chart 2 (DESIGN.md new section proposed): speed over time, with a 1h/12h/24h range
 * selector (the selector itself lives in `DashboardPage`, which also decides *which* series
 * -- site total or one queue -- this instance renders; this component only ever draws one
 * line). Speed is derived at render time from the same bucketed byte deltas the bar chart
 * uses (`bytes / bucket_seconds`, decision 5: one table serves both charts by re-bucketing).
 * A `down` bucket breaks the line into a separate path segment rather than dropping to zero
 * -- the same idle-vs-down distinction as `BytesPerHourChart`.
 */
export function SpeedLineChart({ buckets, bucketSeconds, seriesLabel, colorVar }: SpeedLineChartProps) {
  const points: SpeedPoint[] = useMemo(
    () =>
      buckets.map((b) => ({
        ts: b.ts,
        up: b.up,
        speedBps: b.up ? (b.total_bytes ?? 0) / bucketSeconds : null,
      })),
    [buckets, bucketSeconds],
  )

  const maxSpeed = Math.max(1, ...points.filter((p) => p.speedBps != null).map((p) => p.speedBps ?? 0))
  const yMax = maxSpeed * 1.1
  const baselineY = PAD_TOP + PLOT_H

  const xForIndex = (i: number) =>
    points.length > 1 ? PAD_LEFT + (i / (points.length - 1)) * PLOT_W : PAD_LEFT
  const yForSpeed = (speed: number) => baselineY - (speed / yMax) * PLOT_H

  const gridlines = Array.from({ length: Y_GRIDLINES + 1 }, (_, i) => {
    const value = (yMax / Y_GRIDLINES) * i
    return { value, y: yForSpeed(value) }
  })

  // Split into contiguous "up" runs -- each becomes its own <path>, so the line breaks
  // (a real gap) at every down bucket instead of interpolating across it.
  const segments: { x: number; y: number; ts: string; speed: number }[][] = []
  let current: { x: number; y: number; ts: string; speed: number }[] = []
  points.forEach((p, i) => {
    if (p.up && p.speedBps != null) {
      current.push({ x: xForIndex(i), y: yForSpeed(p.speedBps), ts: p.ts, speed: p.speedBps })
    } else {
      if (current.length > 0) segments.push(current)
      current = []
    }
  })
  if (current.length > 0) segments.push(current)

  const downRuns = points.length === 0 || points.every((p) => !p.up)
  const labelEvery = Math.max(1, Math.ceil(points.length / 6))

  return (
    <div className="viz-root flex flex-col gap-2">
      {buckets.length === 0 || downRuns ? (
        <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-zinc-300 text-sm text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
          {buckets.length === 0
            ? 'No throughput data yet.'
            : 'lftpweb was not running for this entire window.'}
        </div>
      ) : (
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          role="img"
          aria-label={`Line chart of ${seriesLabel} transfer speed over time`}
          className="w-full"
        >
          {gridlines.map(({ value, y }) => (
            <g key={value}>
              <line
                x1={PAD_LEFT}
                x2={WIDTH - PAD_RIGHT}
                y1={y}
                y2={y}
                stroke="var(--chart-grid)"
                strokeWidth={1}
              />
              <text
                x={PAD_LEFT - 6}
                y={y}
                textAnchor="end"
                dominantBaseline="middle"
                fontSize={10}
                fill="var(--chart-muted)"
              >
                {formatRate(value)}
              </text>
            </g>
          ))}
          <line
            x1={PAD_LEFT}
            x2={WIDTH - PAD_RIGHT}
            y1={baselineY}
            y2={baselineY}
            stroke="var(--chart-axis)"
            strokeWidth={1}
          />

          {segments.map((seg, si) => (
            <path
              key={si}
              d={seg.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${p.y}`).join(' ')}
              fill="none"
              stroke={colorVar}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          ))}
          {segments.flat().map((p) => (
            <circle key={p.ts} cx={p.x} cy={p.y} r={2.5} fill={colorVar}>
              <title>{`${formatTime(p.ts)} — ${formatRate(p.speed)}`}</title>
            </circle>
          ))}

          {points.map((p, i) =>
            i % labelEvery === 0 || i === points.length - 1 ? (
              <text
                key={p.ts}
                x={xForIndex(i)}
                y={baselineY + 16}
                textAnchor="middle"
                fontSize={9}
                fill="var(--chart-muted)"
              >
                {formatTime(p.ts)}
              </text>
            ) : null,
          )}
        </svg>
      )}

      <table className="sr-only">
        <caption>{seriesLabel} transfer speed over time</caption>
        <thead>
          <tr>
            <th>Time</th>
            <th>Status</th>
            <th>Speed</th>
          </tr>
        </thead>
        <tbody>
          {points.map((p) => (
            <tr key={p.ts}>
              <td>{formatTime(p.ts)}</td>
              <td>{p.up ? 'up' : 'lftpweb offline'}</td>
              <td>{p.speedBps != null ? formatRate(p.speedBps) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
