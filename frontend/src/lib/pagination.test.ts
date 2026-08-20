import { describe, expect, it } from 'vitest'
import { clampPage, pageCount, pageWindow, paginateClientSide } from './pagination'

describe('pageCount', () => {
  it('is 1 for an empty result set -- "page 1 of 1", never "page 1 of 0"', () => {
    expect(pageCount(0, 20)).toBe(1)
  })

  it('is 1 when total exactly fills one page', () => {
    expect(pageCount(20, 20)).toBe(1)
  })

  it('rounds up for a partially-filled final page', () => {
    expect(pageCount(21, 20)).toBe(2)
    expect(pageCount(39, 20)).toBe(2)
    expect(pageCount(40, 20)).toBe(2)
    expect(pageCount(41, 20)).toBe(3)
  })

  it('floors to 1 for a non-positive pageSize rather than dividing by zero', () => {
    expect(pageCount(100, 0)).toBe(1)
    expect(pageCount(100, -5)).toBe(1)
  })

  it('floors to 1 for a negative total', () => {
    expect(pageCount(-3, 20)).toBe(1)
  })
})

describe('clampPage', () => {
  it('leaves an in-range page untouched', () => {
    expect(clampPage(2, 100, 20)).toBe(2)
  })

  it('clamps a page below 1 up to 1', () => {
    expect(clampPage(0, 100, 20)).toBe(1)
    expect(clampPage(-4, 100, 20)).toBe(1)
  })

  it('clamps a page beyond the last one down to the last real page -- the filter-narrows-while-on-page-4 case', () => {
    // 100 items / 20 per page = 5 pages; page 4 no longer exists once total drops to 30 (2 pages).
    expect(clampPage(4, 30, 20)).toBe(2)
  })

  it('clamps to page 1 when there is exactly one page', () => {
    expect(clampPage(4, 5, 20)).toBe(1)
  })

  it('floors a non-finite page to 1 instead of propagating NaN', () => {
    expect(clampPage(Number.NaN, 100, 20)).toBe(1)
  })

  it('truncates a fractional page rather than rounding', () => {
    expect(clampPage(2.9, 100, 20)).toBe(2)
  })
})

describe('pageWindow', () => {
  it('returns every page when the count fits inside the window', () => {
    expect(pageWindow(1, 3, 5)).toEqual([1, 2, 3])
    expect(pageWindow(3, 5, 5)).toEqual([1, 2, 3, 4, 5])
  })

  it('centers the window around the current page in the middle of a long list', () => {
    expect(pageWindow(10, 20, 5)).toEqual([8, 9, 10, 11, 12])
  })

  it('does not run off the left edge -- page 1 of a long list', () => {
    expect(pageWindow(1, 20, 5)).toEqual([1, 2, 3, 4, 5])
  })

  it('does not run off the right edge -- the last page of a long list', () => {
    expect(pageWindow(20, 20, 5)).toEqual([16, 17, 18, 19, 20])
  })

  it('stays a full-width window one step in from either edge', () => {
    expect(pageWindow(2, 20, 5)).toEqual([1, 2, 3, 4, 5])
    expect(pageWindow(19, 20, 5)).toEqual([16, 17, 18, 19, 20])
  })

  it('clamps an out-of-range current page into range first, rather than an empty/garbage window', () => {
    // Same "filter narrowed while on page 4" scenario as clampPage's own test -- count dropped
    // to 2 pages, but the stale `current` (4) hasn't been clamped by the caller yet.
    expect(pageWindow(4, 2, 5)).toEqual([1, 2])
  })

  it('handles a window wider than the actual page count without padding or duplicates', () => {
    expect(pageWindow(1, 1, 5)).toEqual([1])
  })

  it('handles an even window size, still fully inside range', () => {
    expect(pageWindow(10, 20, 4)).toEqual([8, 9, 10, 11])
  })
})

describe('paginateClientSide', () => {
  const items = Array.from({ length: 45 }, (_, i) => i) // 0..44

  it('slices the first page', () => {
    expect(paginateClientSide(items, 1, 20)).toEqual(items.slice(0, 20))
  })

  it('slices a middle page', () => {
    expect(paginateClientSide(items, 2, 20)).toEqual(items.slice(20, 40))
  })

  it('slices a short final page', () => {
    expect(paginateClientSide(items, 3, 20)).toEqual(items.slice(40, 45))
  })

  it('clamps to the last page when asked for one beyond the end', () => {
    expect(paginateClientSide(items, 99, 20)).toEqual(items.slice(40, 45))
  })

  it('returns an empty page, not an error, for an empty input array', () => {
    expect(paginateClientSide([], 1, 20)).toEqual([])
  })

  it('never mutates the input array', () => {
    const copy = [...items]
    paginateClientSide(items, 2, 20)
    expect(items).toEqual(copy)
  })
})
