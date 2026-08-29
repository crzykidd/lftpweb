import { useState } from 'react'
import { runDiskReviewScan } from '../api/client'
import type {
  DiskReviewClientOut,
  DiskReviewDebrisOut,
  DiskReviewScanResponse,
  DiskReviewTorrentOut,
} from '../api/types'
import {
  type DebrisGroup,
  type ExcludedContentGroup,
  filesByClaimKey,
  formatRatio,
  formatSeedTime,
  freedBytes,
  groupDebrisByDirectory,
  groupExcludedContentByDirectory,
  groupUnclaimedByDirectory,
  type SeedingEstateGroup,
  type UnclaimedGroup,
} from '../lib/diskReview'
import {
  attributionLabel,
  filterTorrentsByLabel,
  getCategoryLabels,
  sortTorrents,
  visibleTorrentColumns,
  type TorrentColumn,
  type TorrentSortColumn,
  type TorrentSortState,
} from '../lib/diskReviewSort'
import { formatBytes } from '../lib/format'

// The chip pattern this page reuses everywhere a small, truncated label is needed (client-type
// chip, category chip) -- copied verbatim from `components/PreflightBox.tsx`'s own queue tag
// (~line 148) per this task's own "a small clip like we use in downloads" instruction, rather
// than inventing a second chip style.
const CHIP_CLASS =
  'max-w-[8rem] shrink-0 truncate rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400'

const COLUMN_LABEL: Record<TorrentColumn, string> = {
  transfer_name: 'Name',
  category: 'Label',
  file_count: 'Files',
  size_on_disk: 'Size',
  uploaded_bytes: 'Uploaded',
  seed_time_s: 'Seeded',
  ratio: 'Ratio',
}

// Right-aligned, numeric-figure columns -- every column except Name/Label.
const NUMERIC_COLUMNS = new Set<TorrentColumn>(['file_count', 'size_on_disk', 'uploaded_bytes', 'seed_time_s', 'ratio'])

/** The category chip, toned by its claim's own `attribution` (spec §17.6/§11.1e's three-state
 * category, copied onto every claim purely for display) -- "not monitored here" needs to be
 * legible on the chip itself, not only in the filter dropdown, per this task's own instruction.
 * `category === null` (a transfer with no category at all) is a different fact from an excluded
 * category and gets its own muted "No category" reading rather than being folded into either
 * attribution tone.
 */
function CategoryChip({ category, attribution }: { category: string | null; attribution: string }) {
  if (category === null) {
    return <span className={CHIP_CLASS}>No category</span>
  }
  const tone =
    attribution === 'excluded'
      ? 'bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300'
      : attribution === 'undecided'
        ? 'bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300'
        : 'bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400'
  return (
    <span className={`max-w-[8rem] shrink-0 truncate rounded px-1.5 py-0.5 text-[10px] font-medium ${tone}`} title={`${category} — ${attributionLabel(attribution)}`}>
      {category}
    </span>
  )
}

/** One torrent's own row plus its (initially collapsed) file expansion -- the same shape the
 * pre-existing `SeedingEstateGroupRow` expansion used, per this task's own instruction, now fed
 * by `claim_key` instead of a torrent identity re-derived from three separate fields.
 */
function TorrentRow({
  torrent,
  columns,
  files,
  expanded,
  onToggleExpand,
}: {
  torrent: DiskReviewTorrentOut
  columns: TorrentColumn[]
  files: SeedingEstateGroup | undefined
  expanded: boolean
  onToggleExpand: () => void
}) {
  const cell = (column: TorrentColumn) => {
    switch (column) {
      case 'transfer_name':
        return null // rendered separately below -- it carries the expand control and markers
      case 'category':
        return <CategoryChip category={torrent.category} attribution={torrent.attribution} />
      case 'file_count':
        return torrent.file_count === null ? '—' : torrent.file_count
      case 'size_on_disk':
        return torrent.size_on_disk === null ? '—' : formatBytes(torrent.size_on_disk)
      case 'uploaded_bytes':
        return torrent.uploaded_bytes === null ? '—' : formatBytes(torrent.uploaded_bytes)
      case 'seed_time_s':
        return formatSeedTime(torrent.seed_time_s)
      case 'ratio':
        return formatRatio(torrent.ratio)
    }
  }
  return (
    <>
      <tr className="border-t border-zinc-100 align-top dark:border-zinc-900">
        {columns.includes('transfer_name') && (
          <td className="min-w-0 px-3 py-2">
            <button
              type="button"
              onClick={onToggleExpand}
              aria-expanded={expanded}
              className="flex min-w-0 items-center gap-2 text-left hover:underline"
            >
              <span
                aria-hidden="true"
                className={`shrink-0 text-zinc-400 transition-transform dark:text-zinc-600 ${expanded ? 'rotate-90' : ''}`}
              >
                ▸
              </span>
              <span className="min-w-0 max-w-xs truncate font-medium text-zinc-700 dark:text-zinc-200" title={torrent.transfer_name}>
                {torrent.transfer_name}
              </span>
            </button>
            {/* The two states that need their own visible marker rather than a blank cell
             * (this task's own §2): the client claims it but nothing was found on disk, vs. the
             * client reported no path at all so Files/Size are genuinely unknown, not zero. */}
            {torrent.missing_on_disk && (
              <span className="ml-6 mt-0.5 block w-fit rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-700 dark:bg-red-950/40 dark:text-red-300">
                Missing on disk
              </span>
            )}
            {torrent.content_path === null && (
              <span className="ml-6 mt-0.5 block w-fit rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
                No path reported
              </span>
            )}
          </td>
        )}
        {columns
          .filter((c) => c !== 'transfer_name')
          .map((column) => (
            <td
              key={column}
              className={`px-3 py-2 whitespace-nowrap ${NUMERIC_COLUMNS.has(column) ? 'text-right' : ''}`}
            >
              {cell(column)}
            </td>
          ))}
      </tr>
      {expanded && (
        <tr className="border-t border-zinc-100 dark:border-zinc-900">
          <td colSpan={columns.length} className="bg-zinc-50/60 px-3 py-2 dark:bg-zinc-900/40">
            {files === undefined || files.entries.length === 0 ? (
              <p className="pl-6 text-xs text-zinc-400">No files found on disk for this claim.</p>
            ) : (
              <table className="w-full text-xs">
                <tbody>
                  {files.entries.map((f) => (
                    <tr key={f.abs_path}>
                      <td className="py-0.5 pl-6 font-mono break-all text-zinc-500 dark:text-zinc-400">{f.rel_path}</td>
                      <td className="w-24 py-0.5 text-right whitespace-nowrap text-zinc-500 dark:text-zinc-400">
                        {formatBytes(f.size)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </td>
        </tr>
      )}
    </>
  )
}

/** One download-client section (this task's own §1: "there is exactly one seedbox host in this
 * product today ... so the client is the only grouping axis"). Sort state and row-expand state
 * are local to this component, deliberately -- **sorting one client's table must never reorder
 * another's** (this task's own §3), and a `useState` scoped to this component is the simplest
 * thing that structurally guarantees that, with no shared key space to get wrong.
 */
function ClientTorrentSection({
  client,
  torrents,
  files,
  selectedLabel,
}: {
  client: DiskReviewClientOut
  torrents: DiskReviewTorrentOut[]
  files: Map<string, SeedingEstateGroup>
  selectedLabel: string | null
}) {
  const [sortState, setSortState] = useState<TorrentSortState>({ column: 'transfer_name', direction: 'asc' })
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const toggleExpand = (claimKey: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(claimKey)) next.delete(claimKey)
      else next.add(claimKey)
      return next
    })
  }

  const onSortClick = (column: TorrentSortColumn) => {
    setSortState((prev) =>
      prev.column === column ? { column, direction: prev.direction === 'asc' ? 'desc' : 'asc' } : { column, direction: 'asc' }
    )
  }

  const columns = visibleTorrentColumns(client.capabilities)
  const filtered = filterTorrentsByLabel(torrents, selectedLabel)
  const sorted = sortTorrents(filtered, sortState)

  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-medium text-zinc-900 dark:text-zinc-100">{client.name}</h2>
        <span className={CHIP_CLASS} title={client.client_type}>
          {client.client_type}
        </span>
        {!client.reachable && (
          <span className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-700 dark:bg-red-950/40 dark:text-red-300">
            Did not report this pass
          </span>
        )}
      </div>

      {!client.reachable ? (
        <p className="text-sm text-zinc-500 dark:text-zinc-400">
          {client.failure_reason ?? 'This client did not report this pass, and no reason was given.'}
        </p>
      ) : torrents.length === 0 ? (
        <p className="text-sm text-zinc-400">No torrents reported this pass.</p>
      ) : sorted.length === 0 ? (
        <p className="text-sm text-zinc-400">No torrents match this label for {client.name}.</p>
      ) : (
        // The trap this repo has hit twice (this task's own §6): jsdom performs no layout at
        // all, so the only defense against a wide table's rightmost column getting clipped is
        // this container's own `overflow-x-auto` -- never the `overflow-hidden` rounded wrapper
        // the debris/unclaimed tables below use, which is the exact shape that caused the
        // earlier bug on a table this wide.
        <div className="overflow-x-auto rounded-md border border-zinc-200 dark:border-zinc-800">
          <table className="w-full min-w-[40rem] text-sm">
            <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
              <tr>
                {columns.map((column) => (
                  <th
                    key={column}
                    className={`px-3 py-2 font-medium whitespace-nowrap ${NUMERIC_COLUMNS.has(column) ? 'text-right' : ''}`}
                  >
                    <button
                      type="button"
                      onClick={() => onSortClick(column)}
                      className="inline-flex items-center gap-1 hover:text-zinc-700 dark:hover:text-zinc-200"
                    >
                      {COLUMN_LABEL[column]}
                      {sortState.column === column && <span aria-hidden="true">{sortState.direction === 'asc' ? '▲' : '▼'}</span>}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map((t) => (
                <TorrentRow
                  key={t.claim_key}
                  torrent={t}
                  columns={columns}
                  files={files.get(t.claim_key)}
                  expanded={expanded.has(t.claim_key)}
                  onToggleExpand={() => toggleExpand(t.claim_key)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

/** One debris group's header row -- directory, file count, and its own link-aware reclaim total
 * (`freedBytes(group.entries, selected)`, the *global* selection set -- see `lib/diskReview.ts.
 * groupDebrisByDirectory`'s own docstring for why this is never a naive per-group sum). Expands
 * to the group's own files, each still individually selectable exactly as before this task --
 * grouping changes only how the list is organised, never what a selection means.
 */
function DebrisGroupRow({
  group,
  selected,
  expanded,
  onToggleExpand,
  onToggleFile,
  onToggleGroup,
}: {
  group: DebrisGroup
  selected: Set<string>
  expanded: boolean
  onToggleExpand: () => void
  onToggleFile: (path: string) => void
  onToggleGroup: (rows: DiskReviewDebrisOut[]) => void
}) {
  const groupTotal = freedBytes(group.entries, selected)
  const allSelected = group.entries.every((e) => selected.has(e.abs_path))
  return (
    <>
      <tr className="border-t border-zinc-100 bg-zinc-50/60 dark:border-zinc-900 dark:bg-zinc-900/40">
        <td className="px-3 py-2">
          <input type="checkbox" checked={allSelected} onChange={() => onToggleGroup(group.entries)} />
        </td>
        <td colSpan={3} className="px-3 py-2">
          <button
            type="button"
            onClick={onToggleExpand}
            aria-expanded={expanded}
            className="flex items-center gap-2 text-left font-medium text-zinc-700 hover:underline dark:text-zinc-200"
          >
            <span
              aria-hidden="true"
              className={`text-zinc-400 transition-transform dark:text-zinc-600 ${expanded ? 'rotate-90' : ''}`}
            >
              ▸
            </span>
            <span className="font-mono text-xs break-all">{group.directory}</span>
            <span className="text-xs font-normal text-zinc-500 dark:text-zinc-400">
              {group.entries.length} file{group.entries.length === 1 ? '' : 's'} — {formatBytes(groupTotal)}
              {' selected'}
            </span>
          </button>
        </td>
      </tr>
      {expanded &&
        group.entries.map((d) => (
          <tr key={d.abs_path} className="border-t border-zinc-100 align-top dark:border-zinc-900">
            <td className="px-3 py-2">
              <input type="checkbox" checked={selected.has(d.abs_path)} onChange={() => onToggleFile(d.abs_path)} />
            </td>
            <td className="px-3 py-2 pl-8 font-mono text-xs break-all">{d.rel_path}</td>
            <td className="px-3 py-2 whitespace-nowrap">{formatBytes(d.size)}</td>
            <td className="px-3 py-2 text-xs text-zinc-500 dark:text-zinc-400">
              {d.link_paths.length > 1 ? `${d.link_paths.length} linked copies` : '--'}
            </td>
          </tr>
        ))}
    </>
  )
}

/** One directory's worth of the unclaimed pile (spec §11.1d, finding #17, 2026-08-23) --
 * **deliberately has no checkbox column at all**, unlike `DebrisGroupRow` above. This pile is
 * visible but not selectable through the ordinary select-and-remove flow: ownership here is
 * genuinely undeterminable, and nothing acts on it until stage 5 builds its own, separate gate
 * (see this task's own `docs/decisions.md` entry). `allAbsPaths` is the *entire* unclaimed pile's
 * own path set, passed to `freedBytes` as "selected" so this group's reclaim figure answers "if
 * this were all resolved," staying link-aware the same way debris's running total does (spec
 * §10.5) -- there is no partial selection to reflect here.
 */
function UnclaimedGroupRow({
  group,
  allAbsPaths,
  expanded,
  onToggleExpand,
}: {
  group: UnclaimedGroup
  allAbsPaths: Set<string>
  expanded: boolean
  onToggleExpand: () => void
}) {
  const groupTotal = freedBytes(group.entries, allAbsPaths)
  return (
    <>
      <tr className="border-t border-zinc-100 bg-zinc-50/60 dark:border-zinc-900 dark:bg-zinc-900/40">
        <td colSpan={3} className="px-3 py-2">
          <button
            type="button"
            onClick={onToggleExpand}
            aria-expanded={expanded}
            className="flex flex-wrap items-center gap-2 text-left font-medium text-zinc-700 hover:underline dark:text-zinc-200"
          >
            <span
              aria-hidden="true"
              className={`text-zinc-400 transition-transform dark:text-zinc-600 ${expanded ? 'rotate-90' : ''}`}
            >
              ▸
            </span>
            <span className="font-mono text-xs break-all">{group.directory}</span>
            <span className="text-xs font-normal text-zinc-500 dark:text-zinc-400">
              {group.entries.length} file{group.entries.length === 1 ? '' : 's'} —{' '}
              {formatBytes(groupTotal)} if resolved
            </span>
          </button>
        </td>
      </tr>
      {expanded &&
        group.entries.map((u) => (
          <tr key={u.abs_path} className="border-t border-zinc-100 align-top dark:border-zinc-900">
            <td className="px-3 py-2 pl-8 font-mono text-xs break-all">{u.rel_path}</td>
            <td className="px-3 py-2 whitespace-nowrap">{formatBytes(u.size)}</td>
            <td className="px-3 py-2 text-xs text-zinc-500 dark:text-zinc-400">{u.reason}</td>
          </tr>
        ))}
    </>
  )
}

/** One directory's worth of the fourth pile, excluded content (2026-08-24, spec §11.1e/§17.6) --
 * same no-checkbox shape as `UnclaimedGroupRow` (never selectable), but its own reclaim figure is
 * deliberately never framed as "if resolved" -- `DiskReviewExcludedContentOut`'s own docstring is
 * explicit that this pile is "never counted toward a reclaim total." The size shown here is
 * purely descriptive: how many bytes are sitting under an excluded path with nothing currently
 * claiming them.
 */
function ExcludedContentGroupRow({
  group,
  expanded,
  onToggleExpand,
}: {
  group: ExcludedContentGroup
  expanded: boolean
  onToggleExpand: () => void
}) {
  const allAbsPaths = new Set(group.entries.map((e) => e.abs_path))
  const groupTotal = freedBytes(group.entries, allAbsPaths)
  return (
    <>
      <tr className="border-t border-zinc-100 bg-zinc-50/60 dark:border-zinc-900 dark:bg-zinc-900/40">
        <td colSpan={3} className="px-3 py-2">
          <button
            type="button"
            onClick={onToggleExpand}
            aria-expanded={expanded}
            className="flex flex-wrap items-center gap-2 text-left font-medium text-zinc-700 hover:underline dark:text-zinc-200"
          >
            <span
              aria-hidden="true"
              className={`text-zinc-400 transition-transform dark:text-zinc-600 ${expanded ? 'rotate-90' : ''}`}
            >
              ▸
            </span>
            <span className="font-mono text-xs break-all">{group.directory}</span>
            <span className="text-xs font-normal text-zinc-500 dark:text-zinc-400">
              {group.entries.length} file{group.entries.length === 1 ? '' : 's'} — {formatBytes(groupTotal)}
            </span>
          </button>
        </td>
      </tr>
      {expanded &&
        group.entries.map((e) => (
          <tr key={e.abs_path} className="border-t border-zinc-100 align-top dark:border-zinc-900">
            <td className="px-3 py-2 pl-8 font-mono text-xs break-all">{e.rel_path}</td>
            <td className="px-3 py-2 whitespace-nowrap">{formatBytes(e.size)}</td>
            <td className="px-3 py-2 font-mono text-xs break-all text-zinc-500 dark:text-zinc-400">
              {e.excluded_path}
            </td>
          </tr>
        ))}
    </>
  )
}

/** The disk review scan (docs/download-client-framework-spec.md §11, stage 4 of #18) --
 * *"Client shows all this on disk… what is in the base folders for the client that don't exist
 * in the UI that could be cleaned up with a review option."* Review-only: this page has no
 * delete button anywhere on it, and never will until #18's stage 5 ships it as its own,
 * separate control. Manual trigger only (spec §11.3) -- the scan is an SSH walk over
 * potentially large trees, so nothing here runs on page load; the user clicks Scan.
 *
 * **2026-08-24, this task (prompts/done/2026-08-24-disk-review-table-frontend.md) -- rebuilt
 * around a section per download client**, each a sortable, filterable torrent table
 * (`ClientTorrentSection` above), replacing the old flat "seeding estate" and "broken seeds"
 * tables now that the backend reports `torrents` (one row per claim) and `clients` (the roster
 * to section by, and the source of each section's own capability-driven column set -- spec
 * §17.2's rule applied here, never a `client_type` string check). Debris, the unclaimed pile, and
 * the new `excluded_content` pile have no torrent to hang columns like ratio or seed time on, and
 * a base path can have several contributing clients (spec §11.1a), so they stay grouped by
 * directory in their own sections below the client sections, unchanged in spirit from before this
 * task -- see each pile's own row component above for what changed and what didn't.
 *
 * **The label filter is global, not per-section** -- see `docs/decisions.md` for the full
 * reasoning; sort state stays per-section (`ClientTorrentSection`'s own local `useState`).
 *
 * **Display naming (this task's own §5):** "Debris" is a verdict; this feature is review-only.
 * The on-screen heading now reads "Not claimed by any client," and the unclaimed pile's heading
 * reads "Ownership unknown" -- **the code-level names (`debris`, `unclaimed`) and every spec
 * reference are untouched**, this is wording on screen only.
 *
 * `broken_seeds` and `skipped_base_paths` are named rather than hidden, the same "don't silently
 * absorb a gap" instinct this codebase applies everywhere else -- `broken_seeds` itself is
 * retired (superseded by a `torrents` row with `missing_on_disk: true`, marked in
 * `TorrentRow` above), `skipped_base_paths` is unchanged.
 */
export function DiskReviewPage() {
  const [scanning, setScanning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<DiskReviewScanResponse | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [expandedDebrisDirs, setExpandedDebrisDirs] = useState<Set<string>>(new Set())
  const [expandedUnclaimedDirs, setExpandedUnclaimedDirs] = useState<Set<string>>(new Set())
  const [expandedExcludedDirs, setExpandedExcludedDirs] = useState<Set<string>>(new Set())
  // The label filter (this task's own §4) -- `null` is "All labels." Global, per the decision
  // recorded in docs/decisions.md; shared across every client section.
  const [selectedLabel, setSelectedLabel] = useState<string | null>(null)

  const runScan = () => {
    setScanning(true)
    setError(null)
    runDiskReviewScan()
      .then((res) => {
        setResult(res)
        setSelected(new Set())
        setExpandedDebrisDirs(new Set())
        setExpandedUnclaimedDirs(new Set())
        setExpandedExcludedDirs(new Set())
        setSelectedLabel(null)
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setScanning(false))
  }

  const toggle = (path: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const toggleAll = (rows: DiskReviewDebrisOut[]) => {
    setSelected((prev) => {
      const allSelected = rows.every((r) => prev.has(r.abs_path))
      const next = new Set(prev)
      for (const r of rows) {
        if (allSelected) next.delete(r.abs_path)
        else next.add(r.abs_path)
      }
      return next
    })
  }

  const toggleSet = (set: Set<string>, setSet: (next: Set<string>) => void, key: string) => {
    const next = new Set(set)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    setSet(next)
  }

  const debrisGroups = result ? groupDebrisByDirectory(result.debris) : []
  const unclaimedGroups = result ? groupUnclaimedByDirectory(result.unclaimed) : []
  const excludedGroups = result ? groupExcludedContentByDirectory(result.excluded_content) : []
  const total = result ? freedBytes(result.debris, selected) : 0
  const unclaimedAbsPaths = new Set(result?.unclaimed.map((u) => u.abs_path) ?? [])
  const unclaimedTotal = result ? freedBytes(result.unclaimed, unclaimedAbsPaths) : 0
  const seedingFiles = result ? filesByClaimKey(result.seeding_estate) : new Map<string, SeedingEstateGroup>()
  const categoryLabels = result ? getCategoryLabels(result.torrents) : []

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-medium text-zinc-900 dark:text-zinc-100">Disk review</h1>
          <p className="text-sm text-zinc-500 dark:text-zinc-400">
            What is on disk under the configured client base paths that no client claims and
            lftpweb isn&apos;t using. Review-only -- nothing here deletes anything.
          </p>
        </div>
        <button
          type="button"
          onClick={runScan}
          disabled={scanning}
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          {scanning ? 'Scanning…' : 'Scan'}
        </button>
      </div>

      {error && (
        <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-900 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-200">
          {error}
        </div>
      )}

      {result && (
        <>
          {result.skipped_base_paths.length > 0 && (
            <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
              <p className="font-medium">
                {result.skipped_base_paths.length} base path
                {result.skipped_base_paths.length === 1 ? '' : 's'} skipped this pass
              </p>
              <ul className="mt-1 list-inside list-disc">
                {result.skipped_base_paths.map((s) => (
                  <li key={s.root}>
                    <span className="font-mono text-xs">{s.root}</span> -- {s.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* The label filter (this task's own §4) -- global across every client section below.
           * `<select>` gets an explicit width, never `w-full`, next to the "All labels" hint --
           * the second half of the layout trap this task's own §6 names: a shared `w-full` on a
           * `<select>` next to a chip crushes the chip to one character, invisible to any test
           * jsdom can run. */}
          {categoryLabels.length > 0 && (
            <div className="flex items-center gap-2">
              <label htmlFor="disk-review-label-filter" className="text-sm text-zinc-500 dark:text-zinc-400">
                Label
              </label>
              <select
                id="disk-review-label-filter"
                value={selectedLabel ?? ''}
                onChange={(e) => setSelectedLabel(e.target.value === '' ? null : e.target.value)}
                className="w-56 rounded-md border border-zinc-300 bg-white px-2 py-1 text-sm dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-200"
              >
                <option value="">All labels</option>
                {categoryLabels.map((l) => (
                  <option key={l.category} value={l.category}>
                    {l.category} — {attributionLabel(l.attribution)}
                  </option>
                ))}
              </select>
            </div>
          )}

          {result.clients.length === 0 ? (
            <p className="text-sm text-zinc-400">No download clients configured -- nothing to section by.</p>
          ) : (
            result.clients.map((client) => (
              <ClientTorrentSection
                key={client.client_id}
                client={client}
                torrents={result.torrents.filter((t) => t.client_id === client.client_id)}
                files={seedingFiles}
                selectedLabel={selectedLabel}
              />
            ))
          )}

          <section className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                Not claimed by any client -- {result.debris.length} candidate{result.debris.length === 1 ? '' : 's'} in{' '}
                {debrisGroups.length} director{debrisGroups.length === 1 ? 'y' : 'ies'}
              </h2>
              <span className="text-sm text-zinc-500 dark:text-zinc-400">
                {selected.size} selected -- {formatBytes(total)}
              </span>
            </div>
            {result.debris.length === 0 ? (
              <p className="text-sm text-zinc-400">Nothing found.</p>
            ) : (
              <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
                    <tr>
                      <th className="w-8 px-3 py-2">
                        <input
                          type="checkbox"
                          checked={result.debris.every((d) => selected.has(d.abs_path))}
                          onChange={() => toggleAll(result.debris)}
                        />
                      </th>
                      <th className="px-3 py-2 font-medium">Directory / path</th>
                      <th className="px-3 py-2 font-medium">Size</th>
                      <th className="px-3 py-2 font-medium">Links</th>
                    </tr>
                  </thead>
                  <tbody>
                    {debrisGroups.map((group) => (
                      <DebrisGroupRow
                        key={group.directory}
                        group={group}
                        selected={selected}
                        expanded={expandedDebrisDirs.has(group.directory)}
                        onToggleExpand={() =>
                          toggleSet(expandedDebrisDirs, setExpandedDebrisDirs, group.directory)
                        }
                        onToggleFile={toggle}
                        onToggleGroup={toggleAll}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {result.excluded_content.length > 0 && (
            <section className="flex flex-col gap-2">
              <div className="rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-400">
                <p className="font-medium text-zinc-700 dark:text-zinc-300">
                  Excluded content -- {result.excluded_content.length} item
                  {result.excluded_content.length === 1 ? '' : 's'} under an excluded path, no claim
                  currently covers {result.excluded_content.length === 1 ? 'it' : 'them'}
                </p>
                <p className="mt-1">
                  This usually means another lftpweb instance&apos;s client dropped its history entry,
                  or removed the torrent, while the bytes stayed on disk. Never selectable, never
                  counted toward a reclaim total -- deleting anything under an excluded path stays
                  refused regardless of whether it shows up here.
                </p>
              </div>
              <h2 className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                {result.excluded_content.length} item{result.excluded_content.length === 1 ? '' : 's'} in{' '}
                {excludedGroups.length} director{excludedGroups.length === 1 ? 'y' : 'ies'}
              </h2>
              <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
                    <tr>
                      <th className="px-3 py-2 font-medium">Directory / path</th>
                      <th className="px-3 py-2 font-medium">Size</th>
                      <th className="px-3 py-2 font-medium">Excluded path matched</th>
                    </tr>
                  </thead>
                  <tbody>
                    {excludedGroups.map((group) => (
                      <ExcludedContentGroupRow
                        key={group.directory}
                        group={group}
                        expanded={expandedExcludedDirs.has(group.directory)}
                        onToggleExpand={() =>
                          toggleSet(expandedExcludedDirs, setExpandedExcludedDirs, group.directory)
                        }
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {result.unclaimed.length > 0 && (
            <section className="flex flex-col gap-2">
              <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
                <p className="font-medium">
                  Ownership unknown -- {result.unclaimed.length} item{result.unclaimed.length === 1 ? '' : 's'}{' '}
                  of undeterminable ownership
                </p>
                <p className="mt-1">
                  This is not the norm -- a single-lftpweb setup should see this pile empty. A
                  populated pile usually means debris left behind by an interrupted operation, or
                  another lftpweb instance&apos;s content sharing this seedbox (a category this
                  instance cannot resolve to a path, so it cannot tell the two apart). Nothing
                  below is selectable through the debris flow above -- reviewing and acting on it
                  needs its own, deliberate step, which does not exist yet.
                </p>
              </div>
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                  {result.unclaimed.length} item{result.unclaimed.length === 1 ? '' : 's'} in{' '}
                  {unclaimedGroups.length} director{unclaimedGroups.length === 1 ? 'y' : 'ies'}
                </h2>
                <span className="text-sm text-zinc-500 dark:text-zinc-400">
                  {formatBytes(unclaimedTotal)} if resolved
                </span>
              </div>
              <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
                    <tr>
                      <th className="px-3 py-2 font-medium">Directory / path</th>
                      <th className="px-3 py-2 font-medium">Size</th>
                      <th className="px-3 py-2 font-medium">Why unclaimed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {unclaimedGroups.map((group) => (
                      <UnclaimedGroupRow
                        key={group.directory}
                        group={group}
                        allAbsPaths={unclaimedAbsPaths}
                        expanded={expandedUnclaimedDirs.has(group.directory)}
                        onToggleExpand={() =>
                          toggleSet(expandedUnclaimedDirs, setExpandedUnclaimedDirs, group.directory)
                        }
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </>
      )}
    </div>
  )
}
