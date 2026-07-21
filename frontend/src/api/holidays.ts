import { api } from './client'

export type EmploymentType = 'FULL_TIME' | 'PART_TIME_HOURLY' | 'LABOR'

export interface HolidayDate {
  holiday_date: string
  name: string
  eligible_employment_types: EmploymentType[]
}

export interface HolidayCalendarPeriod {
  period: string
  is_finalized: boolean
  finalized_by: number | null
  finalized_at: string | null
}

export interface HolidayWorkRecord {
  employee_id: number
  holiday_date: string
  worked: boolean
  reason: string | null
  evidence_url: string | null
}

export interface HolidayWorkInput {
  worked: boolean
  reason?: string
  evidence_url?: string
  correction_reason?: string
}

export async function fetchHolidayDates(period: string): Promise<HolidayDate[]> {
  return (
    await api.get<HolidayDate[]>('/api/holiday-calendar/dates', {
      params: { period },
    })
  ).data
}

export async function fetchHolidayCalendarPeriod(
  period: string,
): Promise<HolidayCalendarPeriod> {
  return (await api.get<HolidayCalendarPeriod>(`/api/holiday-calendar/periods/${period}`)).data
}

export async function upsertHolidayDate(input: HolidayDate): Promise<HolidayDate> {
  return (
    await api.put<HolidayDate>(`/api/holiday-calendar/dates/${input.holiday_date}`, input)
  ).data
}

export async function finalizeHolidayCalendar(
  period: string,
): Promise<HolidayCalendarPeriod> {
  return (
    await api.post<HolidayCalendarPeriod>(`/api/holiday-calendar/periods/${period}/finalize`)
  ).data
}

export async function unfinalizeHolidayCalendar(
  period: string,
): Promise<HolidayCalendarPeriod> {
  return (
    await api.post<HolidayCalendarPeriod>(`/api/holiday-calendar/periods/${period}/unfinalize`)
  ).data
}

export async function fetchHolidayWork(
  employeeId: number,
  period: string,
): Promise<HolidayWorkRecord[]> {
  return (
    await api.get<HolidayWorkRecord[]>(
      `/api/holiday-calendar/employees/${employeeId}/work`,
      { params: { period } },
    )
  ).data
}

export async function setHolidayWork(
  employeeId: number,
  holidayDate: string,
  input: HolidayWorkInput,
): Promise<HolidayWorkRecord> {
  return (
    await api.put<HolidayWorkRecord>(
      `/api/holiday-calendar/employees/${employeeId}/work/${holidayDate}`,
      input,
    )
  ).data
}
