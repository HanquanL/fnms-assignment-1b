import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../auth/useAuth.js'
import { AuthLayout } from '../components/layouts.jsx'
import { Alert, Button, Field, PasswordField } from '../components/ui.jsx'

export default function RegisterPage() {
  const { register } = useAuth()
  const [form, setForm] = useState({ username: '', email: '', password: '', confirm: '' })
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const update = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }))

  async function onSubmit(e) {
    e.preventDefault()
    setError('')
    if (form.password !== form.confirm) {
      setError('Passwords do not match.')
      return
    }
    setSubmitting(true)
    try {
      await register({
        username: form.username.trim(),
        email: form.email.trim(),
        password: form.password,
      })
      // Registered + signed in; PublicOnlyRoute takes it from here
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  return (
    <AuthLayout
      title="Create your account"
      subtitle="Set up your home base in a few seconds."
      footer={
        <>
          Already have an account?{' '}
          <Link to="/login" className="font-medium text-indigo-300 hover:text-indigo-200">
            Sign in
          </Link>
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-5">
        <Alert>{error}</Alert>
        <Field
          id="username"
          label="Username"
          autoComplete="username"
          required
          autoFocus
          minLength={3}
          maxLength={32}
          pattern="[A-Za-z0-9_.\-]+"
          title="3-32 characters: letters, numbers, _ . -"
          hint="3-32 characters: letters, numbers, _ . -"
          value={form.username}
          onChange={update('username')}
        />
        <Field
          id="email"
          label="Email"
          type="email"
          autoComplete="email"
          optional
          value={form.email}
          onChange={update('email')}
        />
        <PasswordField
          id="password"
          label="Password"
          autoComplete="new-password"
          required
          minLength={8}
          maxLength={128}
          hint="At least 8 characters."
          value={form.password}
          onChange={update('password')}
        />
        <PasswordField
          id="confirm"
          label="Confirm password"
          autoComplete="new-password"
          required
          value={form.confirm}
          onChange={update('confirm')}
        />
        <Button type="submit" loading={submitting} className="w-full">
          Create account
        </Button>
      </form>
    </AuthLayout>
  )
}