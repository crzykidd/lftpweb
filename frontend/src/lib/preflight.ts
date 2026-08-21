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

import type { PreflightRowOut } from '../api/types'
import { formatBytes, formatEta } from './format'

/** Collapsed row count -- "5 rows by default" is the user's own words for this box. */
export const PREFLIGHT_DEFAULT_ROWS = 5

/** The expanded view's own fixed page size. **Deliberately no per-box "Show 10/20/50" selector**
 * here, unlike the Active/Complete boxes (`lib/pagination.ts`'s own `PAGE_SIZE_OPTIONS`) -- a
 * box whose entire point is a handful of not-yet-arrived releases doesn't have the same "I want
 * to see more/fewer at once" use case a growing job history does, and a third independent size
 * preference is one more control to explain for a box that's supposed to stay out of the way.
 * Matches `ACTIVE_PAGE_SIZE`/`COMPLETE_PAGE_SIZE`'s own settled default so the expanded view
 * still reads consistently with the rest of the page.
 */
export const PREFLIGHT_EXPANDED_PAGE_SIZE = 20

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
 * that out. waiting for download that might be better"). `downloading` -- the state the user
 * actually saw -- becomes **"Waiting for download"**: lftpweb is doing nothing at all here, just
 * watching the *arr's own download client work, and the raw word reads as though lftpweb itself
 * were downloading.
 *
 * `importing` (`core/arrclient.py.TRACKED_DOWNLOAD_STATE_IMPORTING`, the one other state that
 * codebase's own module docstring records as verified against a live Sonarr) gets its own word
 * too, **"Importing"** -- a release in this state has already finished arriving and is a
 * meaningfully different situation from "still waiting on the download client," so collapsing it
 * into "Waiting for download" would be actively wrong, not just imprecise.
 *
 * Anything else falls through to `preflightStatusLabel`'s existing "render the source's own
 * wording verbatim" rule -- **deliberately not translated further**, per the handoff prompt's own
 * "do not invent a state the *arr does not report": this codebase has only verified those two
 * `trackedDownloadState` values live, so a third (paused/stalled/queued at the download client,
 * say) gets shown honestly rather than guessed at.
 */
const ARR_CHIP_LABELS: Record<string, string> = {
  downloading: 'Waiting for download',
  importing: 'Importing',
}

export function preflightChipLabel(row: Pick<PreflightRowOut, 'source' | 'status_label'>): string | null {
  if (row.source !== 'arr') return preflightStatusLabel(row.status_label)
  if (!row.status_label) return null
  return ARR_CHIP_LABELS[row.status_label] ?? preflightStatusLabel(row.status_label)
}

/** The chip's own hover text for an *arr row (2026-08-21, user's own words: "tooltip maybe we
 * should show the arr details. Downloading from '<download client name>' from arr") -- the *arr's
 * own detail and its provenance, kept off the chip's visible text (which now speaks lftpweb's own
 * vocabulary, `preflightChipLabel` above) and onto hover instead. `download_client` is read
 * straight from the *arr's own response (`core/arrsync.py`, `QueueRecord.raw["downloadClient"]`);
 * `source_label` is already this row's own *arr instance name. Falls back to naming just the
 * instance when the *arr didn't report a download client, rather than an empty quote. `null` for
 * a settle row (nothing *arr to attribute) or a status-less *arr row.
 */
export function preflightChipTooltip(
  row: Pick<PreflightRowOut, 'source' | 'status_label' | 'download_client' | 'source_label'>,
): string | null {
  if (row.source !== 'arr' || !row.status_label) return null
  return row.download_client
    ? `Downloading from "${row.download_client}" — reported by ${row.source_label}`
    : `Reported by ${row.source_label}`
}
