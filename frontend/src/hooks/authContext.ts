import { createContext, useContext } from 'react'
import type { AuthSessionOut } from '../api/types'

export interface AuthContextValue {
  /** `null` only while the initial `GET /api/auth/session` is in flight. */
  session: AuthSessionOut | null
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

// Split out of `useAuth.tsx` so that file exports only the `AuthProvider` component --
// oxlint's react/only-export-components flags a .tsx file that exports both a component and
// plain values/hooks, since Vite's Fast Refresh can't hot-reload it cleanly otherwise.
export const AuthContext = createContext<AuthContextValue | null>(null)

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
