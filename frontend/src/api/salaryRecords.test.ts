import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import { fetchSalaryRecords } from './salaryRecords'

describe('salary records API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.get.mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 20 } })
  })

  it('passes the historical salary filters and pagination to the backend', async () => {
    await fetchSalaryRecords({
      name: '张三',
      period: '2026-06',
      store: '北京路店',
      page: 2,
      page_size: 50,
    })

    expect(client.get).toHaveBeenCalledWith('/api/salary-records', {
      params: {
        name: '张三',
        period: '2026-06',
        store: '北京路店',
        page: 2,
        page_size: 50,
      },
    })
  })

  it('omits blank filters instead of sending empty query values', async () => {
    await fetchSalaryRecords({ name: '', period: undefined, store: '', page: 1, page_size: 20 })

    expect(client.get).toHaveBeenCalledWith('/api/salary-records', {
      params: { page: 1, page_size: 20 },
    })
  })
})
