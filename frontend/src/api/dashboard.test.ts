import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import { fetchDashboard } from './dashboard'

describe('dashboard API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.get.mockResolvedValue({ data: {} })
  })

  it('passes the selected payroll month to the aggregate endpoint', async () => {
    await fetchDashboard('2026-07')

    expect(client.get).toHaveBeenCalledWith('/api/dashboard', { params: { period: '2026-07' } })
  })
})
