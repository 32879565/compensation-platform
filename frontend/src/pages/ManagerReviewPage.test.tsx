import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const managerApi = vi.hoisted(() => ({
  createManagerDispute: vi.fn(),
  exchangeManagerSession: vi.fn(),
  fetchManagerReview: vi.fn(),
  fetchManagerReviewConfig: vi.fn(),
  confirmManagerReview: vi.fn(),
}))
const bridge = vi.hoisted(() => ({ requestDingTalkAuthCode: vi.fn() }))

vi.mock('../api/managerReview', () => managerApi)
vi.mock('../dingtalk/bridge', () => bridge)

import ManagerReviewPage from './ManagerReviewPage'

const reviewId = '0123456789abcdef0123456789abcdef'
const review = {
  review_id: reviewId,
  period: '2026-07',
  store_name: 'Review Store',
  department: 'DINING' as const,
  confirmation_status: 'PENDING',
  employees: [
    {
      employee_id: 7,
      emp_no: 'E-007',
      employee_name: 'Dining Employee',
      actual_attendance_days: '22.00',
      statutory_holiday_days: '1.00',
      statutory_holiday_worked_days: '1.00',
      gross: '6000.00',
      deposit: '0.00',
      net: '6000.00',
      carry_forward: '0.00',
      lines: [
        { code: 'ATTEND_WAGE', name: 'Attendance wage', amount: '5500.00' },
        { code: 'HOUSING', name: 'Housing allowance', amount: '500.00' },
      ],
    },
  ],
}

function renderPage() {
  return render(
    <MemoryRouter
      initialEntries={[`/manager-review/${reviewId}`]}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <Routes>
        <Route path="/manager-review/:reviewId" element={<ManagerReviewPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ManagerReviewPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    managerApi.fetchManagerReviewConfig.mockResolvedValue({
      enabled: true,
      client_id: 'ding-client',
      corp_id: 'ding-corp',
    })
    bridge.requestDingTalkAuthCode.mockResolvedValue('one-time-code')
    managerApi.exchangeManagerSession.mockResolvedValue({
      access_token: 'manager-token',
      token_type: 'bearer',
      expires_in: 900,
    })
    managerApi.fetchManagerReview.mockResolvedValue(review)
    managerApi.createManagerDispute.mockResolvedValue({
      dispute_id: 19,
      batch_status: 'HAS_DISPUTE',
    })
    managerApi.confirmManagerReview.mockResolvedValue({
      confirmation_status: 'CONFIRMED',
      batch_status: 'PENDING_HR',
    })
  })

  afterEach(cleanup)

  it('uses DingTalk login, shows employee details, and raises an item-level dispute', async () => {
    renderPage()

    expect(await screen.findByText('Dining Employee')).toBeInTheDocument()
    expect(screen.getByText('¥6,000.00')).toBeInTheDocument()
    expect(bridge.requestDingTalkAuthCode).toHaveBeenCalledWith({
      clientId: 'ding-client',
      corpId: 'ding-corp',
    })
    expect(managerApi.exchangeManagerSession).toHaveBeenCalledWith({
      review_id: reviewId,
      auth_code: 'one-time-code',
    })
    expect(managerApi.fetchManagerReview).toHaveBeenCalledWith(reviewId, 'manager-token')

    fireEvent.click(screen.getByRole('button', { name: '对 Attendance wage 提出异议' }))
    fireEvent.change(screen.getByLabelText('异议说明'), {
      target: { value: 'Attendance days should be checked.' },
    })
    fireEvent.click(screen.getByRole('button', { name: '提交异议' }))

    await waitFor(() =>
      expect(managerApi.createManagerDispute).toHaveBeenCalledWith(
        reviewId,
        'manager-token',
        {
          employee_id: 7,
          salary_item: 'ATTEND_WAGE',
          opinion: 'Attendance days should be checked.',
        },
      ),
    )
  })

  it('confirms the exact department after an explicit second click', async () => {
    renderPage()
    await screen.findByText('Dining Employee')

    fireEvent.click(screen.getByRole('button', { name: '确认本部门工资' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认无误' }))

    await waitFor(() =>
      expect(managerApi.confirmManagerReview).toHaveBeenCalledWith(reviewId, 'manager-token'),
    )
  })

  it('does not expose payroll when DingTalk H5 authentication is unavailable', async () => {
    managerApi.fetchManagerReviewConfig.mockResolvedValue({
      enabled: false,
      client_id: null,
      corp_id: null,
    })
    renderPage()

    expect(await screen.findByText('请从钉钉工作通知中打开此页面')).toBeInTheDocument()
    expect(managerApi.fetchManagerReview).not.toHaveBeenCalled()
  })
})
