import { useEffect, useState } from 'react'
import changelogSource from '../../../../CHANGELOG.md?raw'
import { getHealth } from '../../api/client'
import { SectionBody } from './MarkdownDoc'

/** Docs -> Release notes (2026-08-17,
 * prompts/2026-08-17-whats-new-popup-and-release-notes.md) -- renders `CHANGELOG.md`
 * **verbatim**, the same file GitHub and `release-cut` both treat as the single source of
 * release notes (`standards.md`'s `release-prep-and-cut`). Deliberately *not* routed through
 * `MarkdownDoc`/`parseDocSource` the way `QuickStartPage`/`ConceptsPage` are -- that parser
 * expects a `# Title` + one lede paragraph + only-`## `-boundaries shape, and would throw on
 * this file's own intro paragraphs and commented-out skeleton. `SectionBody` (exported from
 * `MarkdownDoc.tsx`) is the same react-markdown + remark-gfm pipeline those pages use for a
 * section's prose, just handed the whole file as one blob instead of a pre-split section.
 *
 * The nav's bottom-left version link (`lib/versionBadge.ts`) now points here for a release
 * build instead of straight to GitHub; the GitHub link isn't gone, it just moved to the small
 * "View on GitHub" line below, so it's still one click away rather than the default target.
 */
export function ReleaseNotesPage() {
  // `repo_url` is `/api/health`-only, not baked in at build time (`lib/versionBadge.ts`'s own
  // comment) -- this page has no other way to reach it, and threading it down from `Layout.tsx`
  // would mean every route re-renders on a health tick just to feed one link on one page. A
  // second, independent one-shot `getHealth()` call (same pattern `VersionLink.tsx` and
  // `WhatsNewDialog.tsx` each already use, not a poll) is the less invasive of the two options
  // named in the task.
  const [repoUrl, setRepoUrl] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getHealth()
      .then((result) => {
        if (!cancelled && result.repo_url) setRepoUrl(result.repo_url)
      })
      .catch(() => {
        // No repo_url yet (unconfigured, or health hasn't loaded) -- the page still renders
        // the changelog; the GitHub line just doesn't appear (never a dead link).
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="flex max-w-3xl flex-col gap-4 pb-12">
      {repoUrl && (
        <a
          href={`${repoUrl}/releases`}
          target="_blank"
          rel="noopener noreferrer"
          className="self-start text-sm text-zinc-500 underline decoration-dotted hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
        >
          View on GitHub &#8599;
        </a>
      )}
      <SectionBody markdown={changelogSource} />
    </div>
  )
}
