import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import {
  createApprovalFlow,
  createSalaryAdjustment,
  decideApprovalInstance,
  fetchApprovalFlows,
  fetchApprovalInstance,
  fetchApprovalTodos,
  fetchSalaryAdjustment,
  fetchSalaryAdjustments,
  submitSalaryAdjustment,
} from './approval'

describe('approval API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.post.mockReset()
    client.get.mockResolvedValue({ data: [] })
    client.post.mockResolvedValue({ data: {} })
  })

  it('uses the salary-adjustment and approval workflow endpoints', async () => {
    const adjustment = {
      employee_id: 7,
      component_id: 3,
      amount: 5500,
      effective_from: '2026-08-01',
      reason: 'Quarterly performance adjustment',
      attachment_url: 'https://files.example.test/adjustments/raise.pdf',
    }
    const flow = {
      code: 'RAISE-DEFAULT',
      name: 'Salary raise approval',
      business_type: 'SALARY_ADJUSTMENT' as const,
      org_unit_id: 9,
      min_amount: 0,
      steps: [{ step_order: 1, name: 'Regional HR', role_code: 'REGION_MANAGER' }],
    }

    await fetchSalaryAdjustments({ status: 'PENDING' })
    await fetchSalaryAdjustment(11)
    await createSalaryAdjustment(adjustment)
    await submitSalaryAdjustment(11)
    await fetchApprovalTodos()
    await fetchApprovalInstance(4)
    await decideApprovalInstance(4, { decision: 'APPROVE', comment: 'Looks good' })
    await fetchApprovalFlows()
    await createApprovalFlow(flow)

    expect(client.get).toHaveBeenNthCalledWith(1, '/api/salary-adjustments', {
      params: { status: 'PENDING' },
    })
    expect(client.get).toHaveBeenNthCalledWith(2, '/api/salary-adjustments/11')
    expect(client.post).toHaveBeenNthCalledWith(1, '/api/salary-adjustments', adjustment)
    expect(client.post).toHaveBeenNthCalledWith(2, '/api/salary-adjustments/11/submit')
    expect(client.get).toHaveBeenNthCalledWith(3, '/api/approval-instances/todos')
    expect(client.get).toHaveBeenNthCalledWith(4, '/api/approval-instances/4')
    expect(client.post).toHaveBeenNthCalledWith(3, '/api/approval-instances/4/decisions', {
      decision: 'APPROVE',
      comment: 'Looks good',
    })
    expect(client.get).toHaveBeenNthCalledWith(5, '/api/approval-flows')
    expect(client.post).toHaveBeenNthCalledWith(4, '/api/approval-flows', flow)
  })
})
