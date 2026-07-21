import { api } from './client'

export interface OrgUnit {
  id: number
  code: string
  name: string
  type: 'GROUP' | 'REGION' | 'STORE'
  parent_id: number | null
  city: string | null
  status: string
}

export interface OrgTreeNode extends OrgUnit {
  children: OrgTreeNode[]
}

export type Department = 'DINING' | 'KITCHEN' | 'OTHER'

export interface Employee {
  id: number
  version: number
  emp_no: string
  name: string
  org_unit_id: number
  job_grade_id: number | null
  employment_type: 'FULL_TIME' | 'PART_TIME_HOURLY' | 'LABOR'
  department: Department
  position_title: string | null
  is_special_position: boolean
  status: 'ACTIVE' | 'RESIGNED' | 'SUSPENDED'
  hire_date: string | null
  probation_end: string | null
  leave_date: string | null
  social_city: string | null
  id_card: string | null
  bank_account: string | null
  dingtalk_linked: boolean
}

export interface EmployeePage {
  items: Employee[]
  total: number
  page: number
  page_size: number
}

export interface EmployeeCreateInput {
  emp_no: string
  name: string
  org_unit_id: number
  job_grade_id?: number | null
  employment_type?: Employee['employment_type']
  department?: Department
  position_title?: string | null
  is_special_position?: boolean
  hire_date: string
  probation_end?: string | null
  leave_date?: string | null
  social_city?: string | null
  id_card?: string | null
  bank_account?: string | null
}

export type EmployeeUpdateFields = Partial<Omit<EmployeeCreateInput, 'emp_no'>> & {
  status?: Employee['status']
}

export type UpdateEmployeeInput = EmployeeUpdateFields & {
  expected_version: number
}

export interface JobGrade {
  id: number
  code: string
  name: string
  rank: number
  version: number
  is_active: boolean
  deactivated_at: string | null
}

export type GradeCatalogStatus = 'active' | 'inactive' | 'all'

export interface GradeCatalogQuery {
  status?: GradeCatalogStatus
}

export interface CreateGradeInput {
  code: string
  name: string
  rank: number
}

export interface UpdateGradeInput {
  name?: string
  rank?: number
  expected_version: number
}

export interface GradeLifecycleInput {
  reason: string
  expected_version: number
}

export interface SalaryBand {
  id: number
  job_grade_id: number
  band_min: string
  band_mid: string
  band_max: string
  effective_from: string
  effective_to: string | null
}

export interface CreateSalaryBandInput {
  band_min: string
  band_mid: string
  band_max: string
  effective_from: string
}

export async function fetchOrgTree(): Promise<OrgTreeNode[]> {
  return (await api.get<OrgTreeNode[]>('/api/org/tree')).data
}

export async function fetchOrgUnits(): Promise<OrgUnit[]> {
  return (await api.get<OrgUnit[]>('/api/org')).data
}

export interface EmployeeQuery {
  name?: string
  emp_no?: string
  org_unit_id?: number
  page?: number
  page_size?: number
}

export async function fetchEmployees(query: EmployeeQuery): Promise<EmployeePage> {
  return (await api.get<EmployeePage>('/api/employees', { params: query })).data
}

export async function createEmployee(payload: EmployeeCreateInput): Promise<Employee> {
  return (await api.post<Employee>('/api/employees', payload)).data
}

export async function updateEmployee(id: number, payload: UpdateEmployeeInput): Promise<Employee> {
  return (await api.patch<Employee>(`/api/employees/${id}`, payload)).data
}

export async function deleteEmployee(id: number): Promise<void> {
  await api.delete(`/api/employees/${id}`)
}

export async function fetchGrades(
  query: GradeCatalogQuery = { status: 'active' },
): Promise<JobGrade[]> {
  return (await api.get<JobGrade[]>('/api/grades', { params: query })).data
}

export async function createGrade(payload: CreateGradeInput): Promise<JobGrade> {
  return (await api.post<JobGrade>('/api/grades', payload)).data
}

export async function updateGrade(gradeId: number, payload: UpdateGradeInput): Promise<JobGrade> {
  return (await api.patch<JobGrade>(`/api/grades/${gradeId}`, payload)).data
}

export async function deactivateGrade(
  gradeId: number,
  payload: GradeLifecycleInput,
): Promise<JobGrade> {
  return (await api.post<JobGrade>(`/api/grades/${gradeId}/deactivate`, payload)).data
}

export async function restoreGrade(
  gradeId: number,
  payload: GradeLifecycleInput,
): Promise<JobGrade> {
  return (await api.post<JobGrade>(`/api/grades/${gradeId}/restore`, payload)).data
}

export async function fetchGradeBands(gradeId: number): Promise<SalaryBand[]> {
  return (await api.get<SalaryBand[]>(`/api/grades/${gradeId}/bands`)).data
}

export async function createSalaryBand(
  gradeId: number,
  payload: CreateSalaryBandInput,
): Promise<SalaryBand> {
  return (await api.post<SalaryBand>(`/api/grades/${gradeId}/bands`, payload)).data
}
