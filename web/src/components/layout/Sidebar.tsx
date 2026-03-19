import { NavLink } from 'react-router-dom'
import { cn } from '../../lib/utils'
import { useT } from '../../lib/i18n'
import {
  LayoutDashboard,
  Briefcase,
  Camera,
  HardDrive,
  Shield,
  Calendar,
  Activity,
  Settings,
  Monitor,
  Bot,
} from 'lucide-react'

export default function Sidebar() {
  const { t } = useT()

  const nav = [
    { to: '/',          label: t.nav.dashboard,   icon: LayoutDashboard, end: true },
    { to: '/jobs',      label: t.nav.jobs,        icon: Briefcase },
    { to: '/snapshots', label: t.nav.snapshots,   icon: Camera },
    { to: '/storage',   label: t.nav.storage,     icon: HardDrive },
    { to: '/scheduler', label: t.nav.scheduler,   icon: Calendar },
    { to: '/machines',  label: t.nav.machines,    icon: Monitor },
    { to: '/agents',    label: t.nav.agents,      icon: Bot },
    { to: '/activity',  label: t.nav.activityLog, icon: Activity },
    { to: '/settings',  label: t.nav.settings,    icon: Settings },
  ]

  return (
    <aside className="flex h-full w-60 flex-col border-r bg-card">
      {/* Brand */}
      <div className="flex h-16 items-center gap-2 border-b px-6">
        <Shield className="h-5 w-5 text-primary" />
        <span className="text-lg font-bold tracking-tight">Sentinel</span>
      </div>

      {/* Navigation */}
      <nav className="flex-1 space-y-1 p-4">
        {nav.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              cn(
                'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                isActive
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
              )
            }
          >
            <Icon className="h-4 w-4" />
            {label}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="border-t p-4">
        <p className="text-xs text-muted-foreground">{t.nav.sprintVersion}</p>
      </div>
    </aside>
  )
}
