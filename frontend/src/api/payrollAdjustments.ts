import { api } from './client'

export type MonthlyPayrollAdjustmentType = 'PREV_MAKEUP' | 'PREV_DEDUCT'

export interface MonthlyPayrollAdjustment {
  id: number
  employee_id: number
  org_unit_id: number
  period: string
  adjustment_type: MonthlyPayrollAdjustmentType
  amount: string
  reason: string
  attachment_url: string
  taxable: boolean | null
  in_social_base: boolean | null
  in_housing_base: boolean | null
  created_by: number
  updated_by: number
  created_at: string
  updated_at: string
}

export interface MonthlyPayrollAdjustmentInput {
  amount: number
  reason: string
  attachment_url: string
  taxable: boolean
  in_social_base: boolean
  in_housing_base: boolean
}

export async function fetchMonthlyPayrollAdjustments(
  period: string,
  employeeId?: number,
): Promise<MonthlyPayrollAdjustment[]> {
  return (
    await api.get<MonthlyPayrollAdjustment[]>('/api/payroll-adjustments', {
      params: {
        period,
        ...(employeeId === undefined ? {} : { employee_id: employeeId }),
      },
    })
  ).data
}

export async function upsertMonthlyPayrollAdjustment(
  employeeId: number,
  period: string,
  adjustmentType: MonthlyPayrollAdjustmentType,
  input: MonthlyPayrollAdjustmentInput,
): Promise<MonthlyPayrollAdjustment> {
  return (
    await api.put<MonthlyPayrollAdjustment>(
      `/api/payroll-adjustments/${employeeId}/${period}/${adjustmentType}`,
      input,
    )
  ).data
}
