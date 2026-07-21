import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import { createBudget, deleteBudget, fetchBudgets, updateBudget } from './budgets'

describe('budget API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.post.mockReset()
    client.patch.mockReset()
    client.delete.mockReset()
    client.get.mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 100 } })
    client.post.mockResolvedValue({ data: {} })
    client.patch.mockResolvedValue({ data: {} })
    client.delete.mockResolvedValue({})
  })

  it('uses scoped budget CRUD endpoints', async () => {
    const payload = {
      org_unit_id: 8,
      period: '2026-07-01',
      headcount_budget: 12,
      labor_cost_budget: 120000,
      note: 'July plan',
    }
    await fetchBudgets({ period: '2026-07-01', page: 2, page_size: 20 })
    await createBudget(payload)
    await updateBudget(4, { version: 3, headcount_budget: 15 })
    await deleteBudget(4, 4)

    expect(client.get).toHaveBeenCalledWith('/api/budgets', {
      params: { period: '2026-07-01', page: 2, page_size: 20 },
    })
    expect(client.post).toHaveBeenCalledWith('/api/budgets', payload)
    expect(client.patch).toHaveBeenCalledWith('/api/budgets/4', { version: 3, headcount_budget: 15 })
    expect(client.delete).toHaveBeenCalledWith('/api/budgets/4', { params: { version: 4 } })
  })
})
