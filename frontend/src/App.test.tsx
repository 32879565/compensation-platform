import { cleanup, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const auth = vi.hoisted(() => ({
  permissions: [] as string[],
  globalPermissions: [] as string[],
}))

vi.mock('./auth/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: () => ({
    user: {
      username: 'route-user',
      permissions: auth.permissions,
      globalPermissions: auth.globalPermissions,
    },
    loading: false,
    hasPermission: (permission: string) => auth.permissions.includes(permission),
    hasGlobalPermission: (permission: string) => auth.globalPermissions.includes(permission),
  }),
}))
vi.mock('./auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: { children: ReactNode }) => children,
}))
vi.mock('./components/AppShell', () => ({
  AppShell: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}))
vi.mock('./pages/DashboardPage', () => ({ default: () => <div>dashboard-page</div> }))
vi.mock('./pages/EmployeesPage', () => ({ default: () => <div>employees-page</div> }))
vi.mock('./pages/SalaryHistoryPage', () => ({ default: () => <div>salary-history-page</div> }))
vi.mock('./pages/PayrollPage', () => ({ default: () => <div>payroll-page</div> }))
vi.mock('./pages/AdjustmentPage', () => ({ default: () => <div>adjustment-page</div> }))
vi.mock('./pages/ImportsPage', () => ({ default: () => <div>imports-page</div> }))

import App from './App'

describe('permission-aware application routes', () => {
  afterEach(() => {
    cleanup()
    auth.globalPermissions = []
    window.history.replaceState({}, '', '/')
  })

  it('redirects the home route to the first module the user can actually access', async () => {
    auth.permissions = ['payroll:read']
    window.history.replaceState({}, '', '/')

    render(<App />)

    expect(await screen.findByText('payroll-page')).toBeTruthy()
    await waitFor(() => expect(window.location.pathname).toBe('/payroll'))
  })

  it('blocks a direct URL without permission and redirects to the first accessible module', async () => {
    auth.permissions = ['salary:read']
    window.history.replaceState({}, '', '/employees')

    render(<App />)

    expect(await screen.findByText('salary-history-page')).toBeTruthy()
    await waitFor(() => expect(window.location.pathname).toBe('/salary-history'))
  })

  it('shows a stable no-access page when an authenticated user has no module permission', async () => {
    auth.permissions = []
    window.history.replaceState({}, '', '/')

    render(<App />)

    expect(await screen.findByText('当前账号没有可访问的功能模块')).toBeTruthy()
    await waitFor(() => expect(window.location.pathname).toBe('/no-access'))
  })

  it('does not expose monthly payroll corrections through generic adjustment permissions', async () => {
    auth.permissions = ['adjustment:create']
    window.history.replaceState({}, '', '/payroll-adjustments')

    render(<App />)

    expect(await screen.findByText('adjustment-page')).toBeTruthy()
    await waitFor(() => expect(window.location.pathname).toBe('/adjustment'))
  })

  it('allows import operators to open the salary import route', async () => {
    auth.permissions = ['import:run']
    auth.globalPermissions = ['import:run']
    window.history.replaceState({}, '', '/imports')

    render(<App />)

    expect(await screen.findByText('imports-page')).toBeTruthy()
    await waitFor(() => expect(window.location.pathname).toBe('/imports'))
  })

  it('blocks the salary import route when import permission is only locally scoped', async () => {
    auth.permissions = ['import:run', 'salary:read']
    auth.globalPermissions = []
    window.history.replaceState({}, '', '/imports')

    render(<App />)

    expect(await screen.findByText('salary-history-page')).toBeTruthy()
    await waitFor(() => expect(window.location.pathname).toBe('/salary-history'))
  })

  it('blocks the salary import route without import permission', async () => {
    auth.permissions = ['salary:read']
    window.history.replaceState({}, '', '/imports')

    render(<App />)

    expect(await screen.findByText('salary-history-page')).toBeTruthy()
    await waitFor(() => expect(window.location.pathname).toBe('/salary-history'))
  })
})
