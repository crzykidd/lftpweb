import { useCallback } from 'react'
import { getPreflight } from '../api/client'
import type { PreflightResponse } from '../api/types'
import { usePoll } from './usePoll'

// Slower than `useJobs`'s 2000ms -- the Preflight box's own data only changes as often as the
// slowest source behind it refreshes (the *arr poller's own default is 60s,
// `core/arrsync.py.ArrSettings.poll_interval_s`), so polling this endpoint at the same cadence
// as the live transfer queue would just be extra requests for data that hasn't moved. Faster
// than doing nothing between page loads, though -- close to `StatsHeader.tsx`/`WhatsNewDialog
// .tsx`'s own 5000ms health poll, just widened since this box is advisory, not actionable.
const POLL_INTERVAL_MS = 15000

/** The Queue tab's Preflight box (docs/transfers-redesign-spec.md §4, prefigured; this task's
 * own handoff prompt, prompts/done/2026-08-20-preflight-box.md). `undefined` until the first
 * response lands -- `components/PreflightBox.tsx` renders nothing in that window rather than
 * flashing "Nothing in preflight" and then hiding, the same "no source configured" case would
 * otherwise briefly look like.
 */
export function usePreflight(): PreflightResponse | undefined {
  const fetcher = useCallback(getPreflight, [])
  return usePoll(fetcher, POLL_INTERVAL_MS)
}
