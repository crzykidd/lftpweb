import { useEffect, useState } from 'react'
import {
  createClientInstance,
  deleteClientInstance,
  getHost,
  listClientInstances,
  listClientTypes,
  listQueues,
  testClientInstance,
  updateClientInstance,
} from '../../api/client'
import type {
  ClientTypeOut,
  DownloadClientOut,
  DownloadClientTestResponse,
  HostOut,
  PathQueueOut,
} from '../../api/types'
import { capabilityRows, type CapabilityRow } from '../../lib/clientCapabilities'
import { inferCategoryMappings } from '../../lib/clientCategoryInference'
import { remoteBrowseDisabled } from '../../lib/pathBrowse'
import { PathBrowseDialog } from '../../components/PathBrowseDialog'

const inputClasses =
  'w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const labelClasses = 'text-sm font-medium text-zinc-700 dark:text-zinc-300'
const hintClasses = 'text-xs text-zinc-500 dark:text-zinc-400'
const buttonClasses =
  'w-fit rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300'
const secondaryButtonClasses =
  'w-fit rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900'

interface CategoryDraft {
  category: string
  queue_id: number | null
}

interface FormState {
  name: string
  client_type: string
  // Non-secret values only, one entry per declared `ConfigField` whose `kind !== "secret"`
  // (spec §8.1) -- rendered generically from the type's own schema, never a hand-picked field
  // list. Kept as `string | boolean` for form-control convenience; `buildConfigPayload` below
  // converts to the connector's own expected JSON types (`int` -> `Number`, `bool` -> `Boolean`)
  // at submit time.
  config: Record<string, string | boolean>
  // Secret values, tracked apart from `config` so "leave blank to keep the stored value"
  // (mirrors `ArrInstanceIn.api_key`) is this section's own property -- `startEdit` never
  // pre-fills a real value here, only ever `''`.
  secrets: Record<string, string>
  enabled: boolean
  basePaths: string[]
  categories: CategoryDraft[]
}

function emptyConfigDraft(type: ClientTypeOut | undefined): Record<string, string | boolean> {
  const draft: Record<string, string | boolean> = {}
  if (type == null) return draft
  for (const field of type.config_schema) {
    if (field.kind === 'secret') continue
    draft[field.key] = field.kind === 'bool' ? Boolean(field.default) : field.default != null ? String(field.default) : ''
  }
  return draft
}

function configDraftFromExisting(
  type: ClientTypeOut | undefined,
  existing: Record<string, unknown>,
): Record<string, string | boolean> {
  const draft = emptyConfigDraft(type)
  if (type == null) return draft
  for (const field of type.config_schema) {
    if (field.kind === 'secret') continue
    const value = existing[field.key]
    if (value === undefined) continue
    draft[field.key] = field.kind === 'bool' ? Boolean(value) : String(value)
  }
  return draft
}

function emptySecretsDraft(type: ClientTypeOut | undefined): Record<string, string> {
  const draft: Record<string, string> = {}
  if (type == null) return draft
  for (const field of type.config_schema) {
    if (field.kind === 'secret') draft[field.key] = ''
  }
  return draft
}

function emptyForm(type: ClientTypeOut | undefined): FormState {
  return {
    name: '',
    client_type: type?.client_type ?? '',
    config: emptyConfigDraft(type),
    secrets: emptySecretsDraft(type),
    enabled: false,
    basePaths: [],
    categories: [],
  }
}

/** Every non-secret schema value, converted to the connector's own expected JSON type, plus
 * every secret value **only when at least one secret field was touched** -- the all-or-nothing
 * rule `DownloadClientIn`'s own docstring documents: an edit either resends every secret field
 * it wants to keep, or none at all, so leaving every secret input blank on an edit must send no
 * secret keys at all rather than a dict of empty strings that would otherwise clear them.
 * Exported for its own pure test -- no `if client_type === …` anywhere in here, only the
 * declared schema's own `kind`.
 */
export function buildConfigPayload(type: ClientTypeOut | undefined, form: FormState): Record<string, unknown> {
  const payload: Record<string, unknown> = {}
  if (type == null) return payload
  for (const field of type.config_schema) {
    if (field.kind === 'secret') continue
    const raw = form.config[field.key]
    if (field.kind === 'bool') payload[field.key] = Boolean(raw)
    else if (field.kind === 'int') payload[field.key] = raw === '' || raw == null ? null : Number(raw)
    else payload[field.key] = raw ?? ''
  }
  const anySecretTouched = type.config_schema.some(
    (f) => f.kind === 'secret' && (form.secrets[f.key] ?? '') !== '',
  )
  if (anySecretTouched) {
    for (const field of type.config_schema) {
      if (field.kind === 'secret') payload[field.key] = form.secrets[field.key] ?? ''
    }
  }
  return payload
}

/** Settings -> Connection's own host status, reused exactly as `QueuesTab.tsx`'s own
 * `useHostForBrowse` does for its Browse button -- one extra `GET /api/settings/host` on
 * mount, not polled, so the remote Browse button's disabled-with-hint state doesn't need to
 * track a mid-session host change any more precisely than that page's own copy does.
 */
function useHostForBrowse(): HostOut | null {
  const [host, setHost] = useState<HostOut | null>(null)
  useEffect(() => {
    getHost()
      .then(setHost)
      .catch(() => setHost(null))
  }, [])
  return host
}

function CapabilityRowView({ row }: { row: CapabilityRow }) {
  const dotClasses =
    row.support === 'native'
      ? 'bg-emerald-500'
      : row.support === 'derived'
        ? 'bg-amber-500'
        : 'bg-zinc-400 dark:bg-zinc-600'
  return (
    <li className="flex flex-col gap-0.5">
      <span className="flex items-center gap-1.5">
        <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${dotClasses}`} aria-hidden="true" />
        <span className={row.support === 'none' ? 'text-zinc-400 dark:text-zinc-600' : undefined}>
          {row.label}
        </span>
        {row.derived && (
          <span className="rounded bg-amber-100 px-1 text-[10px] font-medium tracking-wide text-amber-800 uppercase dark:bg-amber-950/60 dark:text-amber-300">
            derived
          </span>
        )}
      </span>
      {/* A `derived` capability's semantics differ from `native` (spec §4.3) -- its `note` is
       * shown every time, never only on hover, so the caveat is visible wherever the value is. */}
      {row.derived && row.note && (
        <span className="pl-3.5 text-xs text-amber-700 dark:text-amber-400">{row.note}</span>
      )}
      {/* A missing capability states its reason -- never a bare greyed-out row (this task's own
       * instruction). */}
      {row.disabledReason && (
        <span className="pl-3.5 text-xs text-zinc-400 dark:text-zinc-600">
          Not available — {row.disabledReason}
        </span>
      )}
    </li>
  )
}

/** The capability readout (spec §4.1-§4.4) -- driven **entirely** by whatever `capabilities`
 * the caller passes in, never by `client_type`. `null` means "never successfully probed," and
 * is rendered as its own distinct state rather than an empty list, so it can never be confused
 * with "probed and reports nothing."
 */
function CapabilityReadout({ capabilities }: { capabilities: DownloadClientOut['capabilities'] }) {
  if (capabilities == null) {
    return (
      <p className={hintClasses}>Not yet tested — capabilities are unknown until Test succeeds once.</p>
    )
  }
  const rows = capabilityRows(capabilities)
  const operationRows = rows.filter((r) => r.group === 'operations')
  const fieldRows = rows.filter((r) => r.group === 'fields')
  return (
    <div className="grid grid-cols-1 gap-4 text-sm sm:grid-cols-2">
      <div>
        <h4 className="mb-1 text-xs font-semibold tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
          Can do
        </h4>
        <ul className="flex flex-col gap-1.5">
          {operationRows.map((row) => (
            <CapabilityRowView key={row.key} row={row} />
          ))}
        </ul>
      </div>
      <div>
        <h4 className="mb-1 text-xs font-semibold tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
          Reports
        </h4>
        <ul className="flex flex-col gap-1.5">
          {fieldRows.map((row) => (
            <CapabilityRowView key={row.key} row={row} />
          ))}
        </ul>
      </div>
    </div>
  )
}

const FAMILY_ORDER: ClientTypeOut['family'][] = ['usenet', 'torrent']
const FAMILY_LABELS: Record<ClientTypeOut['family'], string> = { usenet: 'Usenet', torrent: 'Torrent' }

/** Settings -> Clients (docs/download-client-framework-spec.md, stage 1b of #18): download-
 * client instance CRUD, a connection form rendered **generically from each connector's own
 * declared `config_schema`** (spec §8.1) -- never a hand-written form per client type -- base
 * paths (reusing the existing remote-browse dialog, spec §8.2), category -> queue mapping with
 * an inference offer (spec §8.3), and the honest capability readout (spec §4.1-§4.4). Structured
 * like `IntegrationsTab.tsx`'s instance list + add/edit form, the closest existing shape to
 * "named external credential, CRUD, testable" in this codebase.
 *
 * **No `if client_type === "…"` anywhere in this file.** `family` groups the type picker only
 * (spec §5.1); the connection form, and every capability shown, come entirely from the server's
 * own declaration.
 */
export function ClientsTab() {
  const [clientTypes, setClientTypes] = useState<ClientTypeOut[]>([])
  const [instances, setInstances] = useState<DownloadClientOut[]>([])
  const [queues, setQueues] = useState<PathQueueOut[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const [form, setForm] = useState<FormState>(emptyForm(undefined))
  const [editingId, setEditingId] = useState<number | null>(null)
  const [basePathDraft, setBasePathDraft] = useState('')
  const [browseOpen, setBrowseOpen] = useState(false)
  const [inferenceMessage, setInferenceMessage] = useState<string | null>(null)

  const [testResults, setTestResults] = useState<Record<number, DownloadClientTestResponse>>({})
  const [testingId, setTestingId] = useState<number | null>(null)

  const host = useHostForBrowse()

  const refreshInstances = () => listClientInstances().then(setInstances)

  useEffect(() => {
    Promise.all([listClientTypes(), refreshInstances(), listQueues().then(setQueues)])
      .then(([types]) => {
        setClientTypes(types)
        setForm((prev) => (prev.client_type === '' ? emptyForm(types[0]) : prev))
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const selectedType = clientTypes.find((t) => t.client_type === form.client_type)

  const groupedTypes = FAMILY_ORDER.map((family) => ({
    family,
    types: clientTypes.filter((t) => t.family === family),
  })).filter((g) => g.types.length > 0)

  const updateConfig = (key: string, value: string | boolean) => {
    setForm((prev) => ({ ...prev, config: { ...prev.config, [key]: value } }))
  }
  const updateSecret = (key: string, value: string) => {
    setForm((prev) => ({ ...prev, secrets: { ...prev.secrets, [key]: value } }))
  }

  const handleTypeChange = (client_type: string) => {
    const type = clientTypes.find((t) => t.client_type === client_type)
    setForm((prev) => ({
      ...prev,
      client_type,
      config: emptyConfigDraft(type),
      secrets: emptySecretsDraft(type),
    }))
  }

  const startEdit = (instance: DownloadClientOut) => {
    setEditingId(instance.id)
    setError(null)
    setInferenceMessage(null)
    const type = clientTypes.find((t) => t.client_type === instance.client_type)
    setForm({
      name: instance.name,
      client_type: instance.client_type,
      config: configDraftFromExisting(type, instance.config),
      secrets: emptySecretsDraft(type),
      enabled: instance.enabled,
      basePaths: instance.base_paths.map((bp) => bp.path),
      categories: instance.categories.map((c) => ({ category: c.category, queue_id: c.queue_id })),
    })
  }

  const cancelEdit = () => {
    setEditingId(null)
    setInferenceMessage(null)
    setForm(emptyForm(clientTypes[0]))
  }

  const addBasePath = (path: string) => {
    const trimmed = path.trim()
    if (trimmed === '') return
    setForm((prev) =>
      prev.basePaths.includes(trimmed) ? prev : { ...prev, basePaths: [...prev.basePaths, trimmed] },
    )
    setBasePathDraft('')
  }

  const removeBasePath = (path: string) => {
    setForm((prev) => ({ ...prev, basePaths: prev.basePaths.filter((p) => p !== path) }))
  }

  const addCategoryRow = () => {
    setForm((prev) => ({ ...prev, categories: [...prev.categories, { category: '', queue_id: null }] }))
  }

  const updateCategoryRow = (index: number, patch: Partial<CategoryDraft>) => {
    setForm((prev) => ({
      ...prev,
      categories: prev.categories.map((c, i) => (i === index ? { ...c, ...patch } : c)),
    }))
  }

  const removeCategoryRow = (index: number) => {
    setForm((prev) => ({ ...prev, categories: prev.categories.filter((_, i) => i !== index) }))
  }

  /** Spec §8.3: "the setup UI should therefore offer to infer the mapping ... with the user
   * confirming rather than typing." Proposes into the *draft* form only -- nothing is saved
   * until Save is pressed, and every proposed row is an ordinary editable/removable row like
   * any typed one, so confirming is just leaving it alone through Save.
   */
  const handleInferCategories = () => {
    const suggestions = inferCategoryMappings(
      form.basePaths,
      queues.map((q) => ({ id: q.id, remote_path: q.remote_path })),
    )
    const existingQueueIds = new Set(
      form.categories.map((c) => c.queue_id).filter((id): id is number => id != null),
    )
    const additions = suggestions.filter((s) => !existingQueueIds.has(s.queue_id))
    if (additions.length === 0) {
      setInferenceMessage(
        'No new mappings found — either every matching queue is already mapped, or no configured base path contains one of your queues.',
      )
      return
    }
    setForm((prev) => ({
      ...prev,
      categories: [
        ...prev.categories,
        ...additions.map((a) => ({ category: a.category, queue_id: a.queue_id })),
      ],
    }))
    setInferenceMessage(
      `Proposed ${additions.length} mapping${additions.length === 1 ? '' : 's'} below from your ` +
        `configured base paths and existing queues — review, then Save to confirm.`,
    )
  }

  const handleSubmit = async () => {
    setError(null)
    setSaving(true)
    try {
      const body = {
        name: form.name,
        client_type: form.client_type,
        config: buildConfigPayload(selectedType, form),
        enabled: form.enabled,
        base_paths: form.basePaths.map((path) => ({ path })),
        categories: form.categories
          .filter((c) => c.category.trim() !== '')
          .map((c) => ({ category: c.category.trim(), queue_id: c.queue_id })),
      }
      if (editingId != null) {
        await updateClientInstance(editingId, body)
      } else {
        await createClientInstance(body)
      }
      cancelEdit()
      await refreshInstances()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (id: number) => {
    setError(null)
    try {
      await deleteClientInstance(id)
      if (editingId === id) cancelEdit()
      setTestResults((prev) => {
        const { [id]: _removed, ...rest } = prev
        return rest
      })
      await refreshInstances()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const handleTest = async (id: number) => {
    setTestingId(id)
    try {
      const result = await testClientInstance(id)
      setTestResults((prev) => ({ ...prev, [id]: result }))
      // A fresh success resets capabilities server-side; a `CapabilityUnavailable` persists a
      // narrowed set too (spec §4.1) -- refresh so the list's own `instance.capabilities`
      // (what every render of this row reads before Test is next clicked) stays in step,
      // rather than only living in `testResults` until some unrelated refresh happens.
      await refreshInstances()
    } catch (err) {
      const instance = instances.find((i) => i.id === id)
      setTestResults((prev) => ({
        ...prev,
        [id]: {
          ok: false,
          error_class: 'RequestFailed',
          message: err instanceof Error ? err.message : String(err),
          version: instance?.version ?? null,
          // Never blank a previously known capability set just because *this* request to our
          // own API failed (spec §4.2's rule applied one layer further out than the backend's
          // own guarantee) -- render whatever was last known.
          capabilities: instance?.capabilities ?? null,
        },
      }))
    } finally {
      setTestingId(null)
    }
  }

  if (loading) return <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        Download-client instances — SABnzbd, rTorrent, and whatever follows
        (docs/download-client-framework-spec.md). Each type declares its own connection form and
        its own capabilities; this page never hard-codes either. A category mapped to a queue
        lets lftpweb attribute that queue's completed items to this client once the poller
        (a later stage) is built.
      </p>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 text-left text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
            <tr>
              <th className="px-3 py-2 font-medium">Name</th>
              <th className="px-3 py-2 font-medium">Type</th>
              <th className="px-3 py-2 font-medium">Enabled</th>
              <th className="px-3 py-2 font-medium">Base paths</th>
              <th className="px-3 py-2 font-medium">Categories</th>
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
              const capabilities = result?.capabilities ?? instance.capabilities
              return (
                <tr key={instance.id} className="border-t border-zinc-100 align-top dark:border-zinc-900">
                  <td className="px-3 py-2">{instance.name}</td>
                  <td className="px-3 py-2 font-mono text-xs">{instance.client_type}</td>
                  <td className="px-3 py-2">{instance.enabled ? 'yes' : 'no'}</td>
                  <td className="px-3 py-2 text-xs">{instance.base_paths.length}</td>
                  <td className="px-3 py-2 text-xs">{instance.categories.length}</td>
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
                      <details className="mt-1">
                        <summary className="cursor-pointer text-xs text-zinc-500 hover:underline dark:text-zinc-400">
                          Capabilities
                        </summary>
                        <div className="mt-2 max-w-md">
                          <CapabilityReadout capabilities={capabilities} />
                        </div>
                      </details>
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
                            `Delete "${instance.name}"? Any category -> queue mapping goes with it.`,
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

      <div className="flex flex-col gap-4 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          {editingId != null ? 'Edit instance' : 'Add an instance'}
        </h3>

        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Name</span>
          <input
            className={inputClasses}
            value={form.name}
            onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
            placeholder="SABnzbd, rTorrent, …"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Type</span>
          <select
            className={inputClasses}
            value={form.client_type}
            onChange={(e) => handleTypeChange(e.target.value)}
            disabled={editingId != null}
          >
            {clientTypes.length === 0 && <option value="">No connector types registered</option>}
            {groupedTypes.map((group) => (
              <optgroup key={group.family} label={FAMILY_LABELS[group.family]}>
                {group.types.map((t) => (
                  <option key={t.client_type} value={t.client_type}>
                    {t.client_type}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          {editingId != null && (
            <p className={hintClasses}>Type can't be changed after creation — add a new instance instead.</p>
          )}
        </label>

        {/* The connection form is rendered entirely from `selectedType.config_schema` (spec
         * §8.1) -- adding a connector never requires touching this component. */}
        {selectedType?.config_schema.map((field) => {
          if (field.kind === 'secret') {
            return (
              <label key={field.key} className="flex flex-col gap-1">
                <span className={labelClasses}>
                  {field.label}
                  {field.required ? ' *' : ''}
                </span>
                <input
                  type="password"
                  className={inputClasses}
                  value={form.secrets[field.key] ?? ''}
                  onChange={(e) => updateSecret(field.key, e.target.value)}
                  placeholder={
                    editingId != null && instances.find((i) => i.id === editingId)?.has_secret
                      ? '•••••••• (leave every secret field blank to keep the stored values)'
                      : ''
                  }
                  autoComplete="off"
                />
                {field.help_text && <p className={hintClasses}>{field.help_text}</p>}
              </label>
            )
          }
          if (field.kind === 'bool') {
            return (
              <div key={field.key} className="flex flex-col gap-1">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={Boolean(form.config[field.key])}
                    onChange={(e) => updateConfig(field.key, e.target.checked)}
                  />
                  <span className={labelClasses}>{field.label}</span>
                </label>
                {field.help_text && <p className={hintClasses}>{field.help_text}</p>}
              </div>
            )
          }
          return (
            <label key={field.key} className="flex flex-col gap-1">
              <span className={labelClasses}>
                {field.label}
                {field.required ? ' *' : ''}
              </span>
              <input
                type={field.kind === 'int' ? 'number' : 'text'}
                className={inputClasses}
                value={String(form.config[field.key] ?? '')}
                onChange={(e) => updateConfig(field.key, e.target.value)}
              />
              {field.help_text && <p className={hintClasses}>{field.help_text}</p>}
            </label>
          )
        })}

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm((prev) => ({ ...prev, enabled: e.target.checked }))}
          />
          <span className={labelClasses}>Enabled</span>
        </label>
        <p className={hintClasses}>
          Off by default, same as every *arr instance. Enabling this doesn't poll anything yet —
          the poller is a later stage of this feature (spec §14 stage 2).
        </p>

        {/* Base paths (spec §8.2) -- browsed and validated on save, reusing the same remote-
         * browse dialog Settings -> Queues uses for `remote_path`. */}
        <div className="flex flex-col gap-2">
          <span className={labelClasses}>Base paths</span>
          <p className={hintClasses}>
            The real roots this client's data lives under on the seedbox — the boundary a future
            delete is authorised to act within. A wrong path here is a wrong safety boundary, so
            it's validated for real when you Save, not accepted silently.
          </p>
          <ul className="flex flex-col gap-1">
            {form.basePaths.map((path) => (
              <li key={path} className="flex items-center gap-2">
                <span className="flex-1 truncate rounded-md border border-zinc-200 bg-zinc-50 px-2 py-1 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-900">
                  {path}
                </span>
                <button
                  type="button"
                  onClick={() => removeBasePath(path)}
                  className="text-xs text-red-600 hover:underline dark:text-red-400"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
          <div className="flex gap-2">
            <input
              className={inputClasses}
              value={basePathDraft}
              onChange={(e) => setBasePathDraft(e.target.value)}
              placeholder="/home/user/downloads/complete"
            />
            <button
              type="button"
              onClick={() => addBasePath(basePathDraft)}
              className={secondaryButtonClasses}
            >
              Add
            </button>
            <span className="flex flex-col gap-1">
              <button
                type="button"
                disabled={remoteBrowseDisabled(host)}
                onClick={() => setBrowseOpen(true)}
                className={secondaryButtonClasses}
              >
                Browse…
              </button>
              {remoteBrowseDisabled(host) && (
                <span className="text-xs text-amber-600 dark:text-amber-400">
                  Configure a host in Settings → Connection first.
                </span>
              )}
            </span>
          </div>
        </div>

        {/* Category -> queue mapping (spec §8.3) -- a site-level instance's completed-folder
         * categories, each optionally bound to one of this app's own queues. */}
        <div className="flex flex-col gap-2">
          <span className={labelClasses}>Category → queue mapping</span>
          <p className={hintClasses}>
            On the reference workflow, a queue's remote path <em>is</em> this client's category
            folder (spec §8.3) — Infer proposes that mapping from your configured base paths and
            existing queues; nothing is saved until you press Save below.
          </p>
          <button type="button" onClick={handleInferCategories} className={secondaryButtonClasses}>
            Infer mappings from base paths + queues
          </button>
          {inferenceMessage && <p className={hintClasses}>{inferenceMessage}</p>}
          <ul className="flex flex-col gap-2">
            {form.categories.map((cat, i) => (
              <li key={i} className="flex items-center gap-2">
                <input
                  className={inputClasses}
                  value={cat.category}
                  onChange={(e) => updateCategoryRow(i, { category: e.target.value })}
                  placeholder="ar-tv"
                />
                <select
                  className={inputClasses}
                  value={cat.queue_id ?? ''}
                  onChange={(e) =>
                    updateCategoryRow(i, { queue_id: e.target.value === '' ? null : Number(e.target.value) })
                  }
                >
                  <option value="">— not bound —</option>
                  {queues.map((q) => (
                    <option key={q.id} value={q.id}>
                      {q.name} ({q.remote_path})
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => removeCategoryRow(i)}
                  className="text-xs text-red-600 hover:underline dark:text-red-400"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
          <button type="button" onClick={addCategoryRow} className={secondaryButtonClasses}>
            + Add mapping
          </button>
        </div>

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

      {browseOpen && (
        <PathBrowseDialog
          side="remote"
          initialPath={basePathDraft || '/'}
          onSelect={(path) => {
            addBasePath(path)
            setBrowseOpen(false)
          }}
          onClose={() => setBrowseOpen(false)}
        />
      )}
    </div>
  )
}
