import { describe, expect, it } from 'vitest'
import type { CapabilitySetOut } from '../api/types'
import { capabilityRows } from './clientCapabilities'

// The honesty rules this task's own handoff prompt calls out (spec §4.3, §4.4), pinned as pure
// function tests per this repo's settled "no component-render harness needed for pure logic"
// convention -- `ClientsTab.tsx` renders `capabilityRows`'s own output and adds no further
// judgment about what counts as derived or disabled.

function capsWith(overrides: Partial<CapabilitySetOut>): CapabilitySetOut {
  return {
    operations: {},
    fields: {},
    ...overrides,
  }
}

describe('capabilityRows', () => {
  it('returns no rows for a never-probed instance (null capabilities)', () => {
    expect(capabilityRows(null)).toEqual([])
  })

  it('marks a native capability as not derived and with no disabled reason', () => {
    const caps = capsWith({ operations: { pause: { support: 'native', note: null } } })
    const [row] = capabilityRows(caps)
    expect(row).toMatchObject({ key: 'pause', support: 'native', derived: false, disabledReason: null })
  })

  it('labels a derived capability as derived and surfaces its note -- the rTorrent seed-time case', () => {
    const caps = capsWith({
      fields: {
        seed_time_s: {
          support: 'derived',
          note: 'wall-clock since completion — a stopped torrent still accrues',
        },
      },
    })
    const [row] = capabilityRows(caps)
    expect(row.derived).toBe(true)
    expect(row.support).toBe('derived')
    expect(row.note).toBe('wall-clock since completion — a stopped torrent still accrues')
    expect(row.disabledReason).toBeNull()
  })

  it('gives a none capability a stated reason, using the connector\'s own note when present', () => {
    const caps = capsWith({ fields: { ratio: { support: 'none', note: 'no ratio (spec §5)' } } })
    const [row] = capabilityRows(caps)
    expect(row.support).toBe('none')
    expect(row.derived).toBe(false)
    expect(row.disabledReason).toBe('no ratio (spec §5)')
  })

  it('falls back to a label-derived reason for a none capability with no note at all', () => {
    const caps = capsWith({ fields: { seed_time_s: { support: 'none', note: null } } })
    const [row] = capabilityRows(caps)
    expect(row.disabledReason).not.toBeNull()
    expect(row.disabledReason).toContain('seed time')
  })

  it('lists operations before fields, each in declaration order', () => {
    const caps = capsWith({
      operations: { pause: { support: 'native', note: null }, resume: { support: 'native', note: null } },
      fields: { ratio: { support: 'none', note: null } },
    })
    const rows = capabilityRows(caps)
    expect(rows.map((r) => r.group)).toEqual(['operations', 'operations', 'fields'])
    expect(rows.map((r) => r.key)).toEqual(['pause', 'resume', 'ratio'])
  })

  it('uses the declared label for a known key and falls back to the raw key for an unknown one', () => {
    const caps = capsWith({ operations: { pause: { support: 'native', note: null } } })
    expect(capabilityRows(caps)[0].label).toBe('Pause')

    const unknownCaps = capsWith({ operations: { some_future_op: { support: 'native', note: null } } })
    expect(capabilityRows(unknownCaps)[0].label).toBe('some_future_op')
  })
})
