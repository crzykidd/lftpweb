import { describe, expect, it } from 'vitest'
import {
  computeCategoryRows,
  describeCategorySource,
  inferCategoryMappings,
  suggestQueueForCategory,
  type QueueForCategorySuggestion,
} from './clientCategoryInference'

// Spec §8.3's reference-workflow inference: "the queue remote paths already *are* the client's
// category folders" -- `/home/crzykidd/downloads/complete/ar-tv` under a configured base path
// `/home/crzykidd/downloads/complete` proposes category `ar-tv` bound to that queue.

describe('inferCategoryMappings', () => {
  it('proposes a mapping for a queue whose remote_path sits directly under a base path', () => {
    const result = inferCategoryMappings(
      ['/home/crzykidd/downloads/complete'],
      [
        { id: 1, remote_path: '/home/crzykidd/downloads/complete/ar-tv' },
        { id: 2, remote_path: '/home/crzykidd/downloads/complete/ar-movies' },
      ],
    )
    expect(result).toEqual([
      { category: 'ar-tv', queue_id: 1, queue_remote_path: '/home/crzykidd/downloads/complete/ar-tv' },
      {
        category: 'ar-movies',
        queue_id: 2,
        queue_remote_path: '/home/crzykidd/downloads/complete/ar-movies',
      },
    ])
  })

  it('proposes nothing for a queue outside every configured base path', () => {
    const result = inferCategoryMappings(
      ['/home/crzykidd/downloads/complete'],
      [{ id: 1, remote_path: '/mnt/other/ar-tv' }],
    )
    expect(result).toEqual([])
  })

  it('does not guess at a queue nested two or more levels under a base path', () => {
    const result = inferCategoryMappings(
      ['/home/crzykidd/downloads/complete'],
      [{ id: 1, remote_path: '/home/crzykidd/downloads/complete/tv/ar-tv' }],
    )
    expect(result).toEqual([])
  })

  it('does not propose the base path itself as a category', () => {
    const result = inferCategoryMappings(
      ['/home/crzykidd/downloads/complete'],
      [{ id: 1, remote_path: '/home/crzykidd/downloads/complete' }],
    )
    expect(result).toEqual([])
  })

  it('tolerates a trailing slash on either side', () => {
    const result = inferCategoryMappings(
      ['/home/crzykidd/downloads/complete/'],
      [{ id: 1, remote_path: '/home/crzykidd/downloads/complete/ar-tv/' }],
    )
    expect(result).toEqual([
      { category: 'ar-tv', queue_id: 1, queue_remote_path: '/home/crzykidd/downloads/complete/ar-tv/' },
    ])
  })

  it('returns nothing with no configured base paths', () => {
    expect(inferCategoryMappings([], [{ id: 1, remote_path: '/anything/ar-tv' }])).toEqual([])
  })
})

// The redesigned control (prompts/2026-08-23-category-binding-redesign.md, findings #10/#11):
// direct signal (the client's own reported categories) preferred, path arithmetic as a labelled
// fallback. No free-text field anywhere -- every row's category string comes from one of these
// two mechanisms or an already-saved mapping, never typed.

const QUEUES: QueueForCategorySuggestion[] = [
  { id: 1, name: 'ar-tv', remote_path: '/home/crzykidd/downloads/complete/ar-tv' },
  { id: 2, name: 'movies', remote_path: '/home/crzykidd/downloads/complete/ar-movies' },
]

describe('suggestQueueForCategory', () => {
  it('matches a queue by name first', () => {
    expect(suggestQueueForCategory('ar-tv', QUEUES)).toBe(1)
  })

  it('falls back to the trailing segment of remote_path', () => {
    expect(suggestQueueForCategory('ar-movies', QUEUES)).toBe(2)
  })

  it('returns null when nothing matches', () => {
    expect(suggestQueueForCategory('music', QUEUES)).toBeNull()
  })
})

describe('computeCategoryRows', () => {
  it('renders one row per category the client reports, with a suggested binding pre-selected', () => {
    const { rows, source } = computeCategoryRows([], ['ar-tv', 'ar-movies'], [], QUEUES)
    expect(source).toBe('client')
    expect(rows).toEqual([
      { category: 'ar-tv', queue_id: 1 },
      { category: 'ar-movies', queue_id: 2 },
    ])
  })

  it('keeps an already-saved binding instead of overwriting it with a suggestion', () => {
    const existing = [{ category: 'ar-tv', queue_id: 2 }]
    const { rows } = computeCategoryRows(existing, ['ar-tv'], [], QUEUES)
    expect(rows).toEqual([{ category: 'ar-tv', queue_id: 2 }])
  })

  it('a suggested (unsaved) binding defaults to a selected value, not unbound', () => {
    const { rows } = computeCategoryRows([], ['ar-tv'], [], QUEUES)
    expect(rows[0].queue_id).toBe(1)
  })

  it('preserves a saved mapping for a category the client no longer reports', () => {
    const existing = [{ category: 'stale-category', queue_id: 2 }]
    const { rows, source } = computeCategoryRows(existing, ['ar-tv'], [], QUEUES)
    expect(source).toBe('client')
    expect(rows).toContainEqual({ category: 'stale-category', queue_id: 2 })
    expect(rows).toContainEqual({ category: 'ar-tv', queue_id: 1 })
  })

  it('falls back to path-arithmetic proposals when the client reports no categories', () => {
    const { rows, source } = computeCategoryRows(
      [],
      [],
      ['/home/crzykidd/downloads/complete'],
      [{ id: 3, name: 'q', remote_path: '/home/crzykidd/downloads/complete/ar-tv' }],
    )
    expect(source).toBe('path_arithmetic')
    expect(rows).toEqual([{ category: 'ar-tv', queue_id: 3 }])
  })

  it('falls back to path arithmetic when the client has never been tested (null)', () => {
    const { rows, source } = computeCategoryRows(
      [],
      null,
      ['/home/crzykidd/downloads/complete'],
      [{ id: 3, name: 'q', remote_path: '/home/crzykidd/downloads/complete/ar-tv' }],
    )
    expect(source).toBe('path_arithmetic')
    expect(rows).toEqual([{ category: 'ar-tv', queue_id: 3 }])
  })

  it('reports source "none" when nothing can be proposed either way', () => {
    const { rows, source } = computeCategoryRows([], [], [], QUEUES)
    expect(source).toBe('none')
    expect(rows).toEqual([])
  })

  it('does not duplicate an already-existing category with a path-arithmetic proposal', () => {
    const existing = [{ category: 'ar-tv', queue_id: 1 }]
    const { rows, source } = computeCategoryRows(
      existing,
      [],
      ['/home/crzykidd/downloads/complete'],
      [{ id: 1, name: 'ar-tv', remote_path: '/home/crzykidd/downloads/complete/ar-tv' }],
    )
    expect(source).toBe('none')
    expect(rows).toEqual([{ category: 'ar-tv', queue_id: 1 }])
  })
})

describe('describeCategorySource', () => {
  it('labels the direct signal', () => {
    expect(describeCategorySource('client', ['ar-tv'])).toMatch(/directly from this client/)
  })

  it('labels a path-arithmetic guess as a guess, distinctly from the direct signal', () => {
    const message = describeCategorySource('path_arithmetic', [])
    expect(message).toMatch(/guessed/)
    expect(message).not.toMatch(/directly from this client/)
  })

  it('distinguishes "never tested" from "tested and reported none"', () => {
    const neverTested = describeCategorySource('none', null)
    const reportedNone = describeCategorySource('none', [])
    expect(neverTested).not.toEqual(reportedNone)
  })
})
