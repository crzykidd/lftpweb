import { useEffect, useState } from 'react'
import {
  createPattern,
  createQueue,
  deleteQueue,
  deletePattern,
  getAutoQueueStatus,
  listPatterns,
  listQueues,
  previewPatterns,
  updateQueue,
} from '../../api/client'
import type {
  PathQueueOut,
  PatternKind,
  PatternOut,
  PatternPreviewResponse,
  QueueAutoQueueStatus,
  SyncMode,
} from '../../api/types'

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
  auto_queue_enabled: boolean
  auto_queue_patterns_only: boolean
}

const EMPTY_FORM: FormState = {
  name: '',
  remote_path: '',
  local_path: '',
  staging_path: '',
  enabled: true,
  sync_mode: 'copy',
  auto_queue_enabled: false,
  auto_queue_patterns_only: false,
}

/** DESIGN.md §9.2 Settings → Queues: add/edit/remove named path queues with their remote →
 * local mapping, sync mode, per-queue auto-queue toggles (§4.7, default off), and the
 * pattern editor with its live "what would this match" preview. Post-processing toggles are
 * phase 5 -- still out of scope here.
 */
export function QueuesTab() {
  const [queues, setQueues] = useState<PathQueueOut[]>([])
  const [loading, setLoading] = useState(true)
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [patternsQueueId, setPatternsQueueId] = useState<number | null>(null)

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
      auto_queue_enabled: queue.auto_queue_enabled,
      auto_queue_patterns_only: queue.auto_queue_patterns_only,
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
      auto_queue_enabled: form.auto_queue_enabled,
      auto_queue_patterns_only: form.auto_queue_patterns_only,
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
    if (patternsQueueId === id) setPatternsQueueId(null)
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
              <th className="px-3 py-2 font-medium">Auto-queue</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {queues.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-4 text-center text-zinc-400">
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
                <td className="px-3 py-2">
                  {q.auto_queue_enabled
                    ? q.auto_queue_patterns_only
                      ? 'on (patterns-only)'
                      : 'on'
                    : 'off'}
                </td>
                <td className="px-3 py-2 text-right whitespace-nowrap">
                  <button
                    type="button"
                    onClick={() => setPatternsQueueId(q.id)}
                    className="mr-2 text-zinc-600 hover:underline dark:text-zinc-300"
                  >
                    Patterns
                  </button>
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

        <div className="flex flex-col gap-2 rounded-md border border-zinc-200 p-3 dark:border-zinc-800">
          <span className={labelClasses}>Auto-queue (DESIGN.md §4.7) — off by default</span>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={form.auto_queue_enabled}
              onChange={(e) => update('auto_queue_enabled', e.target.checked)}
            />
            <span className="text-sm text-zinc-700 dark:text-zinc-300">
              Auto-queue new remote items matching the patterns below
            </span>
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={form.auto_queue_patterns_only}
              onChange={(e) => update('auto_queue_patterns_only', e.target.checked)}
              disabled={!form.auto_queue_enabled}
            />
            <span className="text-sm text-zinc-700 dark:text-zinc-300">
              Patterns-only (with no <code>select</code> pattern, match nothing instead of
              everything)
            </span>
          </label>
        </div>

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

      {patternsQueueId != null && (
        <PatternsEditor
          queue={queues.find((q) => q.id === patternsQueueId) ?? null}
          onClose={() => setPatternsQueueId(null)}
        />
      )}
    </div>
  )
}

const KIND_HINT: Record<PatternKind, string> = {
  select: 'which items auto-queue picks up (item name)',
  skip: 'items auto-queue never picks up (item name)',
  file_exclude: 'files never transferred at all (file name, any depth)',
}

interface NewPatternForm {
  kind: PatternKind
  expr: string
  global: boolean
}

const EMPTY_PATTERN_FORM: NewPatternForm = { kind: 'select', expr: '', global: false }

/** DESIGN.md §9.2's "live 'what would this match' preview against the current remote tree" —
 * patterns are the feature most likely to be subtly wrong, and this is the cheap fix: show
 * the answer before anything is saved. Pattern add/remove is applied immediately (there's no
 * separate save step for patterns themselves, unlike the queue form above); the preview
 * button re-evaluates against whatever is currently saved for this queue plus the pattern
 * currently being composed, so a mistake is visible before an auto-queue pass could act on it.
 */
function PatternsEditor({ queue, onClose }: { queue: PathQueueOut | null; onClose: () => void }) {
  const [patterns, setPatterns] = useState<PatternOut[]>([])
  const [status, setStatus] = useState<QueueAutoQueueStatus | null>(null)
  const [newPattern, setNewPattern] = useState<NewPatternForm>(EMPTY_PATTERN_FORM)
  const [preview, setPreview] = useState<PatternPreviewResponse | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const queueId = queue?.id ?? null

  const refresh = () => {
    if (queueId == null) return
    listPatterns(queueId).then(setPatterns)
    getAutoQueueStatus(queueId).then(setStatus).catch(() => setStatus(null))
  }

  useEffect(() => {
    setPreview(null)
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queueId])

  if (queue == null || queueId == null) return null

  const handleAdd = async () => {
    if (!newPattern.expr.trim()) return
    setBusy(true)
    try {
      await createPattern({
        queue_id: newPattern.global ? null : queueId,
        kind: newPattern.kind,
        expr: newPattern.expr.trim(),
        enabled: true,
      })
      setNewPattern(EMPTY_PATTERN_FORM)
      refresh()
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async (id: number) => {
    await deletePattern(id)
    refresh()
  }

  const handlePreview = async () => {
    setPreviewError(null)
    try {
      const draft = [
        ...patterns.map((p) => ({ queue_id: p.queue_id, kind: p.kind, expr: p.expr, enabled: p.enabled })),
      ]
      if (newPattern.expr.trim()) {
        draft.push({
          queue_id: newPattern.global ? null : queueId,
          kind: newPattern.kind,
          expr: newPattern.expr.trim(),
          enabled: true,
        })
      }
      const result = await previewPatterns(queueId, {
        patterns: draft,
        patterns_only: queue.auto_queue_patterns_only,
      })
      setPreview(result)
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="flex flex-col gap-4 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          Patterns — {queue.name}
        </h3>
        <button type="button" onClick={onClose} className="text-sm text-zinc-500 hover:underline">
          Close
        </button>
      </div>

      {status != null && !status.mount_ok && queue.auto_queue_enabled && (
        <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:bg-amber-900/30 dark:text-amber-300">
          ⚠ Auto-queue is currently gated off for this queue: the local mount hasn't confirmed
          present, readable, and containing the sentinel yet (DESIGN.md §7.3). No auto-queue
          action will be taken until it does.
          {status.gated_reason ? ` (${status.gated_reason})` : ''}
        </p>
      )}

      <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
            <tr>
              <th className="px-3 py-2 font-medium">Kind</th>
              <th className="px-3 py-2 font-medium">Expression</th>
              <th className="px-3 py-2 font-medium">Scope</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {patterns.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-4 text-center text-zinc-400">
                  No patterns yet — everything matches (unless patterns-only is on).
                </td>
              </tr>
            )}
            {patterns.map((p) => (
              <tr key={p.id} className="border-t border-zinc-100 dark:border-zinc-900">
                <td className="px-3 py-2">{p.kind}</td>
                <td className="px-3 py-2 font-mono text-xs">{p.expr}</td>
                <td className="px-3 py-2">{p.queue_id == null ? 'global' : 'this queue'}</td>
                <td className="px-3 py-2 text-right">
                  <button
                    type="button"
                    onClick={() => handleDelete(p.id)}
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

      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Kind</span>
          <select
            className={inputClasses}
            value={newPattern.kind}
            onChange={(e) => setNewPattern((p) => ({ ...p, kind: e.target.value as PatternKind }))}
          >
            <option value="select">select</option>
            <option value="skip">skip</option>
            <option value="file_exclude">file_exclude</option>
          </select>
        </label>
        <label className="flex flex-1 flex-col gap-1">
          <span className={labelClasses}>Expression ({KIND_HINT[newPattern.kind]})</span>
          <input
            className={inputClasses}
            placeholder="*.nfo, 1080p, *SAMPLE*…"
            value={newPattern.expr}
            onChange={(e) => setNewPattern((p) => ({ ...p, expr: e.target.value }))}
          />
        </label>
        <label className="flex items-center gap-2 pb-1.5">
          <input
            type="checkbox"
            checked={newPattern.global}
            onChange={(e) => setNewPattern((p) => ({ ...p, global: e.target.checked }))}
          />
          <span className="text-sm text-zinc-700 dark:text-zinc-300">Global (every queue)</span>
        </label>
        <button
          type="button"
          disabled={busy}
          onClick={handleAdd}
          className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          Add
        </button>
      </div>

      <div className="flex flex-col gap-2">
        <button
          type="button"
          onClick={handlePreview}
          className="w-fit rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          Preview what this would match
        </button>
        {previewError && <p className="text-sm text-red-600 dark:text-red-400">{previewError}</p>}
        {preview && (
          <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-3 text-sm dark:border-zinc-800">
            <div>
              <p className="mb-1 font-medium text-zinc-700 dark:text-zinc-300">
                Items ({preview.items.filter((i) => i.matched).length} of {preview.items.length}{' '}
                selected)
              </p>
              <ul className="flex flex-col gap-0.5">
                {preview.items.map((item) => (
                  <li
                    key={item.rel_path}
                    className={item.matched ? 'text-emerald-700 dark:text-emerald-400' : 'text-zinc-400'}
                  >
                    {item.matched ? '✓ selected' : '· skipped'} — {item.rel_path}
                    {item.is_dir ? '/' : ''}
                  </li>
                ))}
                {preview.items.length === 0 && <li className="text-zinc-400">Nothing scanned yet.</li>}
              </ul>
            </div>
            {preview.sample_item && (
              <div>
                <p className="mb-1 font-medium text-zinc-700 dark:text-zinc-300">
                  Files inside <span className="font-mono">{preview.sample_item}/</span>
                </p>
                <ul className="flex flex-col gap-0.5">
                  {preview.sample_files.map((f) => (
                    <li
                      key={f.rel_path}
                      className={f.excluded ? 'text-red-600 line-through dark:text-red-400' : 'text-zinc-600 dark:text-zinc-300'}
                    >
                      {f.excluded ? 'excluded' : 'included'} — {f.rel_path}
                    </li>
                  ))}
                  {preview.sample_files.length === 0 && (
                    <li className="text-zinc-400">No files under this item yet.</li>
                  )}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
