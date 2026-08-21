/** "Show 10/20/50" rows-per-page selector (2026-08-20, prompts/2026-08-20-transfers-page-size-
 * selector.md), one independent instance per box. **Extracted from `TransfersPage.tsx`'s own
 * local component** (2026-08-21, prompts/2026-08-21-preflight-label-and-page-size.md) so
 * `components/PreflightBox.tsx` can reuse the identical control with its own narrower option
 * list, rather than a second, independently-styled `<select>` drifting from the first -- the
 * same "reuse the existing pager ... rather than a third pagination idiom" instruction that
 * already moved `components/Pager.tsx` out of `TransfersPage.tsx`, extended to this control.
 *
 * Generic over `T extends number` so each caller supplies its own option list --
 * `lib/pagination.ts.PAGE_SIZE_OPTIONS` (10/20/50) for the Active/Complete boxes,
 * `lib/preflight.ts.PREFLIGHT_PAGE_SIZE_OPTIONS` (5/10/20) for Preflight -- without this
 * component needing to know either exists. Always rendered whenever its own box is (a control
 * that vanishes once the row count drops is hard to find again, and there's no crowding
 * argument here to outweigh that -- it sits in the same footer row as the pager/page-count
 * text, which already tolerates a variable-width neighbour). `id` is per-box so multiple
 * instances' `<label>`s never collide.
 */
export function PageSizeSelect<T extends number>({
  id,
  value,
  options,
  onChange,
}: {
  id: string
  value: T
  options: readonly T[]
  onChange: (size: T) => void
}) {
  const selectId = `page-size-${id}`
  return (
    <div className="flex items-center gap-1.5">
      <label htmlFor={selectId} className="text-xs text-zinc-500 dark:text-zinc-400">
        Show
      </label>
      <select
        id={selectId}
        value={value}
        onChange={(e) => onChange(Number(e.target.value) as T)}
        className="rounded-md border border-zinc-300 bg-white px-1.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800"
      >
        {options.map((size) => (
          <option key={size} value={size}>
            {size}
          </option>
        ))}
      </select>
    </div>
  )
}
