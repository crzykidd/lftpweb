import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  bothSidesRows,
  bytesToMB,
  formatBytes,
  formatEta,
  formatPercent,
  formatRate,
  formatRelativeTime,
  formatRelativeTimeIntl,
  hasBothSides,
  isStillArriving,
  mbToBytes,
  percentValue,
  settleArrivingLabel,
  settleArrivingShortLabel,
  settleWaitLabel,
  settleWaitShortLabel,
  stateAgeLabel,
} from './format'

describe('formatBytes', () => {
  it('floors non-positive input to "0 B", including exactly zero and negative', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(-1)).toBe('0 B')
  })

  it('shows whole bytes with no decimal', () => {
    expect(formatBytes(1)).toBe('1 B')
    expect(formatBytes(500)).toBe('500 B')
  })

  it('switches units at each 1024 boundary and shows one decimal past bytes', () => {
    expect(formatBytes(1024)).toBe('1.0 KB')
    expect(formatBytes(1024 * 1024)).toBe('1.0 MB')
    expect(formatBytes(1024 * 1024 * 1024)).toBe('1.0 GB')
    expect(formatBytes(1024 * 1024 * 1024 * 1024)).toBe('1.0 TB')
  })

  it('never exceeds the largest unit (TB), even for absurd sizes', () => {
    expect(formatBytes(1024 ** 5)).toBe('1024.0 TB')
  })
})

describe('formatRate', () => {
  it('wraps formatBytes with a /s suffix', () => {
    expect(formatRate(0)).toBe('0 B/s')
    expect(formatRate(1024)).toBe('1.0 KB/s')
  })
})

describe('formatEta', () => {
  it('renders null, undefined, and non-finite input as the unknown dash', () => {
    expect(formatEta(null)).toBe('—')
    expect(formatEta(undefined)).toBe('—')
    expect(formatEta(Number.POSITIVE_INFINITY)).toBe('—')
    expect(formatEta(Number.NaN)).toBe('—')
  })

  it('clamps negative input to zero seconds', () => {
    expect(formatEta(-5)).toBe('0s')
  })

  it('renders seconds-only under a minute', () => {
    expect(formatEta(0)).toBe('0s')
    expect(formatEta(45)).toBe('45s')
  })

  it('renders minutes without seconds once past a minute', () => {
    expect(formatEta(90)).toBe('1m')
    expect(formatEta(3599)).toBe('59m')
  })

  it('renders hours and minutes once past an hour, dropping seconds', () => {
    expect(formatEta(3600)).toBe('1h 0m')
    expect(formatEta(3661)).toBe('1h 1m')
  })
})

describe('percentValue', () => {
  it('is null when done or total is missing', () => {
    expect(percentValue(null, 100)).toBeNull()
    expect(percentValue(50, null)).toBeNull()
    expect(percentValue(null, null)).toBeNull()
  })

  it('is null for a zero or negative total -- never divides by it', () => {
    expect(percentValue(0, 0)).toBeNull()
    expect(percentValue(10, -5)).toBeNull()
  })

  it('rounds to the nearest whole percent', () => {
    expect(percentValue(1, 3)).toBe(33)
    expect(percentValue(2, 3)).toBe(67)
  })

  it('clamps at 100 even when done exceeds total', () => {
    expect(percentValue(150, 100)).toBe(100)
  })

  it('is 0 for done=0 over a positive total, not null', () => {
    expect(percentValue(0, 100)).toBe(0)
  })
})

describe('formatPercent', () => {
  it('renders the unknown dash when percentValue is null', () => {
    expect(formatPercent(null, 100)).toBe('—')
    expect(formatPercent(10, 0)).toBe('—')
  })

  it('renders a percent sign on a real value', () => {
    expect(formatPercent(50, 100)).toBe('50%')
  })
})

describe('bothSidesRows', () => {
  it('omits the Modified row entirely for a directory', () => {
    const rows = bothSidesRows({
      is_dir: true,
      remote_size: 100,
      local_size: 50,
      remote_mtime: 1000,
      local_mtime: 1000,
    })
    expect(rows.map((r) => r.label)).toEqual(['Size'])
  })

  it('includes a Modified row for a file', () => {
    const rows = bothSidesRows({
      is_dir: false,
      remote_size: 100,
      local_size: 50,
      remote_mtime: 1_700_000_000,
      local_mtime: null,
    })
    expect(rows.map((r) => r.label)).toEqual(['Size', 'Modified'])
    expect(rows[1].local).toBe('—')
    expect(rows[1].remote).not.toBe('—')
  })

  it('renders the unknown dash for a null size on either side', () => {
    const rows = bothSidesRows({
      is_dir: false,
      remote_size: null,
      local_size: null,
      remote_mtime: null,
      local_mtime: null,
    })
    expect(rows[0].remote).toBe('—')
    expect(rows[0].local).toBe('—')
    expect(rows[1].remote).toBe('—')
    expect(rows[1].local).toBe('—')
  })
})

describe('hasBothSides', () => {
  it('is true only when both sizes are non-null', () => {
    expect(hasBothSides({ remote_size: 1, local_size: 1 })).toBe(true)
  })

  it('is false when either side is null', () => {
    expect(hasBothSides({ remote_size: null, local_size: 1 })).toBe(false)
    expect(hasBothSides({ remote_size: 1, local_size: null })).toBe(false)
    expect(hasBothSides({ remote_size: null, local_size: null })).toBe(false)
  })
})

describe('bytesToMB / mbToBytes', () => {
  it('round-trips a clean decimal-MB value', () => {
    expect(bytesToMB(10_000_000)).toBe(10)
    expect(mbToBytes(10)).toBe(10_000_000)
  })

  it('mbToBytes rounds to a whole byte count', () => {
    expect(mbToBytes(1.23456)).toBe(1_234_560)
  })
})

describe('formatRelativeTime', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-13T12:00:00.000Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('reads "just now" under 5 seconds', () => {
    expect(formatRelativeTime(new Date('2026-08-13T11:59:57.000Z').toISOString())).toBe('just now')
  })

  it('reads whole seconds between 5s and a minute', () => {
    expect(formatRelativeTime(new Date('2026-08-13T11:59:30.000Z').toISOString())).toBe('30s ago')
  })

  it('reads whole minutes between a minute and an hour', () => {
    expect(formatRelativeTime(new Date('2026-08-13T11:55:00.000Z').toISOString())).toBe('5m ago')
  })

  it('reads whole hours between an hour and a day', () => {
    expect(formatRelativeTime(new Date('2026-08-13T09:00:00.000Z').toISOString())).toBe('3h ago')
  })

  it('reads whole days past a day', () => {
    expect(formatRelativeTime(new Date('2026-08-10T12:00:00.000Z').toISOString())).toBe('3d ago')
  })

  it('never reads negative for a timestamp at or after now (clock skew)', () => {
    expect(formatRelativeTime(new Date('2026-08-13T12:00:05.000Z').toISOString())).toBe('just now')
  })
})

describe('formatRelativeTimeIntl', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-13T12:00:00.000Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('buckets by minute under an hour', () => {
    expect(formatRelativeTimeIntl(new Date('2026-08-13T11:57:00.000Z').toISOString())).toBe('3m ago')
  })

  it('buckets by hour under a day', () => {
    expect(formatRelativeTimeIntl(new Date('2026-08-13T11:00:00.000Z').toISOString())).toBe('1h ago')
  })

  it('buckets by day at a day or more', () => {
    expect(formatRelativeTimeIntl(new Date('2026-08-11T12:00:00.000Z').toISOString())).toBe('2d ago')
  })

  it('falls through to seconds under a minute', () => {
    expect(formatRelativeTimeIntl(new Date('2026-08-13T11:59:15.000Z').toISOString())).toBe('45s ago')
  })
})

describe('stateAgeLabel', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-13T12:00:00.000Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('combines the verb-phrase label with a relative reading when dated', () => {
    expect(stateAgeLabel('DOWNLOADED', new Date('2026-08-13T11:57:00.000Z').toISOString())).toBe(
      'Downloaded 3m ago',
    )
  })

  it('falls back to the bare label when state_changed_at is null (undated backfill row)', () => {
    expect(stateAgeLabel('DOWNLOADED', null)).toBe('Downloaded')
  })

  it('falls back to the raw state string for an unmapped state', () => {
    expect(stateAgeLabel('SOME_NEW_STATE', null)).toBe('SOME_NEW_STATE')
  })
})

describe('isStillArriving', () => {
  it('is true only when settle_matched_scans is exactly 1', () => {
    expect(isStillArriving({ settle_matched_scans: 1 })).toBe(true)
  })

  it('is false for 0, 2+, and null', () => {
    expect(isStillArriving({ settle_matched_scans: 0 })).toBe(false)
    expect(isStillArriving({ settle_matched_scans: 2 })).toBe(false)
    expect(isStillArriving({ settle_matched_scans: null })).toBe(false)
  })
})

describe('settleWaitLabel / settleWaitShortLabel', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-13T12:00:35.000Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  const settle = { required_scans: 2, min_age_s: 60 }

  it('degrades to a bare label when settle constants are unavailable', () => {
    expect(
      settleWaitLabel({ settle_matched_scans: 1, settle_first_matched_at: '2026-08-13T12:00:00.000Z' }, null),
    ).toBe('Waiting for changes')
    expect(settleWaitShortLabel({ settle_matched_scans: 1, settle_first_matched_at: '2026-08-13T12:00:00.000Z' }, null)).toBe(
      'Waiting…',
    )
  })

  it('degrades to a bare label when the row has no settle progress yet', () => {
    expect(settleWaitLabel({ settle_matched_scans: null, settle_first_matched_at: null }, settle)).toBe(
      'Waiting for changes',
    )
  })

  it('spells out the full countdown when both are available', () => {
    expect(
      settleWaitLabel({ settle_matched_scans: 1, settle_first_matched_at: '2026-08-13T12:00:00.000Z' }, settle),
    ).toBe('Waiting for changes -- 1 of 2 scans, 35s of 60s')
  })

  it('renders the short chip form with the same numbers', () => {
    expect(
      settleWaitShortLabel({ settle_matched_scans: 1, settle_first_matched_at: '2026-08-13T12:00:00.000Z' }, settle),
    ).toBe('Waiting 1/2 · 35s')
  })
})

describe('settleArrivingLabel / settleArrivingShortLabel', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-13T12:00:40.000Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('degrades every clause independently when its backing field is null', () => {
    expect(
      settleArrivingLabel({
        settle_total_bytes: null,
        settle_first_observed_at: null,
        settle_last_changed_at: null,
      }),
    ).toBe('Still arriving -- an unknown size so far')
  })

  it('includes size, changed-when, and watched-for once all three are known', () => {
    expect(
      settleArrivingLabel({
        settle_total_bytes: 1024,
        settle_first_observed_at: '2026-08-13T12:00:00.000Z',
        settle_last_changed_at: '2026-08-13T12:00:10.000Z',
      }),
    ).toBe('Still arriving -- 1.0 KB, changed 30s ago -- watching for 40s')
  })

  it('short label falls back to an ellipsis without a byte count', () => {
    expect(settleArrivingShortLabel({ settle_total_bytes: null })).toBe('Arriving…')
  })

  it('short label shows the byte count when known', () => {
    expect(settleArrivingShortLabel({ settle_total_bytes: 2048 })).toBe('Arriving · 2.0 KB')
  })
})
