// The Queue tab's site-bandwidth slider (2026-08-21,
// `prompts/done/2026-08-21-bandwidth-from-the-queue-page.md`, reshaped the same day by
// `prompts/done/2026-08-21-bandwidth-ceiling-and-autocommit.md`) -- pure decision logic only:
// the slider's bounds, what commits and when, and exactly what the one banner says in each of
// its states. `components/BandwidthControl.tsx` renders what this file decides and nothing more,
// the same split `lib/startNow.ts`/`components/StartNowMenu.tsx` already use (README.md's Known
// gaps: no component rendering is tested, so the decisions live where they *can* be).
//
// **Two values, not one.** Settings → Transfer owns the **ceiling** (`max_bandwidth_bps`); this
// slider owns the **throttle** within it, and `effective_bandwidth_bps` -- what the server
// reports as the limit in force -- is what it displays and edits. The slider's maximum is the
// ceiling, live. It was one shared value for exactly one day; capping a slider at the number the
// slider itself edits is a ratchet (lower it once and the ceiling comes down with it, so it can
// never be raised back from this page), which is why the model changed. See `docs/decisions.md`.
//
// **Nothing here is a confirmation step.** The old two-buttons-plus-amber-dialog flow was four
// interactions to change a number; it is replaced by a checkbox that is *already* the consent
// for the interrupting path, plus a visible countdown that doubles as the cancel affordance
// (keep dragging, or drag back to where you started, and nothing commits).

/** Whole MB/s. Deliberately coarse: a site ceiling is a rough intent ("about 20 MB/s"), and a
 * finer step would invite drag-by-drag precision the value does not deserve.
 */
export const BANDWIDTH_STEP_MBPS = 1

/** How long after the last change the slider commits itself. **Long, and visible** -- a silent
 * 5 s wait would read as broken, a counted-down one reads as deliberate, which is precisely why
 * it can afford to be this long (the user's own settled wording: "bandwidth update applied in 5
 * seconds"). Every change restarts it: moving the slider again, and toggling the checkbox, since
 * that changes what is about to happen.
 */
export const BANDWIDTH_COMMIT_DELAY_MS = 5000

const MB = 1_000_000

export interface BandwidthBounds {
  minMBps: number
  maxMBps: number
}

/** The slider's range, in whole MB/s.
 *
 * **The maximum is the configured ceiling** (`max_bandwidth_bps`), not an invented bound -- the
 * user's model, stated directly: "the max on this should never exceed the max set in transfer
 * settings. That is the max." Rounded *down* to a whole MB/s so no reachable position exceeds
 * the ceiling; `bandwidthCommitBps` below sends the exact ceiling at the top notch so the
 * rounding can never cost the user the last fraction of their own limit.
 *
 * The floor **reuses the existing `min_share_floor_bps`** rather than inventing a bound (the
 * original task's instruction): a limit below the per-job floor means the very first admission
 * already violates it, and `core/queue.py.TransferQueue.set_site_bandwidth` refuses such a value
 * outright. Rounded *up* to a whole MB/s so every position the slider can reach is a value the
 * server will accept. Never below 1 MB/s -- 0 is not "unlimited", it makes headroom zero on
 * every scheduling pass and the main lane admits nothing, ever (DESIGN.md §4.5).
 *
 * The floor wins a collision (a ceiling below the floor is an expert-surface configuration
 * Settings → Transfer allows on purpose), so the range is never inverted.
 */
export function bandwidthBounds(
  ceilingBps: number | null | undefined,
  minShareFloorBps: number | null | undefined,
): BandwidthBounds {
  const floorMBps = Math.ceil(Math.max(minShareFloorBps ?? 0, 0) / MB)
  const minMBps = Math.max(1, floorMBps)
  const ceilingMBps = Math.floor(Math.max(ceilingBps ?? 0, 0) / MB)
  return { minMBps, maxMBps: Math.max(minMBps, ceilingMBps) }
}

/** Where the slider sits for the limit currently in force -- clamped into the bounds so an
 * out-of-range value (a ceiling typed into Settings → Transfer, which is unvalidated on purpose)
 * still renders a usable handle instead of pinning off the end of the track.
 */
export function bandwidthSliderValue(
  effectiveBps: number | null | undefined,
  bounds: BandwidthBounds,
): number {
  const mbps = Math.round(Math.max(effectiveBps ?? 0, 0) / MB)
  return Math.min(bounds.maxMBps, Math.max(bounds.minMBps, mbps))
}

/** The bytes/sec to send for a slider position. Whole MB/s is exact — **except at the top
 * notch**, which sends the ceiling verbatim: `bandwidthBounds` floors the ceiling to a whole
 * MB/s, so a 10.5 MB/s ceiling would otherwise be untouchable from its own slider (dragging to
 * "max" would quietly throttle to 10). The server clamps anything above the ceiling anyway, so
 * this is about honesty at the top of the track, not about safety.
 */
export function bandwidthCommitBps(
  draftMBps: number,
  bounds: BandwidthBounds,
  ceilingBps: number | null | undefined,
): number {
  const ceiling = Math.max(ceilingBps ?? 0, 0)
  if (draftMBps >= bounds.maxMBps && ceiling > 0) return ceiling
  return Math.round(draftMBps * MB)
}

/** Whether the slider's current position differs from what the server has in force. Compared in
 * *slider* units, not bytes: a stored 10_500_000 B/s renders at the 11 MB/s notch, and calling
 * that "dirty" the moment the page loads would start a countdown nobody asked for.
 *
 * This is also the cancel affordance: drag away and back, and this reads clean again, so the
 * pending commit is dropped rather than firing on the value it started at.
 */
export function isBandwidthDirty(
  draftMBps: number,
  effectiveBps: number | null | undefined,
  bounds: BandwidthBounds,
): boolean {
  if (effectiveBps == null) return false
  return draftMBps !== bandwidthSliderValue(effectiveBps, bounds)
}

/** Whole seconds still to run on the auto-commit countdown, never negative and never above the
 * delay itself. Ceiling-rounded so a freshly-armed timer reads "5", not "4" -- the banner counts
 * 5, 4, 3, 2, 1 and then changes to the result, which is what makes the wait read as deliberate.
 * `deadlineMs`/`nowMs` are `Date.now()`-domain; the caller owns the ticker (no timer lives here).
 */
export function bandwidthCountdownSeconds(deadlineMs: number, nowMs: number): number {
  return Math.max(0, Math.ceil((deadlineMs - nowMs) / 1000))
}

/** A rate in the same decimal MB/s the slider itself is labelled in -- **not** `lib/format.ts`'s
 * `formatRate`, which is binary (1024-based) and would render the 10_000_000 B/s the user just
 * dragged to as "9.5 MB/s". One decimal place only when the value genuinely has one (a ceiling
 * of 10.5 MB/s sent verbatim by `bandwidthCommitBps`).
 */
export function bandwidthLabel(bps: number): string {
  const mbps = Math.max(bps, 0) / MB
  const rounded = Math.round(mbps * 10) / 10
  return `${Number.isInteger(rounded) ? rounded : rounded.toFixed(1)} MB/s`
}

/** The banner's **first** state, shown the instant the slider moves: the change is coming, here
 * is how long you have to keep changing your mind. One banner, two states, in place -- this
 * becomes `bandwidthAppliedNotice` below on commit rather than a second banner appearing under
 * it (the user's own settled shape: "banner pops on change... then the update to applied").
 */
export function bandwidthPendingNotice(bps: number, secondsRemaining: number): string {
  const unit = secondsRemaining === 1 ? 'second' : 'seconds'
  return `Bandwidth update to ${bandwidthLabel(bps)} applied in ${secondsRemaining} ${unit}…`
}

/** The banner's **second** state, from the server's own outcome -- never guessed client-side,
 * since only the server knows what was actually running at the moment the request arrived, and
 * only the server knows whether it clamped the value to the ceiling.
 *
 * Four shapes, because there are four genuinely different outcomes:
 *
 * - **"New transfers only" (the checkbox, checked by default)** -- nothing was interrupted
 *   because nothing was asked to be.
 * - **Unchecked, and the queue is paused** -- the backend deliberately skips re-admission while
 *   paused (that is what stops a bandwidth change cancelling a timed pause) and says so with
 *   `skipped_because_paused`. **Reporting a restart that did not happen would be worse than the
 *   confirmation dialog this banner replaced**, so the paused case is stated outright.
 * - **Unchecked, nothing running** -- the honest "there was nothing to restart".
 * - **Unchecked, N running** -- the real count, from the response, never a generic phrase.
 */
export function bandwidthAppliedNotice(
  outcome: {
    effective_bandwidth_bps: number
    interrupted: number
    skipped_because_paused: boolean
  },
  applyToNewOnly: boolean,
): string {
  const rate = bandwidthLabel(outcome.effective_bandwidth_bps)
  if (applyToNewOnly) return `Bandwidth set to ${rate} for all new transfers.`
  if (outcome.skipped_because_paused) {
    return `Bandwidth set to ${rate} — the queue is paused, so nothing was restarted.`
  }
  if (outcome.interrupted <= 0) {
    return `Bandwidth set to ${rate} — nothing was running, so nothing was restarted.`
  }
  const plural = outcome.interrupted === 1 ? 'transfer' : 'transfers'
  return `Bandwidth set to ${rate} — ${outcome.interrupted} running ${plural} restarted.`
}
