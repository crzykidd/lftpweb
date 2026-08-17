import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { NAV_ITEMS, tabsForPath } from '../nav'
import { useAuth } from '../hooks/authContext'
import { CredentialsBanner } from './CredentialsBanner'
import { StatsHeader } from './StatsHeader'
import { ThemeToggle } from './ThemeToggle'
import { VersionLink } from './VersionLink'
import { WhatsNewDialog } from './WhatsNewDialog'

const navLinkClasses = ({ isActive }: { isActive: boolean }) =>
  `block rounded-md px-3 py-2 text-sm font-medium transition-colors ${
    isActive
      ? 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900'
      : 'text-zinc-700 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800'
  }`

const tabLinkClasses = ({ isActive }: { isActive: boolean }) =>
  `border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
    isActive
      ? 'border-zinc-900 text-zinc-900 dark:border-zinc-100 dark:text-zinc-100'
      : 'border-transparent text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200'
  }`

/** The app chrome: left nav, top tabs (only where a section has more than one page — Settings
 * and, since 2026-08-13, Docs), and the stats header. Every later phase's pages render into the
 * <Outlet />. DESIGN.md §9.1.
 */
export function Layout() {
  const location = useLocation()
  const tabs = tabsForPath(location.pathname)
  const { session, logout } = useAuth()

  return (
    // `h-dvh` + `overflow-hidden` (2026-08-17, prompts/done/2026-08-17-chart-height-cap-and-
    // single-scroll.md; see docs/decisions.md for `h-dvh` vs `h-screen`) -- previously
    // `min-h-screen` let this root *grow* past the viewport when content was tall, so the
    // window scrollbar engaged alongside `<main>`'s own `overflow-auto` below: two scroll
    // contexts, and scrolling the window past this root's painted background revealed the
    // unstyled document background (white, worst in dark mode -- see index.css for the
    // matching fix on `html`/`body`). Pinning the root to exactly one viewport height and
    // clipping overflow here makes `<main>` the *only* scroll context; the sidebar stays put
    // while content scrolls, which was already the markup's intent.
    <div className="flex h-dvh overflow-hidden bg-white text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      {/* Mounted once for the whole shell (2026-08-17, DESIGN.md §9.1) -- it renders nothing
       * until its own health fetch resolves to sections worth showing (lib/releaseNotes.ts). */}
      <WhatsNewDialog />
      <aside className="flex w-48 shrink-0 flex-col justify-between border-r border-zinc-200 p-3 dark:border-zinc-800">
        <nav className="flex flex-col gap-1">
          {NAV_ITEMS.map((item) => (
            <NavLink key={item.path} to={item.path} className={navLinkClasses}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="flex flex-col gap-2 border-t border-zinc-200 pt-3 dark:border-zinc-800">
          {/* Only meaningful in password mode with a real session -- `none` and `proxy`
           * (identity comes from the reverse proxy, not a session this app owns) have
           * nothing for "sign out" to do. */}
          {session?.mode === 'password' && session.authenticated && (
            <div className="flex items-center justify-between gap-2 text-xs text-zinc-500 dark:text-zinc-400">
              <span className="truncate" title={session.username ?? undefined}>
                {session.username}
              </span>
              <button
                type="button"
                onClick={() => void logout()}
                className="shrink-0 rounded px-1.5 py-0.5 hover:bg-zinc-100 hover:text-zinc-900 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
              >
                Sign out
              </button>
            </div>
          )}
          <ThemeToggle />
          <VersionLink />
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <CredentialsBanner />
        <StatsHeader />
        {tabs != null && (
          <nav className="flex gap-1 overflow-x-auto border-b border-zinc-200 px-4 dark:border-zinc-800">
            {tabs.map((tab) => (
              <NavLink key={tab.path} to={tab.path} className={tabLinkClasses}>
                {tab.label}
              </NavLink>
            ))}
          </nav>
        )}
        <main className="min-w-0 flex-1 overflow-auto p-4">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
