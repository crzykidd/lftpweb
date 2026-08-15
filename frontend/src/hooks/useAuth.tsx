import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { getAuthSession, login as apiLogin, logout as apiLogout, setCsrfToken } from '../api/client'
import type { AuthSessionOut } from '../api/types'
import { AuthContext } from './authContext'

/** Wraps the whole app (`main.tsx`). Fetches `GET /api/auth/session` once on mount so
 * `App.tsx` can decide whether to render the login page at all — this is deliberately a
 * one-shot fetch, not a poll: a session going stale mid-visit surfaces the next time any
 * mutating call gets a 401/403, at which point the user is asked to sign in again rather
 * than being silently bounced out from under them by a background timer.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSessionOut | null>(null)

  const refresh = useCallback(async () => {
    try {
      const result = await getAuthSession()
      setSession(result)
      setCsrfToken(result.csrf_token)
    } catch {
      // The backend not answering yet (container still starting) reads as "not
      // authenticated" rather than crashing the shell — a reload once it's up sorts itself
      // out, and `mode: 'none'` here is just a safe placeholder, not an actual mode read.
      setSession({ mode: 'none', authenticated: false, username: null, csrf_token: null })
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const login = useCallback(async (username: string, password: string) => {
    const result = await apiLogin({ username, password })
    setSession(result)
    setCsrfToken(result.csrf_token)
  }, [])

  const logout = useCallback(async () => {
    try {
      await apiLogout()
    } finally {
      setCsrfToken(null)
      await refresh()
    }
  }, [refresh])

  return (
    <AuthContext.Provider value={{ session, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  )
}
