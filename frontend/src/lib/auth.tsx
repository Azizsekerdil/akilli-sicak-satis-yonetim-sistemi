/**
 * Authentication context.
 *
 * Holds the signed-in user plus the permission set the backend computed, and
 * exposes `can(resource, action)` so screens and menu items can hide what the
 * user may not touch.  The server enforces the same rules independently — this
 * is for usability, never for security.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { api, ApiError, langStore, onUnauthorized, tokens, type Lang } from './api'

export interface Role {
  id: number
  code: string
  name_tr: string
  name_en: string
  data_scope: string
  rank: number
}

export interface User {
  id: number
  username: string
  full_name: string
  email?: string | null
  phone?: string | null
  status: string
  language: Lang
  theme?: string
  region_id?: number | null
  must_change_password: boolean
  last_login_at?: string | null
  role?: Role | null
}

export interface Session {
  user: User
  permissions: string[]
  modules: string[]
  resources: string[]
  data_scope: string
  role: string | null
  role_rank: number
  salesperson_id: number | null
  language: Lang
}

interface AuthState {
  session: Session | null
  loading: boolean
  error: string | null
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
  can: (resource: string, action?: string) => boolean
  canAny: (...pairs: [string, string][]) => boolean
  hasModule: (module: string) => boolean
  isAdmin: boolean
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const loadSession = useCallback(async () => {
    if (!tokens.access) {
      setSession(null)
      setLoading(false)
      return
    }
    try {
      const s = await api.get<Session>('/auth/me')
      setSession(s)
      if (s.language) langStore.set(s.language)
    } catch {
      tokens.clear()
      setSession(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadSession()
    // A refresh failure anywhere in the app drops us back to the login screen.
    return onUnauthorized(() => setSession(null))
  }, [loadSession])

  const login = useCallback(async (username: string, password: string) => {
    setError(null)
    try {
      const res = await api.post<{
        tokens: { access_token: string; refresh_token: string }
        session: Session
      }>('/auth/login', {
        username,
        password,
        device_label: navigator.userAgent.slice(0, 120),
      })
      tokens.set(res.tokens.access_token, res.tokens.refresh_token)
      setSession(res.session)
      if (res.session.language) langStore.set(res.session.language)
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e)
      setError(msg)
      throw e
    }
  }, [])

  const logout = useCallback(async () => {
    try {
      await api.post('/auth/logout', { refresh_token: tokens.refresh })
    } catch {
      // Signing out locally must succeed even if the server call does not.
    }
    tokens.clear()
    setSession(null)
  }, [])

  const permissionSet = useMemo(
    () => new Set(session?.permissions ?? []),
    [session?.permissions],
  )

  const can = useCallback(
    (resource: string, action = 'VIEW') => permissionSet.has(`${resource}:${action}`),
    [permissionSet],
  )

  const canAny = useCallback(
    (...pairs: [string, string][]) => pairs.some(([r, a]) => permissionSet.has(`${r}:${a}`)),
    [permissionSet],
  )

  const hasModule = useCallback(
    (m: string) => (session?.modules ?? []).includes(m),
    [session?.modules],
  )

  const value: AuthState = {
    session,
    loading,
    error,
    login,
    logout,
    refresh: loadSession,
    can,
    canAny,
    hasModule,
    isAdmin: session?.role === 'SYSTEM_ADMIN' || session?.role === 'COMPANY_OWNER',
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
  return ctx
}
