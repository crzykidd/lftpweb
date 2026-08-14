import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  bothSidesRows,
  bytesToMB,
  childEtaS,
  formatBytes,
  formatEta,
  formatPercent,
  formatRate,
  formatRelativeTime,
  formatRelativeTimeIntl,
  hasBothSides,
  isRemovalGracePending,
  isStillArriving,
  mbToBytes,
  percentValue,
  REMOVAL_GRACE_ELIGIBLE_STATES,
  removalGraceLabel,
  removalGraceRemainingS,
  removalGraceShortLabel,
  settleArrivingLabel,
  settleArrivingShortLabel,
  settleWaitLabel,
  settleWaitShortLabel,
  stateAgeLabel,
  transferEtaLabel,
  transferSpeedLabel,
  transferSpeedSortValue,
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

// 2026-08-14 (prompts/2026-08-14-files-page-speed-column.md): the Files page's Speed column.
// Both functions gate on state === 'DOWNLOADING', never on whether a value is present -- see
// their own docstrings in format.ts for why (a `progress` WS reading is never pruned client-side
// on job completion, so a stale value must not linger past the state that actually made it live).
describe('transferSpeedLabel', () => {
  it('shows the formatted rate for an actively downloading row', () => {
    expect(transferSpeedLabel('DOWNLOADING', 5_242_880)).toBe('5.0 MB/s')
  })

  it('shows a real 0 B/s for a downloading row with a genuine zero reading -- ' +
    'a stalled transfer is not the same statement as "not transferring"', () => {
    expect(transferSpeedLabel('DOWNLOADING', 0)).toBe('0 B/s')
  })

  it('is a dash for any non-DOWNLOADING state regardless of what speed value happens to be present', () => {
    expect(transferSpeedLabel('DOWNLOADED', 5_000_000)).toBe('—')
    expect(transferSpeedLabel('PARTIAL', 5_000_000)).toBe('—')
    expect(transferSpeedLabel('QUEUED', 5_000_000)).toBe('—')
    expect(transferSpeedLabel('REMOTE_ONLY', null)).toBe('—')
  })

  it('is a dash when downloading but no speed reading has arrived yet, or the value is non-finite', () => {
    expect(transferSpeedLabel('DOWNLOADING', null)).toBe('—')
    expect(transferSpeedLabel('DOWNLOADING', undefined)).toBe('—')
    expect(transferSpeedLabel('DOWNLOADING', Number.NaN)).toBe('—')
  })

  it('floors a negative speed reading to zero rather than showing a negative rate', () => {
    expect(transferSpeedLabel('DOWNLOADING', -100)).toBe('0 B/s')
  })
})

describe('transferSpeedSortValue', () => {
  it('returns the raw speed for an actively downloading row, including a genuine zero', () => {
    expect(transferSpeedSortValue('DOWNLOADING', 5_000_000)).toBe(5_000_000)
    expect(transferSpeedSortValue('DOWNLOADING', 0)).toBe(0)
  })

  it('returns null for any non-transferring row, so it sorts alongside every other unknown reading', () => {
    expect(transferSpeedSortValue('DOWNLOADED', 5_000_000)).toBeNull()
    expect(transferSpeedSortValue('REMOTE_ONLY', null)).toBeNull()
  })

  it('returns null when downloading but no reading is present yet', () => {
    expect(transferSpeedSortValue('DOWNLOADING', null)).toBeNull()
    expect(transferSpeedSortValue('DOWNLOADING', undefined)).toBeNull()
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

// 2026-08-14 ("ETA on Files rows"): the job-level ETA text for a Files row -- `entry.eta_s` is
// already fully computed server-side (`core/progress.py.JobProgress.eta_s`), so this only gates
// display, the identical rule `transferSpeedLabel` above already applies to `speed_bps`.
describe('transferEtaLabel', () => {
  it('shows the formatted ETA for an actively downloading row', () => {
    expect(transferEtaLabel('DOWNLOADING', 125)).toBe('2m')
  })

  it('is a dash for any non-DOWNLOADING state regardless of what eta value happens to be present', () => {
    expect(transferEtaLabel('DOWNLOADED', 125)).toBe('—')
    expect(transferEtaLabel('PARTIAL', 125)).toBe('—')
    expect(transferEtaLabel('QUEUED', 125)).toBe('—')
    expect(transferEtaLabel('REMOTE_ONLY', null)).toBe('—')
  })

  it('is a dash when downloading but no eta reading has arrived, or bytes_total is unknown, ' +
    'or the value is non-finite', () => {
    expect(transferEtaLabel('DOWNLOADING', null)).toBe('—')
    expect(transferEtaLabel('DOWNLOADING', undefined)).toBe('—')
    expect(transferEtaLabel('DOWNLOADING', Number.NaN)).toBe('—')
    expect(transferEtaLabel('DOWNLOADING', Number.POSITIVE_INFINITY)).toBe('—')
  })
})

// 2026-08-14 ("ETA on Files rows"): a child file's own ETA, derived client-side (no `eta_s` is
// ever published for a child -- `_publish_child_progress` only ever emits a rate). "Show nothing
// rather than a wrong number" is the task's own bar -- every degenerate case below returns
// `null`, never `Infinity`, `NaN`, or a negative reading.
describe('childEtaS', () => {
  it('divides remaining bytes by the given rate for the normal case', () => {
    // 100 MB remaining at 1 MB/s -- 100 seconds left.
    expect(childEtaS(200_000_000, 100_000_000, 1_000_000)).toBe(100)
  })

  it('is null when remote_size is unknown -- no denominator, not this path\'s call to guess one', () => {
    expect(childEtaS(null, 10, 1_000_000)).toBeNull()
  })

  it('is null when local_size is unknown', () => {
    expect(childEtaS(1_000_000, null, 1_000_000)).toBeNull()
  })

  it('is null for a zero rate -- never divides by zero into Infinity', () => {
    expect(childEtaS(1_000_000, 0, 0)).toBeNull()
  })

  it('is null when there is no fresh sample at all (the caller passes null)', () => {
    expect(childEtaS(1_000_000, 0, null)).toBeNull()
  })

  it('is null for a non-finite rate', () => {
    expect(childEtaS(1_000_000, 0, Number.NaN)).toBeNull()
    expect(childEtaS(1_000_000, 0, Number.POSITIVE_INFINITY)).toBeNull()
  })

  it('is null for a negative rate -- a malformed reading, not a valid one to divide by', () => {
    expect(childEtaS(1_000_000, 0, -500)).toBeNull()
  })

  it('is null once remaining bytes drop to zero or below -- the file is done, not "0s ETA"', () => {
    expect(childEtaS(1_000_000, 1_000_000, 500_000)).toBeNull() // exactly done
    expect(childEtaS(1_000_000, 1_500_000, 500_000)).toBeNull() // local exceeds remote
  })

  it('a very small rate produces a very large (but honest, uncapped) ETA', () => {
    // 1 GB remaining at 1 B/s -- a huge but real number, not clamped to some "> 1h" ceiling.
    expect(childEtaS(1_000_000_000, 0, 1)).toBe(1_000_000_000)
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
    ).toBe('Still arriving on the remote -- an unknown size so far')
  })

  it('includes size, changed-when, and watched-for once all three are known', () => {
    expect(
      settleArrivingLabel({
        settle_total_bytes: 1024,
        settle_first_observed_at: '2026-08-13T12:00:00.000Z',
        settle_last_changed_at: '2026-08-13T12:00:10.000Z',
      }),
    ).toBe('Still arriving on the remote -- 1.0 KB, changed 30s ago -- watching for 40s')
  })

  it('short label falls back to an ellipsis without a byte count', () => {
    expect(settleArrivingShortLabel({ settle_total_bytes: null })).toBe('Remote…')
  })

  it('short label shows the byte count when known', () => {
    expect(settleArrivingShortLabel({ settle_total_bytes: 2048 })).toBe('Remote · 2.0 KB')
  })
})

// The removal grace period's countdown (2026-08-14, prompts/2026-08-14-removal-grace-
// countdown.md) -- DESIGN.md §3.2 rule 3 / §7.3.
describe('isRemovalGracePending', () => {
  // Every state outside REMOVAL_GRACE_ELIGIBLE_STATES that the §3.2 vocabulary has
  // (`StateChip.tsx`'s own STYLES keys, minus the eligible set) -- deliberately including
  // REMOVED_LOCAL/REMOVED_BOTH: a row already rewritten to either has *finished*, not
  // pending, and must never show a countdown even if `first_missing_at` is somehow still set.
  const INELIGIBLE_STATES = [
    'REMOTE_ONLY',
    'LOCAL_ONLY',
    'PARTIAL',
    'QUEUED',
    'DOWNLOADING',
    'STOPPED',
    'FAILED',
    'EXCLUDED',
    'REMOVED_LOCAL',
    'REMOVED_BOTH',
  ]

  it('is true for every state in REMOVAL_GRACE_ELIGIBLE_STATES once first_missing_at is set', () => {
    for (const state of REMOVAL_GRACE_ELIGIBLE_STATES) {
      expect(isRemovalGracePending({ state, first_missing_at: '2026-08-13T12:00:00.000Z' })).toBe(true)
    }
  })

  it('is false for every state outside the eligible set, even with first_missing_at set', () => {
    for (const state of INELIGIBLE_STATES) {
      expect(isRemovalGracePending({ state, first_missing_at: '2026-08-13T12:00:00.000Z' })).toBe(false)
    }
  })

  it('is false for an eligible state whose local copy never went missing', () => {
    expect(isRemovalGracePending({ state: 'VERIFIED', first_missing_at: null })).toBe(false)
  })
})

describe('removalGraceRemainingS', () => {
  const NOW = new Date('2026-08-13T12:00:00.000Z').getTime()

  it('returns seconds left within the window', () => {
    // 524s elapsed of a 600s grace window -- the live case this task closes ("76 seconds from
    // resolving").
    const firstMissingAt = new Date(NOW - 524_000).toISOString()
    expect(removalGraceRemainingS(firstMissingAt, 600, NOW)).toBe(76)
  })

  it('returns null once elapsed has reached the grace window (capped, not 0 or negative)', () => {
    const firstMissingAt = new Date(NOW - 600_000).toISOString()
    expect(removalGraceRemainingS(firstMissingAt, 600, NOW)).toBeNull()
  })

  it('returns null well past the grace window too -- the frozen-clock case', () => {
    const firstMissingAt = new Date(NOW - 6_000_000).toISOString()
    expect(removalGraceRemainingS(firstMissingAt, 600, NOW)).toBeNull()
  })

  it('returns null when first_missing_at is null', () => {
    expect(removalGraceRemainingS(null, 600, NOW)).toBeNull()
  })

  it('returns null when the grace constant has not loaded yet', () => {
    const firstMissingAt = new Date(NOW - 76_000).toISOString()
    expect(removalGraceRemainingS(firstMissingAt, null, NOW)).toBeNull()
  })

  it('returns null for an unparseable timestamp', () => {
    expect(removalGraceRemainingS('not-a-date', 600, NOW)).toBeNull()
  })

  it('returns null for a first_missing_at in the future (clock skew)', () => {
    const firstMissingAt = new Date(NOW + 5_000).toISOString()
    expect(removalGraceRemainingS(firstMissingAt, 600, NOW)).toBeNull()
  })
})

describe('removalGraceShortLabel / removalGraceLabel', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-13T12:00:00.000Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  const grace = { grace_s: 600 }

  it('short label shows the countdown when the number is trustworthy', () => {
    const firstMissingAt = new Date(Date.now() - 524_000).toISOString()
    expect(removalGraceShortLabel({ first_missing_at: firstMissingAt }, grace)).toBe('Missing · 1m')
  })

  it('short label degrades to the bare word once capped', () => {
    const firstMissingAt = new Date(Date.now() - 600_000).toISOString()
    expect(removalGraceShortLabel({ first_missing_at: firstMissingAt }, grace)).toBe('Missing')
  })

  it('short label degrades to the bare word when constants have not loaded', () => {
    const firstMissingAt = new Date(Date.now() - 524_000).toISOString()
    expect(removalGraceShortLabel({ first_missing_at: firstMissingAt }, null)).toBe('Missing')
  })

  it('full label spells out the absolute time and the countdown', () => {
    const firstMissingAt = new Date(Date.now() - 524_000).toISOString()
    const expectedSince = new Date(firstMissingAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    expect(removalGraceLabel({ first_missing_at: firstMissingAt }, grace)).toBe(
      `Local copy gone since ${expectedSince}. Treated as removed in 1m unless it comes back.`,
    )
  })

  it('full label degrades the outcome clause to "soon" once capped', () => {
    const firstMissingAt = new Date(Date.now() - 600_000).toISOString()
    const expectedSince = new Date(firstMissingAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    expect(removalGraceLabel({ first_missing_at: firstMissingAt }, grace)).toBe(
      `Local copy gone since ${expectedSince}. Treated as removed soon unless it comes back.`,
    )
  })

  it('full label handles a null first_missing_at without fabricating a time', () => {
    expect(removalGraceLabel({ first_missing_at: null }, grace)).toBe('Local copy missing.')
  })
})
