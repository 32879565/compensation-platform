import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import {
  fetchMonthlyPayrollAdjustments,
  upsertMonthlyPayrollAdjustment,
} from './payrollAdjustments'

describe('monthly payroll adjustment API client', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    client.get.mockResolvedValue({ data: [] })
    client.put.mockResolvedValue({ data: {} })
  })

  it('filters the source ledger by payroll month and optional employee', async () => {
    await fetchMonthlyPayrollAdjustments('2026-07')
    await fetchMonthlyPayrollAdjustments('2026-07', 17)

    expect(client.get).toHaveBeenNthCalledWith(1, '/api/payroll-adjustments', {
      params: { period: '2026-07' },
    })
    expect(client.get).toHaveBeenNthCalledWith(2, '/api/payroll-adjustments', {
      params: { period: '2026-07', employee_id: 17 },
    })
  })

  it('upserts a reasoned and evidenced prior-period source amount', async () => {
    const input = {
      amount: 325.5,
      reason: '补发上月漏记加班',
      attachment_url: 'https://evidence.example/overtime.pdf',
      taxable: true,
      in_social_base: false,
      in_housing_base: true,
    }

    await upsertMonthlyPayrollAdjustment(17, '2026-07', 'PREV_MAKEUP', input)

    expect(client.put).toHaveBeenCalledWith(
      '/api/payroll-adjustments/17/2026-07/PREV_MAKEUP',
      input,
    )
  })
})
