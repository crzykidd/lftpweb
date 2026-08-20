import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FileNode, JobOut, JobState } from '../api/types'
import type { ChildSpeedSample } from '../hooks/useLiveModel'
import {
  type LiveProgress,
  DISMISS_OUTCOMES,
  DISMISS_OUTCOME_LABELS,
  FAST_LANE_HINT,
  canDismiss,
  canMoveDown,
  canMoveUp,
  canResolveManually,
  childDisplayName,
  completedTimeLabel,
  dismissMenuOptions,
  fileListCapNote,
  filterTransferJobs,
  hasArrGroup,
  isDismissable,
  isFastLane,
  isPipelineInFlight,
  manualOutcomeLabel,
  mergeFileListChildren,
  processingGroupFields,
  resolveMenuOptions,
  showsFileList,
  sortTransferRows,
  transferFilterSummary,
  transferGroupFields,
  transferLineValue,
  transferredSummary,
  waitingReasonLabel,
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
    queue_short_name: null,
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
    forced_rate_fraction: null,
    bytes_start: 0,
    bytes_done: 0,
    bytes_total: null,
    speed_bps: null,
    eta_s: null,
    exit_code: null,
    error_class: null,
    output_tail: null,
    has_output_tail: false,
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

describe('transferLineValue -- the row-collapse decision', () => {
  it('shows percent + live rate + ETA while running, from the live progress reading', () => {
    const live: LiveProgress = { bytes_done: 500, bytes_total: 1000, speed_bps: 1024 * 1024, eta_s: 30 }
    const value = transferLineValue(job('running', { bytes_done: 100, bytes_total: 1000 }), live)
    expect(value).toBe('50% · 1.0 MB/s · 30s left')
  })

  it('falls back to the job\'s own bytes_done/speed_bps/eta_s while running if no live reading has arrived yet', () => {
    const value = transferLineValue(
      job('running', { bytes_done: 250, bytes_total: 1000, speed_bps: 512 * 1024, eta_s: 90 }),
    )
    expect(value).toBe('25% · 512.0 KB/s · 1m left')
  })

  // 2026-08-19 (prompts/2026-08-19-transfers-row-shows-eta.md): "how long until this finishes"
  // surfaced on the collapsed line, one level up from the expand panel's existing ETA.
  it('omits the ETA figure entirely (no placeholder) while running with no ETA known yet', () => {
    const value = transferLineValue(job('running', { bytes_done: 100, bytes_total: 1000, eta_s: null }))
    expect(value).toBe('10% · 0 B/s')
  })

  it("prefers the live sample's eta_s over the job's own while running, same as bytes_done/speed_bps", () => {
    const live: LiveProgress = { bytes_done: 500, bytes_total: 1000, speed_bps: 1024 * 1024, eta_s: 45 }
    const value = transferLineValue(job('running', { bytes_done: 500, bytes_total: 1000, eta_s: 999 }), live)
    expect(value).toBe('50% · 1.0 MB/s · 45s left')
  })

  it('shows the final size, not percent/rate/ETA, once a job is terminal', () => {
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

describe('transferredSummary -- the terminal-job "X in Y (Z avg)" reading', () => {
  it('composes bytes/elapsed/average speed into one sentence in the normal case', () => {
    expect(transferredSummary(1024 * 1024, 1, 1024 * 1024)).toBe('1.0 MB in 1s (1.0 MB/s avg)')
  })

  it('omits the average-speed clause, without dividing by zero, once elapsed is under the rate guard', () => {
    expect(transferredSummary(500, 0, null)).toBe('500 B in 0s')
  })

  it('is null when there is no elapsed time to report at all (the job never started)', () => {
    expect(transferredSummary(0, null, null)).toBeNull()
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

  it('adds Elapsed and Average speed, never Transferred, for a running job that has started', () => {
    const running = job('running', {
      started_at: '2026-08-15T00:00:00.000000Z',
      bytes_start: 0,
      bytes_done: 6_000_000,
      bytes_total: 6_000_000,
    })
    const fields = transferGroupFields(running, { fileCount: 1, nowMs: new Date('2026-08-15T00:01:00.000000Z').getTime() })
    const labels = fields.map((f) => f.label)
    expect(labels).toContain('Elapsed')
    expect(labels).toContain('Average speed')
    expect(labels).not.toContain('Transferred')
    expect(fields.find((f) => f.label === 'Elapsed')?.value).toBe('1m')
  })

  it('collapses Elapsed and Average speed into one Transferred field for a terminal job', () => {
    const started = job('succeeded', {
      started_at: '2026-08-15T00:00:00.000000Z',
      finished_at: '2026-08-15T00:00:01.000000Z',
      bytes_start: 0,
      bytes_done: 1024 * 1024,
      bytes_total: 1024 * 1024,
    })
    const fields = transferGroupFields(started, { fileCount: 1 })
    const labels = fields.map((f) => f.label)
    expect(labels).toContain('Transferred')
    expect(labels).not.toContain('Elapsed')
    expect(labels).not.toContain('Average speed')
    expect(fields.find((f) => f.label === 'Transferred')?.value).toBe('1.0 MB in 1s (1.0 MB/s avg)')
  })

  it('the Transferred field still composes correctly when bytes_total is missing entirely', () => {
    const started = job('failed', {
      started_at: '2026-08-15T00:00:00.000000Z',
      finished_at: '2026-08-15T00:00:01.000000Z',
      bytes_start: 0,
      bytes_done: 1024 * 1024,
      bytes_total: null,
    })
    const fields = transferGroupFields(started, { fileCount: 1 })
    expect(fields.find((f) => f.label === 'Transferred')?.value).toBe('1.0 MB in 1s (1.0 MB/s avg)')
    expect(fields.find((f) => f.label === 'Bytes')?.value).toBe('1.0 MB / ? (—)')
  })

  it('the Transferred field omits the average-speed clause under the zero-elapsed guard, never divides by zero', () => {
    const instant = job('cancelled', {
      started_at: '2026-08-15T00:00:00.000000Z',
      finished_at: '2026-08-15T00:00:00.000000Z',
      bytes_start: 0,
      bytes_done: 500,
      bytes_total: 500,
    })
    const fields = transferGroupFields(instant, { fileCount: 1 })
    expect(fields.find((f) => f.label === 'Transferred')?.value).toBe('500 B in 0s')
    expect(fields.map((f) => f.label)).not.toContain('Average speed')
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

// 2026-08-17 (prompts/2026-08-17-transfers-dismiss-per-queue.md): `isDismissable` moved here
// from `TransfersPage.tsx` -- see that function's own docstring. `TransfersPage.test.ts`'s
// pre-existing `isDismissable` coverage keeps passing unchanged (the page re-exports the same
// function), so this only adds the states this task's move didn't already have direct coverage
// for.
describe('isDismissable', () => {
  it('is true for a terminal state -- failed, cancelled, succeeded', () => {
    expect(isDismissable('failed')).toBe(true)
    expect(isDismissable('cancelled')).toBe(true)
    expect(isDismissable('succeeded')).toBe(true)
  })

  it('is false for an active state -- queued, running', () => {
    expect(isDismissable('queued')).toBe(false)
    expect(isDismissable('running')).toBe(false)
  })
})

// 2026-08-20 (follow-up to phase 1 stage 4b from the user's browser review,
// prompts/2026-08-20-transfers-dismiss-menu-and-counts.md): the Complete box's "Dismiss" menu --
// "all, downloaded, failed (or whatever the completed status are)", the user's own words.
describe('DISMISS_OUTCOMES -- the menu\'s own state vocabulary', () => {
  it('is exactly the three states isDismissable allows, nothing more and nothing less', () => {
    for (const state of DISMISS_OUTCOMES) {
      expect(isDismissable(state)).toBe(true)
    }
    for (const state of ['queued', 'running'] as const) {
      expect(DISMISS_OUTCOMES).not.toContain(state)
    }
  })
})

describe('dismissMenuOptions -- the Dismiss menu\'s option list', () => {
  it('lists "All" first, then one option per dismissable outcome', () => {
    const options = dismissMenuOptions()
    expect(options[0]).toEqual({ outcome: null, label: 'All' })
    expect(options.slice(1).map((o) => o.outcome)).toEqual([...DISMISS_OUTCOMES])
  })

  it('labels each outcome the same way the row\'s own state chip does', () => {
    const options = dismissMenuOptions()
    expect(options.find((o) => o.outcome === 'succeeded')?.label).toBe('Downloaded')
    expect(options.find((o) => o.outcome === 'failed')?.label).toBe('Failed')
    expect(options.find((o) => o.outcome === 'cancelled')?.label).toBe('Stopped')
  })

  it('has a label for every outcome it lists, drawn from DISMISS_OUTCOME_LABELS', () => {
    for (const option of dismissMenuOptions()) {
      if (option.outcome != null) {
        expect(option.label).toBe(DISMISS_OUTCOME_LABELS[option.outcome])
      }
    }
  })
})

// 2026-08-19 (docs/transfers-redesign-spec.md §3.5, phase 1 stage 4a): the fast-lane marker's
// own predicate -- a single ordered list intermixes lanes, so a row needs to say why it can start
// before a lower-numbered one.
describe('isFastLane', () => {
  it('is true for a job admitted from the small lane', () => {
    expect(isFastLane(job('queued', { lane: 'small' }))).toBe(true)
  })

  it('is false for a main-lane job', () => {
    expect(isFastLane(job('queued', { lane: 'main' }))).toBe(false)
  })

  it('does not depend on job state -- a running or terminal fast-lane job is still fast-lane', () => {
    expect(isFastLane(job('running', { lane: 'small' }))).toBe(true)
    expect(isFastLane(job('succeeded', { lane: 'small' }))).toBe(true)
  })
})

describe('FAST_LANE_HINT', () => {
  it('explains why, not just that -- names both the size threshold and the ordering consequence', () => {
    expect(FAST_LANE_HINT).toMatch(/small/i)
    expect(FAST_LANE_HINT).toMatch(/start before/i)
  })
})

// 2026-08-19 (prompts/2026-08-19-queue-reorder-chevrons.md): the chevron reorder controls' own
// enabled/disabled logic -- "can this row move" pulled out of `TransfersPage.tsx`'s `Row` so it's
// unit-testable without mounting anything (README.md's Known gaps: no component rendering).
describe('canMoveUp -- the ▲/▲▲ chevrons\' enabled state', () => {
  it('is false for the first queued row -- nothing above it to trade places with', () => {
    expect(canMoveUp(1)).toBe(false)
  })

  it('is true for any row past the first', () => {
    expect(canMoveUp(2)).toBe(true)
    expect(canMoveUp(9)).toBe(true)
  })

  it('is false for a non-queued row (no position at all)', () => {
    expect(canMoveUp(undefined)).toBe(false)
  })
})

describe('canMoveDown -- the ▼ chevron\'s enabled state', () => {
  it('is false for the last queued row -- nothing below it to trade places with', () => {
    expect(canMoveDown(3, 3)).toBe(false)
  })

  it('is true for any row before the last', () => {
    expect(canMoveDown(1, 3)).toBe(true)
    expect(canMoveDown(2, 3)).toBe(true)
  })

  it('is false for a non-queued row (no position at all)', () => {
    expect(canMoveDown(undefined, 3)).toBe(false)
  })

  it('is false when it is the only queued row', () => {
    expect(canMoveDown(1, 1)).toBe(false)
  })
})

// 2026-08-19 (prompts/2026-08-19-transfers-name-filter.md): the Transfers page's name filter --
// `rel_path`-only substring match, empty-search identity passthrough, and the "Dismiss list"
// button's own id list.
describe('filterTransferJobs -- the name filter', () => {
  it('matches case-insensitively', () => {
    const jobs = [job('queued', { id: 1, rel_path: 'Married.At.First.Sight.S01' })]
    expect(filterTransferJobs(jobs, 'married')).toEqual(jobs)
    expect(filterTransferJobs(jobs, 'MARRIED')).toEqual(jobs)
  })

  it('matches a dotted literal like a plain substring -- no glob/regex parsing', () => {
    const jobs = [
      job('queued', { id: 1, rel_path: 'Married.At.First.Sight.S01E02' }),
      job('queued', { id: 2, rel_path: 'Some.Other.Release' }),
    ]
    expect(filterTransferJobs(jobs, 'at.first.sight')).toEqual([jobs[0]])
  })

  it('returns the exact same array by identity for an empty search', () => {
    const jobs = [job('queued', { id: 1 })]
    expect(filterTransferJobs(jobs, '')).toBe(jobs)
  })

  it('returns the exact same array by identity for a whitespace-only search', () => {
    const jobs = [job('queued', { id: 1 })]
    expect(filterTransferJobs(jobs, '   ')).toBe(jobs)
  })

  it('returns an empty array when nothing matches', () => {
    const jobs = [job('queued', { id: 1, rel_path: 'Some.Release' })]
    expect(filterTransferJobs(jobs, 'nope')).toEqual([])
  })

  it('matches only rel_path, never queue_name -- a "movies" queue must not make every row in it match "movies"', () => {
    const jobs = [job('queued', { id: 1, rel_path: 'Some.Release', queue_name: 'movies' })]
    expect(filterTransferJobs(jobs, 'movies')).toEqual([])
  })

  it('matches a nested path, not just the trailing name', () => {
    const jobs = [job('queued', { id: 1, rel_path: 'Show.S01/Show.S01E01.mkv' })]
    expect(filterTransferJobs(jobs, 'show.s01e01')).toEqual(jobs)
  })

  it('preserves input order', () => {
    const jobs = [
      job('queued', { id: 1, rel_path: 'Zebra.Release' }),
      job('queued', { id: 2, rel_path: 'Apple.Release' }),
    ]
    expect(filterTransferJobs(jobs, 'release')).toEqual(jobs)
  })
})

describe('transferFilterSummary', () => {
  it('is null while the search is empty', () => {
    expect(transferFilterSummary(3, 12, '')).toBeNull()
  })

  it('is null for a whitespace-only search', () => {
    expect(transferFilterSummary(3, 12, '   ')).toBeNull()
  })

  it('reports shown of total once the search is non-empty', () => {
    expect(transferFilterSummary(3, 12, 'married')).toBe('Showing 3 of 12 transfers')
  })

  it('uses the singular "transfer" for a total of exactly one', () => {
    expect(transferFilterSummary(1, 1, 'x')).toBe('Showing 1 of 1 transfer')
  })
})

// 2026-08-20 (docs/transfers-redesign-spec.md §3.3, phase 1 stage 5): the Transfers row's
// per-file expansion -- "the thing Files is currently used for, moved to where the ordering
// lives." Pure functions only, same discipline as every other describe block in this file.

const DIM_FACETS = {
  remote: { level: 'dim' as const, reason: 'absent' },
  local: { level: 'dim' as const, reason: 'missing' },
  verified: { level: 'dim' as const, reason: 'unverified' },
  extracted: { level: 'dim' as const, reason: 'not_extracted' },
}

function fileNode(rel_path: string, overrides: Partial<FileNode> = {}): FileNode {
  return {
    id: 1,
    rel_path,
    is_dir: false,
    state: 'PARTIAL',
    substate: null,
    suppressed_reason: null,
    remote_size: 1000,
    local_size: 500,
    remote_mtime: null,
    local_mtime: null,
    state_changed_at: null,
    first_seen_at: null,
    settle_matched_scans: null,
    settle_first_matched_at: null,
    settle_total_bytes: null,
    settle_first_observed_at: null,
    settle_last_changed_at: null,
    downloaded_at: null,
    verified_at: null,
    extracted_at: null,
    first_missing_at: null,
    remote_deleted_at: null,
    pending_download_prefix: null,
    deleted_archive_at: null,
    arr_status: null,
    arr_status_at: null,
    facets: DIM_FACETS,
    ...overrides,
  }
}

describe('showsFileList -- whether a row offers the per-file expansion', () => {
  it('a directory (mirror) job offers it', () => {
    expect(showsFileList(job('running', { is_dir: true }))).toBe(true)
  })

  it('a single-file (pget) job does not -- it has no children by construction', () => {
    expect(showsFileList(job('running', { is_dir: false }))).toBe(false)
  })
})

describe('childDisplayName -- a child row\'s name relative to its job', () => {
  it('strips the job\'s own rel_path prefix', () => {
    expect(childDisplayName('Release/Season 01/e01.mkv', 'Release')).toBe('Season 01/e01.mkv')
  })

  it('strips a single-level prefix down to the bare filename', () => {
    expect(childDisplayName('Release/e01.mkv', 'Release')).toBe('e01.mkv')
  })

  it('falls back to the untouched rel_path if the expected prefix is absent (defensive)', () => {
    expect(childDisplayName('Other/e01.mkv', 'Release')).toBe('Other/e01.mkv')
  })
})

describe('fileListCapNote -- the "showing N of total" line', () => {
  it('is null when nothing was capped', () => {
    expect(fileListCapNote(30, 30)).toBeNull()
  })

  it('names both numbers when the server-side cap trimmed the list', () => {
    expect(fileListCapNote(500, 812)).toBe('Showing 500 of 812 files.')
  })
})

describe('mergeFileListChildren -- overlaying live WS state onto the fetched, capped row set', () => {
  it('prefers the live node\'s state/sizes over the initially-fetched ones, by rel_path', () => {
    const fetched = [fileNode('Release/a.mkv', { state: 'PARTIAL', local_size: 100, remote_size: 1000 })]
    const live = [fileNode('Release/a.mkv', { state: 'DOWNLOADED', local_size: 1000, remote_size: 1000 })]
    const rows = mergeFileListChildren(fetched, live, {})
    expect(rows).toEqual([
      { id: 1, rel_path: 'Release/a.mkv', state: 'DOWNLOADED', remote_size: 1000, local_size: 1000, speed_bps: null },
    ])
  })

  it('keeps the fetched row as-is when the live tree has nothing for that rel_path yet', () => {
    const fetched = [fileNode('Release/a.mkv', { state: 'PARTIAL', local_size: 100, remote_size: 1000 })]
    const rows = mergeFileListChildren(fetched, [], {})
    expect(rows[0]).toMatchObject({ state: 'PARTIAL', local_size: 100 })
  })

  it('never grows or shrinks the row set -- the fetched, capped list is the fixed set', () => {
    const fetched = [fileNode('Release/a.mkv'), fileNode('Release/b.mkv', { id: 2 })]
    // A live tree with an extra file (`c.mkv`) that was never part of the fetched/capped set --
    // it must not appear, since the cap's whole point is a bounded row count.
    const live = [...fetched, fileNode('Release/c.mkv', { id: 3 })]
    const rows = mergeFileListChildren(fetched, live, {})
    expect(rows.map((r) => r.rel_path)).toEqual(['Release/a.mkv', 'Release/b.mkv'])
  })

  it('attaches a fresh child_progress sample as this row\'s speed_bps, by item id', () => {
    const fetched = [fileNode('Release/a.mkv', { id: 7 })]
    const samples: Record<number, ChildSpeedSample> = { 7: { speedBps: 4096, receivedAt: 1000 } }
    const rows = mergeFileListChildren(fetched, fetched, samples, 1000)
    expect(rows[0].speed_bps).toBe(4096)
  })

  it('reads null for a stale child_progress sample rather than a phantom rate', () => {
    const fetched = [fileNode('Release/a.mkv', { id: 7 })]
    const samples: Record<number, ChildSpeedSample> = { 7: { speedBps: 4096, receivedAt: 1000 } }
    const rows = mergeFileListChildren(fetched, fetched, samples, 1000 + 60_000)
    expect(rows[0].speed_bps).toBeNull()
  })

  it('reads null for a row with no id at all, rather than throwing', () => {
    const fetched = [fileNode('Release/a.mkv', { id: null })]
    const rows = mergeFileListChildren(fetched, fetched, {}, 1000)
    expect(rows[0].speed_bps).toBeNull()
  })
})

// --- The pipeline-completion split (2026-08-20, docs/transfers-redesign-spec.md §3.2,
// prompts/done/2026-08-20-active-box-holds-inflight-pipeline.md) ------------------------------
//
// The rule itself is server-side (`core/pipeline_flight.py`) and deliberately not reimplemented
// here -- the Active box is client-side while the Complete box is a server-paginated query with
// its own `total`, so two independently written tests would drift. What *is* tested here is the
// page's own use of the server's answer: which box a row lands in, what the row says it is
// waiting on, and which controls it offers.

describe('isPipelineInFlight -- which box a row belongs in', () => {
  it('holds a succeeded job whose pipeline is still running in the Active box', () => {
    expect(isPipelineInFlight(job('succeeded', { pipeline_in_flight: true }))).toBe(true)
  })

  it('files a succeeded job whose pipeline is finished under Complete', () => {
    expect(isPipelineInFlight(job('succeeded', { pipeline_in_flight: false }))).toBe(false)
  })

  it('never moves a queued or running job out of Active, whatever the flag says', () => {
    expect(isPipelineInFlight(job('queued', { pipeline_in_flight: false }))).toBe(true)
    expect(isPipelineInFlight(job('running', { pipeline_in_flight: false }))).toBe(true)
  })

  it('degrades a missing flag to "complete" rather than wedging the row in Active', () => {
    // An older server that doesn't send the field at all -- the same fail-safe direction the
    // server-side predicate takes for anything it can't bound.
    expect(isPipelineInFlight(job('failed'))).toBe(false)
  })
})

describe('waitingReasonLabel -- the row says what it is waiting on', () => {
  it('maps each reason the server can send to its own wording', () => {
    expect(waitingReasonLabel('verifying')).toBe('Verifying')
    expect(waitingReasonLabel('extracting')).toBe('Extracting')
    expect(waitingReasonLabel('processing')).toBe('Processing')
    expect(waitingReasonLabel('awaiting_import')).toBe('Awaiting import')
    expect(waitingReasonLabel('deleting_source')).toBe('Deleting source')
  })

  it('renders nothing when there is no reason (a queued/running row says it on its state chip)', () => {
    expect(waitingReasonLabel(null)).toBeNull()
    expect(waitingReasonLabel(undefined)).toBeNull()
    expect(waitingReasonLabel('')).toBeNull()
  })

  it('falls back to the raw value for a reason this build does not know yet', () => {
    expect(waitingReasonLabel('awaiting_something_new')).toBe('awaiting_something_new')
  })
})

describe('canDismiss -- an in-flight row is not dismissable', () => {
  it('offers Dismiss on a terminal row whose pipeline is done', () => {
    expect(canDismiss(job('succeeded', { pipeline_in_flight: false }))).toBe(true)
    expect(canDismiss(job('failed'))).toBe(true)
  })

  it('withholds it while the pipeline is still in flight', () => {
    // `core/queue.py.dismiss_job` rejects this with a 409 now, and `list_jobs()` drops a
    // dismissed job unconditionally -- so allowing the click is how a row vanishes from *both*
    // boxes at once.
    expect(canDismiss(job('succeeded', { pipeline_in_flight: true }))).toBe(false)
  })

  it('still withholds it from an active job, exactly as isDismissable always did', () => {
    expect(canDismiss(job('queued'))).toBe(false)
    expect(canDismiss(job('running'))).toBe(false)
  })
})

describe('canResolveManually -- where the escape hatch is offered', () => {
  it('offers it on an in-flight row that is no longer transferring', () => {
    expect(canResolveManually(job('succeeded', { pipeline_in_flight: true }))).toBe(true)
  })

  it('never offers it on a genuinely queued or running job', () => {
    // A transfer that is actually running is not something a classification button gets to hide
    // -- Stop is the control for that, and `api/jobs.py.resolve_item` refuses the write besides.
    expect(canResolveManually(job('queued'))).toBe(false)
    expect(canResolveManually(job('running'))).toBe(false)
  })

  it('never offers it on a row that has already reached Complete', () => {
    expect(canResolveManually(job('succeeded', { pipeline_in_flight: false }))).toBe(false)
  })
})

describe('manualOutcomeLabel -- a hand-resolved row must not look like a normal completion', () => {
  it('names the outcome that was chosen', () => {
    expect(manualOutcomeLabel(job('succeeded', { manual_outcome: 'complete' }))).toBe('Marked complete')
    expect(manualOutcomeLabel(job('failed', { manual_outcome: 'failed' }))).toBe('Marked failed')
  })

  it('renders nothing for a row nobody touched', () => {
    expect(manualOutcomeLabel(job('succeeded'))).toBeNull()
  })
})

describe('resolveMenuOptions', () => {
  it('offers the two outcomes, complete first', () => {
    expect(resolveMenuOptions()).toEqual([
      { outcome: 'complete', label: 'Mark complete' },
      { outcome: 'failed', label: 'Mark failed' },
    ])
  })

  it('adds the undo option only once a row actually carries a manual outcome', () => {
    expect(resolveMenuOptions(true).map((o) => o.outcome)).toEqual(['complete', 'failed', null])
  })
})

describe('sortTransferRows -- in-flight post-transfer rows sit with the running ones', () => {
  it('places a still-processing row above the queued backlog, not below it', () => {
    // Otherwise it sorts into the newest-finished-first tail beneath the whole backlog -- page 11
    // of a busy queue, where the user never sees the thing they're being told is still moving.
    const running = job('running', { id: 1 })
    const processing = job('succeeded', {
      id: 2,
      pipeline_in_flight: true,
      finished_at: '2026-08-20T10:00:00.000000Z',
    })
    const queued = job('queued', { id: 3 })
    const done = job('succeeded', {
      id: 4,
      pipeline_in_flight: false,
      finished_at: '2026-08-20T11:00:00.000000Z',
    })
    expect(sortTransferRows([done, queued, processing, running]).map((j) => j.id)).toEqual([
      1, 2, 3, 4,
    ])
  })

  it('orders several in-flight rows newest-finished first, same as the terminal tail', () => {
    const older = job('succeeded', {
      id: 1,
      pipeline_in_flight: true,
      finished_at: '2026-08-20T09:00:00.000000Z',
    })
    const newer = job('succeeded', {
      id: 2,
      pipeline_in_flight: true,
      finished_at: '2026-08-20T10:00:00.000000Z',
    })
    expect(sortTransferRows([older, newer]).map((j) => j.id)).toEqual([2, 1])
  })
})
