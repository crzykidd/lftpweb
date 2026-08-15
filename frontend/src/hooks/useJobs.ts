import { useCallback, useEffect, useRef, useState } from 'react'
import { getJobs } from '../api/client'
import type { JobOut } from '../api/types'

const POLL_INTERVAL_MS = 2000

/**
 * The Transfers page's job list (DESIGN.md §9.2). `GET /api/jobs` is REST, per the app's
 * existing convention: a hand-rolled `fetch` client and poll hook, never TanStack Query.
 * DESIGN.md §9 called for the library from its first draft and nothing ever adopted it; §9 was
 * corrected on 2026-08-13 to describe what exists, while recording that actually adopting it
 * remains an open choice -- if that ever happens it is its own scoped piece of work, not a side
 * effect of whatever next touches this file. `usePoll` isn't reused directly because actions
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
