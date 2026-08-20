// Pure pagination helpers for the Queue tab's two paginated boxes (docs/transfers-redesign-
// spec.md §3.2, phase 1 stage 4b) -- Active/pending: client-side, `ACTIVE_PAGE_SIZE`/page, the
// set is bounded and already fully loaded; Complete: server-side, `COMPLETE_PAGE_SIZE`/page.
// Page-number windowing in particular is exactly the kind of arithmetic that goes subtly wrong
// at the boundaries (first page, last page, a window wider than the page count), so it lives
// here, pure and unit-tested, rather than inlined in `TransfersPage.tsx` -- this project's
// whole component-testing story is `lib/*.test.ts` (README.md's Known gaps: no component
// rendering is tested).

export const ACTIVE_PAGE_SIZE = 20
export const COMPLETE_PAGE_SIZE = 50

/** How many page-number buttons show at once -- the task's own SAB-style example, `1 2 3 4 >`. */
export const PAGE_WINDOW_SIZE = 5

/** Total pages for `total` items at `pageSize` per page. Always at least `1`, even for
 * `total === 0` -- a page-number control never has to render a "zero pages" special case, and
 * an empty box still reads as "page 1 of 1", not "page 1 of 0". `pageSize <= 0` also floors to
 * `1` rather than dividing by zero or going negative -- a defensive case this codebase's own
 * callers never hit (both page sizes above are positive constants), kept anyway so a caller
 * error degrades to "one page" instead of `Infinity`/`NaN` propagating into `pageWindow`.
 */
export function pageCount(total: number, pageSize: number): number {
  if (pageSize <= 0 || total <= 0) return 1
  return Math.max(1, Math.ceil(total / pageSize))
}

/** Keeps a 1-based page index inside `[1, pageCount(total, pageSize)]` -- the clamp-on-filter-
 * change case: the filter narrows while on page 4, `total` drops, and the page the caller was
 * on no longer exists. Also floors a non-finite/non-positive `page` (`0`, a negative number,
 * `NaN`) to `1` rather than propagating it, so a caller never has to special-case "the stored
 * page was garbage" before calling this.
 */
export function clampPage(page: number, total: number, pageSize: number): number {
  const count = pageCount(total, pageSize)
  const safePage = Number.isFinite(page) ? Math.trunc(page) : 1
  return Math.min(Math.max(1, safePage), count)
}

/** The visible window of page numbers around `current` -- up to `windowSize` consecutive
 * numbers, shifted (never centered-with-overflow) to stay inside `[1, count]`. `count <=
 * windowSize` returns every page, `[1..count]`, so a short list never shows a partial window
 * with room to spare. `current` is clamped into range first, so an out-of-range value (the same
 * "filter narrowed, page 4 no longer exists" case `clampPage` handles) still produces a sane
 * window rather than an empty or out-of-bounds one.
 */
export function pageWindow(current: number, count: number, windowSize: number = PAGE_WINDOW_SIZE): number[] {
  const safeCount = Math.max(1, Math.trunc(count))
  const safeWindow = Math.max(1, Math.trunc(windowSize))
  const safeCurrent = clampPage(current, safeCount, 1)

  if (safeCount <= safeWindow) {
    return Array.from({ length: safeCount }, (_, i) => i + 1)
  }

  let start = safeCurrent - Math.floor(safeWindow / 2)
  start = Math.max(1, start)
  let end = start + safeWindow - 1
  if (end > safeCount) {
    end = safeCount
    start = end - safeWindow + 1
  }
  return Array.from({ length: end - start + 1 }, (_, i) => start + i)
}

/** One page's worth of an already-loaded, bounded array -- the Active/pending box's own
 * pagination (client-side: the set is bounded and fully loaded already, docs/transfers-
 * redesign-spec.md §3.2's own table). 1-based `page`, clamped via `clampPage` so an out-of-range
 * page (the filter just narrowed `items`) returns the last real page's rows rather than an
 * empty slice.
 */
export function paginateClientSide<T>(items: readonly T[], page: number, pageSize: number): T[] {
  const clamped = clampPage(page, items.length, pageSize)
  const start = (clamped - 1) * pageSize
  return items.slice(start, start + pageSize)
}
