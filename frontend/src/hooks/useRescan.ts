import { useEffect, useState } from 'react'
import { rescanFiles } from '../api/client'

/** Rescan state as a plain value, factored out of the hook below so the three transitions --
 * starting a rescan, the request failing outright, and a `scan_complete` sequence bump arriving
 * -- are ordinary pure functions a test can call directly, this repo's own convention for logic
 * that lives inside a page/hook (see `pages/TransfersPage.test.ts`'s own docstring: pure-logic
 * tests, no component rendering -- README.md's Known gaps). The hook itself
 * (`useRescan` below) is just `useState`/`useEffect` plumbing around these three.
 */
export interface RescanState {
  rescanning: boolean
  /** The `scanCompleteSeq` value captured right before the request went out -- always `null`
   * while `rescanning` is false. */
  baselineSeq: number | null
}

export const initialRescanState: RescanState = { rescanning: false, baselineSeq: null }

/** Called when the button is clicked, before the request goes out. */
export function startRescan(currentSeq: number): RescanState {
  return { rescanning: true, baselineSeq: currentSeq }
}

/** Called when `POST /api/files/rescan` itself fails (network/HTTP) -- there will be no
 * `scan_complete` to clear this, since the engine's wake event was never even set. */
export function rescanRequestFailed(): RescanState {
  return initialRescanState
}

/** Called on every `scan_complete` WS message (any queue), i.e. each time `scanCompleteSeq`
 * changes. Clears only if a rescan is actually in flight and the sequence has moved past the
 * baseline captured by `startRescan` -- a bump that arrives while nothing is rescanning (someone
 * else's scheduled scan, or the interval timer ticking on its own) leaves the state untouched,
 * and is returned as the same object so a caller wiring this into `useState` triggers no extra
 * re-render for a no-op tick. */
export function observeScanComplete(state: RescanState, currentSeq: number): RescanState {
  if (state.rescanning && state.baselineSeq !== null && currentSeq !== state.baselineSeq) {
    return initialRescanState
  }
  return state
}

/**
 * Shared "Rescan now" button behavior (2026-08-21,
 * prompts/2026-08-21-queue-tab-rescan-button.md), lifted out of `FilesPage.tsx` -- the Files tab
 * had this first; the Queue tab (`TransfersPage.tsx`) reuses it exactly rather than pasting the
 * baseline-sequence dance into a second component. `POST /api/files/rescan` (`api/files.py`)
 * only sets the engine's wake event and returns 202 immediately, so completion can only be
 * observed on the wire (`scanCompleteSeq`, bumped by `useLiveModel.ts` on every `scan_complete`),
 * not from the response. A bare `setTimeout(…, 1000)` used to fake it on the Files page, which
 * was simply wrong on any tree that took longer than a second and stayed "Rescanning…" for
 * exactly 1s even when a scan failed outright. See docs/decisions.md for why this is a
 * WebSocket message rather than a blocking endpoint.
 *
 * Rescans **every** queue -- there is no per-queue variant of this endpoint or this hook.
 * `scanCompleteSeq` bumps for any queue's scan, so on a multi-queue install this clears on the
 * first queue to finish, not once all of them have (`core/engine.py.scan_all` iterates queues in
 * sequence; there is no request id in the wire protocol to correlate a specific rescan to a
 * specific completion, and DESIGN.md's rescan button is instance-wide, not per-queue, anyway).
 */
export function useRescan(scanCompleteSeq: number): { rescanning: boolean; triggerRescan: () => Promise<void> } {
  const [state, setState] = useState<RescanState>(initialRescanState)

  const triggerRescan = async () => {
    setState(startRescan(scanCompleteSeq))
    try {
      await rescanFiles()
    } catch {
      setState(rescanRequestFailed())
    }
  }

  useEffect(() => {
    setState((prev) => observeScanComplete(prev, scanCompleteSeq))
  }, [scanCompleteSeq])

  return { rescanning: state.rescanning, triggerRescan }
}
