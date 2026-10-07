import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { useAuth } from './useAuth.js'

function FullPageSpinner() {
  return (
    <div className="grid min-h-screen place-items-center">
      <Loader2 className="size-6 animate-spin text-slate-500" />
    </div>
  )
}

// Signed-in only. Remembers where you were headed so login can send you back.
export function ProtectedRoute() {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) return <FullPageSpinner />
  if (!user) return <Navigate to="/login" replace state={{ from: location }} />
  return <Outlet />
}

// Signed-out only (login/register). Once a user exists, move on.
export function PublicOnlyRoute() {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) return <FullPageSpinner />
  if (user) return <Navigate to={location.state?.from?.pathname ?? '/'} replace />
  return <Outlet />
}