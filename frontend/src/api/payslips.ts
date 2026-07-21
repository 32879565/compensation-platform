import { api } from './client'

export interface PayslipLine {
  code: string
  category: string
  formula: string
  amount: string
}

export interface PayslipPeriod {
  period: string
  locked_at: string | null
}

export interface Payslip {
  period: string
  locked_at: string | null
  actual_attendance_days: string
  gross: string
  deposit: string
  net: string
  carry_forward: string
  rule_version: string
  lines: PayslipLine[]
  warnings: string[]
}

export async function fetchMyPayslipPeriods(): Promise<PayslipPeriod[]> {
  return (await api.get<PayslipPeriod[]>('/api/payslips/me/periods')).data
}

export async function fetchMyPayslip(period: string): Promise<Payslip> {
  return (await api.get<Payslip>('/api/payslips/me', { params: { period } })).data
}
