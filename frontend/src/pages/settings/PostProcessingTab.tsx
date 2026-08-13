import { useEffect, useState } from 'react'
import { getPostprocessSettings, putPostprocessSettings } from '../../api/client'
import type { PostprocessSettingsOut } from '../../api/types'

const inputClasses =
  'w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const labelClasses = 'text-sm font-medium text-zinc-700 dark:text-zinc-300'

const EMPTY: PostprocessSettingsOut = {
  verify_enabled: false,
  verify_hash_on_disk: false,
  extract_enabled: false,
  extract_target_dir: null,
  extract_passwords: [],
  delete_archives_after_extract: false,
  move_enabled: false,
  concurrency: 1,
}

/** Settings → Post-processing (DESIGN.md §6, phase 5): the *site-wide* defaults for
 * verify/extract/move. A step only actually runs for a given queue's items when both this
 * global flag AND that queue's own auto_verify/auto_extract/auto_move toggle (Settings →
 * Queues) are on -- see docs/decisions.md for why the two layers are ANDed rather than one
 * overriding the other. Every field here defaults off; a fresh install runs no
 * post-processing at all even before anyone visits this page.
 */
export function PostProcessingTab() {
  const [settings, setSettings] = useState<PostprocessSettingsOut>(EMPTY)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [passwordsText, setPasswordsText] = useState('')

  useEffect(() => {
    getPostprocessSettings()
      .then((s) => {
        setSettings(s)
        setPasswordsText(s.extract_passwords.join('\n'))
      })
      .finally(() => setLoading(false))
  }, [])

  const update = <K extends keyof PostprocessSettingsOut>(
    key: K,
    value: PostprocessSettingsOut[K],
  ) => setSettings((prev) => ({ ...prev, [key]: value }))

  const handleSave = async () => {
    setError(null)
    setSaving(true)
    try {
      const body: PostprocessSettingsOut = {
        ...settings,
        extract_passwords: passwordsText
          .split('\n')
          .map((p) => p.trim())
          .filter(Boolean),
        concurrency: Math.max(1, settings.concurrency),
      }
      const saved = await putPostprocessSettings(body)
      setSettings(saved)
      setPasswordsText(saved.extract_passwords.join('\n'))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>

  return (
    <div className="flex max-w-xl flex-col gap-6">
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        A step here only runs for a queue's items when it's also turned on for that queue in
        Settings → Queues — both are off by default (DESIGN.md §6).
      </p>

      <div className="flex flex-col gap-2 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Verify</h3>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={settings.verify_enabled}
            onChange={(e) => update('verify_enabled', e.target.checked)}
          />
          <span className="text-sm text-zinc-700 dark:text-zinc-300">
            Verify completed items against .sfv/.md5 sidecars
          </span>
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={settings.verify_hash_on_disk}
            onChange={(e) => update('verify_hash_on_disk', e.target.checked)}
          />
          <span className="text-sm text-zinc-700 dark:text-zinc-300">
            When no sidecar is present, read every file fully as a weaker fallback (confirms
            the bytes are on disk and readable, not that they're correct)
          </span>
        </label>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          A <code>move</code>-mode queue always verifies before deleting the remote copy,
          regardless of this toggle (DESIGN.md §6/§7.3) — it's the sole gate on an irreversible
          delete. With no sidecar and this fallback off, a move queue's items report no
          verification evidence and the delete is withheld, not silently skipped.
        </p>
      </div>

      <div className="flex flex-col gap-2 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Extract</h3>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={settings.extract_enabled}
            onChange={(e) => update('extract_enabled', e.target.checked)}
          />
          <span className="text-sm text-zinc-700 dark:text-zinc-300">
            Extract archives (7zz — zip/7z/rar/rar5/tar/gz/bz2/xz)
          </span>
        </label>
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Extraction target directory (optional)</span>
          <input
            className={inputClasses}
            placeholder="Leave blank to extract in place, next to the archive"
            value={settings.extract_target_dir ?? ''}
            onChange={(e) => update('extract_target_dir', e.target.value || null)}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Password list (one per line, tried in order)</span>
          <textarea
            className={`${inputClasses} min-h-20 font-mono`}
            value={passwordsText}
            onChange={(e) => setPasswordsText(e.target.value)}
          />
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={settings.delete_archives_after_extract}
            onChange={(e) => update('delete_archives_after_extract', e.target.checked)}
          />
          <span className="text-sm text-zinc-700 dark:text-zinc-300">
            Delete archive volumes (.rar/.r00/.7z/.zip/...) once they've extracted successfully
          </span>
        </label>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Only ever deletes files extraction actually used, and only on a full success — never
          on a failed or incomplete extraction. Sidecars (.sfv/.md5), .nfo files, and samples
          are never touched. Off by default, like every other capability in this project that
          deletes something.
        </p>
      </div>

      <div className="flex flex-col gap-2 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Move</h3>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={settings.move_enabled}
            onChange={(e) => update('move_enabled', e.target.checked)}
          />
          <span className="text-sm text-zinc-700 dark:text-zinc-300">
            Move finished items to each queue's final destination (needs a queue's own
            "Final destination" and "Move to staging path" set in Settings → Queues)
          </span>
        </label>
      </div>

      <label className="flex flex-col gap-1">
        <span className={labelClasses}>Concurrency</span>
        <input
          type="number"
          min={1}
          className={`${inputClasses} max-w-32`}
          value={settings.concurrency}
          onChange={(e) => update('concurrency', Math.max(1, Number(e.target.value) || 1))}
        />
        <span className="text-xs text-zinc-500 dark:text-zinc-400">
          How many items post-processing works on at once (DESIGN.md §6) — 1 by default.
        </span>
      </label>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      <button
        type="button"
        disabled={saving}
        onClick={handleSave}
        className="w-fit rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
      >
        {saving ? 'Saving…' : 'Save'}
      </button>
    </div>
  )
}
