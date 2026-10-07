import { useState } from 'react'
import { AlertCircle, CheckCircle2, Eye, EyeOff, Loader2 } from 'lucide-react'

export function Button({ variant = 'primary', loading = false, className = '', children, disabled, ...props }) {
  const base =
    'inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2.5 text-sm font-medium transition ' +
    'focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-60'
  const variants = {
    primary: 'bg-indigo-500 text-white hover:bg-indigo-400',
    secondary: 'bg-white/5 text-slate-200 ring-1 ring-inset ring-white/10 hover:bg-white/10',
    danger: 'bg-red-500/90 text-white hover:bg-red-500',
    ghost: 'text-slate-400 hover:bg-white/5 hover:text-slate-100',
  }
  return (
    <button className={`${base} ${variants[variant]} ${className}`} disabled={loading || disabled} {...props}>
      {loading && <Loader2 className="size-4 animate-spin" />}
      {children}
    </button>
  )
}

export function Field({ label, id, hint, optional = false, trailing, className = '', ...props }) {
  return (
    <div className={`space-y-1.5 ${className}`}>
      <div className="flex items-baseline justify-between">
        <label htmlFor={id} className="block text-sm font-medium text-slate-300">
          {label}
        </label>
        {optional && <span className="text-xs text-slate-500">Optional</span>}
      </div>
      <div className="relative">
        <input
          id={id}
          className={
            'block w-full rounded-lg border-0 bg-white/5 px-3.5 py-2.5 text-slate-100 ring-1 ring-inset ring-white/10 ' +
            'placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-400 ' +
            (trailing ? 'pr-10' : '')
          }
          {...props}
        />
        {trailing && <div className="absolute inset-y-0 right-0 flex items-center pr-3">{trailing}</div>}
      </div>
      {hint && <p className="text-xs text-slate-500">{hint}</p>}
    </div>
  )
}

export function PasswordField(props) {
  const [show, setShow] = useState(false)
  return (
    <Field
      {...props}
      type={show ? 'text' : 'password'}
      trailing={
        <button
          type="button"
          onClick={() => setShow((s) => !s)}
          className="text-slate-500 hover:text-slate-300"
          aria-label={show ? 'Hide password' : 'Show password'}
        >
          {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
        </button>
      }
    />
  )
}

export function Alert({ kind = 'error', children }) {
  if (!children) return null
  const styles = {
    error: 'bg-red-500/10 text-red-300 ring-red-500/20',
    success: 'bg-emerald-500/10 text-emerald-300 ring-emerald-500/20',
  }
  const Icon = kind === 'error' ? AlertCircle : CheckCircle2
  return (
    <div role="alert" className={`flex items-start gap-2 rounded-lg px-3.5 py-3 text-sm ring-1 ring-inset ${styles[kind]}`}>
      <Icon className="mt-0.5 size-4 shrink-0" />
      <span>{children}</span>
    </div>
  )
}

export function Card({ className = '', children }) {
  return <div className={`rounded-2xl bg-slate-900/60 p-6 ring-1 ring-white/10 backdrop-blur ${className}`}>{children}</div>
}