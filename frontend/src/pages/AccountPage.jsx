import { useState } from 'react'
import { Trash2 } from 'lucide-react'
import { useAuth } from '../auth/useAuth.js'
import { Alert, Button, Field, PasswordField } from '../components/ui.jsx'

function Section({ title, description, danger = false, children }) {
  return (
    <section
      className={`rounded-2xl bg-slate-900/60 p-6 ring-1 backdrop-blur ${danger ? 'ring-red-500/30' : 'ring-white/10'}`}
    >
      <div className="grid gap-6 md:grid-cols-3">
        <div>
          <h2 className={`font-medium ${danger ? 'text-red-300' : ''}`}>{title}</h2>
          <p className="mt-1 text-sm text-slate-400">{description}</p>
        </div>
        <div className="md:col-span-2">{children}</div>
      </div>
    </section>
  )
}

function EmailSection() {
  const { user, updateAccount } = useAuth()
  const [email, setEmail] = useState(user.email ?? '')
  const [status, setStatus] = useState({ kind: 'error', msg: '' })
  const [saving, setSaving] = useState(false)

  const unchanged = email.trim().toLowerCase() === (user.email ?? '')

  async function onSubmit(e) {
    e.preventDefault()
    setStatus({ kind: 'error', msg: '' })
    setSaving(true)
    try {
      const updated = await updateAccount({ email: email.trim() || null })
      setEmail(updated.email ?? '')
      setStatus({ kind: 'success', msg: updated.email ? 'Email updated.' : 'Email removed.' })
    } catch (err) {
      setStatus({ kind: 'error', msg: err.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <Section title="Email" description="You can sign in with it instead of your username. Leave it empty to remove it.">
      <form onSubmit={onSubmit} className="space-y-4">
        <Alert kind={status.kind}>{status.msg}</Alert>
        <Field
          id="email"
          label="Email address"
          type="email"
          autoComplete="email"
          placeholder="you@example.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <div className="flex justify-end">
          <Button type="submit" loading={saving} disabled={unchanged}>
            Save email
          </Button>
        </div>
      </form>
    </Section>
  )
}

function PasswordSection() {
  const { updateAccount } = useAuth()
  const empty = { current: '', next: '', confirm: '' }
  const [form, setForm] = useState(empty)
  const [status, setStatus] = useState({ kind: 'error', msg: '' })
  const [saving, setSaving] = useState(false)

  const update = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }))

  async function onSubmit(e) {
    e.preventDefault()
    setStatus({ kind: 'error', msg: '' })
    if (form.next !== form.confirm) {
      setStatus({ kind: 'error', msg: 'New passwords do not match.' })
      return
    }
    if (form.next === form.current) {
      setStatus({ kind: 'error', msg: 'New password must be different from the current one.' })
      return
    }
    setSaving(true)
    try {
      await updateAccount({ password: form.next, current_password: form.current })
      setForm(empty)
      setStatus({ kind: 'success', msg: 'Password changed.' })
    } catch (err) {
      setStatus({ kind: 'error', msg: err.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <Section title="Password" description="Confirm your current password to set a new one.">
      <form onSubmit={onSubmit} className="space-y-4">
        <Alert kind={status.kind}>{status.msg}</Alert>
        <PasswordField
          id="current-password"
          label="Current password"
          autoComplete="current-password"
          required
          value={form.current}
          onChange={update('current')}
        />
        <div className="grid gap-4 sm:grid-cols-2">
          <PasswordField
            id="new-password"
            label="New password"
            autoComplete="new-password"
            required
            minLength={8}
            maxLength={128}
            hint="At least 8 characters."
            value={form.next}
            onChange={update('next')}
          />
          <PasswordField
            id="confirm-password"
            label="Confirm new password"
            autoComplete="new-password"
            required
            value={form.confirm}
            onChange={update('confirm')}
          />
        </div>
        <div className="flex justify-end">
          <Button type="submit" loading={saving}>
            Change password
          </Button>
        </div>
      </form>
    </Section>
  )
}

function DangerZone() {
  const { user, deleteAccount } = useAuth()
  const [open, setOpen] = useState(false)
  const [confirmText, setConfirmText] = useState('')
  const [error, setError] = useState('')
  const [deleting, setDeleting] = useState(false)

  function cancel() {
    setOpen(false)
    setConfirmText('')
    setError('')
  }

  async function onDelete() {
    setError('')
    setDeleting(true)
    try {
      await deleteAccount()
      // user is now null, so ProtectedRoute sends us to /login
    } catch (err) {
      setError(err.message)
      setDeleting(false)
    }
  }

  return (
    <Section
      danger
      title="Delete account"
      description="Permanently delete your account and everything in it. This cannot be undone."
    >
      {!open ? (
        <Button variant="danger" onClick={() => setOpen(true)}>
          <Trash2 className="size-4" />
          Delete account
        </Button>
      ) : (
        <div className="space-y-4">
          <Alert>{error}</Alert>
          <Field
            id="confirm-delete"
            label={
              <>
                Type <span className="font-mono text-red-300">{user.username}</span> to confirm
              </>
            }
            autoComplete="off"
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
          />
          <div className="flex justify-end gap-3">
            <Button variant="secondary" onClick={cancel}>
              Cancel
            </Button>
            <Button variant="danger" loading={deleting} disabled={confirmText !== user.username} onClick={onDelete}>
              Permanently delete
            </Button>
          </div>
        </div>
      )}
    </Section>
  )
}

export default function AccountPage() {
  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight">Account</h1>
        <p className="mt-1 text-slate-400">Manage your email, password, and account.</p>
      </header>
      <EmailSection />
      <PasswordSection />
      <DangerZone />
    </div>
  )
}