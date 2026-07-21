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
  return render(
    <QueryClientProvider client={queryClient}>
      <EmployeesPage />
    </QueryClientProvider>,
  )
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
      }),
    )
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
