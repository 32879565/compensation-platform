import { api } from './client'

export interface DashboardMetrics {
  employee_count: number
  actual_gross: string
  actual_net: string
  average_gross: string
  budget_headcount: number | null
  budget_cost: string | null
  headcount_variance: number | null
  cost_variance: string | null
}

export interface DashboardTrend {
  period: string
  employee_count: number
  actual_gross: string
  budget_cost: string | null
}

export interface StoreRank {
  org_unit_id: number
  org_code: string
  org_name: string
  employee_count: number
  actual_gross: string
  average_gross: string
  budget_cost: string | null
  cost_variance: string | null
}

export interface DashboardData {
  period: string
  metrics: DashboardMetrics
  trend: DashboardTrend[]
  store_ranking: StoreRank[]
}

export async function fetchDashboard(period: string): Promise<DashboardData> {
  return (await api.get<DashboardData>('/api/dashboard', { params: { period } })).data
}
