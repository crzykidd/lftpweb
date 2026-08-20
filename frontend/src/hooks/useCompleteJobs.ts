import { useCallback, useEffect, useRef, useState } from 'react'
import { getCompleteJobs } from '../api/client'
import type { JobOut } from '../api/types'
import { COMPLETE_PAGE_SIZE, type PageSize } from '../lib/pagination'

const POLL_INTERVAL_MS = 2000

/**
 * The Queue tab's **Complete** box (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1
 * stage 4b) -- `GET /api/jobs/complete`, server-side paginated and (optionally) filtered, the
 * terminal counterpart to `useJobs.ts`'s own Active/pending fetch. Same polling shape as
 * `useJobs` (2s, force-refetch on demand) so newly-finished jobs land here without a manual
 * reload -- "rows shifting between pages as work completes is accepted and explicitly not a
 * problem to solve" (the spec's own words), so a poll landing mid-read is fine.
 *
 * `page` (1-based) and `nameFilter` together determine the request -- `TransfersPage.tsx` is
 * responsible for debouncing `nameFilter` before it reaches here (typing shouldn't fire a
 * request per keystroke) and for resetting `page` to `1` when the filter text changes.
 *
 * `pageSize` (2026-08-20, prompts/2026-08-20-transfers-page-size-selector.md) is likewise owned
 * by the caller -- it's a per-browser persisted preference, not a constant, now that the
 * Complete box carries its own "Show 10/20/50" selector. Defaults to `COMPLETE_PAGE_SIZE` so
 * every existing call site (and every existing test) keeps working unchanged. Same as `page`
 * changing, changing `pageSize` is just a different request: a new `limit`/`offset` pair, caught
 * by the same request-id guard below -- there is nothing size-specific to add.
 *
 * A request-id guard (`requestIdRef`), not `useJobs.ts`'s simpler `cancelledRef`, because two
 * requests can genuinely race here (a poll tick landing while `page`/`nameFilter`/`pageSize` just
 * changed and fired a new request) -- only the response to the *latest* request is ever applied,
 * so a slow response to a since-superseded page/filter/size can't clobber newer state. This is
 * exactly what covers "the user switches Complete's page size while a fetch for the old size is
 * still in flight": the in-flight request's `requestId` no longer matches `requestIdRef.current`
 * (the size change bumped it via a fresh `refresh()` call, same as a page/filter change already
 * did) by the time it resolves, so its `.then`/`.catch` both bail out before touching state.
 */
export function useCompleteJobs(
  page: number,
  nameFilter: string,
  pageSize: PageSize = COMPLETE_PAGE_SIZE,
): { jobs: JobOut[]; total: number; loading: boolean; error: string | null; refresh: () => void } {
  const [jobs, setJobs] = useState<JobOut[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requestIdRef = useRef(0)

  const refresh = useCallback(() => {
    const requestId = ++requestIdRef.current
    setLoading(true)
    // Trimmed, not the raw value -- matches `lib/transferPanel.ts.filterTransferJobs`'s own
    // `search.trim().toLowerCase()` (the Active box's client-side twin), and matters here
    // specifically: `TransfersPage.tsx.handleDismissList` sends this same trimmed text to
    // `dismissAllJobs`'s `name_filter`, so what gets dismissed must match what this fetch is
    // displaying byte-for-byte, not "the same string modulo incidental whitespace".
    const trimmedFilter = nameFilter.trim()
    getCompleteJobs({
      nameFilter: trimmedFilter !== '' ? trimmedFilter : undefined,
      limit: pageSize,
      offset: (page - 1) * pageSize,
    })
      .then((res) => {
        if (requestIdRef.current !== requestId) return // superseded by a newer request
        setJobs(res.jobs)
        setTotal(res.total)
        setError(null)
      })
      .catch((err: unknown) => {
        if (requestIdRef.current !== requestId) return
        setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (requestIdRef.current === requestId) setLoading(false)
      })
  }, [page, nameFilter, pageSize])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [refresh])

  return { jobs, total, loading, error, refresh }
}
