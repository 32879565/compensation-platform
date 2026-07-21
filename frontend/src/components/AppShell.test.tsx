import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const auth = vi.hoisted(() => ({
  logout: vi.fn(),
  permissions: ['salary:read'] as string[],
  globalPermissions: [] as string[],
}))

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: {
      username: 'preview_admin',
      permissions: auth.permissions,
      globalPermissions: auth.globalPermissions,
    },
    logout: auth.logout,
    hasPermission: (permission: string) => auth.permissions.includes(permission),
    hasGlobalPermission: (permission: string) => auth.globalPermissions.includes(permission),
  }),
}))

import { AppShell } from './AppShell'

describe('AppShell historical salary navigation', () => {
  beforeEach(() => {
    auth.permissions = ['salary:read']
    auth.globalPermissions = []
    auth.logout.mockReset()
  })

  afterEach(cleanup)

  it('shows a dedicated historical salary menu item for salary readers', () => {
    render(
      <MemoryRouter
        initialEntries={['/salary-history']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <AppShell>
          <div>历史薪资页面</div>
        </AppShell>
      </MemoryRouter>,
    )

    expect(screen.getByTestId('nav-salary-history')).toBeTruthy()
  })

  it('shows both compensation catalog entries with their business names', () => {
    auth.permissions = ['grade:read', 'salary_structure:read']
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <AppShell>
          <div>content</div>
        </AppShell>
      </MemoryRouter>,
    )

    expect(screen.getByTestId('nav-grades').textContent).toBe('职级体系')
    expect(screen.getByTestId('nav-components').textContent).toBe('薪资组件')
  })

  it('shows salary import navigation only to import operators', () => {
    auth.permissions = ['import:run']
    auth.globalPermissions = ['import:run']
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <AppShell>
          <div>content</div>
        </AppShell>
      </MemoryRouter>,
    )

    expect(screen.getByTestId('nav-imports').textContent).toBe('薪酬导入')
  })

  it('hides salary import navigation from locally scoped import operators', () => {
    auth.permissions = ['import:run']
    auth.globalPermissions = []
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <AppShell>
          <div>content</div>
        </AppShell>
      </MemoryRouter>,
    )

    expect(screen.queryByTestId('nav-imports')).toBeNull()
  })

  it('hides salary import navigation without import permission', () => {
    auth.permissions = ['salary:read']
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <AppShell>
          <div>content</div>
        </AppShell>
      </MemoryRouter>,
    )

    expect(screen.queryByTestId('nav-imports')).toBeNull()
  })

  it('handles a rejected logout request without leaking an unhandled promise', async () => {
    auth.logout.mockRejectedValueOnce(new Error('logout transport failed'))
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <AppShell>
          <div>content</div>
        </AppShell>
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByTestId('logout'))
    await waitFor(() => expect(auth.logout).toHaveBeenCalledTimes(1))
    await new Promise((resolve) => setTimeout(resolve, 0))
  })

  it('shows the monthly payroll source ledger only to payroll correctors', () => {
    auth.permissions = ['adjustment:read', 'adjustment:create']
    const view = render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <AppShell>
          <div>content</div>
        </AppShell>
      </MemoryRouter>,
    )
    expect(screen.queryByTestId('nav-payroll-adjustments')).toBeNull()

    view.unmount()
    auth.permissions = ['payroll:correct']
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <AppShell>
          <div>content</div>
        </AppShell>
      </MemoryRouter>,
    )
    expect(screen.getByTestId('nav-payroll-adjustments')).toBeTruthy()
  })
})
