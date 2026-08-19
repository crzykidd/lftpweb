// "Start now" as a menu, not a single button (2026-08-19,
// prompts/done/2026-08-19-start-now-bandwidth-fractions.md) -- DESIGN.md §4.5's "Start now at
// max bandwidth" escape hatch widened into 10%/25%/50%/75%/Max of the site total limit
// (Settings -> Transfer's `max_bandwidth_bps`). Pure decision logic only: which options are
// disabled, what each one is labelled, and what `client.ts.startJobNow` should be called with
// -- this project's whole component-testing story is pure functions in `lib/*.test.ts`
// (README.md's Known gaps: no component rendering is tested), so `components/StartNowMenu.tsx`
// renders exactly what this file decides and nothing more.

/** The five values `POST /api/jobs/{id}/start-now`'s `rate_percent` accepts
 * (`StartNowRequest`'s own `Literal`, `models.py`) -- `100` is Max.
 */
export type StartNowRatePercent = 10 | 25 | 50 | 75 | 100

const PERCENT_OPTIONS: readonly StartNowRatePercent[] = [10, 25, 50, 75, 100]

export const NO_SITE_LIMIT_HINT = 'set a site bandwidth limit to use fractions'

export interface StartNowOption {
  ratePercent: StartNowRatePercent
  label: string
  disabled: boolean
  disabledHint: string | null
}

/** Whether Settings -> Transfer has a real site bandwidth ceiling configured -- mirrors
 * `core/queue.py.TransferQueue.start_now`'s own "no site limit configured" reading exactly
 * (`max_bandwidth_bps <= 0`; `docs/decisions.md` has the call). `null`/`undefined` covers the
 * moment before `GET /api/settings/transfer` has resolved -- treated the same as "not
 * configured" so the menu opens disabled rather than briefly enabled-then-disabled once the
 * real value lands.
 */
export function isSiteLimitConfigured(maxBandwidthBps: number | null | undefined): boolean {
  return maxBandwidthBps != null && maxBandwidthBps > 0
}

/** The menu's five options, in order, for the current site bandwidth setting. The four percent
 * options are disabled (with a hint) exactly when no site limit is configured -- Max is never
 * disabled by this, since it "remains enabled and behaves as today" regardless (the task's own
 * settled decision): it reuses whatever `max_bandwidth_bps` already is, unconditionally, the
 * same as before this task existed.
 */
export function startNowOptions(maxBandwidthBps: number | null | undefined): StartNowOption[] {
  const configured = isSiteLimitConfigured(maxBandwidthBps)
  return PERCENT_OPTIONS.map((ratePercent) => {
    const isMax = ratePercent === 100
    const disabled = !isMax && !configured
    return {
      ratePercent,
      label: isMax ? 'Max' : `${ratePercent}%`,
      disabled,
      disabledHint: disabled ? NO_SITE_LIMIT_HINT : null,
    }
  })
}

/** The argument `client.ts.startJobNow` takes for a chosen option. Max maps to `undefined` --
 * omitting `rate_percent` entirely, matching `api/jobs.py.start_now`'s own "no body at all
 * means Max" contract -- rather than sending an explicit `rate_percent: 100` that would mean
 * the identical thing over the wire.
 */
export function startNowRequestArg(option: StartNowOption): StartNowRatePercent | undefined {
  return option.ratePercent === 100 ? undefined : option.ratePercent
}
