import { useState } from 'react'
import type { FormEvent } from 'react'
import { useAuth } from '../hooks/authContext'

/** Rendered instead of the whole routed app (`App.tsx`) whenever `AUTH_MODE=password` and
 * there's no valid session — never click-tested (no browser in this build environment, see
 * docs/decisions.md), but exercised end to end over real HTTP via `tests/test_auth_api.py`.
 */
export function LoginPage() {
  const { login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(username, password)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'login failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-white p-4 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 rounded-lg border border-zinc-200 p-6 shadow-sm dark:border-zinc-800"
      >
        <h1 className="text-lg font-semibold">lftpweb</h1>
        {error && (
          <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
            {error}
          </p>
        )}
        <div className="space-y-1">
          <label className="block text-sm text-zinc-600 dark:text-zinc-400" htmlFor="login-username">
            Username
          </label>
          <input
            id="login-username"
            className="w-full rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </div>
        <div className="space-y-1">
          <label className="block text-sm text-zinc-600 dark:text-zinc-400" htmlFor="login-password">
            Password
          </label>
          <input
            id="login-password"
            type="password"
            className="w-full rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </div>
        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-md bg-zinc-900 px-3 py-2 text-sm font-medium text-white transition-opacity disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
        >
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
