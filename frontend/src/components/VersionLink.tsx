import { useEffect, useState } from 'react'
import { getHealth } from '../api/client'
import type { HealthResponse } from '../api/types'

/** Bottom-left of the nav (DESIGN.md §9.1). Links to the GitHub release when
 * LFTPWEB_REPO_URL is configured; renders as plain text otherwise — the repo doesn't
 * exist yet, and the UI must never show a dead link.
 */
export function VersionLink() {
  const [health, setHealth] = useState<HealthResponse | null>(null)

  useEffect(() => {
    let cancelled = false
    getHealth()
      .then((result) => {
        if (!cancelled) setHealth(result)
      })
      .catch(() => {
        // Leave the placeholder up; nothing useful to show for a health-check failure here.
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (!health) {
    return <span className="text-sm text-zinc-400 dark:text-zinc-600">v&hellip;</span>
  }

  const label = `v${health.version}`

  if (!health.repo_url) {
    return <span className="text-sm text-zinc-500 dark:text-zinc-400">{label}</span>
  }

  return (
    <a
      href={`${health.repo_url}/releases/tag/${label}`}
      target="_blank"
      rel="noreferrer"
      className="text-sm text-zinc-500 underline decoration-dotted hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
    >
      {label} &#8599;
    </a>
  )
}
