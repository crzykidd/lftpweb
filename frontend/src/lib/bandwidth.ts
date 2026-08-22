// The Queue tab's site-bandwidth slider (2026-08-21,
// `prompts/done/2026-08-21-bandwidth-from-the-queue-page.md`) -- pure decision logic only: the
// slider's bounds, whether there is anything to apply, and exactly what the "also apply to
// in-progress" confirmation says. `components/BandwidthControl.tsx` renders what this file
// decides and nothing more, the same split `lib/startNow.ts`/`components/StartNowMenu.tsx`
// already use (README.md's Known gaps: no component rendering is tested, so the decisions live
// where they *can* be).
//
// **One control, one setting.** The slider edits the site-wide `max_bandwidth_bps` -- the same
// value Settings -> Transfer owns (DESIGN.md §4.5: "one container serves one site... A queue
// governs *what* and *where*, never *how fast*"). This is a second surface onto one setting, not
// a new per-queue limit.

/** Whole MB/s. Deliberately coarse: a site ceiling is a rough intent ("about 20 MB/s"), and a
 * finer step would invite drag-by-drag precision the value does not deserve.
 */
export const BANDWIDTH_STEP_MBPS = 1

/** The slider's upper bound when the configured ceiling is smaller -- roughly a saturated
 * gigabit link, which is the practical top end for the seedbox case this project serves. A
 * configured value above it raises the bound instead (below), so a 10 GbE setup is never
 * silently clamped down by opening the page.
 */
export const BANDWIDTH_DEFAULT_MAX_MBPS = 125

const MB = 1_000_000

export interface BandwidthBounds {
  minMBps: number
  maxMBps: number
}

/** The slider's range, in whole MB/s.
 *
 * The floor **reuses the existing `min_share_floor_bps`** rather than inventing a bound (the
 * task's own instruction): a ceiling below the per-job floor means the very first admission
 * already violates it, and `core/queue.py.TransferQueue.set_site_bandwidth` refuses such a value
 * outright. Rounded *up* to a whole MB/s so every position the slider can reach is a value the
 * server will accept. Never below 1 MB/s -- 0 is not "unlimited", it makes headroom zero on
 * every scheduling pass and the main lane admits nothing, ever (DESIGN.md §4.5, and that
 * method's own docstring).
 */
export function bandwidthBounds(
  currentBps: number | null | undefined,
  minShareFloorBps: number | null | undefined,
): BandwidthBounds {
  const floorMBps = Math.ceil(Math.max(minShareFloorBps ?? 0, 0) / MB)
  const minMBps = Math.max(1, floorMBps)
  const currentMBps = Math.ceil(Math.max(currentBps ?? 0, 0) / MB)
  const maxMBps = Math.max(BANDWIDTH_DEFAULT_MAX_MBPS, currentMBps, minMBps)
  return { minMBps, maxMBps }
}

/** Where the slider sits for a stored byte value -- clamped into the bounds so an out-of-range
 * setting (typed into Settings -> Transfer, which is unvalidated on purpose) still renders a
 * usable handle instead of pinning off the end of the track.
 */
export function bandwidthSliderValue(
  currentBps: number | null | undefined,
  bounds: BandwidthBounds,
): number {
  const mbps = Math.round(Math.max(currentBps ?? 0, 0) / MB)
  return Math.min(bounds.maxMBps, Math.max(bounds.minMBps, mbps))
}

/** MB/s -> the bytes/sec the API takes. Whole MB/s in, so this is exact. */
export function bandwidthToBps(mbps: number): number {
  return Math.round(mbps * MB)
}

/** Whether the slider's current position differs from what the server has stored. Compared in
 * *slider* units, not bytes: a stored 10_500_000 B/s renders at the 11 MB/s notch, and calling
 * that "dirty" the moment the page loads would light up the Apply buttons with nothing to apply.
 */
export function isBandwidthDirty(
  draftMBps: number,
  currentBps: number | null | undefined,
  bounds: BandwidthBounds,
): boolean {
  if (currentBps == null) return false
  return draftMBps !== bandwidthSliderValue(currentBps, bounds)
}

export interface ApplyToRunningWarning {
  /** The heading the confirmation leads with. */
  title: string
  /** The body -- what will actually happen, in the user's terms. */
  body: string
  /** The label on the button that goes through with it. */
  confirmLabel: string
}

/** What the "also apply to in-progress" confirmation says **before** acting. The second option
 * interrupts every running transfer, so it has to state how many and what happens to them --
 * presented as a confirmation, not a silent side effect (the task's own requirement).
 *
 * Three shapes, because there are three genuinely different situations:
 *
 * - **Paused** -- nothing will be interrupted *and the pause is not disturbed*. The queue stays
 *   paused, and a "pause for 30 minutes" deadline keeps counting down; the new limit simply
 *   governs everything admitted once the pause ends. Said plainly, because a user who paused on
 *   purpose needs to know this button will not restart their queue.
 * - **Nothing running** -- there is simply nothing to interrupt; the two options are equivalent
 *   right now.
 * - **N running** -- the real case: N transfers stop and immediately restart from the bytes they
 *   have already downloaded, not from the beginning.
 */
export function applyToRunningWarning(
  runningCount: number,
  queuePaused: boolean,
): ApplyToRunningWarning {
  if (queuePaused) {
    return {
      title: 'The queue is paused',
      body:
        'Nothing is being admitted, so there is nothing to interrupt. The new limit is saved ' +
        'and applies to everything that starts once you unpause — and this will not resume the ' +
        'queue or change when a timed pause ends.',
      confirmLabel: 'Save the new limit',
    }
  }
  if (runningCount <= 0) {
    return {
      title: 'Nothing is running',
      body:
        'There are no transfers in progress, so nothing will be interrupted. The new limit ' +
        'applies to everything admitted from here on.',
      confirmLabel: 'Save the new limit',
    }
  }
  const plural = runningCount === 1 ? 'transfer' : 'transfers'
  return {
    title: `This interrupts ${runningCount} running ${plural}`,
    body:
      `A running transfer's speed cannot be changed in place, so ${
        runningCount === 1 ? 'it is' : 'each one is'
      } stopped and immediately restarted at the new limit. ` +
      `${runningCount === 1 ? 'It resumes' : 'They resume'} from the bytes already downloaded — ` +
      'nothing is re-downloaded, and nothing is marked failed or stopped.',
    confirmLabel: `Apply and restart ${runningCount} ${plural}`,
  }
}

/** The one-line result notice shown after an apply lands, from the server's own outcome --
 * never guessed client-side, since only the server knows what was actually running at the
 * moment the request arrived.
 */
export function bandwidthAppliedNotice(outcome: {
  interrupted: number
  skipped_because_paused: boolean
}): string {
  if (outcome.skipped_because_paused) {
    return 'New limit saved. The queue is still paused, so nothing was interrupted.'
  }
  if (outcome.interrupted <= 0) return 'New limit saved.'
  const plural = outcome.interrupted === 1 ? 'transfer' : 'transfers'
  return `New limit saved. ${outcome.interrupted} ${plural} re-started at the new limit.`
}
