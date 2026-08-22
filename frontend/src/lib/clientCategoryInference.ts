// Category -> queue inference (spec §8.3) -- kept out of `ClientsTab.tsx` per this repo's
// settled pattern for a pure, Vitest-able predicate.
//
// "Observed on the live test system, 2026-08-22: its two queues are
// /home/crzykidd/downloads/complete/ar-movies and .../ar-tv -- i.e. the queue remote paths
// already *are* the client's category folders." (spec §8.3). This matches every already-
// configured queue's `remote_path` against the instance's own configured base paths (spec
// §8.2 -- user-entered, never the client's own `list_base_paths` answer, which is a prefill
// only and is not wired to any endpoint in this stage) and proposes the trailing path segment
// as the category name.
//
// **Propose, never auto-apply** (spec §8.3's own words) -- this function only returns
// suggestions; `ClientsTab.tsx` is responsible for showing them to the user for confirmation
// before anything is saved.

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
