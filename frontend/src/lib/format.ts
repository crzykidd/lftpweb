const UNITS = ['B', 'KB', 'MB', 'GB', 'TB']

export function formatBytes(bytes: number): string {
  if (bytes <= 0) return '0 B'
  const exp = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), UNITS.length - 1)
  const value = bytes / 1024 ** exp
  return `${value.toFixed(exp === 0 ? 0 : 1)} ${UNITS[exp]}`
}

export function formatRate(bytesPerSecond: number): string {
  return `${formatBytes(bytesPerSecond)}/s`
}

// The Files page's Speed column (2026-08-14, prompts/2026-08-14-files-page-speed-column.md):
// `FileTree.tsx`'s `TreeEntry.speed_bps` is the live, EMA-smoothed instantaneous rate from
// `core/progress.py`, threaded in from the `progress` WS message
// (`core/queue.py._sample_and_publish_progress`) keyed by `item_id` -- never a derived average.
// The task's own brief spells out why a derived average was rejected here: dividing the row's
// cumulative `local_size` by time-since-`state_changed_at` produces a phantom rate on a resumed
// transfer (18 GB already on disk, state changed 2 minutes ago reads as ~150 MB/s, a number
// nothing ever achieved) -- the same non-monotonicity trap `core/metrics.py`'s own docstring
// documents for `bytes_done` vs. `bytes_start`. So this column shows the live rate or nothing,
// never a computed one.
//
// Both functions gate on `state === 'DOWNLOADING'`, not on whether `speedBps` happens to be
// present -- the `progress` WS message is never pruned client-side once a job finishes
// (`useLiveModel.ts`'s `speedByItemId` keeps the last value forever, same as `progressByJobId`
// always has), so a stale reading from a completed transfer would otherwise linger. `state`
// leaving `DOWNLOADING` (`core/queue.py` line ~304/1491: only the currently-running item's job
// holds that state) is the one signal that's actually live, so it's what gates display -- not a
// zero/non-zero check on the value itself. A real `0 B/s` while `DOWNLOADING` (a stalled but
// still-running transfer) is an honest reading and is shown as such: "a zero rate and 'not
// transferring' are different statements" (the task's own bar).

/** The Speed column's in-cell text. `null`/dash for anything not currently downloading --
 * never `0 B/s` for a row that simply isn't transferring (see the module comment above).
 */
export function transferSpeedLabel(state: string, speedBps: number | null | undefined): string {
  if (state !== 'DOWNLOADING' || speedBps == null || !Number.isFinite(speedBps)) return '—'
  return formatRate(Math.max(speedBps, 0))
}

/** The Speed column's sort value -- `null` for anything not currently downloading, so
 * `compareValues`' existing null-last rule (`FileTree.tsx`) puts every non-transferring row at
 * one end regardless of direction, rather than interleaving them by a coincidental zero. A
 * transferring row with a genuine `0 B/s` reading still sorts as `0`, not `null` -- it's a real
 * measurement, not an absence of one.
 */
export function transferSpeedSortValue(state: string, speedBps: number | null | undefined): number | null {
  if (state !== 'DOWNLOADING' || speedBps == null || !Number.isFinite(speedBps)) return null
  return speedBps
}

// A **child** file inside a mirroring directory (2026-08-14, "per-file speed inside a mirror")
// cannot use the two functions above: `core/reconcile.py`'s leaf rule puts an
// actively-transferring child at `PARTIAL`, never `DOWNLOADING` -- gating on `state ===
// 'DOWNLOADING'` the same way the parent row does would hide exactly the rows this exists to
// show. There is also no per-child `state` transition to gate staleness on the way there is at
// the job level (a job's `state` genuinely leaves `DOWNLOADING`; a child's `PARTIAL` doesn't
// change just because the transfer stopped). So gating here is **freshness**, not `state`: the
// caller (`FileTree.tsx`'s `buildTree`) already resolves `null` for any sample older than
// `CHILD_SPEED_FRESHNESS_MS`, so by the time a value reaches either function below, "present"
// already means "should render" -- see docs/decisions.md for the two gating options considered
// and why freshness (closed by construction: the backend simply stops emitting a sample for a
// child that stopped changing) was chosen over threading job-liveness through the tree.

/** The Speed column's in-cell text for a child row -- `speedBps` must already be
 * freshness-filtered by the caller (`null` for a stale/absent sample); see the module comment
 * above.
 */
export function childSpeedLabel(speedBps: number | null | undefined): string {
  if (speedBps == null || !Number.isFinite(speedBps)) return '—'
  return formatRate(Math.max(speedBps, 0))
}

/** The Speed column's sort value for a child row -- same freshness contract as
 * `childSpeedLabel`, and the same null-last sort behavior `transferSpeedSortValue` documents.
 */
export function childSpeedSortValue(speedBps: number | null | undefined): number | null {
  if (speedBps == null || !Number.isFinite(speedBps)) return null
  return speedBps
}

/** `eta_s` from `core/progress.py` -- `null` when speed is 0 or the total is unknown. */
export function formatEta(etaSeconds: number | null | undefined): string {
  if (etaSeconds == null || !Number.isFinite(etaSeconds)) return '—'
  const total = Math.max(Math.round(etaSeconds), 0)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m`
  return `${s}s`
}

// A Files-row ETA (2026-08-14, "ETA on Files rows") -- the top-level item's own `eta_s` is
// already computed server-side (`core/progress.py.ProgressSampler.sample`) and threaded onto
// the wire on the same `progress` message `speed_bps` already rides (`wsTypes.ts.ProgressJob.
// eta_s`); nothing new is computed for it here, only formatted through the same `formatEta`
// above the Transfers page already uses ("ETA 3m"). A **child** file inside a mirroring
// directory has no server-computed ETA of its own -- `_publish_child_progress` only ever emits
// a rate (`ChildProgressItem.speed_bps`), never an ETA -- so `childEtaS` below derives one
// client-side, the same way the task's own brief spelled out. Both read through the identical
// `formatEta`, and `FileTree.tsx`'s `effectiveEtaLabel` shows both the same way a row's Speed
// cell already picks between job-level and child-level rates -- no second duration vocabulary,
// no second gating mechanism.

/** The Files tree row's job-level ETA text -- the same gate `transferSpeedLabel` already applies
 * to `speed_bps` (`state === 'DOWNLOADING'`), so a stale `eta_s` from a finished or never-started
 * job never lingers (this value, like `speedByItemId`, is never pruned client-side; see
 * `useLiveModel.ts`). `etaS` is already `null` from the backend whenever `bytes_total` is unknown
 * or speed is 0 (`JobProgress.eta_s`'s own docstring) -- nothing further to guard here beyond
 * that and the usual `Number.isFinite` defense against a malformed payload.
 */
export function transferEtaLabel(state: string, etaS: number | null | undefined): string {
  if (state !== 'DOWNLOADING' || etaS == null || !Number.isFinite(etaS)) return '—'
  return formatEta(etaS)
}

/** A child file's own ETA (2026-08-14, "ETA on Files rows") -- `remote_size - local_size` (both
 * persisted; the exact pair `_publish_child_progress`'s own DOWNLOADED/PARTIAL leaf rule already
 * compares), divided by that child's freshness-gated smoothed rate (`child_speed_bps`, resolved
 * by `FileTree.tsx`'s `buildTree` the same way `childSpeedLabel` already consumes it -- a stale
 * or absent sample is already `null` by the time it reaches here, so no separate freshness check
 * belongs in this function). "Show nothing rather than a wrong number" (this task's own bar) --
 * every degenerate case returns `null`, never `Infinity`, `NaN`, or a negative reading:
 *  - `remoteSize`/`localSize` null: no denominator -- the same "unknown vs. 0 is not this path's
 *    call" rule the Size column already follows (`nodeDisplaySize`, `FileTree.tsx`).
 *  - `speedBps` null, zero, or non-finite: no fresh sample, or a genuinely stalled rate -- never
 *    divide by zero into `Infinity`.
 *  - `remaining <= 0`: local already meets or exceeds remote -- the file is done, which is a
 *    different fact from "0 seconds left."
 * Deliberately uncapped on the high end: a very small rate produces a very large (but honest)
 * reading rather than a fabricated "> 1h" ceiling -- see docs/decisions.md for why honest won
 * over capped here.
 */
export function childEtaS(
  remoteSize: number | null,
  localSize: number | null,
  speedBps: number | null,
): number | null {
  if (remoteSize == null || localSize == null) return null
  if (speedBps == null || !Number.isFinite(speedBps) || speedBps <= 0) return null
  const remaining = remoteSize - localSize
  if (remaining <= 0) return null
  return remaining / speedBps
}

/** The one place `done`/`total` becomes a percentage (0-100, clamped, or `null` when it
 * wouldn't mean anything -- no total, a non-positive total, or no `done` reading yet). Guards
 * the exact inputs that would otherwise produce `NaN` or a negative width: `total <= 0`
 * (division by zero or a nonsensical negative denominator) and `done == null`/`total == null`
 * (nothing to compare). `formatPercent` and `FileTree.tsx`'s inline progress bar both build on
 * this rather than each rolling their own -- "reuse formatPercent rather than writing a second
 * percentage" (prompts/2026-08-13-lifecycle-icons.md), factored so the bar's numeric fraction
 * and the text's rounded label can never disagree.
 */
export function percentValue(done: number | null, total: number | null): number | null {
  if (done == null || total == null || total <= 0) return null
  return Math.min(Math.round((done / total) * 100), 100)
}

export function formatPercent(done: number | null, total: number | null): string {
  const value = percentValue(done, total)
  return value == null ? '—' : `${value}%`
}

// Both-sides panels (2026-08-13, prompts/2026-08-13-both-sides-hover-card.md): `ItemDrawer.tsx`'s
// `SideBySideDetails` grid and `FileTree.tsx`'s row hover card both need "size / modified,
// remote vs. local" as label/value triples for the same item. This project has been bitten
// three separate times by exactly this shape of duplication (column widths declared twice in
// `FileTree.tsx` before `2026-08-13-resizable-file-columns.md`'s fix, an item projection
// hand-copied into four publishers, `_LOCAL_CONTENT_ASSERTED_STATES` forked from
// `mount_sentinel.COMPLETE_STATES`) -- a tooltip and a drawer independently formatting the same
// numbers would disagree eventually. One function, shared by both.

interface BothSidesFields {
  is_dir: boolean
  remote_size: number | null
  local_size: number | null
  remote_mtime: number | null
  local_mtime: number | null
}

export interface BothSidesRow {
  label: string
  remote: string
  local: string
}

function formatMtimeSide(epochSeconds: number | null): string {
  return epochSeconds != null ? new Date(epochSeconds * 1000).toLocaleString() : '—'
}

/** Label/remote/local triples: Size always, Modified only for a file. **A directory gets no
 * Modified row at all** -- not one reading `—` -- because `remote_mtime`/`local_mtime` are
 * files-only by deliberate convention (`core/reconcile.py`; `de85753`'s decision entry weighed
 * and rejected both a directory-inode mtime and a recursive newest-child rollup as answers to a
 * question the byte-comparison model doesn't ask). A directory never had a Modified value to
 * report, which is a different fact from "unknown," so the row itself is absent rather than
 * blank.
 */
export function bothSidesRows(entry: BothSidesFields): BothSidesRow[] {
  const rows: BothSidesRow[] = [
    {
      label: 'Size',
      remote: entry.remote_size != null ? formatBytes(entry.remote_size) : '—',
      local: entry.local_size != null ? formatBytes(entry.local_size) : '—',
    },
  ]
  if (!entry.is_dir) {
    rows.push({
      label: 'Modified',
      remote: formatMtimeSide(entry.remote_mtime),
      local: formatMtimeSide(entry.local_mtime),
    })
  }
  return rows
}

/** Whether both sides have anything to show at all -- `remote_size`/`local_size` null means that
 * side was never tracked (no remote copy at all, or nothing downloaded yet), not merely an
 * unknown reading inside an otherwise-populated row. Drives whether `FileTree.tsx`'s hover card
 * renders two columns or degrades to one labelled column -- a two-column layout with an empty
 * half reads worse than a single column, per that task's own bar. `ItemDrawer.tsx`'s own grid
 * keeps rendering two columns unconditionally regardless of this (unchanged): a drawer has the
 * room for an explicit `—`, a small hover card does not.
 */
export function hasBothSides(entry: { remote_size: number | null; local_size: number | null }): boolean {
  return entry.remote_size != null && entry.local_size != null
}

// DESIGN.md §4.5/§9.3's Settings -> Transfer fields are stored on the wire as bytes(/s) --
// nobody should have to type `10000000`. Decimal MB (1,000,000 B), not binary MiB: it's what
// makes `core/queue.py.TransferSettings`'s own round defaults (10_000_000 bps, 1_000_000 B
// floors) come back as clean numbers ("10", not "9.5367...") rather than an arbitrary choice.
const MB_BYTES = 1_000_000

export function bytesToMB(bytes: number): number {
  return bytes / MB_BYTES
}

export function mbToBytes(mb: number): number {
  return Math.round(mb * MB_BYTES)
}

/** "scanned 12s ago" style reading for a `scan_complete`/snapshot timestamp (Files page,
 * DESIGN.md §9.2). Deliberately recomputed only on render, never on a ticking timer -- the
 * Files page is WebSocket-driven (DESIGN.md §9), and a client-side interval here would be
 * exactly the kind of client-side refresh loop that page is built to avoid; each queue
 * already re-renders at least every `scan_interval_s` (default 30s) as its own `scan_complete`
 * arrives, which is fresh enough for a relative reading measured in seconds-to-minutes.
 */
export function formatRelativeTime(iso: string): string {
  const deltaMs = Date.now() - new Date(iso).getTime()
  const deltaS = Math.max(Math.round(deltaMs / 1000), 0)
  if (deltaS < 5) return 'just now'
  if (deltaS < 60) return `${deltaS}s ago`
  const deltaM = Math.round(deltaS / 60)
  if (deltaM < 60) return `${deltaM}m ago`
  const deltaH = Math.round(deltaM / 60)
  if (deltaH < 24) return `${deltaH}h ago`
  const deltaD = Math.round(deltaH / 24)
  return `${deltaD}d ago`
}

// migration 006's `item.state_changed_at` -- the Files tree's per-row "when did this last
// move" reading (DESIGN.md §9.2). Unlike `formatRelativeTime` above, this one is built on
// `Intl.RelativeTimeFormat` (no new dependency -- this project has deliberately avoided
// adding frontend ones, docs/decisions.md) rather than a hand-rolled bucket chain, because the
// Files tree can hold thousands of rows and needs the day/hour/minute/second boundaries
// (including "yesterday"/"now") got right in one place instead of re-derived per caller.
const RELATIVE_UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ['day', 86400],
  ['hour', 3600],
  ['minute', 60],
]
const relativeTimeFormatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto', style: 'narrow' })

/** "3m ago" / "yesterday" style reading of an ISO timestamp, always relative to `Date.now()`
 * at call time -- this function owns no timer of its own. `FileTree.tsx` drives freshness with
 * one shared ticker per tree (a bumped counter that forces a re-render), never a per-row
 * `setInterval`, which is the expensive mistake to avoid at the row counts this page can hit.
 */
export function formatRelativeTimeIntl(iso: string): string {
  const elapsedS = Math.max(Math.round((Date.now() - new Date(iso).getTime()) / 1000), 0)
  for (const [unit, secondsPerUnit] of RELATIVE_UNITS) {
    if (elapsedS >= secondsPerUnit) {
      return relativeTimeFormatter.format(-Math.round(elapsedS / secondsPerUnit), unit)
    }
  }
  return relativeTimeFormatter.format(-elapsedS, 'second')
}

// A verb-phrase reading of each §3.2 state for the "Downloaded 3m ago" / "Remote 2h ago"
// column -- distinct from `StateChip.tsx`'s STYLES map (which colors the state's own name
// verbatim) because a chip and a sentence want different words for the same value.
const STATE_AGE_LABELS: Record<string, string> = {
  REMOTE_ONLY: 'Remote',
  QUEUED: 'Queued',
  DOWNLOADING: 'Downloading',
  PARTIAL: 'Partial',
  STOPPED: 'Stopped',
  DOWNLOADED: 'Downloaded',
  EXCLUDED: 'Excluded',
  VERIFYING: 'Verifying',
  VERIFIED: 'Verified',
  CORRUPT: 'Corrupt',
  EXTRACTING: 'Extracting',
  EXTRACTED: 'Extracted',
  EXTRACT_FAILED: 'Extract failed',
  FAILED: 'Failed',
  LOCAL_ONLY: 'Local',
  // 'Removed locally' (2026-08-13, prompts/2026-08-13-resizable-file-columns.md audit)
  // combined with a relative-time reading ("Removed locally yesterday") ran past this
  // column's width more often than any other label here -- shortened to 'Deleted', still
  // distinct from REMOVED_BOTH's 'Removed' below (this row's remote copy survives; that one's
  // doesn't). The fuller distinction stays available regardless: the Status chip in the
  // neighboring column always shows the raw state, `REMOVED_LOCAL`/`REMOVED_BOTH` verbatim,
  // uncut -- this column is a second, shorter reading of the same fact, not the only one.
  REMOVED_LOCAL: 'Deleted',
  REMOVED_BOTH: 'Removed',
}

/** "Downloaded 3m ago" / "Remote 2h ago" -- the label a Files row's age column actually shows.
 * `stateChangedAt` is `null` when migration 006's backfill genuinely couldn't date a
 * pre-existing row (DESIGN.md §9.2); shown as the bare state label rather than something
 * fabricated.
 */
export function stateAgeLabel(state: string, stateChangedAt: string | null): string {
  const label = STATE_AGE_LABELS[state] ?? state
  if (stateChangedAt == null) return label
  return `${label} ${formatRelativeTimeIntl(stateChangedAt)}`
}

// The settle gate's wait, spelled out (2026-08-13, prompts/2026-08-13-files-ux-pass.md item 3)
// -- replaces the previous 6px dot (`h-1.5 w-1.5`, effectively invisible in `FileTree.tsx`)
// with a readable countdown, "Waiting for changes -- 1 of 2 scans, 35s of 60s". Shared between
// `FileTree.tsx`'s Status-column chip label (which substitutes it wholesale for the normal
// state text while a top-level item is settling) and `LifecycleIcons.tsx`'s R-icon tooltip --
// one function, so the two can never disagree about what "waiting" means for the same row. The
// node/settings shapes it needs (`settle_matched_scans`/`_first_matched_at`, `required_scans`/
// `min_age_s`) are structural enough not to warrant importing `api/types.ts`'s named types here
// -- a minimal inline shape keeps this file free of a dependency on the wire-shape module.
interface SettleProgressNode {
  settle_matched_scans: number | null
  settle_first_matched_at: string | null
}
interface SettleConstants {
  required_scans: number
  min_age_s: number
}

/** `settle` is `null` before `getSettleSettings()`'s one site-wide fetch resolves
 * (`FileTree.tsx` fetches it once, not per row) or if it failed; `node.settle_matched_scans`/
 * `_first_matched_at` are `null` whenever `core/itemview.py.item_view` didn't have both a join
 * match and `substate === 'settling'` for this row. Either way this degrades to the bare label
 * rather than showing a stale or fabricated count -- never blocks the row from rendering.
 */
export function settleWaitLabel(node: SettleProgressNode, settle: SettleConstants | null): string {
  if (settle == null || node.settle_matched_scans == null || node.settle_first_matched_at == null) {
    return 'Waiting for changes'
  }
  const elapsedS = Math.max(
    0,
    Math.floor((Date.now() - new Date(node.settle_first_matched_at).getTime()) / 1000),
  )
  return (
    `Waiting for changes -- ${node.settle_matched_scans} of ${settle.required_scans} scans, ` +
    `${elapsedS}s of ${Math.round(settle.min_age_s)}s`
  )
}

/** The Status chip's own in-cell text (2026-08-13, prompts/2026-08-13-resizable-file-
 * columns.md) -- `settleWaitLabel` above is the complete sentence, right for a hover tooltip,
 * but it was being shown verbatim *in the chip itself*, inside a column a few characters wide,
 * where it just ran off the edge. This is the short form the chip actually renders; the full
 * sentence survives on hover (`FileTree.tsx`'s `Row` passes `settleWaitLabel`'s result as the
 * chip's own `title`), so nothing is lost, just not force-fit into the cell. Keeps a verb
 * ("Waiting") rather than a bare `1/2 · 35s` -- a number pair with no verb reads as data, not
 * status, which was the one thing to avoid per the task's own bar.
 */
export function settleWaitShortLabel(node: SettleProgressNode, settle: SettleConstants | null): string {
  if (settle == null || node.settle_matched_scans == null || node.settle_first_matched_at == null) {
    return 'Waiting…'
  }
  const elapsedS = Math.max(
    0,
    Math.floor((Date.now() - new Date(node.settle_first_matched_at).getTime()) / 1000),
  )
  return `Waiting ${node.settle_matched_scans}/${settle.required_scans} · ${elapsedS}s`
}

// "Still arriving" -- the settle gate's other display state (2026-08-13,
// prompts/2026-08-13-settle-progress-visibility.md). User report: copying a large directory
// straight onto the seedbox, the countdown above sat at "1 of 2" for the whole copy and
// conveyed nothing -- every scan found the fingerprint still growing, which resets
// `settle_matched_scans` right back to 1 every time (`core/settle.py.advance_settle`'s counter
// arithmetic is unchanged by this task -- see `docs/decisions.md` for why an earlier version of
// this task tried starting it at 0 instead for this exact case and reverted: it silently added
// a whole extra required scan to real settle timing, which is the growing-denominator problem
// this task's own brief already ruled out, just relocated into the numerator).
// `settle_matched_scans === 1` covers both a genuinely first-ever sighting and a fingerprint
// that just changed from a previous one -- deliberately not split further: in *both* cases
// nothing has been confirmed unchanged even once yet, so the ordinary "Waiting n of 2 scans"
// countdown below has nothing meaningful to count. This is a different sentence for that
// shared case, not a different phase of the same one: the byte count climbing
// (`settle_total_bytes`, `item_settle.total_bytes`, already computed as part of the
// fingerprint) *is* the progress signal while it applies, and `settle_last_changed_at`
// (migration 013) says when it last actually moved -- "changed just now" for a first-ever
// sighting (`last_changed_at` is set to the same instant as `first_observed_at` in that case),
// a real elapsed reading for an item that has changed since. `isStillArriving` is the one place
// that draws the line between the two displays, so `FileTree.tsx`'s `Row` never has to
// duplicate the threshold.
export function isStillArriving(node: { settle_matched_scans: number | null }): boolean {
  return node.settle_matched_scans === 1
}

interface SettleArrivingNode {
  settle_total_bytes: number | null
  settle_first_observed_at: string | null
  settle_last_changed_at: string | null
}

/** The full sentence, for the chip's `title` (hover) -- `settleWaitLabel`'s counterpart for
 * this state. `settle_first_observed_at`/`settle_last_changed_at` are `null` on a row whose
 * `item_settle` record predates migration 013 and hasn't changed again since
 * (`core/settle.py.SettleRecord`'s own docstring) -- degrades by omitting that clause rather
 * than fabricating a time, the same "never blocks the row from rendering" rule
 * `settleWaitLabel` already follows.
 */
export function settleArrivingLabel(node: SettleArrivingNode): string {
  const size = node.settle_total_bytes != null ? formatBytes(node.settle_total_bytes) : 'an unknown size so far'
  const changed =
    node.settle_last_changed_at != null
      ? `, changed ${formatRelativeTimeIntl(node.settle_last_changed_at)}`
      : ''
  const watchedS =
    node.settle_first_observed_at != null
      ? Math.max(0, Math.floor((Date.now() - new Date(node.settle_first_observed_at).getTime()) / 1000))
      : null
  const watched = watchedS != null ? ` -- watching for ${formatEta(watchedS)}` : ''
  return `Still arriving on the remote -- ${size}${changed}${watched}`
}

/** The Status chip's own in-cell text for this state -- `settleWaitShortLabel`'s counterpart,
 * kept to the same short shape ("Waiting 1/2 · 35s") since it sits in the same fixed-width,
 * already-once-trimmed column (`a4a626d`). The full sentence survives on hover via
 * `settleArrivingLabel` above.
 *
 * **Reads "Remote", not "Arriving" (2026-08-14, user request.)** "Arriving" was ambiguous in
 * exactly the wrong direction: it sounds like the item is arriving *here*, i.e. downloading to
 * the local side, when the whole point of this state is that nothing has been queued yet and the
 * bytes are still landing on the *seedbox*. Naming the side removes the ambiguity.
 *
 * This deliberately shares its leading word with a plain (non-settling) `REMOTE_ONLY` chip,
 * which reads just "Remote" (`STATE_LABELS` above) -- correctly, since both mean "on the remote,
 * not here." The two stay distinguishable by **color**, not by wording: a settling row renders
 * the synthetic amber `SETTLING` chip (`components/StateChip.tsx`), a settled remote-only row the
 * sky-blue `REMOTE_ONLY` one. The climbing byte count and the trailing ellipsis carry the "still
 * growing" reading on top of that. The function keeps its `arriving` name because the *concept*
 * it selects for is unchanged -- only the user-facing word moved.
 */
export function settleArrivingShortLabel(node: { settle_total_bytes: number | null }): string {
  return node.settle_total_bytes != null ? `Remote · ${formatBytes(node.settle_total_bytes)}` : 'Remote…'
}

// The removal grace period's countdown (2026-08-14, prompts/2026-08-14-removal-grace-
// countdown.md, DESIGN.md §3.2 rule 3 / §7.3) -- mirrors the settle-gate countdown above
// exactly: same substitution shape (`FileTree.tsx`'s `Row` swaps the state chip wholesale,
// same as `isSettling`/SETTLING), same short-label-in-cell / full-sentence-on-hover split.
//
// The live case this closes: a `move`-mode release whose local copy had just been moved out
// sat at `VERIFIED` -- both presence icons dim, no size, 22 children already at
// `REMOVED_BOTH` -- for the whole ~10-minute grace window with nothing on screen saying a
// clock was running. `DESIGN.md` §3.2 rule 3 was working correctly; the row just looked
// broken. **The lifecycle icons (`core/itemview.py`'s R/L/V/E facets) are untouched by this
// -- V staying green while L goes dim is correct, the presence/milestone split this project
// already made deliberately (docs/decisions.md).** The fix belongs on the state chip, the one
// field that already carries a transient reading (see SETTLING above).

// `core/mount_sentinel.py._COMPLETE_PREV_STATES` ({"DOWNLOADED"} | core/postprocess.py.
// OWNED_STATES), deliberately duplicated here rather than imported -- same cross-language
// **A bootstrap default, not a second source of truth.** The real set ships from
// `GET /api/settings/removal-grace` as `eligible_states`, straight out of
// `core/mount_sentinel.py.COMPLETE_STATES` -- the same set `resolve_absence` actually gates the
// grace clock on -- and `tests/test_settings_api.py` pins that equality against the live Python
// set. `FileTree.tsx` passes the fetched set to `isRemovalGracePending`; this constant is only
// what the one render before that fetch resolves uses, and what the pure-function tests below
// exercise. So a state added to `_COMPLETE_PREV_STATES` on the Python side self-corrects here
// within one fetch rather than needing a matching TypeScript edit -- which is the drift this
// project has been bitten by repeatedly (a projection hand-copied into four publishers, column
// widths declared twice, `_LOCAL_CONTENT_ASSERTED_STATES` forked from this very set).
export const REMOVAL_GRACE_ELIGIBLE_STATES = new Set([
  'DOWNLOADED',
  'VERIFYING',
  'VERIFIED',
  'CORRUPT',
  'EXTRACTING',
  'EXTRACTED',
  'EXTRACT_FAILED',
])

/** The grace-countdown substitution trigger, `isSettling`'s counterpart in `FileTree.tsx`'s
 * `Row`. `first_missing_at != null` is `core/mount_sentinel.py.resolve_absence`'s own signal
 * that the grace clock is running for this row *right now* -- a row already rewritten to
 * `REMOVED_LOCAL`/`REMOVED_BOTH` is finished (not in `REMOVAL_GRACE_ELIGIBLE_STATES`, so this
 * is `false` for it regardless of what `first_missing_at` still holds), and a row whose local
 * copy never went missing has `first_missing_at === null`.
 */
export function isRemovalGracePending(
  node: { state: string; first_missing_at: string | null },
  eligibleStates: ReadonlySet<string> = REMOVAL_GRACE_ELIGIBLE_STATES,
): boolean {
  return eligibleStates.has(node.state) && node.first_missing_at != null
}

export interface RemovalGraceConstants {
  grace_s: number
}

/** Seconds left before the grace window elapses, or `null` whenever showing a number would be
 * a guess or a lie rather than a fact -- callers render the bare `Missing` label for `null`,
 * never a fabricated or stuck figure (recommendation from
 * prompts/2026-08-14-removal-grace-countdown.md, recorded in docs/decisions.md):
 *
 * - `firstMissingAt`/`graceS` not yet available (settings still loading, or the row somehow
 *   has no timestamp) -- nothing to compute from.
 * - `firstMissingAt` doesn't parse as a date -- guard against `NaN` arithmetic.
 * - `firstMissingAt` in the future -- clock skew between browser and server; a negative
 *   countdown is worse than none.
 * - elapsed already `>= graceS` -- **both** of the two honest-but-different reasons this can
 *   happen collapse to the same cap on purpose: the backend's own scan hasn't rewritten the
 *   row to `REMOVED_LOCAL` yet (ordinary polling lag), *or* `core/mount_sentinel.py.
 *   resolve_absence` has frozen the clock because `mount_ok` is false for this queue (DESIGN.md
 *   §7.3: "never start the grace clock on a reading we can't trust"). The Files page's
 *   WebSocket-driven tree (`FileTree.tsx`) has no per-row visibility into `mount_ok` today --
 *   it travels only on `GET /api/files`'s per-queue `QueueFiles.mount_ok`, never on the
 *   `snapshot`/`queue_delta`/`item_delta` messages the tree actually renders from
 *   (`api/wsTypes.ts`) -- so a countdown ticking to zero while the backend never transitions
 *   the row is a real possibility this function can't tell apart from ordinary lag without new
 *   backend plumbing. Capping at `Missing` (no number) the moment elapsed reaches `graceS`
 *   avoids the worse failure (a countdown stuck at `0s`, or negative) without claiming
 *   precision this function doesn't have. See docs/decisions.md for the plumbing that would
 *   remove this limitation and why it wasn't added here.
 */
export function removalGraceRemainingS(
  firstMissingAt: string | null,
  graceS: number | null,
  now: number = Date.now(),
): number | null {
  if (firstMissingAt == null || graceS == null) return null
  const missingMs = new Date(firstMissingAt).getTime()
  if (!Number.isFinite(missingMs)) return null
  const elapsedS = (now - missingMs) / 1000
  if (elapsedS < 0 || elapsedS >= graceS) return null
  return graceS - elapsedS
}

/** The Status chip's own in-cell text -- `settleWaitShortLabel`'s counterpart. `"Missing"`
 * alone (no bullet, no number) whenever `removalGraceRemainingS` has nothing trustworthy to
 * show; `"Missing · 1m"` once it does. Deliberately not `"Removed"` or `"Removing"` -- the
 * item hasn't been removed yet (that's `REMOVED_LOCAL`, a real, different `state`), and it
 * isn't this codebase doing the removing (that's `REMOVING`, `substate === 'removing'`) --
 * `"Missing"` names what's actually different about this row: presence, not action.
 *
 * **`arr_status === 'cleaned'` reads the identical clock as `"Processed"` instead**
 * (docs/arr-integration-spec.md "Cleanup": a Sonarr/Radarr-driven local cleanup rides the exact
 * same removal-grace machinery as any other absence -- `core/arrsync.py`'s cleanup step
 * deliberately never writes `item.state`, only removes bytes, per that module's own docstring
 * -- but the absence here is deliberate and audited, not an alarm, so the word changes while
 * the countdown itself does not. Same clock, different words; no new timer.
 */
export function removalGraceShortLabel(
  node: { first_missing_at: string | null; arr_status?: string | null },
  constants: RemovalGraceConstants | null,
): string {
  const remainingS = removalGraceRemainingS(node.first_missing_at, constants?.grace_s ?? null)
  const label = node.arr_status === 'cleaned' ? 'Processed' : 'Missing'
  return remainingS == null ? label : `${label} · ${formatEta(remainingS)}`
}

const GRACE_TIME_FORMAT: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit' }

/** The full sentence, for the chip's `title` (hover) and the item drawer -- e.g. "Local copy
 * gone since 17:35. Treated as removed in 1m unless it comes back." Degrades the second
 * clause to "soon" (never a stuck/negative number) under exactly the conditions
 * `removalGraceRemainingS` itself returns `null` for, including the frozen-clock case -- see
 * that function's own docstring.
 */
export function removalGraceLabel(
  node: { first_missing_at: string | null; arr_status?: string | null },
  constants: RemovalGraceConstants | null,
): string {
  const cleaned = node.arr_status === 'cleaned'
  if (node.first_missing_at == null) {
    return cleaned ? 'Processed by the *arr; local copy removed.' : 'Local copy missing.'
  }
  const missingDate = new Date(node.first_missing_at)
  const sinceText = Number.isFinite(missingDate.getTime())
    ? missingDate.toLocaleTimeString([], GRACE_TIME_FORMAT)
    : 'an unknown time'
  const remainingS = removalGraceRemainingS(node.first_missing_at, constants?.grace_s ?? null)
  const outcome = cleaned
    ? remainingS != null
      ? `Leaves this view in ${formatEta(remainingS)}.`
      : 'Leaves this view soon.'
    : remainingS != null
      ? `Treated as removed in ${formatEta(remainingS)} unless it comes back.`
      : 'Treated as removed soon unless it comes back.'
  const opening = cleaned
    ? `Processed by the *arr and cleaned up locally since ${sinceText}.`
    : `Local copy gone since ${sinceText}.`
  return `${opening} ${outcome}`
}

// A spent archive volume, resting (2026-08-14, prompts/2026-08-14-extracted-archives-rest-as-
// extracted.md, DESIGN.md §3.2 rule 8 / §6, §7.3). `core/local_delete.py.
// delete_extracted_archives` removes a release's `.rar`/`.r00`/... volumes once extraction has
// succeeded -- on purpose, the successful conclusion of the thing that just worked -- and
// `core/engine.py._persist`'s vanished-row sweep resolves that row straight to `EXCLUDED`
// (never through §7.3's grace clock) on *both* sync modes, so a `copy` queue (remote volume
// survives) and a `move` queue (remote already gone) read identically. `EXCLUDED` is truthful
// (DESIGN.md §3.2 rule 8: not counted, not missing) but reads to a user as "this was never
// wanted" -- the wrong story for a volume that *was* fetched and unpacked, then cleaned up.
// `deleted_archive_at` (`core/itemview.py.item_view`, joined from the `deleted_archive` table)
// is the one signal that tells this `EXCLUDED` apart from an ordinary pattern-`EXCLUDED` file;
// these two functions are `FileTree.tsx`'s `Row` substitution (same shape as `isRemoving`/
// `isSettling`/`isMissing` above) and `LifecycleIcons.tsx`'s matching tooltip override.

/** The chip substitution trigger. `state` itself is deliberately not consulted -- a row this
 * `true` for always carries `state === 'EXCLUDED'` server-side (`_persist`'s vanished-row
 * sweep, above), but the wire fact that actually matters here is "did this codebase delete it
 * as a spent archive," not the state string that happens to result from that.
 */
export function isDeletedArchiveVolume(node: { deleted_archive_at: string | null }): boolean {
  return node.deleted_archive_at != null
}

/** The chip's own hover text and the item drawer's short form -- plain language, no jargon
 * ("Extracted"/`EXCLUDED`/`deleted_archive` never appear), per the task's own instruction:
 * "the volume was removed after its contents were extracted."
 */
export function deletedArchiveLabel(node: { deleted_archive_at: string | null }): string {
  if (node.deleted_archive_at == null) {
    return 'This archive volume was removed after its contents were extracted.'
  }
  return (
    'This archive volume was removed after its contents were extracted, ' +
    `${formatRelativeTimeIntl(node.deleted_archive_at)}.`
  )
}
