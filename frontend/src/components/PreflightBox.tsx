import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getSettleSettings } from '../api/client'
import type { PreflightResponse, PreflightRowOut, SettleSettingsOut } from '../api/types'
import { clientEditHref } from '../lib/clientEditLink'
import { pageCount, pageReadout, paginateClientSide } from '../lib/pagination'
import {
  isPreflightPageSize,
  PREFLIGHT_DEFAULT_PAGE_SIZE,
  PREFLIGHT_PAGE_SIZE_OPTIONS,
  preflightBadges,
  preflightChipLabel,
  preflightChipState,
  preflightChipTooltip,
  preflightDetailEntries,
  preflightFillPercent,
  preflightRemainingLabel,
  type PreflightBadge,
  type PreflightPageSize,
} from '../lib/preflight'
import { queueDisplayName } from '../lib/queueDisplayName'
import { readLocalStorage, writeLocalStorage } from '../lib/storage'
import { ArrBrandMark } from './LifecycleIcons'
import { Pager } from './Pager'
import { PageSizeSelect } from './PageSizeSelect'
import { StateChip } from './StateChip'

/** One badge -- the real Sonarr/Radarr logo for a recognized *arr `source_kind`; a plain text
 * chip using the source's own `source_label` for anything else (a client's own `'sabnzbd'`/
 * `'rtorrent'` today, which this codebase has no brand mark for yet, or an *arr kind this
 * codebase hasn't verified). Never gated on `source === 'arr'` any more -- see `SourceBadges`
 * below for why.
 */
function Badge({ badge }: { badge: PreflightBadge }) {
  if (badge.source_kind === 'sonarr' || badge.source_kind === 'radarr') {
    return <ArrBrandMark kind={badge.source_kind} title={badge.source_label} />
  }
  return (
    <span
      className="shrink-0 rounded-full bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400"
      title={badge.source_label}
    >
      {badge.source_label}
    </span>
  )
}

/** A row's own provenance badges -- **the one place source-specific rendering is allowed in this
 * component** (docs/transfers-redesign-spec.md §4's settle-gate follow-up, prefigured, is why
 * this distinction exists at all rather than always drawing the same thing). Widened 2026-08-23
 * (finding #3: "we should show a sonarr AND a SAB icon") from a single *arr-only chip to
 * `lib/preflight.ts.preflightBadges`'s own one-badge-per-contributor rule: **two** badges for a
 * row merged across the *arr and a download client, **one** badge for any standalone row that
 * has a source to name (now including a standalone client row, previously blank here), **zero**
 * for a settle row (lftpweb itself is the source, no brand to show) -- never an empty second
 * slot regardless of which case applies.
 *
 * **Renders nothing at all for a settle row** (2026-08-21, "the columns moved around" fix,
 * user's own report: "it should still have the tag and the column for status on arr icon") --
 * `TransfersPage.tsx`'s own `Row` leaves this exact slot empty whenever a row has nothing to put
 * there (`waiting`/`manual`/`ArrRowChip` all follow the same "render nothing, let the row's own
 * `flex-1` title absorb the slack" idiom, never a reserved blank box), so a settle row's own
 * "column for status on arr icon" is genuinely empty here too, the same way, rather than
 * repeating the queue name a second time in a chip of its own.
 */
function SourceBadges({ row }: { row: PreflightRowOut }) {
  const badges = preflightBadges(row)
  if (badges.length === 0) return null
  return (
    <div className="flex shrink-0 items-center gap-1">
      {badges.map((badge, index) => (
        <Badge key={`${badge.source}:${index}`} badge={badge} />
      ))}
    </div>
  )
}

/** The row's expand panel (finding #6, 2026-08-23: "it is probably time to add a preflight
 * expand option that shows more detail") -- per-contributor detail via `lib/preflight.ts.
 * preflightDetailEntries`: which source, its own raw `status_label`, and its own size/remaining.
 * **Information only, never a control** -- no `onClick` here reaches anything beyond this
 * component's own local `expanded` toggle in the parent; there is no `item`/`job` behind a
 * Preflight row for a button here to act on (`core/preflight.py`'s own docstring, unchanged by
 * this task). Framed as a cache (§4.6): every field shown already arrived on this same response,
 * so collapsing it back loses nothing that a re-fetch wouldn't restore.
 */
function PreflightRowDetail({ row }: { row: PreflightRowOut }) {
  const entries = preflightDetailEntries(row)
  return (
    <div className="flex flex-col gap-1.5 border-t border-zinc-100 bg-zinc-50/60 px-3 py-2 text-xs dark:border-zinc-900 dark:bg-zinc-900/40">
      {entries.map((entry, index) => (
        <div key={`${entry.source}:${index}`} className="flex flex-wrap items-center gap-2">
          <Badge badge={entry} />
          <span className="font-medium text-zinc-600 dark:text-zinc-300">{entry.source_label}</span>
          {entry.status_label && (
            <span className="text-zinc-500 dark:text-zinc-400">{entry.status_label}</span>
          )}
          {entry.sizeLabel && (
            <span className="text-zinc-400 dark:text-zinc-500">{entry.sizeLabel}</span>
          )}
        </div>
      ))}
    </div>
  )
}

/** One Preflight row -- **still deliberately inert**, per the handoff prompt's own instruction:
 * no queue-position number, and (structurally, simply by this component taking no handler props
 * at all beyond its own local expand toggle) no way for Dismiss/Start now/Stop/Retry to ever
 * reach a row here. There is no `item` and no `job` behind it yet, so there is nothing for any
 * of those controls to act on. **The expand chevron added here (finding #6, 2026-08-23) is not
 * an exception to that rule** -- it toggles only this component's own `expanded` state, unfolding
 * `PreflightRowDetail` (information already on this response), never a request or a per-row
 * action.
 *
 * **Column order mirrors `TransfersPage.tsx`'s own `Row`** (2026-08-21, the user's first browser
 * look at the shipped box: "we moved the columns around ... arr icon is at the first of the line
 * now") -- queue tag, title, state chip, source badge(s), then the right-aligned figure column,
 * the same sequence and the same `w-44` width every other row on the page uses, so three boxes
 * stacked in one view read as one table rather than three different layouts.
 */
function PreflightRowView({ row, settle }: { row: PreflightRowOut; settle: SettleSettingsOut | null }) {
  const [expanded, setExpanded] = useState(false)
  const chipLabel = preflightChipLabel(row)
  const chipTooltip = preflightChipTooltip(row, settle)
  const figureLabel = preflightRemainingLabel(row)
  const chipState = preflightChipState(row)
  const fillPercent = preflightFillPercent(row)
  return (
    <div className="border-b border-zinc-200 last:border-b-0 dark:border-zinc-800">
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        aria-expanded={expanded}
        className="flex w-full flex-wrap items-center gap-3 px-3 py-2 text-left text-sm hover:bg-zinc-50 dark:hover:bg-zinc-900/60"
      >
        {/* The expand chevron (finding #6) -- rotates in place, never moves any other column. */}
        <span
          aria-hidden="true"
          className={`shrink-0 text-zinc-400 transition-transform dark:text-zinc-600 ${expanded ? 'rotate-90' : ''}`}
        >
          ▸
        </span>
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
         * or settle, reads the same amber (`preflightChipState` -> `WAITING`/`SETTLING`, both
         * `STYLES` entries are the identical amber) -- "a Preflight row is a waiting row whatever
         * its source" (this task's own reasoning); only the label text (`preflightChipLabel`), the
         * tooltip (`preflightChipTooltip`), and now whether it fills (`preflightFillPercent`,
         * 2026-08-21 follow-up: "we get that detail from arr so we should include it behind the
         * chip") differ by source/status. `fillPercent` is passed unconditionally, same idiom
         * `StateChipProps.percent`'s own docstring recommends -- `SETTLING` has no `FILL_STYLES`
         * entry, so it renders plain regardless of what's passed; only a `WAITING` row (an *arr
         * release actively downloading at a remote client) ever actually shows a bar. */}
        {chipLabel && (
          <StateChip state={chipState} percent={fillPercent} label={chipLabel} title={chipTooltip ?? undefined} />
        )}
        <SourceBadges row={row} />
        {/* The figure column -- `w-44`, matching `Row`'s own (widened from `w-32` when its ETA
         * figure was added; the same reason applies here: `preflightRemainingLabel` can make this
         * the longest figure this box ever shows, once both a size and a remaining time exist). */}
        {figureLabel && (
          <span className="w-44 shrink-0 whitespace-nowrap text-right text-xs text-zinc-500 dark:text-zinc-400">
            {figureLabel}
          </span>
        )}
      </button>
      {expanded && <PreflightRowDetail row={row} />}
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

/** The unattributed-clients banner (finding #2, 2026-08-23,
 * prompts/2026-08-23-tilde-and-visibility.md) -- `GatedQueueBanner`'s own shape (one line per
 * affected thing, never one row per dropped item), applied to a different silent drop: a
 * configured, authenticating, enabled download-client instance whose category -> queue mapping
 * doesn't cover what it's currently reporting. Before this, that client contributed nothing here
 * and said nothing anywhere -- indistinguishable from broken. A separate box from
 * `GatedQueueBanner` above (a different amber fact, named as its own thing) rather than merged
 * into it -- "this queue is blocked" and "this client's items aren't reaching any queue" are
 * different problems with different fixes (a mount, vs. Settings -> Clients' own category
 * mapping), and folding them into one list would blur which fix applies to which line.
 *
 * **Deep-links to the specific instance** (finding #13, 2026-08-23,
 * prompts/2026-08-23-category-control-and-banner-link.md), rather than naming a settings path in
 * prose. The prose it replaced -- "Settings → Integrations → API Clients" -- named a page that
 * does not exist: `nav.ts` has `/settings/integrations` (Sonarr/Radarr) and `/settings/clients`
 * (download clients) as two separate tabs, and "API Clients" is the user's own *eventual*
 * unified-page idea (spec §8.1) leaking into shipped copy before that page exists. `clientEditHref`
 * (`lib/clientEditLink.ts`) opens Settings → Clients with this exact instance already in edit
 * mode (`ClientsTab.tsx`'s own read side), so there's no tab or row left for the user to hunt for.
 */
function UnattributedClientBanner({
  unattributed,
}: {
  unattributed: PreflightResponse['unattributed_clients']
}) {
  if (unattributed.length === 0) return null
  return (
    <div className="flex flex-col gap-1 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
      {unattributed.map((u) => (
        <p key={u.client_id}>
          <span className="font-semibold">{u.client_name}:</span> reports {u.count}{' '}
          {u.count === 1 ? 'item' : 'items'}, none attributable to a queue —{' '}
          <Link to={clientEditHref(u.client_id)} className="underline hover:no-underline">
            fix its category → queue mapping
          </Link>
          .
        </p>
      ))}
    </div>
  )
}

/** The Queue tab's third, small box (docs/transfers-redesign-spec.md §4, prefigured; this task's
 * own handoff prompt, prompts/done/2026-08-20-preflight-box.md, plus its follow-ups
 * prompts/2026-08-20-preflight-waiting-sources.md and
 * prompts/2026-08-21-preflight-label-and-page-size.md) -- things a configured source already
 * knows about but lftpweb has no work to do on yet, sitting above Active/pending because it is
 * first in the pipeline. `TransfersPage.tsx` feeds this `usePreflight()`'s return value directly,
 * unchanged.
 *
 * **Hidden entirely while `response` hasn't loaded yet, or none of `source_configured`,
 * `gated_queues`, or `unattributed_clients` has anything to say** -- the first task's own
 * explicit case (with no row source configured anywhere, "Nothing in preflight" would be
 * permanently true and meaningless) widened twice since: once by the mount-gate banner (a queue
 * can be mount-gated whether or not either row source is configured) and again by the
 * unattributed-clients banner (finding #2, 2026-08-23 -- an unattributable client can have
 * something to say under the identical reasoning), so the box exists whenever *any* of the three
 * has something to show, and stays gone only when none do.
 *
 * **Scales to its content, not to a reserved row count** -- zero rows collapses to the header
 * plus one line ("Nothing in preflight."), never a page's worth of empty space; the row list
 * itself is exactly as tall as it has rows, up to the selected page size. This box sits above the
 * two main ones, so wasted vertical space here pushes real work off-screen -- tightness matters
 * more here than anywhere else on the page (the handoff prompt's own words).
 *
 * **A "Show 5/10/20" selector, not an expand button** (2026-08-21, "we should have a drop down
 * on preflight like the rest, show 5/10/20 etc" -- a follow-up to the first task's own deliberate
 * skip: a 5-row box was judged not to share a growing job history's "see more at once" need,
 * which the user's own real use has now settled the other way). Replaces the previous "Show all
 * (N)" expand-then-page toggle outright rather than sitting beside it -- two controls doing
 * overlapping jobs on one small box is worse than either alone. Persisted per browser
 * (`preflight.pageSize`, `lib/storage.ts`, validated on read via `isPreflightPageSize` exactly as
 * `transfers.activePageSize`/`completePageSize` already are), and reuses `Pager`/`pageReadout`/
 * `PageSizeSelect` -- the identical components the Active/Complete boxes use ("reuse the existing
 * pager ... rather than a third pagination idiom" was the first task's own instruction, extended
 * here to the selector too).
 */
export function PreflightBox({ response }: { response: PreflightResponse | undefined }) {
  const [pageSize, setPageSizeState] = useState<PreflightPageSize>(
    () => readLocalStorage('preflight.pageSize', isPreflightPageSize) ?? PREFLIGHT_DEFAULT_PAGE_SIZE,
  )
  const [page, setPage] = useState(1)
  const setPageSize = (next: PreflightPageSize) => {
    setPageSizeState(next)
    writeLocalStorage('preflight.pageSize', next)
    setPage(1)
  }

  // The settle-wait tooltip's own site-wide constants (`lib/format.ts.settleWaitLabel`'s
  // `required_scans`/`min_age_s`) -- the identical `GET /api/settings/settle` fetch
  // `FileTree.tsx` already makes for the same helper, `null` until it resolves or forever on
  // failure, both of which `preflightChipTooltip` degrades gracefully for (never blocks the box
  // from rendering).
  const [settleSettings, setSettleSettings] = useState<SettleSettingsOut | null>(null)
  useEffect(() => {
    getSettleSettings()
      .then(setSettleSettings)
      .catch(() => {
        // Degrades gracefully -- see the comment above.
      })
  }, [])

  if (response == null) return null
  if (
    !response.source_configured &&
    response.gated_queues.length === 0 &&
    response.unattributed_clients.length === 0
  )
    return null

  const rows = response.rows
  const count = pageCount(rows.length, pageSize)
  const pageRows = paginateClientSide(rows, page, pageSize)

  return (
    <div className="flex flex-col gap-2">
      <h2 className="text-xs font-semibold tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
        Preflight
      </h2>

      <GatedQueueBanner gated={response.gated_queues} />
      <UnattributedClientBanner unattributed={response.unattributed_clients} />

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
                  settle={settleSettings}
                />
              ))}
            </div>
          )}

          {/* The selector itself is never gated on row count (unlike the old "Show all (N)"
           * expand, which only appeared past a fixed collapsed-row threshold) -- it's hidden
           * only by the same rule the whole row list already is, `response.source_configured`.
           * `Pager` and `pageReadout` already degrade to nothing on their own (`count <= 1`,
           * `total <= 0`) when there's nothing yet to page through. */}
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-zinc-500 dark:text-zinc-400">
              {pageReadout(page, count, rows.length)}
            </span>
            <div className="flex items-center gap-2">
              <PageSizeSelect
                id="preflight"
                value={pageSize}
                options={PREFLIGHT_PAGE_SIZE_OPTIONS}
                onChange={setPageSize}
              />
              <Pager current={page} count={count} onChange={setPage} />
            </div>
          </div>
        </>
      )}
    </div>
  )
}
