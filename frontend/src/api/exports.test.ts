import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import {
  bankPaymentExportFilename,
  exportBankPayment,
  exportErrorMessage,
  exportIndividualIncomeTax,
  exportPayroll,
  exportSocialInsurance,
  individualIncomeTaxExportFilename,
  payrollExportFilename,
  socialInsuranceExportFilename,
} from './exports'

describe('export API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.get.mockResolvedValue({ data: new Blob() })
  })

  it('requests the payroll workbook as a blob', async () => {
    await exportPayroll('2026-07')

    expect(client.get).toHaveBeenCalledWith('/api/exports/payroll', {
      params: { period: '2026-07' },
      responseType: 'blob',
    })
  })

  it('keeps the requested period in the download filename', () => {
    expect(payrollExportFilename('2026-07')).toBe('payroll-2026-07.xlsx')
  })

  it('requests all regulated reconciliation workbooks as blobs', async () => {
    await exportSocialInsurance('2026-07')
    await exportIndividualIncomeTax('2026-07')
    await exportBankPayment('2026-07')

    expect(client.get).toHaveBeenNthCalledWith(1, '/api/exports/social-insurance', {
      params: { period: '2026-07' },
      responseType: 'blob',
    })
    expect(client.get).toHaveBeenNthCalledWith(2, '/api/exports/individual-income-tax', {
      params: { period: '2026-07' },
      responseType: 'blob',
    })
    expect(client.get).toHaveBeenNthCalledWith(3, '/api/exports/bank-payment', {
      params: { period: '2026-07' },
      responseType: 'blob',
    })
  })

  it('uses period-specific filenames for the regulated reconciliation workbooks', () => {
    expect(socialInsuranceExportFilename('2026-07')).toBe('social-insurance-2026-07.xlsx')
    expect(individualIncomeTaxExportFilename('2026-07')).toBe(
      'individual-income-tax-2026-07.xlsx',
    )
    expect(bankPaymentExportFilename('2026-07')).toBe('bank-payment-2026-07.xlsx')
  })

  it('reads FastAPI error details from a blob download response', async () => {
    const error = {
      response: {
        data: new Blob([JSON.stringify({ detail: '当前周期没有已锁定的工资结果' })], {
          type: 'application/json',
        }),
      },
    }

    await expect(exportErrorMessage(error)).resolves.toBe('当前周期没有已锁定的工资结果')
  })
})
