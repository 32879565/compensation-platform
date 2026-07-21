import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const compApi = vi.hoisted(() => ({
  fetchComponents: vi.fn(),
  createComponent: vi.fn(),
  updateComponent: vi.fn(),
}))

vi.mock('../api/comp', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/comp')>()),
  fetchComponents: compApi.fetchComponents,
  createComponent: compApi.createComponent,
  updateComponent: compApi.updateComponent,
}))
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'hr' },
    hasPermission: (permission: string) => permission === 'salary_structure:write',
  }),
}))

import ComponentsPage from './ComponentsPage'

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <ComponentsPage />
    </QueryClientProvider>,
  )
  return { ...rendered, queryClient }
}

describe('ComponentsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    compApi.fetchComponents.mockResolvedValue([
      {
        id: 1,
        code: 'MEAL',
        name: '餐补',
        component_type: 'ALLOWANCE',
        allowance_kind: 'FIXED',
        taxable: true,
        in_social_base: false,
        in_housing_base: false,
        prorate_by_attendance: true,
        sort_order: 0,
      },
    ])
    compApi.updateComponent.mockResolvedValue({})
  })

  afterEach(cleanup)

  it('shows whether an allowance is configured for attendance proration', async () => {
    renderPage()

    const allowanceName = await screen.findByText('餐补')
    expect(screen.getByText('按出勤折算')).toBeTruthy()
    const row = allowanceName.closest('tr')
    expect(row).not.toBeNull()
    expect(within(row as HTMLTableRowElement).getAllByText('是').length).toBeGreaterThan(0)
  })

  it('lets payroll configuration writers update attendance proration', async () => {
    renderPage()

    fireEvent.click(await screen.findByRole('switch', { name: 'MEAL 按出勤折算' }))

    await waitFor(() =>
      expect(compApi.updateComponent).toHaveBeenCalledWith(1, {
        prorate_by_attendance: false,
      }),
    )
  })

  it('keeps the proration toggle pending until the refreshed component list is ready', async () => {
    let resolveInvalidation: (() => void) | undefined
    const { queryClient } = renderPage()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries').mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveInvalidation = resolve
        }),
    )

    const toggle = await screen.findByRole('switch', { name: 'MEAL 按出勤折算' })
    fireEvent.click(toggle)

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['components', 'hr'] }))
    expect((toggle as HTMLButtonElement).disabled).toBe(true)
    if (!resolveInvalidation) throw new Error('component invalidation did not start')
    resolveInvalidation()
    await waitFor(() => expect((toggle as HTMLButtonElement).disabled).toBe(false))
  })

  it('shows a visible failure and restores the controlled toggle when updating fails', async () => {
    compApi.updateComponent.mockRejectedValue(new Error('update failed'))
    renderPage()

    const toggle = await screen.findByRole('switch', { name: 'MEAL 按出勤折算' })
    fireEvent.click(toggle)

    expect(await screen.findByText('更新按出勤折算设置失败')).toBeTruthy()
    expect((toggle as HTMLButtonElement).getAttribute('aria-checked')).toBe('true')
  })

  it('does not treat a failed component read as an empty list or allow creation', async () => {
    compApi.fetchComponents.mockRejectedValue({
      response: { data: { detail: '薪资组件服务暂不可用' } },
    })

    renderPage()

    expect(await screen.findByText('薪资组件加载失败')).toBeTruthy()
    expect(screen.getByText('薪资组件服务暂不可用')).toBeTruthy()
    const create = screen.getByRole('button', { name: '新增组件' })
    expect((create as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(create)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(compApi.createComponent).not.toHaveBeenCalled()
  })
})
