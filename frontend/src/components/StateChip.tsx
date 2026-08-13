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
}
const FALLBACK_STYLE = 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300'

// Inline progress (2026-08-13, prompts/2026-08-13-lifecycle-icons.md): "a box with the word
// partial in it that shows a color background that keeps ticking up ... Something sexy looking"
// -- SABnzbd named as the reference point. The fill is a second, more saturated shade of the
// same chip color (never a different hue), so text stays legible whether it's sitting on the
// filled or unfilled portion -- the two shades were chosen close enough in lightness for that,
// but this is unverified against a real browser (no UI access in this environment).
const FILL_STYLES: Record<string, string> = {
  PARTIAL: 'bg-amber-300 dark:bg-amber-700/80',
  DOWNLOADING: 'bg-blue-300 dark:bg-blue-700/80',
}

interface StateChipProps {
  state: string
  /** 0-100, or `null`/`undefined` for no bar at all. Only ever meaningful for `PARTIAL`/
   * `DOWNLOADING` (`FileTree.tsx`'s `stateProgressPercent` is the one place that decides
   * that) -- a state with no entry in `FILL_STYLES` renders as a plain chip regardless of what
   * `percent` is passed, so a caller can pass a percent for every row without checking the
   * state itself.
   */
  percent?: number | null
}

/** DESIGN.md §9.2's Files-row state chip. The progress fill is the pill's own background, not
 * a separate column or element -- it grows behind the state word, exactly the SABnzbd reference
 * point above. `overflow-hidden` + a CSS `width` transition (no JS animation loop, no per-row
 * timer): the fill only ever needs to catch up to a new `percent` prop, which is already bounded
 * by how often this row re-renders (the WebSocket's own ~1 Hz progress cadence).
 */
export function StateChip({ state, percent }: StateChipProps) {
  const style = STYLES[state] ?? FALLBACK_STYLE
  const fillStyle = FILL_STYLES[state]
  const showBar = fillStyle != null && percent != null

  if (!showBar) {
    return (
      <span className={`rounded px-1.5 py-0.5 text-xs font-medium whitespace-nowrap ${style}`}>
        {state}
      </span>
    )
  }

  return (
    <span
      className={`relative inline-block overflow-hidden rounded px-1.5 py-0.5 text-xs font-medium whitespace-nowrap ${style}`}
    >
      <span
        aria-hidden="true"
        className={`absolute inset-y-0 left-0 transition-[width] duration-700 ease-out ${fillStyle}`}
        style={{ width: `${percent}%` }}
      />
      <span className="relative z-10">
        {state} {percent}%
      </span>
    </span>
  )
}
