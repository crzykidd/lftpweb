import { useEffect, useState } from 'react'
import { getHealth } from '../api/client'
import type { HealthResponse } from '../api/types'
import { versionBadge } from '../lib/versionBadge'

/** Bottom-left of the nav (DESIGN.md §9.1). Links to the GitHub release when
 * LFTPWEB_REPO_URL is configured; renders as plain text otherwise — the repo doesn't
 * exist yet, and the UI must never show a dead link.
 *
 * 2026-08-16 (docs/decisions.md): a `:dev` image bakes `build_sha`/`build_channel` at build
 * time, so a dev container can flag itself here as `DEV: v0.1.1 · <sha>` (amber, linking to
 * the commit) rather than being mistaken for a release. All the branching lives in the pure
 * `lib/versionBadge.ts` (unit-tested there); this component only renders whatever it returns.
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

  const badge = versionBadge(health)

  if (!badge) {
    return <span className="text-sm text-zinc-400 dark:text-zinc-600">v&hellip;</span>
  }

  const textClassName = badge.dev
    ? 'text-sm text-amber-600 dark:text-amber-400'
    : 'text-sm text-zinc-500 dark:text-zinc-400'

  if (!badge.href) {
    return <span className={textClassName}>{badge.label}</span>
  }

  return (
    <a
      href={badge.href}
      target="_blank"
      rel="noreferrer"
      className={`${textClassName} underline decoration-dotted hover:text-zinc-900 dark:hover:text-zinc-100`}
    >
      {badge.label} &#8599;
    </a>
  )
}
