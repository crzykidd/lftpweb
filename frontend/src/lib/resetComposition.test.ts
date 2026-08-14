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

  // 2026-08-14, prompts/2026-08-14-reset-all-preview-undercounts.md: `unpublishedCount` explains
  // rows the preview lists that the Files page no longer shows (a terminal removed row) --
  // without this, the same preview would read as the app inventing items nobody can see.
  it('omits the unpublished clause entirely when the count is zero (the default)', () => {
    expect(describeResetTargets([{ is_dir: true }])).toBe('1 directory — 1 item')
    expect(describeResetTargets([{ is_dir: true }], 0)).toBe('1 directory — 1 item')
  })

  it('appends a singular already-removed clause for exactly one unpublished row', () => {
    expect(describeResetTargets([{ is_dir: true }, { is_dir: true }], 1)).toBe(
      '2 directories — 2 items. 1 of these is already-removed item still tracked in the ' +
        'database, no longer shown on the Files page',
    )
  })

  it('appends a plural already-removed clause for more than one unpublished row', () => {
    expect(
      describeResetTargets([{ is_dir: true }, { is_dir: true }, { is_dir: false }], 2),
    ).toBe(
      '2 directories and 1 file — 3 items. 2 of these are already-removed items still ' +
        'tracked in the database, no longer shown on the Files page',
    )
  })

  it('never appends the clause on the explicit zero-items case', () => {
    // unpublishedCount can't exceed total in practice, but total === 0 must still short-circuit
    // before ever looking at it.
    expect(describeResetTargets([], 5)).toBe('Nothing matches — 0 items.')
  })
})
