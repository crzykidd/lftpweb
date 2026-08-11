// Left-nav sections and (for Settings) its top tabs — DESIGN.md §9.1 / §9.2.

export interface NavItem {
  path: string
  label: string
}

export const NAV_ITEMS: NavItem[] = [
  { path: '/files', label: 'Files' },
  { path: '/transfers', label: 'Transfers' },
  { path: '/history', label: 'History' },
  { path: '/settings', label: 'Settings' },
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
