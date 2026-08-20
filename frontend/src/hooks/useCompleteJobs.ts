import { useCallback, useEffect, useRef, useState } from 'react'
import { getCompleteJobs } from '../api/client'
import type { JobOut } from '../api/types'
import { COMPLETE_PAGE_SIZE } from '../lib/pagination'

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
 * A request-id guard (`requestIdRef`), not `useJobs.ts`'s simpler `cancelledRef`, because two
 * requests can genuinely race here (a poll tick landing while `page`/`nameFilter` just changed
 * and fired a new request) -- only the response to the *latest* request is ever applied, so a
 * slow response to a since-superseded page/filter can't clobber newer state.
 */
export function useCompleteJobs(
  page: number,
  nameFilter: string,
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
      limit: COMPLETE_PAGE_SIZE,
      offset: (page - 1) * COMPLETE_PAGE_SIZE,
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
  }, [page, nameFilter])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [refresh])

  return { jobs, total, loading, error, refresh }
}
