// Pure pagination helpers for the Queue tab's two paginated boxes (docs/transfers-redesign-
// spec.md §3.2, phase 1 stage 4b) -- Active/pending: client-side, `ACTIVE_PAGE_SIZE`/page, the
// set is bounded and already fully loaded; Complete: server-side, `COMPLETE_PAGE_SIZE`/page.
// Page-number windowing in particular is exactly the kind of arithmetic that goes subtly wrong
// at the boundaries (first page, last page, a window wider than the page count), so it lives
// here, pure and unit-tested, rather than inlined in `TransfersPage.tsx` -- this project's
// whole component-testing story is `lib/*.test.ts` (README.md's Known gaps: no component
// rendering is tested).

// Both are now *defaults*, not fixed sizes (2026-08-20, prompts/2026-08-20-transfers-page-size-
// selector.md, a follow-up to phase 1 stage 4b from the user's first real look at the finished
// page) -- each box now carries its own "Show 10/20/50" selector, persisted per browser
// (`TransfersPage.tsx`'s `activePageSize`/`completePageSize` state, `lib/storage.ts`). These
// constants are what a box falls back to when nothing is stored yet (or what's stored is
// invalid -- `isPageSize` below). `COMPLETE_PAGE_SIZE` changes from 50 to 20 here -- the user's
// own words, "probably default to 20 now that I have seen it on screen": 50 was too many rows at
// once in practice, and there is no reason for the two boxes to default differently.
export const ACTIVE_PAGE_SIZE = 20
export const COMPLETE_PAGE_SIZE = 20

/** The only sizes either box's selector offers. A single shared list -- both boxes' dropdowns
 * read from it, and `isPageSize` validates against it, so adding/removing an option can never
 * leave the selector and the validator disagreeing with each other.
 */
export const PAGE_SIZE_OPTIONS = [10, 20, 50] as const

export type PageSize = (typeof PAGE_SIZE_OPTIONS)[number]

/** Validates a page size read back out of `localStorage` -- a hand-edited value (`"999"`), a
 * foreign/corrupt one, or a size this version no longer offers, must fall back to the box's
 * default rather than being trusted (this codebase's existing defensive shape for every other
 * persisted preference, `lib/storage.ts`'s own docstring). `readLocalStorage` already rejects
 * anything that fails `JSON.parse` (a bare `abc`) or throws; this only has to handle a value
 * that *did* parse but isn't one of the offered sizes -- a non-number, or a number not in
 * `PAGE_SIZE_OPTIONS` (`999`, `0`, a stale size an earlier version offered and this one dropped).
 */
export function isPageSize(value: unknown): value is PageSize {
  return typeof value === 'number' && (PAGE_SIZE_OPTIONS as readonly number[]).includes(value)
}

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

/** The "Page X of Y (Z total)" readout shared by both of the Queue tab's boxes' footers
 * (2026-08-20, follow-up to phase 1 stage 4b from the user's browser review: "I have Page 1 of
 * 1 (30 total) at the bottom of the completed section. I don't see that at the active/pending
 * section" -- a real bug, not a design choice. The Complete box already had this wording inline;
 * the Active box had nothing at all. One shared function, not two copies of the same template
 * literal, so the wording can never drift between the two boxes again.
 *
 * `null` while `total <= 0` -- an empty box has nothing to page through, reproducing the
 * Complete box's own pre-existing `completeTotal > 0` guard exactly. Deliberately independent
 * of `Pager`'s own `count <= 1` guard (`TransfersPage.tsx`) -- a single-page box still has a
 * real total worth reading ("Page 1 of 1 (3 total)"), even though `Pager` itself renders
 * nothing for it; the two guards protect different things and must stay separate.
 */
export function pageReadout(page: number, count: number, total: number): string | null {
  if (total <= 0) return null
  return `Page ${page} of ${count} (${total} total)`
}
