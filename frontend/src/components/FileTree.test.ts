import { describe, expect, it } from 'vitest'
import type { FileNode, LifecycleFacets } from '../api/types'
import type { ChildSpeedSample } from '../hooks/useLiveModel'
import {
  buildTree,
  CHILD_SPEED_FRESHNESS_MS,
  clampColumnWidth,
  columnMinWidth,
  defaultColumnWidths,
  effectiveEtaLabel,
  effectiveSpeedLabel,
  effectiveSpeedSortValue,
  flatten,
  isCollapsePreference,
  isColumnWidths,
  isSortPreference,
  matchesFacetFilter,
  mergeColumnWidths,
  RESIZABLE_COLUMNS,
  resolveCollapsed,
  sortTree,
  type TreeEntry,
} from './FileTree'

const DIM: LifecycleFacets = {
  remote: { level: 'dim', reason: 'absent' },
  local: { level: 'dim', reason: 'missing' },
  verified: { level: 'dim', reason: 'unverified' },
  extracted: { level: 'dim', reason: 'not_extracted' },
}

function node(rel_path: string, is_dir: boolean, overrides: Partial<FileNode> = {}): FileNode {
  return {
    id: 1,
    rel_path,
    is_dir,
    state: 'REMOTE_ONLY',
    substate: null,
    suppressed_reason: null,
    remote_size: null,
    local_size: null,
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
    facets: DIM,
    ...overrides,
  }
}

// --- buildTree ---------------------------------------------------------------------------

describe('buildTree', () => {
  it('nests children under their parent by rel_path, regardless of input order', () => {
    const nodes = [
      node('a/b/file.txt', false),
      node('a', true),
      node('a/b', true),
    ]
    const tree = buildTree(nodes)
    expect(tree).toHaveLength(1)
    expect(tree[0].rel_path).toBe('a')
    expect(tree[0].depth).toBe(0)
    expect(tree[0].children).toHaveLength(1)
    expect(tree[0].children[0].rel_path).toBe('a/b')
    expect(tree[0].children[0].depth).toBe(1)
    expect(tree[0].children[0].children).toHaveLength(1)
    expect(tree[0].children[0].children[0].rel_path).toBe('a/b/file.txt')
    expect(tree[0].children[0].children[0].depth).toBe(2)
  })

  it('derives name from the last path segment, and root name from the whole path', () => {
    const tree = buildTree([node('top.txt', false), node('dir', true), node('dir/inner.txt', false)])
    const top = tree.find((e) => e.rel_path === 'top.txt')
    const inner = tree.find((e) => e.rel_path === 'dir')?.children[0]
    expect(top?.name).toBe('top.txt')
    expect(inner?.name).toBe('inner.txt')
  })

  it('treats a node with no matching parent as its own root, defensively', () => {
    // 'orphan/child.txt' has no 'orphan' node in the input at all.
    const tree = buildTree([node('orphan/child.txt', false)])
    expect(tree).toHaveLength(1)
    expect(tree[0].rel_path).toBe('orphan/child.txt')
  })

  // 2026-08-14 (prompts/2026-08-14-files-page-speed-column.md): `speed_bps` is looked up by id
  // from the second, optional `speedByItemId` argument -- not part of `FileNode`'s wire shape,
  // computed here the same way `name`/`depth` already are.
  it('resolves speed_bps by id from the optional speedByItemId map, null when absent', () => {
    const nodes = [
      node('a.txt', false, { id: 1 }),
      node('b.txt', false, { id: 2 }),
      node('c.txt', false, { id: null }),
    ]
    const tree = buildTree(nodes, { 1: 5_000_000 })
    expect(tree.find((e) => e.rel_path === 'a.txt')?.speed_bps).toBe(5_000_000)
    // Present in the tree but no matching id in the map (job finished, or never ran).
    expect(tree.find((e) => e.rel_path === 'b.txt')?.speed_bps).toBeNull()
    // No id at all (a node the backend never persisted an item row for).
    expect(tree.find((e) => e.rel_path === 'c.txt')?.speed_bps).toBeNull()
  })

  it('defaults speed_bps to null for every row when no speedByItemId argument is passed at all', () => {
    const tree = buildTree([node('a.txt', false, { id: 1 })])
    expect(tree[0].speed_bps).toBeNull()
  })

  // 2026-08-14 ("per-file speed inside a mirror"): `child_speed_bps` is resolved the same
  // shape `speed_bps` already is, but gated on freshness (`now - receivedAt`) rather than
  // passed straight through -- a child never reaches `DOWNLOADING`, so there is no `state`
  // transition to gate staleness on the way there is for the job-level reading.
  describe('resolves child_speed_bps from the optional childSpeedByItemId map, gated on freshness', () => {
    it('a fresh sample resolves to its speed', () => {
      const nodes = [node('a.txt', false, { id: 1 })]
      const childSpeedByItemId: Record<number, ChildSpeedSample> = { 1: { speedBps: 42_000, receivedAt: 1_000 } }
      const tree = buildTree(nodes, {}, childSpeedByItemId, {}, 1_000)
      expect(tree[0].child_speed_bps).toBe(42_000)
    })

    it('a sample exactly at the freshness boundary still counts', () => {
      const nodes = [node('a.txt', false, { id: 1 })]
      const childSpeedByItemId: Record<number, ChildSpeedSample> = { 1: { speedBps: 5_000, receivedAt: 0 } }
      const tree = buildTree(nodes, {}, childSpeedByItemId, {}, CHILD_SPEED_FRESHNESS_MS)
      expect(tree[0].child_speed_bps).toBe(5_000)
    })

    it('a sample older than the freshness window resolves to null -- the stale-sample case', () => {
      const nodes = [node('a.txt', false, { id: 1 })]
      const childSpeedByItemId: Record<number, ChildSpeedSample> = { 1: { speedBps: 5_000, receivedAt: 0 } }
      const tree = buildTree(nodes, {}, childSpeedByItemId, {}, CHILD_SPEED_FRESHNESS_MS + 1)
      expect(tree[0].child_speed_bps).toBeNull()
    })

    it('no entry in the map at all resolves to null, same as speed_bps', () => {
      const nodes = [node('a.txt', false, { id: 1 })]
      const tree = buildTree(nodes, {}, {}, {}, 1_000)
      expect(tree[0].child_speed_bps).toBeNull()
    })

    it('a node with no id at all can never match, regardless of the map contents', () => {
      const nodes = [node('a.txt', false, { id: null })]
      const childSpeedByItemId: Record<number, ChildSpeedSample> = { 1: { speedBps: 5_000, receivedAt: 1_000 } }
      const tree = buildTree(nodes, {}, childSpeedByItemId, {}, 1_000)
      expect(tree[0].child_speed_bps).toBeNull()
    })

    it('defaults to null for every row when no childSpeedByItemId argument is passed at all', () => {
      const tree = buildTree([node('a.txt', false, { id: 1 })])
      expect(tree[0].child_speed_bps).toBeNull()
    })
  })

  // 2026-08-14 ("ETA on Files rows"): `eta_s` is resolved by id from the optional `etaByItemId`
  // argument, the same shape and same "null when absent" fallback `speed_bps` already has above
  // -- it's a straight passthrough of `core/progress.py`'s own already-computed value, not a
  // derivation `buildTree` does itself.
  it('resolves eta_s by id from the optional etaByItemId map, null when absent', () => {
    const nodes = [
      node('a.txt', false, { id: 1 }),
      node('b.txt', false, { id: 2 }),
      node('c.txt', false, { id: null }),
    ]
    const tree = buildTree(nodes, {}, {}, { 1: 125 })
    expect(tree.find((e) => e.rel_path === 'a.txt')?.eta_s).toBe(125)
    // Present in the tree but no matching id in the map (job finished, or never ran).
    expect(tree.find((e) => e.rel_path === 'b.txt')?.eta_s).toBeNull()
    // No id at all.
    expect(tree.find((e) => e.rel_path === 'c.txt')?.eta_s).toBeNull()
  })

  it('resolves eta_s to null when the map holds an explicit null for that id (bytes_total unknown or zero speed)', () => {
    const tree = buildTree([node('a.txt', false, { id: 1 })], {}, {}, { 1: null })
    expect(tree[0].eta_s).toBeNull()
  })

  it('defaults eta_s to null for every row when no etaByItemId argument is passed at all', () => {
    const tree = buildTree([node('a.txt', false, { id: 1 })])
    expect(tree[0].eta_s).toBeNull()
  })
})

// --- effectiveSpeedLabel / effectiveSpeedSortValue: job-level first, child-level fallback ----
//
// 2026-08-14 ("per-file speed inside a mirror"). A row's Speed cell and its sort value both
// prefer the job-level reading (`speed_bps`, gated on `state === 'DOWNLOADING'`) and only fall
// back to the child-level one (`child_speed_bps`, already freshness-filtered by `buildTree`)
// when the job-level reading has nothing to show -- never both, never summed, so the two
// granularities can never read as additive (DESIGN.md §9.2's own bar for this task).

describe('effectiveSpeedLabel / effectiveSpeedSortValue', () => {
  function entry(overrides: Partial<TreeEntry>): TreeEntry {
    return {
      ...node('x', false),
      name: 'x',
      depth: 0,
      children: [],
      speed_bps: null,
      child_speed_bps: null,
      eta_s: null,
      ...overrides,
    }
  }

  it('a DOWNLOADING parent row shows its job-level rate, ignoring any child_speed_bps', () => {
    const e = entry({ state: 'DOWNLOADING', speed_bps: 5_000_000, child_speed_bps: 999 })
    expect(effectiveSpeedLabel(e)).toBe('4.8 MB/s') // formatBytes is 1024-based, not decimal
    expect(effectiveSpeedSortValue(e)).toBe(5_000_000)
  })

  it('a PARTIAL child row with no job-level rate falls back to its own live rate', () => {
    const e = entry({ state: 'PARTIAL', speed_bps: null, child_speed_bps: 250_000 })
    expect(effectiveSpeedLabel(e)).toBe('244.1 KB/s') // formatBytes is 1024-based, not decimal
    expect(effectiveSpeedSortValue(e)).toBe(250_000)
  })

  it('a PARTIAL child row with a stale (already-nulled) sample shows nothing, not the last value', () => {
    // buildTree already resolved the stale sample to null before this ever runs -- this pins
    // that a null child_speed_bps reads as "not transferring", not as a leftover reading.
    const e = entry({ state: 'PARTIAL', speed_bps: null, child_speed_bps: null })
    expect(effectiveSpeedLabel(e)).toBe('—')
    expect(effectiveSpeedSortValue(e)).toBeNull()
  })

  it('a row that is neither a running job nor a fresh child shows nothing', () => {
    const e = entry({ state: 'DOWNLOADED', speed_bps: null, child_speed_bps: null })
    expect(effectiveSpeedLabel(e)).toBe('—')
    expect(effectiveSpeedSortValue(e)).toBeNull()
  })
})

// --- effectiveEtaLabel: job-level first, child-level fallback (2026-08-14, "ETA on Files rows")
//
// Same shape as effectiveSpeedLabel above, and the same reason: `entry.eta_s` (job-level, from
// `core/progress.py.JobProgress.eta_s`) is preferred, falling back to a client-derived
// `childEtaS(remote_size, local_size, child_speed_bps)` only when the job-level reading has
// nothing to show -- never both, never summed.

describe('effectiveEtaLabel', () => {
  function entry(overrides: Partial<TreeEntry>): TreeEntry {
    return {
      ...node('x', false),
      name: 'x',
      depth: 0,
      children: [],
      speed_bps: null,
      child_speed_bps: null,
      eta_s: null,
      ...overrides,
    }
  }

  it('a DOWNLOADING parent row shows its already-computed job-level eta_s', () => {
    const e = entry({ state: 'DOWNLOADING', eta_s: 125 })
    expect(effectiveEtaLabel(e)).toBe('2m')
  })

  it('a DOWNLOADING parent row with no eta_s (bytes_total unknown, or zero speed) shows ' +
    'nothing, even with a live speed reading', () => {
    const e = entry({ state: 'DOWNLOADING', speed_bps: 5_000_000, eta_s: null })
    expect(effectiveEtaLabel(e)).toBe('—')
  })

  it('a PARTIAL child row derives its own ETA from remote_size/local_size/child_speed_bps', () => {
    const e = entry({
      state: 'PARTIAL',
      remote_size: 200_000_000,
      local_size: 100_000_000,
      child_speed_bps: 1_000_000,
    })
    expect(effectiveEtaLabel(e)).toBe('1m')
  })

  it('a completed child (remaining bytes <= 0) shows no ETA even though state is still PARTIAL', () => {
    const e = entry({ state: 'PARTIAL', remote_size: 100, local_size: 100, child_speed_bps: 1_000 })
    expect(effectiveEtaLabel(e)).toBe('—')
  })

  it('a PARTIAL child row with a stale (already-nulled) sample shows nothing, not a stale ETA', () => {
    // buildTree already resolved the stale child_speed_bps to null before this ever runs.
    const e = entry({ state: 'PARTIAL', remote_size: 200_000_000, local_size: 100_000_000, child_speed_bps: null })
    expect(effectiveEtaLabel(e)).toBe('—')
  })

  it('a row that is neither a running job nor a fresh child shows nothing', () => {
    const e = entry({ state: 'DOWNLOADED', eta_s: null, child_speed_bps: null })
    expect(effectiveEtaLabel(e)).toBe('—')
  })
})

// --- flatten -------------------------------------------------------------------------------

describe('flatten', () => {
  it('is depth-first and includes every node when nothing is collapsed', () => {
    const tree = buildTree([node('a', true), node('a/b', true), node('a/b/c.txt', false), node('z.txt', false)])
    const flat = flatten(tree, () => false)
    expect(flat.map((e) => e.rel_path)).toEqual(['a', 'a/b', 'a/b/c.txt', 'z.txt'])
  })

  it('omits every descendant of a collapsed directory, not just its immediate children', () => {
    const tree = buildTree([node('a', true), node('a/b', true), node('a/b/c.txt', false), node('z.txt', false)])
    const flat = flatten(tree, (path) => path === 'a')
    expect(flat.map((e) => e.rel_path)).toEqual(['a', 'z.txt'])
  })

  it('a file entry is never treated as collapsible even if the predicate says so', () => {
    const tree = buildTree([node('a.txt', false)])
    const flat = flatten(tree, () => true)
    expect(flat.map((e) => e.rel_path)).toEqual(['a.txt'])
  })
})

// --- sortTree: the sibling-preserving invariant ---------------------------------------------

describe('sortTree', () => {
  it('reorders siblings within each parent and never flattens the hierarchy', () => {
    // A child whose sort value would sort "before" a top-level sibling if the whole thing were
    // flattened -- proves nesting survives sorting rather than everything collapsing into one
    // array in value order.
    const nodes = [
      node('b_dir', true),
      node('b_dir/z_child.txt', false, { remote_size: 999 }),
      node('a_top.txt', false, { remote_size: 1 }),
    ]
    const sorted = sortTree(buildTree(nodes), 'size', 'asc')

    // Still two roots, not three flattened entries.
    expect(sorted).toHaveLength(2)
    const bDir = sorted.find((e) => e.rel_path === 'b_dir')
    expect(bDir).toBeDefined()
    // The child stays nested under its own parent, not hoisted to root despite its much larger
    // sort value than the top-level file.
    expect(bDir?.children.map((c) => c.rel_path)).toEqual(['b_dir/z_child.txt'])
  })

  it('sorts by name with directories grouped before files at each level', () => {
    const nodes = [
      node('root', true),
      node('root/z_file.txt', false),
      node('root/b_dir', true),
      node('root/a_file.txt', false),
    ]
    const sorted = sortTree(buildTree(nodes), 'name', 'asc')
    const children = sorted[0].children.map((c) => c.name)
    // b_dir (directory) first despite 'b' > 'a' alphabetically, then files in name order.
    expect(children).toEqual(['b_dir', 'a_file.txt', 'z_file.txt'])
  })

  it('name direction flips the within-group order without moving the dir/file grouping', () => {
    const nodes = [node('root', true), node('root/a.txt', false), node('root/b.txt', false), node('root/c_dir', true)]
    const asc = sortTree(buildTree(nodes), 'name', 'asc')[0].children.map((c) => c.name)
    const desc = sortTree(buildTree(nodes), 'name', 'desc')[0].children.map((c) => c.name)
    expect(asc).toEqual(['c_dir', 'a.txt', 'b.txt'])
    expect(desc).toEqual(['c_dir', 'b.txt', 'a.txt'])
  })

  it('sorts a non-name key (size) purely by value, not by directory-first grouping', () => {
    const nodes = [
      node('root', true),
      node('root/small_dir', true, { remote_size: 10 }),
      node('root/big_file.txt', false, { remote_size: 90 }),
    ]
    const sorted = sortTree(buildTree(nodes), 'size', 'asc')[0].children.map((c) => c.rel_path)
    expect(sorted).toEqual(['root/small_dir', 'root/big_file.txt'])
  })

  it('sorts null values last regardless of direction', () => {
    const nodes = [
      node('root', true),
      node('root/known.txt', false, { remote_size: 50 }),
      node('root/unknown.txt', false, { remote_size: null, local_size: null }),
    ]
    const asc = sortTree(buildTree(nodes), 'size', 'asc')[0].children.map((c) => c.rel_path)
    const desc = sortTree(buildTree(nodes), 'size', 'desc')[0].children.map((c) => c.rel_path)
    expect(asc).toEqual(['root/known.txt', 'root/unknown.txt'])
    expect(desc).toEqual(['root/known.txt', 'root/unknown.txt'])
  })

  it('does not mutate the input tree -- returns a fresh clone', () => {
    const tree = buildTree([node('root', true), node('root/b.txt', false), node('root/a.txt', false)])
    const originalOrder = tree[0].children.map((c) => c.name)
    sortTree(tree, 'name', 'desc')
    expect(tree[0].children.map((c) => c.name)).toEqual(originalOrder)
  })

  // 2026-08-14 (prompts/2026-08-14-files-page-speed-column.md): sorting by Speed must put
  // every non-transferring row (never downloading at all, or downloading and finished/stopped)
  // in a defined place -- all at one end, not interleaved by a coincidental zero -- covering the
  // sibling-preserving shape the other sort tests above use, not a flat-ordering assertion.
  describe('sorting by speed', () => {
    it('a transferring row sorts by its live rate; non-transferring rows sort last regardless of direction', () => {
      const nodes = [
        node('root', true),
        // Actively downloading, id matches an entry in speedByItemId below.
        node('root/fast.bin', false, { id: 10, state: 'DOWNLOADING' }),
        node('root/slow.bin', false, { id: 11, state: 'DOWNLOADING' }),
        // Completed -- state isn't DOWNLOADING even though a stale speed value happens to
        // still be sitting in the map (useLiveModel never prunes on completion; see
        // lib/format.ts.transferSpeedSortValue's own docstring for why state is what gates
        // this, not presence of a value).
        node('root/done.bin', false, { id: 12, state: 'DOWNLOADED' }),
        // Never transferred at all -- no entry in the map.
        node('root/idle.bin', false, { id: 13, state: 'REMOTE_ONLY' }),
      ]
      const speedByItemId = { 10: 5_000_000, 11: 1_000_000, 12: 9_999_999 }
      const asc = sortTree(buildTree(nodes, speedByItemId), 'speed', 'asc')[0].children.map((c) => c.rel_path)
      const desc = sortTree(buildTree(nodes, speedByItemId), 'speed', 'desc')[0].children.map((c) => c.rel_path)
      // Transferring rows order by rate (ascending/descending flips just those two); the two
      // non-transferring rows always land after them, in either direction, and their relative
      // order between each other is stable (tie-broken by name, same as every other sort key).
      expect(asc).toEqual(['root/slow.bin', 'root/fast.bin', 'root/done.bin', 'root/idle.bin'])
      expect(desc).toEqual(['root/fast.bin', 'root/slow.bin', 'root/done.bin', 'root/idle.bin'])
    })

    it('a transferring row with a genuine 0 B/s reading sorts as a real zero, not with the non-transferring rows', () => {
      const nodes = [
        node('root', true),
        node('root/stalled.bin', false, { id: 20, state: 'DOWNLOADING' }),
        node('root/idle.bin', false, { id: 21, state: 'REMOTE_ONLY' }),
      ]
      const sorted = sortTree(buildTree(nodes, { 20: 0 }), 'speed', 'asc')[0].children.map((c) => c.rel_path)
      // The stalled-but-running 0 B/s row sorts before the row with no reading at all -- a real
      // zero is a lesser value than "unknown", not the same bucket as it.
      expect(sorted).toEqual(['root/stalled.bin', 'root/idle.bin'])
    })

    // 2026-08-14 ("per-file speed inside a mirror"): a mirroring directory's children are never
    // DOWNLOADING (PARTIAL instead, `core/reconcile.py`'s leaf rule), so their own live rate
    // only ever reaches this sort key through the childSpeedByItemId fallback -- and a stale
    // sample (older than CHILD_SPEED_FRESHNESS_MS) must sort exactly like "never transferred",
    // not linger at its last value.
    it('a PARTIAL child row sorts by its own child_speed_bps fallback; a stale sample sorts last', () => {
      const nodes = [
        node('root', true),
        node('root/fast.rar', false, { id: 30, state: 'PARTIAL' }),
        node('root/slow.rar', false, { id: 31, state: 'PARTIAL' }),
        // A sample old enough to have fallen out of the freshness window at `now` below.
        node('root/stale.rar', false, { id: 32, state: 'PARTIAL' }),
        node('root/idle.rar', false, { id: 33, state: 'REMOTE_ONLY' }),
      ]
      const now = 100_000
      const childSpeedByItemId: Record<number, ChildSpeedSample> = {
        30: { speedBps: 5_000_000, receivedAt: now },
        31: { speedBps: 1_000_000, receivedAt: now },
        32: { speedBps: 9_999_999, receivedAt: now - CHILD_SPEED_FRESHNESS_MS - 1 },
      }
      const asc = sortTree(buildTree(nodes, {}, childSpeedByItemId, {}, now), 'speed', 'asc')[0].children.map(
        (c) => c.rel_path,
      )
      expect(asc).toEqual(['root/slow.rar', 'root/fast.rar', 'root/idle.rar', 'root/stale.rar'])
    })
  })
})

// --- Collapse preference: default-plus-exceptions -------------------------------------------

describe('resolveCollapsed', () => {
  it('a path with no exception reads as the plain default, expanded', () => {
    expect(resolveCollapsed(false, new Set(), 'any/path')).toBe(false)
  })

  it('a path with no exception reads as the plain default, collapsed', () => {
    expect(resolveCollapsed(true, new Set(), 'any/path')).toBe(true)
  })

  it('a path listed as an exception reads as the opposite of the default', () => {
    expect(resolveCollapsed(false, new Set(['x']), 'x')).toBe(true)
    expect(resolveCollapsed(true, new Set(['x']), 'x')).toBe(false)
  })

  it('a newly-arrived directory (never in the exception set) inherits the current default -- ' +
    'the case a naive "save the collapsed set" implementation gets wrong', () => {
    // Simulates: user set default to collapsed and made one exception, then a brand-new
    // directory shows up over the WebSocket. It was never added to `exceptions` because it
    // didn't exist yet -- it must fall through to the default, not read as expanded by omission.
    const exceptions = new Set(['existing/expanded-override'])
    expect(resolveCollapsed(true, exceptions, 'brand/new/directory')).toBe(true)
  })

  it('toggling exception membership is what flips a single path -- not touching the default', () => {
    const defaultCollapsed = false
    let exceptions = new Set<string>()
    expect(resolveCollapsed(defaultCollapsed, exceptions, 'p')).toBe(false)
    exceptions = new Set(exceptions).add('p')
    expect(resolveCollapsed(defaultCollapsed, exceptions, 'p')).toBe(true)
    // Every other path is unaffected by that one path's override.
    expect(resolveCollapsed(defaultCollapsed, exceptions, 'other')).toBe(false)
  })
})

describe('isCollapsePreference', () => {
  it('accepts a well-formed preference', () => {
    expect(isCollapsePreference({ defaultCollapsed: true, exceptions: ['a', 'b'] })).toBe(true)
    expect(isCollapsePreference({ defaultCollapsed: false, exceptions: [] })).toBe(true)
  })

  it('rejects null, non-objects, missing fields, and wrong-typed fields', () => {
    expect(isCollapsePreference(null)).toBe(false)
    expect(isCollapsePreference('nope')).toBe(false)
    expect(isCollapsePreference({})).toBe(false)
    expect(isCollapsePreference({ defaultCollapsed: 'yes', exceptions: [] })).toBe(false)
    expect(isCollapsePreference({ defaultCollapsed: true, exceptions: [1, 2] })).toBe(false)
    expect(isCollapsePreference({ defaultCollapsed: true, exceptions: 'a' })).toBe(false)
  })
})

describe('isSortPreference', () => {
  it('accepts a known key and direction', () => {
    expect(isSortPreference({ key: 'name', dir: 'asc' })).toBe(true)
    expect(isSortPreference({ key: 'percent', dir: 'desc' })).toBe(true)
  })

  it('rejects an unknown key, an invalid direction, or a malformed shape', () => {
    expect(isSortPreference({ key: 'bogus', dir: 'asc' })).toBe(false)
    expect(isSortPreference({ key: 'name', dir: 'sideways' })).toBe(false)
    expect(isSortPreference(null)).toBe(false)
    expect(isSortPreference({})).toBe(false)
  })
})

// --- Facet filter ----------------------------------------------------------------------------

describe('matchesFacetFilter', () => {
  const entry = (facets: Partial<LifecycleFacets>, downloaded_at: string | null = null): TreeEntry => ({
    ...node('x', false, { facets: { ...DIM, ...facets }, downloaded_at }),
    name: 'x',
    depth: 0,
    children: [],
    speed_bps: null,
    child_speed_bps: null,
    eta_s: null,
  })

  it('"" (All items) matches everything', () => {
    expect(matchesFacetFilter(entry({}), '')).toBe(true)
  })

  it('has_remote matches only facets.remote.reason === "present"', () => {
    expect(matchesFacetFilter(entry({ remote: { level: 'green', reason: 'present' } }), 'has_remote')).toBe(true)
    expect(matchesFacetFilter(entry({ remote: { level: 'dim', reason: 'absent' } }), 'has_remote')).toBe(false)
  })

  it('has_local matches any non-dim local level, not a specific reason', () => {
    expect(matchesFacetFilter(entry({ local: { level: 'amber', reason: 'partial' } }), 'has_local')).toBe(true)
    expect(matchesFacetFilter(entry({ local: { level: 'dim', reason: 'missing' } }), 'has_local')).toBe(false)
  })

  it('extracted / not_extracted are exact complements of the extracted reason', () => {
    const extractedEntry = entry({ extracted: { level: 'green', reason: 'extracted' } })
    expect(matchesFacetFilter(extractedEntry, 'extracted')).toBe(true)
    expect(matchesFacetFilter(extractedEntry, 'not_extracted')).toBe(false)

    const notExtractedEntry = entry({ extracted: { level: 'dim', reason: 'not_extracted' } })
    expect(matchesFacetFilter(notExtractedEntry, 'extracted')).toBe(false)
    expect(matchesFacetFilter(notExtractedEntry, 'not_extracted')).toBe(true)
  })

  it('missing_locally requires both downloaded_at set and a "missing" local reason', () => {
    const missingAfterDownload = entry({ local: { level: 'red', reason: 'missing' } }, '2026-08-13T00:00:00Z')
    expect(matchesFacetFilter(missingAfterDownload, 'missing_locally')).toBe(true)

    // Never downloaded -- local absence is not the *arr-import diagnostic this filter targets.
    const neverDownloaded = entry({ local: { level: 'dim', reason: 'missing' } }, null)
    expect(matchesFacetFilter(neverDownloaded, 'missing_locally')).toBe(false)

    // Downloaded but present locally -- nothing missing to report.
    const stillPresent = entry({ local: { level: 'green', reason: 'present' } }, '2026-08-13T00:00:00Z')
    expect(matchesFacetFilter(stillPresent, 'missing_locally')).toBe(false)
  })
})

// --- Column widths -----------------------------------------------------------------------

describe('column width helpers', () => {
  it('defaultColumnWidths returns exactly the RESIZABLE_COLUMNS defaults', () => {
    const widths = defaultColumnWidths()
    for (const col of RESIZABLE_COLUMNS) {
      expect(widths[col.id]).toBe(col.defaultWidth)
    }
    expect(Object.keys(widths)).toHaveLength(RESIZABLE_COLUMNS.length)
  })

  it('clampColumnWidth floors at the column\'s own minWidth', () => {
    const id = RESIZABLE_COLUMNS[0].id
    const min = columnMinWidth(id)
    expect(clampColumnWidth(id, 0)).toBe(min)
    expect(clampColumnWidth(id, min - 1)).toBe(min)
  })

  it('clampColumnWidth rounds fractional widths', () => {
    const id = RESIZABLE_COLUMNS[0].id
    expect(clampColumnWidth(id, columnMinWidth(id) + 10.6)).toBe(columnMinWidth(id) + 11)
  })

  it('clampColumnWidth has no ceiling -- an absurdly large width passes through', () => {
    const id = RESIZABLE_COLUMNS[0].id
    expect(clampColumnWidth(id, 100_000)).toBe(100_000)
  })

  it('columnMinWidth falls back to 40 for an unknown column id', () => {
    expect(columnMinWidth('not-a-real-column')).toBe(40)
  })

  it('isColumnWidths accepts an object of finite numbers and rejects everything else', () => {
    expect(isColumnWidths({ size: 100, status: 128 })).toBe(true)
    expect(isColumnWidths({})).toBe(true)
    expect(isColumnWidths(null)).toBe(false)
    expect(isColumnWidths({ size: 'wide' })).toBe(false)
    expect(isColumnWidths({ size: Number.NaN })).toBe(false)
    expect(isColumnWidths({ size: Number.POSITIVE_INFINITY })).toBe(false)
  })

  describe('mergeColumnWidths', () => {
    it('returns plain defaults when nothing was saved', () => {
      expect(mergeColumnWidths(null)).toEqual(defaultColumnWidths())
    })

    it('applies a saved width for a known column, clamped to its minimum', () => {
      const id = RESIZABLE_COLUMNS[0].id
      const merged = mergeColumnWidths({ [id]: 1 })
      expect(merged[id]).toBe(columnMinWidth(id))
    })

    it('keeps the default for any column absent from the saved object', () => {
      const [first, second] = RESIZABLE_COLUMNS
      const merged = mergeColumnWidths({ [first.id]: first.defaultWidth + 20 })
      expect(merged[second.id]).toBe(second.defaultWidth)
    })

    it('silently drops a saved id that is no longer a real column', () => {
      const merged = mergeColumnWidths({ 'removed-column-from-a-past-version': 999 })
      expect(merged).toEqual(defaultColumnWidths())
      expect(merged['removed-column-from-a-past-version']).toBeUndefined()
    })

    // 2026-08-14 (prompts/2026-08-14-files-page-speed-column.md): a layout saved before the
    // Speed column existed has no 'speed' key at all -- simulates exactly that upgrade case
    // rather than relying on the generic "absent id" test above to happen to cover it.
    it('a layout persisted before the Speed column existed gets a sane default for it, ' +
      'and every pre-existing width survives untouched', () => {
      const preSpeedSaved = { size: 140, status: 150, lifecycle: 90, changed: 160, actions: 100 }
      const merged = mergeColumnWidths(preSpeedSaved)
      expect(merged.speed).toBe(defaultColumnWidths().speed)
      expect(merged.size).toBe(140)
      expect(merged.status).toBe(150)
      expect(merged.lifecycle).toBe(90)
      expect(merged.changed).toBe(160)
      expect(merged.actions).toBe(100)
      // Every value is a finite, renderable number -- an upgrade must not corrupt the header
      // into an unrenderable width for the one column that predates this saved layout.
      for (const width of Object.values(merged)) {
        expect(Number.isFinite(width)).toBe(true)
      }
    })
  })
})
