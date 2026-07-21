import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiClient = vi.hoisted(() => {
  const state: { listener?: () => void } = {}
  return {
    api: { post: vi.fn() },
    setAccessToken: vi.fn(),
    state,
    subscribeAuthSessionExpired: vi.fn((listener: () => void) => {
      state.listener = listener
      return () => {
        if (state.listener === listener) state.listener = undefined
      }
    }),
  }
})

vi.mock('../api/client', () => apiClient)

import { clearSessionQueries, queryClient } from '../queryClient'
import { AuthProvider, useAuth } from './AuthContext'

function AuthProbe() {
  const { user, hasGlobalPermission } = useAuth()
  return (
    <span>
      {user?.username ?? 'guest'}:
      {hasGlobalPermission('payroll:run') ? 'global-run' : 'no-global-run'}
    </span>
  )
}

describe('AuthProvider session expiry', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    clearSessionQueries()
    apiClient.state.listener = undefined
    apiClient.api.post.mockResolvedValue({
      status: 200,
      data: {
        access_token: 'fresh-token',
        username: 'alice',
        permissions: ['payroll:read'],
        global_permissions: ['payroll:run'],
      },
    })
  })

  afterEach(() => {
    cleanup()
    clearSessionQueries()
  })

  it('clears the principal and salary queries after an interceptor refresh failure', async () => {
    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    )

    expect(await screen.findByText('alice:global-run')).toBeTruthy()
    queryClient.setQueryData(
      ['payrollResults', 'alice', 17],
      [{ employee_name: 'sensitive payroll' }],
    )

    act(() => apiClient.state.listener?.())

    await waitFor(() => expect(screen.getByText('guest:no-global-run')).toBeTruthy())
    expect(queryClient.getQueryData(['payrollResults', 'alice', 17])).toBeUndefined()
  })

  it('fails closed when an older backend omits permission-level global scope', async () => {
    apiClient.api.post.mockResolvedValueOnce({
      status: 200,
      data: {
        access_token: 'rolling-deploy-token',
        username: 'alice',
        permissions: ['payroll:run'],
      },
    })

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    )

    expect(await screen.findByText('alice:no-global-run')).toBeTruthy()
  })
})
