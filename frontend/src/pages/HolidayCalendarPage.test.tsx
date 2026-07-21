import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const holidaysApi = vi.hoisted(() => ({
  fetchHolidayDates: vi.fn(),
  fetchHolidayCalendarPeriod: vi.fn(),
  fetchHolidayWork: vi.fn(),
  upsertHolidayDate: vi.fn(),
  finalizeHolidayCalendar: vi.fn(),
  unfinalizeHolidayCalendar: vi.fn(),
  setHolidayWork: vi.fn(),
}))
const masterdataApi = vi.hoisted(() => ({ fetchEmployees: vi.fn() }))
const auth = vi.hoisted(() => ({ permissions: [] as string[] }))

vi.mock('../api/holidays', () => holidaysApi)
vi.mock('../api/masterdata', () => masterdataApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'holiday-hr' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))

import HolidayCalendarPage from './HolidayCalendarPage'

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <HolidayCalendarPage />
    </QueryClientProvider>,
  )
}

describe('HolidayCalendarPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.permissions = [
      'holiday_calendar:read',
      'holiday_calendar:write',
      'attendance:read',
      'attendance:write',
      'employee:read',
    ]
    holidaysApi.fetchHolidayCalendarPeriod.mockResolvedValue({
      period: '2026-07',
      is_finalized: false,
      finalized_by: null,
      finalized_at: null,
    })
    holidaysApi.fetchHolidayDates.mockResolvedValue([
      {
        holiday_date: '2026-07-01',
        name: '法定假日',
        eligible_employment_types: ['FULL_TIME'],
      },
    ])
    holidaysApi.fetchHolidayWork.mockResolvedValue([])
    masterdataApi.fetchEmployees.mockResolvedValue({
      items: [
        {
          id: 17,
          emp_no: 'E0017',
          name: '陈星',
          employment_type: 'FULL_TIME',
          department: 'DINING',
        },
      ],
      total: 1,
      page: 1,
      page_size: 500,
    })
  })

  afterEach(cleanup)

  it('shows the monthly source ledger, calculation rule, and HR actions', async () => {
    renderPage()

    expect(await screen.findByText('法定节假日台账')).toBeTruthy()
    expect(await screen.findByText('法定假日')).toBeTruthy()
    expect(screen.getByText('未确认')).toBeTruthy()
    expect(screen.getByText(/3000.*当月应出勤天数.*出勤 3 倍.*未出勤 1 倍/)).toBeTruthy()
    expect(screen.getByRole('button', { name: '新增法定日' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '确认本月日历' })).toBeTruthy()
    expect(screen.getByRole('combobox', { name: '选择员工' })).toBeTruthy()
  })

  it('keeps maintenance controls hidden for a read-only payroll viewer', async () => {
    auth.permissions = ['holiday_calendar:read']

    renderPage()

    expect(await screen.findByText('法定节假日台账')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '新增法定日' })).toBeNull()
    expect(screen.queryByRole('button', { name: '确认本月日历' })).toBeNull()
    expect(screen.queryByRole('combobox', { name: '选择员工' })).toBeNull()
  })

  it('loads every employee page instead of silently stopping at the first 500 people', async () => {
    masterdataApi.fetchEmployees
      .mockResolvedValueOnce({
        items: [{ id: 17, emp_no: 'E0017', name: '陈星' }],
        total: 501,
        page: 1,
        page_size: 500,
      })
      .mockResolvedValueOnce({
        items: [{ id: 501, emp_no: 'E0501', name: '周远' }],
        total: 501,
        page: 2,
        page_size: 500,
      })

    renderPage()

    await waitFor(() =>
      expect(masterdataApi.fetchEmployees).toHaveBeenCalledWith({ page: 2, page_size: 500 }),
    )
  })

  it('shows a work-ledger load failure and disables overwriting until the source is readable', async () => {
    holidaysApi.fetchHolidayWork.mockRejectedValue(new Error('ledger unavailable'))
    renderPage()

    const employee = await screen.findByRole('combobox', { name: '选择员工' })
    fireEvent.mouseDown(employee)
    fireEvent.click(await screen.findByText('E0017 · 陈星'))

    expect(await screen.findByText('无法读取该员工的法定日出勤记录，已停用登记操作')).toBeTruthy()
    const saveSource = await screen.findByRole('button', { name: '登记出勤' })
    expect((saveSource as HTMLButtonElement).disabled).toBe(true)
  })

  it('submits an explicit correction reason when overwriting an existing work record', async () => {
    holidaysApi.fetchHolidayWork.mockResolvedValue([
      {
        employee_id: 17,
        holiday_date: '2026-07-01',
        worked: false,
        reason: '原排班',
        evidence_url: null,
      },
    ])
    holidaysApi.setHolidayWork.mockResolvedValue({})
    renderPage()

    fireEvent.mouseDown(await screen.findByRole('combobox', { name: '选择员工' }))
    fireEvent.click(await screen.findByText('E0017 · 陈星'))
    const register = await screen.findByRole('button', { name: '登记出勤' })
    await waitFor(() => expect((register as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(register)
    fireEvent.change(await screen.findByLabelText('更正原因（覆盖已有记录时必填）'), {
      target: { value: '门店补交审批排班' },
    })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(holidaysApi.setHolidayWork).toHaveBeenCalledWith(
        17,
        '2026-07-01',
        expect.objectContaining({
          worked: false,
          correction_reason: '门店补交审批排班',
        }),
      ),
    )
  })

  it.each([
    ['法定日列表', 'fetchHolidayDates'],
    ['法定日历确认状态', 'fetchHolidayCalendarPeriod'],
  ] as const)(
    'shows a %s failure and blocks calendar mutations while state is unknown',
    async (label, failingQuery) => {
      holidaysApi[failingQuery].mockRejectedValue(new Error('calendar read failed'))
      renderPage()

      expect(await screen.findByText(`无法读取${label}，已停用日历维护操作`)).toBeTruthy()
      if (failingQuery === 'fetchHolidayCalendarPeriod') {
        expect(screen.getByText('状态未知')).toBeTruthy()
      }
      expect(
        (screen.getByRole('button', { name: '新增法定日' }) as HTMLButtonElement).disabled,
      ).toBe(true)
      expect(
        (screen.getByRole('button', { name: '确认本月日历' }) as HTMLButtonElement).disabled,
      ).toBe(true)
    },
  )
})
