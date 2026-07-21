import { api } from './client'

export interface AuditLogEntry {
  id: number
  ts: string
  actor_user_id: number | null
  actor_username: string | null
  action: string
  result: string
  target_type: string | null
  target_id: number | null
  detail: Record<string, unknown> | null
}

export interface AuditLogPage {
  items: AuditLogEntry[]
  total: number
  page: number
  page_size: number
}

export interface AuditLogQuery {
  page: number
  page_size: number
  action?: string
  actor_username?: string
}

export async function fetchAuditLogs(query: AuditLogQuery): Promise<AuditLogPage> {
  return (await api.get<AuditLogPage>('/api/audit-logs', { params: query })).data
}
