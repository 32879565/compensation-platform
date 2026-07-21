import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const exportApi = vi.hoisted(() => ({
  bankPaymentExportFilename: vi.fn((period: string) => `bank-payment-${period}.xlsx`),
  exportBankPayment: vi.fn(),
  exportErrorMessage: vi.fn(),
  exportIndividualIncomeTax: vi.fn(),
  exportPayroll: vi.fn(),
  exportSocialInsurance: vi.fn(),
  individualIncomeTaxExportFilename: vi.fn((period: string) =>
    `individual-income-tax-${period}.xlsx`,
  ),
  payrollExportFilename: vi.fn((period: string) => `payroll-${period}.xlsx`),
  socialInsuranceExportFilename: vi.fn((period: string) => `social-insurance-${period}.xlsx`),
}))
const auth = vi.hoisted(() => ({ permissions: [] as string[] }))

vi.mock('../api/exports', () => exportApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))

import ExportPage from './ExportPage'

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ExportPage />
    </QueryClientProvider>,
  )
}

describe('ExportPage regulated reconciliation downloads', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    auth.permissions = ['export:data', 'employee:pii']
    const workbook = new Blob(['workbook'])
    exportApi.exportPayroll.mockResolvedValue(workbook)
    exportApi.exportSocialInsurance.mockResolvedValue(workbook)
    exportApi.exportIndividualIncomeTax.mockResolvedValue(workbook)
    exportApi.exportBankPayment.mockResolvedValue(workbook)
    exportApi.exportErrorMessage.mockResolvedValue('导出失败，请稍后重试。')
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:test') })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('shows all three regulated download controls and the generic-file warning', () => {
    renderPage()

    expect(screen.getByRole('button', { name: '下载社保对账 XLSX' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '下载个税对账 XLSX' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '下载银行付款对账 XLSX' })).toBeTruthy()
    expect(screen.getByText('仅生成通用对账文件，不是官方申报或银行导入格式。')).toBeTruthy()
  })

  it('does not show regulated controls without the employee PII permission', () => {
    auth.permissions = ['export:data']

    renderPage()

    expect(screen.queryByRole('button', { name: '下载社保对账 XLSX' })).toBeNull()
    expect(
      screen.getByText('社保、个税和银行付款对账文件需要数据导出及员工敏感信息权限。'),
    ).toBeTruthy()
  })

  it('does not show any export controls without the data export permission', () => {
    auth.permissions = ['employee:pii']

    renderPage()

    expect(screen.queryByRole('button', { name: '导出 XLSX' })).toBeNull()
    expect(screen.queryByRole('button', { name: '下载社保对账 XLSX' })).toBeNull()
    expect(screen.getByText('数据导出需要数据导出权限。')).toBeTruthy()
  })

  it('downloads the selected-month social insurance reconciliation workbook', async () => {
    renderPage()
    fireEvent.change(screen.getByLabelText('导出计薪周期'), { target: { value: '2026-06' } })
    fireEvent.click(screen.getByRole('button', { name: '下载社保对账 XLSX' }))

    await waitFor(() => expect(exportApi.exportSocialInsurance).toHaveBeenCalledWith('2026-06'))
    expect(exportApi.socialInsuranceExportFilename).toHaveBeenCalledWith('2026-06')
  })

  it('displays the backend error detail when a regulated download fails', async () => {
    exportApi.exportIndividualIncomeTax.mockRejectedValue({ response: { data: new Blob() } })
    exportApi.exportErrorMessage.mockResolvedValue('当前周期没有可导出的工资结果')
    renderPage()

    fireEvent.click(screen.getByRole('button', { name: '下载个税对账 XLSX' }))

    expect(await screen.findByText('个税对账导出失败：当前周期没有可导出的工资结果')).toBeTruthy()
  })
})
