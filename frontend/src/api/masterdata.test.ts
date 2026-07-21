import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), post: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import {
  createEmployee,
  createSalaryBand,
  deactivateGrade,
  fetchGradeBands,
  fetchGrades,
  restoreGrade,
  updateEmployee,
  updateGrade,
  type EmployeeCreateInput,
  type UpdateEmployeeInput,
} from './masterdata'

describe('employee API write contracts', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    client.patch.mockResolvedValue({ data: {} })
    client.post.mockResolvedValue({ data: {} })
  })

  it('posts only the explicit create contract and preserves an explicit grade removal', async () => {
    const createPayload: EmployeeCreateInput = {
      emp_no: 'E0099',
      name: '林月',
      org_unit_id: 3,
      hire_date: '2026-07-01',
      job_grade_id: 8,
    }
    const updatePayload: UpdateEmployeeInput = {
      job_grade_id: null,
      expected_version: 4,
    }

    await createEmployee(createPayload)
    await updateEmployee(99, updatePayload)

    expect(client.post).toHaveBeenCalledWith('/api/employees', createPayload)
    expect(client.patch).toHaveBeenCalledWith('/api/employees/99', updatePayload)
  })

  it('rejects response-only employee fields at compile time', () => {
    const validCreate: EmployeeCreateInput = {
      emp_no: 'E0100',
      name: '周青',
      org_unit_id: 3,
      hire_date: '2026-07-02',
    }

    // @ts-expect-error id is assigned by the server and is never writable.
    const invalidCreate: Parameters<typeof createEmployee>[0] = { ...validCreate, id: 100 }
    // @ts-expect-error version is a response field; use expected_version for concurrency.
    const invalidVersion: Parameters<typeof updateEmployee>[1] = { expected_version: 2, version: 3 }
    const invalidDingTalk: Parameters<typeof updateEmployee>[1] = {
      expected_version: 2,
      // @ts-expect-error DingTalk link state is maintained by the directory integration.
      dingtalk_linked: true,
    }
    const invalidEmployeeNumber: Parameters<typeof updateEmployee>[1] = {
      expected_version: 2,
      // @ts-expect-error employee numbers are immutable after creation.
      emp_no: 'E0101',
    }

    expect(validCreate.emp_no).toBe('E0100')
    void [invalidCreate, invalidVersion, invalidDingTalk, invalidEmployeeNumber]
  })
})

describe('grade catalog API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    client.get.mockResolvedValue({ data: [] })
    client.patch.mockResolvedValue({ data: {} })
    client.post.mockResolvedValue({ data: {} })
  })

  it('passes the lifecycle filter and lazily reads the selected grade bands', async () => {
    await fetchGrades({ status: 'inactive' })
    await fetchGradeBands(12)

    expect(client.get).toHaveBeenNthCalledWith(1, '/api/grades', {
      params: { status: 'inactive' },
    })
    expect(client.get).toHaveBeenNthCalledWith(2, '/api/grades/12/bands')
  })

  it('uses the grade version for edits and reasoned lifecycle actions', async () => {
    await updateGrade(12, { name: '资深主管', rank: 8, expected_version: 3 })
    const lifecycle = { reason: '组织职级调整', expected_version: 3 }
    await deactivateGrade(12, lifecycle)
    await restoreGrade(12, lifecycle)

    expect(client.patch).toHaveBeenCalledWith('/api/grades/12', {
      name: '资深主管',
      rank: 8,
      expected_version: 3,
    })
    expect(client.post).toHaveBeenNthCalledWith(1, '/api/grades/12/deactivate', lifecycle)
    expect(client.post).toHaveBeenNthCalledWith(2, '/api/grades/12/restore', lifecycle)
  })

  it('posts decimal strings to the path-selected grade band endpoint', async () => {
    const payload = {
      effective_from: '2026-08-01',
      band_min: '6000.00',
      band_mid: '8000.00',
      band_max: '10000.00',
    }

    await createSalaryBand(12, payload)

    expect(client.post).toHaveBeenCalledWith('/api/grades/12/bands', payload)
  })
})
