import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  createPattern,
  createQueue,
  deleteQueue,
  deletePattern,
  getAutoQueueSettings,
  getAutoQueueStatus,
  getPostprocessSettings,
  listPatterns,
  listQueues,
  previewPatterns,
  putAutoQueueSettings,
  updateQueue,
} from '../../api/client'
import type {
  AutoQueueSettingsOut,
  PathQueueOut,
  PatternKind,
  PatternOut,
  PatternPreviewResponse,
  PostprocessSettingsOut,
  QueueAutoQueueStatus,
  SyncMode,
} from '../../api/types'

const inputClasses =
  'w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const labelClasses = 'text-sm font-medium text-zinc-700 dark:text-zinc-300'
const hintClasses = 'text-xs text-zinc-500 dark:text-zinc-400'

interface FormState {
  name: string
  remote_path: string
  local_path: string
  staging_path: string
  enabled: boolean
  sync_mode: SyncMode
  auto_queue_enabled: boolean
  auto_queue_patterns_only: boolean
  auto_verify: boolean
  auto_extract: boolean
  auto_move: boolean
  auto_delete_archives: boolean
  scan_interval_s: number | null
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
  auto_verify: false,
  auto_extract: false,
  auto_move: false,
  auto_delete_archives: false,
  scan_interval_s: null,
}

/** Settings → Post-processing's site-wide defaults, fetched here too so each per-queue toggle
 * below can show what it actually resolves to (2026-08-13,
 * `prompts/2026-08-13-per-queue-archive-cleanup.md`, item 2) -- a per-queue toggle can be on
 * while the matching site-wide flag is off, and until this task nothing on this page said so.
 * `null` while loading; the readout below degrades to a neutral "loading" line rather than
 * guessing on or off.
 */
function usePostprocessSiteSettings(): PostprocessSettingsOut | null {
  const [settings, setSettings] = useState<PostprocessSettingsOut | null>(null)
  useEffect(() => {
    getPostprocessSettings()
      .then(setSettings)
      .catch(() => setSettings(null))
  }, [])
  return settings
}

/** One per-queue post-processing toggle's readout: what the site-wide half currently resolves
 * to, and therefore whether the queue's own toggle above actually does anything right now.
 * `forcedOn` is `move`-mode verification's own case -- DESIGN.md §6/§7.3: it runs regardless of
 * *either* toggle, so the readout must say "always on," never "system setting: off" (which
 * would be a lie for exactly that queue).
 */
function PostprocessStepReadout({
  site,
  queueEnabled,
  forcedOn = false,
}: {
  // `null` covers two cases the same way: the site-wide fetch hasn't resolved yet, or it
  // failed -- both render the same "loading" line rather than guessing a value that could be
  // wrong in either direction.
  site: boolean | null
  queueEnabled: boolean
  forcedOn?: boolean
}) {
  if (forcedOn) {
    return (
      <p className={hintClasses}>
        Always runs for this <code>move</code> queue, regardless of the site-wide setting or
        this toggle — verification is the sole gate on the irreversible remote delete
        (DESIGN.md §6/§7.3).
      </p>
    )
  }
  if (site == null) {
    return <p className={hintClasses}>Loading the site-wide setting…</p>
  }
  if (!site) {
    return (
      <p className="text-xs text-amber-600 dark:text-amber-400">
        System setting: off — this queue's toggle has no effect until it's also turned on in{' '}
        <Link to="/settings/post-processing" className="underline">
          Settings → Post-processing
        </Link>
        .
      </p>
    )
  }
  return (
    <p className={hintClasses}>
      System setting: on —{' '}
      {queueEnabled ? 'active for this queue.' : "this queue's toggle above is off, so nothing runs yet."}
    </p>
  )
}

/** The dropdown's own fixed choices (DESIGN.md §5/§9.3; prompts/open-issues.md #11). `null` is
 * "site default" and `0` is "none, on-demand only" -- both reserved by the API/DB, not just
 * this form (`api/types.ts`'s `PathQueueIn.scan_interval_s` doc comment). A queue whose stored
 * value doesn't match one of these four (e.g. set by a direct API call) still round-trips --
 * see `scanIntervalOptions` below, which appends a synthetic entry for it rather than silently
 * snapping to the nearest preset the moment the form is opened.
 */
const SCAN_INTERVAL_CHOICES: { value: string; label: string }[] = [
  { value: 'default', label: 'Site default (30s unless overridden by LFTPWEB_SCAN_INTERVAL_S)' },
  { value: '10', label: '10s' },
  { value: '30', label: '30s' },
  { value: '60', label: '60s' },
  { value: 'none', label: 'None — on-demand only' },
]

function scanIntervalToChoice(value: number | null): string {
  if (value === null) return 'default'
  if (value === 0) return 'none'
  if (value === 10 || value === 30 || value === 60) return String(value)
  return String(value) // a custom value set outside this form -- kept as its own option below
}

function choiceToScanInterval(choice: string): number | null {
  if (choice === 'default') return null
  if (choice === 'none') return 0
  return Number(choice)
}

function formatScanInterval(value: number | null): string {
  if (value === null) return 'default'
  if (value === 0) return 'none'
  return `${value}s`
}

const AUTOQUEUE_SETTINGS_EMPTY: AutoQueueSettingsOut = { re_download_externally_removed: false }

/** Settings → Queues' "Re-download items removed outside lftpweb" section
 * (`core/autoqueue.py.AutoQueueSettings`; reverted+setting-ified 2026-08-12, docs/decisions.md).
 * A self-contained load/save cycle against its own endpoint (`GET`/`PUT /api/settings/
 * autoqueue`), the same idiom `TransferTab.tsx`'s `SettleGateSection` uses -- this is a
 * site-level setting, not a per-queue `path_queue` column, even though it only ever affects
 * auto-queue, which is why it lives here rather than folded into each queue's own form below.
 */
function AutoQueueSettingsSection() {
  const [settings, setSettings] = useState<AutoQueueSettingsOut>(AUTOQUEUE_SETTINGS_EMPTY)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getAutoQueueSettings()
      .then(setSettings)
      .finally(() => setLoading(false))
  }, [])

  const handleToggle = async (re_download_externally_removed: boolean) => {
    setError(null)
    setSaving(true)
    try {
      setSettings(await putAutoQueueSettings({ re_download_externally_removed }))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
      <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
        Re-download items removed outside lftpweb
      </h3>
      <p className={hintClasses}>
        There are two ways an item's local copy can go away: lftpweb deleted it itself (a
        manual delete from Files, or the retention sweep) -- that item is <strong>never</strong>{' '}
        re-fetched, no matter what this setting is. Or something outside lftpweb removed it --
        an <code>*arr</code> importer picking up a finished release, a human, a script. This
        setting controls only the second case.
      </p>
      {loading ? (
        <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>
      ) : (
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={settings.re_download_externally_removed}
            disabled={saving}
            onChange={(e) => handleToggle(e.target.checked)}
          />
          <span className={labelClasses}>
            Re-fetch an item auto-queue's pattern still matches once something outside lftpweb
            removes its local copy
          </span>
        </label>
      )}
      <p className={hintClasses}>
        Off by default. Only matters on a <strong>copy</strong>-mode queue with auto-queue on:
        in <code>copy</code> mode the remote copy is never touched, so if this is on, an item an
        importer just moved into your library re-downloads on the very next scan, gets
        re-imported, and repeats forever -- the concrete case that decided the default is
        Sonarr/Radarr importing on one schedule while a separate script prunes the seedbox on
        another, with every release in between re-fetched and handed to the importer as a
        duplicate. On a <strong>move</strong>-mode queue this changes nothing either way -- the
        remote copy is already deleted once a download verifies, so there is nothing left to
        re-fetch.
      </p>
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
    </div>
  )
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
  // DESIGN.md §7.1's misconfiguration warning: "switching a queue to move requires explicit
  // confirmation." Re-armed (reset to false) on every edit start / cancel / mode change away
  // from 'move', so saving a move queue always requires a fresh, deliberate acknowledgement
  // in *this* editing session rather than a checkbox that silently stays checked.
  const [moveConfirmed, setMoveConfirmed] = useState(false)
  const postprocessSite = usePostprocessSiteSettings()

  const refresh = () => listQueues().then(setQueues)

  useEffect(() => {
    refresh().finally(() => setLoading(false))
  }, [])

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }))
    if (key === 'sync_mode' && value !== 'move') setMoveConfirmed(false)
  }

  const startEdit = (queue: PathQueueOut) => {
    setEditingId(queue.id)
    setMoveConfirmed(false)
    setForm({
      name: queue.name,
      remote_path: queue.remote_path,
      local_path: queue.local_path,
      staging_path: queue.staging_path ?? '',
      enabled: queue.enabled,
      sync_mode: queue.sync_mode,
      auto_queue_enabled: queue.auto_queue_enabled,
      auto_queue_patterns_only: queue.auto_queue_patterns_only,
      auto_verify: queue.auto_verify,
      auto_extract: queue.auto_extract,
      auto_move: queue.auto_move,
      auto_delete_archives: queue.auto_delete_archives,
      scan_interval_s: queue.scan_interval_s,
    })
  }

  const cancelEdit = () => {
    setEditingId(null)
    setMoveConfirmed(false)
    setForm(EMPTY_FORM)
  }

  // DESIGN.md §6: "auto_verify is forced on and cannot be turned off in the UI" for a move
  // queue -- it is the sole gate on an irreversible remote delete (§7.3). The backend also
  // forces this server-side (api/settings.py._effective_auto_verify) so a direct API call
  // can't bypass it; this mirrors that in the form so the checkbox never lies about what
  // will actually be saved.
  const effectiveAutoVerify = form.auto_verify || form.sync_mode === 'move'

  const handleSubmit = async () => {
    setError(null)
    if (form.sync_mode === 'move' && !moveConfirmed) {
      setError(
        'Confirm the hardlink-pickup-directory checkbox below before saving a move queue.',
      )
      return
    }
    const body = {
      name: form.name,
      remote_path: form.remote_path,
      local_path: form.local_path,
      staging_path: form.staging_path || null,
      enabled: form.enabled,
      sync_mode: form.sync_mode,
      auto_queue_enabled: form.auto_queue_enabled,
      auto_queue_patterns_only: form.auto_queue_patterns_only,
      auto_verify: effectiveAutoVerify,
      auto_extract: form.auto_extract,
      auto_move: form.auto_move,
      auto_delete_archives: form.auto_delete_archives,
      scan_interval_s: form.scan_interval_s,
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
      <AutoQueueSettingsSection />

      <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
            <tr>
              <th className="px-3 py-2 font-medium">Name</th>
              <th className="px-3 py-2 font-medium">Remote path</th>
              <th className="px-3 py-2 font-medium">Local path</th>
              <th className="px-3 py-2 font-medium">Sync mode</th>
              <th className="px-3 py-2 font-medium">Enabled</th>
              <th className="px-3 py-2 font-medium">Scan interval</th>
              <th className="px-3 py-2 font-medium">Auto-queue</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {queues.length === 0 && (
              <tr>
                <td colSpan={8} className="px-3 py-4 text-center text-zinc-400">
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
                <td className="px-3 py-2">{formatScanInterval(q.scan_interval_s)}</td>
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
          <span className={labelClasses}>
            Final destination (optional)
            <span className="ml-2 font-normal text-zinc-500 dark:text-zinc-400">
              — downloads always land in Local path above; the post-processing "Move to
              staging path" step (DESIGN.md §6) relocates a finished item here, e.g. from an
              NVMe download cache onto the array. Leave blank to keep items in Local path.
            </span>
          </span>
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
            <option value="move">
              move — download, verify, then delete the remote copy (DESIGN.md §7)
            </option>
            <option value="sync" disabled>sync — not scheduled (DESIGN.md §7)</option>
          </select>
        </label>
        {form.sync_mode === 'move' && (
          <div className="flex flex-col gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-900/20">
            <p className="text-sm text-amber-900 dark:text-amber-200">
              <strong>move</strong> deletes the remote copy of every item, once verified, the
              moment it finishes downloading. This is irreversible. It is only safe when the
              remote path above is a <strong>hardlink pickup directory</strong> your torrent
              client populates on completion — never the torrent client's own seeding data
              directory. Pointing this at a live seeding directory will destroy your seeds
              (DESIGN.md §7.1).
            </p>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={moveConfirmed}
                onChange={(e) => setMoveConfirmed(e.target.checked)}
              />
              <span className="text-sm font-medium text-amber-900 dark:text-amber-200">
                I confirm {form.remote_path || 'the remote path above'} is a hardlink pickup
                directory, not live seeding data.
              </span>
            </label>
          </div>
        )}
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={form.enabled} onChange={(e) => update('enabled', e.target.checked)} />
          <span className={labelClasses}>Enabled</span>
        </label>

        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Scan interval (DESIGN.md §5/§9.3)</span>
          <select
            className={inputClasses}
            value={scanIntervalToChoice(form.scan_interval_s)}
            onChange={(e) => update('scan_interval_s', choiceToScanInterval(e.target.value))}
          >
            {[
              ...SCAN_INTERVAL_CHOICES,
              ...(SCAN_INTERVAL_CHOICES.some((c) => c.value === scanIntervalToChoice(form.scan_interval_s))
                ? []
                : [
                    {
                      value: scanIntervalToChoice(form.scan_interval_s),
                      label: `Custom: ${form.scan_interval_s}s (set outside this form)`,
                    },
                  ]),
            ].map((choice) => (
              <option key={choice.value} value={choice.value}>
                {choice.label}
              </option>
            ))}
          </select>
          {scanIntervalToChoice(form.scan_interval_s) === '10' && (
            <span className="text-xs text-amber-600 dark:text-amber-400">
              ⚠ A scan is an SSH round trip running <code>find</code> over the entire remote
              tree. Every 10s is real, continuous load on a shared seedbox — use it only if you
              need this queue's changes to show up fast and the seedbox can take it.
            </span>
          )}
          {scanIntervalToChoice(form.scan_interval_s) === 'none' && (
            <span className={hintClasses}>
              This queue is never scanned on a timer — only "Rescan now" (Files page) or a
              settings change here triggers a pass. Auto-queue only runs at the end of a scan
              pass (DESIGN.md §4.7), so with auto-queue on, new remote items will not be picked
              up automatically until something forces a scan.
            </span>
          )}
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

        <div className="flex flex-col gap-2 rounded-md border border-zinc-200 p-3 dark:border-zinc-800">
          <span className={labelClasses}>
            Post-processing (DESIGN.md §6) — off by default; also gated by the site-wide
            defaults in Settings → Post-processing
          </span>
          <div className="flex flex-col gap-1">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={effectiveAutoVerify}
                onChange={(e) => update('auto_verify', e.target.checked)}
                disabled={form.sync_mode === 'move'}
              />
              <span className="text-sm text-zinc-700 dark:text-zinc-300">
                Verify (.sfv/.md5, or hash-on-disk if enabled site-wide)
                {form.sync_mode === 'move' && (
                  <span className="ml-1 text-amber-600 dark:text-amber-400">
                    — forced on for move (it gates the remote delete)
                  </span>
                )}
              </span>
            </label>
            <PostprocessStepReadout
              site={postprocessSite && postprocessSite.verify_enabled}
              queueEnabled={form.auto_verify}
              forcedOn={form.sync_mode === 'move'}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={form.auto_extract}
                onChange={(e) => update('auto_extract', e.target.checked)}
              />
              <span className="text-sm text-zinc-700 dark:text-zinc-300">
                Extract archives (7zz — zip/7z/rar/rar5/tar/gz/bz2/xz)
              </span>
            </label>
            <PostprocessStepReadout
              site={postprocessSite && postprocessSite.extract_enabled}
              queueEnabled={form.auto_extract}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={form.auto_delete_archives}
                onChange={(e) => update('auto_delete_archives', e.target.checked)}
                disabled={!form.auto_extract}
              />
              <span className="text-sm text-zinc-700 dark:text-zinc-300">
                Delete archive volumes once they've extracted successfully
                {!form.auto_extract && ' (turn on Extract above first)'}
              </span>
            </label>
            <PostprocessStepReadout
              site={postprocessSite && postprocessSite.delete_archives_after_extract}
              queueEnabled={form.auto_delete_archives}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={form.auto_move}
                onChange={(e) => update('auto_move', e.target.checked)}
                disabled={!form.staging_path}
              />
              <span className="text-sm text-zinc-700 dark:text-zinc-300">
                Move to staging path once finished
                {!form.staging_path && ' (set a staging path above first)'}
              </span>
            </label>
            <PostprocessStepReadout
              site={postprocessSite && postprocessSite.move_enabled}
              queueEnabled={form.auto_move}
            />
          </div>
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
