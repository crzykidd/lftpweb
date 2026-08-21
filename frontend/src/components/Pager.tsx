import { pageWindow } from '../lib/pagination'

/** Numbered pages, SAB-style (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage
 * 4b) -- `1 2 3 4 ›`, the task's own example. All the boundary arithmetic (the visible window,
 * whether ‹/› are enabled) lives in `lib/pagination.ts` and is unit-tested there; this component
 * is pure layout over that. Renders nothing at all for a single-page box -- a pager with one,
 * disabled page number is clutter, not a control.
 *
 * **Extracted from `TransfersPage.tsx`** (2026-08-20, the Preflight box's own handoff prompt,
 * prompts/done/2026-08-20-preflight-box.md) so `components/PreflightBox.tsx` can reuse the exact
 * same component -- "reuse the existing pager ... rather than a third pagination idiom" was that
 * task's own explicit instruction. Byte-identical behavior to the version that used to live
 * locally in `TransfersPage.tsx`; only the file moved.
 */
export function Pager({
  current,
  count,
  onChange,
}: {
  current: number
  count: number
  onChange: (page: number) => void
}) {
  if (count <= 1) return null
  const visible = pageWindow(current, count)
  return (
    <div className="flex items-center gap-1">
      <button
        type="button"
        disabled={current <= 1}
        onClick={() => onChange(current - 1)}
        aria-label="Previous page"
        className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
      >
        ‹
      </button>
      {visible.map((p) => (
        <button
          key={p}
          type="button"
          aria-current={p === current ? 'page' : undefined}
          onClick={() => onChange(p)}
          className={
            p === current
              ? 'rounded-md border border-indigo-400 bg-indigo-50 px-2 py-1 text-xs font-semibold text-indigo-800 dark:border-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300'
              : 'rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900'
          }
        >
          {p}
        </button>
      ))}
      <button
        type="button"
        disabled={current >= count}
        onClick={() => onChange(current + 1)}
        aria-label="Next page"
        className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
      >
        ›
      </button>
    </div>
  )
}
