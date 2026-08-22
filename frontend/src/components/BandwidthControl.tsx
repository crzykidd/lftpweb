import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { setQueueBandwidth } from '../api/client'
import type { TransferSettingsOut } from '../api/types'
import {
  BANDWIDTH_COMMIT_DELAY_MS,
  BANDWIDTH_STEP_MBPS,
  bandwidthAppliedNotice,
  bandwidthBounds,
  bandwidthCommitBps,
  bandwidthCountdownSeconds,
  bandwidthPendingNotice,
  bandwidthSliderValue,
  isBandwidthDirty,
} from '../lib/bandwidth'

// The Queue tab's site-bandwidth slider (2026-08-21,
// `prompts/done/2026-08-21-bandwidth-from-the-queue-page.md`, reshaped the same day by
// `prompts/done/2026-08-21-bandwidth-ceiling-and-autocommit.md`).
//
// **Two values.** Settings → Transfer owns the ceiling (`max_bandwidth_bps`); this slider owns
// the throttle within it and displays `effective_bandwidth_bps`, the limit actually in force.
// The two surfaces no longer show the same number and no longer need to: the slider's *maximum*
// tracks the ceiling live (the page polls `GET /api/settings/transfer` every 5s), which is the
// relationship that replaced "both surfaces reflect each other".
//
// **It commits itself.** Drag → a banner counts five seconds down → the change applies. No Apply
// button, no Cancel, and no confirmation dialog: moving the slider again restarts the countdown,
// so dragging back to where you started cancels for free, and the "Apply to new items only"
// checkbox (checked by default) *is* the consent for the interrupting path. That was settled
// with the user explicitly -- a deliberate uncheck is a better guard than a dialog on every drag,
// and the result banner then reports what actually happened, including the paused case where
// nothing was restarted at all.
//
// **All the wording, bounds and arithmetic live in `lib/bandwidth.ts`**, which is where the tests
// are (README.md's Known gaps: component rendering isn't tested, so decisions belong in pure
// modules). This file owns the timers and the DOM, nothing else.

// How often the countdown re-renders. Not the commit clock -- that is a deadline compared
// against `Date.now()`, the same "a stored deadline, never a running timer" reasoning
// `QueuePauseState.paused_until` uses, so a slow tick or a backgrounded tab can delay the commit
// but can never lose it.
const COUNTDOWN_TICK_MS = 250

interface PendingChange {
  bps: number
  applyToNewOnly: boolean
  deadlineMs: number
}

export function BandwidthControl({
  settings,
  runningCount,
  queuePaused,
}: {
  /** `GET /api/settings/transfer`, polled by the page -- `undefined` until it first resolves. */
  settings: TransferSettingsOut | undefined
  /** How many transfers are running right now. Display only: the count the banner reports comes
   * from the server's own response, since only it knows what was running when the write landed.
   */
  runningCount: number
  queuePaused: boolean
}) {
  // Optimistic echo of a value we just wrote, so the readout doesn't snap back to the old
  // number for the up-to-one-poll-interval before `settings` catches up. Cleared by the effect
  // below the moment the server agrees, so a later change made in Settings → Transfer still
  // wins here rather than being masked forever.
  const [optimisticBps, setOptimisticBps] = useState<number | null>(null)
  const effectiveBps = optimisticBps ?? settings?.effective_bandwidth_bps
  useEffect(() => {
    if (optimisticBps != null && settings?.effective_bandwidth_bps === optimisticBps) {
      setOptimisticBps(null)
    }
  }, [optimisticBps, settings?.effective_bandwidth_bps])

  const ceilingBps = settings?.max_bandwidth_bps
  const bounds = useMemo(
    () => bandwidthBounds(ceilingBps, settings?.min_share_floor_bps),
    [ceilingBps, settings?.min_share_floor_bps],
  )
  const serverMBps = bandwidthSliderValue(effectiveBps, bounds)
  // `null` means "follow the server" -- so a poll that brings in a change made elsewhere is
  // reflected immediately, but a value the user is actively editing is never yanked out from
  // under them.
  const [draftMBps, setDraftMBps] = useState<number | null>(null)
  const valueMBps = draftMBps ?? serverMBps

  const [applyToNewOnly, setApplyToNewOnly] = useState(true)
  const [pending, setPending] = useState<PendingChange | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const commit = useCallback(async (change: PendingChange) => {
    setPending(null)
    setBusy(true)
    setError(null)
    try {
      const outcome = await setQueueBandwidth(change.bps, !change.applyToNewOnly)
      setOptimisticBps(outcome.effective_bandwidth_bps)
      setDraftMBps(null)
      setNotice(bandwidthAppliedNotice(outcome, change.applyToNewOnly))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [])

  // One interval drives both the visible countdown and the commit itself, so the number on
  // screen and the moment it fires can never disagree. `commitRef` keeps the effect from
  // re-arming (and so restarting the tick) on every render.
  const commitRef = useRef(commit)
  commitRef.current = commit
  useEffect(() => {
    if (pending == null) return
    setNow(Date.now())
    const id = window.setInterval(() => {
      const tick = Date.now()
      // Commit *instead of* publishing the tick, never as well as: publishing it would render
      // "applied in 0 seconds…" for the one frame before the result line replaces it.
      if (tick >= pending.deadlineMs) {
        void commitRef.current(pending)
        return
      }
      setNow(tick)
    }, COUNTDOWN_TICK_MS)
    return () => window.clearInterval(id)
  }, [pending])

  /** Arm (or re-arm) the countdown for a change, or drop it entirely when the slider is back on
   * the value already in force -- that is the cancel affordance, and why there is no Cancel
   * button to click.
   */
  const schedule = (nextMBps: number, nextApplyToNewOnly: boolean) => {
    setNotice(null)
    setError(null)
    if (!isBandwidthDirty(nextMBps, effectiveBps, bounds)) {
      setPending(null)
      return
    }
    setPending({
      bps: bandwidthCommitBps(nextMBps, bounds, ceilingBps),
      applyToNewOnly: nextApplyToNewOnly,
      deadlineMs: Date.now() + BANDWIDTH_COMMIT_DELAY_MS,
    })
  }

  const disabled = settings === undefined || busy
  const banner = pending
    ? bandwidthPendingNotice(pending.bps, bandwidthCountdownSeconds(pending.deadlineMs, now))
    : notice

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
            const next = Number(e.target.value)
            setDraftMBps(next)
            schedule(next, applyToNewOnly)
          }}
          className="h-1.5 w-48 cursor-pointer appearance-none rounded-full bg-zinc-200 accent-zinc-700 disabled:opacity-50 dark:bg-zinc-700 dark:accent-zinc-300"
          aria-describedby="site-bandwidth-hint"
        />
        <span className="tabular-nums text-xs font-medium text-zinc-700 dark:text-zinc-200">
          {valueMBps} MB/s
        </span>
        {/* The checkbox *is* the consent for the interrupting path -- unchecking it is the
         * deliberate act, rather than a dialog on every drag (settled with the user). Checked
         * by default, so the safe path is the one that happens by accident. Toggling it mid-
         * countdown re-arms the timer: it changes what is about to happen. */}
        <label className="flex items-center gap-1.5 text-xs text-zinc-600 dark:text-zinc-400">
          <input
            type="checkbox"
            checked={applyToNewOnly}
            disabled={disabled}
            onChange={(e) => {
              const next = e.target.checked
              setApplyToNewOnly(next)
              schedule(valueMBps, next)
            }}
            className="h-3.5 w-3.5 rounded border-zinc-300 accent-zinc-700 disabled:opacity-50 dark:border-zinc-700 dark:accent-zinc-300"
          />
          Apply to new items only
        </label>
        <span id="site-bandwidth-hint" className="text-xs text-zinc-500 dark:text-zinc-400">
          site-wide, up to the Settings → Transfer maximum
          {!applyToNewOnly &&
            (queuePaused
              ? ' — the queue is paused, so nothing will be restarted'
              : runningCount > 0
                ? ` — restarts ${runningCount} running transfer${runningCount === 1 ? '' : 's'}`
                : ' — nothing is running to restart')}
        </span>
      </div>

      {/* One banner, two states, in the same place: the countdown becomes the result. It must
       * never appear twice or stack -- that is why `notice` is cleared the moment a new change
       * is scheduled. */}
      {banner && <span className="text-xs text-zinc-500 dark:text-zinc-400">{banner}</span>}
      {error && (
        <span className="text-xs text-red-600 dark:text-red-400">
          Couldn't change the bandwidth limit: {error}
        </span>
      )}
    </div>
  )
}
