import { useMemo } from 'react'
import type { MetricsBucketOut, PathQueueOut } from '../../api/types'
import { formatBytes } from '../../lib/format'
import { assignQueueColorSlots, colorVarForSlot } from './queueColors'
import './chartTheme.css'

const WIDTH = 760
const HEIGHT = 260
const PAD_LEFT = 52
const PAD_RIGHT = 8
const PAD_TOP = 16
const PAD_BOTTOM = 28
const PLOT_W = WIDTH - PAD_LEFT - PAD_RIGHT
const PLOT_H = HEIGHT - PAD_TOP - PAD_BOTTOM
const Y_GRIDLINES = 4

function formatHour(ts: string): string {
  const d = new Date(ts)
  return Number.isNaN(d.getTime())
    ? ts
    : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

interface BytesPerHourChartProps {
  buckets: MetricsBucketOut[]
  queues: PathQueueOut[]
}

/** Chart 1 (DESIGN.md new section proposed): bytes transferred per hour, last 24h, stacked
 * per queue with a site total. Hand-rolled inline SVG, no charting dependency
 * (docs/decisions.md) -- a `down` bucket (no heartbeat at all, `up: false`) renders as a
 * short muted dash at the baseline, never a zero-height bar, so "nothing was transferring"
 * and "lftpweb wasn't running" are never visually the same thing.
 */
export function BytesPerHourChart({ buckets, queues }: BytesPerHourChartProps) {
  const colorSlots = useMemo(() => assignQueueColorSlots(queues), [queues])
  const queuesById = useMemo(() => new Map(queues.map((q) => [q.id, q])), [queues])

  const upBuckets = buckets.filter((b) => b.up)
  const maxTotal = Math.max(1, ...upBuckets.map((b) => b.total_bytes ?? 0))
  // 10% headroom so the tallest bar doesn't touch the plot's top edge.
  const yMax = maxTotal * 1.1

  const barSlot = buckets.length > 0 ? PLOT_W / buckets.length : PLOT_W
  const barWidth = Math.max(barSlot * 0.7, 1)
  const baselineY = PAD_TOP + PLOT_H

  const yToPixel = (bytes: number) => baselineY - (bytes / yMax) * PLOT_H

  const gridlines = Array.from({ length: Y_GRIDLINES + 1 }, (_, i) => {
    const value = (yMax / Y_GRIDLINES) * i
    return { value, y: yToPixel(value) }
  })

  // Every queue that appears anywhere in this window, in fixed color order -- the legend and
  // the stacking order both use this so a queue's segment is always in the same visual
  // position relative to the others.
  const activeQueueIds = useMemo(() => {
    const ids = new Set<number>()
    for (const b of buckets) {
      for (const qid of Object.keys(b.by_queue)) ids.add(Number(qid))
    }
    return [...ids].sort((a, b) => (colorSlots.get(a) ?? 0) - (colorSlots.get(b) ?? 0))
  }, [buckets, colorSlots])

  const totalWindowBytes = upBuckets.reduce((sum, b) => sum + (b.total_bytes ?? 0), 0)

  // Thin the x-axis labels -- 24 labels on a 760px-wide chart is unreadable clutter
  // (dataviz skill: "recessive grid/axes"). Show roughly every 4th bucket, plus the last.
  const labelEvery = Math.max(1, Math.ceil(buckets.length / 6))

  return (
    <div className="viz-root flex flex-col gap-2">
      <div className="flex items-baseline justify-between">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          Bytes transferred per hour — last 24h
        </h3>
        <span className="text-xs" style={{ color: 'var(--chart-ink)' }}>
          total {formatBytes(totalWindowBytes)}
        </span>
      </div>

      {buckets.length === 0 ? (
        <div className="flex h-40 items-center justify-center rounded-lg border border-dashed border-zinc-300 text-sm text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
          No throughput data yet.
        </div>
      ) : (
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          role="img"
          aria-label={`Bar chart of bytes transferred per hour over the last 24 hours, total ${formatBytes(totalWindowBytes)}`}
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
                {formatBytes(value)}
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

          {buckets.map((bucket, i) => {
            const x = PAD_LEFT + i * barSlot + (barSlot - barWidth) / 2
            const showLabel = i % labelEvery === 0 || i === buckets.length - 1

            if (!bucket.up) {
              return (
                <g key={bucket.ts}>
                  <rect
                    x={x}
                    y={baselineY - 3}
                    width={barWidth}
                    height={3}
                    rx={1}
                    fill="var(--chart-gap-fill)"
                  >
                    <title>{`${formatHour(bucket.ts)} — no data (lftpweb was not running)`}</title>
                  </rect>
                  {showLabel && (
                    <text
                      x={x + barWidth / 2}
                      y={baselineY + 14}
                      textAnchor="middle"
                      fontSize={9}
                      fill="var(--chart-muted)"
                    >
                      {formatHour(bucket.ts)}
                    </text>
                  )}
                </g>
              )
            }

            let cumulative = 0
            return (
              <g key={bucket.ts}>
                {activeQueueIds.map((qid) => {
                  const value = bucket.by_queue[String(qid)] ?? 0
                  if (value <= 0) return null
                  const h = (value / yMax) * PLOT_H
                  const y = baselineY - cumulative - h
                  cumulative += h
                  const slot = colorSlots.get(qid) ?? 0
                  const queueName = queuesById.get(qid)?.name ?? `queue ${qid}`
                  return (
                    <rect
                      key={qid}
                      x={x}
                      y={y}
                      width={barWidth}
                      height={h}
                      fill={colorVarForSlot(slot)}
                    >
                      <title>{`${formatHour(bucket.ts)} — ${queueName}: ${formatBytes(value)}`}</title>
                    </rect>
                  )
                })}
                {showLabel && (
                  <text
                    x={x + barWidth / 2}
                    y={baselineY + 14}
                    textAnchor="middle"
                    fontSize={9}
                    fill="var(--chart-muted)"
                  >
                    {formatHour(bucket.ts)}
                  </text>
                )}
              </g>
            )
          })}
        </svg>
      )}

      {activeQueueIds.length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs" style={{ color: 'var(--chart-ink)' }}>
          {activeQueueIds.map((qid) => (
            <span key={qid} className="flex items-center gap-1.5">
              <span
                className="inline-block h-2.5 w-2.5 rounded-full"
                style={{ backgroundColor: colorVarForSlot(colorSlots.get(qid) ?? 0) }}
              />
              {queuesById.get(qid)?.name ?? `queue ${qid}`}
            </span>
          ))}
        </div>
      )}

      {/* Accessible text alternative for the numbers above -- the existing pages' convention
       * (aria-label on interactive controls) doesn't cover a chart's data directly, so this
       * mirrors the same intent as a plain table a screen reader can read row by row. */}
      <table className="sr-only">
        <caption>Bytes transferred per hour, last 24 hours, by queue</caption>
        <thead>
          <tr>
            <th>Hour</th>
            <th>Status</th>
            {activeQueueIds.map((qid) => (
              <th key={qid}>{queuesById.get(qid)?.name ?? `queue ${qid}`}</th>
            ))}
            <th>Total</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((bucket) => (
            <tr key={bucket.ts}>
              <td>{formatHour(bucket.ts)}</td>
              <td>{bucket.up ? 'up' : 'lftpweb offline'}</td>
              {activeQueueIds.map((qid) => (
                <td key={qid}>{formatBytes(bucket.by_queue[String(qid)] ?? 0)}</td>
              ))}
              <td>{bucket.up ? formatBytes(bucket.total_bytes ?? 0) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
