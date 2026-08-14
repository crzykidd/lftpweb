import { describe, expect, it } from 'vitest'
import { describeResetTargets } from './resetComposition'

describe('describeResetTargets', () => {
  it('returns the explicit zero case rather than "— 0 items"', () => {
    expect(describeResetTargets([])).toBe('Nothing matches — 0 items.')
  })

  it('describes a single directory, singular', () => {
    expect(describeResetTargets([{ is_dir: true }])).toBe('1 directory — 1 item')
  })

  it('describes a single file, singular', () => {
    expect(describeResetTargets([{ is_dir: false }])).toBe('1 file — 1 item')
  })

  it('pluralizes directories only', () => {
    expect(
      describeResetTargets([{ is_dir: true }, { is_dir: true }]),
    ).toBe('2 directories — 2 items')
  })

  it('pluralizes files only', () => {
    expect(
      describeResetTargets([{ is_dir: false }, { is_dir: false }, { is_dir: false }]),
    ).toBe('3 files — 3 items')
  })

  it('sits at the singular/plural boundary for a mix of exactly one each', () => {
    expect(
      describeResetTargets([{ is_dir: true }, { is_dir: false }]),
    ).toBe('1 directory and 1 file — 2 items')
  })

  it('describes a larger mix, per the prompt\'s own example', () => {
    const items = [
      ...Array.from({ length: 3 }, () => ({ is_dir: true })),
      ...Array.from({ length: 12 }, () => ({ is_dir: false })),
    ]
    expect(describeResetTargets(items)).toBe('3 directories and 12 files — 15 items')
  })
})
