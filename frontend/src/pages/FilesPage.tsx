import { useEffect, useRef, useState } from 'react'
import { listQueues, rescanFiles } from '../api/client'
import type { PathQueueOut } from '../api/types'
import { FileTree } from '../components/FileTree'
import { QueueResetControls } from '../components/QueueResetControls'
import { useLiveModel } from '../hooks/useLiveModel'
import { formatRelativeTime } from '../lib/format'

/** DESIGN.md §9.2 Files page. Live updates over the one WebSocket (§9, DESIGN.md's delta
 * contract — see `hooks/useLiveModel.ts`), grouped and collapsible per queue. Phase 3b adds
 * the actions phase 2 explicitly deferred (no job engine existed yet): Queue / Stop per row
 * and in bulk via multi-select (`FileTree.tsx`), plus virtualization.
 */
export function FilesPage() {
  const { queues, state, scanCompleteSeq, speedByItemId } = useLiveModel()
  const [rescanning, setRescanning] = useState(false)

  // The `path_queue` config this page's own `useLiveModel` reading never carries (`QueueFiles`
  // is deliberately just the tree -- `queue_id`/`queue_name`/nodes; see hooks/useLiveModel.ts's
  // own `QueueState`). Fetched once via the same REST endpoint Settings → Queues uses, and
  // needed here for exactly one thing added 2026-08-13
  // (`prompts/2026-08-13-reset-item-tracking.md`): `FileTree`'s "Reset selected" and
  // `QueueResetControls`'s whole-queue/purge-by-pattern panels need `sync_mode`/
  // `auto_queue_enabled`/`scan_interval_s` to state the real re-download consequence rather
  // than a generic hedge. Not live-updated over the socket -- these change rarely, and a stale
  // read for the few seconds after an edit in Settings is a fine trade against a second
  // WS-driven cache for config that already has one.
  const [queueConfigs, setQueueConfigs] = useState<Record<number, PathQueueOut>>({})
  useEffect(() => {
    listQueues()
      .then((rows) => setQueueConfigs(Object.fromEntries(rows.map((q) => [q.id, q]))))
      .catch(() => {
        // Degrades gracefully: FileTree/QueueResetControls below fall back to safe defaults
        // (`copy`/`false`/`null`) when a queue's config hasn't loaded yet, same as
        // `settleSettings`'s own load failure already does elsewhere on this page.
      })
  }, [])
  // The sequence value seen right before this rescan was requested -- `POST
  // /api/files/rescan` (`api/files.py`) only sets the engine's wake event and returns 202
  // immediately, so completion can only be observed on the wire, not from the response. A
  // bare `setTimeout(…, 1000)` used to fake it, which was simply wrong on any tree that took
  // longer than a second and stayed "Rescanning…" for exactly 1s even when a scan failed
  // outright. See docs/decisions.md for why this is a WebSocket message rather than a
  // blocking endpoint.
  const rescanBaselineSeq = useRef<number | null>(null)

  const handleRescan = async () => {
    rescanBaselineSeq.current = scanCompleteSeq
    setRescanning(true)
    try {
      await rescanFiles()
    } catch {
      // The request itself failed (network/HTTP) -- there will be no `scan_complete` to
      // clear this, since the engine's wake event was never even set.
      setRescanning(false)
      rescanBaselineSeq.current = null
    }
  }

  // Clears on the first `scan_complete` (any queue) whose sequence number moved past the
  // baseline captured above -- not a timer. Every enabled queue gets its own scan_complete
  // per pass (`core/engine.py.scan_all` iterates them in sequence), so on a multi-queue
  // install this clears on the first queue to finish rather than waiting for all of them;
  // DESIGN.md's rescan button is instance-wide, not per-queue, and there is no request id in
  // the wire protocol to correlate a specific rescan to a specific completion.
  useEffect(() => {
    if (rescanning && rescanBaselineSeq.current !== null && scanCompleteSeq !== rescanBaselineSeq.current) {
      setRescanning(false)
      rescanBaselineSeq.current = null
    }
  }, [scanCompleteSeq, rescanning])

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm">
          {state === 'open' && <span className="text-emerald-600 dark:text-emerald-400">● live</span>}
          {state === 'connecting' && <span className="text-zinc-400">○ connecting…</span>}
          {state === 'reconnecting' && <span className="text-amber-600 dark:text-amber-400">○ reconnecting…</span>}
        </div>
        <button
          type="button"
          onClick={handleRescan}
          disabled={rescanning}
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          {rescanning ? 'Rescanning…' : 'Rescan now'}
        </button>
      </div>

      {queues.length === 0 && (
        <div className="flex h-40 items-center justify-center rounded-lg border border-dashed border-zinc-300 text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
          No queues configured yet — add one in Settings → Queues.
        </div>
      )}

      {queues.map((queue) => {
        const config = queueConfigs[queue.queue_id]
        return (
          <section key={queue.queue_id} className="flex flex-col gap-2">
            <div className="flex items-baseline gap-2">
              <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">{queue.queue_name}</h2>
              {/* Relative reading driven by `scan_complete` (`useLiveModel.ts`), the readout the
               * user actually asked for when they asked for a refresh dropdown -- it makes the
               * 30s `scan_interval_s` visible instead of something inferred from rows not
               * changing. Absolute timestamp on hover via `title`, not a second visible string.
               * A warning surfaces right here rather than only in a log, per the same message. */}
              <span
                className="text-xs text-zinc-500 dark:text-zinc-400"
                title={queue.scanned_at ? new Date(queue.scanned_at).toLocaleString() : undefined}
              >
                {queue.scanned_at ? `scanned ${formatRelativeTime(queue.scanned_at)}` : 'not scanned yet'}
                {queue.warning ? ' ⚠' : ''}
              </span>
              {queue.error && (
                <span className="text-xs text-red-600 dark:text-red-400">scan error: {queue.error}</span>
              )}
              {!queue.error && queue.warning && (
                <span className="text-xs text-amber-600 dark:text-amber-400" title={queue.warning}>
                  {queue.warning}
                </span>
              )}
            </div>
            <FileTree
              nodes={queue.nodes}
              connected={state === 'open'}
              syncMode={config?.sync_mode ?? 'copy'}
              autoQueueEnabled={config?.auto_queue_enabled ?? false}
              scanIntervalS={config?.scan_interval_s ?? null}
              speedByItemId={speedByItemId}
            />
            {/* Queue-scoped reset controls (2026-08-13, prompts/2026-08-13-reset-item-tracking.md)
             * -- the whole-queue and purge-by-pattern scopes, sitting below the tree rather than
             * inside its toolbar since they act on the queue as a whole, not on a selection. */}
            <QueueResetControls
              queueId={queue.queue_id}
              queueName={queue.queue_name}
              syncMode={config?.sync_mode ?? 'copy'}
              autoQueueEnabled={config?.auto_queue_enabled ?? false}
              scanIntervalS={config?.scan_interval_s ?? null}
              nodes={queue.nodes}
            />
          </section>
        )
      })}
    </div>
  )
}
