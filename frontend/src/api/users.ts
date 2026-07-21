import { api } from './client'

export type ReviewDepartment = 'DINING' | 'KITCHEN' | 'OTHER'

export interface ReviewScope {
  org_unit_id: number
  department: ReviewDepartment
}

export interface ManagedUser {
  id: number
  username: string
  status: string
  employee_id: number | null
  dingtalk_recipient_configured: boolean
  roles: string[]
  review_scopes: ReviewScope[]
}

export async function fetchUsers(): Promise<ManagedUser[]> {
  return (await api.get<ManagedUser[]>('/api/users')).data
}

export async function fetchReviewScopes(userId: number): Promise<ReviewScope[]> {
  return (await api.get<ReviewScope[]>(`/api/users/${userId}/review-scopes`)).data
}

export async function replaceReviewScopes(
  userId: number,
  scopes: ReviewScope[],
): Promise<ReviewScope[]> {
  return (await api.put<ReviewScope[]>(`/api/users/${userId}/review-scopes`, { scopes })).data
}

export async function replaceDingTalkRecipient(
  userId: number,
  dingtalkUserId: string | null,
): Promise<{ configured: boolean }> {
  return (
    await api.put<{ configured: boolean }>(`/api/users/${userId}/dingtalk-recipient`, {
      dingtalk_user_id: dingtalkUserId,
    })
  ).data
}
