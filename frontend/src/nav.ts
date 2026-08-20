// Left-nav sections and (for Settings and Docs) their top tabs — DESIGN.md §9.1 / §9.2.

export interface NavItem {
  path: string
  label: string
}

export const NAV_ITEMS: NavItem[] = [
  { path: '/transfers', label: 'Transfers' },
  // 2026-08-20 (docs/transfers-redesign-spec.md §2, phase 1 stage 7): History becomes Events --
  // the audit-event log only, its `job` list dropped since the Queue tab's Complete box (stage
  // 4b) already covers "what finished, in what order." `/history` still redirects here
  // (`App.tsx`) so nothing that links or bookmarks the old path breaks.
  { path: '/events', label: 'Events' },
  { path: '/dashboard', label: 'Dashboard' },
  { path: '/settings', label: 'Settings' },
  { path: '/docs', label: 'Docs' },
]

/** Transfers section tabs (2026-08-20, docs/transfers-redesign-spec.md §2, phase 1 stage 6) --
 * Transfers is now the main section, and Files (the old standalone nav entry) is demoted to its
 * second tab rather than removed: it stays the only view of `REMOTE_ONLY` items that never
 * entered the pipeline, the only home for Delete, and the only tree-shaped view of the remote.
 * Queue is first / the default tab -- "the working surface now" (the task's own instruction).
 */
export const TRANSFERS_TABS: NavItem[] = [
  { path: '/transfers/queue', label: 'Queue' },
  { path: '/transfers/files', label: 'Files' },
]

export const SETTINGS_TABS: NavItem[] = [
  { path: '/settings/connection', label: 'Connection' },
  { path: '/settings/queues', label: 'Queues' },
  { path: '/settings/transfer', label: 'Transfer' },
  { path: '/settings/post-processing', label: 'Post-processing' },
  { path: '/settings/integrations', label: 'Integrations' },
  { path: '/settings/logs', label: 'Logs' },
  { path: '/settings/backup', label: 'Backup' },
  { path: '/settings/auth', label: 'Auth' },
]

/** In-app user documentation (2026-08-13, prompts/2026-08-13-docs-section.md) — the section for
 * someone whose instance is *running* and who does not know why nothing is downloading.
 * `README.md` still serves the person who hasn't deployed yet and links onward here rather than
 * repeating any of it.
 */
export const DOCS_TABS: NavItem[] = [
  { path: '/docs/quick-start', label: 'Quick start' },
  { path: '/docs/how-it-works', label: 'How it works' },
  { path: '/docs/concepts', label: 'Concepts' },
  // 2026-08-17: renders CHANGELOG.md verbatim (ReleaseNotesPage.tsx); the nav's bottom-left
  // version link now points here for a release build instead of straight to GitHub.
  { path: '/docs/release-notes', label: 'Release notes' },
]

/** Which top-tab strip (if any) belongs above a given route. `Layout.tsx` used to hardcode a
 * single `pathname.startsWith('/settings')` check; Docs is the second section with tabs, and a
 * second hardcoded branch is exactly the shape that grows a third. Longest-prefix-free by
 * construction — the three section roots don't nest — and returns `null` for a section with no
 * tabs so the caller renders no strip rather than an empty one.
 */
export function tabsForPath(pathname: string): NavItem[] | null {
  if (pathname === '/transfers' || pathname.startsWith('/transfers/')) return TRANSFERS_TABS
  if (pathname === '/settings' || pathname.startsWith('/settings/')) return SETTINGS_TABS
  if (pathname === '/docs' || pathname.startsWith('/docs/')) return DOCS_TABS
  return null
}
