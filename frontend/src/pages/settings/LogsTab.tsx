import { useEffect, useState } from 'react'
import { getLogFiles, getLogTail, logDownloadUrl } from '../../api/client'
import type { LogFileOut, LogLevel } from '../../api/types'
import { formatBytes } from '../../lib/format'

const LEVELS: LogLevel[] = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

const selectClasses =
  'rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const buttonClasses =
  'rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300'

/** Settings → Logs (DESIGN.md §10.1): the app log only -- never the per-job lftp output
 * (History's job detail) or the event audit trail (also History). Tails the *current* file
 * with a server-bounded read (`core/logtail.py`) and lets a level filter narrow it; lists
 * every rotated file for download. No auto-refresh/polling -- a manual "Refresh" button,
 * the same call History made for its own filtered views (docs/decisions.md, phase 6) so a
 * tail the user is reading doesn't jump out from under them on a timer.
 */
export function LogsTab() {
  const [files, setFiles] = useState<LogFileOut[]>([])
  const [lines, setLines] = useState<string[]>([])
  const [truncated, setTruncated] = useState(false)
  const [level, setLevel] = useState<LogLevel | ''>('')
  const [maxLines, setMaxLines] = useState(200)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setError(null)
    try {
      const [filesResp, tailResp] = await Promise.all([
        getLogFiles(),
        getLogTail(maxLines, level || undefined),
      ])
      setFiles(filesResp.files)
      setLines(tailResp.lines)
      setTruncated(tailResp.truncated)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (loading) return <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>

  return (
    <div className="flex flex-col gap-6">
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        The rotating app log (DESIGN.md §10.1) -- errors, warnings, scheduler admission
        decisions, scan failures. Credentials are redacted before a line ever reaches disk
        (see <code>logsetup.py</code>), not on the way out here.
      </p>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-zinc-700 dark:text-zinc-300">Level</span>
          <select
            className={selectClasses}
            value={level}
            onChange={(e) => setLevel(e.target.value as LogLevel | '')}
          >
            <option value="">All levels</option>
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-zinc-700 dark:text-zinc-300">Lines</span>
          <select
            className={selectClasses}
            value={maxLines}
            onChange={(e) => setMaxLines(Number(e.target.value))}
          >
            {[100, 200, 500, 1000, 2000].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>

        <button type="button" className={buttonClasses} onClick={load}>
          Refresh
        </button>
      </div>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      {truncated && (
        <p className="text-xs text-amber-600 dark:text-amber-400">
          The current file is larger than this view's read window -- older matching lines may
          exist above what's shown here. Download the file for the full history.
        </p>
      )}

      <pre className="max-h-[32rem] overflow-auto rounded-md border border-zinc-200 bg-zinc-50 p-3 text-xs text-zinc-800 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-200">
        {lines.length > 0 ? lines.join('\n') : '(no matching lines)'}
      </pre>

      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Log files</h3>
        <table className="w-full max-w-xl text-sm">
          <thead>
            <tr className="text-left text-zinc-500 dark:text-zinc-400">
              <th className="py-1 pr-4 font-medium">File</th>
              <th className="py-1 pr-4 font-medium">Size</th>
              <th className="py-1 pr-4 font-medium">Modified</th>
              <th className="py-1 font-medium">Download</th>
            </tr>
          </thead>
          <tbody>
            {files.map((f) => (
              <tr key={f.name} className="border-t border-zinc-100 dark:border-zinc-800">
                <td className="py-1 pr-4 text-zinc-800 dark:text-zinc-200">
                  {f.name}
                  {f.is_current && (
                    <span className="ml-2 rounded bg-zinc-200 px-1.5 py-0.5 text-xs text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
                      current
                    </span>
                  )}
                </td>
                <td className="py-1 pr-4 text-zinc-600 dark:text-zinc-400">
                  {formatBytes(f.size_bytes)}
                </td>
                <td className="py-1 pr-4 text-zinc-600 dark:text-zinc-400">
                  {new Date(f.modified_at).toLocaleString()}
                </td>
                <td className="py-1">
                  <a
                    className="text-zinc-700 underline hover:text-zinc-900 dark:text-zinc-300 dark:hover:text-zinc-100"
                    href={logDownloadUrl(f.name)}
                    download
                  >
                    Download
                  </a>
                </td>
              </tr>
            ))}
            {files.length === 0 && (
              <tr>
                <td colSpan={4} className="py-2 text-zinc-500 dark:text-zinc-400">
                  No log files yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
