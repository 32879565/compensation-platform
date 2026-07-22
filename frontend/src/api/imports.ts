import { api } from './client'

export interface SalaryImportBatchSummary {
  id: number
  filename: string
  period: string | null
  status: 'PARSED' | 'CONFIRMED' | 'FAILED'
  total_rows: number
  error_rows: number
}

export interface SalaryImportStagingRow {
  row_index: number
  period: string
  emp_no: string | null
  name: string
  store_name: string
  parsed_fields: Record<string, unknown>
  errors: unknown[]
  status: 'OK' | 'ERROR'
}

export interface SalaryImportConfirmResult {
  written: number
}

export interface SalaryImportPublishTarget {
  store_id: number
  store_name: string
  employee_count: number
  departments: Array<'DINING' | 'KITCHEN'>
  locked: boolean
}

export interface SalaryImportPublishResult {
  import_batch_id: number
  payroll_batch_id: number
  batch_version: number
  employees: number
  scopes: number
  routed: number
  configuration_failures: number
  existing: number
  already_published?: boolean
  sandbox: boolean
}

export async function uploadSalaryImport(
  period: string,
  file: File,
): Promise<SalaryImportBatchSummary> {
  const formData = new FormData()
  formData.append('file', file)
  return (
    await api.post<SalaryImportBatchSummary>('/api/imports', formData, {
      params: { period },
    })
  ).data
}

export async function fetchSalaryImportRows(batchId: number): Promise<SalaryImportStagingRow[]> {
  return (await api.get<SalaryImportStagingRow[]>(`/api/imports/${batchId}`)).data
}

export async function confirmSalaryImport(batchId: number): Promise<SalaryImportConfirmResult> {
  return (await api.post<SalaryImportConfirmResult>(`/api/imports/${batchId}/confirm`)).data
}

export async function fetchSalaryImportPublishTargets(
  batchId: number,
): Promise<SalaryImportPublishTarget[]> {
  return (await api.get<SalaryImportPublishTarget[]>(`/api/imports/${batchId}/publish-targets`))
    .data
}

export async function publishSalaryImport(
  batchId: number,
  storeIds: number[],
): Promise<SalaryImportPublishResult> {
  return (
    await api.post<SalaryImportPublishResult>(`/api/imports/${batchId}/publish`, {
      store_ids: storeIds,
    })
  ).data
}
