import { describe, expect, it } from 'vitest'
import {
  BANDWIDTH_COMMIT_DELAY_MS,
  bandwidthAppliedNotice,
  bandwidthBounds,
  bandwidthCommitBps,
  bandwidthCountdownSeconds,
  bandwidthLabel,
  bandwidthPendingNotice,
  bandwidthSliderValue,
  isBandwidthDirty,
} from './bandwidth'

describe('bandwidthBounds', () => {
  it('floors at 1 MB/s when no meaningful min share floor is configured', () => {
    expect(bandwidthBounds(10_000_000, 0).minMBps).toBe(1)
  })

  it('reuses min_share_floor_bps as the floor, rounded up to a whole MB/s', () => {
    // 2.5 MB/s floor -> the slider must not be able to reach 2 MB/s, which the server rejects.
    expect(bandwidthBounds(10_000_000, 2_500_000).minMBps).toBe(3)
  })

  it('never lets the floor sit below 1 MB/s even for a sub-MB floor', () => {
    expect(bandwidthBounds(10_000_000, 500_000).minMBps).toBe(1)
  })

  it('tops out at the configured ceiling, not at an invented bound', () => {
    // The whole point of the two-value model: "the max on this should never exceed the max set
    // in transfer settings."
    expect(bandwidthBounds(10_000_000, 500_000).maxMBps).toBe(10)
    expect(bandwidthBounds(400_000_000, 500_000).maxMBps).toBe(400)
  })

  it('rounds a fractional ceiling down, so no reachable notch exceeds it', () => {
    expect(bandwidthBounds(10_500_000, 500_000).maxMBps).toBe(10)
  })

  it('keeps the upper bound at or above the floor even for an absurd floor', () => {
    const bounds = bandwidthBounds(1_000_000, 900_000_000)
    expect(bounds.maxMBps).toBeGreaterThanOrEqual(bounds.minMBps)
  })

  it('collapses to a single notch before the settings response has resolved', () => {
    // The control renders disabled until then; what matters is that it is never a range
    // implying a ceiling nobody has configured.
    expect(bandwidthBounds(undefined, undefined)).toEqual({ minMBps: 1, maxMBps: 1 })
  })
})

describe('bandwidthSliderValue', () => {
  const bounds = bandwidthBounds(20_000_000, 500_000)

  it('renders the limit in force at its MB/s notch', () => {
    expect(bandwidthSliderValue(10_000_000, bounds)).toBe(10)
  })

  it('rounds a fractional value to the nearest notch', () => {
    expect(bandwidthSliderValue(10_500_000, bounds)).toBe(11)
  })

  it('clamps an out-of-range value into the track', () => {
    expect(bandwidthSliderValue(0, bounds)).toBe(bounds.minMBps)
    expect(bandwidthSliderValue(999_000_000, bounds)).toBe(bounds.maxMBps)
  })
})

describe('bandwidthCommitBps', () => {
  const bounds = bandwidthBounds(10_000_000, 500_000)

  it('converts a whole MB/s notch to bytes/sec exactly', () => {
    expect(bandwidthCommitBps(4, bounds, 10_000_000)).toBe(4_000_000)
  })

  it('sends the ceiling verbatim at the top notch, fraction included', () => {
    // A 10.5 MB/s ceiling floors to a 10 MB/s top notch; dragging to "max" must still mean the
    // ceiling, not a silent throttle to 10.
    const fractional = bandwidthBounds(10_500_000, 500_000)
    expect(bandwidthCommitBps(fractional.maxMBps, fractional, 10_500_000)).toBe(10_500_000)
  })

  it('never depends on a ceiling it does not have', () => {
    expect(bandwidthCommitBps(4, bounds, undefined)).toBe(4_000_000)
  })
})

describe('isBandwidthDirty', () => {
  const bounds = bandwidthBounds(20_000_000, 500_000)

  it('is clean when the slider matches the limit in force', () => {
    expect(isBandwidthDirty(10, 10_000_000, bounds)).toBe(false)
  })

  it('is clean when the value merely rounds to the same notch', () => {
    expect(isBandwidthDirty(11, 10_500_000, bounds)).toBe(false)
  })

  it('is dirty once the slider has moved off the notch in force', () => {
    expect(isBandwidthDirty(20, 10_000_000, bounds)).toBe(true)
  })

  it('is never dirty before the settings request has resolved', () => {
    expect(isBandwidthDirty(20, undefined, bounds)).toBe(false)
  })

  it('reads clean again after a drag back to where it started -- the cancel affordance', () => {
    expect(isBandwidthDirty(20, 10_000_000, bounds)).toBe(true)
    expect(isBandwidthDirty(10, 10_000_000, bounds)).toBe(false)
  })
})

describe('bandwidthCountdownSeconds', () => {
  it('reads the full delay the instant the timer is armed', () => {
    const now = 1_000_000
    expect(bandwidthCountdownSeconds(now + BANDWIDTH_COMMIT_DELAY_MS, now)).toBe(5)
  })

  it('counts down whole seconds', () => {
    const deadline = 1_005_000
    expect(bandwidthCountdownSeconds(deadline, 1_001_000)).toBe(4)
    expect(bandwidthCountdownSeconds(deadline, 1_004_100)).toBe(1)
  })

  it('never reads zero while there is still time on the clock', () => {
    expect(bandwidthCountdownSeconds(1_005_000, 1_004_999)).toBe(1)
  })

  it('never goes negative once the deadline has passed', () => {
    expect(bandwidthCountdownSeconds(1_005_000, 1_009_000)).toBe(0)
  })
})

describe('bandwidthLabel', () => {
  it('reads in the decimal MB/s the slider is labelled in, not binary MB', () => {
    // lib/format.ts's formatRate would call this 9.5 MB/s.
    expect(bandwidthLabel(10_000_000)).toBe('10 MB/s')
  })

  it('keeps one decimal only when the value has one', () => {
    expect(bandwidthLabel(10_500_000)).toBe('10.5 MB/s')
  })
})

describe('bandwidthPendingNotice', () => {
  it('names the value and counts down', () => {
    expect(bandwidthPendingNotice(10_000_000, 5)).toBe(
      'Bandwidth update to 10 MB/s applied in 5 seconds…',
    )
  })

  it('reads naturally at one second', () => {
    expect(bandwidthPendingNotice(10_000_000, 1)).toContain('in 1 second…')
  })
})

describe('bandwidthAppliedNotice', () => {
  const applied = (over: Partial<Parameters<typeof bandwidthAppliedNotice>[0]> = {}) => ({
    effective_bandwidth_bps: 10_000_000,
    interrupted: 0,
    skipped_because_paused: false,
    ...over,
  })

  it('says "for all new transfers" on the default, checked path', () => {
    expect(bandwidthAppliedNotice(applied(), true)).toBe(
      'Bandwidth set to 10 MB/s for all new transfers.',
    )
  })

  it('states the real restart count, never a generic phrase', () => {
    expect(bandwidthAppliedNotice(applied({ interrupted: 3 }), false)).toBe(
      'Bandwidth set to 10 MB/s — 3 running transfers restarted.',
    )
  })

  it('reads naturally for a single restarted transfer', () => {
    expect(bandwidthAppliedNotice(applied({ interrupted: 1 }), false)).toContain(
      '1 running transfer restarted',
    )
  })

  it('says nothing was restarted when the queue is paused', () => {
    // The backend deliberately skips re-admission while paused; reporting a restart that did
    // not happen would be worse than the dialog this banner replaced.
    const text = bandwidthAppliedNotice(applied({ skipped_because_paused: true }), false)
    expect(text).toContain('paused')
    expect(text).toContain('nothing was restarted')
    expect(text).not.toContain('transfers restarted')
  })

  it('never claims a restart when the paused skip coincides with a stale count', () => {
    const text = bandwidthAppliedNotice(
      applied({ interrupted: 4, skipped_because_paused: true }),
      false,
    )
    expect(text).not.toContain('4')
  })

  it('says nothing was running when there was nothing to restart', () => {
    expect(bandwidthAppliedNotice(applied(), false)).toBe(
      'Bandwidth set to 10 MB/s — nothing was running, so nothing was restarted.',
    )
  })

  it('announces the value the server applied, not the one that was asked for', () => {
    // A request above the ceiling is clamped server-side; the banner must state what is in
    // force.
    expect(bandwidthAppliedNotice(applied({ effective_bandwidth_bps: 20_000_000 }), true)).toContain(
      '20 MB/s',
    )
  })
})
