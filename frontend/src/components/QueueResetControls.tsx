import { useState } from 'react'
import { previewResetByPattern, resetByPattern, resetQueue } from '../api/client'
import type { FileNode, ResetPatternPreviewItem, ResetSummaryResponse, SyncMode } from '../api/types'
import { resetWarningLines } from '../lib/resetWarning'

/** `remote_size` is `null` only for a node never tracked remotely -- the identical reading
 * `FileTree.tsx.hasRemoteCopy` uses for the same question; duplicated here (one line) rather
 * than exported across files for a helper this small.
 */
function hasRemoteCopy(node: { remote_size: number | null }): boolean {
  return node.remote_size != null
}

const buttonClasses =
  'rounded-md border border-violet-300 px-2 py-1 text-xs font-medium text-violet-700 hover:bg-violet-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-violet-800 dark:text-violet-300 dark:hover:bg-violet-950'
const panelClasses =
  'flex flex-col gap-2 rounded-md border border-violet-300 bg-violet-50 px-3 py-2 text-sm dark:border-violet-800 dark:bg-violet-950/40'
const inputClasses =
  'rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'

function WithheldList({ withheld }: { withheld: { rel_path: string; reason: string }[] }) {
  if (withheld.length === 0) return null
  return (
    <ul className="list-disc space-y-0.5 pl-5 text-xs">
      {withheld.map((w) => (
        <li key={w.rel_path}>
          <span className="font-mono">{w.rel_path}</span> — {w.reason}
        </li>
      ))}
    </ul>
  )
}

/** The two queue-scoped "Reset item tracking" scopes -- whole-queue (the clean-slate case) and
 * purge-by-pattern (2026-08-13, `prompts/2026-08-13-reset-item-tracking.md` plus the same-day
 * follow-up adding pattern purge). The third scope, selected items, lives in `FileTree.tsx`
 * itself (its own multi-select already exists there); these two are queue-level actions with
 * nowhere natural to sit inside the virtualized row list, so they get their own small toolbar
 * mounted once per queue section (`FilesPage.tsx`).
 *
 * Deliberately **not** named or styled anything like "Clear History" (a few pixels away on the
 * History page, a wholly different action) -- violet accent throughout, matching the identical
 * choice in `FileTree.tsx`'s own "Reset selected" button, so every reset control in the app
 * reads as one family, visually distinct from Delete (red) and Queue (sky).
 */
export function QueueResetControls({
  queueId,
  queueName,
  syncMode,
  autoQueueEnabled,
  scanIntervalS,
  nodes,
}: {
  queueId: number
  queueName: string
  syncMode: SyncMode
  autoQueueEnabled: boolean
  scanIntervalS: number | null
  nodes: FileNode[]
}) {
  const ctx = { syncMode, autoQueueEnabled, scanIntervalS }
  // Only top-level entries are "items" in the sense a reset actually targets (DESIGN.md §4.7;
  // the same filter `api/settings.py.pattern_preview` applies to its own remote-tree read) --
  // a nested file's presence/absence isn't what the whole-queue warning is about.
  const topLevel = nodes.filter((n) => !n.rel_path.includes('/'))

  // --- Whole-queue scope: typed confirmation (the most destructive action in the app) --------
  const [wholeQueueOpen, setWholeQueueOpen] = useState(false)
  const [confirmName, setConfirmName] = useState('')
  const [wholeQueueBusy, setWholeQueueBusy] = useState(false)
  const [wholeQueueResult, setWholeQueueResult] = useState<ResetSummaryResponse | null>(null)
  const [wholeQueueError, setWholeQueueError] = useState<string | null>(null)

  const wholeQueueRemoteCount = topLevel.filter(hasRemoteCopy).length
  const nameMatches = confirmName === queueName

  const openWholeQueue = () => {
    setWholeQueueOpen(true)
    setConfirmName('')
    setWholeQueueResult(null)
    setWholeQueueError(null)
  }

  const confirmWholeQueueReset = async () => {
    if (!nameMatches) return
    setWholeQueueBusy(true)
    setWholeQueueError(null)
    try {
      const result = await resetQueue(queueId, { confirm_name: confirmName })
      setWholeQueueResult(result)
      setWholeQueueOpen(false)
      setConfirmName('')
    } catch (err) {
      setWholeQueueError(err instanceof Error ? err.message : String(err))
    } finally {
      setWholeQueueBusy(false)
    }
  }

  // --- Purge-by-pattern scope: preview first, single queue only ------------------------------
  const [patternOpen, setPatternOpen] = useState(false)
  const [pattern, setPattern] = useState('')
  const [preview, setPreview] = useState<ResetPatternPreviewItem[] | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [patternResult, setPatternResult] = useState<ResetSummaryResponse | null>(null)
  const [patternBusy, setPatternBusy] = useState(false)
  const [patternError, setPatternError] = useState<string | null>(null)

  const openPattern = () => {
    setPatternOpen(true)
    setPattern('')
    setPreview(null)
    setPreviewError(null)
    setPatternResult(null)
    setPatternError(null)
  }

  const runPreview = async () => {
    const trimmed = pattern.trim()
    if (!trimmed) return
    setPreviewBusy(true)
    setPreviewError(null)
    setPreview(null)
    try {
      const result = await previewResetByPattern(queueId, { pattern: trimmed })
      setPreview(result.items)
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : String(err))
    } finally {
      setPreviewBusy(false)
    }
  }

  const previewRemoteCount = (preview ?? []).filter(hasRemoteCopy).length

  const confirmPatternReset = async () => {
    const trimmed = pattern.trim()
    if (!trimmed || preview == null) return
    setPatternBusy(true)
    setPatternError(null)
    try {
      const result = await resetByPattern(queueId, { pattern: trimmed })
      setPatternResult(result)
      setPatternOpen(false)
      setPattern('')
      setPreview(null)
    } catch (err) {
      setPatternError(err instanceof Error ? err.message : String(err))
    } finally {
      setPatternBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={openWholeQueue} className={buttonClasses}>
          Reset queue tracking…
        </button>
        <button type="button" onClick={openPattern} className={buttonClasses}>
          Purge by pattern…
        </button>
      </div>

      {wholeQueueOpen && (
        <div className={panelClasses}>
          <p className="font-medium text-violet-900 dark:text-violet-200">
            Reset tracking for <strong>every item</strong> in <strong>{queueName}</strong> —{' '}
            {topLevel.length} {topLevel.length === 1 ? 'item' : 'items'}. This forgets lftpweb
            ever saw any of them; the next scan treats this whole queue as brand new. This is
            the most destructive action in the app and cannot be undone.
          </p>
          {resetWarningLines(topLevel.length, wholeQueueRemoteCount, ctx).map((line) => (
            <p key={line} className="text-violet-900 dark:text-violet-200">
              {line}
            </p>
          ))}
          <label className="flex flex-col gap-1 text-violet-900 dark:text-violet-200">
            Type the queue name (<span className="font-mono">{queueName}</span>) to confirm:
            <input
              type="text"
              value={confirmName}
              onChange={(e) => setConfirmName(e.target.value)}
              className={inputClasses}
              placeholder={queueName}
              aria-label="Type the queue name to confirm"
            />
          </label>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={confirmWholeQueueReset}
              disabled={!nameMatches || wholeQueueBusy}
              className="rounded-md bg-violet-700 px-2 py-1 text-xs font-medium text-white hover:bg-violet-800 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-violet-800 dark:hover:bg-violet-700"
            >
              {wholeQueueBusy ? 'Resetting…' : 'Reset this queue'}
            </button>
            <button
              type="button"
              onClick={() => setWholeQueueOpen(false)}
              disabled={wholeQueueBusy}
              className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {patternOpen && (
        <div className={panelClasses}>
          <p className="text-violet-900 dark:text-violet-200">
            Reset tracking for every top-level item in <strong>{queueName}</strong> whose name
            matches a pattern (case-insensitive; glob with <span className="font-mono">*?[</span>,
            plain substring otherwise -- the identical matching auto-queue's own select/skip
            patterns use). Preview the matches before anything is reset.
          </p>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={pattern}
              onChange={(e) => {
                setPattern(e.target.value)
                setPreview(null)
                setPreviewError(null)
              }}
              placeholder="e.g. Show.S01* or a substring"
              className={inputClasses}
              aria-label="Pattern to purge"
            />
            <button
              type="button"
              onClick={runPreview}
              disabled={!pattern.trim() || previewBusy}
              className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
            >
              {previewBusy ? 'Previewing…' : 'Preview'}
            </button>
          </div>
          {previewError && <p className="text-red-700 dark:text-red-300">{previewError}</p>}
          {preview && (
            <>
              <p className="font-medium text-violet-900 dark:text-violet-200">
                Matches <strong>{preview.length}</strong> {preview.length === 1 ? 'item' : 'items'}
                {preview.length > 0 ? ':' : '.'}
              </p>
              {preview.length > 0 && (
                <ul className="max-h-40 list-disc space-y-0.5 overflow-y-auto pl-5 text-xs">
                  {preview.map((item) => (
                    <li key={item.rel_path} className="font-mono">
                      {item.rel_path}
                    </li>
                  ))}
                </ul>
              )}
              {preview.length > 0 && (
                <>
                  {resetWarningLines(preview.length, previewRemoteCount, ctx).map((line) => (
                    <p key={line} className="text-violet-900 dark:text-violet-200">
                      {line}
                    </p>
                  ))}
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={confirmPatternReset}
                      disabled={patternBusy}
                      className="rounded-md bg-violet-700 px-2 py-1 text-xs font-medium text-white hover:bg-violet-800 disabled:opacity-50 dark:bg-violet-800 dark:hover:bg-violet-700"
                    >
                      {patternBusy ? 'Resetting…' : `Reset these ${preview.length}`}
                    </button>
                    <button
                      type="button"
                      onClick={() => setPatternOpen(false)}
                      disabled={patternBusy}
                      className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
                    >
                      Cancel
                    </button>
                  </div>
                </>
              )}
            </>
          )}
          {preview && preview.length === 0 && (
            <button
              type="button"
              onClick={() => setPatternOpen(false)}
              className="self-start rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
            >
              Close
            </button>
          )}
          {patternError && <p className="text-red-700 dark:text-red-300">{patternError}</p>}
        </div>
      )}

      {wholeQueueResult && (
        <div className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-xs text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
          <div className="flex items-center justify-between gap-3">
            <span>
              Reset {wholeQueueResult.reset_top_level} item(s) ({wholeQueueResult.affected_count}{' '}
              row(s) forgotten across item/item_settle/deleted_archive)
              {wholeQueueResult.withheld.length > 0 &&
                `, ${wholeQueueResult.withheld.length} withheld`}
              .
            </span>
            <button
              type="button"
              onClick={() => setWholeQueueResult(null)}
              className="shrink-0 text-xs underline decoration-dotted"
            >
              Dismiss
            </button>
          </div>
          <WithheldList withheld={wholeQueueResult.withheld} />
        </div>
      )}

      {patternResult && (
        <div className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-xs text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
          <div className="flex items-center justify-between gap-3">
            <span>
              Reset {patternResult.reset_top_level} item(s) ({patternResult.affected_count} row(s)
              forgotten)
              {patternResult.withheld.length > 0 && `, ${patternResult.withheld.length} withheld`}.
            </span>
            <button
              type="button"
              onClick={() => setPatternResult(null)}
              className="shrink-0 text-xs underline decoration-dotted"
            >
              Dismiss
            </button>
          </div>
          <WithheldList withheld={patternResult.withheld} />
        </div>
      )}

      {wholeQueueError && (
        <div className="flex items-center justify-between gap-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-900 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200">
          <span>{wholeQueueError}</span>
          <button
            type="button"
            onClick={() => setWholeQueueError(null)}
            className="shrink-0 text-xs underline decoration-dotted"
          >
            Dismiss
          </button>
        </div>
      )}
    </div>
  )
}
