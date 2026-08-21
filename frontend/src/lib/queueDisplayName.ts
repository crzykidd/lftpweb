// "What do we display for this queue" (docs/transfers-redesign-spec.md §3.6, migration 024,
// phase 1 stage 3, prompts/done/2026-08-19-queue-short-display-name.md) -- kept out of any
// component per this repo's settled pattern (lib/fileTree.ts, lib/pathBrowse.ts): pure logic,
// directly Vitest-able with no render harness. Not called from any component yet -- stage 4
// renders it on Transfers rows once grouping drops and there's a single list to render into --
// but lives here now so that call site (and any other future one) has exactly one fallback to
// import rather than re-deriving `short_name || name` ad hoc.

/** A queue's short display name if it has one, otherwise its full `name` -- mirrors the
 * backend's own `api/settings_queues.py.resolve_queue_display_name` so both sides agree on the
 * fallback. `short_name` is `null` (never `''` -- the backend normalizes empty-after-trim to
 * `null` at save time) for "no short name set."
 */
export function queueDisplayName(shortName: string | null, name: string): string {
  return shortName || name
}
