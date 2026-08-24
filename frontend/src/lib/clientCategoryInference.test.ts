import { describe, expect, it } from 'vitest'
import {
  canRemoveCategoryRow,
  computeCategoryRows,
  describeCategorySource,
  inferCategoryMappings,
  isStaleCategoryRow,
  newCategoryCount,
  suggestQueueForCategory,
  withExcludedToggle,
  withQueueSelection,
  type CategoryRowDraft,
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
      { category: 'ar-tv', queue_id: 1, source: 'client', excluded: false },
      { category: 'ar-movies', queue_id: 2, source: 'client', excluded: false },
    ])
  })

  it('defaults a newly detected category with no suggested queue to excluded, not undecided', () => {
    // 2026-08-23, prompts/2026-08-23-auto-add-categories-default-excluded.md, defect 2: a
    // category with no direct name/path match to propose lands excluded ("not used here"), the
    // safer default -- never undecided, which would put it inside the delete containment
    // boundary and the scan's proposal set until someone happened to notice and act.
    const { rows } = computeCategoryRows([], ['music'], [], QUEUES)
    expect(rows).toEqual([{ category: 'music', queue_id: null, source: 'client', excluded: true }])
  })

  it('keeps an already-saved binding instead of overwriting it with a suggestion', () => {
    const existing: CategoryRowDraft[] = [
      { category: 'ar-tv', queue_id: 2, source: 'client', excluded: false },
    ]
    const { rows } = computeCategoryRows(existing, ['ar-tv'], [], QUEUES)
    expect(rows).toEqual([{ category: 'ar-tv', queue_id: 2, source: 'client', excluded: false }])
  })

  it('keeps an already-saved exclusion instead of overwriting it with a suggestion', () => {
    // Finding #15: "not used by this instance" is a saved decision -- a category the client
    // still reports must not silently regain a queue suggestion once it's been excluded.
    const existing: CategoryRowDraft[] = [
      { category: 'ar-tv', queue_id: null, source: 'client', excluded: true },
    ]
    const { rows } = computeCategoryRows(existing, ['ar-tv'], [], QUEUES)
    expect(rows).toEqual([{ category: 'ar-tv', queue_id: null, source: 'client', excluded: true }])
  })

  it('a suggested (unsaved) binding defaults to a selected value, not unbound', () => {
    const { rows } = computeCategoryRows([], ['ar-tv'], [], QUEUES)
    expect(rows[0].queue_id).toBe(1)
  })

  it('preserves a saved mapping for a category the client no longer reports', () => {
    const existing: CategoryRowDraft[] = [
      { category: 'stale-category', queue_id: 2, source: 'client', excluded: false },
    ]
    const { rows, source } = computeCategoryRows(existing, ['ar-tv'], [], QUEUES)
    expect(source).toBe('client')
    expect(rows).toContainEqual({
      category: 'stale-category',
      queue_id: 2,
      source: 'client',
      excluded: false,
    })
    expect(rows).toContainEqual({ category: 'ar-tv', queue_id: 1, source: 'client', excluded: false })
  })

  it('falls back to path-arithmetic proposals when the client reports no categories', () => {
    const { rows, source } = computeCategoryRows(
      [],
      [],
      ['/home/crzykidd/downloads/complete'],
      [{ id: 3, name: 'q', remote_path: '/home/crzykidd/downloads/complete/ar-tv' }],
    )
    expect(source).toBe('path_arithmetic')
    expect(rows).toEqual([{ category: 'ar-tv', queue_id: 3, source: 'client', excluded: false }])
  })

  it('falls back to path arithmetic when the client has never been tested (null)', () => {
    const { rows, source } = computeCategoryRows(
      [],
      null,
      ['/home/crzykidd/downloads/complete'],
      [{ id: 3, name: 'q', remote_path: '/home/crzykidd/downloads/complete/ar-tv' }],
    )
    expect(source).toBe('path_arithmetic')
    expect(rows).toEqual([{ category: 'ar-tv', queue_id: 3, source: 'client', excluded: false }])
  })

  it('reports source "none" when nothing can be proposed either way', () => {
    const { rows, source } = computeCategoryRows([], [], [], QUEUES)
    expect(source).toBe('none')
    expect(rows).toEqual([])
  })

  it('does not duplicate an already-existing category with a path-arithmetic proposal', () => {
    const existing: CategoryRowDraft[] = [
      { category: 'ar-tv', queue_id: 1, source: 'client', excluded: false },
    ]
    const { rows, source } = computeCategoryRows(
      existing,
      [],
      ['/home/crzykidd/downloads/complete'],
      [{ id: 1, name: 'ar-tv', remote_path: '/home/crzykidd/downloads/complete/ar-tv' }],
    )
    expect(source).toBe('none')
    expect(rows).toEqual([{ category: 'ar-tv', queue_id: 1, source: 'client', excluded: false }])
  })

  // Round 4 (2026-08-23): the manual "Add category" escape hatch.

  it('preserves a manually-added row the client does not (yet) report', () => {
    const existing: CategoryRowDraft[] = [
      { category: 'ar-movies', queue_id: null, source: 'manual', excluded: false },
    ]
    const { rows, source } = computeCategoryRows(existing, ['ar-tv'], [], QUEUES)
    expect(source).toBe('client')
    expect(rows).toContainEqual({
      category: 'ar-movies',
      queue_id: null,
      source: 'manual',
      excluded: false,
    })
  })

  it('flips a manual row to source "client" once the client actually reports it', () => {
    const existing: CategoryRowDraft[] = [
      { category: 'ar-movies', queue_id: 2, source: 'manual', excluded: false },
    ]
    const { rows } = computeCategoryRows(existing, ['ar-movies'], [], QUEUES)
    expect(rows).toEqual([{ category: 'ar-movies', queue_id: 2, source: 'client', excluded: false }])
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

  // Finding #14 (2026-08-23): the screenshot evidence showed this exact hint -- "Test the
  // connection above to see this client's own categories" -- rendered while editing a *saved*
  // instance whose rows were already on screen, reading as an instruction the user hadn't
  // followed rather than an explanation of what they were looking at.
  it('explains saved rows differently from a genuinely untested instance', () => {
    const untested = describeCategorySource('none', null, false)
    const savedButNotRetested = describeCategorySource('none', null, true)
    expect(untested).not.toEqual(savedButNotRetested)
    expect(savedButNotRetested).toMatch(/saved with this instance/)
  })

  it('defaults hasSavedRows to false, so every existing two-argument call site is unchanged', () => {
    expect(describeCategorySource('none', null)).toBe(describeCategorySource('none', null, false))
  })
})

describe('isStaleCategoryRow', () => {
  it('is false when the client currently reports this category', () => {
    expect(isStaleCategoryRow('ar-tv', ['ar-tv', 'ar-movies'])).toBe(false)
  })

  it('is true for a saved category the client no longer reports', () => {
    expect(isStaleCategoryRow('old-cat', ['ar-tv'])).toBe(true)
  })

  it('is false when never tested this session -- staleness cannot be known from nothing', () => {
    expect(isStaleCategoryRow('anything', null)).toBe(false)
  })
})

describe('canRemoveCategoryRow', () => {
  // Round 4 (2026-08-23): the manual escape hatch's own rows must always be removable, even
  // before any Test has run this session (`detectedCategories === null`) -- a manual row is
  // never auto-produced by `computeCategoryRows`, so nothing will silently bring it back.

  it('is always true for a manual row, even when never tested this session', () => {
    expect(canRemoveCategoryRow({ category: 'ar-movies', source: 'manual' }, null)).toBe(true)
  })

  it('is always true for a manual row the client also currently reports', () => {
    expect(canRemoveCategoryRow({ category: 'ar-tv', source: 'manual' }, ['ar-tv'])).toBe(true)
  })

  it('falls back to staleness for a client-sourced row', () => {
    expect(canRemoveCategoryRow({ category: 'ar-tv', source: 'client' }, ['ar-tv'])).toBe(false)
    expect(canRemoveCategoryRow({ category: 'old-cat', source: 'client' }, ['ar-tv'])).toBe(true)
  })

  it('is false for a client-sourced row when never tested this session', () => {
    expect(canRemoveCategoryRow({ category: 'anything', source: 'client' }, null)).toBe(false)
  })
})

// --- Finding #15 (2026-08-23): three-state categories -- mutual exclusion between a queue
// binding and "not used by this instance," enforced identically to the backend's own validator.

describe('withQueueSelection / withExcludedToggle', () => {
  const excludedRow: CategoryRowDraft = {
    category: 'other-site-tv',
    queue_id: null,
    source: 'client',
    excluded: true,
  }
  const boundRow: CategoryRowDraft = {
    category: 'ar-tv',
    queue_id: 1,
    source: 'client',
    excluded: false,
  }

  it('picking a queue clears any prior exclusion', () => {
    const result = withQueueSelection(excludedRow, 5)
    expect(result).toEqual({ category: 'other-site-tv', queue_id: 5, source: 'client', excluded: false })
  })

  it('picking "undecided" (null) also clears exclusion, landing on undecided rather than excluded', () => {
    const result = withQueueSelection(excludedRow, null)
    expect(result.excluded).toBe(false)
    expect(result.queue_id).toBeNull()
  })

  it('checking "not used" clears any existing queue binding', () => {
    const result = withExcludedToggle(boundRow, true)
    expect(result).toEqual({ category: 'ar-tv', queue_id: null, source: 'client', excluded: true })
  })

  it('unchecking "not used" returns to undecided, not back to the prior queue binding', () => {
    const excludedWasBound: CategoryRowDraft = {
      category: 'ar-tv',
      queue_id: null,
      source: 'client',
      excluded: true,
    }
    const result = withExcludedToggle(excludedWasBound, false)
    expect(result).toEqual({ category: 'ar-tv', queue_id: null, source: 'client', excluded: false })
  })

  it('the two functions never produce a row that is both bound and excluded', () => {
    const afterQueue = withQueueSelection(excludedRow, 5)
    expect(afterQueue.excluded && afterQueue.queue_id != null).toBe(false)
    const afterExclude = withExcludedToggle(boundRow, true)
    expect(afterExclude.excluded && afterExclude.queue_id != null).toBe(false)
  })
})

// --- "New since you last looked" (2026-08-23,
// prompts/2026-08-23-auto-add-categories-default-excluded.md) -- the calm signal replacing the
// unattributed-clients banner's old always-on nagging.

describe('newCategoryCount', () => {
  it('counts an observed category never acknowledged', () => {
    const categories = [{ first_seen_at: '2026-08-23T10:00:00Z' }]
    expect(newCategoryCount(categories, null)).toBe(1)
  })

  it('counts only categories observed after the acknowledgment', () => {
    const categories = [
      { first_seen_at: '2026-08-23T09:00:00Z' }, // before -- not new
      { first_seen_at: '2026-08-23T11:00:00Z' }, // after -- new
    ]
    expect(newCategoryCount(categories, '2026-08-23T10:00:00Z')).toBe(1)
  })

  it('clears to zero once every observed category is acknowledged', () => {
    const categories = [{ first_seen_at: '2026-08-23T10:00:00Z' }]
    expect(newCategoryCount(categories, '2026-08-23T12:00:00Z')).toBe(0)
  })

  it('never counts a category with no first_seen_at (predates the migration, hand-typed, or survived a save)', () => {
    const categories = [{ first_seen_at: null }]
    expect(newCategoryCount(categories, null)).toBe(0)
  })
})
