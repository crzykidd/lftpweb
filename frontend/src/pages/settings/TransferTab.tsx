import { useEffect, useState } from 'react'
import {
  getHost,
  getSettleSettings,
  getTransferSettings,
  putSettleSettings,
  putTransferSettings,
} from '../../api/client'
import type { SettleSettingsOut, TransferSettingsOut } from '../../api/types'
import { bytesToMB, formatRate, mbToBytes } from '../../lib/format'

const inputClasses =
  'w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const labelClasses = 'text-sm font-medium text-zinc-700 dark:text-zinc-300'
const hintClasses = 'text-xs text-zinc-500 dark:text-zinc-400'

// core/queue.py.TransferSettings's own defaults -- used only until the real GET resolves,
// same convention as PostProcessingTab.tsx's EMPTY.
const EMPTY: TransferSettingsOut = {
  max_bandwidth_bps: 10_000_000,
  max_concurrent_transfers: 2,
  small_item_threshold_bytes: 10_000_000,
  small_lane_concurrency: 2,
  small_lane_reserve_bps: null,
  min_share_floor_bps: 500_000,
  mirror_parallel_transfer_count: 4,
  mirror_use_pget_n: 4,
  pget_default_n: 4,
  max_attempts: 3,
  retry_backoff_base_s: 30,
  extra_lftp_settings: '',
}

/** Mirrors `core/queue.py.TransferSettings.effective_small_lane_reserve_bps()` exactly --
 * the B/2 clamp is load-bearing (its docstring explains why: unconditional "min 1 MB/s"
 * plus no clamp means any ceiling <= 1 MB/s yields a reserve >= the ceiling, so the main
 * lane admits nothing, ever, with no error and no log line). Kept in lockstep with the
 * backend function by hand since this is a client-side *preview* of a server-computed
 * value, not a second source of truth -- if the two ever diverge, the live readout below is
 * the tell.
 */
function rawReserveBps(maxBandwidthBps: number, reserveBps: number | null): number {
  return reserveBps ?? Math.max(Math.round(maxBandwidthBps * 0.1), 1_000_000)
}

function effectiveReserveBps(maxBandwidthBps: number, reserveBps: number | null): number {
  return Math.min(rawReserveBps(maxBandwidthBps, reserveBps), Math.floor(maxBandwidthBps / 2))
}

interface AdmissionPreview {
  admitsNothing: boolean
  ready: number
  shareBps: number
}

/** DESIGN.md §4.5's exact admission pseudocode, evaluated for the "N queued, nothing
 * currently running" case -- the same shape as the worked examples in §4.5's own table
 * ("5 items queued at once, nothing running -> 2 admitted at 5 MB/s each"). This is what
 * turns the abstract twelve fields into the one number that actually matters: what a job
 * gets if you queue enough of them right now.
 */
function admissionPreview(
  maxConcurrentTransfers: number,
  maxBandwidthBps: number,
  reserveBps: number,
  minShareFloorBps: number,
): AdmissionPreview {
  const n = Math.max(0, Math.round(maxConcurrentTransfers))
  const headroom = maxBandwidthBps - reserveBps
  if (n === 0 || headroom <= 0) return { admitsNothing: true, ready: 0, shareBps: 0 }
  let ready = n
  let share = headroom / ready
  while (share < minShareFloorBps && ready > 1) {
    ready -= 1
    share = headroom / ready
  }
  return { admitsNothing: false, ready, shareBps: share }
}

interface FormState {
  maxBandwidthMBps: number
  minShareFloorMBps: number
  maxConcurrentTransfers: number
  mirrorParallelTransferCount: number
  mirrorUsePgetN: number
  pgetDefaultN: number
  smallItemThresholdMB: number
  smallLaneConcurrency: number
  reserveMode: 'derived' | 'custom'
  // Kept even while reserveMode === 'derived', so toggling to "custom" starts from the
  // current effective value instead of resetting to 0.
  smallLaneReserveMBps: number
  maxAttempts: number
  retryBackoffBaseS: number
  extraLftpSettings: string
}

function formFromSettings(s: TransferSettingsOut): FormState {
  return {
    maxBandwidthMBps: bytesToMB(s.max_bandwidth_bps),
    minShareFloorMBps: bytesToMB(s.min_share_floor_bps),
    maxConcurrentTransfers: s.max_concurrent_transfers,
    mirrorParallelTransferCount: s.mirror_parallel_transfer_count,
    mirrorUsePgetN: s.mirror_use_pget_n,
    pgetDefaultN: s.pget_default_n,
    smallItemThresholdMB: bytesToMB(s.small_item_threshold_bytes),
    smallLaneConcurrency: s.small_lane_concurrency,
    reserveMode: s.small_lane_reserve_bps === null ? 'derived' : 'custom',
    smallLaneReserveMBps: bytesToMB(effectiveReserveBps(s.max_bandwidth_bps, s.small_lane_reserve_bps)),
    maxAttempts: s.max_attempts,
    retryBackoffBaseS: s.retry_backoff_base_s,
    extraLftpSettings: s.extra_lftp_settings,
  }
}

function formToBody(f: FormState): TransferSettingsOut {
  return {
    max_bandwidth_bps: mbToBytes(f.maxBandwidthMBps),
    max_concurrent_transfers: Math.max(0, Math.round(f.maxConcurrentTransfers)),
    small_item_threshold_bytes: mbToBytes(f.smallItemThresholdMB),
    small_lane_concurrency: Math.max(0, Math.round(f.smallLaneConcurrency)),
    small_lane_reserve_bps: f.reserveMode === 'derived' ? null : mbToBytes(f.smallLaneReserveMBps),
    min_share_floor_bps: mbToBytes(f.minShareFloorMBps),
    mirror_parallel_transfer_count: Math.max(0, Math.round(f.mirrorParallelTransferCount)),
    mirror_use_pget_n: Math.max(0, Math.round(f.mirrorUsePgetN)),
    pget_default_n: Math.max(0, Math.round(f.pgetDefaultN)),
    max_attempts: Math.max(0, Math.round(f.maxAttempts)),
    retry_backoff_base_s: f.retryBackoffBaseS,
    extra_lftp_settings: f.extraLftpSettings,
  }
}

interface NumberFieldProps {
  label: string
  hint?: string
  value: number
  step?: number
  min?: number
  onChange: (value: number) => void
}

function NumberField({ label, hint, value, step, min = 0, onChange }: NumberFieldProps) {
  return (
    <label className="flex flex-col gap-1">
      <span className={labelClasses}>{label}</span>
      <input
        type="number"
        className={`${inputClasses} max-w-40`}
        value={value}
        step={step}
        min={min}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {hint && <span className={hintClasses}>{hint}</span>}
    </label>
  )
}

// SettleSettingsOut before the real GET resolves -- `required_scans`/`min_age_s` are only
// ever overwritten by the server's response (they're read-only, computed from
// core/settle.py's own constants), so these two values just need to not flash something
// implausible for the one render before the fetch lands.
const SETTLE_EMPTY: SettleSettingsOut = { enabled: true, required_scans: 2, min_age_s: 60 }

/** Settings → Transfer's "the settle gate" section (prompts/open-issues.md #2,
 * `core/settle.py`; UI built in prompts/2026-08-12-settle-gate-followups.md). A self-contained
 * load/save cycle against its own endpoint (`GET`/`PUT /api/settings/settle`) rather than
 * folded into `TransferTab`'s own form state -- a different settings object entirely, and
 * unlike every other field on this page it isn't part of DESIGN.md §4.5's bandwidth/
 * concurrency surface.
 */
function SettleGateSection() {
  const [settings, setSettings] = useState<SettleSettingsOut>(SETTLE_EMPTY)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    getSettleSettings()
      .then(setSettings)
      .finally(() => setLoading(false))
  }, [])

  const handleToggle = async (enabled: boolean) => {
    setError(null)
    setSaving(true)
    setSaved(false)
    try {
      const result = await putSettleSettings({ enabled })
      setSettings(result)
      setSaved(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
      <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Settle gate</h3>
      <p className={hintClasses}>
        Before an item is treated as complete, its remote side must stop changing: the same
        fingerprint of file count, total bytes, and newest modification time must hold across{' '}
        {loading ? '…' : settings.required_scans} consecutive scans, spread over at least{' '}
        {loading ? '…' : settings.min_age_s} seconds of wall-clock time. Both conditions are
        required — the scan count alone can't tell a genuinely settled item from one on a
        queue that just hasn't been rescanned enough times yet. Without this, a multi-file
        release caught mid-upload can read as fully downloaded off whichever files happened to
        finish first, and post-processing (verify/extract/move, and any remote delete) runs on
        a release that is still arriving.
      </p>
      {loading ? (
        <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>
      ) : (
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={settings.enabled}
            disabled={saving}
            onChange={(e) => handleToggle(e.target.checked)}
          />
          <span className={labelClasses}>Enabled</span>
        </label>
      )}
      <p className={hintClasses}>
        Delays every transfer's completion by up to about {loading ? '…' : settings.min_age_s}{' '}
        seconds, including on a landing path that was already atomic and never needed the
        wait — the price of not being fooled by one that isn't. On by default; turn this off
        only if your seedbox's landing path is atomic end to end (e.g. hardlinked torrent
        pickup) and you want to shed that latency entirely.
      </p>
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      {saved && !error && (
        <p className="text-sm text-emerald-600 dark:text-emerald-400">Saved.</p>
      )}
    </div>
  )
}

/** Settings → Transfer (DESIGN.md §4.5, §9.2, §9.3). Site-level bandwidth, concurrency, fast
 * lane, and retry -- "a queue governs what and where, never how fast" (§4.5), so these
 * twelve fields are the entire transfer-tuning surface for the whole instance, not per-queue.
 *
 * The live connection-count readout (§9.3) is the point of this tab, not decoration: these
 * three numbers multiply *silently* into a connection count seedboxes refuse well below what
 * the inputs will happily accept, and nothing in lftp's own output says so.
 */
export function TransferTab() {
  const [form, setForm] = useState<FormState>(formFromSettings(EMPTY))
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  // DESIGN.md §4.5/§9.3 calls net:connection-limit "a first-class setting, host-level" --
  // it isn't yet (docs/decisions.md, 2026-08-12): it's dug out of a JSON blob on the host
  // row with no `HostIn` field to set it. `null` here almost always means "unconfigured,"
  // not "no limit" -- see the note rendered below the readout.
  const [connectionLimit, setConnectionLimit] = useState<number | null>(null)

  useEffect(() => {
    Promise.all([getTransferSettings(), getHost()])
      .then(([settings, host]) => {
        setForm(formFromSettings(settings))
        setConnectionLimit(host?.net_connection_limit ?? null)
      })
      .finally(() => setLoading(false))
  }, [])

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }))
    setSaved(false)
  }

  const handleSave = async () => {
    setError(null)
    setSaving(true)
    try {
      const saved_ = await putTransferSettings(formToBody(form))
      setForm(formFromSettings(saved_))
      setSaved(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>

  const maxBandwidthBps = mbToBytes(form.maxBandwidthMBps)
  const reserveBps = effectiveReserveBps(
    maxBandwidthBps,
    form.reserveMode === 'derived' ? null : mbToBytes(form.smallLaneReserveMBps),
  )
  const worstCaseConnections =
    Math.max(0, Math.round(form.maxConcurrentTransfers)) *
    Math.max(0, Math.round(form.mirrorParallelTransferCount)) *
    Math.max(0, Math.round(form.mirrorUsePgetN))
  const overLimit = connectionLimit != null && worstCaseConnections > connectionLimit
  const preview = admissionPreview(
    form.maxConcurrentTransfers,
    maxBandwidthBps,
    reserveBps,
    mbToBytes(form.minShareFloorMBps),
  )

  return (
    <div className="flex max-w-2xl flex-col gap-6">
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        One set of transfer knobs for the whole instance (DESIGN.md §4.5) — a queue governs
        what and where, never how fast.
      </p>

      {/* DESIGN.md §9.3: "required, not a nice-to-have." Placed first, above the fields that
       * feed it, so it stays visible while the user is turning the dials that drive it. */}
      <div className="flex flex-col gap-2 rounded-md border border-zinc-200 bg-zinc-50 p-4 dark:border-zinc-800 dark:bg-zinc-900/40">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          Live connection-count readout
        </h3>
        <p className="font-mono text-sm text-zinc-800 dark:text-zinc-200">
          {Math.max(0, Math.round(form.maxConcurrentTransfers))} jobs × {Math.max(0, Math.round(form.mirrorParallelTransferCount))} parallel ×{' '}
          {Math.max(0, Math.round(form.mirrorUsePgetN))} pget-n = {worstCaseConnections} concurrent SFTP sessions
          {overLimit && (
            <span className="ml-2 font-sans font-semibold text-red-600 dark:text-red-400">
              ⚠ over net:connection-limit ({connectionLimit})
            </span>
          )}
        </p>
        {connectionLimit == null ? (
          <p className={hintClasses}>
            <code>net:connection-limit</code> isn't set anywhere reachable from this UI today
            (DESIGN.md §4.5 calls it host-level and first-class; Settings → Connection has no
            field for it — see README.md's Known gaps). This warning can only fire once a
            limit is configured some other way.
          </p>
        ) : (
          <p className={hintClasses}>
            Read from the host's connection overrides (<code>net:connection-limit</code>{' '}
            = {connectionLimit}).
          </p>
        )}
        {preview.admitsNothing ? (
          <p className="text-sm font-medium text-red-600 dark:text-red-400">
            ⚠ Fast-lane reserve ({formatRate(reserveBps)}) leaves no headroom under the{' '}
            {formatRate(maxBandwidthBps)} ceiling — the main lane will admit nothing, ever, with
            no error and no log line (DESIGN.md §4.5's reserve-clamp trap).
          </p>
        ) : (
          <p className={hintClasses}>
            At rest, with enough queued to fill every slot: {preview.ready} job
            {preview.ready === 1 ? '' : 's'} admitted at {formatRate(preview.shareBps)} each.
          </p>
        )}
      </div>

      <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Bandwidth</h3>
        <div className="flex flex-wrap gap-4">
          <NumberField
            label="Max bandwidth (MB/s)"
            hint="Ceiling across everything (DESIGN.md §4.5)."
            value={form.maxBandwidthMBps}
            step={0.1}
            onChange={(v) => update('maxBandwidthMBps', v)}
          />
          <NumberField
            label="Minimum share floor (MB/s)"
            hint="Refuse to admit a job below this rate — run fewer, faster instead."
            value={form.minShareFloorMBps}
            step={0.1}
            onChange={(v) => update('minShareFloorMBps', v)}
          />
        </div>
      </div>

      <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Concurrency</h3>
        <div className="flex flex-wrap gap-4">
          <NumberField
            label="Max concurrent jobs"
            hint="Main-lane slots — directories in flight at once."
            value={form.maxConcurrentTransfers}
            step={1}
            onChange={(v) => update('maxConcurrentTransfers', v)}
          />
          <NumberField
            label="Files in parallel per job"
            hint="mirror --parallel"
            value={form.mirrorParallelTransferCount}
            step={1}
            onChange={(v) => update('mirrorParallelTransferCount', v)}
          />
          <NumberField
            label="Connections per file (mirror)"
            hint="mirror --use-pget-n"
            value={form.mirrorUsePgetN}
            step={1}
            onChange={(v) => update('mirrorUsePgetN', v)}
          />
          <NumberField
            label="Connections per file (single-file)"
            hint="pget --default-n, for non-mirror jobs"
            value={form.pgetDefaultN}
            step={1}
            onChange={(v) => update('pgetDefaultN', v)}
          />
        </div>
      </div>

      <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Fast lane</h3>
        <p className={hintClasses}>
          An item under the threshold runs in its own lane, sharing its own reserve — it never
          waits behind a large release holding the main-lane ceiling (DESIGN.md §4.5).
        </p>
        <div className="flex flex-wrap gap-4">
          <NumberField
            label="Threshold (MB)"
            hint="Item's total remote size, below which it's a fast-lane item."
            value={form.smallItemThresholdMB}
            step={1}
            onChange={(v) => update('smallItemThresholdMB', v)}
          />
          <NumberField
            label="Fast-lane concurrency"
            value={form.smallLaneConcurrency}
            step={1}
            onChange={(v) => update('smallLaneConcurrency', v)}
          />
        </div>
        <div className="flex flex-col gap-2">
          <span className={labelClasses}>Fast-lane reserve</span>
          <div className="flex items-center gap-4">
            <label className="flex items-center gap-1.5 text-sm text-zinc-700 dark:text-zinc-300">
              <input
                type="radio"
                checked={form.reserveMode === 'derived'}
                onChange={() => update('reserveMode', 'derived')}
              />
              Derived (10% of ceiling, min 1 MB/s)
            </label>
            <label className="flex items-center gap-1.5 text-sm text-zinc-700 dark:text-zinc-300">
              <input
                type="radio"
                checked={form.reserveMode === 'custom'}
                onChange={() => update('reserveMode', 'custom')}
              />
              Custom
            </label>
          </div>
          {form.reserveMode === 'custom' && (
            <NumberField
              label="Reserve (MB/s)"
              value={form.smallLaneReserveMBps}
              step={0.1}
              onChange={(v) => update('smallLaneReserveMBps', v)}
            />
          )}
          {/* The B/2 clamp applies whether the reserve is derived or a custom number
           * (core/queue.py.effective_small_lane_reserve_bps) -- always show what will
           * actually be used, since that clamp is exactly the thing that's invisible
           * otherwise (see the reserve-clamp-trap warning above). */}
          <p className={hintClasses}>
            Effective reserve: <strong>{formatRate(reserveBps)}</strong>
            {rawReserveBps(
              maxBandwidthBps,
              form.reserveMode === 'derived' ? null : mbToBytes(form.smallLaneReserveMBps),
            ) > Math.floor(maxBandwidthBps / 2) &&
              ` (clamped to half the ${formatRate(maxBandwidthBps)} ceiling)`}
          </p>
        </div>
      </div>

      <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Retry</h3>
        <div className="flex flex-wrap gap-4">
          <NumberField
            label="Max attempts"
            value={form.maxAttempts}
            step={1}
            min={1}
            onChange={(v) => update('maxAttempts', v)}
          />
          <NumberField
            label="Retry backoff base (seconds)"
            value={form.retryBackoffBaseS}
            step={1}
            onChange={(v) => update('retryBackoffBaseS', v)}
          />
        </div>
      </div>

      <SettleGateSection />

      <div className="flex flex-col gap-2 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          Extra lftp settings
        </h3>
        <p className={hintClasses}>
          Free text, injected verbatim into every job's rc file (DESIGN.md §9.3) — the escape
          hatch for anything not exposed as a field above.
        </p>
        <p className={hintClasses}>
          One lftp command per line, exactly as you would write it in <code>.lftprc</code>:{' '}
          <code>set &lt;key&gt; &lt;value&gt;;</code>. Quote any value containing spaces. These
          lines are applied <em>after</em> every setting above, so they override the fields on
          this page. Note that lftp silently ignores a line it doesn't understand rather than
          failing — a typo shows up as behaviour that doesn't change, not as an error.
        </p>
        <textarea
          className={`${inputClasses} min-h-24 font-mono`}
          value={form.extraLftpSettings}
          placeholder={
            'set net:socket-buffer 262144;\n' +
            'set sftp:max-packets-in-flight 16;\n' +
            'set mirror:parallel-directories yes;'
          }
          onChange={(e) => update('extraLftpSettings', e.target.value)}
        />
      </div>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      {saved && !error && (
        <p className="text-sm text-emerald-600 dark:text-emerald-400">Saved.</p>
      )}

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
