import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { FilesPage } from './pages/FilesPage'
import { TransfersPage } from './pages/TransfersPage'
import { HistoryPage } from './pages/HistoryPage'
import { ConnectionTab } from './pages/settings/ConnectionTab'
import { QueuesTab } from './pages/settings/QueuesTab'
import { TransferTab } from './pages/settings/TransferTab'
import { PostProcessingTab } from './pages/settings/PostProcessingTab'
import { LogsTab } from './pages/settings/LogsTab'
import { BackupTab } from './pages/settings/BackupTab'
import { AuthTab } from './pages/settings/AuthTab'

function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/files" replace />} />
        <Route path="files" element={<FilesPage />} />
        <Route path="transfers" element={<TransfersPage />} />
        <Route path="history" element={<HistoryPage />} />
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
        <Route path="*" element={<Navigate to="/files" replace />} />
      </Route>
    </Routes>
  )
}

export default App
