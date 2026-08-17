import type { ReactNode } from 'react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  createPattern,
  createQueue,
  deleteQueue,
  deletePattern,
  getAutoQueueSettings,
  getAutoQueueStatus,
  getDownloadPrefixSettings,
  getHost,
  getPostprocessSettings,
  listArrInstances,
  listPatterns,
  listQueues,
  previewPatterns,
  putAutoQueueSettings,
  updateQueue,
} from '../../api/client'
import type {
  ArrInstanceOut,
  AutoQueueSettingsOut,
  DownloadPrefixSettingsOut,
  HostOut,
  PathQueueOut,
  PatternKind,
  PatternOut,
  PatternPreviewResponse,
  PostprocessSettingsOut,
  QueueAutoQueueStatus,
  SyncMode,
} from '../../api/types'
import { ArrBrandMark } from '../../components/LifecycleIcons'
import { FieldHelp } from '../../components/FieldHelp'
import { PathBrowseDialog } from '../../components/PathBrowseDialog'
import { remoteBrowseDisabled } from '../../lib/pathBrowse'

const inputClasses =
  'w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const labelClasses = 'text-sm font-medium text-zinc-700 dark:text-zinc-300'
const hintClasses = 'text-xs text-zinc-500 dark:text-zinc-400'

// --- Sonarr/Radarr integration form logic (docs/arr-integration-spec.md "UI") --------------
//
// Two small pure predicates, exported so `QueuesTab.test.ts` can pin them without a component-
// render harness -- this project's frontend suite has none for Settings tabs (README.md's
// "Known gaps"), and `TransfersPage.tsx`'s own exported `chipStateFor`/`isDismissable` are the
// existing precedent for factoring exactly this much logic out of a page component for testing.

/** "Delete when imported" (`arr_delete_completed`) is disabled, with a hint, unless an *arr
 * instance is selected -- mirrors the backend's own rule
 * (`api/settings_queues.py._validate_arr_binding`: `arr_delete_completed` can never be `true`
 * with no `arr_instance_id` bound) so the checkbox never lies about what a save would accept.
 */
export function arrDeleteCompletedDisabled(arrInstanceId: number | null): boolean {
  return arrInstanceId == null
}

/** Applied whenever the *arr instance dropdown changes: clearing it force-unchecks "Delete when
 * imported" too, rather than leaving a checked-but-disabled box that would 400 on save against
 * the same backend rule `arrDeleteCompletedDisabled` mirrors above. Selecting an instance (or
 * changing which one) never touches the checkbox's current value either way.
 */
export function nextArrDeleteCompleted(arrInstanceId: number | null, current: boolean): boolean {
  return arrInstanceId == null ? false : current
}

interface FormState {
  name: string
  remote_path: string
  local_path: string
  staging_path: string
  enabled: boolean
  sync_mode: SyncMode
  auto_queue_enabled: boolean
  auto_queue_patterns_only: boolean
  // `null` = inherit the site-wide Settings -> Post-processing flag (the default -- see
  // `InheritableToggle` below); `true`/`false` = an explicit per-queue override.
  auto_verify: boolean | null
  auto_extract: boolean | null
  auto_move: boolean | null
  auto_delete_archives: boolean | null
  scan_interval_s: number | null
  // "Folder prefix during transfer" (core/download_prefix.py), migration 017 -- same
  // inherit-or-override shape as the four post-processing toggles above, resolved
  // independently of each other (a queue can override just the toggle, just the prefix
  // string, both, or neither).
  download_prefix_enabled: boolean | null
  download_prefix: string | null
  // Sonarr/Radarr integration (migration 018, docs/arr-integration-spec.md). `null` = no
  // integration for this queue -- the default, and every existing queue's value after the
  // migration. `arr_visible_path` uses `''` (not `null`) as its own form-state empty value,
  // the same convention `staging_path` above already uses -- converted to `null` at submit time.
  arr_instance_id: number | null
  arr_delete_completed: boolean
  arr_visible_path: string
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
  auto_verify: null,
  auto_extract: null,
  auto_move: null,
  auto_delete_archives: null,
  scan_interval_s: null,
  download_prefix_enabled: null,
  download_prefix: null,
  arr_instance_id: null,
  arr_delete_completed: false,
  arr_visible_path: '',
}

/** Settings → Transfer's site-wide "folder prefix during transfer" default, fetched here so
 * this page's own per-queue override can show what it actually resolves to -- the identical
 * shape `usePostprocessSiteSettings` below uses for the four post-processing toggles.
 */
function useDownloadPrefixSiteSettings(): DownloadPrefixSettingsOut | null {
  const [settings, setSettings] = useState<DownloadPrefixSettingsOut | null>(null)
  useEffect(() => {
    getDownloadPrefixSettings()
      .then(setSettings)
      .catch(() => setSettings(null))
  }, [])
  return settings
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

/** Settings → Integrations' own instance list, fetched here for the "*arr instance" dropdown
 * below (docs/arr-integration-spec.md "UI") -- the same "fetch once, feed a per-queue form
 * field" shape `useDownloadPrefixSiteSettings`/`usePostprocessSiteSettings` above already use,
 * except this one's an array (there can be many instances) rather than a single settings
 * object. Empty array on failure, not `null` -- the dropdown degrades to "None" only, which is
 * always a safe, honest option regardless of why the fetch failed.
 *
 * `loaded` (2026-08-17, prompts/2026-08-17-queues-list-arr-brand-icon.md) distinguishes "the
 * fetch simply hasn't resolved yet" from "it resolved (successfully or not) to a list that
 * doesn't contain this queue's bound instance" -- the queue list's brand-icon binding indicator
 * (`queueArrBindingMark` below) renders nothing for the former (still-in-flight is a fine,
 * ordinary transient) but must fall back to a named-id text chip for the latter, never silent
 * nothing for a bound queue once the fetch has actually settled either way.
 */
function useArrInstances(): { instances: ArrInstanceOut[]; loaded: boolean } {
  const [instances, setInstances] = useState<ArrInstanceOut[]>([])
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    listArrInstances()
      .then(setInstances)
      .catch(() => setInstances([]))
      .finally(() => setLoaded(true))
  }, [])
  return { instances, loaded }
}

/** What the queue list's Name cell should render beside a bound queue's name -- pure so it's
 * unit-testable without a render harness, the same shape `arrDeleteCompletedDisabled`/
 * `nextArrDeleteCompleted` above already use. `null` covers two genuinely different "render
 * nothing" cases: no `arr_instance_id` bound at all (never had one), and the instances fetch
 * still in flight (`instancesLoaded` false) -- but *not* a bound id the loaded list doesn't
 * contain (a deleted instance, or a fetch that failed and settled to `[]`), which returns the
 * `kind: null` branch below instead so `ArrBrandMark`'s own text-chip fallback names the id
 * rather than a bound queue silently showing nothing forever, per this task's own rule.
 */
export function queueArrBindingMark(
  arrInstanceId: number | null,
  instances: ArrInstanceOut[],
  instancesLoaded: boolean,
): { kind: string | null; title: string; muted: boolean } | null {
  if (arrInstanceId == null) return null
  if (!instancesLoaded) return null
  const instance = instances.find((i) => i.id === arrInstanceId) ?? null
  if (instance == null) {
    return {
      kind: null,
      title: `Bound to *arr instance #${arrInstanceId} (not found in Settings → Integrations)`,
      muted: false,
    }
  }
  const kindLabel = instance.kind === 'sonarr' ? 'Sonarr' : 'Radarr'
  const title = instance.enabled
    ? `Bound to ${kindLabel} instance '${instance.name}'`
    : `Bound to ${kindLabel} instance '${instance.name}' (instance disabled)`
  return { kind: instance.kind, title, muted: !instance.enabled }
}

/** Settings -> Connection's own host status (GitHub issue #4, Browse dialog) -- reused here
 * rather than a new poll, the same data source `CredentialsBanner.tsx`/`ConnectionTab.tsx`
 * already read (`getHost()`), just fetched once for this page instead of polled: the remote
 * Browse button's disabled-with-hint state doesn't need to track a mid-session host change any
 * more precisely than any other field on this page already does. `null` on failure or while
 * loading -- `lib/pathBrowse.ts.remoteBrowseDisabled` treats that the same as "no host."
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

/** One `Browse…` button beside a path field, opening `PathBrowseDialog` for it. Local-side is
 * always enabled; remote-side is disabled-with-hint per `remoteBrowseDisabled` when no host is
 * configured or its credentials need re-entry -- mirrors `arrDeleteCompletedDisabled`'s pure-
 * predicate pattern above so the rule is unit-testable without a render harness.
 */
function BrowseButton({
  side,
  host,
  onClick,
}: {
  side: 'local' | 'remote'
  host: HostOut | null
  onClick: () => void
}) {
  const disabled = side === 'remote' && remoteBrowseDisabled(host)
  return (
    <span className="flex flex-col gap-1">
      <button
        type="button"
        disabled={disabled}
        onClick={onClick}
        className="w-fit rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
      >
        Browse…
      </button>
      {disabled && (
        <span className="text-xs text-amber-600 dark:text-amber-400">
          Configure a host in Settings → Connection first.
        </span>
      )}
    </span>
  )
}

/** One per-queue post-processing toggle, inherit-or-override (2026-08-13,
 * `prompts/2026-08-13-postprocess-inherit-or-override.md`). `value === null` means this queue
 * inherits the site-wide Settings → Post-processing flag -- the checkbox shows that resolved
 * value but is locked, and an "Override for this queue" button unlocks it, seeded at the
 * currently-resolved value so clicking it alone never changes what actually runs. Once
 * overridden, a "Revert to inherit" button flips back to `null` -- and says up front what that
 * will resolve to, since reverting to an invisible value is the same discoverability problem
 * in reverse (this component replaces `PostprocessStepReadout`'s old "System setting: off —
 * this toggle has no effect" readout, which described the AND this task removed).
 *
 * `forcedOn` is `move`-mode verification's own case -- DESIGN.md §6/§7.3: it runs regardless
 * of either level, so the checkbox is shown checked and locked with its own explanation,
 * never folded into ordinary inherit/override.
 */
function InheritableToggle({
  label,
  value,
  onChange,
  // `null` covers two cases the same way: the site-wide fetch hasn't resolved yet, or it
  // failed -- both read as "resolves to off" for display purposes until it's known, rather
  // than guessing a value that could be wrong in either direction.
  siteValue,
  forcedOn = false,
  forcedOnMessage,
  disabled = false,
  disabledMessage,
}: {
  label: ReactNode
  value: boolean | null
  onChange: (next: boolean | null) => void
  siteValue: boolean | null
  forcedOn?: boolean
  forcedOnMessage?: ReactNode
  disabled?: boolean
  disabledMessage?: ReactNode
}) {
  if (forcedOn) {
    return (
      <div className="flex flex-col gap-1">
        <label className="flex items-center gap-2">
          <input type="checkbox" checked readOnly disabled />
          <span className="text-sm text-zinc-700 dark:text-zinc-300">{label}</span>
        </label>
        <p className={hintClasses}>{forcedOnMessage}</p>
      </div>
    )
  }

  const siteLabel = siteValue == null ? 'loading…' : siteValue ? 'on' : 'off'
  const overridden = value !== null
  const effective = overridden ? value : !!siteValue

  return (
    <div className="flex flex-col gap-1">
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={effective}
          disabled={disabled || !overridden}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="text-sm text-zinc-700 dark:text-zinc-300">{label}</span>
      </label>
      {!overridden && (
        <p className={hintClasses}>
          Inherits{' '}
          <Link to="/settings/post-processing" className="underline">
            Settings → Post-processing
          </Link>
          's site-wide value (currently <strong>{siteLabel}</strong>).{' '}
          <button
            type="button"
            disabled={disabled}
            onClick={() => onChange(!!siteValue)}
            className="text-zinc-600 underline hover:no-underline disabled:opacity-50 dark:text-zinc-300"
          >
            Override for this queue
          </button>
        </p>
      )}
      {overridden && (
        <p className={hintClasses}>
          Overridden for this queue.{' '}
          <button
            type="button"
            onClick={() => onChange(null)}
            className="text-zinc-600 underline hover:no-underline dark:text-zinc-300"
          >
            Revert to inherit (currently resolves to {siteLabel})
          </button>
        </p>
      )}
      {disabled && disabledMessage && (
        <p className="text-xs text-amber-600 dark:text-amber-400">{disabledMessage}</p>
      )}
    </div>
  )
}

/** `InheritableToggle`'s text-field counterpart -- the "folder prefix during transfer" string
 * itself (`download_prefix`), inherit-or-override just like the toggle above but resolving to
 * a string rather than a boolean. `value === null` means this queue inherits the site-wide
 * prefix; the input shows that resolved value but is locked until "Override for this queue"
 * seeds it with the currently-resolved text, exactly mirroring `InheritableToggle`'s own
 * discoverability reasoning (a checkbox/field that silently starts editing from empty, rather
 * than from what's actually in effect, is the same surprise in both shapes).
 */
function InheritableTextField({
  label,
  value,
  onChange,
  siteValue,
  disabled = false,
  disabledMessage,
}: {
  label: ReactNode
  value: string | null
  onChange: (next: string | null) => void
  siteValue: string | null
  disabled?: boolean
  disabledMessage?: ReactNode
}) {
  const siteLabel = siteValue == null ? 'loading…' : `"${siteValue}"`
  const overridden = value !== null
  const effective = overridden ? value : (siteValue ?? '')

  return (
    <div className="flex flex-col gap-1">
      <label className="flex flex-col gap-1">
        <span className="text-sm text-zinc-700 dark:text-zinc-300">{label}</span>
        <input
          type="text"
          className={`${inputClasses} max-w-64`}
          value={effective}
          disabled={disabled || !overridden}
          onChange={(e) => onChange(e.target.value)}
        />
      </label>
      {!overridden && (
        <p className={hintClasses}>
          Inherits Settings → Transfer's site-wide prefix (currently <strong>{siteLabel}</strong>
          ).{' '}
          <button
            type="button"
            disabled={disabled}
            onClick={() => onChange(siteValue ?? '')}
            className="text-zinc-600 underline hover:no-underline disabled:opacity-50 dark:text-zinc-300"
          >
            Override for this queue
          </button>
        </p>
      )}
      {overridden && (
        <p className={hintClasses}>
          Overridden for this queue.{' '}
          <button
            type="button"
            onClick={() => onChange(null)}
            className="text-zinc-600 underline hover:no-underline dark:text-zinc-300"
          >
            Revert to inherit (currently resolves to {siteLabel})
          </button>
        </p>
      )}
      {disabled && disabledMessage && (
        <p className="text-xs text-amber-600 dark:text-amber-400">{disabledMessage}</p>
      )}
    </div>
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
  const downloadPrefixSite = useDownloadPrefixSiteSettings()
  const { instances: arrInstances, loaded: arrInstancesLoaded } = useArrInstances()
  const hostForBrowse = useHostForBrowse()
  // Which field's Browse dialog is open, if any (GitHub issue #4,
  // prompts/done/2026-08-16-path-browse-dialog.md) -- `remote_path`/`local_path` are always
  // in scope; `staging_path` too, once it has a value to browse from ("Final destination" is
  // optional -- see the field itself below). `arr_visible_path` and Settings -> Connection's
  // `key_path` are deliberately never wired to this: `arr_visible_path` describes the path as
  // the *arr's own host sees it, which neither this container nor the seedbox can list, and
  // `key_path` is a file, not a directory.
  const [browseField, setBrowseField] = useState<
    'remote_path' | 'local_path' | 'staging_path' | null
  >(null)

  const refresh = () => listQueues().then(setQueues)

  useEffect(() => {
    refresh().finally(() => setLoading(false))
  }, [])

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }))
    if (key === 'sync_mode' && value !== 'move') setMoveConfirmed(false)
    if (key === 'arr_instance_id') {
      const nextInstanceId = value as number | null
      setForm((prev) => ({
        ...prev,
        arr_delete_completed: nextArrDeleteCompleted(nextInstanceId, prev.arr_delete_completed),
      }))
    }
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
      download_prefix_enabled: queue.download_prefix_enabled,
      download_prefix: queue.download_prefix,
      arr_instance_id: queue.arr_instance_id,
      arr_delete_completed: queue.arr_delete_completed,
      arr_visible_path: queue.arr_visible_path ?? '',
    })
  }

  const cancelEdit = () => {
    setEditingId(null)
    setMoveConfirmed(false)
    setForm(EMPTY_FORM)
  }

  // DESIGN.md §6: "auto_verify is forced on and cannot be turned off in the UI" for a move
  // queue -- it is the sole gate on an irreversible remote delete (§7.3). Forced to an
  // explicit `true`, never left on inherit (`null`) -- inheriting would let a later change to
  // the site-wide verify flag silently turn it back off for this queue. The backend also
  // forces this server-side (api/settings.py._effective_auto_verify) so a direct API call
  // can't bypass it; this mirrors that in the form so the checkbox never lies about what
  // will actually be saved.
  const submitAutoVerify = form.sync_mode === 'move' ? true : form.auto_verify

  // What "Extract" actually resolves to right now (inherit or override) -- used only to gate
  // "Delete archive volumes," which is meaningless while nothing extracts. Checking the raw
  // `form.auto_extract` value instead would wrongly disable this whenever Extract is on
  // inherit, even on a site where the site-wide Extract flag is on.
  const effectiveAutoExtract = form.auto_extract ?? !!(postprocessSite && postprocessSite.extract_enabled)

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
      auto_verify: submitAutoVerify,
      auto_extract: form.auto_extract,
      auto_move: form.auto_move,
      auto_delete_archives: form.auto_delete_archives,
      scan_interval_s: form.scan_interval_s,
      download_prefix_enabled: form.download_prefix_enabled,
      download_prefix: form.download_prefix,
      arr_instance_id: form.arr_instance_id,
      arr_delete_completed: form.arr_delete_completed,
      arr_visible_path: form.arr_visible_path || null,
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
            {queues.map((q) => {
              const arrMark = queueArrBindingMark(q.arr_instance_id, arrInstances, arrInstancesLoaded)
              return (
                <tr key={q.id} className="border-t border-zinc-100 dark:border-zinc-900">
                  <td className="px-3 py-2">
                    <span className="flex items-center gap-1.5">
                      {q.name}
                      {arrMark && <ArrBrandMark kind={arrMark.kind} title={arrMark.title} muted={arrMark.muted} />}
                    </span>
                  </td>
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
              )
            })}
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
          <div className="flex items-start gap-2">
            <input
              className={inputClasses}
              value={form.remote_path}
              onChange={(e) => update('remote_path', e.target.value)}
            />
            <BrowseButton
              side="remote"
              host={hostForBrowse}
              onClick={() => setBrowseField('remote_path')}
            />
          </div>
        </label>
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Local path</span>
          <div className="flex items-start gap-2">
            <input
              className={inputClasses}
              value={form.local_path}
              onChange={(e) => update('local_path', e.target.value)}
            />
            <BrowseButton
              side="local"
              host={hostForBrowse}
              onClick={() => setBrowseField('local_path')}
            />
          </div>
        </label>
        {/* Moved here from the *arr section (user request, 2026-08-17) so the namespace pair
         * reads as one thought: "this is where *we* see the files; this is where the *arr*
         * sees the same files." The 2026-08-17 production incident is why this field earns
         * front-row placement: it was unset, every notify pushed a path the *arr couldn't
         * see, and imports silently fell back to the *arr's own scan schedule.
         * No Browse button here (GitHub issue #4, prompts/done/2026-08-16-path-browse-dialog.md)
         * -- deliberately: this describes the path as the *arr's own host sees it, which
         * neither this container nor the seedbox can list, so a browser here would be
         * actively misleading. */}
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>
            Path as seen by the *arr (optional)
            <FieldHelp label="Path as seen by the *arr">
              <p>
                Only used when this queue is bound to a Sonarr/Radarr instance (below). lftpweb
                and the *arr often mount the same storage at <em>different</em> paths — lftpweb
                might see <code>/downloads/tv</code> where Sonarr sees{' '}
                <code>/mnt/media/working/tv</code>. When lftpweb tells the *arr "your files are
                here, import now", it must speak the <em>*arr&apos;s</em> path, not its own.
              </p>
              <p>
                Set this to the directory the *arr would use for this queue&apos;s Local path —
                the easiest way to find it is the download&apos;s own path shown in the
                *arr&apos;s Queue/History. Leave blank only when both containers genuinely mount
                the storage at the identical path.
              </p>
              <p>
                If it&apos;s wrong or missing, the *arr accepts every scan request and silently
                finds nothing — imports still happen, but only on the *arr&apos;s own schedule,
                and lftpweb&apos;s source-delete/cleanup steps lag or strand. lftpweb flags the
                mismatch as a warning in History → Events when it can detect it.
              </p>
            </FieldHelp>
          </span>
          <input
            className={`${inputClasses} max-w-64`}
            value={form.arr_visible_path}
            onChange={(e) => update('arr_visible_path', e.target.value)}
            placeholder="same as Local path above -- leave blank"
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
          <div className="flex items-start gap-2">
            <input
              className={inputClasses}
              value={form.staging_path}
              onChange={(e) => update('staging_path', e.target.value)}
            />
            <BrowseButton
              side="local"
              host={hostForBrowse}
              onClick={() => setBrowseField('staging_path')}
            />
          </div>
        </label>
        {browseField && (
          <PathBrowseDialog
            side={browseField === 'remote_path' ? 'remote' : 'local'}
            initialPath={form[browseField]}
            onSelect={(path) => {
              update(browseField, path)
              setBrowseField(null)
            }}
            onClose={() => setBrowseField(null)}
          />
        )}
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>
            Sync mode
            {/* The first `FieldHelp` in the app (2026-08-13, prompts/2026-08-13-docs-section.md).
             * `sync_mode` was the obvious field to establish the pattern on: it is the one
             * control on this page that can delete data you cannot get back, and the inline
             * warning below only appears *after* you have already picked a non-copy mode. A
             * companion task applies `FieldHelp` across the rest of the settings surface. */}
            <FieldHelp label="Sync mode">
              <p>
                <strong>copy</strong> downloads and never touches the seedbox. <strong>move</strong>{' '}
                downloads, verifies, and then <strong>deletes the remote copy</strong> — once per
                item, at the end of post-processing. That delete is irreversible.
              </p>
              <p>
                A <strong>move</strong> queue's remote path must be a hardlink pickup directory
                your torrent client populates on completion — never its live seeding data
                directory. It also forces verification on regardless of any other toggle, because
                verification is the only gate on that delete.
              </p>
              <p>
                <strong>sync</strong> (propagating local deletes back to the seedbox) is designed
                but not built, and is rejected if set.
              </p>
              <p>
                <Link to="/docs/concepts" className="underline">
                  copy vs move, in the docs →
                </Link>
              </p>
            </FieldHelp>
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
          <span className={labelClasses}>
            Auto-queue (DESIGN.md §4.7) — off by default
            <FieldHelp label="Auto-queue">
              <p>
                Runs at the end of every scan of this queue, picking up any new remote item the
                patterns below select and queuing it for download — no click required.
              </p>
              <p>
                Two things make it hold off even when a pattern matches:{' '}
                <strong>suppression</strong> (an item that was stopped, permanently failed, or
                that you deleted locally is never picked up again on its own) and, if the{' '}
                <Link to="/settings/transfer" className="underline">
                  settle gate
                </Link>{' '}
                is on, an item that's still visibly arriving. A manual <strong>Queue</strong>{' '}
                click on the Files page bypasses both.
              </p>
              <p>
                <Link to="/docs/concepts#suppression" className="underline">
                  Suppression and the settle gate, in the docs →
                </Link>
              </p>
            </FieldHelp>
          </span>
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
              <FieldHelp label="Patterns-only">
                <p>
                  This changes what "no <code>select</code> pattern" means for this queue.
                </p>
                <p>
                  <strong>Off</strong> (the default): with no <code>select</code> pattern,
                  auto-queue matches <strong>everything</strong> in the remote path that isn't
                  skipped or excluded. Turning auto-queue on with an empty pattern list starts
                  downloading the whole tree.
                </p>
                <p>
                  <strong>On</strong>: with no <code>select</code> pattern, auto-queue matches{' '}
                  <strong>nothing</strong>. Use this when you want auto-queue armed but idle until
                  you add the pattern you actually want.
                </p>
              </FieldHelp>
            </span>
          </label>
        </div>

        <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-3 dark:border-zinc-800">
          <span className={labelClasses}>
            Post-processing (DESIGN.md §6) — each step inherits its site-wide default from{' '}
            <Link to="/settings/post-processing" className="underline">
              Settings → Post-processing
            </Link>{' '}
            unless explicitly overridden below.
          </span>
          <InheritableToggle
            label="Verify (.sfv/.md5, or hash-on-disk if enabled site-wide)"
            value={form.auto_verify}
            onChange={(v) => update('auto_verify', v)}
            siteValue={postprocessSite ? postprocessSite.verify_enabled : null}
            forcedOn={form.sync_mode === 'move'}
            forcedOnMessage="Always runs for this move queue, regardless of the site-wide setting or any per-queue override — verification is the sole gate on the irreversible remote delete (DESIGN.md §6/§7.3)."
          />
          <InheritableToggle
            label={
              <>
                Extract archives (7zz for zip/7z/tar/gz/bz2/xz; unrar for rar/rar5)
                <FieldHelp label="Extract archives">
                  <p>
                    Alpine's <code>7zz</code> package ships with no RAR codec at all — its RAR
                    decoder derives from unRAR source under a licence that forbids sharing it
                    with an archiver, so distros strip it. This image separately builds a
                    freeware <code>unrar</code> binary from RARLAB source (see{' '}
                    <code>NOTICE</code>) just for <code>.rar</code>/<code>.rar5</code> sets.
                  </p>
                  <p>
                    Dispatch is automatic by file extension — <code>7zz</code> handles
                    zip/7z/tar/gz/bz2/xz, <code>unrar</code> handles rar/rar5. There is nothing
                    to choose here; this one toggle covers both.
                  </p>
                </FieldHelp>
              </>
            }
            value={form.auto_extract}
            onChange={(v) => update('auto_extract', v)}
            siteValue={postprocessSite ? postprocessSite.extract_enabled : null}
          />
          <InheritableToggle
            label="Delete archive volumes once they've extracted successfully"
            value={form.auto_delete_archives}
            onChange={(v) => update('auto_delete_archives', v)}
            siteValue={postprocessSite ? postprocessSite.delete_archives_after_extract : null}
            disabled={!effectiveAutoExtract}
            disabledMessage="Extract (above) doesn't currently run for this queue — turn it on, or its own inherited value, first."
          />
          <InheritableToggle
            label="Move to staging path once finished"
            value={form.auto_move}
            onChange={(v) => update('auto_move', v)}
            siteValue={postprocessSite ? postprocessSite.move_enabled : null}
            disabled={!form.staging_path}
            disabledMessage="Set a staging path above first."
          />
        </div>

        <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-3 dark:border-zinc-800">
          <span className={labelClasses}>
            Folder prefix during transfer — inherits its site-wide default from{' '}
            <Link to="/settings/transfer" className="underline">
              Settings → Transfer
            </Link>{' '}
            unless explicitly overridden below.
            <FieldHelp label="Folder prefix during transfer">
              <p>
                While a <strong>directory</strong> item is downloading, write it into a
                hidden-by-convention folder (e.g. <code>.downloading-Release.Name</code>) and
                rename it to its real name only once the transfer is complete <em>and</em>
                post-processing (verify, then extract) has finished successfully. Sonarr, Radarr,
                Plex, and Jellyfin all skip hidden (dot-prefixed) folders regardless of whether
                they know about lftpweb, so an importer polling the download tree never sees a
                partial multi-file release, nor one that downloaded cleanly but came back
                <code>CORRUPT</code> or failed to extract — those stay hidden under the prefixed
                name until a retry succeeds.
              </p>
              <p>
                <strong>Directory items only</strong> — a single-file download is already
                complete the instant it's renamed off its own in-flight name, so there is no
                partial state for this to protect against.
              </p>
            </FieldHelp>
          </span>
          <InheritableToggle
            label="Enabled"
            value={form.download_prefix_enabled}
            onChange={(v) => update('download_prefix_enabled', v)}
            siteValue={downloadPrefixSite ? downloadPrefixSite.enabled : null}
          />
          <InheritableTextField
            label="Prefix"
            value={form.download_prefix}
            onChange={(v) => update('download_prefix', v)}
            siteValue={downloadPrefixSite ? downloadPrefixSite.prefix : null}
          />
        </div>

        <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-3 dark:border-zinc-800">
          <span className={labelClasses}>
            Sonarr/Radarr integration — off unless an instance is selected below
            <FieldHelp label="Sonarr/Radarr integration">
              <p>
                Binds this queue to a Sonarr or Radarr instance from{' '}
                <Link to="/settings/integrations" className="underline">
                  Settings → Integrations
                </Link>
                . Once bound (and the instance itself is enabled there), items in this queue get
                matched against that instance's download queue, and their Files-page row shows
                the *arr icon once a match is found.
              </p>
              <p>
                <strong>Delete when imported</strong> removes the local copy once the *arr
                confirms it has fully imported the release — never before. It only ever runs
                after two independent signals agree (the *arr's own queue record is gone <em>and</em>{' '}
                its history shows an import event), checked twice, roughly a minute apart, before
                anything is deleted.
              </p>
              <p>
                <strong>Path as seen by the *arr</strong> only matters if "Notify on complete" is
                turned on for the bound instance. Leave it blank when lftpweb and the *arr share
                the exact same path to this queue's files (a common mount, no container path
                translation). Set it when they don't — e.g. lftpweb sees{' '}
                <code>/downloads/tv</code> but the *arr's own container sees the same directory
                as <code>/data/torrents/tv</code>. This describes the queue's{' '}
                <strong>post-move</strong> location: if "Move to staging path" (above) relocates
                a finished item, this field is where <em>that</em> destination lands in the
                *arr's own view, not where downloads first land.
              </p>
            </FieldHelp>
          </span>
          <label className="flex flex-col gap-1">
            <span className={labelClasses}>*arr instance</span>
            <select
              className={inputClasses}
              value={form.arr_instance_id ?? ''}
              onChange={(e) => update('arr_instance_id', e.target.value === '' ? null : Number(e.target.value))}
            >
              <option value="">None</option>
              {arrInstances.map((instance) => (
                <option key={instance.id} value={instance.id}>
                  {instance.name} ({instance.kind})
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={form.arr_delete_completed}
              disabled={arrDeleteCompletedDisabled(form.arr_instance_id)}
              onChange={(e) => update('arr_delete_completed', e.target.checked)}
            />
            <span className="text-sm text-zinc-700 dark:text-zinc-300">
              Delete when imported (only once the *arr confirms it, never on ambiguity)
            </span>
          </label>
          {arrDeleteCompletedDisabled(form.arr_instance_id) && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              Select an *arr instance above first.
            </p>
          )}
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
                {preview.items.length === 0 && (
                  <li className="text-zinc-400">
                    No items to preview — this queue has nothing on the remote or local side yet.
                  </li>
                )}
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
