import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getHealth } from '../api/client'
import type { HealthResponse } from '../api/types'
import { classifyLink } from '../lib/docLinks'
import { versionBadge } from '../lib/versionBadge'

/** Bottom-left of the nav (DESIGN.md §9.1). Links to the in-app Release notes page for a
 * release build (or the no-channel fallback); renders as plain text only in the dev-channel
 * dead-link case (no `build_sha`/`repo_url` at all — `lib/versionBadge.ts`).
 *
 * 2026-08-16 (docs/decisions.md): a `:dev` image bakes `build_sha`/`build_channel` at build
 * time, so a dev container can flag itself here as `DEV: v0.1.1 · <sha>` (amber, linking to
 * the commit) rather than being mistaken for a release. All the branching lives in the pure
 * `lib/versionBadge.ts` (unit-tested there); this component only renders whatever it returns.
 *
 * 2026-08-17: `badge.href` can now be an in-app route (`/docs/release-notes`), not just a
 * GitHub URL — `classifyLink` (already used by the Docs Markdown renderer for the identical
 * decision) picks `Link` for that case so it navigates through the router instead of a full
 * page reload; anything else still gets a plain `<a target="_blank">` out to GitHub.
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

  const linkClassName = `${textClassName} underline decoration-dotted hover:text-zinc-900 dark:hover:text-zinc-100`

  if (classifyLink(badge.href) === 'internal') {
    // No "↗" (external-link) glyph and no `target="_blank"` -- this stays inside the app.
    return (
      <Link to={badge.href} className={linkClassName}>
        {badge.label}
      </Link>
    )
  }

  return (
    <a href={badge.href} target="_blank" rel="noreferrer" className={linkClassName}>
      {badge.label} &#8599;
    </a>
  )
}
