import { api } from './client'

export type ContributionKind =
  | 'PENSION'
  | 'MEDICAL'
  | 'UNEMPLOYMENT'
  | 'WORK_INJURY'
  | 'MATERNITY'
  | 'HOUSING'

export interface SocialRule {
  kind: ContributionKind
  employee_rate: string
  employer_rate: string
  base_min: string
  base_max: string | null
}

export interface TaxBracket {
  upper_bound: string | null
  rate: string
  quick_deduction: string
}

export interface DerivedIncomeRule {
  code: 'OVERTIME' | 'HOLIDAY'
  taxable: boolean
  in_social_base: boolean
  in_housing_base: boolean
}

export interface PayrollPolicyInput {
  city: string
  effective_from: string
  social_rules: SocialRule[]
  monthly_basic_deduction: string
  tax_brackets: TaxBracket[]
  derived_income_rules: DerivedIncomeRule[]
}

export interface PayrollPolicy extends PayrollPolicyInput {
  id: number
  is_finalized: boolean
  finalized_by: number | null
  finalized_at: string | null
}

export type PayrollPolicyUpdate = Partial<PayrollPolicyInput>

export interface PayrollPolicyQuery {
  city?: string
  includeDrafts?: boolean
}

export async function fetchPayrollPolicies(
  query: PayrollPolicyQuery = {},
): Promise<PayrollPolicy[]> {
  const params: Record<string, string | boolean> = {}
  if (query.city?.trim()) params.city = query.city.trim()
  if (query.includeDrafts) params.include_drafts = true
  return (await api.get<PayrollPolicy[]>('/api/payroll-policies', { params })).data
}

export async function createPayrollPolicy(payload: PayrollPolicyInput): Promise<PayrollPolicy> {
  return (await api.post<PayrollPolicy>('/api/payroll-policies', payload)).data
}

export async function updatePayrollPolicy(
  policyId: number,
  payload: PayrollPolicyUpdate,
): Promise<PayrollPolicy> {
  return (await api.patch<PayrollPolicy>(`/api/payroll-policies/${policyId}`, payload)).data
}

export async function finalizePayrollPolicy(policyId: number): Promise<PayrollPolicy> {
  return (await api.post<PayrollPolicy>(`/api/payroll-policies/${policyId}/finalize`)).data
}

export interface TaxOpeningInput {
  tax_year: number
  through_period: string
  employment_months_to_date: number
  taxable_income: string
  employee_contribution: string
  special_deduction: string
  tax_withheld: string
  evidence_ref: string
}

export interface TaxOpening extends TaxOpeningInput {
  id: number
  employee_id: number
  revision: number
  is_finalized: boolean
  finalized_by: number | null
  finalized_at: string | null
  supersedes_id: number | null
  superseded_by: number | null
  superseded_at: string | null
}

export async function fetchTaxOpenings(employeeId: number): Promise<TaxOpening[]> {
  return (await api.get<TaxOpening[]>(`/api/employees/${employeeId}/tax-ytd-openings`)).data
}

export async function createTaxOpening(
  employeeId: number,
  payload: TaxOpeningInput,
): Promise<TaxOpening> {
  return (await api.post<TaxOpening>(`/api/employees/${employeeId}/tax-ytd-openings`, payload)).data
}

export async function updateTaxOpening(
  employeeId: number,
  openingId: number,
  payload: TaxOpeningInput,
): Promise<TaxOpening> {
  return (
    await api.patch<TaxOpening>(`/api/employees/${employeeId}/tax-ytd-openings/${openingId}`, payload)
  ).data
}

export async function finalizeTaxOpening(
  employeeId: number,
  openingId: number,
): Promise<TaxOpening> {
  return (
    await api.post<TaxOpening>(`/api/employees/${employeeId}/tax-ytd-openings/${openingId}/finalize`)
  ).data
}

export async function supersedeTaxOpening(
  employeeId: number,
  openingId: number,
  payload: TaxOpeningInput,
): Promise<TaxOpening> {
  return (
    await api.post<TaxOpening>(
      `/api/employees/${employeeId}/tax-ytd-openings/${openingId}/supersede`,
      payload,
    )
  ).data
}
