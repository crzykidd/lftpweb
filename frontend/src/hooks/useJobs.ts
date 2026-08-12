import { useCallback, useEffect, useRef, useState } from 'react'
import { getJobs } from '../api/client'
import type { JobOut } from '../api/types'

const POLL_INTERVAL_MS = 2000

/**
 * The Transfers page's job list (DESIGN.md §9.2). `GET /api/jobs` is REST, per the app's
 * existing convention (DESIGN.md §9 calls for TanStack Query here; phases 1-3a never adopted
 * it and used a hand-rolled poll instead -- see the phase 3b report's design-decisions
 * section. This follows the convention already in the codebase rather than introducing a
 * new data-fetching library mid-project) -- `usePoll` isn't reused directly because actions
 * (queue/stop/move-to-top/start-now/retry) need to force an immediate refetch rather than
 * waiting up to `POLL_INTERVAL_MS` for their own result to show up.
 */
export function useJobs(): { jobs: JobOut[]; refresh: () => void } {
  const [jobs, setJobs] = useState<JobOut[]>([])
  const cancelledRef = useRef(false)

  const refresh = useCallback(() => {
    getJobs()
      .then((res) => {
        if (!cancelledRef.current) setJobs(res.jobs)
      })
      .catch(() => {
        // Transient fetch failures (backend restart, brief network blip) keep the
        // last-known list on screen rather than flashing empty.
      })
  }, [])

  useEffect(() => {
    cancelledRef.current = false
    refresh()
    const id = setInterval(refresh, POLL_INTERVAL_MS)
    return () => {
      cancelledRef.current = true
      clearInterval(id)
    }
  }, [refresh])

  return { jobs, refresh }
}
