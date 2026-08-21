import { describe, expect, it } from 'vitest'
import { preflightSizeLabel, preflightStatusLabel } from './preflight'

describe('preflightSizeLabel', () => {
  it('is null when the source gave neither figure -- never a placeholder', () => {
    expect(preflightSizeLabel({ size_bytes: null, size_remaining_bytes: null })).toBeNull()
  })

  it('is just the total when only a total is known -- the settle-gate follow-up\'s own expected shape', () => {
    expect(preflightSizeLabel({ size_bytes: 1_000_000_000, size_remaining_bytes: null })).toBe('953.7 MB')
  })

  it('is a percent-of-total once both figures are known -- an *arr row still downloading', () => {
    expect(preflightSizeLabel({ size_bytes: 1_000_000, size_remaining_bytes: 250_000 })).toBe(
      '75% of 976.6 KB',
    )
  })

  it('is null for a non-positive total -- defensive, matches every other size helper in this codebase', () => {
    expect(preflightSizeLabel({ size_bytes: 0, size_remaining_bytes: 0 })).toBeNull()
  })
})

describe('preflightStatusLabel', () => {
  it('capitalizes the first letter of the source\'s own free-form text', () => {
    expect(preflightStatusLabel('downloading')).toBe('Downloading')
  })

  it('is null straight through -- a row with nothing to say renders nothing, not a placeholder', () => {
    expect(preflightStatusLabel(null)).toBeNull()
  })

  it('is null for an empty string too', () => {
    expect(preflightStatusLabel('')).toBeNull()
  })
})
