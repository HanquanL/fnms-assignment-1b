import { Link, NavLink, Outlet } from 'react-router-dom'
import { Command, LogOut } from 'lucide-react'
import { useAuth } from '../auth/useAuth.js'
import { Button, Card } from './ui.jsx'

function Backdrop() {
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 -z-10 overflow-hidden">
      <div className="absolute -top-48 left-1/2 h-[480px] w-[880px] -translate-x-1/2 rounded-full bg-indigo-600/20 blur-3xl" />
      <div className="absolute inset-0 bg-[linear-gradient(to_right,rgba(255,255,255,0.03)_1px,transparent_1px),linear-gradient(to_bottom,rgba(255,255,255,0.03)_1px,transparent_1px)] [background-size:48px_48px] [mask-image:radial-gradient(ellipse_at_top,black,transparent_70%)]" />
    </div>
  )
}

function Logo() {
  return (
    <Link to="/" className="flex items-center gap-2 font-semibold tracking-tight text-slate-100">
      <span className="grid size-8 place-items-center rounded-lg bg-indigo-500/15 ring-1 ring-indigo-400/30">
        <Command className="size-4 text-indigo-300" />
      </span>
      Home Base
    </Link>
  )
}

export function AuthLayout({ title, subtitle, footer, children }) {
  return (
    <div className="relative isolate flex min-h-screen flex-col items-center justify-center px-4 py-12">
      <Backdrop />
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center text-center">
          <Logo />
          <h1 className="mt-8 text-2xl font-semibold tracking-tight">{title}</h1>
          {subtitle && <p className="mt-2 text-sm text-slate-400">{subtitle}</p>}
        </div>
        <Card>{children}</Card>
        {footer && <p className="mt-6 text-center text-sm text-slate-400">{footer}</p>}
      </div>
    </div>
  )
}

export function AppLayout() {
  const { user, logout } = useAuth()
  const linkClass = ({ isActive }) =>
    `rounded-lg px-3 py-1.5 text-sm font-medium transition ${
      isActive ? 'bg-white/10 text-white' : 'text-slate-400 hover:text-white'
    }`

  return (
    <div className="relative isolate min-h-screen">
      <Backdrop />
      <header className="sticky top-0 z-10 border-b border-white/5 bg-slate-950/70 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-5xl items-center gap-4 px-4 sm:gap-6">
          <Logo />
          <nav className="flex gap-1">
            <NavLink to="/" end className={linkClass}>
              Home
            </NavLink>
            <NavLink to="/account" className={linkClass}>
              Account
            </NavLink>
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <span className="hidden text-sm text-slate-400 md:block">@{user.username}</span>
            <Button variant="ghost" onClick={logout} aria-label="Log out">
              <LogOut className="size-4" />
              <span className="hidden sm:inline">Log out</span>
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-10">
        <Outlet />
      </main>
    </div>
  )
}