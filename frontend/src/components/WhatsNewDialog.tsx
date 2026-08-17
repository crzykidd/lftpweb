import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import changelogSource from '../../../CHANGELOG.md?raw'
import { getHealth } from '../api/client'
import type { HealthResponse } from '../api/types'
import { parseChangelog, trimEmptySubsections, whatsNewSections } from '../lib/releaseNotes'
import { readLocalStorage, writeLocalStorage } from '../lib/storage'
import { SectionBody } from '../pages/docs/MarkdownDoc'

const STORAGE_KEY = 'whatsnew.lastSeenVersion'

function isVersionString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0
}

/** The what's-new popup (2026-08-17, DESIGN.md §9.1,
 * prompts/2026-08-17-whats-new-popup-and-release-notes.md): the first page load after an
 * upgrade shows what changed, without needing to already know Release notes exists.
 *
 * All the "should this show, and what" logic lives in `lib/releaseNotes.ts.whatsNewSections`,
 * unit-tested there against every case (fresh browser, same version, multi-version
 * accumulation, downgrade, archived-out) -- this component only reads `whatsnew.lastSeenVersion`
 * out of storage, calls that function, and renders whatever it returns. Dev builds need no
 * special casing: `health.version` is the same bare `MAJOR.MINOR.PATCH` either way (dev or
 * release), and it only actually changes when a real release bump lands, so the popup shows on
 * exactly that event regardless of build channel.
 *
 * Health source: a second, independent one-shot `getHealth()` call on mount, matching
 * `VersionLink.tsx`'s own pattern -- not `StatsHeader.tsx`'s 5s `usePoll`, since nothing here
 * needs to notice a later change; the version this browser saw at mount is the only thing that
 * matters. Per-browser semantics (`localStorage`) is a settled, named limitation: two browsers
 * (or one browser's normal/private windows) each track "last seen" independently, and there is
 * no server-side per-user seen-state (docs/decisions.md).
 */
export function WhatsNewDialog() {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [sections, setSections] = useState<ReturnType<typeof whatsNewSections> | null>(null)

  useEffect(() => {
    let cancelled = false
    getHealth()
      .then((result) => {
        if (!cancelled) setHealth(result)
      })
      .catch(() => {
        // No health yet -- the popup simply never appears this load; nothing to show for a
        // failed health check here, same as VersionLink's own placeholder-forever fallback.
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!health) return

    const lastSeen = readLocalStorage(STORAGE_KEY, isVersionString)
    const toShow = whatsNewSections(health.version, lastSeen, parseChangelog(changelogSource))

    if (toShow.length === 0) {
      // Fresh browser, same version, downgrade, or archived-out -- store silently either way
      // (`whatsNewSections`'s own doc comment) so `lastSeenVersion` never goes stale.
      writeLocalStorage(STORAGE_KEY, health.version)
    }
    setSections(toShow)
  }, [health])

  if (!sections || sections.length === 0) return null

  const dismiss = () => {
    if (health) writeLocalStorage(STORAGE_KEY, health.version)
    setSections([])
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop: same convention as PathBrowseDialog.tsx/ItemDrawer.tsx's own -- click closes,
       * which here means "dismiss," same as the explicit button. */}
      <button type="button" aria-label="Close" className="absolute inset-0 bg-black/20 dark:bg-black/40" onClick={dismiss} />
      <div className="relative flex max-h-[80vh] w-full max-w-lg flex-col rounded-md border border-zinc-200 bg-white shadow-xl dark:border-zinc-800 dark:bg-zinc-950">
        <div className="flex items-center justify-between gap-2 border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">What's new</h2>
          <button
            type="button"
            onClick={dismiss}
            className="rounded-md border border-zinc-300 px-2.5 py-1 text-sm text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
          >
            Close
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          <div className="flex flex-col gap-6">
            {sections.map((section) => (
              <div key={section.version} className="flex flex-col gap-2">
                <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                  v{section.version}
                  {section.date && <span className="ml-2 font-normal text-zinc-500 dark:text-zinc-400">{section.date}</span>}
                </h3>
                <SectionBody markdown={trimEmptySubsections(section.body)} />
              </div>
            ))}
          </div>
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <Link
            to="/docs/release-notes"
            onClick={dismiss}
            className="text-sm text-zinc-600 underline decoration-zinc-400 underline-offset-2 hover:text-zinc-900 dark:text-zinc-300 dark:hover:text-zinc-100"
          >
            View full release notes
          </Link>
          <button
            type="button"
            onClick={dismiss}
            className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
          >
            Got it
          </button>
        </div>
      </div>
    </div>
  )
}
