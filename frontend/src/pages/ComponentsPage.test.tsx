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
const auth = vi.hoisted(() => ({
  permissions: ['salary_structure:write'] as string[],
  globalPermissions: ['salary_structure:write'] as string[],
}))
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
    hasGlobalPermission: (permission: string) => auth.globalPermissions.includes(permission),
  }),
}))
vi.mock('../components/LegacyCatalogReviewDrawer', () => ({
  default: ({ open, mode, onApplied }: { open: boolean; mode: string; onApplied: () => void }) => {
    legacyReview.onApplied = onApplied
    return open ? (
      <div role="dialog" aria-label={`旧系统真实数据-${mode}`}>
        <button onClick={onApplied}>模拟应用真实数据</button>
      </div>
    ) : null
  },
}))
vi.mock('../components/LegacyCatalogEvidencePanel', () => ({
  default: ({ mode, onReview }: { mode: string; onReview: () => void }) => (
    <section aria-label={`默认展示旧系统真实数据-${mode}`}>
      <button onClick={onReview}>从真实数据区创建正式组件</button>
    </section>
  ),
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

function pressEscape(dialog: HTMLElement) {
  const wrapper = dialog.closest<HTMLElement>('.ant-modal-wrap')
  if (!wrapper) throw new Error('modal wrapper did not render')
  const event = new KeyboardEvent('keydown', { bubbles: true, key: 'Escape', code: 'Escape' })
  Object.defineProperty(event, 'keyCode', { value: 27 })
  fireEvent(wrapper, event)
}

describe('ComponentsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.permissions = ['salary_structure:write']
    auth.globalPermissions = ['salary_structure:write']
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

  it('shows a clear salary component page title', async () => {
    renderPage()

    expect(await screen.findByText('薪资组件', { selector: '.ant-card-head-title' })).toBeTruthy()
  })

  it('shows whether an allowance is configured for attendance proration', async () => {
    renderPage()

    const allowanceName = await screen.findByText('餐补')
    expect(screen.getByText('按出勤折算')).toBeTruthy()
    const row = allowanceName.closest('tr')
    expect(row).not.toBeNull()
    expect(within(row as HTMLTableRowElement).getAllByText('是').length).toBeGreaterThan(0)
  })

  it('shows legacy evidence and opens its review for import-capable writers', async () => {
    auth.permissions = ['salary_structure:write', 'import:run']
    auth.globalPermissions = ['salary_structure:write', 'import:run']
    const { queryClient } = renderPage()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    await screen.findByText('餐补')
    expect(screen.getByRole('region', { name: '默认展示旧系统真实数据-components' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '从真实数据区创建正式组件' }))

    expect(screen.getByRole('dialog', { name: '旧系统真实数据-components' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '模拟应用真实数据' }))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['components', 'hr'] }))
    expect(screen.queryByRole('dialog', { name: '旧系统真实数据-components' })).toBeNull()
  })

  it('hides legacy review when its permissions are only locally scoped', async () => {
    auth.permissions = ['salary_structure:write', 'import:run']
    auth.globalPermissions = []
    renderPage()

    await screen.findByText('餐补')
    expect(screen.queryByRole('region', { name: '默认展示旧系统真实数据-components' })).toBeNull()
  })

  it('does not expose legacy catalog creation to import-only users', async () => {
    auth.permissions = ['import:run']
    auth.globalPermissions = ['import:run']
    renderPage()

    await screen.findByText('餐补')
    expect(screen.queryByRole('button', { name: '审阅旧系统真实数据' })).toBeNull()
    expect(screen.queryByRole('region', { name: '默认展示旧系统真实数据-components' })).toBeNull()
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

  it('prevents duplicate component creation in the same render frame', async () => {
    let resolveCreate: ((value: Record<string, never>) => void) | undefined
    let form: HTMLFormElement | null = null
    let replayedSubmit = false
    compApi.createComponent.mockImplementation(() => {
      if (!replayedSubmit) {
        replayedSubmit = true
        if (!form) throw new Error('component create form was not captured')
        fireEvent.submit(form)
      }
      return new Promise<Record<string, never>>((resolve) => {
        resolveCreate = resolve
      })
    })
    renderPage()

    const open = await screen.findByRole('button', { name: '新增组件' })
    await waitFor(() => expect((open as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(open)
    const dialog = await screen.findByRole('dialog', { name: '新增薪资组件' })
    fireEvent.change(within(dialog).getByLabelText('编码'), { target: { value: 'BASE' } })
    fireEvent.change(within(dialog).getByLabelText('名称'), { target: { value: '基本工资' } })
    fireEvent.mouseDown(within(dialog).getByLabelText('类型'))
    fireEvent.click(await screen.findByText('基本'))
    form = dialog.querySelector('form')
    if (!form) throw new Error('component create form did not render')
    const submit = within(dialog).getByRole('button', { name: /OK|确\s*定/i })

    fireEvent.click(submit)
    fireEvent.click(submit)

    await waitFor(() => expect(compApi.createComponent).toHaveBeenCalled())
    await new Promise<void>((resolve) => setTimeout(resolve, 0))
    expect(compApi.createComponent).toHaveBeenCalledTimes(1)

    if (!resolveCreate) throw new Error('component creation did not start')
    resolveCreate({})
  })

  it('keeps the edit modal isolated while its mutation is pending', async () => {
    let resolveUpdate: ((value: Record<string, never>) => void) | undefined
    let form: HTMLFormElement | null = null
    let replayedSubmit = false
    compApi.updateComponent.mockImplementation(() => {
      if (!replayedSubmit) {
        replayedSubmit = true
        if (!form) throw new Error('component edit form was not captured')
        fireEvent.submit(form)
      }
      return new Promise<Record<string, never>>((resolve) => {
        resolveUpdate = resolve
      })
    })
    renderPage()

    const row = (await screen.findByText('餐补')).closest('tr') as HTMLTableRowElement
    fireEvent.click(within(row).getByRole('button', { name: '编辑' }))
    const dialog = await screen.findByRole('dialog', { name: '编辑薪资组件' })
    form = dialog.querySelector('form')
    if (!form) throw new Error('component edit form did not render')
    const submit = within(dialog).getByRole('button', { name: /OK|确\s*定/i })
    fireEvent.click(submit)
    fireEvent.click(submit)

    await waitFor(() => expect(compApi.updateComponent).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(
        (within(dialog).getByRole('button', { name: /Cancel|取\s*消/i }) as HTMLButtonElement)
          .disabled,
      ).toBe(true),
    )
    pressEscape(dialog)

    expect(screen.getByRole('dialog', { name: '编辑薪资组件' })).toBeTruthy()
    expect(screen.queryByRole('dialog', { name: '停用薪资组件' })).toBeNull()

    if (!resolveUpdate) throw new Error('component update did not start')
    resolveUpdate({})
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑薪资组件' })).toBeNull())
  })

  it('keeps the lifecycle modal isolated while its mutation is pending', async () => {
    let resolveDeactivate: ((value: Record<string, never>) => void) | undefined
    let form: HTMLFormElement | null = null
    let replayedSubmit = false
    compApi.deactivateComponent.mockImplementation(() => {
      if (!replayedSubmit) {
        replayedSubmit = true
        if (!form) throw new Error('component lifecycle form was not captured')
        fireEvent.submit(form)
      }
      return new Promise<Record<string, never>>((resolve) => {
        resolveDeactivate = resolve
      })
    })
    renderPage()

    const row = (await screen.findByText('餐补')).closest('tr') as HTMLTableRowElement
    fireEvent.click(within(row).getByRole('button', { name: '停用' }))
    const dialog = await screen.findByRole('dialog', { name: '停用薪资组件' })
    fireEvent.change(within(dialog).getByLabelText('停用原因'), {
      target: { value: '待服务端确认停用' },
    })
    form = dialog.querySelector('form')
    if (!form) throw new Error('component lifecycle form did not render')
    const submit = within(dialog).getByRole('button', { name: /OK|确\s*定|确认/i })
    fireEvent.click(submit)
    fireEvent.click(submit)

    await waitFor(() => expect(compApi.deactivateComponent).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(
        (within(dialog).getByRole('button', { name: /Cancel|取\s*消/i }) as HTMLButtonElement)
          .disabled,
      ).toBe(true),
    )
    pressEscape(dialog)

    expect(screen.getByRole('dialog', { name: '停用薪资组件' })).toBeTruthy()
    expect(screen.queryByRole('dialog', { name: '编辑薪资组件' })).toBeNull()

    if (!resolveDeactivate) throw new Error('component lifecycle mutation did not start')
    resolveDeactivate({})
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '停用薪资组件' })).toBeNull())
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
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '补齐分类' }))
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

  it('requires reasoned classification before lifecycle even when legacy allowance is unlocked', async () => {
    compApi.fetchComponents.mockResolvedValue([
      {
        id: 7,
        code: 'LEGACY_UNLOCKED',
        name: '未分类历史补贴',
        component_type: 'ALLOWANCE',
        allowance_kind: null,
        taxable: true,
        in_social_base: false,
        in_housing_base: false,
        prorate_by_attendance: false,
        sort_order: 5,
        is_active: true,
        deactivated_at: null,
        updated_at: '2026-07-21T06:40:00Z',
        calculation_locked: false,
        calculation_lock_reason: null,
      },
    ])
    renderPage()

    const row = (await screen.findByText('未分类历史补贴')).closest('tr') as HTMLTableRowElement
    expect((within(row).getByRole('button', { name: '停用' }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    fireEvent.click(within(row).getByRole('button', { name: '补齐分类' }))
    const dialog = await screen.findByRole('dialog', { name: '编辑薪资组件' })
    fireEvent.mouseDown(within(dialog).getByLabelText('补贴方式'))
    fireEvent.click(await screen.findByText('浮动补贴（变量）'))
    fireEvent.change(within(dialog).getByLabelText('历史补贴分类原因'), {
      target: { value: '依据旧审批台账确认' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(compApi.updateComponent).toHaveBeenCalledWith(7, {
        name: '未分类历史补贴',
        allowance_kind: 'FLOATING',
        prorate_by_attendance: false,
        taxable: true,
        in_social_base: false,
        in_housing_base: false,
        sort_order: 5,
        expected_updated_at: '2026-07-21T06:40:00Z',
        reason: '依据旧审批台账确认',
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
