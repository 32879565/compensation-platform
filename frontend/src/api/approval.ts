import { api } from './client'

export type ApprovalBusinessType = 'SALARY_ADJUSTMENT' | 'COMP_APPEAL'
export type SalaryAdjustmentStatus = 'DRAFT' | 'PENDING' | 'APPROVED' | 'REJECTED' | 'CANCELLED'
export type ApprovalInstanceStatus = 'PENDING' | 'APPROVED' | 'REJECTED' | 'CANCELLED'
export type ApprovalActionType = 'APPROVE' | 'REJECT' | 'CANCEL'
export type ApprovalDecision = Extract<ApprovalActionType, 'APPROVE' | 'REJECT'>

export interface SalaryAdjustmentCreateInput {
  employee_id: number
  component_id: number
  amount: number
  effective_from: string
  reason: string
  attachment_url: string
}

export interface SalaryAdjustment {
  id: number
  employee_id: number
  org_unit_id: number
  component_id: number
  amount: string
  effective_from: string
  reason: string
  attachment_url: string
  requester_id: number
  status: SalaryAdjustmentStatus
  before_snapshot: Record<string, unknown>
  approval_instance_id: number | null
  applied_structure_id: number | null
}

export interface SalaryAdjustmentQuery {
  status?: SalaryAdjustmentStatus
}

export interface ApprovalFlowStep {
  step_order: number
  name: string
  role_code: string
}

export interface ApprovalFlow {
  id: number
  code: string
  name: string
  business_type: ApprovalBusinessType
  org_unit_id: number | null
  min_amount: string | null
  max_amount: string | null
  is_active: boolean
  steps: ApprovalFlowStep[]
}

export interface ApprovalFlowCreateInput {
  code: string
  name: string
  business_type: ApprovalBusinessType
  org_unit_id?: number | null
  min_amount?: number
  max_amount?: number
  is_active?: boolean
  steps: ApprovalFlowStep[]
}

export interface ApprovalAction {
  step_order: number
  action: ApprovalActionType
  actor_id: number
  comment: string | null
}

export interface ApprovalFlowSnapshot {
  steps: ApprovalFlowStep[]
}

export interface ApprovalInstance {
  id: number
  flow_id: number
  business_type: ApprovalBusinessType
  business_id: number
  requester_id: number
  org_unit_id: number
  amount: string
  status: ApprovalInstanceStatus
  current_step_order: number | null
  flow_snapshot: ApprovalFlowSnapshot
  actions: ApprovalAction[]
}

export interface ApprovalTodo {
  id: number
  business_type: ApprovalBusinessType
  business_id: number
  org_unit_id: number
  amount: string
  requester_id: number
  current_step_order: number
  current_step_name: string
}

export interface ApprovalDecisionInput {
  decision: ApprovalDecision
  comment?: string
}

export async function fetchSalaryAdjustments(
  query: SalaryAdjustmentQuery = {},
): Promise<SalaryAdjustment[]> {
  return (await api.get<SalaryAdjustment[]>('/api/salary-adjustments', { params: query })).data
}

export async function fetchSalaryAdjustment(id: number): Promise<SalaryAdjustment> {
  return (await api.get<SalaryAdjustment>(`/api/salary-adjustments/${id}`)).data
}

export async function createSalaryAdjustment(
  payload: SalaryAdjustmentCreateInput,
): Promise<SalaryAdjustment> {
  return (await api.post<SalaryAdjustment>('/api/salary-adjustments', payload)).data
}

export async function submitSalaryAdjustment(id: number): Promise<SalaryAdjustment> {
  return (await api.post<SalaryAdjustment>(`/api/salary-adjustments/${id}/submit`)).data
}

export async function fetchApprovalTodos(): Promise<ApprovalTodo[]> {
  return (await api.get<ApprovalTodo[]>('/api/approval-instances/todos')).data
}

export async function fetchApprovalInstance(id: number): Promise<ApprovalInstance> {
  return (await api.get<ApprovalInstance>(`/api/approval-instances/${id}`)).data
}

export async function decideApprovalInstance(
  id: number,
  payload: ApprovalDecisionInput,
): Promise<ApprovalInstance> {
  return (await api.post<ApprovalInstance>(`/api/approval-instances/${id}/decisions`, payload)).data
}

export async function fetchApprovalFlows(): Promise<ApprovalFlow[]> {
  return (await api.get<ApprovalFlow[]>('/api/approval-flows')).data
}

export async function createApprovalFlow(
  payload: ApprovalFlowCreateInput,
): Promise<ApprovalFlow> {
  return (await api.post<ApprovalFlow>('/api/approval-flows', payload)).data
}
