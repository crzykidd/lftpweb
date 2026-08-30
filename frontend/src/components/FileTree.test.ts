import { describe, expect, it } from 'vitest'
import type { FileNode, LifecycleFacets } from '../api/types'
import type { ChildSpeedSample } from '../hooks/useLiveModel'
import {
  arrChipOverlay,
  arrHoverLabel,
  arrIconVariant,
  buildTree,
  canConfirmDelete,
  canDeleteLocal,
  CHILD_SPEED_FRESHNESS_MS,
  childDisplayState,
  clampColumnWidth,
  columnMinWidth,
  defaultColumnWidths,
  defaultSourceChecked,
  effectiveDeleteScope,
  effectiveEtaLabel,
  effectiveSpeedLabel,
  effectiveSpeedSortValue,
  flatten,
  freshChildSpeedBps,
  hasLocalContent,
  isCollapsePreference,
  isColumnWidths,
  isSortPreference,
  matchesFacetFilter,
  mergeColumnWidths,
  nodeDisplaySize,
  RESIZABLE_COLUMNS,
  resolveCollapsed,
  rowAction,
  shouldOfferLocalScope,
  shouldOfferSourceScope,
  showsCopyQueueSourceWarning,
  sortTree,
  stateProgressPercent,
  type TreeEntry,
} from '../lib/fileTree'

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
    arr_status: null,
    arr_status_at: null,
    client_instance_name: null,
    client_instance_kind: null,
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

  it('arr_tracked matches any non-null arr_status, and only that', () => {
    expect(matchesFacetFilter({ ...entry({}), arr_status: null }, 'arr_tracked')).toBe(false)
    for (const status of ['detected', 'notified', 'imported', 'cleaned', 'dropped', 'gone']) {
      expect(matchesFacetFilter({ ...entry({}), arr_status: status }, 'arr_tracked')).toBe(true)
    }
  })

  it('arr_gone matches only arr_status === "gone" -- "dropped" (2026-08-18) does NOT count, '
    + 'by design: it is a transient amber state, not the actionable one this filter targets', () => {
    expect(matchesFacetFilter({ ...entry({}), arr_status: 'gone' }, 'arr_gone')).toBe(true)
    for (const status of ['detected', 'notified', 'imported', 'cleaned', 'dropped', null]) {
      expect(matchesFacetFilter({ ...entry({}), arr_status: status }, 'arr_gone')).toBe(false)
    }
  })
})

// --- Sonarr/Radarr integration icon (docs/arr-integration-spec.md "UI") -------------------

describe('arrIconVariant', () => {
  it('maps all six known arr_status values to the spec\'s icon-state table', () => {
    expect(arrIconVariant('detected')).toBe('neutral')
    expect(arrIconVariant('notified')).toBe('neutral')
    expect(arrIconVariant('imported')).toBe('imported')
    expect(arrIconVariant('gone')).toBe('gone')
    expect(arrIconVariant('cleaned')).toBe('imported')
    expect(arrIconVariant('dropped')).toBe('dropped')
  })

  it('"dropped" (2026-08-18) gets its own variant, not folded into "gone" -- it is the amber '
    + '"rechecking" grace state, not yet the actionable red one', () => {
    expect(arrIconVariant('dropped')).not.toBe(arrIconVariant('gone'))
    expect(arrIconVariant('dropped')).not.toBe(arrIconVariant('imported'))
  })

  it('"cleaned" shares the green-check variant with "imported" (2026-08-16: with "Delete when '
    + 'imported" on, "imported" is a seconds-long transient, so the success check must survive '
    + 'into "cleaned" to ever be seen)', () => {
    expect(arrIconVariant('cleaned')).toBe(arrIconVariant('imported'))
  })

  it('is "none" for a null arr_status -- no bound instance, or not yet matched', () => {
    expect(arrIconVariant(null)).toBe('none')
  })

  it('degrades an unrecognized status string to the neutral mark rather than nothing', () => {
    expect(arrIconVariant('some_future_status')).toBe('neutral')
  })
})

describe('arrHoverLabel', () => {
  it('is null when arr_status itself is null -- nothing to show', () => {
    expect(arrHoverLabel({ arr_status: null, arr_status_at: null }, 'Sonarr')).toBeNull()
  })

  it('names the instance when one is known', () => {
    const label = arrHoverLabel({ arr_status: 'imported', arr_status_at: null }, 'Sonarr')
    expect(label).toContain('Sonarr')
    expect(label).toContain('imported')
  })

  it('falls back to a generic name when the instance is not known', () => {
    const label = arrHoverLabel({ arr_status: 'gone', arr_status_at: null }, null)
    expect(label).toContain('the bound *arr instance')
  })

  it('includes a relative time when arr_status_at is set', () => {
    const label = arrHoverLabel(
      { arr_status: 'detected', arr_status_at: new Date(Date.now() - 60_000).toISOString() },
      'Radarr',
    )
    expect(label).toMatch(/\(.*\)/)
  })

  it('"imported" and "cleaned" share an icon variant but keep distinct hover text', () => {
    const imported = arrHoverLabel({ arr_status: 'imported', arr_status_at: null }, 'Sonarr')
    const cleaned = arrHoverLabel({ arr_status: 'cleaned', arr_status_at: null }, 'Sonarr')
    expect(imported).not.toBe(cleaned)
    expect(cleaned).toContain('cleaned up')
  })

  it('"dropped" (2026-08-18) embeds the relative time inline -- "removed ... <time> ago -- '
    + 'rechecking" -- rather than the generic "statusText (when)" shape every other status uses', () => {
    const label = arrHoverLabel(
      { arr_status: 'dropped', arr_status_at: new Date(Date.now() - 5 * 60_000).toISOString() },
      'Sonarr',
    )
    expect(label).toContain('Sonarr')
    expect(label).toContain("removed from the *arr's queue")
    expect(label).toContain('rechecking')
    expect(label).not.toMatch(/\(.*ago.*\)/) // not the generic "(Xm ago)" parenthetical shape
  })

  it('"dropped" still reads sensibly with no arr_status_at yet', () => {
    const label = arrHoverLabel({ arr_status: 'dropped', arr_status_at: null }, null)
    expect(label).toContain('the bound *arr instance')
    expect(label).toContain('rechecking')
  })
})

// --- Sonarr/Radarr row chip (Files + Transfers + History, 2026-08-16,
// prompts/2026-08-16-arr-chip-on-row-lines.md, prompts/2026-08-16-files-brand-logo-icons.md) --
// `ArrRowChip` (`LifecycleIcons.tsx`) is the one component all three surfaces render on their
// row line; `arrChipOverlay` is its status-to-overlay mapping, shared verbatim -- no per-surface
// branch anywhere in this function, so a color asserted here is the color every surface shows.

describe('arrChipOverlay', () => {
  it('all six arr_status values (via arrIconVariant) map to the row chip\'s overlay per the spec', () => {
    // detected/notified -- mid-flight, logo alone, no overlay
    expect(arrChipOverlay(arrIconVariant('detected'))).toBeNull()
    expect(arrChipOverlay(arrIconVariant('notified'))).toBeNull()
    // imported/cleaned -- the *arr processed it: green check
    expect(arrChipOverlay(arrIconVariant('imported'))).toBe('check')
    expect(arrChipOverlay(arrIconVariant('cleaned'))).toBe('check')
    // dropped (2026-08-18) -- left the queue moments ago, rechecking: amber pending
    expect(arrChipOverlay(arrIconVariant('dropped'))).toBe('pending')
    // gone -- unconfirmed past the grace window: red warn
    expect(arrChipOverlay(arrIconVariant('gone'))).toBe('warn')
  })

  it('"dropped" is amber "pending", distinct from both "gone"\'s red warn and "imported"\'s '
    + 'green check', () => {
    const pending = arrChipOverlay(arrIconVariant('dropped'))
    expect(pending).toBe('pending')
    expect(pending).not.toBe(arrChipOverlay(arrIconVariant('gone')))
    expect(pending).not.toBe(arrChipOverlay(arrIconVariant('imported')))
    expect(pending).not.toBeNull()
  })

  it('a null arr_status resolves to the "none" variant, which the caller uses to render no chip at all', () => {
    expect(arrIconVariant(null)).toBe('none')
    expect(arrChipOverlay(arrIconVariant(null))).toBeNull()
  })

  it('an unrecognized future status degrades to the neutral variant -- logo alone, no overlay', () => {
    expect(arrChipOverlay(arrIconVariant('some_future_status'))).toBeNull()
  })

  it('"gone" resolves to the red "warn" overlay, not the job-detail drawer icon\'s amber -- ' +
    'the Files row chip (2026-08-16, prompts/2026-08-16-files-brand-logo-icons.md) reads this ' +
    'exact value, same as Transfers/History, so "gone" is red on all three surfaces now', () => {
    expect(arrChipOverlay(arrIconVariant('gone'))).toBe('warn')
    expect(arrChipOverlay(arrIconVariant('gone'))).not.toBeNull()
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

// --- rowAction -----------------------------------------------------------------------------
// 2026-08-14 (prompts/2026-08-14-hide-queue-when-there-is-no-remote-copy.md): the Files page
// still offered "Queue" on a row with nothing remote to fetch -- a REMOVED_BOTH child, and a
// move-mode parent whose remote this codebase deleted itself. `rowAction` now gates on
// `hasRemoteCopy` (remote_size != null) generally rather than testing the single `LOCAL_ONLY`
// state string.

describe('rowAction', () => {
  it('offers nothing for a REMOVED_BOTH row with no remote copy', () => {
    expect(rowAction(node('gone/child.mkv', false, { state: 'REMOVED_BOTH', remote_size: null }))).toBeNull()
  })

  it('offers nothing for a move-mode parent whose verified remote copy was deleted on purpose ' +
    '(remote_size null, remote_deleted_at set, state left at VERIFIED/EXTRACTED)', () => {
    const deletedParent = node('release', true, {
      state: 'EXTRACTED',
      remote_size: null,
      local_size: 12345,
      remote_deleted_at: '2026-08-14T00:00:00Z',
    })
    expect(rowAction(deletedParent)).toBeNull()
  })

  it('still offers nothing for LOCAL_ONLY, now via the general no-remote-copy rule', () => {
    expect(rowAction(node('local-only-file.txt', false, { state: 'LOCAL_ONLY', remote_size: null }))).toBeNull()
  })

  it('still offers redownload for a row we deleted locally whose remote copy has come back', () => {
    const backAgain = node('came-back', true, {
      state: 'REMOVED_BOTH',
      suppressed_reason: 'deleted_local',
      remote_size: 500,
    })
    expect(rowAction(backAgain)).toBe('redownload')
  })

  it('still offers queue for STOPPED with a remote copy -- manual queueing stays unfiltered by suppression', () => {
    expect(
      rowAction(node('stopped.iso', false, { state: 'STOPPED', remote_size: 1000, local_size: 200 })),
    ).toBe('queue')
  })

  it('still offers queue for FAILED with a remote copy', () => {
    expect(rowAction(node('failed.iso', false, { state: 'FAILED', remote_size: 1000, local_size: 0 }))).toBe('queue')
  })

  it('still offers stop for QUEUED/DOWNLOADING regardless of remote presence', () => {
    expect(rowAction(node('q.iso', false, { state: 'QUEUED', remote_size: 1000 }))).toBe('stop')
    expect(rowAction(node('d.iso', false, { state: 'DOWNLOADING', remote_size: 1000 }))).toBe('stop')
  })

  it('offers nothing for a row with no id', () => {
    expect(rowAction(node('unpersisted.iso', false, { id: null, remote_size: 1000 }))).toBeNull()
  })
})

// --- canDeleteLocal / hasLocalContent / shouldOfferLocalScope ------------------------------
// 2026-08-17 (prompts/2026-08-17-stranded-source-delete-retry.md): a failed rung-4 deferred
// source delete used to strand the remote copy with no Delete affordance at all -- a
// `REMOVED_LOCAL` row (no local content) had its only escape hatch, the Source scope, hidden
// because `canDeleteLocal` only ever asked about local content. Widened to "local content OR a
// surviving remote copy," pinned here directly against the two shapes the incident produced.

describe('canDeleteLocal', () => {
  it('offers Delete for a REMOVED_LOCAL row with a surviving remote copy (the stranded rung-4 case)', () => {
    const stranded = node('stranded-release', true, { state: 'REMOVED_LOCAL', remote_size: 5000 })
    expect(canDeleteLocal(stranded)).toBe(true)
  })

  it('offers nothing for a REMOVED_BOTH row -- nothing anywhere to delete', () => {
    const goneEverywhere = node('gone-everywhere', true, { state: 'REMOVED_BOTH', remote_size: null })
    expect(canDeleteLocal(goneEverywhere)).toBe(false)
  })

  it('still offers Delete for the ordinary case: local content, no remote copy', () => {
    expect(canDeleteLocal(node('local.iso', false, { state: 'DOWNLOADED', remote_size: null, local_size: 1000 }))).toBe(true)
  })

  it('offers nothing for a row with no id, even with a remote copy', () => {
    expect(canDeleteLocal(node('unpersisted.iso', false, { id: null, remote_size: 1000 }))).toBe(false)
  })
})

describe('hasLocalContent', () => {
  it('is false for REMOVED_LOCAL regardless of remote presence', () => {
    expect(hasLocalContent(node('x', true, { state: 'REMOVED_LOCAL', remote_size: 5000 }))).toBe(false)
  })

  it('is true for an ordinary downloaded state', () => {
    expect(hasLocalContent(node('x', false, { state: 'DOWNLOADED' }))).toBe(true)
  })
})

describe('shouldOfferLocalScope', () => {
  it('offers the Local checkbox when at least one pending entry has local content', () => {
    const entries = [
      node('a', true, { state: 'REMOVED_LOCAL', remote_size: 5000 }),
      node('b', false, { state: 'DOWNLOADED' }),
    ]
    expect(shouldOfferLocalScope(entries)).toBe(true)
  })

  it('hides the Local checkbox for a selection made entirely of stranded no-local-content rows', () => {
    const entries = [
      node('a', true, { state: 'REMOVED_LOCAL', remote_size: 5000 }),
      node('b', true, { state: 'REMOVED_LOCAL', remote_size: 2000 }),
    ]
    expect(shouldOfferLocalScope(entries)).toBe(false)
  })

  it('hides the Local checkbox for an empty selection', () => {
    expect(shouldOfferLocalScope([])).toBe(false)
  })
})

// --- The delete dialog's Local/Source scopes ------------------------------------------------
// 2026-08-16 (prompts/2026-08-16-manual-delete-local-and-remote.md, settled design): the
// first manual remote-delete path in the app. Pure functions only -- the JSX/state wiring
// (checkboxes, the §7.1 warning banner) lives in `FileTree.tsx` itself and isn't exercised
// here, per this module's own "no React, no hooks, no DOM" scope.

describe('defaultSourceChecked', () => {
  it('checks Source by default for a move queue with a remote copy', () => {
    expect(defaultSourceChecked('move', true)).toBe(true)
  })

  it('leaves Source unchecked for a move queue with no remote copy at all', () => {
    expect(defaultSourceChecked('move', false)).toBe(false)
  })

  it('leaves Source unchecked by default for a copy queue, even with a remote copy (§7.1)', () => {
    expect(defaultSourceChecked('copy', true)).toBe(false)
  })

  it('leaves Source unchecked by default for the unbuilt sync mode', () => {
    expect(defaultSourceChecked('sync', true)).toBe(false)
  })
})

describe('shouldOfferSourceScope', () => {
  it('offers the Source checkbox when at least one pending entry has a remote copy', () => {
    const entries = [
      node('a', false, { remote_size: null }),
      node('b', false, { remote_size: 1000 }),
    ]
    expect(shouldOfferSourceScope(entries)).toBe(true)
  })

  it('hides the Source checkbox when nothing pending has a remote copy', () => {
    const entries = [
      node('a', false, { remote_size: null }),
      node('b', false, { remote_size: null }),
    ]
    expect(shouldOfferSourceScope(entries)).toBe(false)
  })

  it('hides the Source checkbox for an empty selection', () => {
    expect(shouldOfferSourceScope([])).toBe(false)
  })
})

describe('canConfirmDelete', () => {
  it('allows local-only, the pre-existing behavior', () => {
    expect(canConfirmDelete(true, false)).toBe(true)
  })

  it('allows source-only, the new behavior', () => {
    expect(canConfirmDelete(false, true)).toBe(true)
  })

  it('allows both checked', () => {
    expect(canConfirmDelete(true, true)).toBe(true)
  })

  it('refuses neither checked', () => {
    expect(canConfirmDelete(false, false)).toBe(false)
  })
})

describe('showsCopyQueueSourceWarning', () => {
  it('warns on a copy queue when Source is checked', () => {
    expect(showsCopyQueueSourceWarning('copy', true)).toBe(true)
  })

  it('does not warn on a copy queue when Source is unchecked', () => {
    expect(showsCopyQueueSourceWarning('copy', false)).toBe(false)
  })

  it('does not warn on a move queue even when Source is checked', () => {
    expect(showsCopyQueueSourceWarning('move', true)).toBe(false)
  })

  it('warns for the unbuilt sync mode too, same as copy', () => {
    expect(showsCopyQueueSourceWarning('sync', true)).toBe(true)
  })
})

// --- effectiveDeleteScope ------------------------------------------------------------------
// 2026-08-17 (prompts/2026-08-17-bulk-delete-per-entry-scopes.md): the live-reported bug -- a
// bulk delete sent a blanket `local: true` to every selected row, so any row with no local
// content (a `REMOTE_ONLY` row, or a stranded `REMOVED_LOCAL` row made selectable by
// `canDeleteLocal`'s own 2026-08-17 widening) 409'd on the local withhold before its source
// delete was ever attempted. `effectiveDeleteScope` is the per-entry fix -- pinned here against
// the truth table plus the exact mixed-selection shape the report described.

describe('effectiveDeleteScope', () => {
  const both = node('both.iso', false, { state: 'DOWNLOADING', remote_size: 1000, local_size: 500 })
  const remoteOnly = node('remote-only', true, { state: 'REMOTE_ONLY', remote_size: 5000, local_size: null })
  const localOnly = node('local-only.iso', false, { state: 'DOWNLOADED', remote_size: null, local_size: 1000 })

  it('local-content+remote row: neither checked -> null (no request at all)', () => {
    expect(effectiveDeleteScope(both, { local: false, source: false })).toBeNull()
  })

  it('local-content+remote row: Local only checked -> local only requested', () => {
    expect(effectiveDeleteScope(both, { local: true, source: false })).toEqual({ local: true, source: false })
  })

  it('local-content+remote row: Source only checked -> source only requested', () => {
    expect(effectiveDeleteScope(both, { local: false, source: true })).toEqual({ local: false, source: true })
  })

  it('local-content+remote row: both checked -> both requested', () => {
    expect(effectiveDeleteScope(both, { local: true, source: true })).toEqual({ local: true, source: true })
  })

  it('REMOTE_ONLY row with both checked -> source only, never local (the bug this fixes)', () => {
    expect(effectiveDeleteScope(remoteOnly, { local: true, source: true })).toEqual({ local: false, source: true })
  })

  it('REMOTE_ONLY row with Local only checked -> null, no request at all', () => {
    expect(effectiveDeleteScope(remoteOnly, { local: true, source: false })).toBeNull()
  })

  it('local-only row with Source only checked -> null, no request at all', () => {
    expect(effectiveDeleteScope(localOnly, { local: false, source: true })).toBeNull()
  })

  it('local-only row with both checked -> local only requested', () => {
    expect(effectiveDeleteScope(localOnly, { local: true, source: true })).toEqual({ local: true, source: false })
  })

  it('regression: mixed selection (a local-content row plus a REMOTE_ONLY row), both boxes ' +
    'checked -- the remote-only row is never sent local: true', () => {
    const checked = { local: true, source: true }
    const localRow = node('local-content-row', false, { state: 'DOWNLOADED', remote_size: 2000, local_size: 2000 })
    const remoteOnlyRow = node('remote-only-row', true, { state: 'REMOTE_ONLY', remote_size: 3000, local_size: null })
    expect(effectiveDeleteScope(localRow, checked)).toEqual({ local: true, source: true })
    expect(effectiveDeleteScope(remoteOnlyRow, checked)).toEqual({ local: false, source: true })
  })
})

// `stateProgressPercent`/`nodeDisplaySize`/`freshChildSpeedBps` (2026-08-20, docs/transfers-
// redesign-spec.md §3.3 stage 5): all three moved/widened so the Transfers row's own file-list
// expansion (`lib/transferPanel.ts`) can share them rather than forking a second copy -- see
// each function's own docstring in `lib/fileTree.ts`.
describe('stateProgressPercent', () => {
  it('DOWNLOADING and PARTIAL are the only states with a meaningful percent', () => {
    expect(stateProgressPercent('DOWNLOADING', 50, 100)).toBe(50)
    expect(stateProgressPercent('PARTIAL', 25, 100)).toBe(25)
  })

  it('every other state reads null regardless of the sizes', () => {
    expect(stateProgressPercent('REMOTE_ONLY', 0, 100)).toBeNull()
    expect(stateProgressPercent('DOWNLOADED', 100, 100)).toBeNull()
    expect(stateProgressPercent('EXCLUDED', null, null)).toBeNull()
  })

  // `QUEUED` (2026-08-21, prompts/done/2026-08-21-paused-item-progress.md, issue #14's second
  // half): a paused-in-place or freshly-retried-after-interruption row that already has real
  // partial bytes on disk shows that progress -- gated on `localSize` genuinely being nonzero,
  // not merely known, since `percentValue(0, total)` would otherwise resolve to `0`, not `null`.
  it('QUEUED reads the percent when the row genuinely has partial bytes on disk', () => {
    expect(stateProgressPercent('QUEUED', 45, 100)).toBe(45)
  })

  it('QUEUED reads null with no local bytes at all -- a plain never-started queued row', () => {
    expect(stateProgressPercent('QUEUED', null, 100)).toBeNull()
    expect(stateProgressPercent('QUEUED', 0, 100)).toBeNull()
  })

  it('QUEUED reads null with no known remote size -- no honest denominator to show', () => {
    expect(stateProgressPercent('QUEUED', 45, null)).toBeNull()
  })
})

// `childDisplayState` (2026-08-21, prompts/done/2026-08-21-child-state-and-active-box-height.md)
// -- the shared mapping `TransfersPage.tsx`'s Queue-row file-list expansion (`FileListRow`) and
// `ItemDrawer.tsx`'s per-file `Row` both call, so a partly-transferred child of a *running* job
// reads "Downloading" rather than the structurally-correct-but-misleading "Partial". The matrix
// here is the exact one the user's own report and the handoff prompt both called out as easy to
// get wrong (blanket-mapping every child of a running job, not just the one actually in flight).
describe('childDisplayState', () => {
  it('maps PARTIAL to DOWNLOADING only when the job is running', () => {
    expect(childDisplayState('PARTIAL', true)).toBe('DOWNLOADING')
  })

  it('leaves a PARTIAL child alone when its job is not running -- stopped/failed/paused', () => {
    expect(childDisplayState('PARTIAL', false)).toBe('PARTIAL')
  })

  it('never touches a complete child, running job or not', () => {
    expect(childDisplayState('DOWNLOADED', true)).toBe('DOWNLOADED')
    expect(childDisplayState('DOWNLOADED', false)).toBe('DOWNLOADED')
  })

  it('never touches an untouched child, running job or not', () => {
    expect(childDisplayState('REMOTE_ONLY', true)).toBe('REMOTE_ONLY')
    expect(childDisplayState('REMOTE_ONLY', false)).toBe('REMOTE_ONLY')
  })

  it('passes through any other state unchanged', () => {
    expect(childDisplayState('EXCLUDED', true)).toBe('EXCLUDED')
    expect(childDisplayState('FAILED', true)).toBe('FAILED')
  })
})

describe('nodeDisplaySize', () => {
  it('a file prefers local_size, falling back to remote_size', () => {
    expect(nodeDisplaySize({ is_dir: false, local_size: 10, remote_size: 20 })).toBe(10)
    expect(nodeDisplaySize({ is_dir: false, local_size: null, remote_size: 20 })).toBe(20)
  })

  it('a directory prefers remote_size, falling back to local_size', () => {
    expect(nodeDisplaySize({ is_dir: true, local_size: 10, remote_size: 20 })).toBe(20)
    expect(nodeDisplaySize({ is_dir: true, local_size: 10, remote_size: null })).toBe(10)
  })

  it('accepts a plain FileNode, not just a full TreeEntry', () => {
    const plain: FileNode = node('a.mkv', false, { local_size: 5, remote_size: 10 })
    expect(nodeDisplaySize(plain)).toBe(5)
  })
})

describe('freshChildSpeedBps', () => {
  it('an absent sample reads null', () => {
    expect(freshChildSpeedBps(undefined, 1000)).toBeNull()
  })

  it('a sample within the freshness window reads its rate', () => {
    const sample: ChildSpeedSample = { speedBps: 1234, receivedAt: 1000 }
    expect(freshChildSpeedBps(sample, 1000 + CHILD_SPEED_FRESHNESS_MS)).toBe(1234)
  })

  it('a sample older than the freshness window reads null', () => {
    const sample: ChildSpeedSample = { speedBps: 1234, receivedAt: 1000 }
    expect(freshChildSpeedBps(sample, 1000 + CHILD_SPEED_FRESHNESS_MS + 1)).toBeNull()
  })
})
