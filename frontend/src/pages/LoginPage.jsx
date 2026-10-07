import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../auth/useAuth.js'
import { AuthLayout } from '../components/layouts.jsx'
import { Alert, Button, Field, PasswordField } from '../components/ui.jsx'

export default function LoginPage() {
  const { login } = useAuth()
  const [form, setForm] = useState({ identifier: '', password: '' })
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const update = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }))

  async function onSubmit(e) {
    e.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      await login(form.identifier, form.password)
      // PublicOnlyRoute redirects as soon as the user is set
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  return (
    <AuthLayout
      title="Welcome back"
      subtitle="Sign in to your home base."
      footer={
        <>
          New here?{' '}
          <Link to="/register" className="font-medium text-indigo-300 hover:text-indigo-200">
            Create an account
          </Link>
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-5">
        <Alert>{error}</Alert>
        <Field
          id="identifier"
          label="Username or email"
          autoComplete="username"
          required
          autoFocus
          value={form.identifier}
          onChange={update('identifier')}
        />
        <PasswordField
          id="password"
          label="Password"
          autoComplete="current-password"
          required
          value={form.password}
          onChange={update('password')}
        />
        <Button type="submit" loading={submitting} className="w-full">
          Sign in
        </Button>
      </form>
    </AuthLayout>
  )
}