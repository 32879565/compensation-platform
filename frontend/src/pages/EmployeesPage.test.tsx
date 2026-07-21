import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const masterdataApi = vi.hoisted(() => ({
  createEmployee: vi.fn(),
  deleteEmployee: vi.fn(),
  fetchEmployees: vi.fn(),
  fetchOrgUnits: vi.fn(),
  updateEmployee: vi.fn(),
}))
const dingtalkApi = vi.hoisted(() => ({
  applyDingTalkEmployeeMatches: vi.fn(),
  fetchDingTalkIntegration: vi.fn(),
  previewDingTalkEmployees: vi.fn(),
}))
const auth = vi.hoisted(() => ({ permissions: ['employee:write'] as string[] }))

vi.mock('../api/masterdata', () => masterdataApi)
vi.mock('../api/dingtalk', () => dingtalkApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'employee-editor' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))
vi.mock('../components/TaxOpeningModal', () => ({ TaxOpeningModal: () => null }))

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
          job_grade_id: null,
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
