// Pure navigation/resolution-display logic for `PathBrowseDialog.tsx` (Settings -> Queues'
// Browse button, GitHub issue #4, prompts/done/2026-08-16-path-browse-dialog.md) -- kept out
// of the component per this repo's settled pattern (lib/fileTree.ts, lib/transferPanel.ts):
// the component stays thin and everything here is directly Vitest-able with no render harness.

import type { HostOut } from '../api/types'

/** The remote-side Browse button's disabled-with-hint rule -- mirrors
 * `QueuesTab.tsx.arrDeleteCompletedDisabled`'s pure-predicate pattern so it's unit-testable the
 * same way. `host` is whatever Settings -> Connection's own data source already returns
 * (`getHost()`, the same `CredentialsBanner.tsx`/`ConnectionTab.tsx` read) -- no new poll.
 * Disabled for the same two reasons the remote browse endpoint itself 409s: no host configured
 * at all, or its stored credentials can't currently be decrypted.
 */
export function remoteBrowseDisabled(host: HostOut | null): boolean {
  return host == null || host.credentials_need_reentry
}

/** The absolute path to request next when the dialog descends into a subdirectory row --
 * `parentPath` is always the dialog's current, already-resolved `path` (never a half-typed
 * value), so this is plain POSIX joining, never another walk-up. `/` is the one case that
 * needs its own branch: joining onto it must not produce a doubled leading slash.
 */
export function descendPath(parentPath: string, entryName: string): string {
  return parentPath === '/' ? `/${entryName}` : `${parentPath.replace(/\/+$/, '')}/${entryName}`
}

export interface Breadcrumb {
  label: string
  path: string
}

/** The path readout / clickable breadcrumb trail for an already-resolved absolute `path` (the
 * dialog's own `path`, never the raw field text) -- `/data/pickup/Release` becomes `/`, `data`,
 * `pickup`, `Release`, each carrying the absolute path a click on it should re-open. `/` itself
 * is always the first crumb, even for `path === '/'`, so there's always at least one clickable
 * segment.
 */
export function breadcrumbSegments(path: string): Breadcrumb[] {
  const crumbs: Breadcrumb[] = [{ label: '/', path: '/' }]
  const parts = path.split('/').filter(Boolean)
  let built = ''
  for (const part of parts) {
    built = `${built}/${part}`
    crumbs.push({ label: part, path: built })
  }
  return crumbs
}

/** The one-line "showing nearest existing directory" note (the prompt's own wording), shown
 * only when the endpoint's `fallback_from` is set -- `null` renders nothing.
 */
export function fallbackNote(fallbackFrom: string | null): string | null {
  if (fallbackFrom == null) return null
  return `${fallbackFrom} doesn't exist or can't be read -- showing the nearest existing directory instead.`
}
