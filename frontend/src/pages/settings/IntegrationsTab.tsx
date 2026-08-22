import { useEffect, useState } from 'react'
import {
  createArrInstance,
  deleteArrInstance,
  getArrPollSettings,
  listArrInstances,
  putArrPollSettings,
  testArrInstance,
  updateArrInstance,
} from '../../api/client'
import type { ArrInstanceOut, ArrKind, ArrPollSettingsOut, ArrTestResponse } from '../../api/types'
import { FieldHelp } from '../../components/FieldHelp'

const inputClasses =
  'w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const labelClasses = 'text-sm font-medium text-zinc-700 dark:text-zinc-300'
const hintClasses = 'text-xs text-zinc-500 dark:text-zinc-400'
const buttonClasses =
  'w-fit rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300'
const secondaryButtonClasses =
  'w-fit rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900'

interface FormState {
  name: string
  kind: ArrKind
  base_url: string
  api_key: string
  enabled: boolean
  notify_on_complete: boolean
}

const EMPTY_FORM: FormState = {
  name: '',
  kind: 'sonarr',
  base_url: '',
  api_key: '',
  enabled: false,
  notify_on_complete: false,
}

// Client-side hints only -- the authoritative range check is server-side
// (`api/settings_arr.py.put_arr_poll_settings`, `core/arrsync.py.ArrSyncScheduler.
// MIN_POLL_INTERVAL_S` / `MAX_POLL_INTERVAL_S`). Mirrored here only so the input's own
// `min`/`max` attributes and the hint text agree with what the server will actually accept.
const POLL_INTERVAL_MIN_S = 5
const POLL_INTERVAL_MAX_S = 3600

const POLL_SETTINGS_EMPTY: ArrPollSettingsOut = { poll_interval_s: 10 }

/** Settings → Integrations's poll-interval section (2026-08-21, issue #16,
 * `prompts/done/2026-08-21-arr-poll-cadence.md`) -- `core/arrsync.py.ArrSettings.
 * poll_interval_s` exposed here for the first time; before this it was DB-only, a default that
 * got written down rather than ever a user choice. Same self-contained load/save shape
 * `TransferTab.tsx`'s `SettleGateSection`/`DownloadPrefixSection` already establish for a
 * site-level setting that isn't part of a bigger form: its own `GET`/`PUT` cycle, a draft value
 * distinct from the last-saved one so typing doesn't fight the server response.
 *
 * **Relabelled 2026-08-21** (`prompts/done/2026-08-21-poll-cadence-labelling.md`, finding 6 of
 * `prompts/test-findings-2026-08-21.md`): the user who asked for this setting and knew it had
 * shipped could not find it. Two causes, both wording/placement, no behaviour change --
 * `poll_interval_s`, its 10s default, 5s floor, 3600s ceiling and validation are untouched.
 * 1. "Poll cadence" named nothing -- it's internal vocabulary that doesn't mention Sonarr/Radarr,
 *    so on a page full of *arr configuration it read as unrelated plumbing. The heading now
 *    names the action and its target the way the rest of this page already does.
 * 2. The help text explained the floor and default -- mechanism, not consequence. It now says
 *    what actually gets faster (Preflight's progress, how soon an item leaves "Awaiting
 *    import") and what it costs (one more request per enabled instance per pass), reusing
 *    `FieldHelp` the way every other field on this page's siblings already do rather than
 *    growing the paragraph under the input.
 * Kept at the top of the page rather than moved: it is site-wide, sitting above the per-instance
 * list only because there's nowhere else for a setting that isn't about any one instance to go.
 * The heading and lead-in sentence below now say "every enabled instance" explicitly so it can't
 * read as belonging to the first row of the table underneath it.
 */
function PollCadenceSection() {
  // One field, fully represented by `draft` -- unlike `DownloadPrefixSection` above (which also
  // tracks a separate `enabled` toggle not covered by its own draft string), there is no second
  // piece of server state to hold onto here, so a distinct `settings` object would just mirror
  // `draft` and go unused.
  const [draft, setDraft] = useState(String(POLL_SETTINGS_EMPTY.poll_interval_s))
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    getArrPollSettings()
      .then((s) => setDraft(String(s.poll_interval_s)))
      .finally(() => setLoading(false))
  }, [])

  const handleSave = async () => {
    const parsed = Number(draft)
    setError(null)
    setSaved(false)
    if (!Number.isFinite(parsed) || parsed < POLL_INTERVAL_MIN_S || parsed > POLL_INTERVAL_MAX_S) {
      setError(
        `Enter a number between ${POLL_INTERVAL_MIN_S} and ${POLL_INTERVAL_MAX_S} seconds.`,
      )
      return
    }
    setSaving(true)
    try {
      const result = await putArrPollSettings({ poll_interval_s: parsed })
      setDraft(String(result.poll_interval_s))
      setSaved(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
      <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
        How often to check Sonarr/Radarr
      </h3>
      <p className={hintClasses}>
        One setting for <strong>every enabled instance</strong> below, not just the first one in
        the list.
      </p>
      {loading ? (
        <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>
      ) : (
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>
            Check interval (seconds)
            <FieldHelp label="Check interval (seconds)">
              <p>
                How often lftpweb asks each enabled Sonarr/Radarr instance for its download
                queue.
              </p>
              <p>
                A shorter interval makes two things happen sooner: a <strong>Preflight</strong>{' '}
                row's progress ticks once per check, so it visibly updates more often; and a
                finished item leaves <strong>"Awaiting import"</strong> sooner -- that needs{' '}
                <strong>two consecutive checks</strong> to agree before lftpweb confirms it, so
                the actual delay you see is roughly <strong>twice</strong> this number, not equal
                to it.
              </p>
              <p>
                The cost: one extra request per enabled instance, every interval -- it grows with
                how many instances you have turned on, not with how many items are downloading.
              </p>
            </FieldHelp>
          </span>
          <input
            type="number"
            min={POLL_INTERVAL_MIN_S}
            max={POLL_INTERVAL_MAX_S}
            step={1}
            className={inputClasses + ' max-w-32'}
            value={draft}
            disabled={saving}
            onChange={(e) => setDraft(e.target.value)}
          />
        </label>
      )}
      <p className={hintClasses}>
        Default {POLL_SETTINGS_EMPTY.poll_interval_s}s. Floored at {POLL_INTERVAL_MIN_S}s; capped
        at {POLL_INTERVAL_MAX_S}s ({POLL_INTERVAL_MAX_S / 60} minutes).
      </p>
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      {saved && !error && <p className="text-sm text-emerald-600 dark:text-emerald-400">Saved.</p>}
      <button
        type="button"
        disabled={saving || loading}
        onClick={handleSave}
        className={buttonClasses}
      >
        {saving ? 'Saving…' : 'Save'}
      </button>
    </div>
  )
}

/** Settings → Integrations (new, docs/arr-integration-spec.md): Sonarr/Radarr instance CRUD
 * plus a Test-connection round trip. Structured the same way `AuthTab.tsx`'s API-key section
 * is -- a list, an add/edit form below it, a write-only secret field -- since that's the
 * closest existing shape to "named external credential, CRUD, never echoed" in this codebase.
 * Never click-tested (no browser in this build environment, same caveat every Settings page in
 * this project carries -- see `docs/decisions.md`).
 *
 * Both opt-ins on the form (`enabled`, `notify_on_complete`) default off, per the spec's
 * "everything OFF by default" rule -- creating an instance here does nothing on its own until
 * a queue is also bound to it (Settings → Queues) and this instance is switched on.
 */
export function IntegrationsTab() {
  const [instances, setInstances] = useState<ArrInstanceOut[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [editingId, setEditingId] = useState<number | null>(null)

  const [testResults, setTestResults] = useState<Record<number, ArrTestResponse>>({})
  const [testingId, setTestingId] = useState<number | null>(null)

  const refresh = () => listArrInstances().then(setInstances)

  useEffect(() => {
    refresh()
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  const startEdit = (instance: ArrInstanceOut) => {
    setEditingId(instance.id)
    setError(null)
    setForm({
      name: instance.name,
      kind: instance.kind,
      base_url: instance.base_url,
      // Never pre-filled with the real key -- the browser was never given it back
      // (`ArrInstanceOut.has_api_key` is the only thing GET ever returns about it). The
      // placeholder below carries the "leave blank to keep it" instruction.
      api_key: '',
      enabled: instance.enabled,
      notify_on_complete: instance.notify_on_complete,
    })
  }

  const cancelEdit = () => {
    setEditingId(null)
    setForm(EMPTY_FORM)
  }

  const handleSubmit = async () => {
    setError(null)
    setSaving(true)
    try {
      const body = {
        name: form.name,
        kind: form.kind,
        base_url: form.base_url,
        // Blank means "unchanged" on an edit (server keeps the stored key) and is rejected as
        // required on create (`api/settings_arr.py.create_arr_instance`).
        api_key: form.api_key || null,
        enabled: form.enabled,
        notify_on_complete: form.notify_on_complete,
      }
      if (editingId != null) {
        await updateArrInstance(editingId, body)
      } else {
        await createArrInstance(body)
      }
      cancelEdit()
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (id: number) => {
    setError(null)
    try {
      await deleteArrInstance(id)
      if (editingId === id) cancelEdit()
      setTestResults((prev) => {
        const { [id]: _removed, ...rest } = prev
        return rest
      })
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const handleTest = async (id: number) => {
    setTestingId(id)
    try {
      const result = await testArrInstance(id)
      setTestResults((prev) => ({ ...prev, [id]: result }))
    } catch (err) {
      setTestResults((prev) => ({
        ...prev,
        [id]: {
          ok: false,
          error_class: 'RequestFailed',
          message: err instanceof Error ? err.message : String(err),
          version: null,
        },
      }))
    } finally {
      setTestingId(null)
    }
  }

  if (loading) return <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>

  return (
    <div className="flex max-w-2xl flex-col gap-6">
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        Sonarr/Radarr integration (docs/arr-integration-spec.md). Off by default at every level:
        creating an instance here does nothing until it is also <strong>enabled</strong> below{' '}
        <em>and</em> bound to a queue at{' '}
        <a href="/settings/queues" className="underline">
          Settings → Queues
        </a>
        . A bound, enabled instance's queue is watched for a matching download; the Files page
        shows an *arr icon once one is found.
      </p>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      <PollCadenceSection />

      <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
            <tr>
              <th className="px-3 py-2 font-medium">Name</th>
              <th className="px-3 py-2 font-medium">Kind</th>
              <th className="px-3 py-2 font-medium">Base URL</th>
              <th className="px-3 py-2 font-medium">Enabled</th>
              <th className="px-3 py-2 font-medium">Notify on complete</th>
              <th className="px-3 py-2 font-medium">Test</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {instances.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-4 text-center text-zinc-400">
                  No instances yet.
                </td>
              </tr>
            )}
            {instances.map((instance) => {
              const result = testResults[instance.id]
              return (
                <tr key={instance.id} className="border-t border-zinc-100 dark:border-zinc-900">
                  <td className="px-3 py-2">{instance.name}</td>
                  <td className="px-3 py-2">{instance.kind}</td>
                  <td className="px-3 py-2 font-mono text-xs">{instance.base_url}</td>
                  <td className="px-3 py-2">{instance.enabled ? 'yes' : 'no'}</td>
                  <td className="px-3 py-2">{instance.notify_on_complete ? 'yes' : 'no'}</td>
                  <td className="px-3 py-2">
                    <div className="flex flex-col gap-1">
                      <button
                        type="button"
                        disabled={testingId === instance.id}
                        onClick={() => handleTest(instance.id)}
                        className={secondaryButtonClasses}
                      >
                        {testingId === instance.id ? 'Testing…' : 'Test'}
                      </button>
                      {result && (
                        <span
                          className={
                            result.ok
                              ? 'text-xs text-emerald-600 dark:text-emerald-400'
                              : 'text-xs text-red-600 dark:text-red-400'
                          }
                        >
                          {result.ok
                            ? `Reachable${result.version ? ` (v${result.version})` : ''}`
                            : result.message}
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    <button
                      type="button"
                      onClick={() => startEdit(instance)}
                      className="mr-2 text-zinc-600 hover:underline dark:text-zinc-300"
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        if (
                          window.confirm(
                            `Delete "${instance.name}"? Any queue bound to it goes back to "no integration."`,
                          )
                        ) {
                          void handleDelete(instance.id)
                        }
                      }}
                      className="text-red-600 hover:underline dark:text-red-400"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          {editingId != null ? 'Edit instance' : 'Add an instance'}
        </h3>

        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Name</span>
          <input
            className={inputClasses}
            value={form.name}
            onChange={(e) => update('name', e.target.value)}
            placeholder="Sonarr, Radarr 4K, …"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Kind</span>
          <select
            className={inputClasses}
            value={form.kind}
            onChange={(e) => update('kind', e.target.value as ArrKind)}
          >
            <option value="sonarr">Sonarr</option>
            <option value="radarr">Radarr</option>
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Base URL</span>
          <input
            className={inputClasses}
            value={form.base_url}
            onChange={(e) => update('base_url', e.target.value)}
            placeholder="https://sonarr.example.com"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className={labelClasses}>API key</span>
          <input
            type="password"
            className={inputClasses}
            value={form.api_key}
            onChange={(e) => update('api_key', e.target.value)}
            placeholder={
              editingId != null && instances.find((i) => i.id === editingId)?.has_api_key
                ? '•••••••• (leave blank to keep the stored key)'
                : 'paste the API key from Settings → General in Sonarr/Radarr'
            }
            autoComplete="off"
          />
        </label>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => update('enabled', e.target.checked)}
          />
          <span className={labelClasses}>Enabled</span>
        </label>
        <p className={hintClasses}>
          Off by default. Nothing polls this instance, matches an item to it, or shows its icon
          on the Files page until this is on.
        </p>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={form.notify_on_complete}
            onChange={(e) => update('notify_on_complete', e.target.checked)}
          />
          <span className={labelClasses}>Notify on complete</span>
        </label>
        <p className={hintClasses}>
          Off by default. When on, lftpweb pushes a "your files are here, import now" command to
          this instance once a bound queue's item fully finishes post-processing. Not required
          for the icons or for import detection to work -- if the *arr already has its own
          Remote Path Mapping to the synced directory, it imports on its own schedule regardless.
        </p>

        {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

        <div className="flex gap-2">
          <button type="button" disabled={saving} onClick={handleSubmit} className={buttonClasses}>
            {saving ? 'Saving…' : editingId != null ? 'Save' : 'Add instance'}
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
