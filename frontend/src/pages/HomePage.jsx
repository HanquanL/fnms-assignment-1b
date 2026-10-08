import { Link } from 'react-router-dom'
import { ArrowRight, CalendarDays, Mail, Radar, Settings, Sparkles, UserRound } from 'lucide-react'
import { useAuth } from '../auth/useAuth.js'
import { Card } from '../components/ui.jsx'

function greeting() {
  const h = new Date().getHours()
  if (h < 12) return 'Good morning'
  if (h < 18) return 'Good afternoon'
  return 'Good evening'
}

function formatDate(iso) {
  return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' })
}

function Info({ icon: Icon, label, value, muted = false }) {
  return (
    <div className="flex items-start gap-3">
      <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-white/5 ring-1 ring-white/10">
        <Icon className="size-4 text-slate-400" />
      </span>
      <div className="min-w-0">
        <dt className="text-xs uppercase tracking-wider text-slate-500">{label}</dt>
        <dd className={`mt-0.5 break-all text-sm ${muted ? 'text-slate-500 italic' : 'text-slate-200'}`}>{value}</dd>
      </div>
    </div>
  )
}

export default function HomePage() {
  const { user } = useAuth()

  return (
    <div className="space-y-8">
      <section className="flex flex-col gap-5 sm:flex-row sm:items-center">
        <div className="grid size-16 shrink-0 place-items-center rounded-2xl bg-linear-to-br from-indigo-500 to-violet-600 text-2xl font-semibold shadow-lg shadow-indigo-500/20">
          {user.username[0].toUpperCase()}
        </div>
        <div>
          <p className="text-sm text-slate-400">{greeting()},</p>
          <h1 className="text-3xl font-semibold tracking-tight">
            Signed in as <span className="text-indigo-300">{user.username}</span>
          </h1>
        </div>
      </section>

      <Card>
        <div className="mb-5 flex items-center justify-between">
          <h2 className="font-medium">Profile</h2>
          <Link
            to="/account"
            className="inline-flex items-center gap-1.5 text-sm text-slate-400 transition hover:text-slate-100"
          >
            <Settings className="size-4" />
            Manage
          </Link>
        </div>
        <dl className="grid gap-5 sm:grid-cols-3">
          <Info icon={UserRound} label="Username" value={user.username} />
          <Info icon={Mail} label="Email" value={user.email ?? 'Not set'} muted={!user.email} />
          <Info icon={CalendarDays} label="Member since" value={formatDate(user.created_at)} />
        </dl>
      </Card>

      <section>
        <h2 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-500">Your toolkit</h2>
        <div className="grid gap-4 sm:grid-cols-3">
          <Link
            to="/tracker"
            className="group flex h-32 flex-col justify-between rounded-2xl bg-indigo-500/10 p-4 ring-1 ring-indigo-400/20 transition hover:bg-indigo-500/15"
          >
            <span className="grid size-9 place-items-center rounded-lg bg-indigo-500/20">
              <Radar className="size-4 text-indigo-200" />
            </span>
            <span>
              <span className="flex items-center gap-1 font-medium text-slate-100">
                Model tracker
                <ArrowRight className="size-4 transition group-hover:translate-x-0.5" />
              </span>
              <span className="text-sm text-slate-400">Top open-weight releases, with sources</span>
            </span>
          </Link>
          {['Slot 2', 'Slot 3'].map((slot) => (
            <div
              key={slot}
              className="grid h-32 place-items-center rounded-2xl border border-dashed border-white/10 text-sm text-slate-500"
            >
              <span className="flex items-center gap-2">
                <Sparkles className="size-4" />
                Coming soon
              </span>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}