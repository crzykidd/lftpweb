import { describe, expect, it } from 'vitest'
import {
  NO_SITE_LIMIT_HINT,
  isSiteLimitConfigured,
  startNowOptions,
  startNowRequestArg,
} from './startNow'

describe('isSiteLimitConfigured', () => {
  it('is false for null/undefined (settings not loaded yet)', () => {
    expect(isSiteLimitConfigured(null)).toBe(false)
    expect(isSiteLimitConfigured(undefined)).toBe(false)
  })

  it('is false for zero or negative (the degenerate "admits nothing" ceiling)', () => {
    expect(isSiteLimitConfigured(0)).toBe(false)
    expect(isSiteLimitConfigured(-1)).toBe(false)
  })

  it('is true for any positive ceiling', () => {
    expect(isSiteLimitConfigured(1)).toBe(true)
    expect(isSiteLimitConfigured(10_000_000)).toBe(true)
  })
})

describe('startNowOptions', () => {
  it('returns all five options, in order, when a site limit is configured', () => {
    const options = startNowOptions(10_000_000)
    expect(options.map((o) => o.ratePercent)).toEqual([10, 25, 50, 75, 100])
    expect(options.map((o) => o.label)).toEqual(['10%', '25%', '50%', '75%', 'Max'])
    expect(options.every((o) => !o.disabled)).toBe(true)
    expect(options.every((o) => o.disabledHint === null)).toBe(true)
  })

  it('disables the four percent options, with a hint, when no site limit is configured', () => {
    const options = startNowOptions(0)
    const percentOptions = options.filter((o) => o.ratePercent !== 100)
    expect(percentOptions).toHaveLength(4)
    expect(percentOptions.every((o) => o.disabled)).toBe(true)
    expect(percentOptions.every((o) => o.disabledHint === NO_SITE_LIMIT_HINT)).toBe(true)
  })

  it('never disables Max, regardless of the site limit', () => {
    for (const maxBandwidthBps of [0, null, undefined, -1, 10_000_000]) {
      const max = startNowOptions(maxBandwidthBps).find((o) => o.ratePercent === 100)
      expect(max?.disabled).toBe(false)
      expect(max?.disabledHint).toBeNull()
      expect(max?.label).toBe('Max')
    }
  })

  it('treats an unresolved (null/undefined) site limit the same as "not configured"', () => {
    const withNull = startNowOptions(null)
    const withZero = startNowOptions(0)
    expect(withNull.map((o) => o.disabled)).toEqual(withZero.map((o) => o.disabled))
  })
})

describe('startNowRequestArg', () => {
  it('maps Max to undefined -- omits rate_percent entirely, matching the API\'s own "omitted means Max"', () => {
    const max = startNowOptions(10_000_000).find((o) => o.ratePercent === 100)!
    expect(startNowRequestArg(max)).toBeUndefined()
  })

  it('passes every percent option straight through', () => {
    const options = startNowOptions(10_000_000)
    for (const option of options.filter((o) => o.ratePercent !== 100)) {
      expect(startNowRequestArg(option)).toBe(option.ratePercent)
    }
  })
})
