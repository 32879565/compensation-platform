import { api } from './client'

export interface SalaryRecord {
  id: number
  period: string
  emp_no: string | null
  name: string
  store_name: string
  source: 'HISTORICAL' | 'IMPORT' | 'PAYROLL_RUN' | string
  fields: Record<string, unknown>
}

export interface SalaryRecordPage {
  items: SalaryRecord[]
  total: number
  page: number
  page_size: number
}

export interface SalaryRecordQuery {
  name?: string
  emp_no?: string
  period?: string
  store?: string
  page: number
  page_size: number
}

export async function fetchSalaryRecords(query: SalaryRecordQuery): Promise<SalaryRecordPage> {
  const params: Record<string, string | number> = {
    page: query.page,
    page_size: query.page_size,
  }
  if (query.name?.trim()) params.name = query.name.trim()
  if (query.emp_no?.trim()) params.emp_no = query.emp_no.trim()
  if (query.period?.trim()) params.period = query.period.trim()
  if (query.store?.trim()) params.store = query.store.trim()

  return (await api.get<SalaryRecordPage>('/api/salary-records', { params })).data
}
