// The reset panels' own "what will this touch" breakdown (2026-08-14,
// prompts/2026-08-14-reset-panel-counts-and-layout.md) -- replaces the previous bare
// `{topLevel.length} items` reading, which gave no sense of composition and rendered
// nonsensically at zero ("— 0 items"). Pure and shared across all three scopes (All/Pattern/
// Selected) so a preview panel's count can never disagree with what the reset itself will act
// on -- same reasoning as `resetWarning.ts`'s own module comment, and deliberately its own
// small file for the same reason that one is: unit-testable without mounting anything.
//
// Takes whatever list of targets a scope's preview actually enumerates. For All and Pattern
// that is top-level items only (DESIGN.md §4.7 -- a reset targets top-level items, and their
// nested children are reset along with them but were never counted here, in the old bare-count
// reading either); for Selected it is exactly the rows the user checked, whatever depth they
// are, since that scope resets each selected row individually rather than only ever top-level
// entries. This function doesn't know or care which scope called it -- it just describes the
// list it was handed.

/** `"3 directories and 12 files — 15 items"`, singular-aware at every boundary
 * (`"1 directory and 1 file — 2 items"`), and its own explicit zero case rather than the
 * nonsensical `"— 0 items"` the old bare-count panel produced.
 */
export function describeResetTargets(items: { is_dir: boolean }[]): string {
  const total = items.length
  if (total === 0) return 'Nothing matches — 0 items.'

  const dirCount = items.filter((i) => i.is_dir).length
  const fileCount = total - dirCount

  const parts: string[] = []
  if (dirCount > 0) parts.push(`${dirCount} ${dirCount === 1 ? 'directory' : 'directories'}`)
  if (fileCount > 0) parts.push(`${fileCount} ${fileCount === 1 ? 'file' : 'files'}`)

  return `${parts.join(' and ')} — ${total} ${total === 1 ? 'item' : 'items'}`
}
