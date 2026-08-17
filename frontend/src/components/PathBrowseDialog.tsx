import { useCallback, useEffect, useState } from 'react'
import { browseLocal, browseRemote } from '../api/client'
import type { BrowseResponse } from '../api/types'
import { breadcrumbSegments, descendPath, fallbackNote } from '../lib/pathBrowse'

interface PathBrowseDialogProps {
  side: 'local' | 'remote'
  /** The field's current text when Browse was clicked -- opened as-is; the endpoint resolves
   * it (DESIGN.md §9.2's walk-up) so a half-typed value still opens somewhere useful.
   */
  initialPath: string
  onSelect: (path: string) => void
  onClose: () => void
}

/** Settings -> Queues' Browse dialog (GitHub issue #4,
 * `prompts/done/2026-08-16-path-browse-dialog.md`) -- one component for both sides, since
 * `api/browse.py` returns the identical shape for local and remote alike. Modal styling
 * mirrors this app's existing overlay pattern (`ItemDrawer.tsx`'s backdrop-button + panel), a
 * centered dialog rather than a side drawer since this is a focused pick-one-thing flow, not a
 * "keep the page visible behind it" one.
 *
 * All navigation re-fetches through the endpoint -- there is no client-side path-joining that
 * feeds a listing; `lib/pathBrowse.ts.descendPath` only builds the *next requested path*, and
 * the server's own walk-up/resolution is what actually decides what's shown. **Select** always
 * writes back `result.path` (the endpoint-resolved absolute path), never anything the user
 * typed or a `~` form -- the stored value feeds `find`/lftp/`rm -rf --` downstream and must be
 * unambiguous.
 */
export function PathBrowseDialog({ side, initialPath, onSelect, onClose }: PathBrowseDialogProps) {
  const [result, setResult] = useState<BrowseResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const fetcher = side === 'local' ? browseLocal : browseRemote

  const load = useCallback(
    (path: string) => {
      setLoading(true)
      setError(null)
      fetcher(path)
        .then(setResult)
        .catch((err) => setError(err instanceof Error ? err.message : String(err)))
        .finally(() => setLoading(false))
    },
    [fetcher],
  )

  useEffect(() => {
    load(initialPath)
    // Only on mount -- `load` is stable per `side` and re-running this on every render would
    // refetch the root listing every time the dialog re-renders for an unrelated reason.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center p-4">
      {/* Backdrop: closes the dialog on click, same convention as ItemDrawer.tsx's own. */}
      <button
        type="button"
        aria-label="Close"
        className="absolute inset-0 bg-black/20 dark:bg-black/40"
        onClick={onClose}
      />
      <div className="relative flex max-h-[80vh] w-full max-w-lg flex-col rounded-md border border-zinc-200 bg-white shadow-xl dark:border-zinc-800 dark:bg-zinc-950">
        <div className="flex items-center justify-between gap-2 border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            Browse {side === 'local' ? 'local (container)' : 'remote (seedbox)'} directory
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-zinc-300 px-2.5 py-1 text-sm text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
          >
            Close
          </button>
        </div>

        {result && (
          <div className="flex flex-wrap items-center gap-1 border-b border-zinc-200 px-4 py-2 text-sm dark:border-zinc-800">
            {breadcrumbSegments(result.path).map((crumb, i) => (
              <span key={crumb.path} className="flex items-center gap-1">
                {i > 0 && <span className="text-zinc-400">/</span>}
                <button
                  type="button"
                  onClick={() => load(crumb.path)}
                  className="text-zinc-600 hover:underline dark:text-zinc-300"
                >
                  {crumb.label}
                </button>
              </span>
            ))}
          </div>
        )}

        {result?.fallback_from && (
          <p className="border-b border-amber-200 bg-amber-50 px-4 py-1.5 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
            {fallbackNote(result.fallback_from)}
          </p>
        )}

        {/* Errors render inline and the dialog stays open so the user can retry or cancel --
         * never a thrown-away dialog on a transient remote failure. */}
        {error && (
          <p className="border-b border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
            {error}
          </p>
        )}

        <div className="flex-1 overflow-y-auto px-2 py-2">
          {loading && <p className="px-2 py-2 text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>}
          {!loading && result && (
            <>
              {result.parent != null && (
                <button
                  type="button"
                  onClick={() => load(result.parent as string)}
                  className="block w-full rounded-md px-2 py-1.5 text-left text-sm text-zinc-700 hover:bg-zinc-100 dark:text-zinc-200 dark:hover:bg-zinc-900"
                >
                  .. (up)
                </button>
              )}
              {result.entries.map((entry) => (
                <button
                  key={entry.name}
                  type="button"
                  onClick={() => load(descendPath(result.path, entry.name))}
                  className="block w-full rounded-md px-2 py-1.5 text-left text-sm text-zinc-700 hover:bg-zinc-100 dark:text-zinc-200 dark:hover:bg-zinc-900"
                >
                  {entry.name}/
                </button>
              ))}
              {result.entries.length === 0 && (
                <p className="px-2 py-2 text-sm text-zinc-400">No subdirectories here.</p>
              )}
              {result.truncated && (
                <p className="px-2 py-2 text-xs text-amber-600 dark:text-amber-400">
                  Showing the first entries only — narrow the path to see the rest.
                </p>
              )}
            </>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <span
            className="truncate font-mono text-xs text-zinc-500 dark:text-zinc-400"
            title={result?.path}
          >
            {result?.path ?? ''}
          </span>
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={!result}
              onClick={() => result && onSelect(result.path)}
              className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
            >
              Select this directory
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
