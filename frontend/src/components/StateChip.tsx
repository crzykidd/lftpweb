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
}
const FALLBACK_STYLE = 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300'

export function StateChip({ state }: { state: string }) {
  const style = STYLES[state] ?? FALLBACK_STYLE
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium whitespace-nowrap ${style}`}>
      {state}
    </span>
  )
}
