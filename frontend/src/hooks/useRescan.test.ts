import { describe, expect, it } from 'vitest'
import { initialRescanState, observeScanComplete, rescanRequestFailed, startRescan } from './useRescan'

// Pure-logic tests for the three transitions `useRescan` wires into `useState`/`useEffect` --
// no component rendering (this repo's own convention, see `pages/TransfersPage.test.ts`'s
// docstring). These three cover exactly the behavior
// prompts/2026-08-21-queue-tab-rescan-button.md asks for: success-then-bump clears, a request
// failure clears immediately without waiting for a bump, and a bump while nothing is rescanning
// is a no-op.

describe('startRescan', () => {
  it('marks rescanning and captures the current sequence as the baseline', () => {
    expect(startRescan(5)).toEqual({ rescanning: true, baselineSeq: 5 })
  })
})

describe('rescanRequestFailed', () => {
  it('clears immediately -- there will be no scan_complete to wait for', () => {
    expect(rescanRequestFailed()).toEqual(initialRescanState)
  })
})

describe('observeScanComplete', () => {
  it('clears once the sequence moves past the baseline (the success path)', () => {
    const afterRequest = startRescan(5)
    expect(observeScanComplete(afterRequest, 6)).toEqual(initialRescanState)
  })

  it('does nothing while not rescanning', () => {
    expect(observeScanComplete(initialRescanState, 6)).toBe(initialRescanState)
  })

  it('does nothing if the sequence has not actually moved past the baseline yet', () => {
    const afterRequest = startRescan(5)
    expect(observeScanComplete(afterRequest, 5)).toBe(afterRequest)
  })
})
