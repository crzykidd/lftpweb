import { describe, expect, it } from 'vitest'
import type { DiskReviewTorrentOut } from '../api/types'
import {
  attributionLabel,
  filterTorrentsByLabel,
  getCategoryLabels,
  sortTorrents,
  visibleTorrentColumns,
  type TorrentSortColumn,
} from './diskReviewSort'

function torrent(overrides: Partial<DiskReviewTorrentOut>): DiskReviewTorrentOut {
  return {
    client_id: 1,
    transfer_id: 't1',
    transfer_name: 'Alpha.Release',
    content_path: '/rtorrent/data/Alpha.Release',
    category: 'ar-tv',
    attribution: 'bound',
    size_bytes: 1000,
    uploaded_bytes: 500,
    ratio: 0.5,
    seed_time_s: 3600,
    added_at: '2026-08-20T00:00:00Z',
    raw_status: 'seeding',
    phase: 'seeding',
    file_count: 3,
    size_on_disk: 1000,
    missing_on_disk: false,
    claim_key: '1:t1',
    ...overrides,
  }
}

// --- Comparators, null-last in both directions ----------------------------------------------

describe('sortTorrents', () => {
  const columns: TorrentSortColumn[] = [
    'transfer_name',
    'category',
    'file_count',
    'size_on_disk',
    'uploaded_bytes',
    'seed_time_s',
    'ratio',
  ]

  it('sorts transfer_name ascending and descending', () => {
    const a = torrent({ transfer_name: 'Zeta', claim_key: 'a' })
    const b = torrent({ transfer_name: 'Alpha', claim_key: 'b' })
    expect(sortTorrents([a, b], { column: 'transfer_name', direction: 'asc' }).map((t) => t.claim_key)).toEqual([
      'b',
      'a',
    ])
    expect(sortTorrents([a, b], { column: 'transfer_name', direction: 'desc' }).map((t) => t.claim_key)).toEqual([
      'a',
      'b',
    ])
  })

  it.each(columns)('%s: null sorts last ascending', (column) => {
    const withValue = torrent({ claim_key: 'has-value', [column]: column === 'category' ? 'z' : 5 } as Partial<
      Record<TorrentSortColumn, unknown>
    > as Partial<DiskReviewTorrentOut>)
    const withNull = torrent({ claim_key: 'is-null', [column]: null } as Partial<
      Record<TorrentSortColumn, unknown>
    > as Partial<DiskReviewTorrentOut>)
    const sorted = sortTorrents([withNull, withValue], { column, direction: 'asc' })
    expect(sorted.map((t) => t.claim_key)).toEqual(['has-value', 'is-null'])
  })

  it.each(columns)('%s: null sorts last descending too (not first)', (column) => {
    const withValue = torrent({ claim_key: 'has-value', [column]: column === 'category' ? 'z' : 5 } as Partial<
      Record<TorrentSortColumn, unknown>
    > as Partial<DiskReviewTorrentOut>)
    const withNull = torrent({ claim_key: 'is-null', [column]: null } as Partial<
      Record<TorrentSortColumn, unknown>
    > as Partial<DiskReviewTorrentOut>)
    const sorted = sortTorrents([withNull, withValue], { column, direction: 'desc' })
    expect(sorted.map((t) => t.claim_key)).toEqual(['has-value', 'is-null'])
  })

  it('is stable: re-sorting equal keys never shuffles rows', () => {
    const rows = [
      torrent({ claim_key: '1', ratio: 1.0, transfer_name: 'A' }),
      torrent({ claim_key: '2', ratio: 1.0, transfer_name: 'B' }),
      torrent({ claim_key: '3', ratio: 1.0, transfer_name: 'C' }),
    ]
    const sorted = sortTorrents(rows, { column: 'ratio', direction: 'asc' })
    expect(sorted.map((t) => t.claim_key)).toEqual(['1', '2', '3'])
    // Sorting again on the same equal keys must reproduce the identical order, not shuffle it.
    const sortedAgain = sortTorrents(sorted, { column: 'ratio', direction: 'asc' })
    expect(sortedAgain.map((t) => t.claim_key)).toEqual(['1', '2', '3'])
  })

  it('never mutates its input array', () => {
    const rows = [torrent({ claim_key: 'a', transfer_name: 'Z' }), torrent({ claim_key: 'b', transfer_name: 'A' })]
    const original = [...rows]
    sortTorrents(rows, { column: 'transfer_name', direction: 'asc' })
    expect(rows).toEqual(original)
  })
})

// --- The label filter ------------------------------------------------------------------------

describe('getCategoryLabels', () => {
  it('lists every distinct category, sorted, with its first-seen attribution', () => {
    const rows = [
      torrent({ category: 'ar-tv', attribution: 'bound' }),
      torrent({ category: 'ar-music', attribution: 'excluded' }),
      torrent({ category: 'ar-tv', attribution: 'bound' }),
    ]
    expect(getCategoryLabels(rows)).toEqual([
      { category: 'ar-music', attribution: 'excluded' },
      { category: 'ar-tv', attribution: 'bound' },
    ])
  })

  it('omits null categories -- there is no dedicated "uncategorized" filter option', () => {
    const rows = [torrent({ category: null }), torrent({ category: 'ar-tv' })]
    expect(getCategoryLabels(rows)).toEqual([{ category: 'ar-tv', attribution: 'bound' }])
  })

  it('a category stays listed even when the current filter/view would show zero rows for it -- '
    + 'the option list is a function of the full scan, never the filtered subset', () => {
    const rows = [torrent({ category: 'ar-music', claim_key: 'm' }), torrent({ category: 'ar-tv', claim_key: 't' })]
    const labels = getCategoryLabels(rows)
    // Filtering the *view* down to ar-tv only leaves ar-music with zero visible rows...
    const filtered = filterTorrentsByLabel(rows, 'ar-tv')
    expect(filtered.map((t) => t.claim_key)).toEqual(['t'])
    // ...but the label list itself, built from `rows` directly, still names both.
    expect(labels.map((l) => l.category)).toEqual(['ar-music', 'ar-tv'])
  })
})

describe('filterTorrentsByLabel', () => {
  it('null label ("All labels") passes every row through unchanged', () => {
    const rows = [torrent({ category: 'ar-tv' }), torrent({ category: null })]
    expect(filterTorrentsByLabel(rows, null)).toEqual(rows)
  })

  it('composes with sort: filter first, then sort the reduced set', () => {
    const rows = [
      torrent({ category: 'ar-tv', transfer_name: 'Zeta', claim_key: 'z' }),
      torrent({ category: 'ar-tv', transfer_name: 'Alpha', claim_key: 'a' }),
      torrent({ category: 'ar-music', transfer_name: 'Middle', claim_key: 'm' }),
    ]
    const filtered = filterTorrentsByLabel(rows, 'ar-tv')
    const sorted = sortTorrents(filtered, { column: 'transfer_name', direction: 'asc' })
    expect(sorted.map((t) => t.claim_key)).toEqual(['a', 'z'])
  })
})

describe('attributionLabel', () => {
  it('prefers plain language over the internal state names', () => {
    expect(attributionLabel('excluded')).toBe('Not monitored here')
    expect(attributionLabel('undecided')).toBe('Unassigned')
    expect(attributionLabel('bound')).toBe('Bound to a queue')
  })
})

// --- Capability-driven columns -----------------------------------------------------------------

describe('visibleTorrentColumns', () => {
  it('shows every column for a fully-capable client', () => {
    expect(visibleTorrentColumns({ ratio: 'native', uploaded_bytes: 'native', seed_time_s: 'derived' })).toEqual([
      'transfer_name',
      'category',
      'file_count',
      'size_on_disk',
      'uploaded_bytes',
      'seed_time_s',
      'ratio',
    ])
  })

  it('a client declaring ratio unsupported yields a column set without ratio, driven by the '
    + 'capability input and not by any client-type string', () => {
    const columns = visibleTorrentColumns({ ratio: 'none', uploaded_bytes: 'none', seed_time_s: 'none' })
    expect(columns).toEqual(['transfer_name', 'category', 'file_count', 'size_on_disk'])
    expect(columns).not.toContain('ratio')
  })

  it('an unprobed client (empty capabilities) keeps every column -- "unknown" is not '
    + '"declared unsupported"', () => {
    expect(visibleTorrentColumns({})).toEqual([
      'transfer_name',
      'category',
      'file_count',
      'size_on_disk',
      'uploaded_bytes',
      'seed_time_s',
      'ratio',
    ])
  })
})

// Layout (the seven-column table's own overflow-x-auto container, the filter select's explicit
// width, cell-level truncation) is deliberately not tested here -- jsdom performs no layout at
// all, so no test in this file could actually catch a clipped column or a crushed sibling; see
// this task's own handoff prompt §6 and DiskReviewPage.tsx's own comment at the table wrapper.
