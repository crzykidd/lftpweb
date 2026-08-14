import { describe, expect, it } from 'vitest'
import { averageSpeedBps, elapsedSeconds, isNotableQueuedWait, postprocessNote, queuedWaitSeconds } from './transferTiming'

describe('elapsedSeconds', () => {
  it('is null for a job that has not started -- nothing to measure elapsed of yet', () => {
    expect(elapsedSeconds(null, null)).toBeNull()
    expect(elapsedSeconds(null, '2026-08-14T05:19:16.872Z')).toBeNull()
  })

  it('measures against "now" for a still-running job (finished_at null)', () => {
    const started = '2026-08-14T05:19:00.000Z'
    const now = new Date('2026-08-14T05:19:49.000Z').getTime()
    expect(elapsedSeconds(started, null, now)).toBe(49)
  })

  it('measures started_at to finished_at for a terminal job, ignoring "now"', () => {
    const started = '2026-08-14T05:19:16.872Z'
    const finished = '2026-08-14T05:19:24.563Z'
    const elapsed = elapsedSeconds(started, finished, Date.now())
    expect(elapsed).toBeCloseTo(7.691, 2)
  })

  it('formats a multi-hour transfer correctly -- no overflow, no truncation', () => {
    const started = '2026-08-14T00:00:00.000Z'
    const finished = '2026-08-14T03:15:30.000Z'
    expect(elapsedSeconds(started, finished)).toBe(3 * 3600 + 15 * 60 + 30)
  })

  it('clamps a zero or negative span to zero rather than going negative', () => {
    const t = '2026-08-14T05:19:16.872Z'
    expect(elapsedSeconds(t, t)).toBe(0)
    // finished_at before started_at (clock skew / same-second race) -- never a negative duration
    expect(elapsedSeconds('2026-08-14T05:19:16.872Z', '2026-08-14T05:19:10.000Z')).toBe(0)
  })
})

describe('queuedWaitSeconds', () => {
  it('is null while the job is still queued -- no started_at to measure against', () => {
    expect(queuedWaitSeconds('2026-08-14T05:00:00.000Z', null)).toBeNull()
  })

  it('measures queued_at to started_at once the job has started', () => {
    expect(queuedWaitSeconds('2026-08-14T05:00:00.000Z', '2026-08-14T05:00:12.000Z')).toBe(12)
  })

  it('clamps a nonsensical negative wait to zero', () => {
    expect(queuedWaitSeconds('2026-08-14T05:00:12.000Z', '2026-08-14T05:00:00.000Z')).toBe(0)
  })
})

describe('isNotableQueuedWait', () => {
  it('is never notable when there is nothing to show', () => {
    expect(isNotableQueuedWait(null)).toBe(false)
  })

  it('is not notable for a trivial wait', () => {
    expect(isNotableQueuedWait(0)).toBe(false)
    expect(isNotableQueuedWait(4.9)).toBe(false)
  })

  it('is notable once the wait crosses the threshold', () => {
    expect(isNotableQueuedWait(5)).toBe(true)
    expect(isNotableQueuedWait(120)).toBe(true)
  })
})

describe('averageSpeedBps', () => {
  it('is null when elapsed is unknown (job never started)', () => {
    expect(averageSpeedBps(1000, 0, null)).toBeNull()
  })

  it('is null for a zero-second span -- guards divide-by-zero', () => {
    expect(averageSpeedBps(1000, 0, 0)).toBeNull()
  })

  it('is null for a sub-second span -- guards an absurd extrapolated rate', () => {
    expect(averageSpeedBps(5_000_000, 0, 0.4)).toBeNull()
  })

  it('computes bytes moved by this attempt (bytes_done - bytes_start) over elapsed time', () => {
    // A real measured example from the task's own brief: 1.7 GB in 49s is ~34 MB/s.
    const bytesTotal = 1_700_000_000
    const avg = averageSpeedBps(bytesTotal, 0, 49)
    expect(avg).toBeCloseTo(bytesTotal / 49, 5)
  })

  it('subtracts bytes_start so a resumed attempt is not credited with bytes an earlier attempt already moved', () => {
    // Attempt landed on disk with 400 MB already present (bytes_start), finished at 1000 MB
    // (bytes_done) after 60s -- this attempt only moved 600 MB, not 1000 MB.
    const bytesStart = 400_000_000
    const bytesDone = 1_000_000_000
    const avg = averageSpeedBps(bytesDone, bytesStart, 60)
    expect(avg).toBeCloseTo(600_000_000 / 60, 5)
  })

  it('never goes negative even if bytes_start somehow exceeds bytes_done', () => {
    expect(averageSpeedBps(100, 500, 10)).toBe(0)
  })

  it('is a different figure from a live instantaneous speed_bps reading for the same job', () => {
    // The task's own bar: average and instantaneous are not the same number and must never be
    // silently substituted for one another. A steady live reading and a lower lifetime average
    // (e.g. a slow ramp-up at the start) are both legitimate and simultaneously true.
    const liveSpeedBps = 40_000_000
    const avg = averageSpeedBps(1_700_000_000, 0, 49)
    expect(avg).not.toBeCloseTo(liveSpeedBps, -3)
  })
})

describe('postprocessNote', () => {
  it('is null for any job that has not succeeded -- only a succeeded job can be mid-postprocess', () => {
    expect(postprocessNote('queued', 'VERIFYING')).toBeNull()
    expect(postprocessNote('running', 'VERIFYING')).toBeNull()
    expect(postprocessNote('failed', 'VERIFYING')).toBeNull()
    expect(postprocessNote('cancelled', 'EXTRACTING')).toBeNull()
  })

  it('is null for a succeeded job whose item has already settled into a terminal state', () => {
    expect(postprocessNote('succeeded', 'DOWNLOADED')).toBeNull()
    expect(postprocessNote('succeeded', 'VERIFIED')).toBeNull()
    expect(postprocessNote('succeeded', null)).toBeNull()
    expect(postprocessNote('succeeded', undefined)).toBeNull()
  })

  it('labels a succeeded job whose item is still verifying or extracting', () => {
    expect(postprocessNote('succeeded', 'VERIFYING')).toBe('Verifying…')
    expect(postprocessNote('succeeded', 'EXTRACTING')).toBe('Extracting…')
  })
})
