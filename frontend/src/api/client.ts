import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios'

// 内存中的 access token（不落 localStorage，降低 XSS 窃取风险）。
let accessToken: string | null = null
type SessionExpiredListener = () => void
const sessionExpiredListeners = new Set<SessionExpiredListener>()

export function setAccessToken(token: string | null): void {
  accessToken = token
}

export function getAccessToken(): string | null {
  return accessToken
}

export function subscribeAuthSessionExpired(listener: SessionExpiredListener): () => void {
  sessionExpiredListeners.add(listener)
  return () => sessionExpiredListeners.delete(listener)
}

function notifyAuthSessionExpired(): void {
  for (const listener of sessionExpiredListeners) listener()
}

export const api = axios.create({ baseURL: '/', withCredentials: true })

api.interceptors.request.use((config) => {
  if (accessToken) {
    config.headers.Authorization = `Bearer ${accessToken}`
  }
  return config
})

interface RefreshResponse {
  access_token: string
}

// 单飞刷新：并发 401 只触发一次 refresh
let refreshing: Promise<string | null> | null = null

async function refreshAccessToken(): Promise<string | null> {
  if (!refreshing) {
    refreshing = axios
      .post<RefreshResponse>('/api/auth/refresh', null, { withCredentials: true })
      .then((r) => {
        const token = r.data?.access_token
        if (typeof token !== 'string' || !token.trim()) {
          throw new Error('Refresh response did not contain an access token')
        }
        setAccessToken(token)
        return token
      })
      .catch(() => {
        setAccessToken(null)
        notifyAuthSessionExpired()
        return null
      })
      .finally(() => {
        refreshing = null
      })
  }
  return refreshing
}

api.interceptors.response.use(
  (resp) => resp,
  async (error: AxiosError) => {
    const original = error.config as InternalAxiosRequestConfig & { _retried?: boolean }
    const isAuthEndpoint = original?.url?.includes('/api/auth/')
    if (error.response?.status === 401 && original && !original._retried && !isAuthEndpoint) {
      original._retried = true
      const token = await refreshAccessToken()
      if (token) {
        original.headers.Authorization = `Bearer ${token}`
        return api(original)
      }
    }
    return Promise.reject(error)
  },
)
