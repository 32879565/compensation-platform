import { api } from './client'

export type DeliveryDepartment = 'DINING' | 'KITCHEN' | 'OTHER'
export type DingTalkDeliveryKind = 'PAYROLL_REVIEW' | 'APPEAL_STATUS'
export type DingTalkDeliveryStatus = 'PENDING' | 'SANDBOXED' | 'SENT' | 'FAILED'
export type CompAppealStatus = 'PENDING' | 'UPHELD' | 'CORRECTION_REQUIRED'
export type DingTalkMode = 'sandbox' | 'live'

export interface DingTalkModeResponse {
  mode: DingTalkMode
}

export interface DingTalkDelivery {
  id: number
  batch_id: number
  batch_version: number
  org_unit_id: number
  department: DeliveryDepartment
  kind: DingTalkDeliveryKind
  status: DingTalkDeliveryStatus
  can_appeal: boolean
  error_code: string | null
  attempt_count: number
  dispatched_at: string | null
}

interface DingTalkDeliveryResponse extends DingTalkDelivery {
  recipient_user_id: number | null
}

export interface DeliveryStageSummary {
  routed: number
  configuration_failures: number
  existing: number
  sandbox: boolean
}

export interface DingTalkIntegration {
  mode: DingTalkMode
  credentials_configured: boolean
  app_id_configured: boolean
  public_base_url_configured: boolean
  ready_for_live: boolean
  read_sync_enabled: boolean
  read_sync_ready: boolean
}

export type DingTalkMatchMethod = 'STABLE_ID' | 'JOB_NUMBER' | 'UNIQUE_NAME'

export interface DingTalkEmployeeMatch {
  employee_id: number
  emp_no: string
  local_name: string
  dingtalk_name: string
  dingtalk_job_number: string | null
  match_method: DingTalkMatchMethod
}

export interface DingTalkEmployeePreview {
  total_remote_users: number
  matched: number
  stable_id_matches: number
  job_number_matches: number
  unique_name_matches: number
  ambiguous: number
  unmatched: number
  truncated: boolean
  items: DingTalkEmployeeMatch[]
}

export interface DingTalkEmployeeApplyResult {
  matched: number
  linked: number
  unchanged: number
  ambiguous: number
  unmatched: number
}

export type DingTalkOrganizationSyncItemStatus = 'READY' | 'CONFLICT'
export type DingTalkReviewerDepartment = 'DINING' | 'KITCHEN'
export type DingTalkOrganizationNodeKind = 'REGION' | 'STORE'
export type DingTalkOrganizationNodeAction =
  'LINK' | 'CREATE' | 'ACTIVATE' | 'UPDATE' | 'DEACTIVATE'
export type DingTalkOrganizationChangeField = 'name' | 'parent_id' | 'dingtalk_dept_id'
export type DingTalkOrganizationReviewerAction = 'ASSIGN' | 'REMOVE' | 'CONFLICT'

export interface DingTalkOrganizationNodeItem {
  id: number
  kind: DingTalkOrganizationNodeKind
  remote_department_id: number | null
  remote_department_name: string
  remote_department_path: string
  action: DingTalkOrganizationNodeAction
  change_fields: DingTalkOrganizationChangeField[]
  match_method: string
  proposed_org_unit_id: number | null
  proposed_org_unit_name: string | null
  proposed_parent_org_unit_id: number | null
  proposed_parent_org_unit_name: string | null
  status: DingTalkOrganizationSyncItemStatus
  conflict_code: string | null
}

export interface DingTalkOrganizationReviewerItem {
  id: number
  remote_department_id: number | null
  remote_department_name: string
  remote_department_path: string
  department: DingTalkReviewerDepartment
  action: DingTalkOrganizationReviewerAction
  dingtalk_name: string | null
  proposed_employee_id: number | null
  proposed_employee_name: string | null
  match_method: string
  current_reviewer_name: string | null
  status: DingTalkOrganizationSyncItemStatus
  conflict_code: string | null
}

export interface DingTalkOrganizationPreview {
  batch_id: string
  expires_at: string
  remote_regions: number
  local_regions: number
  ready_regions: number
  region_conflicts: number
  remote_stores: number
  local_stores: number
  ready_stores: number
  store_conflicts: number
  ready_reviewers: number
  reviewer_conflicts: number
  region_items: DingTalkOrganizationNodeItem[]
  store_items: DingTalkOrganizationNodeItem[]
  reviewer_items: DingTalkOrganizationReviewerItem[]
}

type LegacyDingTalkOrganizationNodeAction =
  | DingTalkOrganizationNodeAction
  | 'MISSING_IN_DINGTALK'

interface DingTalkOrganizationNodeItemResponse
  extends Omit<DingTalkOrganizationNodeItem, 'action'> {
  action: LegacyDingTalkOrganizationNodeAction
}

type DingTalkOrganizationPreviewResponse = Omit<
  DingTalkOrganizationPreview,
  | 'remote_regions'
  | 'local_regions'
  | 'ready_regions'
  | 'region_conflicts'
  | 'region_items'
  | 'store_items'
> & {
  remote_regions?: number
  local_regions?: number
  ready_regions?: number
  region_conflicts?: number
  region_items?: DingTalkOrganizationNodeItemResponse[]
  store_items: DingTalkOrganizationNodeItemResponse[]
}

export interface DingTalkOrganizationApplyResult {
  applied_stores: number
  applied_reviewers: number
  unresolved: number
  already_applied: boolean
}

export interface DingTalkAttendancePreviewRow {
  employee_id: number
  emp_no: string
  name: string
  record_count: number
  normal_count: number
  late_count: number
  early_count: number
  absent_count: number
  not_signed_count: number
  other_count: number
}

export interface DingTalkAttendancePreview {
  period: string
  matched_employees: number
  employees_with_records: number
  total_records: number
  ambiguous_directory_users: number
  unmatched_directory_users: number
  items: DingTalkAttendancePreviewRow[]
}

export type DingTalkAttendanceSyncStatus =
  'NOT_STARTED' | 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED'

export interface DingTalkAttendanceSnapshot extends DingTalkAttendancePreview {
  status: DingTalkAttendanceSyncStatus
  source_start: string | null
  source_end: string | null
  started_at: string | null
  refreshed_at: string | null
  error_code: string | null
}

export interface DingTalkConnectionTest {
  connected: boolean
  token_expires_in_seconds: number
}

export interface CompAppeal {
  id: number
  delivery_id: number
  batch_id: number
  batch_version: number
  org_unit_id: number
  department: DeliveryDepartment
  status: CompAppealStatus
  approval_instance_id: number | null
  created_at: string
}

interface CompAppealResponse extends CompAppeal {
  employee_id: number | null
  requester_id: number
  reason: string
  resolution: string | null
}

export interface CompAppealCreateInput {
  delivery_id: number
  employee_id?: number
  reason: string
}

function toDeliverySummary(delivery: DingTalkDeliveryResponse): DingTalkDelivery {
  return {
    id: delivery.id,
    batch_id: delivery.batch_id,
    batch_version: delivery.batch_version,
    org_unit_id: delivery.org_unit_id,
    department: delivery.department,
    kind: delivery.kind,
    status: delivery.status,
    can_appeal: delivery.can_appeal,
    error_code: delivery.error_code,
    attempt_count: delivery.attempt_count,
    dispatched_at: delivery.dispatched_at,
  }
}

function toAppealSummary(appeal: CompAppealResponse): CompAppeal {
  return {
    id: appeal.id,
    delivery_id: appeal.delivery_id,
    batch_id: appeal.batch_id,
    batch_version: appeal.batch_version,
    org_unit_id: appeal.org_unit_id,
    department: appeal.department,
    status: appeal.status,
    approval_instance_id: appeal.approval_instance_id,
    created_at: appeal.created_at,
  }
}

function toOrganizationNodeItem(
  item: DingTalkOrganizationNodeItemResponse,
): DingTalkOrganizationNodeItem {
  return {
    id: item.id,
    kind: item.kind,
    remote_department_id: item.remote_department_id,
    remote_department_name: item.remote_department_name,
    remote_department_path: item.remote_department_path,
    action: item.action === 'MISSING_IN_DINGTALK' ? 'DEACTIVATE' : item.action,
    change_fields: [...item.change_fields],
    match_method: item.match_method,
    proposed_org_unit_id: item.proposed_org_unit_id,
    proposed_org_unit_name: item.proposed_org_unit_name,
    proposed_parent_org_unit_id: item.proposed_parent_org_unit_id,
    proposed_parent_org_unit_name: item.proposed_parent_org_unit_name,
    status: item.status,
    conflict_code: item.conflict_code,
  }
}

function toOrganizationReviewerItem(
  item: DingTalkOrganizationReviewerItem,
): DingTalkOrganizationReviewerItem {
  return {
    id: item.id,
    remote_department_id: item.remote_department_id,
    remote_department_name: item.remote_department_name,
    remote_department_path: item.remote_department_path,
    department: item.department,
    action: item.action,
    dingtalk_name: item.dingtalk_name,
    proposed_employee_id: item.proposed_employee_id,
    proposed_employee_name: item.proposed_employee_name,
    match_method: item.match_method,
    current_reviewer_name: item.current_reviewer_name,
    status: item.status,
    conflict_code: item.conflict_code,
  }
}

export async function fetchDingTalkDeliveries(batchId?: number): Promise<DingTalkDelivery[]> {
  const response =
    batchId === undefined
      ? await api.get<DingTalkDeliveryResponse[]>('/api/dingtalk/deliveries')
      : await api.get<DingTalkDeliveryResponse[]>('/api/dingtalk/deliveries', {
          params: { batch_id: batchId },
        })
  return response.data.map(toDeliverySummary)
}

export async function fetchDingTalkIntegration(): Promise<DingTalkIntegration> {
  return (await api.get<DingTalkIntegration>('/api/dingtalk/integration')).data
}

export async function fetchDingTalkMode(): Promise<DingTalkModeResponse> {
  return (await api.get<DingTalkModeResponse>('/api/dingtalk/mode')).data
}

export async function testDingTalkIntegration(): Promise<DingTalkConnectionTest> {
  return (await api.post<DingTalkConnectionTest>('/api/dingtalk/integration/test')).data
}

export async function previewDingTalkEmployees(): Promise<DingTalkEmployeePreview> {
  return (await api.post<DingTalkEmployeePreview>('/api/dingtalk/sync/employees/preview')).data
}

export async function applyDingTalkEmployeeMatches(): Promise<DingTalkEmployeeApplyResult> {
  return (await api.post<DingTalkEmployeeApplyResult>('/api/dingtalk/sync/employees/apply')).data
}

export async function previewDingTalkOrganization(): Promise<DingTalkOrganizationPreview> {
  const preview = (
    await api.post<DingTalkOrganizationPreviewResponse>('/api/dingtalk/sync/organization/preview')
  ).data
  return {
    batch_id: preview.batch_id,
    expires_at: preview.expires_at,
    remote_regions: preview.remote_regions ?? 0,
    local_regions: preview.local_regions ?? 0,
    ready_regions: preview.ready_regions ?? 0,
    region_conflicts: preview.region_conflicts ?? 0,
    remote_stores: preview.remote_stores,
    local_stores: preview.local_stores,
    ready_stores: preview.ready_stores,
    store_conflicts: preview.store_conflicts,
    ready_reviewers: preview.ready_reviewers,
    reviewer_conflicts: preview.reviewer_conflicts,
    region_items: (preview.region_items ?? []).map(toOrganizationNodeItem),
    store_items: preview.store_items.map(toOrganizationNodeItem),
    reviewer_items: preview.reviewer_items.map(toOrganizationReviewerItem),
  }
}

export async function applyDingTalkOrganization(
  batchId: string,
): Promise<DingTalkOrganizationApplyResult> {
  return (
    await api.post<DingTalkOrganizationApplyResult>(
      `/api/dingtalk/sync/organization/${batchId}/apply`,
    )
  ).data
}

export async function previewDingTalkAttendance(
  period: string,
): Promise<DingTalkAttendancePreview> {
  return (
    await api.post<DingTalkAttendancePreview>('/api/dingtalk/sync/attendance/preview', { period })
  ).data
}

export async function fetchDingTalkAttendanceSnapshot(
  period: string,
): Promise<DingTalkAttendanceSnapshot> {
  return (
    await api.get<DingTalkAttendanceSnapshot>('/api/dingtalk/sync/attendance/snapshot', {
      params: { period },
    })
  ).data
}

export async function refreshDingTalkAttendance(
  period: string,
): Promise<DingTalkAttendanceSnapshot> {
  return (
    await api.post<DingTalkAttendanceSnapshot>('/api/dingtalk/sync/attendance/refresh', { period })
  ).data
}

export async function stageReviewDeliveries(batchId: number): Promise<DeliveryStageSummary> {
  return (
    await api.post<DeliveryStageSummary>('/api/dingtalk/batches/' + batchId + '/review-deliveries')
  ).data
}

export async function retryDingTalkDelivery(deliveryId: number): Promise<DingTalkDelivery> {
  const response = await api.post<DingTalkDeliveryResponse>(
    '/api/dingtalk/deliveries/' + deliveryId + '/retry',
  )
  return toDeliverySummary(response.data)
}

export async function fetchCompAppeals(): Promise<CompAppeal[]> {
  return (await api.get<CompAppealResponse[]>('/api/comp-appeals')).data.map(toAppealSummary)
}

export async function fetchCompAppeal(appealId: number): Promise<CompAppeal> {
  return toAppealSummary((await api.get<CompAppealResponse>('/api/comp-appeals/' + appealId)).data)
}

export async function createCompAppeal(payload: CompAppealCreateInput): Promise<CompAppeal> {
  return toAppealSummary((await api.post<CompAppealResponse>('/api/comp-appeals', payload)).data)
}
