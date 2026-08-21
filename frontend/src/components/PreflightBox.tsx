import { useState } from 'react'
import type { PreflightResponse, PreflightRowOut } from '../api/types'
import { pageCount, pageReadout, paginateClientSide } from '../lib/pagination'
import { PREFLIGHT_DEFAULT_ROWS, PREFLIGHT_EXPANDED_PAGE_SIZE, preflightSizeLabel, preflightStatusLabel } from '../lib/preflight'
import { ArrBrandMark } from './LifecycleIcons'
import { Pager } from './Pager'

/** A row's own source chip -- **the one place *arr-specific rendering is allowed in this
 * component**, gated on `row.source === 'arr'` (docs/transfers-redesign-spec.md §4's settle-gate
 * follow-up, prefigured, is why this check exists at all rather than always drawing a brand
 * logo). The real Sonarr/Radarr logo for an *arr row with a recognized `source_kind`; a plain
 * text chip using the source's own `source_label` for everything else -- an *arr row with an
 * unrecognized kind, or any future non-*arr source, which has no logo of its own to draw.
 */
function SourceChip({ row }: { row: PreflightRowOut }) {
  if (row.source === 'arr' && (row.source_kind === 'sonarr' || row.source_kind === 'radarr')) {
    return <ArrBrandMark kind={row.source_kind} title={row.source_label} />
  }
  return (
    <span
      className="shrink-0 rounded-full bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400"
      title={row.source_label}
    >
      {row.source_label}
    </span>
  )
}

/** One Preflight row -- **deliberately inert**, per the handoff prompt's own instruction: no
 * `onClick`, no expand chevron, no queue-position number, and (structurally, simply by this
 * component taking no handler props at all) no way for Dismiss/Start now/Stop/Retry to ever
 * reach a row here. There is no `item` and no `job` behind it yet, so there is nothing for any
 * of those controls to act on.
 */
function PreflightRowView({ row }: { row: PreflightRowOut }) {
  const statusLabel = preflightStatusLabel(row.status_label)
  const sizeLabel = preflightSizeLabel(row)
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-zinc-200 px-3 py-2 text-sm last:border-b-0 dark:border-zinc-800">
      <SourceChip row={row} />
      <span className="min-w-0 flex-1 truncate text-zinc-700 dark:text-zinc-300" title={row.title}>
        {row.title}
      </span>
      {statusLabel && (
        <span className="shrink-0 rounded-full bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
          {statusLabel}
        </span>
      )}
      {sizeLabel && (
        <span className="w-28 shrink-0 text-right text-xs text-zinc-500 dark:text-zinc-400">{sizeLabel}</span>
      )}
    </div>
  )
}

/** The Queue tab's third, small box (docs/transfers-redesign-spec.md §4, prefigured; this task's
 * own handoff prompt, prompts/done/2026-08-20-preflight-box.md) -- things a configured source
 * already knows about but lftpweb has no work to do on yet, sitting above Active/pending because
 * it is first in the pipeline. `TransfersPage.tsx` feeds this `usePreflight()`'s return value
 * directly, unchanged.
 *
 * **Hidden entirely while `response` hasn't loaded yet, or `source_configured` is false** -- the
 * handoff prompt's own explicit case: with no bound, enabled source anywhere, "Nothing in
 * preflight" would be permanently true and meaningless, so the box doesn't exist for that user
 * at all rather than showing an empty shell forever.
 *
 * **Scales to its content, not to a reserved row count** -- zero rows collapses to the header
 * plus one line ("Nothing in preflight."), never five rows' worth of empty space; the row list
 * itself is exactly as tall as it has rows, up to `PREFLIGHT_DEFAULT_ROWS` while collapsed. This
 * box sits above the two main ones, so wasted vertical space here pushes real work off-screen --
 * tightness matters more here than anywhere else on the page (the handoff prompt's own words).
 *
 * **Expand, then page** -- collapsed shows the first `PREFLIGHT_DEFAULT_ROWS` with no footer at
 * all (nothing to page through yet); "Show all (N)" past that switches to `Pager`/`pageReadout`
 * (`lib/pagination.ts`, the exact same components the Active/Complete boxes use -- "reuse the
 * existing pager ... rather than a third pagination idiom" was the task's own instruction) at a
 * fixed `PREFLIGHT_EXPANDED_PAGE_SIZE`. No per-box "Show 10/20/50" selector here -- see
 * `lib/preflight.ts`'s own comment on `PREFLIGHT_EXPANDED_PAGE_SIZE` for why a 5-row-by-default
 * box doesn't want one.
 */
export function PreflightBox({ response }: { response: PreflightResponse | undefined }) {
  const [expanded, setExpanded] = useState(false)
  const [page, setPage] = useState(1)

  if (response == null || !response.source_configured) return null

  const rows = response.rows
  const count = pageCount(rows.length, PREFLIGHT_EXPANDED_PAGE_SIZE)
  const pageRows = expanded
    ? paginateClientSide(rows, page, PREFLIGHT_EXPANDED_PAGE_SIZE)
    : rows.slice(0, PREFLIGHT_DEFAULT_ROWS)
  const canExpand = rows.length > PREFLIGHT_DEFAULT_ROWS

  const collapse = () => {
    setExpanded(false)
    setPage(1)
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-xs font-semibold tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
          Preflight
        </h2>
        {expanded && (
          <button
            type="button"
            onClick={collapse}
            className="shrink-0 text-xs text-zinc-500 underline decoration-dotted hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
          >
            Show less
          </button>
        )}
      </div>

      {/* Zero rows -- one line, not a padded empty-state block (unlike the Active/Complete
       * boxes' own `h-40` dashed panels): "Keeps interface tight" is the user's own reasoning
       * for why this box in particular must not reserve space it isn't using. */}
      {rows.length === 0 && (
        <p className="text-sm text-zinc-400 dark:text-zinc-600">Nothing in preflight.</p>
      )}

      {pageRows.length > 0 && (
        <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
          {pageRows.map((row, index) => (
            <PreflightRowView key={`${row.source}:${row.queue_id}:${row.title}:${index}`} row={row} />
          ))}
        </div>
      )}

      {!expanded && canExpand && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="self-start text-xs text-zinc-500 underline decoration-dotted hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
        >
          Show all ({rows.length})
        </button>
      )}

      {expanded && (
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-zinc-500 dark:text-zinc-400">
            {pageReadout(page, count, rows.length)}
          </span>
          <Pager current={page} count={count} onChange={setPage} />
        </div>
      )}
    </div>
  )
}
