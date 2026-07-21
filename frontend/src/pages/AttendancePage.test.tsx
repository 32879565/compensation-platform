import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const attendanceApi = vi.hoisted(() => ({
  fetchPerformance: vi.fn(),
  importPerformance: vi.fn(),
  fetchAttendanceSchedules: vi.fn(),
  createAttendanceSchedule: vi.fn(),
  updateAttendanceSchedule: vi.fn(),
  generateExpectedAttendance: vi.fn(),
  isPerformanceImportFile: vi.fn((file: File) => /\.(xlsx|xlsm)$/i.test(file.name)),
  PERFORMANCE_IMPORT_ACCEPT: '.xlsx,.xlsm',
}))
const apiClient = vi.hoisted(() => ({ api: { get: vi.fn(), put: vi.fn() } }))
const auth = vi.hoisted(() => ({ permissions: [] as string[] }))
const masterdataApi = vi.hoisted(() => ({ fetchEmployees: vi.fn(), fetchOrgUnits: vi.fn() }))
const dingtalkApi = vi.hoisted(() => ({
  fetchDingTalkIntegration: vi.fn(),
  fetchDingTalkAttendanceSnapshot: vi.fn(),
  previewDingTalkAttendance: vi.fn(),
  refreshDingTalkAttendance: vi.fn(),
}))

vi.mock('../api/attendance', () => attendanceApi)
vi.mock('../api/client', () => apiClient)
vi.mock('../api/masterdata', () => masterdataApi)
vi.mock('../api/dingtalk', () => dingtalkApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'performance-admin' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))

import AttendancePage from './AttendancePage'

const employee = {
  id: 7,
  emp_no: 'E1001',
  name: '陈星',
  org_unit_id: 1,
  job_grade_id: null,
  employment_type: 'FULL_TIME' as const,
  department: 'OTHER' as const,
  position_title: null,
  is_special_position: false,
  status: 'ACTIVE' as const,
  hire_date: null,
  probation_end: null,
  leave_date: null,
  social_city: null,
  id_card: null,
  bank_account: null,
}
const laterPageEmployee = {
  ...employee,
  id: 201,
  emp_no: 'E0201',
  name: '赵飞',
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <AttendancePage />
    </QueryClientProvider>,
  )
}

describe('AttendancePage performance workflow', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    auth.permissions = ['attendance:read', 'attendance:write', 'employee:read']
    apiClient.api.get.mockResolvedValue({ data: [] })
    apiClient.api.put.mockResolvedValue({})
    masterdataApi.fetchEmployees.mockResolvedValue({
      items: [employee],
      total: 1,
      page: 1,
      page_size: 200,
    })
    masterdataApi.fetchOrgUnits.mockResolvedValue([])
    attendanceApi.fetchPerformance.mockResolvedValue([
      {
        employee_id: 7,
        period: '2026-07',
        coefficient: '1.250',
        score: '96.50',
        remark: '表现优秀',
      },
    ])
    attendanceApi.fetchAttendanceSchedules.mockResolvedValue([
      {
        id: 11,
        name: '厅面全职规则',
        org_unit_id: null,
        employment_type: 'FULL_TIME',
        department: 'DINING',
        position_title: null,
        is_special_position: false,
        weekly_rest_days: [5, 6],
        monthly_expected_days: null,
        effective_from: '2026-01-01',
        effective_to: null,
        priority: 10,
        is_active: true,
      },
    ])
    attendanceApi.createAttendanceSchedule.mockResolvedValue({ id: 12 })
    attendanceApi.updateAttendanceSchedule.mockImplementation(
      (_id: number, payload: Record<string, unknown>) => Promise.resolve({ id: 11, ...payload }),
    )
    attendanceApi.generateExpectedAttendance.mockResolvedValue({
      period: '2026-07',
      generated: 18,
      adjusted_preserved: 2,
    })
    dingtalkApi.fetchDingTalkIntegration.mockResolvedValue({
      mode: 'sandbox',
      credentials_configured: true,
      app_id_configured: true,
      public_base_url_configured: false,
      ready_for_live: false,
      read_sync_enabled: true,
      read_sync_ready: true,
    })
    dingtalkApi.previewDingTalkAttendance.mockResolvedValue({
      period: '2026-07',
      matched_employees: 2,
      employees_with_records: 2,
      total_records: 3,
      ambiguous_directory_users: 1,
      unmatched_directory_users: 1,
      items: [
        {
          employee_id: 7,
          emp_no: 'E1001',
          name: '陈星',
          record_count: 2,
          normal_count: 1,
          late_count: 1,
          early_count: 0,
          absent_count: 0,
          not_signed_count: 0,
          other_count: 0,
        },
      ],
    })
    dingtalkApi.fetchDingTalkAttendanceSnapshot.mockResolvedValue({
      period: '2026-07',
      status: 'COMPLETED',
      matched_employees: 2,
      employees_with_records: 2,
      total_records: 3,
      ambiguous_directory_users: 1,
      unmatched_directory_users: 1,
      source_start: '2026-07-01T00:00:00Z',
      source_end: '2026-07-31T23:59:59Z',
      started_at: '2026-07-21T12:00:00Z',
      refreshed_at: '2026-07-21T12:30:00Z',
      error_code: null,
      items: [
        {
          employee_id: 7,
          emp_no: 'E1001',
          name: '陈星',
          record_count: 2,
          normal_count: 1,
          late_count: 1,
          early_count: 0,
          absent_count: 0,
          not_signed_count: 0,
          other_count: 0,
        },
      ],
    })
    dingtalkApi.refreshDingTalkAttendance.mockResolvedValue({
      period: '2026-07',
      status: 'QUEUED',
      matched_employees: 0,
      employees_with_records: 0,
      total_records: 0,
      ambiguous_directory_users: 0,
      unmatched_directory_users: 0,
      source_start: null,
      source_end: null,
      started_at: null,
      refreshed_at: null,
      error_code: null,
      items: [],
    })
  })

  afterEach(() => {
    cleanup()
  })

  it('lists current-period performance and reloads it when the month changes', async () => {
    renderPage()

    expect(await screen.findByText('绩效列表')).toBeTruthy()
    expect(await screen.findByText('1.250')).toBeTruthy()
    expect(screen.getByText('96.50')).toBeTruthy()
    expect(screen.getByText('表现优秀')).toBeTruthy()

    fireEvent.change(screen.getByLabelText(/计薪周期/), { target: { value: '2026-06' } })

    await waitFor(() => expect(attendanceApi.fetchPerformance).toHaveBeenLastCalledWith('2026-06'))
  })

  it('hides the performance import control from read-only users', async () => {
    auth.permissions = ['attendance:read']

    renderPage()

    await screen.findByText('绩效列表')
    expect(screen.queryByRole('button', { name: '导入绩效' })).toBeNull()
  })

  it('does not request the employee directory without permission and explains the ID fallback', async () => {
    auth.permissions = ['attendance:read']

    renderPage()

    expect(
      await screen.findByText('当前账号没有员工目录权限；考勤和绩效记录将仅显示员工 ID。'),
    ).toBeTruthy()
    expect(masterdataApi.fetchEmployees).not.toHaveBeenCalled()
  })

  it('loads every employee page needed to identify performance records', async () => {
    masterdataApi.fetchEmployees.mockImplementation((query: { page?: number }) =>
      Promise.resolve(
        query.page === 2
          ? { items: [laterPageEmployee], total: 201, page: 2, page_size: 200 }
          : { items: [employee], total: 201, page: 1, page_size: 200 },
      ),
    )
    attendanceApi.fetchPerformance.mockResolvedValue([
      {
        employee_id: 201,
        period: '2026-07',
        coefficient: '1.000',
        score: null,
        remark: null,
      },
    ])

    renderPage()

    expect((await screen.findAllByText('E0201')).length).toBeGreaterThan(1)
    expect(masterdataApi.fetchEmployees).toHaveBeenCalledWith({ page: 2, page_size: 200 })
  })

  it('shows an attendance read failure and blocks entry instead of applying defaults', async () => {
    apiClient.api.get.mockRejectedValue(new Error('attendance read failed'))

    renderPage()

    expect(await screen.findByText('考勤来源加载失败，已停用考勤录入。')).toBeTruthy()
    const entry = await screen.findByRole('button', { name: /录\s*入/ })
    expect((entry as HTMLButtonElement).disabled).toBe(true)
  })

  it('keeps expected-day controls read-only without the dedicated adjustment permission', async () => {
    apiClient.api.get.mockResolvedValue({
      data: [
        {
          employee_id: 7,
          expected_days: '22.00',
          actual_days: '21.00',
          worked_hours: '189.00',
          rest_days: '4.00',
          overtime_hours: '2.00',
          holiday_worked_days: '0.00',
          leave_days: '1.00',
        },
      ],
    })

    renderPage()

    const entry = await screen.findByRole('button', { name: /录\s*入/ })
    await waitFor(() => expect((entry as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(entry)

    expect((screen.getByLabelText('应出勤天数') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByLabelText('应出勤调整原因') as HTMLTextAreaElement).disabled).toBe(true)
    expect((screen.getByLabelText('实出勤天数') as HTMLInputElement).disabled).toBe(false)
  })

  it('requires a new reason before submitting an expected-days adjustment', async () => {
    auth.permissions = [
      'attendance:read',
      'attendance:write',
      'attendance:expected_days:adjust',
      'employee:read',
    ]
    apiClient.api.get.mockResolvedValue({
      data: [
        {
          employee_id: 7,
          expected_days: '22.00',
          expected_days_adjust_reason: 'Prior roster exception',
          actual_days: '21.00',
          worked_hours: '189.00',
          rest_days: '4.00',
          overtime_hours: '2.00',
          holiday_worked_days: '0.00',
          leave_days: '1.00',
        },
      ],
    })

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /录\s*入/ }))
    fireEvent.change(screen.getByLabelText('应出勤天数'), { target: { value: '21' } })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    expect(await screen.findByText('调整应出勤天数必须填写新的调整原因')).toBeTruthy()
    expect(apiClient.api.put).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('应出勤调整原因'), {
      target: { value: 'New approved roster correction' },
    })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() => expect(apiClient.api.put).toHaveBeenCalledTimes(1))
    expect(apiClient.api.put.mock.calls[0][1]).toEqual(
      expect.objectContaining({
        expected_days: 21,
        expected_days_adjust_reason: 'New approved roster correction',
      }),
    )
  })

  it('requires proof when an unlocked-batch correction reason is entered', async () => {
    apiClient.api.get.mockResolvedValue({
      data: [
        {
          employee_id: 7,
          expected_days: '22.00',
          expected_days_adjust_reason: null,
          actual_days: '21.00',
          worked_hours: '189.00',
          rest_days: '4.00',
          overtime_hours: '2.00',
          holiday_worked_days: '0.00',
          leave_days: '1.00',
        },
      ],
    })

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /录\s*入/ }))
    fireEvent.change(screen.getByLabelText('实出勤天数'), { target: { value: '20' } })
    fireEvent.change(screen.getByLabelText('已解锁批次更正原因'), {
      target: { value: 'Correct approved attendance source' },
    })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    expect(await screen.findByText('已解锁批次更正必须填写证明附件地址')).toBeTruthy()
    expect(apiClient.api.put).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('证明附件地址（已解锁批次更正必填）'), {
      target: { value: 'https://files.example.test/attendance-proof.pdf' },
    })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() => expect(apiClient.api.put).toHaveBeenCalledTimes(1))
  })

  it('shows the server 422 detail when an unlocked-batch correction is rejected', async () => {
    apiClient.api.get.mockResolvedValue({
      data: [
        {
          employee_id: 7,
          expected_days: '22.00',
          expected_days_adjust_reason: null,
          actual_days: '21.00',
          worked_hours: '189.00',
          rest_days: '4.00',
          overtime_hours: '2.00',
          holiday_worked_days: '0.00',
          leave_days: '1.00',
        },
      ],
    })
    apiClient.api.put.mockRejectedValue({
      response: {
        status: 422,
        data: { detail: '更正已解锁批次的源数据必须上传证明附件' },
      },
    })

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /录\s*入/ }))
    fireEvent.change(screen.getByLabelText('实出勤天数'), { target: { value: '20' } })
    fireEvent.change(screen.getByLabelText('已解锁批次更正原因'), {
      target: { value: 'Correct approved attendance source' },
    })
    fireEvent.change(screen.getByLabelText('证明附件地址（已解锁批次更正必填）'), {
      target: { value: 'https://files.example.test/attendance-proof.pdf' },
    })
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    expect(await screen.findByText('更正已解锁批次的源数据必须上传证明附件')).toBeTruthy()
  })

  it('submits the preserved required expected days for an ordinary attendance edit', async () => {
    apiClient.api.get.mockResolvedValue({
      data: [
        {
          employee_id: 7,
          expected_days: '22.00',
          expected_days_adjust_reason: null,
          actual_days: '21.00',
          worked_hours: '189.00',
          rest_days: '4.00',
          overtime_hours: '2.00',
          holiday_worked_days: '0.00',
          leave_days: '1.00',
        },
      ],
    })

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /录\s*入/ }))
    fireEvent.click(screen.getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() => expect(apiClient.api.put).toHaveBeenCalledTimes(1))
    const payload = apiClient.api.put.mock.calls[0][1]
    expect(payload).toEqual(expect.objectContaining({ expected_days: 22, actual_days: 21 }))
    expect(payload).not.toHaveProperty('expected_days_adjust_reason')
  })

  it('keeps nullable worked hours blank and defaults new actual attendance to zero', async () => {
    apiClient.api.get.mockResolvedValue({
      data: [
        {
          employee_id: 7,
          expected_days: '22.00',
          expected_days_adjust_reason: null,
          actual_days: '0.00',
          worked_hours: null,
          rest_days: '0.00',
          overtime_hours: '0.00',
          holiday_worked_days: '0.00',
          leave_days: '0.00',
        },
      ],
    })

    const firstRender = renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /录\s*入/ }))
    expect((screen.getByLabelText('出勤工时') as HTMLInputElement).value).toBe('')
    firstRender.unmount()

    apiClient.api.get.mockResolvedValue({ data: [] })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /录\s*入/ }))
    expect((screen.getByLabelText('实出勤天数') as HTMLInputElement).value).toBe('0')
  })

  it('keeps attendance records visible by employee ID when the directory fails', async () => {
    masterdataApi.fetchEmployees.mockRejectedValue(new Error('directory unavailable'))
    apiClient.api.get.mockResolvedValue({
      data: [
        {
          employee_id: 7,
          expected_days: '22.00',
          expected_days_adjust_reason: null,
          actual_days: '13.00',
          worked_hours: null,
          rest_days: '0.00',
          overtime_hours: '0.00',
          holiday_worked_days: '0.00',
          leave_days: '0.00',
        },
      ],
    })

    renderPage()

    expect(await screen.findByText('13.00')).toBeTruthy()
    expect(screen.getAllByText('员工 ID #7').length).toBeGreaterThan(0)
  })

  it('imports an accepted workbook, shows its summary, and refreshes the list', async () => {
    attendanceApi.importPerformance.mockResolvedValue({ matched: 1, skipped: ['E404'] })
    renderPage()

    await screen.findByText('绩效列表')
    const input = screen.getByLabelText('选择绩效导入文件') as HTMLInputElement
    expect(input.accept).toContain('.xlsx')
    expect(input.accept).toContain('.xlsm')
    const file = new File(['workbook'], 'performance.xlsm', {
      type: 'application/vnd.ms-excel.sheet.macroEnabled.12',
    })

    fireEvent.change(input, { target: { files: [file] } })

    await waitFor(() =>
      expect(attendanceApi.importPerformance).toHaveBeenCalledWith(expect.any(String), file),
    )
    expect(await screen.findByText('绩效导入完成：成功匹配 1 条，跳过 1 条。')).toBeTruthy()
    expect(screen.getByText('跳过工号：E404')).toBeTruthy()
    await waitFor(() => expect(attendanceApi.fetchPerformance).toHaveBeenCalledTimes(2))
  })

  it('shows the server failure reason when an import is rejected', async () => {
    attendanceApi.importPerformance.mockRejectedValue({
      response: { data: { detail: '该周期的批次已锁定' } },
    })
    renderPage()

    await screen.findByText('绩效列表')
    const file = new File(['workbook'], 'performance.xlsx')
    fireEvent.change(screen.getByLabelText('选择绩效导入文件'), {
      target: { files: [file] },
    })

    expect(await screen.findByText('导入失败：该周期的批次已锁定')).toBeTruthy()
  })

  it('keeps an in-flight import result scoped to the month it was submitted for', async () => {
    let resolveImport: ((result: { matched: number; skipped: string[] }) => void) | undefined
    attendanceApi.importPerformance.mockImplementation(
      () =>
        new Promise<{ matched: number; skipped: string[] }>((resolve) => {
          resolveImport = resolve
        }),
    )
    renderPage()

    await screen.findByText('绩效列表')
    const periodInput = screen.getByLabelText(/计薪周期/) as HTMLInputElement
    const submittedPeriod = periodInput.value
    const otherPeriod = submittedPeriod === '2026-06' ? '2026-05' : '2026-06'
    const file = new File(['workbook'], 'performance.xlsx')
    fireEvent.change(screen.getByLabelText('选择绩效导入文件'), {
      target: { files: [file] },
    })
    await waitFor(() => expect(attendanceApi.importPerformance).toHaveBeenCalledTimes(1))

    fireEvent.change(periodInput, { target: { value: otherPeriod } })
    await waitFor(() =>
      expect(attendanceApi.fetchPerformance).toHaveBeenLastCalledWith(otherPeriod),
    )
    if (!resolveImport) throw new Error('import promise did not start')
    resolveImport({ matched: 1, skipped: [] })

    await waitFor(() => expect(screen.queryByText(/绩效导入完成/)).toBeNull())
    fireEvent.change(periodInput, { target: { value: submittedPeriod } })
    expect(await screen.findByText('绩效导入完成：成功匹配 1 条，跳过 0 条。')).toBeTruthy()
  })

  it('loads cached DingTalk attendance and queues refresh without writing payroll attendance', async () => {
    auth.permissions = [
      'attendance:read',
      'attendance:write',
      'employee:read',
      'notification:manage',
    ]
    renderPage()

    expect(await screen.findByText('钉钉考勤')).toBeTruthy()
    expect(await screen.findByText('共 3 条打卡结果')).toBeTruthy()
    expect(screen.getByText(/不会写入计薪考勤/)).toBeTruthy()
    expect(dingtalkApi.fetchDingTalkAttendanceSnapshot).toHaveBeenCalledWith(expect.any(String))

    const button = await screen.findByRole('button', { name: '刷新钉钉考勤' })
    fireEvent.click(button)

    await waitFor(() =>
      expect(dingtalkApi.refreshDingTalkAttendance).toHaveBeenCalledWith(expect.any(String)),
    )
    expect(apiClient.api.put).not.toHaveBeenCalled()
  })

  it('keeps schedule maintenance completely hidden from ordinary store users', async () => {
    auth.permissions = ['attendance:read', 'attendance:write', 'employee:read']

    renderPage()

    await screen.findByText('绩效列表')
    expect(screen.queryByText('应出勤规则')).toBeNull()
    expect(attendanceApi.fetchAttendanceSchedules).not.toHaveBeenCalled()
  })

  it('lets schedule readers inspect rules without exposing write actions', async () => {
    auth.permissions = ['attendance:read', 'attendance_schedule:read', 'employee:read']

    renderPage()

    expect(await screen.findByText('应出勤规则')).toBeTruthy()
    expect(await screen.findByText('厅面全职规则')).toBeTruthy()
    expect(attendanceApi.fetchAttendanceSchedules).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: '新建规则' })).toBeNull()
    expect(screen.queryByRole('button', { name: /生成 .* 应出勤/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /编\s*辑/ })).toBeNull()
  })

  it('only offers store organizations when maintaining an attendance schedule', async () => {
    auth.permissions = ['attendance:read', 'attendance_schedule:write', 'employee:read', 'org:read']
    masterdataApi.fetchOrgUnits.mockResolvedValue([
      {
        id: 1,
        code: 'GROUP',
        name: '集团总部',
        type: 'GROUP',
        parent_id: null,
        city: null,
        status: 'ACTIVE',
      },
      {
        id: 2,
        code: 'S002',
        name: '二店',
        type: 'STORE',
        parent_id: 1,
        city: null,
        status: 'ACTIVE',
      },
    ])

    renderPage()

    await screen.findByText('应出勤规则')
    fireEvent.click(screen.getByRole('button', { name: '新建规则' }))
    fireEvent.mouseDown(screen.getByLabelText('适用组织'))

    expect(await screen.findByText('二店 (S002)')).toBeTruthy()
    expect(screen.queryByText('集团总部 (GROUP)')).toBeNull()
  })

  it('lets HR generate the selected month and reports generated and preserved counts', async () => {
    auth.permissions = [
      'attendance:read',
      'attendance:write',
      'attendance_schedule:write',
      'employee:read',
    ]

    renderPage()

    expect(await screen.findByText('应出勤规则')).toBeTruthy()
    expect(await screen.findByText('厅面全职规则')).toBeTruthy()
    fireEvent.change(screen.getByLabelText(/计薪周期/), { target: { value: '2026-07' } })
    fireEvent.click(screen.getByRole('button', { name: '生成 2026-07 应出勤' }))

    await waitFor(() =>
      expect(attendanceApi.generateExpectedAttendance).toHaveBeenCalledWith('2026-07'),
    )
    expect(await screen.findByText('已生成 18 人，保留人工调整 2 人。')).toBeTruthy()
  })

  it('shows every employee-specific generation error returned by the API', async () => {
    auth.permissions = ['attendance:read', 'attendance_schedule:write', 'employee:read']
    attendanceApi.generateExpectedAttendance.mockRejectedValue({
      response: {
        data: {
          detail: {
            message: '应出勤生成失败',
            errors: ['E1001: 未找到匹配规则', 'E1002: 存在同优先级规则'],
          },
        },
      },
    })

    renderPage()

    await screen.findByText('应出勤规则')
    const generate = screen.getByRole('button', { name: new RegExp(`生成 .* 应出勤`) })
    await waitFor(() => expect((generate as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(generate)

    expect(await screen.findByText('应出勤生成失败')).toBeTruthy()
    expect(screen.getByText('E1001: 未找到匹配规则')).toBeTruthy()
    expect(screen.getByText('E1002: 存在同优先级规则')).toBeTruthy()
  })

  it('blocks expected-attendance generation when the rule list cannot be read', async () => {
    auth.permissions = ['attendance:read', 'attendance_schedule:write', 'employee:read']
    attendanceApi.fetchAttendanceSchedules.mockRejectedValue({
      response: { data: { detail: '规则服务暂不可用' } },
    })

    renderPage()

    expect(await screen.findByText('应出勤规则加载失败')).toBeTruthy()
    const generate = screen.getByRole('button', { name: /生成 .* 应出勤/ })
    expect((generate as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(generate)
    expect(attendanceApi.generateExpectedAttendance).not.toHaveBeenCalled()
  })

  it('lets HR deactivate an active rule with the full replacement payload', async () => {
    auth.permissions = ['attendance:read', 'attendance_schedule:write', 'employee:read']

    renderPage()

    await screen.findByText('厅面全职规则')
    fireEvent.click(screen.getByRole('button', { name: /停\s*用/ }))
    fireEvent.click(await screen.findByRole('button', { name: '确认停用' }))

    await waitFor(() =>
      expect(attendanceApi.updateAttendanceSchedule).toHaveBeenCalledWith(
        11,
        expect.objectContaining({
          name: '厅面全职规则',
          weekly_rest_days: [5, 6],
          is_active: false,
        }),
      ),
    )
  })

  it('lets HR create a rule with auditable matching and calendar defaults', async () => {
    auth.permissions = ['attendance:read', 'attendance_schedule:write', 'employee:read']

    renderPage()

    const selectedPeriod = (screen.getByLabelText(/计薪周期/) as HTMLInputElement).value
    await screen.findByText('应出勤规则')
    fireEvent.click(screen.getByRole('button', { name: '新建规则' }))
    fireEvent.change(screen.getByLabelText('规则名称'), {
      target: { value: '集团通用双休' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存规则' }))

    await waitFor(() =>
      expect(attendanceApi.createAttendanceSchedule).toHaveBeenCalledWith(
        expect.objectContaining({
          name: '集团通用双休',
          weekly_rest_days: [5, 6],
          effective_from: `${selectedPeriod}-01`,
          is_active: true,
        }),
      ),
    )
  })

  it('loads a rule into the editor and replaces the complete rule', async () => {
    auth.permissions = ['attendance:read', 'attendance_schedule:write', 'employee:read']

    renderPage()

    await screen.findByText('厅面全职规则')
    fireEvent.click(screen.getByRole('button', { name: /编\s*辑/ }))
    const name = screen.getByLabelText('规则名称')
    fireEvent.change(name, { target: { value: '厅面全职规则（修订）' } })
    fireEvent.click(screen.getByRole('button', { name: '保存规则' }))

    await waitFor(() =>
      expect(attendanceApi.updateAttendanceSchedule).toHaveBeenCalledWith(
        11,
        expect.objectContaining({
          name: '厅面全职规则（修订）',
          employment_type: 'FULL_TIME',
          department: 'DINING',
          weekly_rest_days: [5, 6],
          priority: 10,
        }),
      ),
    )
  })

  it('serializes a cleared fixed monthly day count as null', async () => {
    auth.permissions = ['attendance:read', 'attendance_schedule:write', 'employee:read']
    attendanceApi.fetchAttendanceSchedules.mockResolvedValue([
      {
        id: 11,
        name: '固定月天数规则',
        org_unit_id: null,
        employment_type: 'FULL_TIME',
        department: 'DINING',
        position_title: null,
        is_special_position: false,
        weekly_rest_days: [5, 6],
        monthly_expected_days: '22.00',
        effective_from: '2026-01-01',
        effective_to: null,
        priority: 10,
        is_active: true,
      },
    ])

    renderPage()

    await screen.findByText('固定月天数规则')
    fireEvent.click(screen.getByRole('button', { name: /编\s*辑/ }))
    fireEvent.change(screen.getByLabelText('固定月应出勤天数'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: '保存规则' }))

    await waitFor(() =>
      expect(attendanceApi.updateAttendanceSchedule).toHaveBeenCalledWith(
        11,
        expect.objectContaining({ monthly_expected_days: null }),
      ),
    )
  })
})
