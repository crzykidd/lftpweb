import type { DiskReviewTorrentOut } from '../api/types'

/** Sorting/filtering/column-visibility for the per-client torrent table
 * (prompts/done/2026-08-24-disk-review-table-frontend.md). Pure functions only -- no React, no
 * fetch -- so every rule here (null-sorts-last, stability, capability-driven columns) is a unit
 * test rather than something only a screenshot can confirm.
 */

export type SortDirection = 'asc' | 'desc'

export type TorrentSortColumn =
  | 'transfer_name'
  | 'category'
  | 'file_count'
  | 'size_on_disk'
  | 'uploaded_bytes'
  | 'seed_time_s'
  | 'ratio'

export interface TorrentSortState {
  column: TorrentSortColumn
  direction: SortDirection
}

/** Compares two nullable values with **`null` sorting last regardless of direction** -- a
 * torrent with no ratio is not "the lowest ratio" (this codebase's established instinct: see
 * `COALESCE(queue_position, 1e18)` and the support-bundle log sort, both cited in this task's own
 * handoff prompt). `direction` only ever flips the *non-null* comparison; a null on either side
 * short-circuits before `direction` is consulted at all, which is what keeps it last on both an
 * ascending and a descending click rather than flipping to first on descending.
 */
function compareNullable<T>(
  a: T | null,
  b: T | null,
  direction: SortDirection,
  compare: (a: T, b: T) => number
): number {
  if (a === null && b === null) return 0
  if (a === null) return 1
  if (b === null) return -1
  const base = compare(a, b)
  return direction === 'asc' ? base : -base
}

const compareString = (a: string, b: string): number => a.localeCompare(b)
const compareNumber = (a: number, b: number): number => a - b

/** One column's own comparator, `null`-last in both directions per `compareNullable` above.
 * `transfer_name` is never `null` on the wire, but is still routed through `compareNullable` for
 * a single uniform shape rather than a special case for the one non-nullable column.
 */
const COLUMN_COMPARATORS: Record<
  TorrentSortColumn,
  (a: DiskReviewTorrentOut, b: DiskReviewTorrentOut, direction: SortDirection) => number
> = {
  transfer_name: (a, b, dir) => compareNullable(a.transfer_name, b.transfer_name, dir, compareString),
  category: (a, b, dir) => compareNullable(a.category, b.category, dir, compareString),
  file_count: (a, b, dir) => compareNullable(a.file_count, b.file_count, dir, compareNumber),
  size_on_disk: (a, b, dir) => compareNullable(a.size_on_disk, b.size_on_disk, dir, compareNumber),
  uploaded_bytes: (a, b, dir) => compareNullable(a.uploaded_bytes, b.uploaded_bytes, dir, compareNumber),
  seed_time_s: (a, b, dir) => compareNullable(a.seed_time_s, b.seed_time_s, dir, compareNumber),
  ratio: (a, b, dir) => compareNullable(a.ratio, b.ratio, dir, compareNumber),
}

/** Sorts a torrent table for one client section, per `TorrentSortState`. **Stable on equal
 * keys** -- decorate-sort-undecorate with the original index as the final tiebreaker, rather than
 * relying on `Array.prototype.sort`'s own stability guarantee implicitly, so a re-sort (clicking
 * the same header, or a fresh scan reordering upstream) never shuffles rows that compare equal.
 * Never mutates its input.
 */
export function sortTorrents(rows: DiskReviewTorrentOut[], state: TorrentSortState): DiskReviewTorrentOut[] {
  const compare = COLUMN_COMPARATORS[state.column]
  return rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const base = compare(a.row, b.row, state.direction)
      return base !== 0 ? base : a.index - b.index
    })
    .map((entry) => entry.row)
}

// --- The label filter (§4 of this task's own handoff prompt) ------------------------------
//
// **Global, not per-section** (docs/decisions.md has the full reasoning) -- the user's ask was
// "finding content across the seedbox," and a per-section filter would need its own state times
// however many clients exist for no benefit today, since there is exactly one seedbox host and a
// category name is a seedbox-wide concept, not a per-client one.

export interface CategoryLabel {
  category: string
  /** The attribution of the *first* torrent this category was seen on. Two torrents sharing a
   * category name are expected to share its attribution (attribution is a property of the
   * category's own three-state row, `download_client_category.excluded`/`queue_id`, not of the
   * individual torrent) -- see docs/decisions.md for why a first-seen pick, rather than
   * reconciling a disagreement, is the deliberately simple answer here.
   */
  attribution: string
}

/** Every distinct category present in the **whole** `torrents` array, sorted, for the filter's
 * own option list. **Deliberately takes the full, unfiltered array, never the currently-visible
 * subset** -- that is what keeps a label in the dropdown even when the current filter selection
 * (or a client section with nothing to show) has reduced its own visible row count to zero: the
 * option list is a function of the scan, not of the current view (this task's own "a label with
 * no rows in the current scan must still appear in the list" requirement).
 */
export function getCategoryLabels(torrents: DiskReviewTorrentOut[]): CategoryLabel[] {
  const seen = new Map<string, string>()
  for (const t of torrents) {
    if (t.category === null) continue
    if (!seen.has(t.category)) seen.set(t.category, t.attribution)
  }
  return Array.from(seen.entries())
    .map(([category, attribution]) => ({ category, attribution }))
    .sort((a, b) => a.category.localeCompare(b.category))
}

/** `label === null` is "All labels" -- every row passes. Otherwise keeps only rows whose own
 * `category` matches exactly; a `null`-category row never matches a specific label (it only ever
 * shows up under "All labels"), and there is no dedicated "uncategorized" filter option -- not
 * asked for, and `null` rows are already visible by default.
 */
export function filterTorrentsByLabel(
  torrents: DiskReviewTorrentOut[],
  label: string | null
): DiskReviewTorrentOut[] {
  if (label === null) return torrents
  return torrents.filter((t) => t.category === label)
}

/** Plain-language wording for an attribution state (this task's own "prefer plain language ...
 * over the internal state names") -- the one place that turns `'bound' | 'excluded' |
 * 'undecided'` into copy, so the filter list and the table's own category chip never drift apart.
 */
export function attributionLabel(attribution: string): string {
  switch (attribution) {
    case 'excluded':
      return 'Not monitored here'
    case 'undecided':
      return 'Unassigned'
    case 'bound':
      return 'Bound to a queue'
    default:
      return attribution
  }
}

// --- Capability-driven columns (§2 of this task's own handoff prompt) ---------------------

// Same seven-value domain as `TorrentSortColumn` above -- every sortable column is also a
// column that can be shown or hidden, so this is an alias, not a second declaration that could
// drift from the first.
export type TorrentColumn = TorrentSortColumn

/** Name/Label/Files/Size always render -- `file_count`/`size_on_disk` are disk-derived (the scan
 * itself walked the tree), not a client-reported field, so there is no capability to gate them
 * on; a `null` value in a rendered column already reads as "unknown" (per-cell, not a hidden
 * column) via the ordinary null-render rule.
 */
const ALWAYS_COLUMNS: readonly TorrentColumn[] = ['transfer_name', 'category', 'file_count', 'size_on_disk']

/** The three columns a connector can genuinely not have -- usenet has no ratio, no uploaded
 * bytes, no seed time (`USENET_BASELINE`, `backend/lftpweb/core/clients/base.py`). Order matches
 * this task's own table spec (Uploaded, Seeded, Ratio).
 */
const CAPABILITY_GATED_COLUMNS: readonly { column: TorrentColumn; capabilityKey: string }[] = [
  { column: 'uploaded_bytes', capabilityKey: 'uploaded_bytes' },
  { column: 'seed_time_s', capabilityKey: 'seed_time_s' },
  { column: 'ratio', capabilityKey: 'ratio' },
]

/** The capability-driven column set for one client's own section (spec §17.2's "the UI is driven
 * by the declaration, never by the client's name," applied here per this task's own instruction)
 * -- **takes only the flat capability map, never a `client_type` string**, so there is
 * structurally nowhere for an `if client_type === 'sabnzbd'` branch to go. A column is dropped
 * only when the client's own declaration says `'none'` for the matching field; `'native'`,
 * `'derived'`, or the field simply being absent (an unprobed client, `capabilities: {}`) all keep
 * the column -- "we don't know" is not the same claim as "declared unsupported," and hiding a
 * column on `'derived'` would render *arr-style seed-time caveats identically to lftpweb hiding
 * something arbitrarily.
 */
export function visibleTorrentColumns(capabilities: Record<string, string>): TorrentColumn[] {
  const gated = CAPABILITY_GATED_COLUMNS.filter((c) => capabilities[c.capabilityKey] !== 'none').map(
    (c) => c.column
  )
  return [...ALWAYS_COLUMNS, ...gated]
}
