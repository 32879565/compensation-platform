import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import { fetchMyPayslip, fetchMyPayslipPeriods } from './payslips'

describe('payslip API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.get.mockResolvedValue({ data: [] })
  })

  it('uses self-service-only endpoints', async () => {
    await fetchMyPayslipPeriods()
    await fetchMyPayslip('2026-05')

    expect(client.get).toHaveBeenNthCalledWith(1, '/api/payslips/me/periods')
    expect(client.get).toHaveBeenNthCalledWith(2, '/api/payslips/me', {
      params: { period: '2026-05' },
    })
  })
})
