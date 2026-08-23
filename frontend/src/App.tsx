import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { FilesPage } from './pages/FilesPage'
import { DiskReviewPage } from './pages/DiskReviewPage'
import { TransfersPage } from './pages/TransfersPage'
import { EventsPage } from './pages/EventsPage'
import { DashboardPage } from './pages/DashboardPage'
import { LoginPage } from './pages/LoginPage'
import { ConnectionTab } from './pages/settings/ConnectionTab'
import { QueuesTab } from './pages/settings/QueuesTab'
import { TransferTab } from './pages/settings/TransferTab'
import { PostProcessingTab } from './pages/settings/PostProcessingTab'
import { IntegrationsTab } from './pages/settings/IntegrationsTab'
import { ClientsTab } from './pages/settings/ClientsTab'
import { LogsTab } from './pages/settings/LogsTab'
import { BackupTab } from './pages/settings/BackupTab'
import { AuthTab } from './pages/settings/AuthTab'
import { QuickStartPage } from './pages/docs/QuickStartPage'
import { ConceptsPage } from './pages/docs/ConceptsPage'
import { HowItWorksPage } from './pages/docs/HowItWorksPage'
import { ReleaseNotesPage } from './pages/docs/ReleaseNotesPage'
import { useAuth } from './hooks/authContext'

function App() {
  const { session } = useAuth()

  // `session === null` only while the very first `GET /api/auth/session` is in flight
  // (`hooks/useAuth.tsx`) -- render nothing for that one tick rather than flashing the login
  // form (or the real app) before we know which one is correct.
  if (session === null) return null

  // Gating the *entire* routed app behind one check, rather than a per-route guard, is
  // deliberate for the same reason `middleware.py` is one gate instead of a per-router
  // Depends() on the backend: it makes "did I forget to protect a page" impossible to get
  // wrong, because there is no per-route decision to forget. `AUTH_MODE=none`/`proxy`-
  // authenticated/`password`-with-a-valid-session all read `authenticated: true` here.
  if (!session.authenticated) return <LoginPage />

  return (
    <Routes>
      <Route element={<Layout />}>
        {/* Transfers is the main section (2026-08-20, docs/transfers-redesign-spec.md §2, phase
         * 1 stage 6): Queue and Files are now tabs beneath it rather than two separate top-level
         * nav entries -- see `nav.ts.TRANSFERS_TABS`. Queue is the default tab ("the working
         * surface now," the task's own instruction), so both the section root and the app's own
         * landing route resolve there. */}
        <Route index element={<Navigate to="/transfers/queue" replace />} />
        {/* `/files` was the standalone Files route before this task. Kept as a redirect, not
         * removed, so nothing that already links or bookmarks it 404s -- see
         * `docs/quick-start.md`/`docs/concepts.md`, which link here by the old path too (updated
         * to the new one in this same change, but a stale bookmark or an external link still
         * lands correctly). */}
        <Route path="files" element={<Navigate to="/transfers/files" replace />} />
        <Route path="transfers">
          <Route index element={<Navigate to="/transfers/queue" replace />} />
          <Route path="queue" element={<TransfersPage />} />
          <Route path="files" element={<FilesPage />} />
          <Route path="disk-review" element={<DiskReviewPage />} />
        </Route>
        <Route path="events" element={<EventsPage />} />
        {/* `/history` was the standalone History route before this task (2026-08-20,
         * docs/transfers-redesign-spec.md §2, phase 1 stage 7): the page it named is now
         * Events, its jobs list dropped since the Queue tab's Complete box already covers
         * "what finished, in what order" (stage 4b). Kept as a redirect, not removed, so nothing
         * that already links or bookmarks it 404s -- the exact `/files` -> `/transfers/files`
         * pattern stage 6 established (see the comment above `Route path="files"` above). */}
        <Route path="history" element={<Navigate to="/events" replace />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="settings">
          <Route index element={<Navigate to="/settings/connection" replace />} />
          <Route path="connection" element={<ConnectionTab />} />
          <Route path="queues" element={<QueuesTab />} />
          <Route path="transfer" element={<TransferTab />} />
          <Route path="post-processing" element={<PostProcessingTab />} />
          <Route path="integrations" element={<IntegrationsTab />} />
          <Route path="clients" element={<ClientsTab />} />
          <Route path="logs" element={<LogsTab />} />
          <Route path="backup" element={<BackupTab />} />
          <Route path="auth" element={<AuthTab />} />
        </Route>
        {/* In-app user documentation (2026-08-13) -- routed exactly like Settings, tabs and
         * all, because it is the second section with more than one page and `nav.ts`'s
         * `tabsForPath` now drives the strip for both. */}
        <Route path="docs">
          <Route index element={<Navigate to="/docs/quick-start" replace />} />
          <Route path="quick-start" element={<QuickStartPage />} />
          <Route path="how-it-works" element={<HowItWorksPage />} />
          <Route path="concepts" element={<ConceptsPage />} />
          <Route path="release-notes" element={<ReleaseNotesPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/transfers/queue" replace />} />
      </Route>
    </Routes>
  )
}

export default App
