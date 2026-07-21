import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const batchesApi = vi.hoisted(() => ({
  approveBatch: vi.fn(),
  confirmScope: vi.fn(),
  createBatch: vi.fn(),
  createDispute: vi.fn(),
  fetchAdjustments: vi.fn(),
  fetchBatches: vi.fn(),
  fetchConfirmations: vi.fn(),
  fetchDisputes: vi.fn(),
  fetchResults: vi.fn(),
  lockBatch: vi.fn(),
  reopenBatch: vi.fn(),
  resolveDispute: vi.fn(),
  runBatch: vi.fn(),
  supplementDispute: vi.fn(),
  unlockBatch: vi.fn(),
}))
const auth = vi.hoisted(() => ({ permissions: [] as string[] }))

vi.mock('../api/batches', () => batchesApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'payroll-auditor' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))

import PayrollPage from './PayrollPage'

function renderPage(seed?: (queryClient: QueryClient) => void) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  seed?.(queryClient)
  return render(
    <QueryClientProvider client={queryClient}>
      <PayrollPage />
    </QueryClientProvider>,
  )
}

describe('PayrollPage audit ledger', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.permissions = []
    batchesApi.fetchBatches.mockResolvedValue([
      {
        id: 7,
        period: '2026-07',
        attendance_start: '2026-06-26',
        attendance_end: '2026-07-25',
        status: 'LOCKED',
        calculation_status: 'CALCULATED',
        store_confirmation_status: 'CONFIRMED',
        hr_review_status: 'APPROVED',
        lock_status: 'LOCKED',
        calculated_at: '2026-07-26T09:30:00Z',
        hr_reviewed_by: 18,
        hr_reviewed_at: '2026-07-27T11:20:00Z',
        locked_by: 21,
        locked_at: '2026-07-28T16:45:00Z',
        version: 3,
      },
    ])
    batchesApi.fetchResults.mockResolvedValue([
      {
        employee_id: 101,
        emp_no: 'E0101',
        employee_name: '林晓',
        org_unit_id: 8,
        version: 2,
        batch_version: 3,
        department: 'DINING',
        actual_attendance_days: '21.00',
        statutory_holiday_days: '1.00',
        statutory_holiday_worked_days: '1.00',
        statutory_holiday_pay: '409.09',
        gross: '7012.34',
        deposit: '600.00',
        net: '6412.34',
        carry_forward: '125.50',
        deferred_deductions: '40.25',
        deferred_deposit: '600.00',
        has_error: false,
        lines: [
          {
            code: 'HOLIDAY',
            category: '法定节假日工资',
            formula: '3000 ÷ 22 × 1 × 3',
            amount: '409.09',
          },
        ],
        exceptions: [],
        warnings: [],
        rule_version: '2026.07',
      },
    ])
    batchesApi.fetchConfirmations.mockResolvedValue([])
    batchesApi.fetchDisputes.mockResolvedValue([])
    batchesApi.fetchAdjustments.mockResolvedValue([
      {
        id: 88,
        batch_id: 7,
        batch_version: 3,
        is_current_version: true,
        employee_id: 101,
        dispute_id: 66,
        item: 'ACTUAL_ATTENDANCE_DAYS',
        before_value: { actual_days: '20.00' },
        after_value: { actual_days: '21.00' },
        reason: '门店补交审批考勤单',
        applicant_id: 15,
        approver_id: 18,
        attachment_url: 'https://files.example.test/attendance-101.pdf',
        recompute_result: { status: 'COMPLETED', batch_version: 3, net: '6412.34' },
        created_at: '2026-07-27T10:15:00Z',
      },
    ])
  })

  afterEach(() => {
    cleanup()
  })

  it('presents the four batch controls with audit actors and timestamps', async () => {
    renderPage()

    expect(await screen.findByText('当前批次 · 2026-07')).toBeTruthy()
    expect(screen.getByText('核算状态')).toBeTruthy()
    expect(screen.getByText('已核算')).toBeTruthy()
    expect(screen.getByText('门店确认')).toBeTruthy()
    expect(screen.getByText('人事审核')).toBeTruthy()
    expect(screen.getByText('审核人 #18')).toBeTruthy()
    expect(screen.getByText('最终锁定')).toBeTruthy()
    expect(screen.getByText('锁定人 #21')).toBeTruthy()
    expect(screen.getByText(/2026-07-26 09:30/)).toBeTruthy()
    expect(screen.getByText(/2026-07-28 16:45/)).toBeTruthy()
  })

  it('shows the payroll totals and keeps the source formula in the expanded ledger row', async () => {
    renderPage()

    const ledger = await screen.findByRole('region', { name: '工资结果账本' })
    expect(ledger.getAttribute('tabindex')).toBe('0')
    await within(ledger).findByText('E0101')
    expect(within(ledger).getAllByText('应发工资').length).toBeGreaterThan(0)
    expect(within(ledger).getAllByText('法定工资').length).toBeGreaterThan(0)
    expect(within(ledger).getAllByText('押金').length).toBeGreaterThan(0)
    expect(within(ledger).getAllByText('实发工资').length).toBeGreaterThan(0)
    expect(within(ledger).getAllByText('结转').length).toBeGreaterThan(0)
    expect(within(ledger).getAllByText('待扣款结转').length).toBeGreaterThan(0)
    expect(within(ledger).getAllByText('待扣押金').length).toBeGreaterThan(0)
    expect(within(ledger).getByText('409.09')).toBeTruthy()
    expect(within(ledger).getByText('125.50')).toBeTruthy()
    expect(within(ledger).getByText('40.25')).toBeTruthy()
    expect(within(ledger).getAllByText('600.00').length).toBeGreaterThan(0)

    fireEvent.click(within(ledger).getByRole('button', { name: /expand row|展开行/i }))
    expect(await within(ledger).findByText('3000 ÷ 22 × 1 × 3')).toBeTruthy()
  })

  it('loads and displays complete modification evidence for the selected batch version', async () => {
    renderPage()

    const records = await screen.findByRole('region', { name: '工资修改记录' })
    expect(records.getAttribute('tabindex')).toBe('0')
    expect(await within(records).findByText('实际出勤天数')).toBeTruthy()
    expect(within(records).getByText(/actual_days: 20.00/)).toBeTruthy()
    expect(within(records).getByText(/actual_days: 21.00/)).toBeTruthy()
    expect(within(records).getByText('门店补交审批考勤单')).toBeTruthy()
    expect(within(records).getByText('申请人 #15')).toBeTruthy()
    expect(within(records).getByText('审批人 #18')).toBeTruthy()
    expect(within(records).getByRole('link', { name: '查看附件' })).toBeTruthy()
    expect(within(records).getByText('已重算 · v3')).toBeTruthy()
    expect(within(records).getByText('v3 · 当前')).toBeTruthy()
    await waitFor(() => expect(batchesApi.fetchAdjustments).toHaveBeenCalledWith(7))
  })

  it.each([
    ['工资结果', 'fetchResults'],
    ['门店复核范围', 'fetchConfirmations'],
    ['工资异议', 'fetchDisputes'],
  ] as const)(
    'shows a %s load error and disables final HR approval',
    async (label, failingQuery) => {
      auth.permissions = ['payroll:approve']
      batchesApi.fetchBatches.mockResolvedValue([
        {
          id: 7,
          period: '2026-07',
          attendance_start: '2026-06-26',
          attendance_end: '2026-07-25',
          status: 'PENDING_HR',
          calculation_status: 'CALCULATED',
          store_confirmation_status: 'CONFIRMED',
          hr_review_status: 'PENDING',
          lock_status: 'UNLOCKED',
          calculated_at: '2026-07-26T09:30:00Z',
          hr_reviewed_by: null,
          hr_reviewed_at: null,
          locked_by: null,
          locked_at: null,
          version: 3,
        },
      ])
      batchesApi[failingQuery].mockRejectedValue(new Error('read failed'))

      renderPage()

      expect(await screen.findByText(`无法读取${label}，关键复核操作已停用`)).toBeTruthy()
      const approve = await screen.findByRole('button', { name: '人事最终审核' })
      expect((approve as HTMLButtonElement).disabled).toBe(true)
    },
  )

  it('sends only the scope identifiers when a reviewer confirms a department', async () => {
    auth.permissions = ['payroll:review']
    batchesApi.fetchBatches.mockResolvedValue([
      {
        id: 7,
        period: '2026-07',
        attendance_start: '2026-06-26',
        attendance_end: '2026-07-25',
        status: 'PENDING_STORE_CONFIRM',
        calculation_status: 'CALCULATED',
        store_confirmation_status: 'PENDING',
        hr_review_status: 'NOT_STARTED',
        lock_status: 'UNLOCKED',
        calculated_at: null,
        hr_reviewed_by: null,
        hr_reviewed_at: null,
        locked_by: null,
        locked_at: null,
        version: 1,
      },
    ])
    batchesApi.fetchConfirmations.mockResolvedValue([
      {
        org_unit_id: 8,
        department: 'DINING',
        status: 'PENDING',
        confirmed_by: null,
        confirmed_at: null,
      },
    ])
    batchesApi.confirmScope.mockResolvedValue({})
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '确认无误' }))
    fireEvent.click(await screen.findByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(batchesApi.confirmScope).toHaveBeenCalledWith(7, {
        org_unit_id: 8,
        department: 'DINING',
      }),
    )
  })

  it('does not render an executable link for a non-HTTP attachment URL', async () => {
    batchesApi.fetchAdjustments.mockResolvedValue([
      {
        id: 89,
        batch_id: 7,
        batch_version: 3,
        is_current_version: true,
        employee_id: 101,
        dispute_id: null,
        item: 'ACTUAL_ATTENDANCE_DAYS',
        before_value: {},
        after_value: {},
        reason: 'bad legacy URL',
        applicant_id: 15,
        approver_id: 18,
        attachment_url: 'javascript:alert(1)',
        recompute_result: null,
        created_at: '2026-07-27T10:15:00Z',
      },
    ])
    renderPage()

    await screen.findByText('bad legacy URL')
    expect(screen.queryByRole('link', { name: '查看附件' })).toBeNull()
    expect(screen.getByText('无效附件地址')).toBeTruthy()
  })

  it('allows a reviewer to submit a dispute for any payroll result line', async () => {
    auth.permissions = ['payroll:review']
    batchesApi.fetchBatches.mockResolvedValue([
      {
        id: 7,
        period: '2026-07',
        attendance_start: '2026-06-26',
        attendance_end: '2026-07-25',
        status: 'PENDING_STORE_CONFIRM',
        calculation_status: 'CALCULATED',
        store_confirmation_status: 'PENDING',
        hr_review_status: 'NOT_STARTED',
        lock_status: 'UNLOCKED',
        calculated_at: null,
        hr_reviewed_by: null,
        hr_reviewed_at: null,
        locked_by: null,
        locked_at: null,
        version: 1,
      },
    ])
    batchesApi.createDispute.mockResolvedValue({ dispute_id: 77 })

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '提异议' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.mouseDown(within(dialog).getByRole('combobox', { name: '具体工资项' }))
    fireEvent.click(await screen.findByText('法定节假日工资（HOLIDAY）'))
    fireEvent.change(within(dialog).getByRole('textbox', { name: '修改意见' }), {
      target: { value: '法定工资金额有误' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(batchesApi.createDispute).toHaveBeenCalledWith(7, {
        employee_id: 101,
        salary_item: 'HOLIDAY',
        opinion: '法定工资金额有误',
      }),
    )
  })

  it('approves a holiday dispute by changing its day-level source and recomputing', async () => {
    auth.permissions = ['payroll:correct']
    batchesApi.resolveDispute.mockResolvedValue({ status: 'APPROVED' })
    batchesApi.fetchDisputes.mockResolvedValue([
      {
        id: 78,
        employee_id: 101,
        org_unit_id: 8,
        department: 'DINING',
        salary_item: 'HOLIDAY',
        opinion: '法定工资金额有误',
        raised_by: 15,
        status: 'OPEN',
        resolution: null,
        resolved_by: null,
        resolved_at: null,
        created_at: '2026-07-27T10:15:00Z',
        allowed_attendance_fields: [],
        correction_options: [
          {
            kind: 'HOLIDAY_WORK',
            label: '法定节假日逐日出勤',
            holiday_dates: [{ holiday_date: '2026-07-01', worked: false }],
          },
        ],
        events: [],
      },
    ])

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '人事处理' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.mouseDown(within(dialog).getByRole('combobox', { name: '处理结论' }))
    fireEvent.click(await screen.findByText('同意并更正来源后重算'))
    expect(within(dialog).getByRole('combobox', { name: '法定节假日' })).toBeTruthy()
    fireEvent.mouseDown(within(dialog).getByRole('combobox', { name: '是否出勤' }))
    fireEvent.click(await screen.findByText('已出勤'))
    fireEvent.change(within(dialog).getByRole('textbox', { name: '处理说明' }), {
      target: { value: '核实法定日排班' },
    })
    fireEvent.change(within(dialog).getByRole('textbox', { name: '证明附件地址' }), {
      target: { value: 'https://files.example.test/holiday.pdf' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(batchesApi.resolveDispute).toHaveBeenCalledWith(78, {
        decision: 'APPROVED',
        resolution: '核实法定日排班',
        attachment_url: 'https://files.example.test/holiday.pdf',
        source_correction: {
          kind: 'HOLIDAY_WORK',
          holiday_date: '2026-07-01',
          worked: true,
        },
      }),
    )
  })

  it('renders the performance source editor and submits the explicit source payload', async () => {
    auth.permissions = ['payroll:correct']
    batchesApi.resolveDispute.mockResolvedValue({ status: 'APPROVED' })
    batchesApi.fetchDisputes.mockResolvedValue([
      {
        id: 84,
        employee_id: 101,
        org_unit_id: 8,
        department: 'DINING',
        salary_item: 'PERF_BONUS',
        opinion: '绩效系数有误',
        raised_by: 15,
        status: 'OPEN',
        resolution: null,
        resolved_by: null,
        resolved_at: null,
        created_at: '2026-07-27T10:15:00Z',
        allowed_attendance_fields: [],
        correction_options: [
          {
            kind: 'PERFORMANCE',
            label: '当月绩效记录',
            coefficient: '1.000',
            score: '80.00',
            remark: '初评',
          },
        ],
        events: [],
      },
    ])
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '人事处理' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.mouseDown(within(dialog).getByRole('combobox', { name: '处理结论' }))
    fireEvent.click(await screen.findByText('同意并更正来源后重算'))
    fireEvent.change(within(dialog).getByRole('spinbutton', { name: '绩效系数' }), {
      target: { value: '1.2' },
    })
    fireEvent.change(within(dialog).getByRole('textbox', { name: '绩效备注' }), {
      target: { value: '终审批次' },
    })
    fireEvent.change(within(dialog).getByRole('textbox', { name: '处理说明' }), {
      target: { value: '按绩效审批表更正' },
    })
    fireEvent.change(within(dialog).getByRole('textbox', { name: '证明附件地址' }), {
      target: { value: 'https://files.example.test/performance.pdf' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(batchesApi.resolveDispute).toHaveBeenCalledWith(84, {
        decision: 'APPROVED',
        resolution: '按绩效审批表更正',
        attachment_url: 'https://files.example.test/performance.pdf',
        source_correction: {
          kind: 'PERFORMANCE',
          coefficient: 1.2,
          score: 80,
          remark: '终审批次',
        },
      }),
    )
  })

  it('keeps policy and carry disputes on a non-blocking dedicated workflow', async () => {
    auth.permissions = ['payroll:correct']
    batchesApi.fetchDisputes.mockResolvedValue([
      {
        id: 85,
        employee_id: 101,
        org_unit_id: 8,
        department: 'DINING',
        salary_item: 'IIT_WITHHOLDING',
        opinion: '个税来源需核验',
        raised_by: 15,
        status: 'OPEN',
        resolution: null,
        resolved_by: null,
        resolved_at: null,
        created_at: '2026-07-27T10:15:00Z',
        allowed_attendance_fields: [],
        correction_options: [
          {
            kind: 'WORKFLOW',
            label: '个税/社保专用来源流程',
            workflow: 'PAYROLL_POLICY_OR_TAX_OPENING',
            reason: '该项目涉及政策或累计计税来源，必须在专用来源流程核验后驳回或要求补充材料。',
          },
        ],
        events: [],
      },
    ])
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '人事处理' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('个税/社保专用来源流程')).toBeTruthy()
    expect(
      within(dialog).getByText(
        '该项目涉及政策或累计计税来源，必须在专用来源流程核验后驳回或要求补充材料。',
      ),
    ).toBeTruthy()
    fireEvent.mouseDown(within(dialog).getByRole('combobox', { name: '处理结论' }))
    expect(screen.queryByText('同意并更正来源后重算')).toBeNull()
    expect((await screen.findAllByText('驳回异议')).length).toBeGreaterThan(0)
    expect(await screen.findByText('要求补充材料')).toBeTruthy()
  })

  it('keeps attendance correction approval for an attendance-wage dispute', async () => {
    auth.permissions = ['payroll:correct']
    batchesApi.fetchDisputes.mockResolvedValue([
      {
        id: 79,
        employee_id: 101,
        org_unit_id: 8,
        department: 'DINING',
        salary_item: 'ATTEND_WAGE',
        opinion: '出勤天数有误',
        raised_by: 15,
        status: 'OPEN',
        resolution: null,
        resolved_by: null,
        resolved_at: null,
        created_at: '2026-07-27T10:15:00Z',
        allowed_attendance_fields: ['expected_days', 'worked_hours'],
      },
    ])

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '人事处理' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.mouseDown(within(dialog).getByRole('combobox', { name: '处理结论' }))
    fireEvent.click(await screen.findByText('同意并改考勤后重算'))

    expect(within(dialog).getByRole('spinbutton', { name: '应出勤天数' })).toBeTruthy()
    expect(within(dialog).getByRole('spinbutton', { name: '出勤工时' })).toBeTruthy()
    expect(within(dialog).queryByRole('spinbutton', { name: '实际出勤天数' })).toBeNull()
    expect(within(dialog).queryByRole('spinbutton', { name: '休息天数' })).toBeNull()
    expect(within(dialog).queryByRole('spinbutton', { name: '加班工时' })).toBeNull()
  })

  it('shows only approved day inputs for a special-position attendance dispute', async () => {
    auth.permissions = ['payroll:correct']
    batchesApi.fetchDisputes.mockResolvedValue([
      {
        id: 82,
        employee_id: 101,
        org_unit_id: 8,
        department: 'KITCHEN',
        salary_item: 'ATTEND_WAGE',
        opinion: '审批后的实际出勤天数有误',
        raised_by: 15,
        status: 'OPEN',
        resolution: null,
        resolved_by: null,
        resolved_at: null,
        created_at: '2026-07-27T10:15:00Z',
        allowed_attendance_fields: ['actual_days', 'expected_days'],
      },
    ])

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '人事处理' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.mouseDown(within(dialog).getByRole('combobox', { name: '处理结论' }))
    fireEvent.click(await screen.findByText('同意并改考勤后重算'))

    expect(within(dialog).getByRole('spinbutton', { name: '实际出勤天数' })).toBeTruthy()
    expect(within(dialog).getByRole('spinbutton', { name: '应出勤天数' })).toBeTruthy()
    expect(within(dialog).queryByRole('spinbutton', { name: '出勤工时' })).toBeNull()
    expect(within(dialog).queryByRole('spinbutton', { name: '休息天数' })).toBeNull()
    expect(within(dialog).queryByRole('spinbutton', { name: '加班工时' })).toBeNull()
  })

  it('shows only overtime hours for an overtime dispute', async () => {
    auth.permissions = ['payroll:correct']
    batchesApi.fetchDisputes.mockResolvedValue([
      {
        id: 83,
        employee_id: 101,
        org_unit_id: 8,
        department: 'DINING',
        salary_item: 'OVERTIME',
        opinion: '加班工时有误',
        raised_by: 15,
        status: 'OPEN',
        resolution: null,
        resolved_by: null,
        resolved_at: null,
        created_at: '2026-07-27T10:15:00Z',
        allowed_attendance_fields: ['overtime_hours'],
      },
    ])

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '人事处理' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.mouseDown(within(dialog).getByRole('combobox', { name: '处理结论' }))
    fireEvent.click(await screen.findByText('同意并改考勤后重算'))

    expect(within(dialog).getByRole('spinbutton', { name: '加班工时' })).toBeTruthy()
    expect(within(dialog).queryByRole('spinbutton', { name: '应出勤天数' })).toBeNull()
    expect(within(dialog).queryByRole('spinbutton', { name: '实际出勤天数' })).toBeNull()
    expect(within(dialog).queryByRole('spinbutton', { name: '出勤工时' })).toBeNull()
    expect(within(dialog).queryByRole('spinbutton', { name: '休息天数' })).toBeNull()
  })

  it('requires an attachment before approving an attendance dispute', async () => {
    auth.permissions = ['payroll:correct']
    batchesApi.fetchDisputes.mockResolvedValue([
      {
        id: 80,
        employee_id: 101,
        org_unit_id: 8,
        department: 'DINING',
        salary_item: 'ATTEND_WAGE',
        opinion: '出勤天数有误',
        raised_by: 15,
        status: 'OPEN',
        resolution: null,
        resolved_by: null,
        resolved_at: null,
        created_at: '2026-07-27T10:15:00Z',
        allowed_attendance_fields: ['expected_days', 'worked_hours'],
        events: [],
      },
    ])

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '人事处理' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.mouseDown(within(dialog).getByRole('combobox', { name: '处理结论' }))
    fireEvent.click(await screen.findByText('同意并改考勤后重算'))
    fireEvent.change(within(dialog).getByRole('spinbutton', { name: '出勤工时' }), {
      target: { value: '207' },
    })
    fireEvent.change(within(dialog).getByRole('textbox', { name: '处理说明' }), {
      target: { value: '按审批考勤更正' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    expect(await within(dialog).findByText('同意异议必须上传证明附件')).toBeTruthy()
    expect(batchesApi.resolveDispute).not.toHaveBeenCalled()
  })

  it('lets an in-scope reviewer supplement a NEED_MORE dispute and shows its event timeline', async () => {
    auth.permissions = ['payroll:review']
    batchesApi.supplementDispute.mockResolvedValue({ status: 'OPEN' })
    batchesApi.fetchDisputes.mockResolvedValue([
      {
        id: 81,
        employee_id: 101,
        org_unit_id: 8,
        department: 'DINING',
        salary_item: 'HOLIDAY',
        opinion: '法定工资金额有误',
        raised_by: 15,
        status: 'NEED_MORE',
        resolution: '请补充排班审批单',
        resolved_by: 18,
        resolved_at: '2026-07-27T11:00:00Z',
        created_at: '2026-07-27T10:15:00Z',
        events: [
          {
            id: 1,
            event_type: 'NEED_MORE',
            note: '请补充排班审批单',
            actor_id: 18,
            attachment_url: null,
            created_at: '2026-07-27T11:00:00Z',
          },
        ],
      },
    ])

    renderPage()

    const disputeCard = (await screen.findByText('异议与处理记录')).closest(
      '.ant-card',
    ) as HTMLElement
    fireEvent.click(await within(disputeCard).findByRole('button', { name: /expand row|展开行/i }))
    expect(await screen.findByText('请补充排班审批单')).toBeTruthy()
    expect(screen.getByText('要求补充材料')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '补充材料' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByRole('textbox', { name: '补充说明' }), {
      target: { value: '已补充负责人签字排班单' },
    })
    fireEvent.change(within(dialog).getByRole('textbox', { name: '证明附件地址' }), {
      target: { value: 'https://files.example.test/roster.pdf' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(batchesApi.supplementDispute).toHaveBeenCalledWith(81, {
        note: '已补充负责人签字排班单',
        attachment_url: 'https://files.example.test/roster.pdf',
      }),
    )
  })

  it('blocks batch lifecycle actions when the batch list refresh fails', async () => {
    auth.permissions = ['payroll:run']
    const draftBatch = {
      id: 7,
      period: '2026-07',
      attendance_start: '2026-06-26',
      attendance_end: '2026-07-25',
      status: 'DRAFT',
      calculation_status: 'PENDING',
      store_confirmation_status: 'NOT_STARTED',
      hr_review_status: 'NOT_STARTED',
      lock_status: 'UNLOCKED',
      calculated_at: null,
      hr_reviewed_by: null,
      hr_reviewed_at: null,
      locked_by: null,
      locked_at: null,
      version: 1,
    }
    batchesApi.fetchBatches.mockRejectedValue(new Error('batch read failed'))

    renderPage((queryClient) => {
      queryClient.setQueryData(['payrollBatches', 'payroll-auditor'], [draftBatch])
    })

    expect(await screen.findByText('无法读取薪资批次，批次操作已停用')).toBeTruthy()
    expect((screen.getByRole('button', { name: '新建批次' }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    expect((screen.getByRole('button', { name: '执行核算' }) as HTMLButtonElement).disabled).toBe(
      true,
    )
  })

  it('blocks scope confirmation while critical reads are still loading', async () => {
    auth.permissions = ['payroll:review']
    batchesApi.fetchBatches.mockResolvedValue([
      {
        id: 7,
        period: '2026-07',
        attendance_start: '2026-06-26',
        attendance_end: '2026-07-25',
        status: 'PENDING_STORE_CONFIRM',
        calculation_status: 'CALCULATED',
        store_confirmation_status: 'PENDING',
        hr_review_status: 'NOT_STARTED',
        lock_status: 'UNLOCKED',
        calculated_at: null,
        hr_reviewed_by: null,
        hr_reviewed_at: null,
        locked_by: null,
        locked_at: null,
        version: 1,
      },
    ])
    batchesApi.fetchResults.mockImplementation(() => new Promise(() => undefined))
    batchesApi.fetchConfirmations.mockResolvedValue([
      {
        org_unit_id: 8,
        department: 'DINING',
        status: 'PENDING',
        confirmed_by: null,
        confirmed_at: null,
      },
    ])

    renderPage()

    const confirm = await screen.findByRole('button', { name: '确认无误' })
    expect((confirm as HTMLButtonElement).disabled).toBe(true)
  })
})
