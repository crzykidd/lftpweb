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
