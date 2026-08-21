// Full §3.2 state vocabulary now that phase 3's job engine produces QUEUED/DOWNLOADING/
// STOPPED/FAILED alongside phase 2's structural states. This chip renders the *internal*
// state as-is (the Files tree and the item drawer show it verbatim per row) -- the
// Transfers page's own three-word visible vocabulary (queued/downloading/downloaded,
// DESIGN.md §9.2) is a presentation choice made in TransfersPage.tsx, not here.
const STYLES: Record<string, string> = {
  REMOTE_ONLY: 'bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300',
  LOCAL_ONLY: 'bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-300',
  PARTIAL: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  DOWNLOADED: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
  QUEUED: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300',
  DOWNLOADING: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
  STOPPED: 'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300',
  FAILED: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  EXCLUDED: 'bg-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400',
  // Phase 4: DESIGN.md §3.2 rule 3 -- previously downloaded, now locally absent, remote
  // still present. Deliberately its own color, distinct from REMOTE_ONLY -- this is "we
  // downloaded this and it's gone now, on purpose," not "never fetched."
  REMOVED_LOCAL: 'bg-fuchsia-100 text-fuchsia-800 dark:bg-fuchsia-900/40 dark:text-fuchsia-300',
  REMOVED_BOTH: 'bg-zinc-200 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-500',
  // Phase 5 (DESIGN.md §6): the post-processing pipeline's own states. VERIFYING/EXTRACTING
  // reuse DOWNLOADING's blue (still in progress); VERIFIED/EXTRACTED reuse DOWNLOADED's
  // green (a good outcome); CORRUPT/EXTRACT_FAILED get FAILED's red -- CORRUPT especially is
  // the gate that withholds a move-mode delete, so it should read as alarming, not neutral.
  VERIFYING: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
  VERIFIED: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
  CORRUPT: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  EXTRACTING: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
  EXTRACTED: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
  EXTRACT_FAILED: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  // Not a real `item.state` -- `FileTree.tsx`'s `Row` substitutes this synthetic label whenever
  // `substate === 'removing'` (2026-08-13, prompts/2026-08-13-delete-state-truthfulness.md), so
  // a row says something honest while `core/local_delete.py.delete_local()` is actually still
  // removing its files, rather than showing its last state (which could be a completed-looking
  // `DOWNLOADED`/`EXTRACTED`) with no indication anything is happening. Red, like FAILED/
  // CORRUPT -- this is destructive and irreversible, unlike the blue "still arriving" states.
  REMOVING: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  // Also not a real `item.state` (2026-08-13, prompts/2026-08-13-files-ux-pass.md item 3):
  // `FileTree.tsx`'s Row substitutes this whenever `substate === 'settling'` on a REMOTE_ONLY
  // row, the same substitution pattern REMOVING above already established -- replaces the
  // previous 6px dot (`h-1.5 w-1.5`, effectively invisible) with a readable, amber chip.
  // Amber, like PARTIAL/DOWNLOADING's in-progress fill: this is also "still becoming what it
  // will be," just one step earlier -- before anything has even been queued.
  SETTLING: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  // 2026-08-21 (prompts/2026-08-21-progress-fill-on-queue-and-preflight-chips.md): Preflight's
  // *arr "Waiting" chip -- a release a download client outside lftpweb is fetching, per the
  // *arr's own queue record. Same exact amber as `SETTLING` above (both "still becoming what it
  // will be," per that state's own comment) -- the two are meant to look like the same family at
  // a glance, differing only in whether `FILL_STYLES` below gives this one a bar. **Not**
  // `PARTIAL`'s bucket: `PARTIAL` means "partially transferred by lftpweb itself," and this
  // percent is a different client's own progress -- conflating the two would be exactly the
  // vocabulary confusion the "Waiting" label (`lib/preflight.ts.ARR_CHIP_LABELS`) was introduced
  // to fix.
  WAITING: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  // Also not a real `item.state` (2026-08-14, prompts/2026-08-14-removal-grace-countdown.md):
  // `FileTree.tsx`'s Row substitutes this whenever `lib/format.ts.isRemovalGracePending` is
  // true for the row -- a previously-complete item (DOWNLOADED or a post-processing outcome)
  // whose local copy just vanished and whose §7.3 grace clock is running, still showing its
  // last-known-good `state` (VERIFIED, say) with nothing indicating a transition is pending.
  // Amber, same bucket as SETTLING -- both are "still becoming what it will be, not a failure"
  // -- but a **distinct fuchsia-adjacent amber**, not SETTLING's exact shade, so the two read
  // as different situations at a glance rather than the same chip with different words (a
  // human should confirm this actually reads as distinct in a real browser -- no UI access in
  // this environment). Never FAILED's red: nothing has failed, a decision is just pending.
  MISSING: 'bg-amber-200 text-amber-900 dark:bg-amber-800/50 dark:text-amber-200',
  // A fourth synthetic substitution (2026-08-14, prompts/2026-08-14-extracted-archives-rest-as-
  // extracted.md): `FileTree.tsx`'s Row swaps to this key whenever `lib/format.ts.
  // isDeletedArchiveVolume` is true -- a spent archive volume `core/local_delete.py.
  // delete_extracted_archives` removed after a successful extraction, real `state ===
  // 'EXCLUDED'` underneath, left untouched. **Deliberately absent from this map** -- the user's
  // own call was a *greyed-out* "Extracted", the same zinc `FALLBACK_STYLE` below already
  // renders for anything unmapped, so there is nothing to add here: the label changes (see
  // `Row`'s own `label`/`title` wiring), the color falls through on its own. Same word as the
  // parent's emerald `EXTRACTED` above, different weight -- "still present and unpacked" vs.
  // "consumed, and this is why" -- never alarming red/amber the way a `Missing` chip would be
  // for a file this codebase removed on purpose.
}
const FALLBACK_STYLE = 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300'

// Inline progress (2026-08-13, prompts/2026-08-13-lifecycle-icons.md): "a box with the word
// partial in it that shows a color background that keeps ticking up ... Something sexy looking"
// -- SABnzbd named as the reference point. The fill is a second, more saturated shade of the
// same chip color (never a different hue), so text stays legible whether it's sitting on the
// filled or unfilled portion -- the two shades were chosen close enough in lightness for that.
// Unverified when first written; the user has since confirmed in a real browser that the
// `PARTIAL`/`DOWNLOADING` pairs read fine (2026-08-21 browser review). `WAITING` below reuses
// `PARTIAL`'s exact shade pair rather than inventing a third, still-unverified one.
const FILL_STYLES: Record<string, string> = {
  PARTIAL: 'bg-amber-300 dark:bg-amber-700/80',
  DOWNLOADING: 'bg-blue-300 dark:bg-blue-700/80',
  // 2026-08-21: `WAITING`'s own fill, for Preflight's *arr "Waiting" chip (the remote download
  // client's own progress, not lftpweb's) -- a `SETTLING` row keeps no entry here at all, on
  // purpose (a settle wait has no honest percentage; its detail is its tooltip, not a bar).
  // Deliberately the **exact same values as `PARTIAL`'s fill**, not a new shade: the pairing
  // comment above says the fill/base gap was "unverified against a real browser" when written,
  // but the user has since confirmed the `PARTIAL`/`DOWNLOADING` pairs read fine in practice --
  // reusing `PARTIAL`'s already-confirmed amber-100/amber-300 (light) and amber-900/40/amber-
  // 700/80 (dark) jump for this new bucket carries that same confirmed legibility forward,
  // rather than introducing a second, still-unverified amber relationship.
  WAITING: 'bg-amber-300 dark:bg-amber-700/80',
}

interface StateChipProps {
  state: string
  /** 0-100, or `null`/`undefined` for no bar at all. Only ever meaningful for `PARTIAL`/
   * `DOWNLOADING` (`lib/fileTree.ts`'s `stateProgressPercent` is the one place that decides
   * that for a Files/Transfers row) or `WAITING` (`lib/preflight.ts`'s `preflightFillPercent`,
   * for a Preflight *arr row) -- a state with no entry in `FILL_STYLES` renders as a plain chip
   * regardless of what `percent` is passed, so a caller can pass a percent for every row without
   * checking the state itself.
   */
  percent?: number | null
  /** Overrides the displayed text without changing which `STYLES`/`FILL_STYLES` bucket `state`
   * keys into (2026-08-13, prompts/2026-08-13-files-ux-pass.md item 3) -- the settle gate's
   * wait ("Waiting for changes -- 1 of 2 scans, 35s of 60s") needs its own sentence, not the
   * bare word `SETTLING`, while still getting `SETTLING`'s amber styling. `undefined` (every
   * caller before this field existed) shows `state` verbatim, unchanged.
   */
  label?: string
  /** Native hover text for the chip (2026-08-13, prompts/2026-08-13-resizable-file-columns.md)
   * -- for a chip whose `label` is a shortened stand-in (the settle countdown), this carries
   * the complete sentence back, so shortening the in-cell text never loses it, just moves it
   * to hover. `undefined` (every caller before this field existed) shows no tooltip -- the
   * chip's own visible text already is the whole story for those.
   */
  title?: string
}

/** DESIGN.md §9.2's Files-row state chip. The progress fill is the pill's own background, not
 * a separate column or element -- it grows behind the state word, exactly the SABnzbd reference
 * point above. `overflow-hidden` + a CSS `width` transition (no JS animation loop, no per-row
 * timer): the fill only ever needs to catch up to a new `percent` prop, which is already bounded
 * by how often this row re-renders (the WebSocket's own ~1 Hz progress cadence).
 */
export function StateChip({ state, percent, label, title }: StateChipProps) {
  const style = STYLES[state] ?? FALLBACK_STYLE
  const fillStyle = FILL_STYLES[state]
  const showBar = fillStyle != null && percent != null
  const text = label ?? state

  if (!showBar) {
    return (
      <span title={title} className={`rounded px-1.5 py-0.5 text-xs font-medium whitespace-nowrap ${style}`}>
        {text}
      </span>
    )
  }

  return (
    <span
      title={title}
      className={`relative inline-block overflow-hidden rounded px-1.5 py-0.5 text-xs font-medium whitespace-nowrap ${style}`}
    >
      <span
        aria-hidden="true"
        className={`absolute inset-y-0 left-0 transition-[width] duration-700 ease-out ${fillStyle}`}
        style={{ width: `${percent}%` }}
      />
      <span className="relative z-10">
        {text} {percent}%
      </span>
    </span>
  )
}
