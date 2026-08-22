import { describe, expect, it } from 'vitest'
import {
  BANDWIDTH_DEFAULT_MAX_MBPS,
  applyToRunningWarning,
  bandwidthAppliedNotice,
  bandwidthBounds,
  bandwidthSliderValue,
  bandwidthToBps,
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

  it('tops out at the default gigabit-ish bound for an ordinary ceiling', () => {
    expect(bandwidthBounds(10_000_000, 500_000).maxMBps).toBe(BANDWIDTH_DEFAULT_MAX_MBPS)
  })

  it('raises the upper bound rather than clamping a larger configured ceiling', () => {
    expect(bandwidthBounds(400_000_000, 500_000).maxMBps).toBe(400)
  })

  it('keeps the upper bound at or above the floor even for an absurd floor', () => {
    const bounds = bandwidthBounds(1_000_000, 900_000_000)
    expect(bounds.maxMBps).toBeGreaterThanOrEqual(bounds.minMBps)
  })

  it('treats a missing settings response as the smallest sane range', () => {
    expect(bandwidthBounds(undefined, undefined)).toEqual({
      minMBps: 1,
      maxMBps: BANDWIDTH_DEFAULT_MAX_MBPS,
    })
  })
})

describe('bandwidthSliderValue', () => {
  const bounds = bandwidthBounds(10_000_000, 500_000)

  it('renders the stored value at its MB/s notch', () => {
    expect(bandwidthSliderValue(10_000_000, bounds)).toBe(10)
  })

  it('rounds a fractional stored value to the nearest notch', () => {
    expect(bandwidthSliderValue(10_500_000, bounds)).toBe(11)
  })

  it('clamps an out-of-range stored value into the track', () => {
    // Settings -> Transfer is unvalidated on purpose, so a stored 0 is reachable.
    expect(bandwidthSliderValue(0, bounds)).toBe(bounds.minMBps)
    expect(bandwidthSliderValue(999_000_000, bounds)).toBe(bounds.maxMBps)
  })
})

describe('bandwidthToBps', () => {
  it('converts whole MB/s to bytes/sec exactly', () => {
    expect(bandwidthToBps(12)).toBe(12_000_000)
  })
})

describe('isBandwidthDirty', () => {
  const bounds = bandwidthBounds(10_000_000, 500_000)

  it('is clean when the slider matches what the server stored', () => {
    expect(isBandwidthDirty(10, 10_000_000, bounds)).toBe(false)
  })

  it('is clean when the stored value merely rounds to the same notch', () => {
    expect(isBandwidthDirty(11, 10_500_000, bounds)).toBe(false)
  })

  it('is dirty once the slider has moved off the stored notch', () => {
    expect(isBandwidthDirty(20, 10_000_000, bounds)).toBe(true)
  })

  it('is never dirty before the settings request has resolved', () => {
    expect(isBandwidthDirty(20, undefined, bounds)).toBe(false)
  })
})

describe('applyToRunningWarning', () => {
  it('names the count and says transfers resume rather than restart', () => {
    const warning = applyToRunningWarning(3, false)
    expect(warning.title).toContain('3 running transfers')
    expect(warning.body).toContain('resume from the bytes already downloaded')
    expect(warning.confirmLabel).toContain('3')
  })

  it('reads naturally for a single running transfer', () => {
    const warning = applyToRunningWarning(1, false)
    expect(warning.title).toBe('This interrupts 1 running transfer')
    expect(warning.body).toContain('It resumes')
    expect(warning.confirmLabel).toBe('Apply and restart 1 transfer')
  })

  it('says nothing will be interrupted when nothing is running', () => {
    const warning = applyToRunningWarning(0, false)
    expect(warning.title).toBe('Nothing is running')
    expect(warning.body).toContain('nothing will be interrupted')
  })

  it('promises a paused queue will neither resume nor lose its deadline', () => {
    const warning = applyToRunningWarning(2, true)
    expect(warning.title).toBe('The queue is paused')
    expect(warning.body).toContain('will not resume the queue')
    expect(warning.body).toContain('timed pause')
  })

  it('prefers the paused wording over the running count', () => {
    // A paused queue can still have running jobs ("pause after current"), and those are
    // deliberately left alone -- so the count must not appear in the confirmation.
    expect(applyToRunningWarning(5, true).confirmLabel).toBe('Save the new limit')
  })
})

describe('bandwidthAppliedNotice', () => {
  it('reports how many transfers were re-started', () => {
    expect(bandwidthAppliedNotice({ interrupted: 2, skipped_because_paused: false })).toContain(
      '2 transfers re-started',
    )
  })

  it('reads naturally for one transfer', () => {
    expect(bandwidthAppliedNotice({ interrupted: 1, skipped_because_paused: false })).toContain(
      '1 transfer re-started',
    )
  })

  it('is plain when nothing was interrupted', () => {
    expect(bandwidthAppliedNotice({ interrupted: 0, skipped_because_paused: false })).toBe(
      'New limit saved.',
    )
  })

  it('says the pause was left alone when the server skipped for that reason', () => {
    expect(bandwidthAppliedNotice({ interrupted: 0, skipped_because_paused: true })).toContain(
      'still paused',
    )
  })
})
