import { describe, expect, it } from 'vitest'
import { arrDeleteCompletedDisabled, nextArrDeleteCompleted } from './QueuesTab'

// Sonarr/Radarr integration (docs/arr-integration-spec.md "UI"): the queues-form
// disabled-with-hint logic for "Delete when imported," pinned as pure functions per
// `QueuesTab.tsx`'s own module comment on why (no component-render harness for Settings tabs
// in this suite -- README.md's "Known gaps").

describe('arrDeleteCompletedDisabled', () => {
  it('is disabled with no *arr instance selected', () => {
    expect(arrDeleteCompletedDisabled(null)).toBe(true)
  })

  it('is enabled once an instance is selected', () => {
    expect(arrDeleteCompletedDisabled(1)).toBe(false)
  })
})

describe('nextArrDeleteCompleted', () => {
  it('force-unchecks when the instance is cleared, regardless of the current value', () => {
    expect(nextArrDeleteCompleted(null, true)).toBe(false)
    expect(nextArrDeleteCompleted(null, false)).toBe(false)
  })

  it('leaves the current value alone when an instance is selected', () => {
    expect(nextArrDeleteCompleted(1, true)).toBe(true)
    expect(nextArrDeleteCompleted(1, false)).toBe(false)
  })

  it('leaves the current value alone when switching between two instances', () => {
    expect(nextArrDeleteCompleted(2, true)).toBe(true)
  })
})
