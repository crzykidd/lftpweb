// The Transfers -> Queue tab's Pause control, redesigned 2026-08-21
// (`prompts/2026-08-21-pause-control-redesign.md`, findings 2 and 3 of
// `prompts/test-findings-2026-08-21.md`) -- pure decision logic only, the same split
// `lib/bandwidth.ts`/`lib/startNow.ts` already use (README.md's Known gaps: no component
// rendering is tested, so decisions live where they *can* be). `components/PauseMenu.tsx`
// renders exactly what this file decides and nothing more.
//
// **What changed.** Before this task, entering a pause was two controls and two steps: a
// duration `<select>` (which did nothing on its own), then a separate two-entry `PauseMenu`
// button ("Pause after current" / "Pause now") that had to be clicked afterwards. The user's own
// words: *"I select it and then I have to hit the pause button, but really it should just do a
// pause on selection of an item."*
//
// **The new shape is one menu plus a checkbox, not two menus.** `pauseMenuOptions` is a single
// list -- "Till I unpause" first (the default), then 1/10/30/60 minutes -- and *selecting* an
// entry is the pause action itself; there is no second click. The fork that used to be the
// second menu ("after current" vs "now") is now a persistent checkbox, **"Pause after active"**,
// unchecked by default (unchecked = pause now, the more common case of the two per the task).
//
// **Finding 2, folded in.** With zero transfers running, "pause after active" and "pause now"
// are the same action, so offering the choice is noise at best and misleading at worst.
// `isPauseAfterActiveAvailable`/`pauseStopRunning` below apply that rule to the checkbox: the
// component disables it (with a reason on hover, matching `lib/startNow.ts`'s own
// disabled-with-hint idiom) when nothing is running, and `pauseStopRunning` independently
// collapses to "pause now" in that same case regardless of the checkbox's last-known value -- so
// a checkbox left checked from a moment ago (when something *was* running) can never send a
// meaningless "wait for nothing" request once the count drops to zero.

/** One of the four fixed durations `POST /api/queue/pause`'s `duration_minutes` accepts, or
 * `undefined` for an indefinite pause ("Till I unpause") -- `pauseQueue`'s own existing contract,
 * untouched by this redesign.
 */
export type PauseDurationMinutes = 1 | 10 | 30 | 60 | undefined

const FIXED_DURATIONS: readonly (1 | 10 | 30 | 60)[] = [1, 10, 30, 60]

export interface PauseMenuOption {
  durationMinutes: PauseDurationMinutes
  label: string
}

/** The single Pause menu's entries, in order: "Till I unpause" first (the default the user asked
 * for), then the four fixed durations. Every entry is always selectable -- unlike
 * `lib/startNow.ts`'s options, none of these depend on server state, so there is nothing to
 * disable here.
 */
export function pauseMenuOptions(): PauseMenuOption[] {
  return [
    { durationMinutes: undefined, label: 'Till I unpause' },
    ...FIXED_DURATIONS.map((durationMinutes) => ({
      durationMinutes,
      label: `${durationMinutes} min`,
    })),
  ]
}

/** Shown as the checkbox's hover title when it is disabled -- the "give a reason on hover" half
 * of the task's own instruction.
 */
export const PAUSE_AFTER_ACTIVE_UNAVAILABLE_HINT =
  'Nothing is running, so pausing after active and pausing now are the same thing.'

/** Finding 2: the "Pause after active" checkbox is only a meaningful choice when something is
 * actually running -- with zero running transfers, checked and unchecked pause the queue
 * identically, so offering the choice would be noise at best and misleading at worst.
 */
export function isPauseAfterActiveAvailable(runningCount: number): boolean {
  return runningCount > 0
}

/** The `stopRunning` argument `pauseQueue` actually sends, given the checkbox's state and how
 * many transfers are running right now. This is where finding 2's rule is enforced, not just
 * displayed: `runningCount <= 0` collapses to `true` ("pause now") unconditionally, so a
 * checkbox left checked from before the running count dropped to zero can never be read as "wait
 * for something that isn't there." Checked ("pause after active") is `false` -- pauseQueue's
 * existing "after current" contract, running jobs finish, nothing new starts; unchecked (the
 * default) is `true` -- "pause now."
 */
export function pauseStopRunning(pauseAfterActive: boolean, runningCount: number): boolean {
  if (!isPauseAfterActiveAvailable(runningCount)) return true
  return !pauseAfterActive
}
