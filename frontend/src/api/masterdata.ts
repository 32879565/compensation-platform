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

export interface Employee {
  id: number
  emp_no: string
  name: string
  org_unit_id: number
  job_grade_id: number | null
  employment_type: 'FULL_TIME' | 'PART_TIME_HOURLY' | 'LABOR'
  status: 'ACTIVE' | 'RESIGNED' | 'SUSPENDED'
  hire_date: string | null
  probation_end: string | null
  leave_date: string | null
  social_city: string | null
  id_card: string | null
  bank_account: string | null
}

export interface EmployeePage {
  items: Employee[]
  total: number
  page: number
  page_size: number
}

export interface JobGrade {
  id: number
  code: string
  name: string
  rank: number
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

export async function createEmployee(payload: Partial<Employee>): Promise<Employee> {
  return (await api.post<Employee>('/api/employees', payload)).data
}

export async function updateEmployee(id: number, payload: Partial<Employee>): Promise<Employee> {
  return (await api.patch<Employee>(`/api/employees/${id}`, payload)).data
}

export async function deleteEmployee(id: number): Promise<void> {
  await api.delete(`/api/employees/${id}`)
}

export async function fetchGrades(): Promise<JobGrade[]> {
  return (await api.get<JobGrade[]>('/api/grades')).data
}
