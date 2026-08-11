import { useEffect, useState } from 'react'

/**
 * Polls `fetcher` every `intervalMs` and returns the latest value. Good enough for phase
 * 1's zeroed-out stats; the WebSocket that pushes live deltas (DESIGN.md §9) replaces this
 * for Files/Transfers in a later phase.
 */
export function usePoll<T>(fetcher: () => Promise<T>, intervalMs: number): T | undefined {
  const [value, setValue] = useState<T | undefined>(undefined)

  useEffect(() => {
    let cancelled = false

    const tick = () => {
      fetcher()
        .then((result) => {
          if (!cancelled) setValue(result)
        })
        .catch(() => {
          // Transient fetch failures are expected while the backend restarts (hot reload,
          // container boot); the last-known value stays on screen rather than flashing.
        })
    }

    tick()
    const id = setInterval(tick, intervalMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [fetcher, intervalMs])

  return value
}
