// Pure helpers for the Queue tab's Preflight box (docs/transfers-redesign-spec.md §4,
// prefigured; this task's own handoff prompt, prompts/done/2026-08-20-preflight-box.md, plus its
// follow-up prompts/2026-08-20-preflight-waiting-sources.md) -- "something lftpweb already knows
// about but has no work to do on yet." Kept source-agnostic, matching `PreflightRowOut` itself
// (`api/types.ts`): the *arr poller and the settle gate's own eligibility check are the two
// sources wired up (a settle row reads "remote — 22 GB", per `preflightSizeLabel` below) --
// nothing here assumes a row always came from the *arr. This project's whole component-testing
// story is `lib/*.test.ts` (README.md's Known gaps: no component rendering is tested), so the
// box's own display logic lives here, unit-tested, rather than inlined in
// `components/PreflightBox.tsx`.

import type { PreflightRowOut, SettleSettingsOut } from '../api/types'
import { formatBytes, formatEta, settleWaitLabel } from './format'

/** The page-size selector's own default -- "5 rows by default" is the user's own original
 * words for this box, unchanged by the selector's arrival (2026-08-21, "we should have a drop
 * down on preflight like the rest, show 5/10/20 etc"): a box whose whole point is a handful of
 * not-yet-arrived releases still starts small, it just no longer needs a separate "Show all"
 * expand to see more -- the selector replaces that control rather than sitting beside it.
 */
export const PREFLIGHT_DEFAULT_PAGE_SIZE = 5

/** The selector's own offered sizes -- **5/10/20, not the other two boxes' 10/20/50**
 * (`lib/pagination.ts.PAGE_SIZE_OPTIONS`): this box is smaller by intent (a handful of
 * not-yet-arrived releases, not a growing job history), so its own "see more at once" ceiling
 * is smaller too. A dedicated list and validator rather than a re-export of the shared one,
 * since the two boxes' option sets are allowed to diverge -- `isPreflightPageSize` is this
 * box's own `lib/pagination.ts.isPageSize`.
 */
export const PREFLIGHT_PAGE_SIZE_OPTIONS = [5, 10, 20] as const

export type PreflightPageSize = (typeof PREFLIGHT_PAGE_SIZE_OPTIONS)[number]

/** Validates a page size read back out of `localStorage` -- `lib/pagination.ts.isPageSize`'s
 * own contract, mirrored for this box's own narrower option list: a hand-edited value, a
 * foreign one, or a size this version no longer offers must fall back to the default rather
 * than being trusted.
 */
export function isPreflightPageSize(value: unknown): value is PreflightPageSize {
  return typeof value === 'number' && (PREFLIGHT_PAGE_SIZE_OPTIONS as readonly number[]).includes(value)
}

/** A row's own size figure, when its source provided one. "`NN% of X`" once both a total and a
 * remaining figure are known (an *arr row still downloading); just the total when only that is
 * known (the settle-gate follow-up's own expected shape -- a release already fully present
 * remotely has nothing "left", just a known size); `null` when the source gave neither --
 * **never a placeholder for a source that didn't report one** (the handoff prompt's own "never a
 * request to enrich it").
 */
export function preflightSizeLabel(
  row: Pick<PreflightRowOut, 'size_bytes' | 'size_remaining_bytes'>,
): string | null {
  const total = row.size_bytes
  const remaining = row.size_remaining_bytes
  if (total == null || total <= 0) return null
  if (remaining == null) return formatBytes(total)
  const done = Math.max(0, total - remaining)
  const percent = Math.round((done / total) * 100)
  return `${percent}% of ${formatBytes(total)}`
}

/** Capitalizes a source's own free-form status text for display (`"downloading"` ->
 * `"Downloading"`) -- shown verbatim otherwise, never interpreted or mapped to a fixed
 * vocabulary (unlike `chipStateFor`'s own job-state chip): a second source's own wording
 * (`"Settling"`, say) must render exactly as that source wrote it. `null` straight through, so a
 * row with nothing to say renders nothing rather than a placeholder dash.
 */
export function preflightStatusLabel(statusLabel: string | null): string | null {
  if (!statusLabel) return null
  return statusLabel.charAt(0).toUpperCase() + statusLabel.slice(1)
}

/** The figure column's own text (2026-08-21, "we missed the remaining time") -- `preflightSizeLabel`
 * above, plus a " · <duration> left" suffix once `remaining_s` is known, through the exact
 * `formatEta` + " left" combination `lib/transferPanel.ts.transferLineValue` already uses for the
 * Transfers row's own ETA, rather than a second time-formatting idiom. A settle-gated row's
 * `remaining_s` is always `null` (`core/preflight.py.PreflightRow`'s own docstring: that source's
 * remaining figure is `size_bytes`, not a time) so it renders exactly as `preflightSizeLabel`
 * alone always has -- this function widens what an *arr row can show without changing a settle
 * row's own reading at all.
 */
export function preflightRemainingLabel(
  row: Pick<PreflightRowOut, 'size_bytes' | 'size_remaining_bytes' | 'remaining_s'>,
): string | null {
  const base = preflightSizeLabel(row)
  if (row.remaining_s == null) return base
  const remaining = `${formatEta(row.remaining_s)} left`
  return base ? `${base} · ${remaining}` : remaining
}

/** An *arr row's own `trackedDownloadState`, translated into what it means **to lftpweb** rather
 * than shown as the *arr's raw word (2026-08-21, user's own browser report: "that downloading
 * chip in preflight is arr telling it is downloading from the client ... I think we should spell
 * that out"). `downloading` -- the state the user actually saw -- becomes **"Waiting"**
 * (2026-08-21, a follow-up browser look: an earlier "Waiting for download" round read equally as
 * "lftpweb is waiting to download it" and "waiting for the download client to finish"; the user
 * considered "Waiting for remote client", asked to shorten it, then chose this from a set of
 * options). Deliberately short and silent on *where*:
 *
 * - **7 characters**, well under the 20 "Waiting for download" cost, so it can never crowd the
 *   row (this box's own figure column is already the widest on the page, `w-44`).
 * - Matches its sibling chip, `Settling`, in shape exactly -- one word, present tense, a state.
 *   The two are the only chips this box ever shows, and now read as one vocabulary.
 * - Says nothing about *where* the waiting is happening, and doesn't need to: the *arr brand
 *   logo sits in the same row, the box itself is called "Preflight", and the tooltip
 *   (`preflightChipTooltip` below) already spells out `Downloading from "<client>" -- reported
 *   by <instance>`. Rejected alternatives, worth keeping on record: "At client" (says where, but
 *   mild jargon), "Downloading" (the *arr's own word -- precisely the wording that caused the
 *   original confusion), "Grabbed" (reads as a past event, sitting oddly beside a present-tense
 *   `Settling`).
 *
 * `importing` (`core/arrclient.py.TRACKED_DOWNLOAD_STATE_IMPORTING`, the one other state that
 * codebase's own module docstring records as verified against a live Sonarr) keeps its own word,
 * **"Importing"** -- a release in this state has already finished arriving and is a meaningfully
 * different situation from "still waiting," so collapsing it into the same word would be
 * actively wrong, not just imprecise. Anything else falls through to `preflightStatusLabel`'s
 * existing "render the source's own wording verbatim" rule -- **deliberately not translated
 * further**, per the handoff prompt's own "do not invent a state the *arr does not report": this
 * codebase has only verified those two `trackedDownloadState` values live, so a third
 * (paused/stalled/queued at the download client, say) gets shown honestly rather than guessed at.
 */
const ARR_CHIP_LABELS: Record<string, string> = {
  downloading: 'Waiting',
  importing: 'Importing',
}

export function preflightChipLabel(row: Pick<PreflightRowOut, 'source' | 'status_label'>): string | null {
  if (row.source !== 'arr') return preflightStatusLabel(row.status_label)
  if (!row.status_label) return null
  return ARR_CHIP_LABELS[row.status_label] ?? preflightStatusLabel(row.status_label)
}

/** The chip's own hover text (2026-08-21). An *arr row's own detail and its provenance
 * (user's own words: "tooltip maybe we should show the arr details. Downloading from
 * '<download client name>' from arr") -- kept off the chip's visible text (which now speaks
 * lftpweb's own vocabulary, `preflightChipLabel` above) and onto hover instead.
 * `download_client` is read straight from the *arr's own response (`core/arrsync.py`,
 * `QueueRecord.raw["downloadClient"]`); `source_label` is already this row's own *arr instance
 * name. Falls back to naming just the instance when the *arr didn't report a download client,
 * rather than an empty quote. `null` only for a status-less *arr row -- there is nothing to
 * attribute yet.
 *
 * **A non-*arr (settle) row gets a tooltip too** (2026-08-21, follow-up: "the settling chip
 * should have a mouse over that shows time details") -- the box was asymmetric before this,
 * since only the *arr chip had one, and "Settling" alone says nothing about how much longer.
 * Rather than inventing a second countdown sentence, this reuses `lib/format.ts.
 * settleWaitLabel` **verbatim** -- the exact "Waiting for changes -- 1 of 2 scans, 35s of 60s"
 * wording the Files tree's own state text and the lifecycle R-icon tooltip already share, so
 * all three agree rather than a third copy of that sentence drifting from the other two. Fed
 * `row.wait_scans`/`wait_since` (`core/preflight.py.PreflightRow`'s own generic fields, `null`
 * for an *arr row) as the *counts*, not a pre-baked string, so the countdown recomputes live
 * from `Date.now()` on every render rather than freezing at whatever the last poll returned --
 * `settleWaitLabel` itself owns that arithmetic. `settle` is the site-wide `GET
 * /api/settings/settle` constants (`required_scans`/`min_age_s`), `null` until that one fetch
 * resolves -- `settleWaitLabel` degrades to the bare "Waiting for changes" either way, never
 * blocking the tooltip from rendering. Always non-null for a non-*arr row (`settleWaitLabel`
 * itself never returns `null`), unlike the *arr branch above.
 */
export function preflightChipTooltip(
  row: Pick<
    PreflightRowOut,
    'source' | 'status_label' | 'download_client' | 'source_label' | 'wait_scans' | 'wait_since'
  >,
  settle: SettleSettingsOut | null,
): string | null {
  if (row.source !== 'arr') {
    return settleWaitLabel(
      { settle_matched_scans: row.wait_scans, settle_first_matched_at: row.wait_since },
      settle,
    )
  }
  if (!row.status_label) return null
  return row.download_client
    ? `Downloading from "${row.download_client}" — reported by ${row.source_label}`
    : `Reported by ${row.source_label}`
}
