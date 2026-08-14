import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { FilesPage } from './pages/FilesPage'
import { TransfersPage } from './pages/TransfersPage'
import { HistoryPage } from './pages/HistoryPage'
import { DashboardPage } from './pages/DashboardPage'
import { LoginPage } from './pages/LoginPage'
import { ConnectionTab } from './pages/settings/ConnectionTab'
import { QueuesTab } from './pages/settings/QueuesTab'
import { TransferTab } from './pages/settings/TransferTab'
import { PostProcessingTab } from './pages/settings/PostProcessingTab'
import { LogsTab } from './pages/settings/LogsTab'
import { BackupTab } from './pages/settings/BackupTab'
import { AuthTab } from './pages/settings/AuthTab'
import { QuickStartPage } from './pages/docs/QuickStartPage'
import { ConceptsPage } from './pages/docs/ConceptsPage'
import { HowItWorksPage } from './pages/docs/HowItWorksPage'
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
        <Route index element={<Navigate to="/files" replace />} />
        <Route path="files" element={<FilesPage />} />
        <Route path="transfers" element={<TransfersPage />} />
        <Route path="history" element={<HistoryPage />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="settings">
          <Route index element={<Navigate to="/settings/connection" replace />} />
          <Route path="connection" element={<ConnectionTab />} />
          <Route path="queues" element={<QueuesTab />} />
          <Route path="transfer" element={<TransferTab />} />
          <Route path="post-processing" element={<PostProcessingTab />} />
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
        </Route>
        <Route path="*" element={<Navigate to="/files" replace />} />
      </Route>
    </Routes>
  )
}

export default App
