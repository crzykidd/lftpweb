import { describe, expect, it } from 'vitest'
import type { ArrInstanceOut } from '../../api/types'
import { arrDeleteCompletedDisabled, nextArrDeleteCompleted, queueArrBindingMark } from './QueuesTab'

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

// Settings -> Queues list's brand-icon binding indicator (2026-08-17,
// prompts/2026-08-17-queues-list-arr-brand-icon.md) -- "what should this row render beside the
// queue's name," pinned the same way as the two describe blocks above: pure function, no
// component-render harness.

const SONARR_INSTANCE: ArrInstanceOut = {
  id: 1,
  name: 'Main Sonarr',
  kind: 'sonarr',
  base_url: 'http://sonarr:8989',
  has_api_key: true,
  enabled: true,
  notify_on_complete: true,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
}

const RADARR_INSTANCE_DISABLED: ArrInstanceOut = {
  id: 2,
  name: 'Backup Radarr',
  kind: 'radarr',
  base_url: 'http://radarr:7878',
  has_api_key: true,
  enabled: false,
  notify_on_complete: false,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
}

describe('queueArrBindingMark', () => {
  it('renders the sonarr mark for a bound, enabled instance', () => {
    expect(queueArrBindingMark(1, [SONARR_INSTANCE, RADARR_INSTANCE_DISABLED], true)).toEqual({
      kind: 'sonarr',
      title: "Bound to Sonarr instance 'Main Sonarr'",
      muted: false,
    })
  })

  it('renders the radarr mark muted, with a disabled note, for a bound but disabled instance', () => {
    expect(queueArrBindingMark(2, [SONARR_INSTANCE, RADARR_INSTANCE_DISABLED], true)).toEqual({
      kind: 'radarr',
      title: "Bound to Radarr instance 'Backup Radarr' (instance disabled)",
      muted: true,
    })
  })

  it('renders nothing for an unbound queue', () => {
    expect(queueArrBindingMark(null, [SONARR_INSTANCE], true)).toBeNull()
  })

  it('falls back to a named-id text chip when the bound instance is not in the loaded list', () => {
    expect(queueArrBindingMark(99, [SONARR_INSTANCE], true)).toEqual({
      kind: null,
      title: 'Bound to *arr instance #99 (not found in Settings → Integrations)',
      muted: false,
    })
  })

  it('renders nothing for a bound queue while the instances fetch is still in flight', () => {
    expect(queueArrBindingMark(1, [], false)).toBeNull()
  })
})
