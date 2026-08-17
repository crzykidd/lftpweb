/** Settings → Logs' text filter (2026-08-17, prompts/2026-08-17-logs-search-and-lookback.md).
 * Case-insensitive substring match over the lines the page has *already fetched* -- never a
 * refetch, never a server-side grep across rotated files. That scope is a settled call, not an
 * oversight: at the new 10k-line ceiling (`core/logtail.py.MAX_LINES_CAP`) the fetched window
 * can span an entire live log file, which is the point of raising the lookback and adding the
 * filter together -- see docs/decisions.md for the rejected server-side-grep alternative. No
 * match highlighting in v1, filtering only.
 */
export function filterLogLines(lines: string[], query: string): string[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return lines
  return lines.filter((line) => line.toLowerCase().includes(needle))
}

/** The "showing N of M lines" readout -- `null` whenever the filter is empty, so the caller
 * renders nothing rather than a no-op "showing 200 of 200 lines" on every load.
 */
export function logFilterSummary(shown: number, total: number, query: string): string | null {
  if (!query.trim()) return null
  return `Showing ${shown} of ${total} lines`
}
