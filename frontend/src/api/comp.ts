import { api } from './client'

export type ComponentType =
  | 'BASE'
  | 'COMPREHENSIVE'
  | 'PERFORMANCE'
  | 'POSITION'
  | 'ALLOWANCE'
  | 'HOUSING'
  | 'OVERTIME'
  | 'DEDUCTION'

// The API calls the variable allowance mode FLOATING.  Keep its wire value
// here even though the UI presents it as a variable allowance.
export type AllowanceKind = 'FIXED' | 'FLOATING'
export type ComponentCatalogStatus = 'active' | 'inactive' | 'all'

export interface SalaryComponent {
  id: number
  code: string
  name: string
  component_type: ComponentType
  taxable: boolean
  in_social_base: boolean
  in_housing_base: boolean
  prorate_by_attendance: boolean
  allowance_kind: AllowanceKind | null
  sort_order: number
  is_active: boolean
  deactivated_at: string | null
  updated_at: string
  calculation_locked: boolean
  calculation_lock_reason: string | null
}

export interface ComponentCreateFormInput {
  code: string
  name: string
  component_type: ComponentType
  taxable?: boolean
  in_social_base?: boolean
  in_housing_base?: boolean
  prorate_by_attendance?: boolean
  allowance_kind?: AllowanceKind
  sort_order?: number
}

interface ComponentCreateBase {
  code: string
  name: string
  taxable?: boolean
  in_social_base?: boolean
  in_housing_base?: boolean
  sort_order?: number
}

export type CreateComponentInput =
  | (ComponentCreateBase & {
      component_type: 'ALLOWANCE'
      allowance_kind: AllowanceKind
      prorate_by_attendance?: boolean
    })
  | (ComponentCreateBase & {
      component_type: Exclude<ComponentType, 'ALLOWANCE'>
      allowance_kind?: never
      prorate_by_attendance?: false
    })

export function normalizeComponentCreateInput(
  input: ComponentCreateFormInput,
): CreateComponentInput {
  const { allowance_kind, component_type, prorate_by_attendance, ...component } = input

  if (component_type === 'ALLOWANCE') {
    if (!allowance_kind) {
      throw new Error('Allowance kind is required for allowance components')
    }
    return { ...component, component_type, allowance_kind, prorate_by_attendance }
  }

  return { ...component, component_type, prorate_by_attendance: false }
}

export interface SalaryStructureItem {
  component_id: number
  amount: string
  effective_from: string
  effective_to: string | null
  source_adjustment_id: number | null
  source_reason: string | null
  source_attachment_url: string | null
}

export interface SalaryStructureHistoryItem extends SalaryStructureItem {
  id: number
  revision: number
  component_code: string
  component_name: string
  component_type: ComponentType
  component_is_active: boolean
}

export interface CompaSummary {
  total: string
  band_status: 'IN_BAND' | 'OVER' | 'UNDER' | 'NO_BAND'
  compa_ratio: string | null
  band_min: string | null
  band_mid: string | null
  band_max: string | null
}

export interface SalaryStructure {
  items: SalaryStructureItem[]
  compa: CompaSummary
}

export interface SetComponentAmountInput {
  amount: number
  effective_from: string
  correction_reason?: string
  attachment_url?: string
}

export interface InitialSalaryStructureItemInput {
  component_id: number
  amount: number
  reason?: string
  attachment_url?: string
}

export interface InitialSalaryStructureInput {
  effective_from: string
  items: InitialSalaryStructureItemInput[]
}

export interface ComponentCatalogQuery {
  status?: ComponentCatalogStatus
}

export async function fetchComponents(
  query: ComponentCatalogQuery = { status: 'active' },
): Promise<SalaryComponent[]> {
  return (await api.get<SalaryComponent[]>('/api/salary-components', { params: query })).data
}

export async function createComponent(payload: CreateComponentInput): Promise<SalaryComponent> {
  return (await api.post<SalaryComponent>('/api/salary-components', payload)).data
}

type MutableComponentFields = Partial<
  Pick<
    SalaryComponent,
    | 'name'
    | 'taxable'
    | 'in_social_base'
    | 'in_housing_base'
    | 'prorate_by_attendance'
    | 'allowance_kind'
    | 'sort_order'
  >
>

export type UpdateComponentInput = MutableComponentFields & {
  expected_updated_at: string
  reason?: string
}

export interface ComponentLifecycleInput {
  reason: string
  expected_updated_at: string
}

export async function updateComponent(
  componentId: number,
  payload: UpdateComponentInput,
): Promise<SalaryComponent> {
  return (await api.patch<SalaryComponent>(`/api/salary-components/${componentId}`, payload)).data
}

export async function deactivateComponent(
  componentId: number,
  payload: ComponentLifecycleInput,
): Promise<SalaryComponent> {
  return (
    await api.post<SalaryComponent>(`/api/salary-components/${componentId}/deactivate`, payload)
  ).data
}

export async function restoreComponent(
  componentId: number,
  payload: ComponentLifecycleInput,
): Promise<SalaryComponent> {
  return (await api.post<SalaryComponent>(`/api/salary-components/${componentId}/restore`, payload))
    .data
}

export async function fetchSalaryStructure(
  employeeId: number,
  onDate: string,
): Promise<SalaryStructure> {
  return (
    await api.get<SalaryStructure>(`/api/employees/${employeeId}/structure`, {
      params: { on_date: onDate },
    })
  ).data
}

export async function fetchSalaryStructureHistory(
  employeeId: number,
): Promise<SalaryStructureHistoryItem[]> {
  return (
    await api.get<SalaryStructureHistoryItem[]>(`/api/employees/${employeeId}/structure/history`)
  ).data
}

export async function setInitialSalaryStructure(
  employeeId: number,
  payload: InitialSalaryStructureInput,
): Promise<SalaryStructureItem[]> {
  return (
    await api.put<SalaryStructureItem[]>(`/api/employees/${employeeId}/initial-structure`, payload)
  ).data
}

export async function setSalaryStructureComponent(
  employeeId: number,
  componentId: number,
  payload: SetComponentAmountInput,
): Promise<SalaryStructureItem> {
  return (
    await api.put<SalaryStructureItem>(
      `/api/employees/${employeeId}/structure/${componentId}`,
      payload,
    )
  ).data
}
