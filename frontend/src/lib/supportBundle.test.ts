import { describe, expect, it } from 'vitest'
import type { ArrInstanceOut } from '../api/types'
import {
  defaultSupportBundleSelection,
  enabledArrInstances,
  toggleArrInstance,
  toSupportBundleRequest,
} from './supportBundle'

function instance(overrides: Partial<ArrInstanceOut> = {}): ArrInstanceOut {
  return {
    id: 1,
    name: 'Sonarr',
    kind: 'sonarr',
    base_url: 'http://sonarr.example.invalid',
    has_api_key: true,
    enabled: true,
    notify_on_complete: false,
    created_at: '2026-08-17T00:00:00.000000Z',
    updated_at: '2026-08-17T00:00:00.000000Z',
    ...overrides,
  }
}

describe('defaultSupportBundleSelection', () => {
  it('defaults every fixed checkbox on', () => {
    const selection = defaultSupportBundleSelection([])
    expect(selection.includeEnvironment).toBe(true)
    expect(selection.includeSettings).toBe(true)
    expect(selection.includeEvents).toBe(true)
    expect(selection.includeJobs).toBe(true)
  })

  it('pre-checks every enabled instance id it is given', () => {
    expect(defaultSupportBundleSelection([1, 2, 3]).arrInstanceIds).toEqual([1, 2, 3])
  })
})

describe('enabledArrInstances', () => {
  it('keeps only enabled instances', () => {
    const enabled = instance({ id: 1, enabled: true })
    const disabled = instance({ id: 2, enabled: false })
    expect(enabledArrInstances([enabled, disabled])).toEqual([enabled])
  })

  it('returns empty for no instances at all -- the "hide the section" case', () => {
    expect(enabledArrInstances([])).toEqual([])
  })

  it('returns empty when every instance is disabled -- also "hide the section"', () => {
    expect(enabledArrInstances([instance({ enabled: false })])).toEqual([])
  })
})

describe('toggleArrInstance', () => {
  it('adds an unchecked instance id', () => {
    const selection = defaultSupportBundleSelection([])
    expect(toggleArrInstance(selection, 5).arrInstanceIds).toEqual([5])
  })

  it('removes an already-checked instance id', () => {
    const selection = defaultSupportBundleSelection([5, 6])
    expect(toggleArrInstance(selection, 5).arrInstanceIds).toEqual([6])
  })

  it('never mutates the input selection', () => {
    const selection = defaultSupportBundleSelection([5])
    const original = [...selection.arrInstanceIds]
    toggleArrInstance(selection, 5)
    expect(selection.arrInstanceIds).toEqual(original)
  })
})

describe('toSupportBundleRequest', () => {
  it('maps every field to the wire shape 1:1', () => {
    const selection = {
      includeEnvironment: true,
      includeSettings: false,
      includeEvents: true,
      includeJobs: false,
      arrInstanceIds: [7, 8],
    }
    expect(toSupportBundleRequest(selection)).toEqual({
      include_environment: true,
      include_settings: false,
      include_events: true,
      include_jobs: false,
      arr_instance_ids: [7, 8],
    })
  })
})
