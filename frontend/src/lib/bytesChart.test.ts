import { describe, expect, it } from 'vitest'
import type { MetricsBucketOut } from '../api/types'
import {
  bucketLabel,
  bytesChartTitle,
  retentionNoteForRange,
  sumBytesByQueue,
  sumTotalBytes,
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

  it('shows a clock time for hourly (3600s) buckets', () => {
    expect(bucketLabel(ts, 3600)).toBe(new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))
  })

  it('shows weekday + hour for 6-hour (21600s) buckets', () => {
    expect(bucketLabel(ts, 21600)).toBe(
      new Date(ts).toLocaleString([], { weekday: 'short', hour: '2-digit' }),
    )
  })

  it('shows just the date for 1-day (86400s) buckets', () => {
    expect(bucketLabel(ts, 86400)).toBe(
      new Date(ts).toLocaleDateString([], { month: 'short', day: 'numeric' }),
    )
  })

  it('falls back to the raw ts on an unparsable date', () => {
    expect(bucketLabel('not-a-date', 3600)).toBe('not-a-date')
  })
})

describe('bytesChartTitle', () => {
  it('names the bucket width for each scale', () => {
    expect(bytesChartTitle(3600)).toBe('Bytes transferred — per hour')
    expect(bytesChartTitle(21600)).toBe('Bytes transferred — per 6 hours')
    expect(bytesChartTitle(86400)).toBe('Bytes transferred — per day')
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
})
