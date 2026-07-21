import { api } from './client'

async function exportWorkbook(path: string, period: string): Promise<Blob> {
  return (
    await api.get<Blob>(path, {
      params: { period },
      responseType: 'blob',
    })
  ).data
}

export async function exportPayroll(period: string): Promise<Blob> {
  return exportWorkbook('/api/exports/payroll', period)
}

export async function exportSocialInsurance(period: string): Promise<Blob> {
  return exportWorkbook('/api/exports/social-insurance', period)
}

export async function exportIndividualIncomeTax(period: string): Promise<Blob> {
  return exportWorkbook('/api/exports/individual-income-tax', period)
}

export async function exportBankPayment(period: string): Promise<Blob> {
  return exportWorkbook('/api/exports/bank-payment', period)
}

export function payrollExportFilename(period: string): string {
  return `payroll-${period}.xlsx`
}

export function socialInsuranceExportFilename(period: string): string {
  return `social-insurance-${period}.xlsx`
}

export function individualIncomeTaxExportFilename(period: string): string {
  return `individual-income-tax-${period}.xlsx`
}

export function bankPaymentExportFilename(period: string): string {
  return `bank-payment-${period}.xlsx`
}

function responseData(error: unknown): unknown {
  if (typeof error !== 'object' || error === null || !('response' in error)) return undefined
  const response = (error as { response?: { data?: unknown } }).response
  return response?.data
}

function detailFromPayload(payload: unknown): string | undefined {
  if (typeof payload !== 'object' || payload === null || !('detail' in payload)) {
    return undefined
  }
  const detail = (payload as { detail?: unknown }).detail
  return typeof detail === 'string' ? detail : undefined
}

function readBlobText(blob: Blob): Promise<string> {
  const withText = blob as Blob & { text?: () => Promise<string> }
  if (typeof withText.text === 'function') return withText.text()

  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(reader.error)
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '')
    reader.readAsText(blob)
  })
}

/** Decode FastAPI JSON errors returned as blobs by Axios's download mode. */
export async function exportErrorMessage(error: unknown): Promise<string> {
  const payload = responseData(error)
  const inlineDetail = detailFromPayload(payload)
  if (inlineDetail) return inlineDetail

  if (payload instanceof Blob) {
    try {
      return detailFromPayload(JSON.parse(await readBlobText(payload))) ?? '导出失败，请稍后重试。'
    } catch {
      // An HTML/proxy response is still a failed download, but has no safe
      // application detail to surface to the operator.
    }
  }
  return '导出失败，请稍后重试。'
}
