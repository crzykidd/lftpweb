import { describe, expect, it } from 'vitest'
import {
  PAUSE_AFTER_ACTIVE_UNAVAILABLE_HINT,
  isPauseAfterActiveAvailable,
  pauseMenuOptions,
  pauseStopRunning,
} from './pause'

describe('pauseMenuOptions', () => {
  it('lists "Till I unpause" first, then the four fixed durations in order', () => {
    const options = pauseMenuOptions()
    expect(options.map((o) => o.durationMinutes)).toEqual([undefined, 1, 10, 30, 60])
    expect(options.map((o) => o.label)).toEqual([
      'Till I unpause',
      '1 min',
      '10 min',
      '30 min',
      '60 min',
    ])
  })

  it('returns exactly five entries', () => {
    expect(pauseMenuOptions()).toHaveLength(5)
  })
})

describe('isPauseAfterActiveAvailable', () => {
  it('is false with zero running transfers -- finding 2, the same action either way', () => {
    expect(isPauseAfterActiveAvailable(0)).toBe(false)
  })

  it('is true whenever at least one transfer is running', () => {
    expect(isPauseAfterActiveAvailable(1)).toBe(true)
    expect(isPauseAfterActiveAvailable(5)).toBe(true)
  })

  it('has a non-empty hover hint for the disabled case', () => {
    expect(PAUSE_AFTER_ACTIVE_UNAVAILABLE_HINT.length).toBeGreaterThan(0)
  })
})

describe('pauseStopRunning', () => {
  it('checked ("pause after active") sends stopRunning=false when something is running', () => {
    expect(pauseStopRunning(true, 3)).toBe(false)
  })

  it('unchecked (the default) sends stopRunning=true when something is running', () => {
    expect(pauseStopRunning(false, 3)).toBe(true)
  })

  it('collapses to stopRunning=true with zero running, regardless of the checkbox', () => {
    expect(pauseStopRunning(true, 0)).toBe(true)
    expect(pauseStopRunning(false, 0)).toBe(true)
  })
})
