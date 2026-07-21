import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), post: vi.fn(), put: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import {
  deactivateComponent,
  fetchComponents,
  fetchSalaryStructureHistory,
  normalizeComponentCreateInput,
  restoreComponent,
  setInitialSalaryStructure,
  updateComponent,
} from './comp'

describe('salary component create input', () => {
  const sharedInput = {
    code: 'MEAL',
    name: '餐补',
    taxable: true,
    in_social_base: false,
    in_housing_base: false,
  }

  it('keeps the required allowance kind for allowance components', () => {
    expect(
      normalizeComponentCreateInput({
        ...sharedInput,
        component_type: 'ALLOWANCE',
        allowance_kind: 'FIXED',
        prorate_by_attendance: true,
      }),
    ).toEqual({
      ...sharedInput,
      component_type: 'ALLOWANCE',
      allowance_kind: 'FIXED',
      prorate_by_attendance: true,
    })
  })

  it('requires an allowance kind only for allowance components', () => {
    expect(() =>
      normalizeComponentCreateInput({ ...sharedInput, component_type: 'ALLOWANCE' }),
    ).toThrow('Allowance kind is required for allowance components')
  })

  it('omits an obsolete allowance kind when the component is not an allowance', () => {
    expect(
      normalizeComponentCreateInput({
        ...sharedInput,
        component_type: 'HOUSING',
        allowance_kind: 'FIXED',
        prorate_by_attendance: true,
      }),
    ).toEqual({ ...sharedInput, component_type: 'HOUSING', prorate_by_attendance: false })
  })
})

describe('salary component update API', () => {
  beforeEach(() => {
    client.patch.mockReset()
    client.patch.mockResolvedValue({ data: {} })
  })

  it('patches the attendance-proration configuration', async () => {
    await updateComponent(7, {
      prorate_by_attendance: true,
      expected_updated_at: '2026-07-21T05:00:00Z',
    })

    expect(client.patch).toHaveBeenCalledWith('/api/salary-components/7', {
      prorate_by_attendance: true,
      expected_updated_at: '2026-07-21T05:00:00Z',
    })
  })
})

describe('salary component catalog API', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.post.mockReset()
    client.get.mockResolvedValue({ data: [] })
    client.post.mockResolvedValue({ data: {} })
  })

  it('passes the lifecycle status filter to the catalog endpoint', async () => {
    await fetchComponents({ status: 'inactive' })

    expect(client.get).toHaveBeenCalledWith('/api/salary-components', {
      params: { status: 'inactive' },
    })
  })

  it('sends the reason and concurrency timestamp for deactivate and restore', async () => {
    const payload = {
      reason: '薪酬政策变更',
      expected_updated_at: '2026-07-21T05:00:00Z',
    }

    await deactivateComponent(7, payload)
    await restoreComponent(7, payload)

    expect(client.post).toHaveBeenNthCalledWith(1, '/api/salary-components/7/deactivate', payload)
    expect(client.post).toHaveBeenNthCalledWith(2, '/api/salary-components/7/restore', payload)
  })
})

describe('employee salary structure API', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.put.mockReset()
    client.get.mockResolvedValue({ data: [] })
    client.put.mockResolvedValue({ data: [] })
  })

  it('loads the complete effective-dated structure history', async () => {
    await fetchSalaryStructureHistory(17)

    expect(client.get).toHaveBeenCalledWith('/api/employees/17/structure/history')
  })

  it('creates the complete initial structure atomically', async () => {
    const payload = {
      effective_from: '2026-07-01',
      items: [
        { component_id: 1, amount: 5000 },
        {
          component_id: 2,
          amount: 300,
          reason: '经薪酬负责人确认的餐补政策',
          attachment_url: 'https://files.example.test/policies/meal.pdf',
        },
      ],
    }

    await setInitialSalaryStructure(17, payload)

    expect(client.put).toHaveBeenCalledWith('/api/employees/17/initial-structure', payload)
  })
})
