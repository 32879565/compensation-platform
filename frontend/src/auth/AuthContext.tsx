import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { api, setAccessToken, subscribeAuthSessionExpired } from '../api/client'
import { clearSessionQueries } from '../queryClient'
import type { AuthUser, LoginResponse } from './types'

interface AuthState {
  user: AuthUser | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  hasPermission: (perm: string) => boolean
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(
    () =>
      subscribeAuthSessionExpired(() => {
        clearSessionQueries()
        setUser(null)
        setLoading(false)
      }),
    [],
  )

  // 首次挂载尝试用 refresh cookie 静默恢复会话
  useEffect(() => {
    let active = true
    async function restore() {
      try {
        const r = await api.post<LoginResponse | null>('/api/auth/refresh')
        // 204 / 无 cookie → 游客，不报错
        if (r.status === 204 || !r.data?.access_token) {
          if (active) {
            clearSessionQueries()
            setUser(null)
          }
        } else {
          setAccessToken(r.data.access_token)
          if (active) {
            clearSessionQueries()
            setUser({ username: r.data.username, permissions: r.data.permissions })
          }
        }
      } catch {
        if (active) {
          clearSessionQueries()
          setUser(null)
        }
      } finally {
        if (active) setLoading(false)
      }
    }
    void restore()
    return () => {
      active = false
    }
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    // Do this before the request resolves: the login page can return to a
    // protected route immediately after success, so no previous principal's
    // cached salary data may be synchronously rendered there.
    clearSessionQueries()
    const r = await api.post<LoginResponse>('/api/auth/login', { username, password })
    setAccessToken(r.data.access_token)
    setUser({ username: r.data.username, permissions: r.data.permissions })
  }, [])

  const logout = useCallback(async () => {
    clearSessionQueries()
    try {
      await api.post('/api/auth/logout')
    } finally {
      setAccessToken(null)
      setUser(null)
      clearSessionQueries()
    }
  }, [])

  const hasPermission = useCallback(
    (perm: string) => user?.permissions.includes(perm) ?? false,
    [user],
  )

  const value = useMemo(
    () => ({ user, loading, login, logout, hasPermission }),
    [user, loading, login, logout, hasPermission],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用')
  return ctx
}
