import { describe, expect, it } from 'vitest'
import type { FileNode, LifecycleFacets } from '../api/types'
import {
  buildTree,
  clampColumnWidth,
  columnMinWidth,
  defaultColumnWidths,
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
  })
})
