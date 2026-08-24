import { describe, expect, it } from 'vitest'
import type { DiskReviewDebrisOut, DiskReviewSeedingEstateOut, DiskReviewUnclaimedOut } from '../api/types'
import {
  freedBytes,
  groupDebrisByDirectory,
  groupSeedingEstateByTorrent,
  groupUnclaimedByDirectory,
} from './diskReview'

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

function unclaimedItem(overrides: Partial<DiskReviewUnclaimedOut>): DiskReviewUnclaimedOut {
  return {
    ...candidate({}),
    reason: 'some-category cannot be resolved to a path',
    ...overrides,
  }
}

function seedingEntry(overrides: Partial<DiskReviewSeedingEstateOut>): DiskReviewSeedingEstateOut {
  return {
    root: '/rtorrent/data',
    rel_path: 'Release/file.mkv',
    abs_path: '/rtorrent/data/Release/file.mkv',
    size: 100,
    claimed_by_client_id: 1,
    claimed_by_client_name: 'rTorrent',
    claimed_transfer_id: 't1',
    claimed_transfer_name: 'Release.S01E01',
    claimed_content_path: '/rtorrent/data/Release',
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

// --- Display-layer rollups (finding #7, 2026-08-23) ----------------------------------------

describe('groupDebrisByDirectory', () => {
  it('buckets debris by its parent directory, sorted', () => {
    const a = candidate({ abs_path: '/complete/tv/Zeta.Release/a.mkv' })
    const b = candidate({ abs_path: '/complete/tv/Alpha.Release/b.mkv' })
    const c = candidate({ abs_path: '/complete/tv/Alpha.Release/c.nfo' })
    const groups = groupDebrisByDirectory([a, b, c])
    expect(groups.map((g) => g.directory)).toEqual([
      '/complete/tv/Alpha.Release',
      '/complete/tv/Zeta.Release',
    ])
    expect(groups[0].entries).toEqual([b, c])
    expect(groups[1].entries).toEqual([a])
  })

  it('a group\'s own reclaim total stays link-aware -- selecting one of two hardlinks reports zero', () => {
    // The two links sit under *different* directories (a torrent's seeding root vs. the
    // completed-folder copy) -- exactly the case that would matter in practice, and the case a
    // naive per-group sum would get wrong.
    const linkPaths = ['/rtorrent/data/Release/file.mkv', '/complete/tv/Release/file.mkv']
    const seedCopy = candidate({ abs_path: linkPaths[0], size: 40_000_000_000, link_paths: linkPaths })
    const hardlink = candidate({ abs_path: linkPaths[1], size: 40_000_000_000, link_paths: linkPaths })
    const groups = groupDebrisByDirectory([seedCopy, hardlink])
    expect(groups).toHaveLength(2)
    const seedGroup = groups.find((g) => g.directory === '/rtorrent/data/Release')!
    // Only the seed copy selected -- the completed-folder link still holds the inode, so this
    // group's own reclaim total (computed against the *global* selection, not just this group's
    // members) must be zero, not the file's full size.
    expect(freedBytes(seedGroup.entries, new Set([seedCopy.abs_path]))).toBe(0)
    // Both links selected -- now it's real.
    expect(freedBytes(seedGroup.entries, new Set(linkPaths))).toBe(40_000_000_000)
  })
})

describe('groupUnclaimedByDirectory', () => {
  it('buckets unclaimed items by directory, sorted, same as debris', () => {
    const a = unclaimedItem({ abs_path: '/rtorrent/data/Zeta.Release/a.mkv' })
    const b = unclaimedItem({ abs_path: '/rtorrent/data/Alpha.Release/b.mkv' })
    const groups = groupUnclaimedByDirectory([a, b])
    expect(groups.map((g) => g.directory)).toEqual([
      '/rtorrent/data/Alpha.Release',
      '/rtorrent/data/Zeta.Release',
    ])
  })

  it('carries each item\'s own reason through to the group', () => {
    const a = unclaimedItem({
      abs_path: '/rtorrent/data/Orphan.Release/a.mkv',
      reason: 'other-site-movies cannot be resolved to a path',
    })
    const groups = groupUnclaimedByDirectory([a])
    expect(groups[0].entries[0].reason).toBe('other-site-movies cannot be resolved to a path')
  })

  it('reclaim total stays link-aware -- selecting one of two hardlinks reports zero (finding #17)', () => {
    const linkPaths = ['/rtorrent/data/Release/file.mkv', '/complete/tv/Release/file.mkv']
    const seedCopy = unclaimedItem({ abs_path: linkPaths[0], size: 40_000_000_000, link_paths: linkPaths })
    const hardlink = unclaimedItem({ abs_path: linkPaths[1], size: 40_000_000_000, link_paths: linkPaths })
    expect(freedBytes([seedCopy, hardlink], new Set([seedCopy.abs_path]))).toBe(0)
    expect(freedBytes([seedCopy, hardlink], new Set(linkPaths))).toBe(40_000_000_000)
  })
})

describe('groupSeedingEstateByTorrent', () => {
  it('rolls two hardlinked files claimed by the same torrent into one group', () => {
    const seedCopy = seedingEntry({ abs_path: '/rtorrent/data/Release/file.mkv', size: 100 })
    const hardlink = seedingEntry({ abs_path: '/complete/tv/Release/file.mkv', size: 100 })
    const groups = groupSeedingEstateByTorrent([seedCopy, hardlink])
    expect(groups).toHaveLength(1)
    expect(groups[0].transferName).toBe('Release.S01E01')
    expect(groups[0].entries).toHaveLength(2)
    expect(groups[0].totalSize).toBe(200)
  })

  it('keeps two different torrents in separate groups, even from the same client', () => {
    const a = seedingEntry({ claimed_transfer_id: 't1', claimed_transfer_name: 'Alpha' })
    const b = seedingEntry({ claimed_transfer_id: 't2', claimed_transfer_name: 'Beta' })
    const groups = groupSeedingEstateByTorrent([a, b])
    expect(groups.map((g) => g.transferName)).toEqual(['Alpha', 'Beta'])
  })

  it('keeps the same transfer id from two different clients separate', () => {
    const a = seedingEntry({ claimed_by_client_id: 1, claimed_transfer_id: 't1', claimed_transfer_name: 'Alpha' })
    const b = seedingEntry({ claimed_by_client_id: 2, claimed_transfer_id: 't1', claimed_transfer_name: 'Alpha' })
    const groups = groupSeedingEstateByTorrent([a, b])
    expect(groups).toHaveLength(2)
  })
})
