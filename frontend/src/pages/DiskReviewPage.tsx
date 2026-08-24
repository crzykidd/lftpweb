import { useState } from 'react'
import { runDiskReviewScan } from '../api/client'
import type { DiskReviewDebrisOut, DiskReviewScanResponse } from '../api/types'
import {
  type DebrisGroup,
  freedBytes,
  groupDebrisByDirectory,
  groupSeedingEstateByTorrent,
  groupUnclaimedByDirectory,
  type SeedingEstateGroup,
  type UnclaimedGroup,
} from '../lib/diskReview'
import { formatBytes } from '../lib/format'

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

/** One torrent's seeding-estate files, rolled up (spec §11.1d, finding #7) -- never selectable,
 * shown for visibility only. `group.totalSize` is a plain sum (`lib/diskReview.ts.
 * groupSeedingEstateByTorrent`'s own docstring has why that's fine here and would not be for
 * debris).
 */
function SeedingEstateGroupRow({
  group,
  expanded,
  onToggleExpand,
}: {
  group: SeedingEstateGroup
  expanded: boolean
  onToggleExpand: () => void
}) {
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
            <span>{group.transferName}</span>
            <span className="text-xs font-normal text-zinc-500 dark:text-zinc-400">
              {group.clientName} — {group.entries.length} file{group.entries.length === 1 ? '' : 's'} —{' '}
              {formatBytes(group.totalSize)}
            </span>
          </button>
        </td>
      </tr>
      {expanded &&
        group.entries.map((s) => (
          <tr key={s.abs_path} className="border-t border-zinc-100 align-top dark:border-zinc-900">
            <td className="px-3 py-2 pl-8 font-mono text-xs break-all">{s.rel_path}</td>
            <td className="px-3 py-2 whitespace-nowrap">{formatBytes(s.size)}</td>
            <td className="px-3 py-2">{s.claimed_by_client_name}</td>
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

/** The disk review scan (docs/download-client-framework-spec.md §11, stage 4 of #18) --
 * *"Client shows all this on disk… what is in the base folders for the client that don't exist
 * in the UI that could be cleaned up with a review option."* Review-only: this page has no
 * delete button anywhere on it, and never will until #18's stage 5 ships it as its own,
 * separate control. Manual trigger only (spec §11.3) -- the scan is an SSH walk over
 * potentially large trees, so nothing here runs on page load; the user clicks Scan.
 *
 * Three piles, labelled distinctly (spec §11.1d): **Debris** is selectable, its running total
 * link-aware (`freedBytes`, mirrors `core/disk_review.py.freed_bytes` exactly -- selecting one
 * side of a hardlinked pair reports zero bytes, because the other link still holds the data).
 * **Seeding estate** is shown for visibility only, never selectable -- it is claimed, not
 * orphaned. **Unclaimed** (finding #17, 2026-08-23) is ownership genuinely undeterminable --
 * shown, grouped by directory the same as debris, its own reclaim figure link-aware the same way
 * -- but with no checkbox anywhere, so it cannot be reached by the ordinary select-and-remove
 * flow. Fail-closed used to mean "don't show it"; that was the same mistake as finding #2
 * (content never surfaced is indistinguishable from content that doesn't exist) applied to this
 * feature's own most valuable output. `broken_seeds` and `skipped_base_paths` are named rather
 * than hidden, the same "don't silently absorb a gap" instinct this codebase applies everywhere
 * else.
 *
 * **All three piles are rolled up for display (2026-08-23, finding #7): "it would be better to
 * show Torrents and expand each torrent to see details like files etc."** `core/disk_review.py.
 * reconcile()` itself is untouched -- still per-file, because inode accounting is inherently
 * per-file (spec §11.1b) -- `lib/diskReview.ts.groupDebrisByDirectory`/
 * `groupSeedingEstateByTorrent`/`groupUnclaimedByDirectory` only bucket its already-flat output
 * for this page. **The piles group differently on purpose**: the seeding estate groups by the
 * claim's own torrent (it always has one); debris and unclaimed have no torrent to group under by
 * definition, so both group by directory instead (finding #17 extends debris's own grouping
 * choice to the third pile rather than inventing a different one).
 */
export function DiskReviewPage() {
  const [scanning, setScanning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<DiskReviewScanResponse | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [expandedDebrisDirs, setExpandedDebrisDirs] = useState<Set<string>>(new Set())
  const [expandedTorrents, setExpandedTorrents] = useState<Set<string>>(new Set())
  const [expandedUnclaimedDirs, setExpandedUnclaimedDirs] = useState<Set<string>>(new Set())

  const runScan = () => {
    setScanning(true)
    setError(null)
    runDiskReviewScan()
      .then((res) => {
        setResult(res)
        setSelected(new Set())
        setExpandedDebrisDirs(new Set())
        setExpandedTorrents(new Set())
        setExpandedUnclaimedDirs(new Set())
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
  const seedingGroups = result ? groupSeedingEstateByTorrent(result.seeding_estate) : []
  const unclaimedGroups = result ? groupUnclaimedByDirectory(result.unclaimed) : []
  const total = result ? freedBytes(result.debris, selected) : 0
  const unclaimedAbsPaths = new Set(result?.unclaimed.map((u) => u.abs_path) ?? [])
  const unclaimedTotal = result ? freedBytes(result.unclaimed, unclaimedAbsPaths) : 0

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

          {result.client_failures.length > 0 && (
            <div className="rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-400">
              <p className="font-medium text-zinc-700 dark:text-zinc-300">
                Client{result.client_failures.length === 1 ? '' : 's'} that did not report this
                pass
              </p>
              <ul className="mt-1 list-inside list-disc">
                {result.client_failures.map((f) => (
                  <li key={f.client_id}>
                    {f.client_name} -- {f.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <section className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                Debris -- {result.debris.length} candidate{result.debris.length === 1 ? '' : 's'} in{' '}
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

          <section className="flex flex-col gap-2">
            <h2 className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
              Seeding estate -- {result.seeding_estate.length} claimed file
              {result.seeding_estate.length === 1 ? '' : 's'} across {seedingGroups.length} torrent
              {seedingGroups.length === 1 ? '' : 's'}, shown for visibility
            </h2>
            {result.seeding_estate.length === 0 ? (
              <p className="text-sm text-zinc-400">Nothing found.</p>
            ) : (
              <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
                    <tr>
                      <th className="px-3 py-2 font-medium">Torrent / path</th>
                      <th className="px-3 py-2 font-medium">Size</th>
                      <th className="px-3 py-2 font-medium">Claimed by</th>
                    </tr>
                  </thead>
                  <tbody>
                    {seedingGroups.map((group) => (
                      <SeedingEstateGroupRow
                        key={group.key}
                        group={group}
                        expanded={expandedTorrents.has(group.key)}
                        onToggleExpand={() => toggleSet(expandedTorrents, setExpandedTorrents, group.key)}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {result.unclaimed.length > 0 && (
            <section className="flex flex-col gap-2">
              <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
                <p className="font-medium">
                  Unclaimed -- {result.unclaimed.length} item{result.unclaimed.length === 1 ? '' : 's'}{' '}
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

          {result.broken_seeds.length > 0 && (
            <section className="flex flex-col gap-2">
              <h2 className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                Broken seeds -- {result.broken_seeds.length} claimed by a client, missing on disk
              </h2>
              <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
                    <tr>
                      <th className="px-3 py-2 font-medium">Client</th>
                      <th className="px-3 py-2 font-medium">Transfer</th>
                      <th className="px-3 py-2 font-medium">Content path</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.broken_seeds.map((b) => (
                      <tr
                        key={`${b.client_id}-${b.transfer_id}`}
                        className="border-t border-zinc-100 align-top dark:border-zinc-900"
                      >
                        <td className="px-3 py-2">{b.client_name}</td>
                        <td className="px-3 py-2">{b.transfer_name}</td>
                        <td className="px-3 py-2 font-mono text-xs break-all">{b.content_path}</td>
                      </tr>
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
