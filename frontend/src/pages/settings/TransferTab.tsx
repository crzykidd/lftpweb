import type { ReactNode } from 'react'
import { useEffect, useMemo, useState } from 'react'
import {
  getDownloadPrefixSettings,
  getEffectiveLftpSettings,
  getHost,
  getSettleSettings,
  getTransferSettings,
  putDownloadPrefixSettings,
  putSettleSettings,
  putTransferSettings,
} from '../../api/client'
import type {
  DownloadPrefixSettingsOut,
  EffectiveLftpSettingsOut,
  SettleSettingsOut,
  TransferSettingsOut,
} from '../../api/types'
import { FieldHelp } from '../../components/FieldHelp'
import { findLftpSettingCollisions } from '../../lib/effectiveLftpSettings'
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
  help?: ReactNode
  hint?: string
  value: number
  step?: number
  min?: number
  onChange: (value: number) => void
}

function NumberField({ label, help, hint, value, step, min = 0, onChange }: NumberFieldProps) {
  return (
    <label className="flex flex-col gap-1">
      <span className={labelClasses}>
        {label}
        {help}
      </span>
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

const DOWNLOAD_PREFIX_EMPTY: DownloadPrefixSettingsOut = {
  enabled: false,
  prefix: '.downloading-',
}

/** Settings → Transfer's "folder prefix during transfer" section (2026-08-14,
 * `core/download_prefix.py`) -- the site-wide default; Settings → Queues has each queue's own
 * inherit-or-override of both fields. Same self-contained load/save shape as
 * `SettleGateSection` above, against its own `GET`/`PUT /api/settings/download-prefix`.
 */
function DownloadPrefixSection() {
  const [settings, setSettings] = useState<DownloadPrefixSettingsOut>(DOWNLOAD_PREFIX_EMPTY)
  const [prefixDraft, setPrefixDraft] = useState(DOWNLOAD_PREFIX_EMPTY.prefix)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    getDownloadPrefixSettings()
      .then((s) => {
        setSettings(s)
        setPrefixDraft(s.prefix)
      })
      .finally(() => setLoading(false))
  }, [])

  const save = async (next: DownloadPrefixSettingsOut) => {
    setError(null)
    setSaving(true)
    setSaved(false)
    try {
      const result = await putDownloadPrefixSettings(next)
      setSettings(result)
      setPrefixDraft(result.prefix)
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
        Folder prefix during transfer
        <FieldHelp label="Folder prefix during transfer">
          <p>
            While a <strong>directory</strong> item is downloading, write it into a
            hidden-by-convention folder (e.g. <code>.downloading-Release.Name</code>) instead of
            its real name, and rename it back only once the transfer is complete <em>and</em>
            post-processing (verify, then extract) has finished successfully — not the instant
            the transfer itself ends. Sonarr, Radarr, Plex, and Jellyfin all skip hidden
            (dot-prefixed) folders regardless of whether they know about lftpweb, so an importer
            polling the download tree can never see a partial multi-file release mid-arrival, nor
            one that downloaded cleanly but failed verification or extraction — a release that
            comes back <code>CORRUPT</code> or fails to extract is never renamed at all, and stays
            hidden under its prefixed name until a retry succeeds.
          </p>
          <p>
            <strong>Directory items only.</strong> A single-file download is already complete
            the instant it's renamed off its own in-flight name — there is no window in which an
            importer could see a partial release, because the release <em>is</em> that one file.
          </p>
          <p>
            The prefix is configurable rather than fixed to <code>.downloading-</code> because
            other tools (or your own scripts) may already use a different in-flight convention
            you'd rather match. Settings → Queues can override either field per queue.
          </p>
        </FieldHelp>
      </h3>
      <p className={hintClasses}>
        Off by default, unlike the settle gate above — this changes where in-flight bytes
        physically live on disk, which an install with a transfer already running when it
        upgrades would notice immediately as a changed path. Turn it on deliberately.
      </p>
      {loading ? (
        <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>
      ) : (
        <>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={settings.enabled}
              disabled={saving}
              onChange={(e) => save({ ...settings, enabled: e.target.checked })}
            />
            <span className={labelClasses}>Enabled</span>
          </label>
          <label className="flex flex-col gap-1">
            <span className={labelClasses}>Prefix</span>
            <div className="flex items-center gap-2">
              <input
                type="text"
                className={`${inputClasses} max-w-64`}
                value={prefixDraft}
                disabled={saving}
                onChange={(e) => setPrefixDraft(e.target.value)}
                onBlur={() => {
                  if (prefixDraft !== settings.prefix) save({ ...settings, prefix: prefixDraft })
                }}
              />
            </div>
            <span className={hintClasses}>
              No path separator; must not collide with lftpweb's own <code>_UNPACK_</code>,{' '}
              <code>_FAILED_</code>, or <code>.lftpweb-mount-ok</code> conventions.
            </span>
          </label>
        </>
      )}
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      {saved && !error && (
        <p className="text-sm text-emerald-600 dark:text-emerald-400">Saved.</p>
      )}
    </div>
  )
}

/** Settings → Transfer's "what lftpweb already sets" readout (2026-08-14,
 * prompts/2026-08-14-show-effective-lftp-settings.md), rendered directly above "Extra lftp
 * settings" (§ the task's own placement instruction: adjacent, visually subordinate to it —
 * this is reference material, not a second control). Collapsed by default via `<details>`:
 * this tab is already dense (DESIGN.md §9.3's live connection-count readout, bandwidth, fast
 * lane, retry, settle gate, and folder-prefix sections all live here too), and a closed
 * disclosure widget is the one option that cannot make an already-crowded page more crowded —
 * no human has click-tested this placement (CLAUDE.md's own "no UI has ever been
 * click-tested" gap), so the safest default was chosen deliberately rather than guessed.
 *
 * Every value shown comes from `GET /api/settings/transfer/effective-lftp`
 * (`api/jobs.py.get_effective_lftp_settings`), itself generated from
 * `core/lftp.py.effective_tuning_settings` / `build_transfer_command` — never hand-typed here,
 * so this section cannot drift the way the Dockerfile's rar-support comment and the old 7zz
 * claim did (both cited in this project's handoff prompt as the failure mode to avoid).
 *
 * Collision detection against the *unsaved* "Extra lftp settings" draft (`extraLftpSettings`
 * prop) is a pure client-side comparison (`lib/effectiveLftpSettings.ts`) — no reason to
 * round-trip a draft to the server just to compare two lists of strings.
 */
function EffectiveLftpSettingsSection({ extraLftpSettings }: { extraLftpSettings: string }) {
  const [data, setData] = useState<EffectiveLftpSettingsOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getEffectiveLftpSettings()
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false))
  }, [])

  const collisions = useMemo(
    () => (data ? findLftpSettingCollisions(extraLftpSettings, data.kinds) : []),
    [data, extraLftpSettings],
  )

  return (
    <details className="group rounded-md border border-zinc-200 dark:border-zinc-800">
      <summary className="cursor-pointer select-none px-4 py-3 text-sm font-medium text-zinc-700 dark:text-zinc-300">
        What lftpweb already sets
        {collisions.length > 0 && (
          <span className="ml-2 font-normal text-amber-600 dark:text-amber-400">
            — {collisions.length} line{collisions.length === 1 ? '' : 's'} below collide
            {collisions.length === 1 ? 's' : ''} with a setting lftpweb already applies
          </span>
        )}
      </summary>
      <div className="flex flex-col gap-4 border-t border-zinc-200 px-4 py-3 dark:border-zinc-800">
        <p className={hintClasses}>
          Every job's rc file and transfer command, generated from this page's own settings —
          so you can tell whether a line in the box below is adding something new, duplicating
          what's already here, or overriding it. Never includes credentials (the seedbox
          password and ssh identity are built separately and never reach this readout).
        </p>
        {loading && <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>}
        {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
        {data && (
          <>
            {collisions.length > 0 && (
              <div className="flex flex-col gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-950/30">
                <p className="text-sm font-medium text-amber-800 dark:text-amber-300">
                  Your "Extra lftp settings" box sets the same key lftpweb already sets, below.
                </p>
                <ul className="flex flex-col gap-1 text-sm text-amber-800 dark:text-amber-300">
                  {collisions.map((c, i) => (
                    <li key={i} className="font-mono text-xs">
                      {c.key}: lftpweb writes{' '}
                      {c.lftpwebOccurrences.map((o) => `${o.value} (${o.kind})`).join(', ')} — your
                      line sets it to {c.userValue}
                    </li>
                  ))}
                </ul>
                <p className={hintClasses}>
                  Your line is appended after every setting lftpweb writes, and lftp applies
                  the last <code>set</code> for a given key — verified against a real lftp
                  binary (tests/test_lftp_settings_accepted.py), so your value is the one that
                  takes effect.
                </p>
              </div>
            )}
            {data.kinds.map((k) => (
              <div key={k.kind} className="flex flex-col gap-2">
                <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
                  {k.kind === 'mirror' ? 'Directory downloads (mirror)' : 'Single-file downloads (pget)'}
                </h4>
                <p className="break-all rounded bg-zinc-100 px-2 py-1.5 font-mono text-xs text-zinc-800 dark:bg-zinc-900 dark:text-zinc-200">
                  {k.argv}
                </p>
                <p className={hintClasses}>{k.argv_why}</p>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="text-zinc-500 dark:text-zinc-400">
                        <th className="pb-1 pr-3 font-medium">Setting</th>
                        <th className="pb-1 pr-3 font-medium">Value</th>
                        <th className="pb-1 font-medium">Why</th>
                      </tr>
                    </thead>
                    <tbody>
                      {k.rc_settings.map((s) => (
                        <tr key={s.key} className="align-top">
                          <td className="py-1 pr-3 font-mono text-zinc-800 dark:text-zinc-200">
                            {s.key}
                            {s.configurable && (
                              <span className="ml-1 rounded bg-zinc-200 px-1 text-[10px] font-sans font-normal text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
                                from this page
                              </span>
                            )}
                          </td>
                          <td className="py-1 pr-3 font-mono text-zinc-800 dark:text-zinc-200">
                            {s.value}
                          </td>
                          <td className={`py-1 ${hintClasses}`}>{s.why}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
            <p className={hintClasses}>{data.bandwidth_note}</p>
          </>
        )}
      </div>
    </details>
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
            help={
              <FieldHelp label="Max concurrent jobs">
                <p>
                  <strong>Main-lane slots only.</strong> The fast lane below has its own,
                  completely separate concurrency budget (<strong>Fast-lane concurrency</strong>)
                  and consumes none of these slots — an item under the fast-lane threshold never
                  waits on this number, and never counts against it.
                </p>
                <p>
                  The real ceiling on transfers running at once is the <strong>sum</strong> of
                  this field and the fast-lane concurrency below. Setting this to 2 while the
                  fast lane allows 2 more can genuinely show 3–4 jobs running — that isn't a bug
                  in this setting, it's the other lane.
                </p>
                <p>
                  <strong>Start now</strong> (Transfers page) bypasses this cap entirely — a
                  forced item always admits immediately, at your chosen share of the bandwidth
                  above (10%/25%/50%/75%, or Max for the full ceiling), regardless of how many
                  slots are already in use.
                </p>
              </FieldHelp>
            }
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
          waits behind a large release holding the main-lane ceiling (DESIGN.md §4.5). Its
          concurrency below is independent of Max concurrent jobs above and consumes none of
          those slots, so the two add together for the real total in flight at once.
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
        <p className={hintClasses}>
          Only three error classes are ever retried at all: host unreachable, a TLS error, and
          (since 2026-08-14) a transient local filesystem error. Everything else (auth failure,
          permission denied, remote path gone, disk full) is permanent and stops on the first
          attempt regardless of the settings below.
        </p>
        <div className="flex flex-wrap gap-4">
          <NumberField
            label="Max attempts"
            help={
              <FieldHelp label="Max attempts">
                <p>
                  How many total attempts a retryable failure gets before it's given up as{' '}
                  <code>retries_exhausted</code> and auto-queue stops touching it (see Docs →
                  Concepts for what that suppression means and how to undo it).
                </p>
              </FieldHelp>
            }
            value={form.maxAttempts}
            step={1}
            min={1}
            onChange={(v) => update('maxAttempts', v)}
          />
          <NumberField
            label="Retry backoff base (seconds)"
            help={
              <FieldHelp label="Retry backoff base (seconds)">
                <p>
                  How long lftpweb waits before retrying a transfer that failed with a{' '}
                  <em>transient</em> error (host unreachable, TLS, or a local filesystem error).
                  The wait doubles each attempt — 30s, 60s, 120s at the default — and is capped
                  at 15 minutes regardless of this value.
                </p>
                <p>
                  A permanent failure (auth, permission denied, remote gone, disk full) is never
                  retried, so this does not apply to those.
                </p>
              </FieldHelp>
            }
            value={form.retryBackoffBaseS}
            step={1}
            onChange={(v) => update('retryBackoffBaseS', v)}
          />
        </div>
      </div>

      <SettleGateSection />

      <DownloadPrefixSection />

      <EffectiveLftpSettingsSection extraLftpSettings={form.extraLftpSettings} />

      <div className="flex flex-col gap-2 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          Extra lftp settings
          <FieldHelp label="Extra lftp settings">
            <p>
              A rejected line can fail in two different, both confusing, ways. An unrecognised{' '}
              <code>set</code> key is silently ignored — the line does nothing, with no error
              anywhere. A recognised key given a value in the wrong format is worse:{' '}
              <code>net:reconnect-interval-base</code> takes a bare number, not a duration —{' '}
              <code>5s</code> makes lftp reject that one line with{' '}
              "5s: invalid unsigned number" while the transfer keeps running on its defaults, so
              the job fails later with a misleading error that has nothing to do with the typo
              that caused it. This class of bug cost this project a real debugging session.
            </p>
            <p>
              If a setting here doesn't seem to be taking effect, check the app log (Settings →
              Logs) for a line like the one above before assuming the field is broken.
            </p>
          </FieldHelp>
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
