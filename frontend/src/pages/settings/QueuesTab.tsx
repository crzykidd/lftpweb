import { useEffect, useState } from 'react'
import { createQueue, deleteQueue, listQueues, updateQueue } from '../../api/client'
import type { PathQueueOut, SyncMode } from '../../api/types'

const inputClasses =
  'w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const labelClasses = 'text-sm font-medium text-zinc-700 dark:text-zinc-300'

interface FormState {
  name: string
  remote_path: string
  local_path: string
  staging_path: string
  enabled: boolean
  sync_mode: SyncMode
}

const EMPTY_FORM: FormState = {
  name: '',
  remote_path: '',
  local_path: '',
  staging_path: '',
  enabled: true,
  sync_mode: 'copy',
}

/** DESIGN.md §9.2 Settings → Queues, scoped to what phase 2 has: add/edit/remove named path
 * queues with their remote → local mapping. Patterns, the live preview, and post-processing
 * toggles are phase 4/5 (§13) — this is deliberately just the CRUD surface.
 */
export function QueuesTab() {
  const [queues, setQueues] = useState<PathQueueOut[]>([])
  const [loading, setLoading] = useState(true)
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = () => listQueues().then(setQueues)

  useEffect(() => {
    refresh().finally(() => setLoading(false))
  }, [])

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const startEdit = (queue: PathQueueOut) => {
    setEditingId(queue.id)
    setForm({
      name: queue.name,
      remote_path: queue.remote_path,
      local_path: queue.local_path,
      staging_path: queue.staging_path ?? '',
      enabled: queue.enabled,
      sync_mode: queue.sync_mode,
    })
  }

  const cancelEdit = () => {
    setEditingId(null)
    setForm(EMPTY_FORM)
  }

  const handleSubmit = async () => {
    setError(null)
    const body = {
      name: form.name,
      remote_path: form.remote_path,
      local_path: form.local_path,
      staging_path: form.staging_path || null,
      enabled: form.enabled,
      sync_mode: form.sync_mode,
    }
    try {
      if (editingId != null) {
        await updateQueue(editingId, body)
      } else {
        await createQueue(body)
      }
      cancelEdit()
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const handleDelete = async (id: number) => {
    await deleteQueue(id)
    if (editingId === id) cancelEdit()
    await refresh()
  }

  if (loading) return <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>

  return (
    <div className="flex flex-col gap-6">
      <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
            <tr>
              <th className="px-3 py-2 font-medium">Name</th>
              <th className="px-3 py-2 font-medium">Remote path</th>
              <th className="px-3 py-2 font-medium">Local path</th>
              <th className="px-3 py-2 font-medium">Sync mode</th>
              <th className="px-3 py-2 font-medium">Enabled</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {queues.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-4 text-center text-zinc-400">
                  No queues yet.
                </td>
              </tr>
            )}
            {queues.map((q) => (
              <tr key={q.id} className="border-t border-zinc-100 dark:border-zinc-900">
                <td className="px-3 py-2">{q.name}</td>
                <td className="px-3 py-2 font-mono text-xs">{q.remote_path}</td>
                <td className="px-3 py-2 font-mono text-xs">{q.local_path}</td>
                <td className="px-3 py-2">{q.sync_mode}</td>
                <td className="px-3 py-2">{q.enabled ? 'yes' : 'no'}</td>
                <td className="px-3 py-2 text-right">
                  <button
                    type="button"
                    onClick={() => startEdit(q)}
                    className="mr-2 text-zinc-600 hover:underline dark:text-zinc-300"
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDelete(q.id)}
                    className="text-red-600 hover:underline dark:text-red-400"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex max-w-lg flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          {editingId != null ? 'Edit queue' : 'Add a queue'}
        </h3>

        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Name</span>
          <input className={inputClasses} value={form.name} onChange={(e) => update('name', e.target.value)} />
        </label>
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Remote path</span>
          <input
            className={inputClasses}
            value={form.remote_path}
            onChange={(e) => update('remote_path', e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Local path</span>
          <input
            className={inputClasses}
            value={form.local_path}
            onChange={(e) => update('local_path', e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Staging path (optional)</span>
          <input
            className={inputClasses}
            value={form.staging_path}
            onChange={(e) => update('staging_path', e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>
            Sync mode
            {form.sync_mode !== 'copy' && (
              <span className="ml-2 font-normal text-amber-600 dark:text-amber-400">
                ⚠ only point this at a hardlink pickup directory, never a live torrent data
                directory (DESIGN.md §7.1) — {form.sync_mode} deletes the remote copy.
              </span>
            )}
          </span>
          <select
            className={inputClasses}
            value={form.sync_mode}
            onChange={(e) => update('sync_mode', e.target.value as SyncMode)}
          >
            <option value="copy">copy — download only, never touches the remote (default)</option>
            <option value="move" disabled>move — not yet implemented (DESIGN.md §13 phase 5)</option>
            <option value="sync" disabled>sync — not scheduled (DESIGN.md §7)</option>
          </select>
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={form.enabled} onChange={(e) => update('enabled', e.target.checked)} />
          <span className={labelClasses}>Enabled</span>
        </label>

        {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

        <div className="flex gap-2">
          <button
            type="button"
            onClick={handleSubmit}
            className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
          >
            {editingId != null ? 'Save' : 'Add queue'}
          </button>
          {editingId != null && (
            <button
              type="button"
              onClick={cancelEdit}
              className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
            >
              Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
