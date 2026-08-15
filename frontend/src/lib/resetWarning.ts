// The real-numbers warning shared by all three reset-item-tracking scopes (selected items,
// whole queue, purge-by-pattern -- prompts/2026-08-13-reset-item-tracking.md). One function so
// the three confirm panels (FileTree.tsx's bulk-selected panel, and QueueResetControls.tsx's
// whole-queue/purge-by-pattern panels) can never quietly disagree about what the consequence
// of a reset actually is.
//
// The user's own instruction was specific: a generic "this may cause re-downloads" is much
// less useful than "12 of these 14 items still exist on the seedbox, and auto-queue is on for
// this queue, so they will start downloading again within 30 seconds" -- the app already knows
// the queue's sync_mode, whether auto-queue is on, and how many of the targets still have a
// remote copy, so it says the real numbers rather than hedging.

import type { SyncMode } from '../api/types'

export interface ResetQueueContext {
  syncMode: SyncMode
  autoQueueEnabled: boolean
  /** `path_queue.scan_interval_s` -- `null` means "site default" (30s), `0` means on-demand
   * only (no timer). Read straight off `PathQueueOut`, never guessed, so the "within Ns"
   * phrasing is never a lie for a queue running a non-default interval.
   */
  scanIntervalS: number | null
}

const SITE_DEFAULT_SCAN_INTERVAL_S = 30

function nextScanPhrase(scanIntervalS: number | null): string {
  if (scanIntervalS === 0) return 'the next time this queue is scanned (on-demand only, no timer)'
  const seconds = scanIntervalS ?? SITE_DEFAULT_SCAN_INTERVAL_S
  return `within about ${seconds}s (this queue's scan interval)`
}

/** The consequence line(s) -- computed from the real counts, never a hedge. Returns one or two
 * sentences depending on whether anything is actually still fetchable; always followed by the
 * two facts that are true regardless of counts (`alwaysTrueResetLines` below).
 */
function reDownloadLine(total: number, remoteCount: number, ctx: ResetQueueContext): string {
  const plural = total !== 1
  if (remoteCount === 0) {
    return plural
      ? `None of these ${total} items still exist on the seedbox, so nothing will be re-downloaded.`
      : `This item no longer exists on the seedbox, so it will not be re-downloaded.`
  }

  const subject =
    remoteCount === total
      ? plural
        ? `All ${total} of these items still exist`
        : `This item still exists`
      : `${remoteCount} of these ${total} items still exist`
  const pronoun = remoteCount === 1 ? 'it' : 'them'
  const modeNote =
    ctx.syncMode === 'move'
      ? ' (a move queue -- most completed items already had their remote copy removed; these have not)'
      : ''

  if (ctx.autoQueueEnabled) {
    return (
      `${subject} on the seedbox${modeNote}, and auto-queue is on for this queue, so ` +
      `${remoteCount === 1 ? 'it' : 'they'} will start downloading again ` +
      `${nextScanPhrase(ctx.scanIntervalS)}.`
    )
  }
  return (
    `${subject} on the seedbox${modeNote}. Auto-queue is off for this queue, so nothing ` +
    `re-downloads automatically -- but queueing ${pronoun} manually, or turning auto-queue on, ` +
    `will fetch ${pronoun} again.`
  )
}

/** The two facts that are true of every reset regardless of counts -- stated plainly rather
 * than left for the user to assume, per the task's own instruction: people will assume local
 * files are deleted given where this button lives, and the job/event cascade
 * (`job.item_id ON DELETE CASCADE`) is a real, unavoidable consequence that deserves a warning
 * rather than a surprise.
 */
const ALWAYS_TRUE_RESET_LINES = [
  'Local files are not deleted — this only resets tracking, not your data.',
  'Transfer history for these items goes too: their job records are deleted outright, and any ' +
    'audit-log entries about them stay in History but lose the link back to them.',
]

/** The full warning, in display order: the real-numbers consequence first (the thing most
 * likely to surprise someone), then the two always-true facts.
 *
 * `total === 0` (2026-08-14, prompts/2026-08-14-reset-panel-counts-and-layout.md) is its own
 * branch, not a fall-through into `reDownloadLine`'s own `remoteCount === 0` case -- that case
 * already reads sensibly ("None of these 3 items still exist…"), but at `total === 0` there are
 * no items for the two always-true lines to be true *of*, and the old bare-count panel this
 * function feeds rendered the nonsensical "— 0 items" / "None of these 0 items still exist on
 * the seedbox, so nothing will be re-downloaded." A single plain line, with neither always-true
 * line following it, is what actually describes "nothing matched."
 */
export function resetWarningLines(
  total: number,
  remoteCount: number,
  ctx: ResetQueueContext,
): string[] {
  if (total === 0) {
    return ['Nothing matches this scope, so there is nothing to reset.']
  }
  return [reDownloadLine(total, remoteCount, ctx), ...ALWAYS_TRUE_RESET_LINES]
}
