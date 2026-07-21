import axios, { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  api,
  getAccessToken,
  setAccessToken,
  subscribeAuthSessionExpired,
} from './client'

async function rejectUnauthorized(config: InternalAxiosRequestConfig): Promise<never> {
  const requestConfig = config as InternalAxiosRequestConfig
  throw new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', requestConfig, undefined, {
    data: {},
    status: 401,
    statusText: 'Unauthorized',
    headers: new AxiosHeaders(),
    config: requestConfig,
  })
}

function requestProtectedResource() {
  return api.request({
    method: 'get',
    url: '/api/payroll/results',
    adapter: rejectUnauthorized,
  })
}

describe('access token store', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    setAccessToken(null)
  })

  it('设置与读取 token', () => {
    setAccessToken('abc')
    expect(getAccessToken()).toBe('abc')
  })

  it('清空 token', () => {
    setAccessToken('abc')
    setAccessToken(null)
    expect(getAccessToken()).toBeNull()
  })

  it('notifies the auth boundary when a 401 refresh fails', async () => {
    vi.spyOn(axios, 'post').mockRejectedValueOnce(new Error('refresh failed'))
    const onSessionExpired = vi.fn()
    const unsubscribe = subscribeAuthSessionExpired(onSessionExpired)
    setAccessToken('expired-token')

    await expect(requestProtectedResource()).rejects.toBeInstanceOf(AxiosError)

    expect(getAccessToken()).toBeNull()
    expect(onSessionExpired).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('expires the session when refresh returns no usable access token', async () => {
    vi.spyOn(axios, 'post').mockResolvedValueOnce({ data: {} })
    const onSessionExpired = vi.fn()
    const unsubscribe = subscribeAuthSessionExpired(onSessionExpired)
    setAccessToken('expired-token')

    await expect(requestProtectedResource()).rejects.toBeInstanceOf(AxiosError)

    expect(getAccessToken()).toBeNull()
    expect(onSessionExpired).toHaveBeenCalledTimes(1)
    unsubscribe()
  })
})
