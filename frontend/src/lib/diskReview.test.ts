import { describe, expect, it } from 'vitest'
import type { DiskReviewDebrisOut } from '../api/types'
import { freedBytes } from './diskReview'

function candidate(overrides: Partial<DiskReviewDebrisOut>): DiskReviewDebrisOut {
  return {
    root: '/complete/tv',
    rel_path: 'a.mkv',
    abs_path: '/complete/tv/a.mkv',
    size: 100,
    mtime: 0,
    inode: null,
    nlink: null,
    link_paths: [],
    ...overrides,
  }
}

describe('freedBytes', () => {
  it('sums unlinked candidates normally', () => {
    const a = candidate({ abs_path: '/complete/tv/a.mkv', size: 100 })
    const b = candidate({ abs_path: '/complete/tv/b.mkv', size: 250 })
    expect(freedBytes([a, b], new Set([a.abs_path]))).toBe(100)
    expect(freedBytes([a, b], new Set([a.abs_path, b.abs_path]))).toBe(350)
  })

  it('reports zero bytes for a partial selection of a linked pair (spec §10.5)', () => {
    const linkPaths = ['/complete/tv/file.mkv', '/complete/tv-alt/file.mkv']
    const a = candidate({ abs_path: linkPaths[0], size: 40_000_000_000, link_paths: linkPaths })
    const b = candidate({ abs_path: linkPaths[1], size: 40_000_000_000, link_paths: linkPaths })
    expect(freedBytes([a, b], new Set([a.abs_path]))).toBe(0)
    expect(freedBytes([a, b], new Set(linkPaths))).toBe(40_000_000_000)
  })

  it('never double-counts a linked group once every link is selected', () => {
    const linkPaths = ['/complete/tv/file.mkv', '/complete/tv-alt/file.mkv']
    const a = candidate({ abs_path: linkPaths[0], size: 500, link_paths: linkPaths })
    const b = candidate({ abs_path: linkPaths[1], size: 500, link_paths: linkPaths })
    expect(freedBytes([a, b], new Set(linkPaths))).toBe(500)
  })
})
