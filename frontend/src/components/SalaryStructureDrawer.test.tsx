import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const compApi = vi.hoisted(() => ({
  fetchComponents: vi.fn(),
  fetchSalaryStructure: vi.fn(),
  fetchSalaryStructureHistory: vi.fn(),
  setInitialSalaryStructure: vi.fn(),
}))
const auth = vi.hoisted(() => ({ permissions: ['salary_structure:write'] as string[] }))

vi.mock('../api/comp', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/comp')>()),
  fetchComponents: compApi.fetchComponents,
  fetchSalaryStructure: compApi.fetchSalaryStructure,
  fetchSalaryStructureHistory: compApi.fetchSalaryStructureHistory,
  setInitialSalaryStructure: compApi.setInitialSalaryStructure,
}))
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'hr' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))

import SalaryStructureDrawer from './SalaryStructureDrawer'

const employee = {
  id: 17,
  emp_no: 'E0017',
  name: '陈星',
  org_unit_id: 3,
  job_grade_id: 4,
  employment_type: 'FULL_TIME',
  department: 'DINING',
  position_title: '服务员',
  is_special_position: false,
  status: 'ACTIVE',
  hire_date: '2025-01-02',
  probation_end: null,
  leave_date: null,
  social_city: null,
  id_card: null,
  bank_account: null,
  dingtalk_linked: false,
}

const components = [
  {
    id: 1,
    code: 'BASE',
    name: '基本工资',
    component_type: 'BASE',
    allowance_kind: null,
    taxable: true,
    in_social_base: true,
    in_housing_base: true,
    prorate_by_attendance: false,
    sort_order: 10,
    is_active: true,
    deactivated_at: null,
    updated_at: '2026-07-21T05:00:00Z',
    calculation_locked: false,
    calculation_lock_reason: null,
  },
  {
    id: 2,
    code: 'MEAL',
    name: '餐补',
    component_type: 'ALLOWANCE',
    allowance_kind: 'FIXED',
    taxable: false,
    in_social_base: false,
    in_housing_base: false,
    prorate_by_attendance: true,
    sort_order: 20,
    is_active: true,
    deactivated_at: null,
    updated_at: '2026-07-21T05:00:00Z',
    calculation_locked: false,
    calculation_lock_reason: null,
  },
  {
    id: 4,
    code: 'HOUSING',
    name: '房补',
    component_type: 'HOUSING',
    allowance_kind: null,
    taxable: false,
    in_social_base: false,
    in_housing_base: false,
    prorate_by_attendance: false,
    sort_order: 30,
    is_active: true,
    deactivated_at: null,
    updated_at: '2026-07-21T05:00:00Z',
    calculation_locked: false,
    calculation_lock_reason: null,
  },
  {
    id: 3,
    code: 'OLD_POSITION',
    name: '历史岗位补贴',
    component_type: 'POSITION',
    allowance_kind: null,
    taxable: true,
    in_social_base: false,
    in_housing_base: false,
    prorate_by_attendance: false,
    sort_order: 40,
    is_active: false,
    deactivated_at: '2026-06-30T12:00:00Z',
    updated_at: '2026-06-30T12:00:00Z',
    calculation_locked: true,
    calculation_lock_reason: '已参与历史工资计算',
  },
]

const currentStructure = {
  items: [
    {
      component_id: 1,
      amount: '5000.00',
      effective_from: '2026-01-01',
      effective_to: null,
      source_adjustment_id: null,
      source_reason: null,
      source_attachment_url: null,
    },
    {
      component_id: 3,
      amount: '300.00',
      effective_from: '2026-01-01',
      effective_to: null,
      source_adjustment_id: 22,
      source_reason: '年度调薪审批',
      source_attachment_url: 'https://files.example.test/adjustments/22.pdf',
    },
  ],
  compa: {
    total: '5300.00',
    band_status: 'IN_BAND',
    compa_ratio: '1.06',
    band_min: '4500.00',
    band_mid: '5000.00',
    band_max: '6000.00',
  },
}

const emptyStructure = {
  items: [],
  compa: {
    total: '0.00',
    band_status: 'NO_BAND',
    compa_ratio: null,
    band_min: null,
    band_mid: null,
    band_max: null,
  },
}

const history = [
  {
    id: 31,
    revision: 2,
    component_id: 3,
    amount: '300.00',
    effective_from: '2026-01-01',
    effective_to: '2026-06-30',
    source_adjustment_id: 22,
    source_reason: '年度调薪审批',
    source_attachment_url: 'https://files.example.test/adjustments/22.pdf',
    component_code: 'OLD_POSITION',
    component_name: '历史岗位补贴',
    component_type: 'POSITION',
    component_is_active: false,
  },
]

function renderDrawer(props: Partial<React.ComponentProps<typeof SalaryStructureDrawer>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const defaultProps: React.ComponentProps<typeof SalaryStructureDrawer> = {
    employee,
    open: true,
    onClose: vi.fn(),
  }
  const rendered = render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <SalaryStructureDrawer {...defaultProps} {...props} />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  return { ...rendered, queryClient }
}

describe('SalaryStructureDrawer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.permissions = ['salary_structure:write']
    compApi.fetchComponents.mockResolvedValue(components)
    compApi.fetchSalaryStructure.mockResolvedValue(currentStructure)
    compApi.fetchSalaryStructureHistory.mockResolvedValue(history)
    compApi.setInitialSalaryStructure.mockResolvedValue([])
  })

  afterEach(cleanup)

  it('loads a dated structure with the full component map and shows compa and audited history', async () => {
    renderDrawer()

    await waitFor(() =>
      expect(compApi.fetchSalaryStructure).toHaveBeenCalledWith(
        17,
        expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
      ),
    )
    expect(compApi.fetchComponents).toHaveBeenCalledWith({ status: 'all' })
    expect(compApi.fetchSalaryStructureHistory).toHaveBeenCalledWith(17)

    expect((await screen.findAllByText('基本工资')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('历史岗位补贴').length).toBeGreaterThan(0)
    expect(screen.getByText(/合计.*5,?300\.00/)).toBeTruthy()
    expect(screen.getByText('薪档内')).toBeTruthy()
    expect(screen.getByText(/Compa.*1\.06/i)).toBeTruthy()
    expect(screen.getByText('v2')).toBeTruthy()
    expect(screen.getByText('2026-01-01 至 2026-06-30')).toBeTruthy()
    expect(screen.getByText('年度调薪审批')).toBeTruthy()
    expect(screen.getByText('组件已停用')).toBeTruthy()
    expect(screen.getByRole('link', { name: '查看附件' }).getAttribute('href')).toBe(
      'https://files.example.test/adjustments/22.pdf',
    )

    fireEvent.change(screen.getByLabelText('查看日期'), {
      target: { value: '2026-06-30' },
    })
    await waitFor(() => expect(compApi.fetchSalaryStructure).toHaveBeenCalledWith(17, '2026-06-30'))
  })

  it('atomically initializes multiple components and requires evidence for allowance and housing', async () => {
    compApi.fetchSalaryStructure.mockResolvedValue(emptyStructure)
    compApi.fetchSalaryStructureHistory.mockResolvedValue([])
    renderDrawer()

    fireEvent.click(await screen.findByRole('button', { name: '初始化薪资结构' }))
    const dialog = await screen.findByRole('dialog', { name: '初始化薪资结构' })
    fireEvent.change(within(dialog).getByLabelText('生效日期'), {
      target: { value: '2026-07-01' },
    })
    for (const name of ['基本工资', '餐补', '房补']) {
      fireEvent.click(within(dialog).getByRole('checkbox', { name: `选择${name}` }))
    }
    fireEvent.change(within(dialog).getByLabelText('基本工资金额'), {
      target: { value: '5000' },
    })
    fireEvent.change(within(dialog).getByLabelText('餐补金额'), { target: { value: '300' } })
    fireEvent.change(within(dialog).getByLabelText('房补金额'), { target: { value: '600' } })
    fireEvent.change(within(dialog).getByLabelText('餐补附件'), {
      target: { value: 'ftp://files.example.test/meal.pdf' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: '确认初始化' }))

    expect(await within(dialog).findByText('餐补必须填写原因')).toBeTruthy()
    expect(within(dialog).getByText('餐补附件必须为 http(s) 地址')).toBeTruthy()
    expect(within(dialog).getByText('房补必须填写原因')).toBeTruthy()
    expect(within(dialog).getByText('房补附件必须为 http(s) 地址')).toBeTruthy()
    expect(compApi.setInitialSalaryStructure).not.toHaveBeenCalled()

    fireEvent.change(within(dialog).getByLabelText('餐补原因'), {
      target: { value: '经薪酬负责人确认的餐补政策' },
    })
    fireEvent.change(within(dialog).getByLabelText('餐补附件'), {
      target: { value: 'https://files.example.test/policies/meal.pdf' },
    })
    fireEvent.change(within(dialog).getByLabelText('房补原因'), {
      target: { value: '经薪酬负责人确认的住房补贴政策' },
    })
    fireEvent.change(within(dialog).getByLabelText('房补附件'), {
      target: { value: 'http://intranet.example.test/policies/housing.pdf' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: '确认初始化' }))

    await waitFor(() =>
      expect(compApi.setInitialSalaryStructure).toHaveBeenCalledWith(17, {
        effective_from: '2026-07-01',
        items: [
          { component_id: 1, amount: 5000 },
          {
            component_id: 2,
            amount: 300,
            reason: '经薪酬负责人确认的餐补政策',
            attachment_url: 'https://files.example.test/policies/meal.pdf',
          },
          {
            component_id: 4,
            amount: 600,
            reason: '经薪酬负责人确认的住房补贴政策',
            attachment_url: 'http://intranet.example.test/policies/housing.pdf',
          },
        ],
      }),
    )
    await waitFor(() => expect(compApi.fetchSalaryStructure.mock.calls.length).toBeGreaterThan(1))
    expect(compApi.fetchSalaryStructureHistory.mock.calls.length).toBeGreaterThan(1)
  })

  it('routes every existing structure change through salary-adjustment approval', async () => {
    renderDrawer()

    expect(await screen.findByText('已有薪资结构不能直接修改，请通过调薪审批变更。')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '初始化薪资结构' })).toBeNull()
    expect(screen.getByRole('link', { name: '发起调薪审批' }).getAttribute('href')).toBe(
      '/adjustment',
    )
  })

  it('fails closed when any required structure evidence cannot be loaded', async () => {
    compApi.fetchSalaryStructureHistory.mockRejectedValue(new Error('历史记录不可用'))
    renderDrawer()

    expect(await screen.findByText(/薪资结构加载失败/)).toBeTruthy()
    expect(screen.getByText('历史记录不可用')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '初始化薪资结构' })).toBeNull()
    expect(screen.queryByRole('link', { name: '发起调薪审批' })).toBeNull()
  })

  it('keeps an empty structure read-only without salary-structure write permission', async () => {
    auth.permissions = []
    compApi.fetchSalaryStructure.mockResolvedValue(emptyStructure)
    compApi.fetchSalaryStructureHistory.mockResolvedValue([])
    renderDrawer()

    expect(await screen.findByText('尚未建立薪资结构')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '初始化薪资结构' })).toBeNull()
  })
})
