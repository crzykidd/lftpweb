import { useState } from 'react'
import type { PreflightResponse, PreflightRowOut } from '../api/types'
import { pageCount, pageReadout, paginateClientSide } from '../lib/pagination'
import {
  PREFLIGHT_DEFAULT_ROWS,
  PREFLIGHT_EXPANDED_PAGE_SIZE,
  preflightChipLabel,
  preflightChipTooltip,
  preflightRemainingLabel,
} from '../lib/preflight'
import { queueDisplayName } from '../lib/queueDisplayName'
import { ArrBrandMark } from './LifecycleIcons'
import { Pager } from './Pager'
import { StateChip } from './StateChip'

/** A row's own *arr chip -- **the one place *arr-specific rendering is allowed in this
 * component**, gated on `row.source === 'arr'` (docs/transfers-redesign-spec.md §4's settle-gate
 * follow-up, prefigured, is why this check exists at all rather than always drawing a brand
 * logo). The real Sonarr/Radarr logo for an *arr row with a recognized `source_kind`; a plain
 * text chip using the source's own `source_label` for an *arr row with an unrecognized kind.
 *
 * **Renders nothing at all for a non-*arr row** (2026-08-21, "the columns moved around" fix,
 * user's own report: "it should still have the tag and the column for status on arr icon") --
 * `TransfersPage.tsx`'s own `Row` leaves this exact slot empty whenever a row has nothing to put
 * there (`waiting`/`manual`/`ArrRowChip` all follow the same "render nothing, let the row's own
 * `flex-1` title absorb the slack" idiom, never a reserved blank box), so a settle row's own
 * "column for status on arr icon" is genuinely empty here too, the same way, rather than
 * repeating the queue name a second time in a chip of its own.
 */
function SourceChip({ row }: { row: PreflightRowOut }) {
  if (row.source !== 'arr') return null
  if (row.source_kind === 'sonarr' || row.source_kind === 'radarr') {
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
 *
 * **Column order mirrors `TransfersPage.tsx`'s own `Row`** (2026-08-21, the user's first browser
 * look at the shipped box: "we moved the columns around ... arr icon is at the first of the line
 * now") -- queue tag, title, state chip, *arr chip, then the right-aligned figure column, the
 * same sequence and the same `w-44` width every other row on the page uses, so three boxes
 * stacked in one view read as one table rather than three different layouts.
 */
function PreflightRowView({ row }: { row: PreflightRowOut }) {
  const chipLabel = preflightChipLabel(row)
  const chipTooltip = preflightChipTooltip(row)
  const figureLabel = preflightRemainingLabel(row)
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-zinc-200 px-3 py-2 text-sm last:border-b-0 dark:border-zinc-800">
      {/* Queue tag (2026-08-21) -- the same compact, muted locator `Row`'s own queue badge is,
       * using the identical `queueDisplayName` short-name fallback so this row's tag always
       * agrees with Settings -> Queues and with every Transfers row for the same queue. Missing
       * entirely before this task -- `PreflightRow` carried `queue_id` but no name at all. */}
      <span
        className="max-w-[8rem] shrink-0 truncate rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400"
        title={row.queue_name}
      >
        {queueDisplayName(row.queue_short_name, row.queue_name)}
      </span>
      <span className="min-w-0 flex-1 truncate text-zinc-700 dark:text-zinc-300" title={row.title}>
        {row.title}
      </span>
      {/* The state chip (2026-08-21, "the settling is just a soft grey chip now") -- routed
       * through `StateChip` (`SETTLING`'s existing amber, never a hand-rolled grey span) rather
       * than bypassing it, the actual defect the user's report named. Every Preflight row, *arr
       * or settle, uses the same `SETTLING` colour -- "a Preflight row is a waiting row whatever
       * its source" (this task's own reasoning); only the label text (`preflightChipLabel`) and
       * the tooltip (`preflightChipTooltip`) differ by source. */}
      {chipLabel && <StateChip state="SETTLING" label={chipLabel} title={chipTooltip ?? undefined} />}
      <SourceChip row={row} />
      {/* The figure column -- `w-44`, matching `Row`'s own (widened from `w-32` when its ETA
       * figure was added; the same reason applies here: `preflightRemainingLabel` can make this
       * the longest figure this box ever shows, once both a size and a remaining time exist). */}
      {figureLabel && (
        <span className="w-44 shrink-0 whitespace-nowrap text-right text-xs text-zinc-500 dark:text-zinc-400">
          {figureLabel}
        </span>
      )}
    </div>
  )
}

/** The mount-gate banner (2026-08-20, prompts/2026-08-20-preflight-waiting-sources.md, decided
 * with the user) -- one line per queue `core/autoqueue.py.AutoQueue.gated` is currently blocking
 * entirely, never one row per affected item (fifty identical rows would bury the single fact
 * that matters). `reason` is the backend's own string, verbatim -- shown as-is, never
 * reformatted, matching `preflightStatusLabel`'s own "a source's own wording renders exactly as
 * written" rule for row status text.
 */
function GatedQueueBanner({ gated }: { gated: PreflightResponse['gated_queues'] }) {
  if (gated.length === 0) return null
  return (
    <div className="flex flex-col gap-1 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
      {gated.map((g) => (
        <p key={g.queue_name}>
          <span className="font-semibold">{g.queue_name}:</span> {g.reason}
        </p>
      ))}
    </div>
  )
}

/** The Queue tab's third, small box (docs/transfers-redesign-spec.md §4, prefigured; this task's
 * own handoff prompt, prompts/done/2026-08-20-preflight-box.md, plus its follow-up
 * prompts/2026-08-20-preflight-waiting-sources.md) -- things a configured source already knows
 * about but lftpweb has no work to do on yet, sitting above Active/pending because it is first
 * in the pipeline. `TransfersPage.tsx` feeds this `usePreflight()`'s return value directly,
 * unchanged.
 *
 * **Hidden entirely while `response` hasn't loaded yet, or neither `source_configured` nor
 * `gated_queues` has anything to say** -- the first task's own explicit case (with no row source
 * configured anywhere, "Nothing in preflight" would be permanently true and meaningless)
 * widened, not replaced, by the mount-gate banner: a queue can be mount-gated whether or not
 * either row source is configured, so the box exists whenever *either* half has something to
 * show, and stays gone for a user with neither.
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

  if (response == null) return null
  if (!response.source_configured && response.gated_queues.length === 0) return null

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

      <GatedQueueBanner gated={response.gated_queues} />

      {/* The row list only exists when a row source is actually configured -- a queue can be
       * mount-gated (the banner above) whether or not either source is, and "Nothing in
       * preflight" would be a meaningless thing to say to a user with no source at all. */}
      {response.source_configured && (
        <>
          {/* Zero rows -- one line, not a padded empty-state block (unlike the Active/Complete
           * boxes' own `h-40` dashed panels): "Keeps interface tight" is the user's own
           * reasoning for why this box in particular must not reserve space it isn't using. */}
          {rows.length === 0 && (
            <p className="text-sm text-zinc-400 dark:text-zinc-600">Nothing in preflight.</p>
          )}

          {pageRows.length > 0 && (
            <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
              {pageRows.map((row, index) => (
                <PreflightRowView
                  key={`${row.source}:${row.queue_id}:${row.title}:${index}`}
                  row={row}
                />
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
        </>
      )}

      {response.source_configured && expanded && (
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
