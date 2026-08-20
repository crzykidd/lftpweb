import { describe, expect, it } from 'vitest'
import { queueDisplayName } from './queueDisplayName'

describe('queueDisplayName', () => {
  it('falls back to the full name when short_name is null', () => {
    expect(queueDisplayName(null, 'DC-Movies')).toBe('DC-Movies')
  })

  it('prefers the short name when set', () => {
    expect(queueDisplayName('MOV', 'DC-Movies')).toBe('MOV')
  })
})
