import { useEffect, useMemo, useState } from 'react'
import { setQueueBandwidth } from '../api/client'
import type { TransferSettingsOut } from '../api/types'
import {
  BANDWIDTH_STEP_MBPS,
  applyToRunningWarning,
  bandwidthAppliedNotice,
  bandwidthBounds,
  bandwidthSliderValue,
  bandwidthToBps,
  isBandwidthDirty,
} from '../lib/bandwidth'

// The Queue tab's site-bandwidth slider (2026-08-21,
// `prompts/done/2026-08-21-bandwidth-from-the-queue-page.md`).
//
// **One control, one setting.** This edits the site-wide `max_bandwidth_bps` -- the same value
// Settings -> Transfer owns (DESIGN.md §4.5: one container serves one site, and "a queue governs
// *what* and *where*, never *how fast*"). It is a second surface onto one setting, not a new
// per-queue limit. The two surfaces stay in step because the Transfers page polls
// `GET /api/settings/transfer` alongside health, and Settings -> Transfer re-reads it on mount.
//
// **Nothing commits on drag.** Moving the handle only moves a local draft; the write happens on
// an explicit Apply. Committing per-pixel would write the setting hundreds of times and, on the
// apply-to-in-progress path, restart every transfer repeatedly -- and the two apply options are
// genuinely different actions, so an explicit choice is required anyway.
//
// **All the wording and bounds live in `lib/bandwidth.ts`**, which is where the tests are
// (README.md's Known gaps: component rendering isn't tested, so decisions belong in pure
// modules). This file only renders them.

const buttonClasses =
  'rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900'

export function BandwidthControl({
  settings,
  runningCount,
  queuePaused,
}: {
  /** `GET /api/settings/transfer`, polled by the page -- `undefined` until it first resolves. */
  settings: TransferSettingsOut | undefined
  /** How many transfers are running right now, for the confirmation's count. */
  runningCount: number
  queuePaused: boolean
}) {
  // Optimistic echo of a value we just wrote, so the readout doesn't snap back to the old
  // number for the up-to-one-poll-interval before `settings` catches up. Cleared by the effect
  // below the moment the server agrees, so a later change made in Settings -> Transfer still
  // wins here rather than being masked forever.
  const [optimisticBps, setOptimisticBps] = useState<number | null>(null)
  const currentBps = optimisticBps ?? settings?.max_bandwidth_bps
  useEffect(() => {
    if (optimisticBps != null && settings?.max_bandwidth_bps === optimisticBps) {
      setOptimisticBps(null)
    }
  }, [optimisticBps, settings?.max_bandwidth_bps])

  const bounds = useMemo(
    () => bandwidthBounds(currentBps, settings?.min_share_floor_bps),
    [currentBps, settings?.min_share_floor_bps],
  )
  const serverMBps = bandwidthSliderValue(currentBps, bounds)
  // `null` means "follow the server" -- so a poll that brings in a change made elsewhere is
  // reflected immediately, but a value the user is actively editing is never yanked out from
  // under them.
  const [draftMBps, setDraftMBps] = useState<number | null>(null)
  const valueMBps = draftMBps ?? serverMBps
  const dirty = isBandwidthDirty(valueMBps, currentBps, bounds)

  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const warning = applyToRunningWarning(runningCount, queuePaused)

  const apply = async (applyToRunning: boolean) => {
    const bps = bandwidthToBps(valueMBps)
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const outcome = await setQueueBandwidth(bps, applyToRunning)
      setOptimisticBps(outcome.max_bandwidth_bps)
      setDraftMBps(null)
      setConfirming(false)
      setNotice(bandwidthAppliedNotice(outcome))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const disabled = settings === undefined || busy

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-3">
        <label
          htmlFor="site-bandwidth"
          className="text-xs font-medium text-zinc-700 dark:text-zinc-300"
        >
          Bandwidth limit
        </label>
        <input
          id="site-bandwidth"
          type="range"
          min={bounds.minMBps}
          max={bounds.maxMBps}
          step={BANDWIDTH_STEP_MBPS}
          value={valueMBps}
          disabled={disabled}
          onChange={(e) => {
            setDraftMBps(Number(e.target.value))
            setNotice(null)
            setConfirming(false)
          }}
          className="h-1.5 w-48 cursor-pointer appearance-none rounded-full bg-zinc-200 accent-zinc-700 disabled:opacity-50 dark:bg-zinc-700 dark:accent-zinc-300"
          aria-describedby="site-bandwidth-hint"
        />
        <span className="tabular-nums text-xs font-medium text-zinc-700 dark:text-zinc-200">
          {valueMBps} MB/s
        </span>
        <span id="site-bandwidth-hint" className="text-xs text-zinc-500 dark:text-zinc-400">
          site-wide — the same setting as Settings → Transfer
        </span>
        {dirty && !confirming && (
          <>
            <button type="button" disabled={busy} onClick={() => apply(false)} className={buttonClasses}>
              {busy ? 'Saving…' : 'Apply to new transfers'}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setConfirming(true)}
              className={buttonClasses}
            >
              Also apply to in-progress…
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setDraftMBps(null)
                setError(null)
              }}
              className="text-xs text-zinc-500 underline hover:text-zinc-700 disabled:opacity-50 dark:text-zinc-400 dark:hover:text-zinc-200"
            >
              Cancel
            </button>
          </>
        )}
      </div>

      {/* The interruption is stated *before* it happens, never as a silent side effect: a
       * running transfer's rate cannot be changed in place (DESIGN.md §4.5's invariant -- lftp
       * gives us no control channel), so the only way to apply a new limit to one is to stop it
       * and let the scheduler re-admit it. It resumes from its partial bytes, which is exactly
       * what a user needs told before clicking. */}
      {confirming && (
        <div className="flex flex-col gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <p>
            <strong>{warning.title}.</strong> {warning.body}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => apply(true)}
              className="rounded-md border border-amber-400 px-2 py-1 text-xs font-medium hover:bg-amber-100 disabled:opacity-50 dark:border-amber-800 dark:hover:bg-amber-900"
            >
              {busy ? 'Applying…' : warning.confirmLabel}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setConfirming(false)}
              className="rounded-md border border-amber-300 px-2 py-1 text-xs font-medium hover:bg-amber-100 disabled:opacity-50 dark:border-amber-800 dark:hover:bg-amber-900"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {notice && <span className="text-xs text-zinc-500 dark:text-zinc-400">{notice}</span>}
      {error && (
        <span className="text-xs text-red-600 dark:text-red-400">
          Couldn't change the bandwidth limit: {error}
        </span>
      )}
    </div>
  )
}
