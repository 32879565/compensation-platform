import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const adjustmentsApi = vi.hoisted(() => ({
  fetchMonthlyPayrollAdjustments: vi.fn(),
  upsertMonthlyPayrollAdjustment: vi.fn(),
}))
const masterdataApi = vi.hoisted(() => ({ fetchEmployees: vi.fn() }))
const auth = vi.hoisted(() => ({ permissions: [] as string[] }))

vi.mock('../api/payrollAdjustments', () => adjustmentsApi)
vi.mock('../api/masterdata', () => masterdataApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'payroll-source-hr' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))

import MonthlyPayrollAdjustmentsPage from './MonthlyPayrollAdjustmentsPage'

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MonthlyPayrollAdjustmentsPage />
    </QueryClientProvider>,
  )
}

describe('MonthlyPayrollAdjustmentsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.permissions = ['payroll:correct', 'employee:read']
    adjustmentsApi.fetchMonthlyPayrollAdjustments.mockResolvedValue([
      {
        id: 9,
        employee_id: 17,
        org_unit_id: 3,
        period: '2026-07',
        adjustment_type: 'PREV_MAKEUP',
        amount: '325.50',
        reason: '补发上月漏记加班',
        attachment_url: 'https://evidence.example/overtime.pdf',
        taxable: true,
        in_social_base: false,
        in_housing_base: true,
        created_by: 4,
        updated_by: 6,
        created_at: '2026-07-20T10:00:00Z',
        updated_at: '2026-07-20T10:00:00Z',
      },
    ])
    masterdataApi.fetchEmployees.mockResolvedValue({
      items: [{ id: 17, emp_no: 'E0017', name: '陈星' }],
      total: 1,
      page: 1,
      page_size: 500,
    })
  })

  afterEach(cleanup)

  it('shows the audited source ledger and its place in the gross-pay formula', async () => {
    renderPage()

    expect(await screen.findByText('上月补发 / 补扣')).toBeTruthy()
    expect(await screen.findByText('补发上月漏记加班')).toBeTruthy()
    expect(screen.getByText('325.50')).toBeTruthy()
    expect(screen.getByText(/应发工资.*上月补发.*上月补扣/)).toBeTruthy()
    expect(screen.getByRole('link', { name: '查看依据' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '登记补发或补扣' })).toBeTruthy()
    expect(screen.getByText('创建人 #4')).toBeTruthy()
    expect(screen.getByText('最后修改人 #6')).toBeTruthy()
  })

  it('does not grant payroll source access through generic adjustment permissions', async () => {
    auth.permissions = ['adjustment:read', 'adjustment:create', 'employee:read']

    renderPage()

    expect(await screen.findByText('上月补发 / 补扣')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '登记补发或补扣' })).toBeNull()
  })

  it('requires explicit tax, social-insurance, and housing-fund classifications without defaults', async () => {
    renderPage()

    const create = await screen.findByRole('button', { name: '登记补发或补扣' })
    await waitFor(() => expect((create as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(create)

    for (const label of ['计入个税计税', '计入社保基数', '计入公积金基数']) {
      const input = screen.getByLabelText(label)
      const item = input.closest('.ant-form-item')
      expect(item?.querySelector('.ant-select-selection-item')).toBeNull()
    }
  })

  it('backfills all three classifications when editing an audited source record', async () => {
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /修\s*改/ }))

    expect((screen.getByLabelText('员工') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByLabelText('项目') as HTMLInputElement).disabled).toBe(true)

    expect(screen.getByLabelText('计入个税计税').closest('.ant-form-item')?.textContent).toContain(
      '是',
    )
    expect(screen.getByLabelText('计入社保基数').closest('.ant-form-item')?.textContent).toContain(
      '否',
    )
    expect(
      screen.getByLabelText('计入公积金基数').closest('.ant-form-item')?.textContent,
    ).toContain('是')
  })

  it('loads every employee page for adjustment selection and filtering', async () => {
    masterdataApi.fetchEmployees.mockImplementation(({ page }: { page: number }) =>
      Promise.resolve(
        page === 2
          ? {
              items: [{ id: 518, emp_no: 'E0518', name: '赵飞' }],
              total: 501,
              page: 2,
              page_size: 500,
            }
          : {
              items: [{ id: 17, emp_no: 'E0017', name: '陈星' }],
              total: 501,
              page: 1,
              page_size: 500,
            },
      ),
    )

    renderPage()

    await waitFor(() =>
      expect(masterdataApi.fetchEmployees).toHaveBeenCalledWith({ page: 2, page_size: 500 }),
    )
    const create = screen.getByRole('button', { name: '登记补发或补扣' })
    await waitFor(() => expect((create as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(create)
    fireEvent.mouseDown(screen.getByLabelText('员工'))
    expect(await screen.findByText('E0518 · 赵飞')).toBeTruthy()
  })

  it.each([
    ['补发补扣来源', 'fetchMonthlyPayrollAdjustments'],
    ['员工目录', 'fetchEmployees'],
  ] as const)(
    'shows a %s read failure and blocks source mutations',
    async (label, failingQuery) => {
      if (failingQuery === 'fetchEmployees') {
        masterdataApi.fetchEmployees.mockRejectedValue(new Error('employee read failed'))
      } else {
        adjustmentsApi.fetchMonthlyPayrollAdjustments.mockRejectedValue(
          new Error('adjustment read failed'),
        )
      }
      renderPage()

      expect(await screen.findByText(`无法读取${label}，已停用登记和修改`)).toBeTruthy()
      const create = screen.getByRole('button', { name: '登记补发或补扣' })
      expect((create as HTMLButtonElement).disabled).toBe(true)
    },
  )
})
