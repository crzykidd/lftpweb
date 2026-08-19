import { describe, expect, it } from 'vitest'
import type { JobOut, JobState } from '../api/types'
import { chipStateFor, isDismissable } from './TransfersPage'

// 2026-08-14 (prompts/2026-08-14-exit-zero-is-not-completion.md): `list_jobs()` now surfaces a
// recently-succeeded job on this page instead of it vanishing the instant it's reaped -- these
// two pure functions are what decide how such a row renders (its state chip) and whether it
// gets a Dismiss button. No component rendering here (README.md's Known gaps) -- just the pure
// logic these two functions own.

function job(state: JobState, overrides: Partial<JobOut> = {}): JobOut {
  return {
    id: 1,
    item_id: 1,
    queue_id: 1,
    queue_name: 'test',
    rel_path: 'Release',
    is_dir: true,
    kind: 'mirror',
    state,
    lane: 'main',
    rank: 0,
    attempt: 1,
    queued_at: '2026-08-14T00:00:00.000000Z',
    started_at: null,
    finished_at: null,
    pid: null,
    rate_limit_bps: null,
    forced_rate_fraction: null,
    bytes_start: 0,
    bytes_done: 0,
    bytes_total: null,
    speed_bps: null,
    eta_s: null,
    exit_code: null,
    error_class: null,
    output_tail: null,
    verified_at: null,
    extracted_at: null,
    remote_deleted_at: null,
    arr_status: null,
    arr_status_at: null,
    arr_instance_name: null,
    arr_instance_kind: null,
    ...overrides,
  }
}

describe('chipStateFor', () => {
  it('maps a succeeded job to the DOWNLOADED chip -- the same vocabulary a scan-derived DOWNLOADED item already uses', () => {
    expect(chipStateFor(job('succeeded'))).toBe('DOWNLOADED')
  })

  it('still maps every other job state the same way it always did', () => {
    expect(chipStateFor(job('queued'))).toBe('QUEUED')
    expect(chipStateFor(job('running'))).toBe('DOWNLOADING')
    expect(chipStateFor(job('cancelled'))).toBe('STOPPED')
    expect(chipStateFor(job('failed'))).toBe('FAILED')
  })
})

describe('isDismissable', () => {
  it('allows dismissing a succeeded job -- matches core/queue.py.dismiss_job\'s guard, extended 2026-08-14', () => {
    expect(isDismissable('succeeded')).toBe(true)
  })

  it('still allows the two states that could always be dismissed', () => {
    expect(isDismissable('failed')).toBe(true)
    expect(isDismissable('cancelled')).toBe(true)
  })

  it('never allows dismissing an active job -- a click here would just 409 against the backend guard', () => {
    expect(isDismissable('queued')).toBe(false)
    expect(isDismissable('running')).toBe(false)
  })
})
