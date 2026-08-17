import { useEffect, useState } from 'react'
import { downloadSupportBundle, listArrInstances } from '../api/client'
import type { ArrInstanceOut } from '../api/types'
import {
  defaultSupportBundleSelection,
  enabledArrInstances,
  toggleArrInstance,
  toSupportBundleRequest,
  type SupportBundleSelection,
} from '../lib/supportBundle'

const checkboxRowClasses = 'flex items-start gap-2 text-sm text-zinc-800 dark:text-zinc-200'
const checkboxClasses = 'mt-0.5 h-4 w-4 rounded border-zinc-300 dark:border-zinc-700'

interface Props {
  onClose: () => void
}

/** Settings -> Logs' "Support bundle…" dialog (2026-08-17,
 * prompts/done/2026-08-17-support-bundle.md): a checkbox per part, all default ON, producing
 * one downloadable zip via `POST /api/support-bundle`. lftpweb's own logs are the one row shown
 * checked *and disabled* -- the backend always includes them regardless of what's sent
 * (`lib/supportBundle.ts`'s own doc comment), so there is nothing here to toggle for it.
 *
 * *arr instance rows are fetched fresh on open (`listArrInstances()`, the same call
 * `FilesPage.tsx`/`IntegrationsTab.tsx` already make) and the section is omitted entirely when
 * none are enabled -- never an empty, pointless heading.
 */
export function SupportBundleDialog({ onClose }: Props) {
  const [instances, setInstances] = useState<ArrInstanceOut[]>([])
  const [selection, setSelection] = useState<SupportBundleSelection>(
    defaultSupportBundleSelection([]),
  )
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listArrInstances()
      .then((all) => {
        if (cancelled) return
        setInstances(all)
        setSelection(defaultSupportBundleSelection(enabledArrInstances(all).map((i) => i.id)))
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const arrRows = enabledArrInstances(instances)

  const generate = async () => {
    setGenerating(true)
    setError(null)
    try {
      await downloadSupportBundle(toSupportBundleRequest(selection))
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setGenerating(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        aria-label="Close"
        className="absolute inset-0 bg-black/20 dark:bg-black/40"
        onClick={onClose}
      />
      <div className="relative flex max-h-[85vh] w-full max-w-md flex-col rounded-md border border-zinc-200 bg-white shadow-xl dark:border-zinc-800 dark:bg-zinc-950">
        <div className="flex items-center justify-between gap-2 border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            Support bundle
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-zinc-300 px-2.5 py-1 text-sm text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
          >
            Close
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
            A zip of diagnostic information you can attach to an issue or send manually.
            Passwords, API keys, and key material are never included, regardless of what's
            checked below.
          </p>

          {loading ? (
            <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>
          ) : (
            <div className="flex flex-col gap-3">
              <label className={checkboxRowClasses}>
                <input type="checkbox" checked disabled className={checkboxClasses} />
                <span>
                  lftpweb logs
                  <span className="block text-xs text-zinc-500 dark:text-zinc-400">
                    The live log file plus every rotated file. Always included.
                  </span>
                </span>
              </label>

              <label className={checkboxRowClasses}>
                <input
                  type="checkbox"
                  className={checkboxClasses}
                  checked={selection.includeEnvironment}
                  onChange={(e) =>
                    setSelection({ ...selection, includeEnvironment: e.target.checked })
                  }
                />
                <span>
                  Build + environment snapshot
                  <span className="block text-xs text-zinc-500 dark:text-zinc-400">
                    Version, build, migration level, health, lftp/Python versions, per-queue
                    disk usage.
                  </span>
                </span>
              </label>

              <label className={checkboxRowClasses}>
                <input
                  type="checkbox"
                  className={checkboxClasses}
                  checked={selection.includeSettings}
                  onChange={(e) =>
                    setSelection({ ...selection, includeSettings: e.target.checked })
                  }
                />
                <span>
                  Sanitized settings dump
                  <span className="block text-xs text-zinc-500 dark:text-zinc-400">
                    Host, queues, patterns, transfer/post-processing, auth mode, *arr instances
                    -- never a secret.
                  </span>
                </span>
              </label>

              <label className={checkboxRowClasses}>
                <input
                  type="checkbox"
                  className={checkboxClasses}
                  checked={selection.includeEvents}
                  onChange={(e) => setSelection({ ...selection, includeEvents: e.target.checked })}
                />
                <span>
                  Recent audit trail
                  <span className="block text-xs text-zinc-500 dark:text-zinc-400">
                    The most recent 1,000 History events.
                  </span>
                </span>
              </label>

              <label className={checkboxRowClasses}>
                <input
                  type="checkbox"
                  className={checkboxClasses}
                  checked={selection.includeJobs}
                  onChange={(e) => setSelection({ ...selection, includeJobs: e.target.checked })}
                />
                <span>
                  Recent job history
                  <span className="block text-xs text-zinc-500 dark:text-zinc-400">
                    The most recent 100 jobs, including their error output.
                  </span>
                </span>
              </label>

              {arrRows.length > 0 && (
                <div className="flex flex-col gap-2 border-t border-zinc-200 pt-3 dark:border-zinc-800">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                    Sonarr/Radarr logs
                  </h3>
                  {arrRows.map((inst) => (
                    <label key={inst.id} className={checkboxRowClasses}>
                      <input
                        type="checkbox"
                        className={checkboxClasses}
                        checked={selection.arrInstanceIds.includes(inst.id)}
                        onChange={() => setSelection(toggleArrInstance(selection, inst.id))}
                      />
                      <span>
                        {inst.name}
                        <span className="ml-1 text-xs text-zinc-500 dark:text-zinc-400">
                          ({inst.kind})
                        </span>
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </div>
          )}

          {error && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{error}</p>}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={loading || generating}
            onClick={generate}
            className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
          >
            {generating ? 'Generating…' : 'Generate'}
          </button>
        </div>
      </div>
    </div>
  )
}
