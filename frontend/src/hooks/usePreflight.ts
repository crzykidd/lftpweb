import { useCallback } from 'react'
import { getPreflight } from '../api/client'
import type { PreflightResponse } from '../api/types'
import { usePoll } from './usePoll'

// Used to be 15000ms, chosen when this box's own eviction was only ever decided once per *arr
// poll (`core/arrsync.py.ArrSettings.poll_interval_s`, 10s default since 2026-08-21's issue #16;
// 60s before that) -- polling faster than that
// just meant re-fetching data that hadn't moved yet. 2026-08-21 ("eviction latency") moved
// retirement to request time in `GET /api/queue/preflight` itself (`ArrSyncScheduler.
// preflight_rows` now re-asks "does a matching item exist" fresh on every call, not just once
// per poll pass), so this endpoint's own freshness is no longer bounded by the *arr's cadence --
// this interval is now the dominant remaining delay, so it drops to match `StatsHeader.tsx`/
// `WhatsNewDialog.tsx`'s own 5000ms health poll. The endpoint itself stays cheap (a cached
// projection plus a couple of small queries), so polling it this often is not a meaningful cost.
const POLL_INTERVAL_MS = 5000

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
