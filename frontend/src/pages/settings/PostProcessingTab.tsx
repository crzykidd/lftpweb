import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
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

/** Settings → Post-processing (DESIGN.md §6, phase 5): the *site-wide* default for each of
 * verify/extract/move/delete-archives. As of 2026-08-13
 * (`prompts/2026-08-13-postprocess-inherit-or-override.md`) a queue's own toggle (Settings →
 * Queues) inherits this value unless explicitly overridden for that queue -- changing a value
 * here now takes effect immediately for every queue that hasn't overridden it, rather than
 * being ANDed against a per-queue flag that could only narrow it toward "off" (see
 * docs/decisions.md for that history). Every field here defaults off; a fresh install runs no
 * post-processing at all even before anyone visits this page, and every queue starts out
 * inheriting that off default.
 *
 * `_FAILED_` staging-directory retention (`failed_retention_enabled`/`failed_retention_days`)
 * has no field on this page or on `PostprocessSettingsOut` above -- a pre-existing "backend
 * first, UI catches up later" gap (same shape as several other settings in this project),
 * distinct from every other field here. Saving from this form has always omitted those two
 * keys entirely; `api/settings.py.put_postprocess_settings` now merges any field genuinely
 * absent from the request over the previously-stored value rather than resetting it to its
 * model default, so a value set via that endpoint directly (there is no UI for it yet) survives
 * a save from this page rather than being silently discarded on every single one (found while
 * investigating `prompts/2026-08-13-per-queue-archive-cleanup.md`'s item 3).
 */
export function PostProcessingTab() {
  const [settings, setSettings] = useState<PostprocessSettingsOut>(EMPTY)
  const [loading, setLoading] = useState(true)
  // Whether the initial GET actually landed (2026-08-13,
  // `prompts/2026-08-13-per-queue-archive-cleanup.md`, item 3). `loading` alone already keeps
  // the Save button out of the DOM while the fetch is in flight -- so the literal "clicked Save
  // before the response arrived" race isn't reachable here -- but a *failed* fetch (a transient
  // 500, the backend not up yet) used to leave `settings` at `EMPTY` with `loading` false and
  // no indication anything was wrong, and Save was fully clickable: a real, reachable way for
  // this page to write defaults over whatever was actually saved, including turning off the
  // deletion toggle below. `loaded` is only ever set on a *successful* load, so Save stays
  // disabled through a failed one instead.
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [passwordsText, setPasswordsText] = useState('')

  useEffect(() => {
    getPostprocessSettings()
      .then((s) => {
        setSettings(s)
        setPasswordsText(s.extract_passwords.join('\n'))
        setLoaded(true)
      })
      .catch((err) => setLoadError(err instanceof Error ? err.message : String(err)))
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
      {/* Corrected 2026-08-13 (prompts/2026-08-13-docs-section.md): this paragraph still
        * described the site-wide AND per-queue **AND** that `3500b3f` replaced with
        * inherit-or-override, so it claimed a step needed turning on in two places when a
        * queue's toggle now follows this page's value by default. Left uncorrected it would
        * have contradicted Docs → Concepts, which documents the real behaviour. */}
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        These are the <strong>site-wide defaults</strong>. Each queue's own toggle in Settings →
        Queues inherits the value here unless it has been explicitly overridden for that queue,
        so changing one of these takes effect immediately for every queue still inheriting it.
        Everything defaults off (DESIGN.md §6) — see{' '}
        <Link className="underline" to="/docs/concepts#inherit">
          Docs → Concepts
        </Link>
        .
      </p>

      {loadError && (
        <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-900/30 dark:text-red-300">
          Couldn't load the current settings ({loadError}). Saving is disabled until this
          loads — reload the page and try again; saving from a blank form would overwrite your
          real settings with these defaults.
        </p>
      )}

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
        disabled={saving || !loaded}
        onClick={handleSave}
        className="w-fit rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
      >
        {saving ? 'Saving…' : 'Save'}
      </button>
    </div>
  )
}
