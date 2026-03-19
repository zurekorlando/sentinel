import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import AppLayout from './components/layout/AppLayout'
import Dashboard from './pages/Dashboard'
import Jobs from './pages/Jobs'
import Snapshots from './pages/Snapshots'
import Storage from './pages/Storage'
import Scheduler from './pages/Scheduler'
import ActivityLog from './pages/ActivityLog'
import Settings from './pages/Settings'
import Machines from './pages/Machines'
import Agents from './pages/Agents'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={<Dashboard />} />
          <Route path="jobs" element={<Jobs />} />
          <Route path="snapshots" element={<Snapshots />} />
          <Route path="storage" element={<Storage />} />
          <Route path="scheduler" element={<Scheduler />} />
          <Route path="machines" element={<Machines />} />
          <Route path="agents" element={<Agents />} />
          <Route path="activity" element={<ActivityLog />} />
          <Route path="settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
