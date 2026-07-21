import { api } from './client'

export interface LaborBudget {
  id: number
  org_unit_id: number
  period: string
  headcount_budget: number
  labor_cost_budget: string
  note: string | null
  version: number
}

export interface LaborBudgetPage {
  items: LaborBudget[]
  total: number
  page: number
  page_size: number
}

export interface BudgetQuery {
  org_unit_id?: number
  period?: string
  page?: number
  page_size?: number
}

export interface BudgetWrite {
  org_unit_id: number
  period: string
  headcount_budget: number
  labor_cost_budget: number
  note?: string
}

export type BudgetUpdate = Partial<BudgetWrite> & { version: number }

export async function fetchBudgets(query: BudgetQuery): Promise<LaborBudgetPage> {
  return (await api.get<LaborBudgetPage>('/api/budgets', { params: query })).data
}

export async function createBudget(payload: BudgetWrite): Promise<LaborBudget> {
  return (await api.post<LaborBudget>('/api/budgets', payload)).data
}

export async function updateBudget(
  id: number,
  payload: BudgetUpdate,
): Promise<LaborBudget> {
  return (await api.patch<LaborBudget>(`/api/budgets/${id}`, payload)).data
}

export async function deleteBudget(id: number, version: number): Promise<void> {
  await api.delete(`/api/budgets/${id}`, { params: { version } })
}
