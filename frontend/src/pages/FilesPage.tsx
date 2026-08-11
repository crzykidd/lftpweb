import { useState } from 'react'
import { rescanFiles } from '../api/client'
import { FileTree } from '../components/FileTree'
import { useFilesSocket } from '../hooks/useFilesSocket'

/** DESIGN.md §9.2 Files page — read-only this phase (no job engine yet to queue/stop/delete
 * against). Live updates over the one WebSocket (§9), grouped and collapsible per queue.
 */
export function FilesPage() {
  const { queues, state } = useFilesSocket()
  const [rescanning, setRescanning] = useState(false)

  const handleRescan = async () => {
    setRescanning(true)
    try {
      await rescanFiles()
    } finally {
      setTimeout(() => setRescanning(false), 1000)
    }
  }

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

      {queues.map((queue) => (
        <section key={queue.queue_id} className="flex flex-col gap-2">
          <div className="flex items-baseline gap-2">
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">{queue.queue_name}</h2>
            <span className="text-xs text-zinc-500 dark:text-zinc-400">
              {queue.scanned_at ? `scanned ${new Date(queue.scanned_at).toLocaleTimeString()}` : 'not scanned yet'}
            </span>
            {queue.error && (
              <span className="text-xs text-red-600 dark:text-red-400">scan error: {queue.error}</span>
            )}
          </div>
          <FileTree nodes={queue.nodes} />
        </section>
      ))}
    </div>
  )
}
