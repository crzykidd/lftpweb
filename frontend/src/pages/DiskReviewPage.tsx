import { useState } from 'react'
import { runDiskReviewScan } from '../api/client'
import type { DiskReviewDebrisOut, DiskReviewScanResponse } from '../api/types'
import { freedBytes } from '../lib/diskReview'
import { formatBytes } from '../lib/format'

/** The disk review scan (docs/download-client-framework-spec.md §11, stage 4 of #18) --
 * *"Client shows all this on disk… what is in the base folders for the client that don't exist
 * in the UI that could be cleaned up with a review option."* Review-only: this page has no
 * delete button anywhere on it, and never will until #18's stage 5 ships it as its own,
 * separate control. Manual trigger only (spec §11.3) -- the scan is an SSH walk over
 * potentially large trees, so nothing here runs on page load; the user clicks Scan.
 *
 * Two piles, labelled distinctly (spec §11.1d): **Debris** is selectable, its running total
 * link-aware (`freedBytes`, mirrors `core/disk_review.py.freed_bytes` exactly -- selecting one
 * side of a hardlinked pair reports zero bytes, because the other link still holds the data).
 * **Seeding estate** is shown for visibility only, never selectable -- it is claimed, not
 * orphaned. `broken_seeds` and `skipped_base_paths` are named rather than hidden, the same
 * "don't silently absorb a gap" instinct this codebase applies everywhere else.
 */
export function DiskReviewPage() {
  const [scanning, setScanning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<DiskReviewScanResponse | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const runScan = () => {
    setScanning(true)
    setError(null)
    runDiskReviewScan()
      .then((res) => {
        setResult(res)
        setSelected(new Set())
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

  const total = result ? freedBytes(result.debris, selected) : 0

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
                Debris -- {result.debris.length} candidate{result.debris.length === 1 ? '' : 's'}
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
                      <th className="px-3 py-2 font-medium">Path</th>
                      <th className="px-3 py-2 font-medium">Size</th>
                      <th className="px-3 py-2 font-medium">Links</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.debris.map((d) => (
                      <tr key={d.abs_path} className="border-t border-zinc-100 align-top dark:border-zinc-900">
                        <td className="px-3 py-2">
                          <input
                            type="checkbox"
                            checked={selected.has(d.abs_path)}
                            onChange={() => toggle(d.abs_path)}
                          />
                        </td>
                        <td className="px-3 py-2 font-mono text-xs break-all">{d.abs_path}</td>
                        <td className="px-3 py-2 whitespace-nowrap">{formatBytes(d.size)}</td>
                        <td className="px-3 py-2 text-xs text-zinc-500 dark:text-zinc-400">
                          {d.link_paths.length > 1 ? `${d.link_paths.length} linked copies` : '--'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="flex flex-col gap-2">
            <h2 className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
              Seeding estate -- {result.seeding_estate.length} claimed file
              {result.seeding_estate.length === 1 ? '' : 's'} shown for visibility
            </h2>
            {result.seeding_estate.length === 0 ? (
              <p className="text-sm text-zinc-400">Nothing found.</p>
            ) : (
              <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
                    <tr>
                      <th className="px-3 py-2 font-medium">Path</th>
                      <th className="px-3 py-2 font-medium">Size</th>
                      <th className="px-3 py-2 font-medium">Claimed by</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.seeding_estate.map((s) => (
                      <tr key={s.abs_path} className="border-t border-zinc-100 align-top dark:border-zinc-900">
                        <td className="px-3 py-2 font-mono text-xs break-all">{s.abs_path}</td>
                        <td className="px-3 py-2 whitespace-nowrap">{formatBytes(s.size)}</td>
                        <td className="px-3 py-2">{s.claimed_by_client_name}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

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
