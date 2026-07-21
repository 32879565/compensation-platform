import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const masterdataApi = vi.hoisted(() => ({
  createEmployee: vi.fn(),
  deleteEmployee: vi.fn(),
  fetchEmployees: vi.fn(),
  fetchGrades: vi.fn(),
  fetchOrgUnits: vi.fn(),
  updateEmployee: vi.fn(),
}))
const dingtalkApi = vi.hoisted(() => ({
  applyDingTalkEmployeeMatches: vi.fn(),
  fetchDingTalkIntegration: vi.fn(),
  previewDingTalkEmployees: vi.fn(),
}))
const auth = vi.hoisted(() => ({ permissions: ['employee:write'] as string[] }))
const salaryStructureDrawer = vi.hoisted(() => ({ render: vi.fn() }))

vi.mock('../api/masterdata', () => masterdataApi)
vi.mock('../api/dingtalk', () => dingtalkApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'employee-editor' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))
vi.mock('../components/TaxOpeningModal', () => ({ TaxOpeningModal: () => null }))
vi.mock('../components/SalaryStructureDrawer', () => ({
  default: (props: {
    employee: { emp_no: string; id: number } | null
    open: boolean
    onClose: () => void
  }) => {
    salaryStructureDrawer.render(props)
    return props.open ? (
      <div role="dialog" aria-label="薪资结构抽屉">
        <span>{props.employee?.emp_no}</span>
        <button type="button" onClick={props.onClose}>
          关闭薪资结构
        </button>
      </div>
    ) : null
  },
}))

import EmployeesPage, { isKnownSpecialPosition } from './EmployeesPage'

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <EmployeesPage />
      </QueryClientProvider>,
    ),
    queryClient,
  }
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  const promise = new Promise<T>((accept) => {
    resolve = accept
  })
  return { promise, resolve }
}

describe('EmployeesPage safe edits', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.permissions = ['employee:write']
    masterdataApi.fetchOrgUnits.mockResolvedValue([
      {
        id: 3,
        code: 'S003',
        name: '三店',
        type: 'STORE',
        parent_id: null,
        city: null,
        status: 'ACTIVE',
      },
    ])
    masterdataApi.fetchEmployees.mockResolvedValue({
      items: [
        {
          id: 17,
          version: 4,
          emp_no: 'E0017',
          name: '陈星',
          org_unit_id: 3,
          job_grade_id: 102,
          employment_type: 'FULL_TIME',
          department: 'DINING',
          position_title: '店员',
          is_special_position: false,
          status: 'ACTIVE',
          hire_date: '2025-01-02',
          probation_end: null,
          leave_date: null,
          social_city: null,
          id_card: '**************1234',
          bank_account: '************5678',
          dingtalk_linked: false,
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    })
    masterdataApi.updateEmployee.mockResolvedValue({})
    masterdataApi.fetchGrades.mockResolvedValue([
      {
        id: 101,
        code: 'G01',
        name: '初级职级',
        rank: 1,
        version: 1,
        is_active: true,
        deactivated_at: null,
      },
      {
        id: 102,
        code: 'G02',
        name: '停用职级',
        rank: 2,
        version: 3,
        is_active: false,
        deactivated_at: '2026-06-30T10:00:00Z',
      },
    ])
    dingtalkApi.fetchDingTalkIntegration.mockResolvedValue({
      mode: 'sandbox',
      credentials_configured: true,
      app_id_configured: true,
      public_base_url_configured: false,
      ready_for_live: false,
      read_sync_enabled: true,
      read_sync_ready: true,
    })
  })

  afterEach(cleanup)

  it('lets keyboard users horizontally scroll the employee directory', async () => {
    renderPage()

    const region = await screen.findByRole('region', { name: '员工岗位目录' })
    expect(region.tabIndex).toBe(0)
    region.scrollLeft = 0
    fireEvent.keyDown(region, { key: 'ArrowRight', code: 'ArrowRight' })
    expect(region.scrollLeft).toBeGreaterThan(0)
    const afterRight = region.scrollLeft
    fireEvent.keyDown(region, { key: 'ArrowLeft', code: 'ArrowLeft' })
    expect(region.scrollLeft).toBeLessThan(afterRight)
  })

  it('never prefills masked PII and sends only fields actually changed by the editor', async () => {
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /编\s*辑/ }))

    expect((screen.getByLabelText('身份证号') as HTMLInputElement).value).toBe('')
    expect((screen.getByLabelText('银行卡号') as HTMLInputElement).value).toBe('')
    const position = screen.getByLabelText('职位名称') as HTMLInputElement
    expect(position.maxLength).toBe(64)
    fireEvent.change(position, { target: { value: '储备店长' } })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(masterdataApi.updateEmployee).toHaveBeenCalledWith(17, {
        position_title: '储备店长',
        is_special_position: true,
        expected_version: 4,
      }),
    )
  })

  it('uses a synchronous latch to collapse rapid save clicks into one request', async () => {
    const save = deferred<Record<string, never>>()
    masterdataApi.updateEmployee.mockReturnValueOnce(save.promise)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /编\s*辑/ }))
    fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '陈星（单次提交）' } })
    const confirm = screen.getByRole('button', { name: /OK|确\s*定/i })
    fireEvent.click(confirm)
    fireEvent.click(confirm)

    await waitFor(() => expect(masterdataApi.updateEmployee).toHaveBeenCalledTimes(1))
    expect(masterdataApi.updateEmployee).toHaveBeenCalledWith(17, {
      name: '陈星（单次提交）',
      expected_version: 4,
    })

    save.resolve({})
    await waitFor(() => expect(screen.queryByLabelText('姓名')).toBeNull())
  })

  it('recovers from a version conflict before allowing the employee to be edited again', async () => {
    masterdataApi.updateEmployee
      .mockRejectedValueOnce({
        response: { status: 409, data: { detail: '员工已被其他操作更新' } },
      })
      .mockResolvedValueOnce({})
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /编\s*辑/ }))
    const stalePage = await masterdataApi.fetchEmployees.mock.results[0].value
    const refreshedPage = {
      ...stalePage,
      items: stalePage.items.map((employee: { id: number }) =>
        employee.id === 17 ? { ...employee, version: 5, name: '陈星（已刷新）' } : employee,
      ),
    }
    const refresh = deferred<typeof refreshedPage>()
    masterdataApi.fetchEmployees.mockImplementationOnce(() => refresh.promise)
    masterdataApi.fetchEmployees.mockResolvedValue(refreshedPage)
    fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '陈星（冲突编辑）' } })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(masterdataApi.updateEmployee).toHaveBeenNthCalledWith(1, 17, {
        name: '陈星（冲突编辑）',
        expected_version: 4,
      }),
    )
    await waitFor(() => expect(screen.queryByLabelText('姓名')).toBeNull())
    await waitFor(() => expect(masterdataApi.fetchEmployees).toHaveBeenCalledTimes(2))
    expect((screen.getByRole('button', { name: /编\s*辑/ }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    expect((screen.getByRole('button', { name: /删\s*除/ }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    expect((screen.getByRole('button', { name: '新增员工' }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    fireEvent.click(screen.getByRole('button', { name: /编\s*辑/ }))
    expect(screen.queryByLabelText('姓名')).toBeNull()

    refresh.resolve(refreshedPage)
    await waitFor(() =>
      expect((screen.getByRole('button', { name: /编\s*辑/ }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    )
    expect(await screen.findByText('陈星（已刷新）')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /编\s*辑/ }))
    expect(((await screen.findByLabelText('姓名')) as HTMLInputElement).value).toBe(
      '陈星（已刷新）',
    )
    fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '陈星（最终编辑）' } })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(masterdataApi.updateEmployee).toHaveBeenNthCalledWith(2, 17, {
        name: '陈星（最终编辑）',
        expected_version: 5,
      }),
    )
  })

  it('keeps employee writes disabled until a successful save refetch completes', async () => {
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /编\s*辑/ }))
    const currentPage = await masterdataApi.fetchEmployees.mock.results[0].value
    const refreshedPage = {
      ...currentPage,
      items: currentPage.items.map((employee: { id: number }) =>
        employee.id === 17 ? { ...employee, version: 5, name: '陈星（保存后）' } : employee,
      ),
    }
    const refresh = deferred<typeof refreshedPage>()
    masterdataApi.fetchEmployees.mockImplementationOnce(() => refresh.promise)
    masterdataApi.fetchEmployees.mockResolvedValue(refreshedPage)
    fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '陈星（保存后）' } })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() => expect(masterdataApi.fetchEmployees).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByLabelText('姓名')).toBeNull())
    expect((screen.getByRole('button', { name: /编\s*辑/ }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    expect((screen.getByRole('button', { name: /删\s*除/ }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    expect((screen.getByRole('button', { name: '新增员工' }) as HTMLButtonElement).disabled).toBe(
      true,
    )

    refresh.resolve(refreshedPage)
    await waitFor(() =>
      expect((screen.getByRole('button', { name: /编\s*辑/ }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    )
    expect(await screen.findByText('陈星（保存后）')).toBeTruthy()
  })

  it('retains the employee form after a non-conflict save failure', async () => {
    masterdataApi.updateEmployee.mockRejectedValueOnce({
      response: { status: 422, data: { detail: '员工数据无效' } },
    })
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /编\s*辑/ }))
    fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '陈星（待修正）' } })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() => expect(masterdataApi.updateEmployee).toHaveBeenCalledTimes(1))
    expect((screen.getByLabelText('姓名') as HTMLInputElement).value).toBe('陈星（待修正）')
    expect(masterdataApi.fetchEmployees).toHaveBeenCalledTimes(1)
  })

  it('retains a new employee form when create returns a 409 business conflict', async () => {
    masterdataApi.createEmployee.mockRejectedValueOnce({
      response: { status: 409, data: { detail: '工号已存在' } },
    })
    renderPage()

    const createButton = await screen.findByRole('button', { name: '新增员工' })
    await waitFor(() => expect((createButton as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(createButton)
    fireEvent.change(await screen.findByLabelText('工号'), { target: { value: 'E0099' } })
    fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '林月' } })
    fireEvent.mouseDown(screen.getByLabelText('所属组织'))
    fireEvent.click(await screen.findByRole('option', { name: '三店' }))
    fireEvent.change(screen.getByLabelText('入职日期'), { target: { value: '2026-07-01' } })
    expect((screen.getByLabelText('工号') as HTMLInputElement).value).toBe('E0099')
    expect((screen.getByLabelText('姓名') as HTMLInputElement).value).toBe('林月')
    expect((screen.getByLabelText('入职日期') as HTMLInputElement).value).toBe('2026-07-01')
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() => expect(masterdataApi.createEmployee).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('工号已存在')).toBeTruthy()
    expect((screen.getByLabelText('工号') as HTMLInputElement).value).toBe('E0099')
    expect((screen.getByLabelText('姓名') as HTMLInputElement).value).toBe('林月')
    expect((screen.getByLabelText('入职日期') as HTMLInputElement).value).toBe('2026-07-01')
    expect(masterdataApi.fetchEmployees).toHaveBeenCalledTimes(1)
  })

  it('handles a null save rejection without replacing the original form', async () => {
    masterdataApi.updateEmployee.mockRejectedValueOnce(null)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /编\s*辑/ }))
    fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '陈星（继续修正）' } })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() => expect(masterdataApi.updateEmployee).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('保存失败')).toBeTruthy()
    expect((screen.getByLabelText('姓名') as HTMLInputElement).value).toBe('陈星（继续修正）')
    expect(masterdataApi.fetchEmployees).toHaveBeenCalledTimes(1)
  })

  it('blocks confirmation when the DingTalk directory preview is truncated', async () => {
    auth.permissions = ['employee:write', 'notification:manage']
    dingtalkApi.previewDingTalkEmployees.mockResolvedValue({
      total_remote_users: 500,
      matched: 2,
      stable_id_matches: 0,
      job_number_matches: 2,
      unique_name_matches: 0,
      ambiguous: 0,
      unmatched: 498,
      truncated: true,
      items: [],
    })

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '预览钉钉员工' }))
    expect(await screen.findByText(/预览结果不完整/)).toBeTruthy()
    const apply = screen.getByRole('button', { name: '确认绑定安全匹配项' })
    expect((apply as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(apply)
    expect(dingtalkApi.applyDingTalkEmployeeMatches).not.toHaveBeenCalled()
  })

  it('loads the full grade catalog and displays the assigned grade for grade readers', async () => {
    auth.permissions = ['grade:read']

    renderPage()

    await waitFor(() => expect(masterdataApi.fetchGrades).toHaveBeenCalledWith({ status: 'all' }))
    expect(await screen.findByRole('columnheader', { name: '职级' })).toBeTruthy()
    expect(await screen.findByText(/G02.*停用职级/)).toBeTruthy()
  })

  it('lets editors assign an active grade while keeping the current inactive grade read-only', async () => {
    auth.permissions = ['employee:write', 'grade:read']

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /编\s*辑/ }))
    const gradeSelect = await screen.findByLabelText('员工职级')
    expect(screen.getByText(/G02.*停用职级.*已停用/)).toBeTruthy()

    fireEvent.mouseDown(gradeSelect)
    const inactiveOption = await screen.findByRole('option', {
      name: /G02.*停用职级.*已停用/,
    })
    expect(inactiveOption.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(await screen.findByRole('option', { name: /G01.*初级职级/ }))
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(masterdataApi.updateEmployee).toHaveBeenCalledWith(17, {
        job_grade_id: 101,
        expected_version: 4,
      }),
    )
  })

  it('fails closed when the grade catalog changes after a new grade was selected', async () => {
    auth.permissions = ['employee:write', 'grade:read']
    const { queryClient } = renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /编\s*辑/ }))
    const gradeSelect = await screen.findByLabelText('员工职级')
    fireEvent.mouseDown(gradeSelect)
    fireEvent.click(await screen.findByRole('option', { name: /G01.*初级职级/ }))
    masterdataApi.fetchGrades.mockRejectedValueOnce(new Error('grade catalog unavailable'))
    await queryClient.invalidateQueries({ queryKey: ['grades'] })
    expect(await screen.findByText('职级目录加载失败，已禁止修改职级')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() => expect(masterdataApi.updateEmployee).not.toHaveBeenCalled())
    expect(await screen.findByText('职级目录尚未完整读取，已禁止修改员工职级')).toBeTruthy()
  })

  it('can explicitly remove an incorrect employee grade assignment', async () => {
    auth.permissions = ['employee:write', 'grade:read']
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /编\s*辑/ }))
    const gradeSelect = await screen.findByLabelText('员工职级')
    fireEvent.mouseDown(gradeSelect)
    fireEvent.click(await screen.findByRole('option', { name: '未分配职级' }))
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(masterdataApi.updateEmployee).toHaveBeenCalledWith(17, {
        job_grade_id: null,
        expected_version: 4,
      }),
    )
  })

  it('does not query or expose grades without grade read permission', async () => {
    auth.permissions = ['employee:write']

    renderPage()

    await screen.findByText('E0017')
    expect(masterdataApi.fetchGrades).not.toHaveBeenCalled()
    expect(screen.queryByRole('columnheader', { name: '职级' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /编\s*辑/ }))
    expect(screen.queryByLabelText('员工职级')).toBeNull()
  })

  it('opens and closes the selected employee salary structure drawer for salary readers', async () => {
    auth.permissions = ['salary_structure:read']

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '薪资结构' }))
    const drawer = await screen.findByRole('dialog', { name: '薪资结构抽屉' })
    expect(within(drawer).getByText('E0017')).toBeTruthy()
    expect(salaryStructureDrawer.render).toHaveBeenLastCalledWith(
      expect.objectContaining({
        employee: expect.objectContaining({ id: 17 }),
        open: true,
      }),
    )

    fireEvent.click(screen.getByRole('button', { name: '关闭薪资结构' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '薪资结构抽屉' })).toBeNull())
    expect(salaryStructureDrawer.render).toHaveBeenLastCalledWith(
      expect.objectContaining({ open: false }),
    )
  })
})

describe('special-position classification', () => {
  it.each(['储备店长', '厨师长（实习）', '洗碗岗位', '寒假工', '暑假工'])(
    'classifies %s as approved-day attendance',
    (position) => {
      expect(isKnownSpecialPosition(position)).toBe(true)
    },
  )

  it('keeps ordinary positions on their department attendance path', () => {
    expect(isKnownSpecialPosition('厅面服务员')).toBe(false)
  })
})
