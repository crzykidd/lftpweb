// Left-nav sections and (for Settings and Docs) their top tabs — DESIGN.md §9.1 / §9.2.

export interface NavItem {
  path: string
  label: string
}

export const NAV_ITEMS: NavItem[] = [
  { path: '/files', label: 'Files' },
  { path: '/transfers', label: 'Transfers' },
  { path: '/history', label: 'History' },
  { path: '/dashboard', label: 'Dashboard' },
  { path: '/settings', label: 'Settings' },
  { path: '/docs', label: 'Docs' },
]

export const SETTINGS_TABS: NavItem[] = [
  { path: '/settings/connection', label: 'Connection' },
  { path: '/settings/queues', label: 'Queues' },
  { path: '/settings/transfer', label: 'Transfer' },
  { path: '/settings/post-processing', label: 'Post-processing' },
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
]

/** Which top-tab strip (if any) belongs above a given route. `Layout.tsx` used to hardcode a
 * single `pathname.startsWith('/settings')` check; Docs is the second section with tabs, and a
 * second hardcoded branch is exactly the shape that grows a third. Longest-prefix-free by
 * construction — the two section roots don't nest — and returns `null` for a section with no
 * tabs so the caller renders no strip rather than an empty one.
 */
export function tabsForPath(pathname: string): NavItem[] | null {
  if (pathname === '/settings' || pathname.startsWith('/settings/')) return SETTINGS_TABS
  if (pathname === '/docs' || pathname.startsWith('/docs/')) return DOCS_TABS
  return null
}
