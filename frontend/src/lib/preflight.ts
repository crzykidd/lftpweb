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
import { formatBytes } from './format'

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
