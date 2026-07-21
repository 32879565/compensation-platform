import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ patch: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import { normalizeComponentCreateInput, updateComponent } from './comp'

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
    await updateComponent(7, { prorate_by_attendance: true })

    expect(client.patch).toHaveBeenCalledWith('/api/salary-components/7', {
      prorate_by_attendance: true,
    })
  })
})
