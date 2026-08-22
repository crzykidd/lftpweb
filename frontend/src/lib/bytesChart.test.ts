import { describe, expect, it } from 'vitest'
import type { MetricsBucketOut, MetricsGroup } from '../api/types'
import {
  bucketLabel,
  bytesChartTitle,
  DEFAULT_GROUP_FOR_RANGE,
  groupOptionsForRange,
  isGroupAvailableForRange,
  isMetricsGroup,
  resolveGroupForRange,
  retentionNoteForRange,
  sumBytesByQueue,
  sumTotalBytes,
  totalSinceLabel,
} from './bytesChart'

function bucket(overrides: Partial<MetricsBucketOut> = {}): MetricsBucketOut {
  return {
    ts: '2026-08-17T06:00:00Z',
    up: true,
    total_bytes: 0,
    by_queue: {},
    ...overrides,
  }
}

describe('sumTotalBytes', () => {
  it('sums total_bytes across up buckets only', () => {
    const buckets = [
      bucket({ total_bytes: 100 }),
      bucket({ total_bytes: 250 }),
      bucket({ up: false, total_bytes: null }),
    ]
    expect(sumTotalBytes(buckets)).toBe(350)
  })

  it('is 0 for no buckets or an all-down window', () => {
    expect(sumTotalBytes([])).toBe(0)
    expect(sumTotalBytes([bucket({ up: false, total_bytes: null })])).toBe(0)
  })
})

describe('sumBytesByQueue', () => {
  it('sums per-queue bytes across up buckets, keyed by numeric queue id', () => {
    const buckets = [
      bucket({ total_bytes: 300, by_queue: { '1': 200, '2': 100 } }),
      bucket({ total_bytes: 150, by_queue: { '1': 150 } }),
      bucket({ up: false, total_bytes: null, by_queue: {} }),
    ]
    expect(sumBytesByQueue(buckets)).toEqual({ 1: 350, 2: 100 })
  })

  it('ignores down buckets even if by_queue is non-empty (should never happen, but defensive)', () => {
    const buckets = [bucket({ up: false, total_bytes: null, by_queue: { '1': 999 } })]
    expect(sumBytesByQueue(buckets)).toEqual({})
  })
})

describe('bucketLabel', () => {
  const ts = '2026-08-17T06:30:00Z'

  it('shows a clock time for hour grouping', () => {
    expect(bucketLabel(ts, 'hour')).toBe(
      new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    )
  })

  it('shows just the date for day grouping', () => {
    expect(bucketLabel(ts, 'day')).toBe(
      new Date(ts).toLocaleDateString([], { month: 'short', day: 'numeric' }),
    )
  })

  it('names the week grouping bucket by the first day it covers', () => {
    expect(bucketLabel(ts, 'week')).toBe(
      `Week of ${new Date(ts).toLocaleDateString([], { month: 'short', day: 'numeric' })}`,
    )
  })

  it('shows month + year for month grouping', () => {
    expect(bucketLabel(ts, 'month')).toBe(
      new Date(ts).toLocaleDateString([], { month: 'short', year: 'numeric' }),
    )
  })

  it('falls back to the raw ts on an unparsable date', () => {
    expect(bucketLabel('not-a-date', 'hour')).toBe('not-a-date')
  })
})

describe('bytesChartTitle', () => {
  it('names the bucket width for each grouping', () => {
    expect(bytesChartTitle('hour')).toBe('Bytes transferred — per hour')
    expect(bytesChartTitle('day')).toBe('Bytes transferred — per day')
    expect(bytesChartTitle('week')).toBe('Bytes transferred — per week')
    expect(bytesChartTitle('month')).toBe('Bytes transferred — per month')
  })
})

describe('isMetricsGroup', () => {
  it('accepts the four known groupings', () => {
    expect(isMetricsGroup('hour')).toBe(true)
    expect(isMetricsGroup('day')).toBe(true)
    expect(isMetricsGroup('week')).toBe(true)
    expect(isMetricsGroup('month')).toBe(true)
  })

  it('rejects anything else, including a stale/hand-edited value', () => {
    expect(isMetricsGroup('minute')).toBe(false)
    expect(isMetricsGroup('')).toBe(false)
    expect(isMetricsGroup(null)).toBe(false)
    expect(isMetricsGroup(42)).toBe(false)
  })
})

describe('DEFAULT_GROUP_FOR_RANGE', () => {
  it('matches the task table -- 24h hourly, 7d/30d daily, 90d/1y weekly', () => {
    expect(DEFAULT_GROUP_FOR_RANGE).toEqual({
      '24h': 'hour',
      '7d': 'day',
      '30d': 'day',
      '90d': 'week',
      '1y': 'week',
    })
  })
})

describe('groupOptionsForRange / isGroupAvailableForRange', () => {
  it('offers every grouping, all available, for the raw-table ranges', () => {
    for (const range of ['24h', '7d', '30d'] as const) {
      const options = groupOptionsForRange(range)
      expect(options.map((o) => o.value)).toEqual(['hour', 'day', 'week', 'month'])
      expect(options.every((o) => o.available)).toBe(true)
    }
  })

  it('disables hour, with a reason, for 90d and 1y -- architecturally impossible', () => {
    for (const range of ['90d', '1y'] as const) {
      const options = groupOptionsForRange(range)
      const hour = options.find((o) => o.value === 'hour')
      expect(hour?.available).toBe(false)
      expect(hour?.reason).toBeTruthy()
      expect(isGroupAvailableForRange(range, 'hour')).toBe(false)

      for (const value of ['day', 'week', 'month'] as MetricsGroup[]) {
        expect(isGroupAvailableForRange(range, value)).toBe(true)
      }
    }
  })
})

describe('resolveGroupForRange', () => {
  it('falls back to the range default when nothing is stored', () => {
    expect(resolveGroupForRange('24h', null)).toBe('hour')
    expect(resolveGroupForRange('90d', null)).toBe('week')
  })

  it('keeps a stored grouping that is still available for the range', () => {
    expect(resolveGroupForRange('30d', 'week')).toBe('week')
    expect(resolveGroupForRange('90d', 'month')).toBe('month')
  })

  it('falls back rather than trusting a stored grouping that is unavailable for this range', () => {
    // 'hour' was valid while a shorter range was selected, but the range changed to 90d/1y,
    // where hourly is impossible -- must not be sent to the server as-is.
    expect(resolveGroupForRange('90d', 'hour')).toBe('week')
    expect(resolveGroupForRange('1y', 'hour')).toBe('week')
  })
})

describe('retentionNoteForRange', () => {
  it('is null when retention is unknown', () => {
    expect(retentionNoteForRange('30d', null)).toBeNull()
  })

  it('is null when the range fits within retention', () => {
    expect(retentionNoteForRange('24h', 7)).toBeNull()
    expect(retentionNoteForRange('7d', 7)).toBeNull()
  })

  it('names the retained window when the range exceeds retention', () => {
    expect(retentionNoteForRange('30d', 7)).toBe(
      'Only the last 7 days are retained — older buckets are empty. Retention is configurable in Settings.',
    )
  })

  it('singularizes a 1-day retention window (verb agreement too)', () => {
    expect(retentionNoteForRange('7d', 1)).toBe(
      'Only the last 1 day is retained — older buckets are empty. Retention is configurable in Settings.',
    )
  })

  // 2026-08-21 (daily rollups, prompts/done/2026-08-21-daily-metric-rollups.md): 90d/1y read
  // metric_daily server-side, not the raw tables -- the raw-retention note's whole premise
  // doesn't apply to them, regardless of how low retentionDays is configured.
  it('never applies to 90d/1y, which read the daily table instead of raw retention', () => {
    expect(retentionNoteForRange('90d', 7)).toBeNull()
    expect(retentionNoteForRange('1y', 1)).toBeNull()
    expect(retentionNoteForRange('90d', null)).toBeNull()
  })
})

describe('totalSinceLabel', () => {
  it('says there is no history yet when since_day is null', () => {
    expect(totalSinceLabel(null)).toBe('no history yet')
  })

  it('formats a real earliest day as "since <date>"', () => {
    expect(totalSinceLabel('2026-05-01')).toBe(
      `since ${new Date('2026-05-01T00:00:00Z').toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })}`,
    )
  })

  it('falls back to the raw string on an unparsable date', () => {
    expect(totalSinceLabel('not-a-date')).toBe('since not-a-date')
  })
})
