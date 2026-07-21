import { api } from './client'

export type Department = 'DINING' | 'KITCHEN' | 'OTHER'
export type BatchStatus =
  | 'DRAFT'
  | 'CALCULATING'
  | 'PENDING_STORE_CONFIRM'
  | 'HAS_DISPUTE'
  | 'PENDING_HR'
  | 'CONFIRMED'
  | 'LOCKED'
export type CalculationStatus = 'PENDING' | 'CALCULATING' | 'CALCULATED'
export type StoreConfirmationStatus = 'NOT_STARTED' | 'PENDING' | 'HAS_DISPUTE' | 'CONFIRMED'
export type HrReviewStatus = 'NOT_STARTED' | 'PENDING' | 'APPROVED'
export type LockStatus = 'UNLOCKED' | 'LOCKED'
export type DisputeStatus = 'OPEN' | 'APPROVED' | 'REJECTED' | 'NEED_MORE'
export type ResolveDecision = Exclude<DisputeStatus, 'OPEN'>

export interface PayrollBatch {
  id: number
  period: string
  attendance_start: string
  attendance_end: string
  status: BatchStatus
  calculation_status: CalculationStatus
  store_confirmation_status: StoreConfirmationStatus
  hr_review_status: HrReviewStatus
  lock_status: LockStatus
  calculated_at: string | null
  hr_reviewed_by: number | null
  hr_reviewed_at: string | null
  locked_by: number | null
  locked_at: string | null
  version: number
}

export interface PayrollLine {
  code: string
  category: string
  formula: string
  amount: string
}

export interface PayrollResult {
  employee_id: number
  emp_no: string
  employee_name: string
  org_unit_id: number | null
  version: number
  batch_version: number
  department: Department
  actual_attendance_days: string
  statutory_holiday_days: string
  statutory_holiday_worked_days: string
  statutory_holiday_pay: string
  gross: string
  deposit: string
  net: string
  carry_forward: string
  deferred_deductions: string
  deferred_deposit: string
  has_error: boolean
  lines: PayrollLine[]
  exceptions: string[]
  warnings: string[]
  rule_version: string
}

export interface PayrollAdjustment {
  id: number
  batch_id: number
  batch_version: number
  is_current_version: boolean
  employee_id: number
  dispute_id: number | null
  item: string
  before_value: Record<string, unknown>
  after_value: Record<string, unknown>
  reason: string
  applicant_id: number | null
  approver_id: number
  attachment_url: string | null
  recompute_result: Record<string, unknown> | null
  created_at: string
}

export interface BatchConfirmation {
  org_unit_id: number
  department: Department
  status: 'PENDING' | 'CONFIRMED' | 'DISPUTED'
  confirmed_by: number | null
  confirmed_at: string | null
}

export interface PayrollDispute {
  id: number
  employee_id: number
  org_unit_id: number | null
  department: Department
  salary_item: string
  opinion: string
  raised_by: number
  status: DisputeStatus
  resolution: string | null
  resolved_by: number | null
  resolved_at: string | null
  created_at: string
  allowed_attendance_fields: AttendanceField[]
  correction_options?: DisputeCorrectionOption[]
  events: PayrollDisputeEvent[]
}

export interface PayrollDisputeEvent {
  id: number
  event_type: 'RAISED' | 'NEED_MORE' | 'SUPPLEMENTED' | 'APPROVED' | 'REJECTED'
  note: string
  actor_id: number
  attachment_url: string | null
  created_at: string
}

export interface BatchCreateInput {
  period: string
  attendance_start: string
  attendance_end: string
}

export interface AttendanceChanges {
  expected_days?: number
  actual_days?: number
  worked_hours?: number
  rest_days?: number
  overtime_hours?: number
}

export type AttendanceField = keyof AttendanceChanges

export interface AttendanceCorrectionOption {
  kind: 'ATTENDANCE'
  label: string
  fields: AttendanceField[]
}

export interface HolidayWorkCorrectionOption {
  kind: 'HOLIDAY_WORK'
  label: string
  holiday_dates: { holiday_date: string; worked: boolean }[]
}

export interface PerformanceCorrectionOption {
  kind: 'PERFORMANCE'
  label: string
  coefficient: string
  score: string | null
  remark: string | null
}

export interface MonthlyAdjustmentCorrectionOption {
  kind: 'MONTHLY_ADJUSTMENT'
  label: string
  adjustment_type: 'PREV_MAKEUP' | 'PREV_DEDUCT'
  amount: string
  taxable: boolean
  in_social_base: boolean
  in_housing_base: boolean
}

export interface SalaryStructureCorrectionOption {
  kind: 'SALARY_STRUCTURE'
  label: string
  components: {
    component_id: number
    code: string
    name: string
    amount: string
  }[]
}

export interface WorkflowCorrectionOption {
  kind: 'WORKFLOW'
  label: string
  workflow: string
  reason: string
}

export type DisputeCorrectionOption =
  | AttendanceCorrectionOption
  | HolidayWorkCorrectionOption
  | PerformanceCorrectionOption
  | MonthlyAdjustmentCorrectionOption
  | SalaryStructureCorrectionOption
  | WorkflowCorrectionOption

export type SourceCorrection =
  | { kind: 'HOLIDAY_WORK'; holiday_date: string; worked: boolean }
  | {
      kind: 'PERFORMANCE'
      coefficient: number
      score: number | null
      remark: string | null
    }
  | {
      kind: 'MONTHLY_ADJUSTMENT'
      amount: number
      taxable: boolean
      in_social_base: boolean
      in_housing_base: boolean
    }
  | { kind: 'SALARY_STRUCTURE'; component_id: number; amount: number }

export type ResolveDisputeInput =
  | {
      decision: 'APPROVED'
      resolution: string
      attendance_changes: AttendanceChanges
      attachment_url: string
    }
  | {
      decision: 'APPROVED'
      resolution: string
      source_correction: SourceCorrection
      attachment_url: string
    }
  | {
      decision: Exclude<ResolveDecision, 'APPROVED'>
      resolution: string
      attachment_url?: string
    }

export async function fetchBatches(): Promise<PayrollBatch[]> {
  return (await api.get<PayrollBatch[]>('/api/batches')).data
}

export async function createBatch(payload: BatchCreateInput): Promise<PayrollBatch> {
  return (await api.post<PayrollBatch>('/api/batches', payload)).data
}

export async function runBatch(
  batchId: number,
): Promise<{ employees: number; status: BatchStatus }> {
  return (await api.post<{ employees: number; status: BatchStatus }>(`/api/batches/${batchId}/run`))
    .data
}

export async function fetchResults(batchId: number): Promise<PayrollResult[]> {
  return (await api.get<PayrollResult[]>(`/api/batches/${batchId}/results`)).data
}

export async function fetchConfirmations(batchId: number): Promise<BatchConfirmation[]> {
  return (await api.get<BatchConfirmation[]>(`/api/batches/${batchId}/confirmations`)).data
}

export async function confirmScope(
  batchId: number,
  payload: Pick<BatchConfirmation, 'org_unit_id' | 'department'>,
): Promise<{ status: string; batch_status: BatchStatus }> {
  return (
    await api.post<{ status: string; batch_status: BatchStatus }>(
      `/api/batches/${batchId}/confirm`,
      payload,
    )
  ).data
}

export async function fetchDisputes(batchId: number): Promise<PayrollDispute[]> {
  return (await api.get<PayrollDispute[]>(`/api/batches/${batchId}/disputes`)).data
}

export async function fetchAdjustments(batchId: number): Promise<PayrollAdjustment[]> {
  return (await api.get<PayrollAdjustment[]>(`/api/batches/${batchId}/adjustments`)).data
}

export async function createDispute(
  batchId: number,
  payload: { employee_id: number; salary_item: string; opinion: string },
): Promise<{ dispute_id: number; batch_status: BatchStatus }> {
  return (
    await api.post<{ dispute_id: number; batch_status: BatchStatus }>(
      `/api/batches/${batchId}/disputes`,
      payload,
    )
  ).data
}

export async function resolveDispute(
  disputeId: number,
  payload: ResolveDisputeInput,
): Promise<{ status: DisputeStatus }> {
  return (
    await api.post<{ status: DisputeStatus }>(`/api/batches/disputes/${disputeId}/resolve`, payload)
  ).data
}

export async function supplementDispute(
  disputeId: number,
  payload: { note: string; attachment_url: string },
): Promise<{ status: 'OPEN' }> {
  return (
    await api.post<{ status: 'OPEN' }>(`/api/batches/disputes/${disputeId}/supplements`, payload)
  ).data
}

export async function lockBatch(batchId: number): Promise<{ status: BatchStatus }> {
  return (await api.post<{ status: BatchStatus }>(`/api/batches/${batchId}/lock`)).data
}

export async function approveBatch(batchId: number): Promise<{ status: BatchStatus }> {
  return (await api.post<{ status: BatchStatus }>(`/api/batches/${batchId}/approve`)).data
}

export async function unlockBatch(
  batchId: number,
  reason: string,
): Promise<{ status: BatchStatus; version: number }> {
  return (
    await api.post<{ status: BatchStatus; version: number }>(`/api/batches/${batchId}/unlock`, {
      reason,
    })
  ).data
}

export async function reopenBatch(
  batchId: number,
  reason: string,
): Promise<{ status: BatchStatus; version: number }> {
  return (
    await api.post<{ status: BatchStatus; version: number }>(`/api/batches/${batchId}/reopen`, {
      reason,
    })
  ).data
}
