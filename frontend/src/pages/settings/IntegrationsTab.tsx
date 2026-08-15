import { useEffect, useState } from 'react'
import {
  createArrInstance,
  deleteArrInstance,
  listArrInstances,
  testArrInstance,
  updateArrInstance,
} from '../../api/client'
import type { ArrInstanceOut, ArrKind, ArrTestResponse } from '../../api/types'

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
