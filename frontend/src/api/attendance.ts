import { api } from './client'

export interface PerformanceRecord {
  employee_id: number
  period: string
  coefficient: string
  score: string | null
  remark: string | null
}

export interface PerformanceImportResult {
  matched: number
  skipped: string[]
}

export type AttendanceEmploymentType = 'FULL_TIME' | 'PART_TIME_HOURLY' | 'LABOR'
export type AttendanceDepartment = 'DINING' | 'KITCHEN' | 'OTHER'

export interface AttendanceScheduleWrite {
  name: string
  org_unit_id: number | null
  employment_type: AttendanceEmploymentType | null
  department: AttendanceDepartment | null
  position_title: string | null
  is_special_position: boolean | null
  weekly_rest_days: number[]
  monthly_expected_days: string | null
  effective_from: string
  effective_to: string | null
  priority: number
  is_active: boolean
}

export interface AttendanceScheduleRule extends AttendanceScheduleWrite {
  id: number
}

export interface ExpectedAttendanceGenerationResult {
  period: string
  generated: number
  adjusted_preserved: number
}

// Keep the client-side chooser aligned with the formats the API accepts.
export const PERFORMANCE_IMPORT_ACCEPT = [
  '.xlsx',
  '.xlsm',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.ms-excel.sheet.macroEnabled.12',
].join(',')

export function isPerformanceImportFile(file: File): boolean {
  return /\.(xlsx|xlsm)$/i.test(file.name)
}

export async function fetchPerformance(period: string): Promise<PerformanceRecord[]> {
  return (await api.get<PerformanceRecord[]>('/api/performance', { params: { period } })).data
}

export async function importPerformance(
  period: string,
  file: File,
): Promise<PerformanceImportResult> {
  const formData = new FormData()
  formData.append('file', file)
  return (
    await api.post<PerformanceImportResult>('/api/performance/import', formData, {
      params: { period },
    })
  ).data
}

export async function fetchAttendanceSchedules(): Promise<AttendanceScheduleRule[]> {
  return (await api.get<AttendanceScheduleRule[]>('/api/attendance-schedules')).data
}

export async function createAttendanceSchedule(
  payload: AttendanceScheduleWrite,
): Promise<AttendanceScheduleRule> {
  return (await api.post<AttendanceScheduleRule>('/api/attendance-schedules', payload)).data
}

export async function updateAttendanceSchedule(
  id: number,
  payload: AttendanceScheduleWrite,
): Promise<AttendanceScheduleRule> {
  return (await api.put<AttendanceScheduleRule>(`/api/attendance-schedules/${id}`, payload)).data
}

export async function generateExpectedAttendance(
  period: string,
): Promise<ExpectedAttendanceGenerationResult> {
  return (
    await api.post<ExpectedAttendanceGenerationResult>(
      '/api/attendance-schedules/generate',
      undefined,
      { params: { period } },
    )
  ).data
}
