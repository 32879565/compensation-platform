import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import {
  fetchReviewScopes,
  fetchUsers,
  replaceDingTalkRecipient,
  replaceLoginEnabled,
  replaceReviewScopes,
} from './users'

describe('user scope API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.put.mockReset()
    client.get.mockResolvedValue({ data: [] })
    client.put.mockResolvedValue({ data: [] })
  })

  it('lists users and replaces only explicit review scopes', async () => {
    const scopes = [{ org_unit_id: 8, department: 'DINING' as const }]
    await fetchUsers()
    await fetchReviewScopes(4)
    await replaceReviewScopes(4, scopes)
    await replaceDingTalkRecipient(4, 'provider-user-id')
    await replaceLoginEnabled(4, false)

    expect(client.get).toHaveBeenNthCalledWith(1, '/api/users')
    expect(client.get).toHaveBeenNthCalledWith(2, '/api/users/4/review-scopes')
    expect(client.put).toHaveBeenNthCalledWith(1, '/api/users/4/review-scopes', { scopes })
    expect(client.put).toHaveBeenNthCalledWith(2, '/api/users/4/dingtalk-recipient', {
      dingtalk_user_id: 'provider-user-id',
    })
    expect(client.put).toHaveBeenNthCalledWith(3, '/api/users/4/login-enabled', {
      login_enabled: false,
    })
  })
})
