import axios from 'axios'

export type ReviewDepartment = 'DINING' | 'KITCHEN' | 'OTHER'

export interface ManagerReviewConfig {
  enabled: boolean
  client_id: string | null
  corp_id: string | null
}

export interface ManagerSessionRequest {
  review_id: string
  auth_code: string
}

export interface ManagerSession {
  access_token: string
  token_type: 'bearer'
  expires_in: number
}

export interface ManagerSalaryLine {
  code: string
  name: string
  amount: string
}

export interface ManagerEmployeePayroll {
  employee_id: number
  emp_no: string | null
  employee_name: string
  actual_attendance_days: string
  statutory_holiday_days: string
  statutory_holiday_worked_days: string
  gross: string
  deposit: string
  net: string
  carry_forward: string
  lines: ManagerSalaryLine[]
}

export interface ManagerReview {
  review_id: string
  period: string
  store_name: string
  department: ReviewDepartment
  confirmation_status: string
  employees: ManagerEmployeePayroll[]
}

export interface ManagerDisputeRequest {
  employee_id: number
  salary_item: string
  opinion: string
}

const managerApi = axios.create({ baseURL: '/', withCredentials: false })

function bearer(token: string) {
  return { Authorization: `Bearer ${token}` }
}

export async function fetchManagerReviewConfig(): Promise<ManagerReviewConfig> {
  return (await managerApi.get<ManagerReviewConfig>('/api/manager-review/config')).data
}

export async function exchangeManagerSession(
  request: ManagerSessionRequest,
): Promise<ManagerSession> {
  return (await managerApi.post<ManagerSession>('/api/manager-review/session', request)).data
}

export async function fetchManagerReview(reviewId: string, token: string): Promise<ManagerReview> {
  return (
    await managerApi.get<ManagerReview>(
      `/api/manager-review/reviews/${encodeURIComponent(reviewId)}`,
      { headers: bearer(token) },
    )
  ).data
}

export async function createManagerDispute(
  reviewId: string,
  token: string,
  request: ManagerDisputeRequest,
): Promise<{ dispute_id: number; batch_status: string }> {
  return (
    await managerApi.post<{ dispute_id: number; batch_status: string }>(
      `/api/manager-review/reviews/${encodeURIComponent(reviewId)}/disputes`,
      request,
      { headers: bearer(token) },
    )
  ).data
}

export async function confirmManagerReview(
  reviewId: string,
  token: string,
): Promise<{ confirmation_status: string; batch_status: string }> {
  return (
    await managerApi.post<{ confirmation_status: string; batch_status: string }>(
      `/api/manager-review/reviews/${encodeURIComponent(reviewId)}/confirm`,
      null,
      { headers: bearer(token) },
    )
  ).data
}
