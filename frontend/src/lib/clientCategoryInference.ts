// Category -> queue inference and binding (spec §8.3) -- kept out of `ClientsTab.tsx` per this
// repo's settled pattern for pure, Vitest-able predicates.
//
// **Redesigned 2026-08-23** (prompts/2026-08-23-category-binding-redesign.md, findings #10/#11
// in prompts/test-findings-2026-08-23.md): the free-text category field that used to sit next
// to this inference offer is gone. It proposed nothing for a real setup (base-path arithmetic
// only ever holds for SABnzbd's `<base>/<category>` layout, and can never work for rTorrent,
// whose labels live in `d.custom1` with no relation to any directory) and silently dropped a
// blank row on save because its `placeholder` text looked like a filled-in value but wasn't
// one. The replacement, the user's own design: show every category the client actually has
// (`list_categories`, spec §2.1/§8.3), one row each, a queue dropdown defaulting to unbound --
// nothing to type, so nothing to typo and nothing blank to drop.
//
// `inferCategoryMappings` (the original base-path-arithmetic proposal) is kept as the fallback
// mechanism `computeCategoryRows` reaches for only when the client itself reports no categories
// at all -- a fresh SAB with an empty queue and empty history is exactly the case a category
// list can't help with, and path arithmetic can still propose something there. `suggestQueue
// ForCategory` and `computeCategoryRows` are the new, preferred path: matching the client's own
// reported category names directly against queue names / remote-path trailing segments, per
// finding #10's own conclusion that this is the *direct* signal and path arithmetic was always
// a proxy for it.
//
// **Propose, never auto-apply the wrong thing; always propose the right thing as a pre-selected
// value.** Unlike the old free-text control, a suggested binding here *is* the dropdown's
// initial value -- saving without touching it is supposed to persist the suggestion, not silently
// discard an unconfirmed guess (spec §8.3's own words, still honoured: the user can always
// change or unbind a row before Save).

export interface QueueForInference {
  id: number
  remote_path: string
}

export interface InferredCategoryMapping {
  category: string
  queue_id: number
  queue_remote_path: string
}

function stripTrailingSlash(path: string): string {
  return path === '/' ? path : path.replace(/\/+$/, '')
}

/** One proposed mapping per queue whose `remote_path` sits **directly** under one of
 * `basePaths` -- a queue nested two or more levels below a base path isn't the reference
 * workflow's shape (spec §1.1's `<base>/<category>` layout), and guessing at which ancestor
 * segment is "the category" would be worse than proposing nothing. Queues under more than one
 * base path, or under none, are silently omitted -- there is nothing safe to propose for them.
 */
export function inferCategoryMappings(
  basePaths: string[],
  queues: QueueForInference[],
): InferredCategoryMapping[] {
  const normalizedBases = basePaths.map(stripTrailingSlash).filter((p) => p.length > 0)
  const results: InferredCategoryMapping[] = []

  for (const queue of queues) {
    const remote = stripTrailingSlash(queue.remote_path)
    for (const base of normalizedBases) {
      const prefix = base === '/' ? '/' : `${base}/`
      if (!remote.startsWith(prefix) || remote === base) continue
      const rest = remote.slice(prefix.length)
      if (rest.length > 0 && !rest.includes('/')) {
        results.push({ category: rest, queue_id: queue.id, queue_remote_path: queue.remote_path })
      }
      break
    }
  }
  return results
}

// --------------------------------------------------------------------------------------------
// The redesigned control (2026-08-23) -- direct signal preferred, path arithmetic as fallback.
// --------------------------------------------------------------------------------------------

export interface QueueForCategorySuggestion extends QueueForInference {
  name: string
}

// Round 4 (2026-08-23, prompts/2026-08-23-path-attribution-and-category-escape-hatch.md):
// restores a manual "Add category" escape hatch, mirroring `BasePathDraft.source`
// ('detected' | 'manual') exactly, for the identical reason -- rTorrent's `list_categories` is
// DERIVED (spec §5): it can only report labels *currently in use*, so a category that will exist
// later (e.g. "ar-movies" before the first movie is grabbed) can never be detected, and the
// prior redesign (2026-08-23-category-binding-redesign.md) removed the only way to enter one.
// `'client'` covers both a directly-detected category and a path-arithmetic guess -- neither was
// typed by a person, so neither is `'manual'`.
export type CategoryRowSource = 'client' | 'manual'

export interface CategoryRowDraft {
  category: string
  queue_id: number | null
  source: CategoryRowSource
}

function trailingSegment(path: string): string {
  const trimmed = stripTrailingSlash(path)
  const idx = trimmed.lastIndexOf('/')
  return idx === -1 ? trimmed : trimmed.slice(idx + 1)
}

/** Suggest a queue for one category the client itself reported -- spec's own words: "a queue's
 * name or the trailing segment of its remote_path matches the category." A pure lookup; never
 * applied to anything except as `computeCategoryRows`'s pre-selected initial value below.
 */
export function suggestQueueForCategory(
  category: string,
  queues: QueueForCategorySuggestion[],
): number | null {
  const byName = queues.find((q) => q.name === category)
  if (byName != null) return byName.id
  const bySegment = queues.find((q) => trailingSegment(q.remote_path) === category)
  return bySegment ? bySegment.id : null
}

/** Which mechanism produced the rows `computeCategoryRows` returned -- #10's own conclusion,
 * applied to the UI: "the empty result must explain itself... never blur 'the client told us'
 * with 'we guessed from your paths.'" `'none'` means neither mechanism has anything new to
 * offer beyond whatever was already saved (`existing`).
 */
export type CategorySource = 'client' | 'path_arithmetic' | 'none'

export interface ComputedCategoryRows {
  rows: CategoryRowDraft[]
  source: CategorySource
}

/** One row per category to show in the settings form -- the redesigned control's whole point:
 * no free-text row, nothing to type, and a suggested binding is always a pre-selected dropdown
 * value rather than placeholder text (findings #10/#11c).
 *
 * - **`detectedCategories` non-null and non-empty** -- the direct signal: one row per reported
 *   category, keeping the already-saved `queue_id` for a category already mapped (`existing`),
 *   or suggesting one otherwise. Any `existing` row for a category the client no longer reports
 *   is preserved, never dropped -- "categories appearing later" cuts both ways, and a stale
 *   mapping is still real configuration the user should see (and can remove) rather than lose
 *   silently because one probe didn't happen to repeat it. `source: 'client'`.
 * - **`detectedCategories` null or empty** -- never tested this session, or the client
 *   genuinely has none configured yet (a fresh SAB, an rTorrent with nothing labelled). Falls
 *   back to `inferCategoryMappings`'s base-path arithmetic for anything not already in
 *   `existing` (`source: 'path_arithmetic'` if it proposed something new), or `source: 'none'`
 *   if it didn't either -- distinguishing "nothing to add" from "here's a guess" is the point.
 */
export function computeCategoryRows(
  existing: CategoryRowDraft[],
  detectedCategories: string[] | null,
  basePaths: string[],
  queues: QueueForCategorySuggestion[],
): ComputedCategoryRows {
  const byCategory = new Map(existing.map((c) => [c.category, c]))

  if (detectedCategories != null && detectedCategories.length > 0) {
    const rows: CategoryRowDraft[] = detectedCategories.map((category) => {
      const saved = byCategory.get(category)
      // A category the client now genuinely reports is no longer speculative, even if a
      // manually-added row for it already existed (someone typed "ar-movies" ahead of its first
      // download, and the first download has now happened) -- `source` flips to `'client'` so
      // Remove's own "only on a stale row" rule (round 3's #14c) governs it going forward,
      // rather than the manual row's own "always removable" escape hatch staying live for a
      // category that is, in fact, live.
      if (saved != null) return { ...saved, source: 'client' }
      return { category, queue_id: suggestQueueForCategory(category, queues), source: 'client' }
    })
    const detectedSet = new Set(detectedCategories)
    for (const row of existing) {
      if (!detectedSet.has(row.category)) rows.push(row)
    }
    return { rows, source: 'client' }
  }

  const proposals = inferCategoryMappings(basePaths, queues)
  const rows = [...existing]
  let addedAny = false
  for (const p of proposals) {
    if (!byCategory.has(p.category)) {
      rows.push({ category: p.category, queue_id: p.queue_id, source: 'client' })
      addedAny = true
    }
  }
  return { rows, source: addedAny ? 'path_arithmetic' : 'none' }
}

/** The one-line explanation of *why* these rows are what they are (finding #11a: "the section
 * has no explanatory text, and the concept is genuinely non-obvious"; #10: "the empty result
 * must explain itself"). Never blur which mechanism produced a suggestion.
 *
 * `hasSavedRows` (finding #14, 2026-08-23) distinguishes two different reasons `source === 'none'
 * && detectedCategories == null` can be true: a genuinely fresh instance nobody has tested yet
 * (nothing to show, "Test... to see"), versus re-opening a *saved* instance for edit in a session
 * that hasn't re-tested it -- the screenshot evidence for this finding. The rows on screen in the
 * second case are real, previously-detected categories (`startEdit` hydrates them from what was
 * saved), not something the instruction "Test the connection above" implies the user hasn't done
 * yet. Defaults to `false` so every existing two-argument call site keeps its prior behaviour.
 */
export function describeCategorySource(
  source: CategorySource,
  detectedCategories: string[] | null,
  hasSavedRows: boolean = false,
): string {
  if (source === 'client') {
    return 'These categories come directly from this client.'
  }
  if (source === 'path_arithmetic') {
    return (
      'This client did not report its categories directly, so these are guessed from your ' +
      'configured base paths and existing queues instead — review before saving.'
    )
  }
  if (detectedCategories == null) {
    return hasSavedRows
      ? 'Showing the categories saved with this instance. Test the connection above to refresh them from the client directly.'
      : 'Test the connection above to see this client’s own categories.'
  }
  return 'This client reported no categories, and nothing could be guessed from your base paths.'
}

/** Whether a category row is "stale" -- a saved mapping for a category the client no longer
 * reports (finding #14c, 2026-08-23: "computeCategoryRows deliberately preserves a stored
 * mapping for a category the client no longer reports... that is the only case where Remove is
 * meaningful"). A category the client still reports can only ever be left unbound in the UI --
 * removing it would just make the row reappear on the next Test, since `computeCategoryRows`
 * rebuilds one row per currently-reported category unconditionally.
 *
 * `detectedCategories === null` means "never tested this session" -- staleness genuinely can't
 * be determined from nothing, so every row reports `false` in that state rather than guessing.
 */
export function isStaleCategoryRow(category: string, detectedCategories: string[] | null): boolean {
  if (detectedCategories == null) return false
  return !detectedCategories.includes(category)
}

/** Whether Remove should appear at all for a given row (round 4, 2026-08-23: restores the manual
 * "Add category" escape hatch). Two independent reasons, either one sufficient:
 *
 * - **`source === 'manual'`** -- a hand-added row is never auto-produced by `computeCategoryRows`
 *   (unless the client has since started actually reporting it, at which point `source` flips to
 *   `'client'` there -- see that function's own comment), so nothing will silently bring it back
 *   the way removing a still-reported row would. Always removable, mirroring base paths' own
 *   manual escape hatch, which is unconditionally removable for the identical reason.
 * - **`isStaleCategoryRow`** -- unchanged from round 3 (#14c): a saved mapping for a category the
 *   client no longer reports.
 *
 * A row that is neither -- a `'client'`-sourced row the client currently still reports -- can
 * only ever be left unbound; Remove would just reappear on the next Test.
 */
export function canRemoveCategoryRow(
  row: Pick<CategoryRowDraft, 'category' | 'source'>,
  detectedCategories: string[] | null,
): boolean {
  return row.source === 'manual' || isStaleCategoryRow(row.category, detectedCategories)
}
