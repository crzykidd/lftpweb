import { describe, expect, it } from 'vitest'
import { inferCategoryMappings } from './clientCategoryInference'

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
