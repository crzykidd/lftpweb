// Phase 2 only models the four states a reconciler without a job engine can produce
// (DESIGN.md phase 2 scope). The rest of §3.2's vocabulary gets a fallback style rather than
// a hard crash if the backend ever returns one early (e.g. during phase 3 development).
const STYLES: Record<string, string> = {
  REMOTE_ONLY: 'bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300',
  LOCAL_ONLY: 'bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-300',
  PARTIAL: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  DOWNLOADED: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
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
