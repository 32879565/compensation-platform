import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const compApi = vi.hoisted(() => ({
  fetchComponents: vi.fn(),
  createComponent: vi.fn(),
  updateComponent: vi.fn(),
  deactivateComponent: vi.fn(),
  restoreComponent: vi.fn(),
}))
const auth = vi.hoisted(() => ({ permissions: ['salary_structure:write'] as string[] }))
const legacyReview = vi.hoisted(() => ({ onApplied: null as (() => void) | null }))

vi.mock('../api/comp', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/comp')>()),
  fetchComponents: compApi.fetchComponents,
  createComponent: compApi.createComponent,
  updateComponent: compApi.updateComponent,
  deactivateComponent: compApi.deactivateComponent,
  restoreComponent: compApi.restoreComponent,
}))
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'hr' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))
vi.mock('../components/LegacyCatalogReviewDrawer', () => ({
  default: ({
    open,
    mode,
    onApplied,
  }: {
    open: boolean
    mode: string
    onApplied: () => void
  }) => {
    legacyReview.onApplied = onApplied
    return open ? (
      <div role="dialog" aria-label={`旧系统真实数据-${mode}`}>
        <button onClick={onApplied}>模拟应用真实数据</button>
      </div>
    ) : null
  },
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

async function submitLifecycleAction(row: HTMLElement, name: '停用' | '恢复', reason: string) {
  fireEvent.click(within(row).getByRole('button', { name }))
  const dialog = await screen.findByRole('dialog', { name: `${name}薪资组件` })
  fireEvent.change(within(dialog).getByLabelText(`${name}原因`), { target: { value: reason } })
  fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定|确认/i }))
}

describe('ComponentsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.permissions = ['salary_structure:write']
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
        is_active: true,
        deactivated_at: null,
        updated_at: '2026-07-21T05:00:00Z',
        calculation_locked: false,
        calculation_lock_reason: null,
      },
    ])
    compApi.updateComponent.mockResolvedValue({})
    compApi.deactivateComponent.mockResolvedValue({})
    compApi.restoreComponent.mockResolvedValue({})
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

  it('opens the reviewed legacy catalog only for import-capable writers and refreshes after apply', async () => {
    auth.permissions = ['salary_structure:write', 'import:run']
    const { queryClient } = renderPage()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    await screen.findByText('餐补')
    fireEvent.click(screen.getByRole('button', { name: '审阅旧系统真实数据' }))

    expect(screen.getByRole('dialog', { name: '旧系统真实数据-components' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '模拟应用真实数据' }))
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['components', 'hr'] }),
    )
    expect(screen.queryByRole('dialog', { name: '旧系统真实数据-components' })).toBeNull()
  })

  it('does not expose legacy catalog creation to import-only users', async () => {
    auth.permissions = ['import:run']
    renderPage()

    await screen.findByText('餐补')
    expect(screen.queryByRole('button', { name: '审阅旧系统真实数据' })).toBeNull()
  })

  it('lets payroll configuration writers update attendance proration', async () => {
    renderPage()

    fireEvent.click(await screen.findByRole('switch', { name: 'MEAL 按出勤折算' }))

    await waitFor(() =>
      expect(compApi.updateComponent).toHaveBeenCalledWith(1, {
        prorate_by_attendance: false,
        expected_updated_at: '2026-07-21T05:00:00Z',
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

  it('filters active and inactive components without mixing their lifecycle states', async () => {
    const active = {
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
      is_active: true,
      deactivated_at: null,
      updated_at: '2026-07-21T05:00:00Z',
      calculation_locked: false,
      calculation_lock_reason: null,
    }
    const inactive = {
      ...active,
      id: 2,
      code: 'TRAVEL',
      name: '已停用交通补贴',
      is_active: false,
      deactivated_at: '2026-07-20T05:00:00Z',
      updated_at: '2026-07-20T05:00:00Z',
    }
    compApi.fetchComponents.mockImplementation(
      ({ status }: { status: 'active' | 'inactive' | 'all' } = { status: 'active' }) =>
        Promise.resolve(
          status === 'inactive' ? [inactive] : status === 'all' ? [active, inactive] : [active],
        ),
    )

    renderPage()

    expect(await screen.findByText('餐补')).toBeTruthy()
    fireEvent.mouseDown(screen.getByLabelText('组件状态'))
    fireEvent.click(await screen.findByText('已停用'))

    await waitFor(() =>
      expect(compApi.fetchComponents).toHaveBeenLastCalledWith({ status: 'inactive' }),
    )
    expect(await screen.findByText('已停用交通补贴')).toBeTruthy()
    expect(screen.queryByText('餐补')).toBeNull()
  })

  it('edits every mutable component field with optimistic concurrency', async () => {
    renderPage()

    const row = (await screen.findByText('餐补')).closest('tr')
    expect(row).not.toBeNull()
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '编辑' }))

    const dialog = await screen.findByRole('dialog', { name: '编辑薪资组件' })
    fireEvent.change(within(dialog).getByLabelText('名称'), { target: { value: '工作餐补贴' } })
    fireEvent.mouseDown(within(dialog).getByLabelText('补贴方式'))
    fireEvent.click(await screen.findByText('浮动补贴（变量）'))
    fireEvent.click(within(dialog).getByLabelText('按实际计薪出勤天数折算'))
    fireEvent.click(within(dialog).getByLabelText('计税'))
    fireEvent.click(within(dialog).getByLabelText('计入社保基数'))
    fireEvent.click(within(dialog).getByLabelText('计入公积金基数'))
    fireEvent.change(within(dialog).getByLabelText('排序'), { target: { value: '8' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(compApi.updateComponent).toHaveBeenCalledWith(1, {
        name: '工作餐补贴',
        allowance_kind: 'FLOATING',
        prorate_by_attendance: false,
        taxable: false,
        in_social_base: true,
        in_housing_base: true,
        sort_order: 8,
        expected_updated_at: '2026-07-21T05:00:00Z',
      }),
    )
  })

  it('explains and disables historical calculation fields while keeping descriptive fields editable', async () => {
    compApi.fetchComponents.mockResolvedValue([
      {
        id: 5,
        code: 'HIST_MEAL',
        name: '历史餐补',
        component_type: 'ALLOWANCE',
        allowance_kind: 'FIXED',
        taxable: true,
        in_social_base: true,
        in_housing_base: false,
        prorate_by_attendance: true,
        sort_order: 3,
        is_active: true,
        deactivated_at: null,
        updated_at: '2026-07-21T06:00:00Z',
        calculation_locked: true,
        calculation_lock_reason: '已参与 2026-06 工资计算',
      },
    ])
    renderPage()

    const row = (await screen.findByText('历史餐补')).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '编辑' }))

    const dialog = await screen.findByRole('dialog', { name: '编辑薪资组件' })
    expect(within(dialog).getByText(/计算属性已锁定/)).toBeTruthy()
    expect(within(dialog).getByText('已参与 2026-06 工资计算')).toBeTruthy()
    expect((within(dialog).getByLabelText('组件类型') as HTMLInputElement).disabled).toBe(true)
    for (const label of [
      '补贴方式',
      '按实际计薪出勤天数折算',
      '计税',
      '计入社保基数',
      '计入公积金基数',
    ]) {
      expect((within(dialog).getByLabelText(label) as HTMLInputElement).disabled).toBe(true)
    }
    expect((within(dialog).getByLabelText('名称') as HTMLInputElement).disabled).toBe(false)
    expect((within(dialog).getByLabelText('排序') as HTMLInputElement).disabled).toBe(false)
  })

  it('allows a reasoned one-time classification for an unclassified historical allowance', async () => {
    compApi.fetchComponents.mockResolvedValue([
      {
        id: 6,
        code: 'LEGACY_ALLOWANCE',
        name: '历史补贴',
        component_type: 'ALLOWANCE',
        allowance_kind: null,
        taxable: true,
        in_social_base: false,
        in_housing_base: false,
        prorate_by_attendance: false,
        sort_order: 4,
        is_active: true,
        deactivated_at: null,
        updated_at: '2026-07-21T06:30:00Z',
        calculation_locked: true,
        calculation_lock_reason: '已参与历史工资计算',
      },
    ])
    renderPage()

    const row = (await screen.findByText('历史补贴')).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '编辑' }))
    const dialog = await screen.findByRole('dialog', { name: '编辑薪资组件' })

    expect((within(dialog).getByLabelText('补贴方式') as HTMLInputElement).disabled).toBe(false)
    expect((within(dialog).getByLabelText('计税') as HTMLInputElement).disabled).toBe(true)
    fireEvent.mouseDown(within(dialog).getByLabelText('补贴方式'))
    fireEvent.click(await screen.findByText('固定补贴'))
    fireEvent.change(within(dialog).getByLabelText('历史补贴分类原因'), {
      target: { value: '依据原补贴审批单补录分类' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(compApi.updateComponent).toHaveBeenCalledWith(6, {
        name: '历史补贴',
        sort_order: 4,
        expected_updated_at: '2026-07-21T06:30:00Z',
        allowance_kind: 'FIXED',
        reason: '依据原补贴审批单补录分类',
      }),
    )
  })

  it('deactivates and restores components using the latest update timestamp', async () => {
    const active = {
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
      is_active: true,
      deactivated_at: null,
      updated_at: '2026-07-21T05:00:00Z',
      calculation_locked: false,
      calculation_lock_reason: null,
    }
    const inactive = {
      ...active,
      id: 2,
      code: 'TRAVEL',
      name: '交通补贴',
      is_active: false,
      deactivated_at: '2026-07-20T05:00:00Z',
      updated_at: '2026-07-20T05:00:00Z',
    }
    compApi.fetchComponents.mockImplementation(
      ({ status }: { status: 'active' | 'inactive' | 'all' } = { status: 'active' }) =>
        Promise.resolve(status === 'inactive' ? [inactive] : [active]),
    )
    renderPage()

    const activeRow = (await screen.findByText('餐补')).closest('tr')
    await submitLifecycleAction(activeRow as HTMLTableRowElement, '停用', '旧补贴政策停止新增使用')
    await waitFor(() =>
      expect(compApi.deactivateComponent).toHaveBeenCalledWith(1, {
        reason: '旧补贴政策停止新增使用',
        expected_updated_at: '2026-07-21T05:00:00Z',
      }),
    )

    fireEvent.mouseDown(screen.getByLabelText('组件状态'))
    fireEvent.click(await screen.findByText('已停用'))
    const inactiveRow = (await screen.findByText('交通补贴')).closest('tr')
    await submitLifecycleAction(
      inactiveRow as HTMLTableRowElement,
      '恢复',
      '经薪酬负责人确认重新启用',
    )
    await waitFor(() =>
      expect(compApi.restoreComponent).toHaveBeenCalledWith(2, {
        reason: '经薪酬负责人确认重新启用',
        expected_updated_at: '2026-07-20T05:00:00Z',
      }),
    )
  })

  it('reports a 409 edit conflict and refreshes the component list', async () => {
    compApi.updateComponent.mockRejectedValue({
      response: { status: 409, data: { detail: '薪资组件已被其他人修改' } },
    })
    renderPage()

    const row = (await screen.findByText('餐补')).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '编辑' }))
    const dialog = await screen.findByRole('dialog', { name: '编辑薪资组件' })
    fireEvent.change(within(dialog).getByLabelText('名称'), { target: { value: '新餐补' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    expect(await screen.findByText(/已被其他人修改.*刷新/)).toBeTruthy()
    await waitFor(() => expect(compApi.fetchComponents.mock.calls.length).toBeGreaterThan(1))
  })

  it('shows no component mutations to read-only users', async () => {
    auth.permissions = []
    renderPage()

    expect(await screen.findByText('餐补')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '新增组件' })).toBeNull()
    expect(screen.queryByRole('button', { name: '编辑' })).toBeNull()
    expect(screen.queryByRole('button', { name: '停用' })).toBeNull()
    expect(screen.queryByRole('button', { name: '恢复' })).toBeNull()
    expect(screen.queryByRole('switch')).toBeNull()
  })
})
