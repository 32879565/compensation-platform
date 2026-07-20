import { api } from './client'

export type ComponentType =
  | 'BASE'
  | 'PERFORMANCE'
  | 'POSITION'
  | 'ALLOWANCE'
  | 'OVERTIME'
  | 'DEDUCTION'

export interface SalaryComponent {
  id: number
  code: string
  name: string
  component_type: ComponentType
  taxable: boolean
  in_social_base: boolean
  in_housing_base: boolean
  sort_order: number
}

export async function fetchComponents(): Promise<SalaryComponent[]> {
  return (await api.get<SalaryComponent[]>('/api/salary-components')).data
}

export async function createComponent(
  payload: Partial<SalaryComponent>,
): Promise<SalaryComponent> {
  return (await api.post<SalaryComponent>('/api/salary-components', payload)).data
}
