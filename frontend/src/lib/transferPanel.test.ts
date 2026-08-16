import { describe, expect, it } from 'vitest'
import type { JobOut, JobState } from '../api/types'
import {
  type LiveProgress,
  hasArrGroup,
  processingGroupFields,
  transferGroupFields,
  transferLineValue,
} from './transferPanel'

// 2026-08-15 (prompts/2026-08-15-transfers-single-line-rows-with-detail.md): "one line per
// download, expandable detail" -- the row-collapse decision (what stays on the line vs. what
// moves into the panel) and the panel's own group assembly, both as pure functions. No
// component rendering here (README.md's Known gaps) -- just the logic these functions own,
// same discipline `TransfersPage.test.ts`/`transferTiming.test.ts` already follow.

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
    queued_at: '2026-08-15T00:00:00.000000Z',
    started_at: null,
    finished_at: null,
    pid: null,
    rate_limit_bps: null,
    forced_full_rate: false,
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
    ...overrides,
  }
}

describe('transferLineValue -- the row-collapse decision', () => {
  it('shows percent + live rate while running, from the live progress reading', () => {
    const live: LiveProgress = { bytes_done: 500, bytes_total: 1000, speed_bps: 1024 * 1024, eta_s: 30 }
    const value = transferLineValue(job('running', { bytes_done: 100, bytes_total: 1000 }), live)
    expect(value).toBe('50% · 1.0 MB/s')
  })

  it('falls back to the job\'s own bytes_done/speed_bps while running if no live reading has arrived yet', () => {
    const value = transferLineValue(job('running', { bytes_done: 250, bytes_total: 1000, speed_bps: 512 * 1024 }))
    expect(value).toBe('25% · 512.0 KB/s')
  })

  it('shows the final size, not percent/rate, once a job is terminal', () => {
    expect(transferLineValue(job('succeeded', { bytes_total: 1_500_000_000 }))).toBe('1.4 GB')
    expect(transferLineValue(job('failed', { bytes_total: 2000 }))).toBe('2.0 KB')
    expect(transferLineValue(job('cancelled', { bytes_total: 1000 }))).toBe('1000 B')
  })

  it('shows the known size for a still-queued job, or a dash when even that is unknown', () => {
    expect(transferLineValue(job('queued', { bytes_total: 5000 }))).toBe('4.9 KB')
    expect(transferLineValue(job('queued', { bytes_total: null }))).toBe('—')
    expect(transferLineValue(job('failed', { bytes_total: null }))).toBe('—')
  })
})

describe('transferGroupFields -- the panel\'s Transfer group assembly', () => {
  it('always includes Bytes and Files, even for a bare queued job', () => {
    const fields = transferGroupFields(job('queued', { bytes_total: 1000 }), { fileCount: 3 })
    const labels = fields.map((f) => f.label)
    expect(labels).toContain('Bytes')
    expect(labels).toContain('Files')
    expect(fields.find((f) => f.label === 'Files')?.value).toBe('3 files')
  })

  it('singularizes a one-file item', () => {
    const fields = transferGroupFields(job('queued'), { fileCount: 1 })
    expect(fields.find((f) => f.label === 'Files')?.value).toBe('1 file')
  })

  it('adds Elapsed and Average speed once a job has started', () => {
    const started = job('succeeded', {
      started_at: '2026-08-15T00:00:00.000000Z',
      finished_at: '2026-08-15T00:01:00.000000Z',
      bytes_start: 0,
      bytes_done: 6_000_000,
      bytes_total: 6_000_000,
    })
    const fields = transferGroupFields(started, { fileCount: 1 })
    const labels = fields.map((f) => f.label)
    expect(labels).toContain('Elapsed')
    expect(labels).toContain('Average speed')
    expect(fields.find((f) => f.label === 'Elapsed')?.value).toBe('1m')
  })

  it('adds Current speed only while running, never for a terminal job', () => {
    const live: LiveProgress = { bytes_done: 500, bytes_total: 1000, speed_bps: 2048, eta_s: 10 }
    const running = transferGroupFields(job('running', { bytes_done: 100, bytes_total: 1000 }), { live, fileCount: 1 })
    expect(running.map((f) => f.label)).toContain('Current speed')

    const succeeded = transferGroupFields(job('succeeded', { bytes_total: 1000 }), { fileCount: 1 })
    expect(succeeded.map((f) => f.label)).not.toContain('Current speed')
  })

  it('adds Allocated only when the job carries a rate_limit_bps', () => {
    const withAlloc = transferGroupFields(job('running', { rate_limit_bps: 1024 }), { fileCount: 1 })
    expect(withAlloc.map((f) => f.label)).toContain('Allocated')

    const withoutAlloc = transferGroupFields(job('queued', { rate_limit_bps: null }), { fileCount: 1 })
    expect(withoutAlloc.map((f) => f.label)).not.toContain('Allocated')
  })

  it('adds Queued wait only when it is notable (>= 5s), matching isNotableQueuedWait', () => {
    const notable = transferGroupFields(
      job('running', { queued_at: '2026-08-15T00:00:00.000000Z', started_at: '2026-08-15T00:00:10.000000Z' }),
      { fileCount: 1 },
    )
    expect(notable.map((f) => f.label)).toContain('Queued wait')

    const trivial = transferGroupFields(
      job('running', { queued_at: '2026-08-15T00:00:00.000000Z', started_at: '2026-08-15T00:00:01.000000Z' }),
      { fileCount: 1 },
    )
    expect(trivial.map((f) => f.label)).not.toContain('Queued wait')
  })
})

describe('processingGroupFields -- the panel\'s Processing group assembly', () => {
  it('is empty for an item with no processing milestones yet', () => {
    expect(processingGroupFields(job('running'))).toEqual([])
  })

  it('includes only the milestones that have actually happened', () => {
    const fields = processingGroupFields(job('succeeded', { verified_at: '2026-08-15T00:00:00.000000Z' }))
    expect(fields.map((f) => f.label)).toEqual(['Verified'])
  })

  it('includes verified/extracted/remote-deleted, in that order, once all three have happened', () => {
    const fields = processingGroupFields(
      job('succeeded', {
        verified_at: '2026-08-15T00:00:00.000000Z',
        extracted_at: '2026-08-15T00:05:00.000000Z',
        remote_deleted_at: '2026-08-15T00:06:00.000000Z',
      }),
    )
    expect(fields.map((f) => f.label)).toEqual(['Verified', 'Extracted', 'Remote deleted'])
  })
})

describe('hasArrGroup -- the *arr group\'s hidden/shown logic', () => {
  it('is hidden when the job\'s queue has no bound *arr instance', () => {
    expect(hasArrGroup(job('succeeded', { arr_instance_name: null }))).toBe(false)
  })

  it('is hidden even when arr_status somehow carries a value without an instance name -- the instance binding is the gate, not the status', () => {
    expect(hasArrGroup(job('succeeded', { arr_instance_name: null, arr_status: 'imported' }))).toBe(false)
  })

  it('shows once the queue has a bound instance, even before arr_status has been set', () => {
    expect(hasArrGroup(job('succeeded', { arr_instance_name: 'Sonarr', arr_status: null }))).toBe(true)
  })

  it('shows with a bound instance and a real arr_status', () => {
    expect(hasArrGroup(job('succeeded', { arr_instance_name: 'Sonarr', arr_status: 'imported' }))).toBe(true)
  })
})
