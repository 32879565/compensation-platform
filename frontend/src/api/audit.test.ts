import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import { fetchAuditLogs } from './audit'

describe('audit API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.get.mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 50 } })
  })

  it('uses the paginated, filtered audit endpoint', async () => {
    await fetchAuditLogs({
      page: 2,
      page_size: 20,
      action: 'employee.create',
      actor_username: 'hr',
    })

    expect(client.get).toHaveBeenCalledWith('/api/audit-logs', {
      params: {
        page: 2,
        page_size: 20,
        action: 'employee.create',
        actor_username: 'hr',
      },
    })
  })
})
