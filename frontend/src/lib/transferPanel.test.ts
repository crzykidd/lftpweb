import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { JobOut, JobState } from '../api/types'
import {
  type LiveProgress,
  completedTimeLabel,
  formatQueueGroupCounts,
  groupJobsByQueue,
  hasArrGroup,
  isQueueCollapsed,
  processingGroupFields,
  queueGroupSummary,
  readCollapsedQueues,
  sortTransferRows,
  transferGroupFields,
  transferLineValue,
  withQueueCollapsed,
  writeCollapsedQueues,
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

describe('completedTimeLabel -- the collapsed line\'s completed-time reading', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-15T00:03:00.000Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('is null for an active job (queued or running), even with a finished_at on the row', () => {
    expect(completedTimeLabel(job('queued', { finished_at: '2026-08-15T00:00:00.000Z' }))).toBeNull()
    expect(completedTimeLabel(job('running', { finished_at: '2026-08-15T00:00:00.000Z' }))).toBeNull()
  })

  it('is null for a terminal job with no finished_at yet', () => {
    expect(completedTimeLabel(job('succeeded', { finished_at: null }))).toBeNull()
  })

  it('renders a relative value and an exact-timestamp title for a terminal job', () => {
    const finishedAt = '2026-08-15T00:00:00.000Z'
    for (const state of ['succeeded', 'failed', 'cancelled'] as const) {
      const label = completedTimeLabel(job(state, { finished_at: finishedAt }))
      expect(label).toEqual({ value: '3m ago', title: new Date(finishedAt).toLocaleString() })
    }
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

  it('adds Completed for a terminal job with finished_at, matching completedTimeLabel', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-15T00:03:00.000Z'))
    try {
      const finishedAt = '2026-08-15T00:00:00.000Z'
      const fields = transferGroupFields(job('succeeded', { finished_at: finishedAt }), { fileCount: 1 })
      expect(fields.find((f) => f.label === 'Completed')).toEqual({
        label: 'Completed',
        value: '3m ago',
        title: new Date(finishedAt).toLocaleString(),
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it('omits Completed for an active job (queued or running)', () => {
    const queued = transferGroupFields(job('queued'), { fileCount: 1 })
    expect(queued.map((f) => f.label)).not.toContain('Completed')

    const running = transferGroupFields(job('running', { finished_at: null }), { fileCount: 1 })
    expect(running.map((f) => f.label)).not.toContain('Completed')
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

describe('sortTransferRows -- the Transfers page\'s row order', () => {
  it('keeps every active row (running, then queued) ahead of every terminal row', () => {
    const running = job('running', { id: 1 })
    const queued = job('queued', { id: 2 })
    const succeeded = job('succeeded', { id: 3, finished_at: '2026-08-15T00:00:00.000000Z' })
    const failed = job('failed', { id: 4, finished_at: '2026-08-15T00:05:00.000000Z' })

    const sorted = sortTransferRows([succeeded, queued, failed, running])
    expect(sorted.map((j) => j.id)).toEqual([1, 2, 4, 3])
  })

  it('preserves the input\'s own relative order within running and within queued (scheduler order untouched)', () => {
    const runningA = job('running', { id: 1 })
    const runningB = job('running', { id: 2 })
    const queuedA = job('queued', { id: 3 })
    const queuedB = job('queued', { id: 4 })

    const sorted = sortTransferRows([queuedB, runningB, queuedA, runningA])
    expect(sorted.map((j) => j.id)).toEqual([2, 1, 4, 3])
  })

  it('sorts terminal rows newest-completed-first', () => {
    const oldest = job('succeeded', { id: 1, finished_at: '2026-08-15T00:00:00.000000Z' })
    const newest = job('failed', { id: 2, finished_at: '2026-08-15T00:10:00.000000Z' })
    const middle = job('cancelled', { id: 3, finished_at: '2026-08-15T00:05:00.000000Z' })

    const sorted = sortTransferRows([oldest, newest, middle])
    expect(sorted.map((j) => j.id)).toEqual([2, 3, 1])
  })

  it('sorts a terminal row with no finished_at last, stable relative to other missing ones', () => {
    const dated = job('succeeded', { id: 1, finished_at: '2026-08-15T00:00:00.000000Z' })
    const missingA = job('failed', { id: 2, finished_at: null })
    const missingB = job('cancelled', { id: 3, finished_at: null })

    const sorted = sortTransferRows([missingA, dated, missingB])
    expect(sorted.map((j) => j.id)).toEqual([1, 2, 3])
  })

  it('is stable for terminal rows sharing the same finished_at', () => {
    const a = job('succeeded', { id: 1, finished_at: '2026-08-15T00:00:00.000000Z' })
    const b = job('failed', { id: 2, finished_at: '2026-08-15T00:00:00.000000Z' })
    const c = job('cancelled', { id: 3, finished_at: '2026-08-15T00:00:00.000000Z' })

    expect(sortTransferRows([a, b, c]).map((j) => j.id)).toEqual([1, 2, 3])
  })

  it('does not mutate the input array', () => {
    const running = job('running', { id: 1 })
    const succeeded = job('succeeded', { id: 2, finished_at: '2026-08-15T00:00:00.000000Z' })
    const input = [succeeded, running]
    sortTransferRows(input)
    expect(input.map((j) => j.id)).toEqual([2, 1])
  })
})

// 2026-08-16 (prompts/2026-08-16-transfers-group-by-queue.md): "group rows by queue,
// collapsible with remembered state" -- grouping, header aggregates, and collapse persistence,
// all as pure functions per the task's own instruction.

describe('groupJobsByQueue', () => {
  it('groups jobs by queue id, one group per queue with at least one visible job', () => {
    const a1 = job('running', { id: 1, queue_id: 1, queue_name: 'Alpha' })
    const b1 = job('queued', { id: 2, queue_id: 2, queue_name: 'Bravo' })
    const a2 = job('failed', { id: 3, queue_id: 1, queue_name: 'Alpha' })

    const groups = groupJobsByQueue([a1, b1, a2])
    expect(groups).toHaveLength(2)
    expect(groups.find((g) => g.queueId === 1)?.jobs.map((j) => j.id)).toEqual([1, 3])
    expect(groups.find((g) => g.queueId === 2)?.jobs.map((j) => j.id)).toEqual([2])
  })

  it('orders groups by queue name, not by first-seen order', () => {
    const zulu = job('running', { id: 1, queue_id: 9, queue_name: 'Zulu' })
    const alpha = job('running', { id: 2, queue_id: 1, queue_name: 'Alpha' })

    const groups = groupJobsByQueue([zulu, alpha])
    expect(groups.map((g) => g.queueName)).toEqual(['Alpha', 'Zulu'])
  })

  it('keeps each group\'s within-group order exactly as passed in', () => {
    const running = job('running', { id: 1, queue_id: 1, queue_name: 'Alpha' })
    const queued = job('queued', { id: 2, queue_id: 1, queue_name: 'Alpha' })
    const terminal = job('succeeded', { id: 3, queue_id: 1, queue_name: 'Alpha' })

    // Callers always pass `sortTransferRows`'s own output -- passing an already-decided order
    // here and asserting it survives untouched is the contract this function must not break.
    const groups = groupJobsByQueue([running, queued, terminal])
    expect(groups[0].jobs.map((j) => j.id)).toEqual([1, 2, 3])
  })

  it('returns no groups for an empty job list', () => {
    expect(groupJobsByQueue([])).toEqual([])
  })
})

describe('queueGroupSummary', () => {
  it('counts each state into its outcome bucket, including cancelled as stopped', () => {
    const jobs = [
      job('running', { id: 1 }),
      job('queued', { id: 2 }),
      job('queued', { id: 3 }),
      job('succeeded', { id: 4 }),
      job('failed', { id: 5 }),
      job('cancelled', { id: 6 }),
    ]
    const summary = queueGroupSummary(jobs, {})
    expect(summary.counts).toEqual({ active: 1, queued: 2, succeeded: 1, failed: 1, stopped: 1 })
  })

  it('sums bytes_done across the group, preferring the live reading for a running job', () => {
    const running = job('running', { id: 1, bytes_done: 100 })
    const queued = job('queued', { id: 2, bytes_done: 50 })
    const live: Record<number, LiveProgress> = {
      1: { bytes_done: 400, bytes_total: 1000, speed_bps: 0, eta_s: null },
    }
    const summary = queueGroupSummary([running, queued], live)
    expect(summary.totalBytesDone).toBe(450) // 400 (live) + 50 (queued's own bytes_done)
  })

  it('falls back to the job\'s own bytes_done when no live sample has arrived for it yet', () => {
    const running = job('running', { id: 1, bytes_done: 100 })
    const summary = queueGroupSummary([running], {})
    expect(summary.totalBytesDone).toBe(100)
  })

  it('combines the current rate only across running jobs, summing live over polled speed_bps', () => {
    const runningWithLive = job('running', { id: 1, speed_bps: 999 })
    const runningNoLive = job('running', { id: 2, speed_bps: 2048 })
    const queued = job('queued', { id: 3, speed_bps: null })
    const live: Record<number, LiveProgress> = {
      1: { bytes_done: 0, bytes_total: null, speed_bps: 1024, eta_s: null },
    }
    const summary = queueGroupSummary([runningWithLive, runningNoLive, queued], live)
    expect(summary.combinedRateBps).toBe(1024 + 2048)
  })

  it('reports no combined rate when nothing in the group is running', () => {
    const queued = job('queued', { id: 1 })
    const succeeded = job('succeeded', { id: 2 })
    const summary = queueGroupSummary([queued, succeeded], {})
    expect(summary.combinedRateBps).toBeNull()
  })
})

describe('formatQueueGroupCounts', () => {
  it('lists every non-zero bucket in a fixed order', () => {
    const text = formatQueueGroupCounts({ active: 1, queued: 2, succeeded: 3, failed: 0, stopped: 1 })
    expect(text).toBe('1 active, 2 queued, 3 succeeded, 1 stopped')
  })

  it('omits every zero-count bucket -- "to keep it quiet"', () => {
    const text = formatQueueGroupCounts({ active: 0, queued: 0, succeeded: 5, failed: 0, stopped: 0 })
    expect(text).toBe('5 succeeded')
  })

  it('renders an empty string when every bucket is zero', () => {
    expect(formatQueueGroupCounts({ active: 0, queued: 0, succeeded: 0, failed: 0, stopped: 0 })).toBe('')
  })
})

describe('per-queue collapse persistence', () => {
  afterEach(() => {
    localStorage.clear()
  })

  it('reads a queue with no stored preference as expanded (not collapsed) by default', () => {
    expect(isQueueCollapsed({}, 42)).toBe(false)
  })

  it('withQueueCollapsed(..., true) marks a queue collapsed; (..., false) clears it back out', () => {
    const collapsed = withQueueCollapsed({}, 1, true)
    expect(isQueueCollapsed(collapsed, 1)).toBe(true)

    const expanded = withQueueCollapsed(collapsed, 1, false)
    expect(isQueueCollapsed(expanded, 1)).toBe(false)
    // Expanded is the default -- clearing back to it drops the entry rather than storing `false`.
    expect(expanded).toEqual({})
  })

  it('round-trips a collapse map through the actual localStorage read/write helpers', () => {
    expect(readCollapsedQueues()).toEqual({})
    const map = withQueueCollapsed({}, 7, true)
    writeCollapsedQueues(map)
    expect(readCollapsedQueues()).toEqual({ '7': true })
    expect(isQueueCollapsed(readCollapsedQueues(), 7)).toBe(true)
  })

  it('a queue that disappears from the payload keeps its stored preference for when it returns', () => {
    // Nothing about `withQueueCollapsed`/`isQueueCollapsed` reads the current job list -- the
    // map is only ever consulted by queue id, so a queue with zero visible jobs right now still
    // reads its previously-stored preference exactly like one that's still present.
    writeCollapsedQueues(withQueueCollapsed({}, 3, true))
    const storedLater = readCollapsedQueues()
    expect(isQueueCollapsed(storedLater, 3)).toBe(true)
  })
})
