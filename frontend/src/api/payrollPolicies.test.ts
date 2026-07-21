import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import {
  createPayrollPolicy,
  createTaxOpening,
  fetchPayrollPolicies,
  fetchTaxOpenings,
  finalizePayrollPolicy,
  finalizeTaxOpening,
  supersedeTaxOpening,
  updatePayrollPolicy,
  updateTaxOpening,
} from './payrollPolicies'

describe('payroll policy API client', () => {
  const policy = {
    city: '广州',
    effective_from: '2026-07-01',
    social_rules: [],
    monthly_basic_deduction: '5000',
    tax_brackets: [{ upper_bound: null, rate: '0.03', quick_deduction: '0' }],
    derived_income_rules: [],
  }
  const opening = {
    tax_year: 2026,
    through_period: '2026-06',
    employment_months_to_date: 6,
    taxable_income: '30000',
    employee_contribution: '2400',
    special_deduction: '0',
    tax_withheld: '120',
    evidence_ref: 'archive://tax/2026-06',
  }

  beforeEach(() => {
    client.get.mockReset()
    client.post.mockReset()
    client.patch.mockReset()
    client.get.mockResolvedValue({ data: [] })
    client.post.mockResolvedValue({ data: {} })
    client.patch.mockResolvedValue({ data: {} })
  })

  it('uses the policy draft, update, and finalization endpoints', async () => {
    await fetchPayrollPolicies({ city: '广州', includeDrafts: true })
    await createPayrollPolicy(policy)
    await updatePayrollPolicy(11, { monthly_basic_deduction: '6000' })
    await finalizePayrollPolicy(11)

    expect(client.get).toHaveBeenCalledWith('/api/payroll-policies', {
      params: { city: '广州', include_drafts: true },
    })
    expect(client.post).toHaveBeenNthCalledWith(1, '/api/payroll-policies', policy)
    expect(client.patch).toHaveBeenCalledWith('/api/payroll-policies/11', {
      monthly_basic_deduction: '6000',
    })
    expect(client.post).toHaveBeenNthCalledWith(2, '/api/payroll-policies/11/finalize')
  })

  it('keeps tax-opening writes scoped to the selected employee', async () => {
    await fetchTaxOpenings(42)
    await createTaxOpening(42, opening)
    await updateTaxOpening(42, 3, opening)
    await finalizeTaxOpening(42, 3)
    await supersedeTaxOpening(42, 3, opening)

    expect(client.get).toHaveBeenCalledWith('/api/employees/42/tax-ytd-openings')
    expect(client.post).toHaveBeenNthCalledWith(1, '/api/employees/42/tax-ytd-openings', opening)
    expect(client.patch).toHaveBeenCalledWith('/api/employees/42/tax-ytd-openings/3', opening)
    expect(client.post).toHaveBeenNthCalledWith(2, '/api/employees/42/tax-ytd-openings/3/finalize')
    expect(client.post).toHaveBeenNthCalledWith(
      3,
      '/api/employees/42/tax-ytd-openings/3/supersede',
      opening,
    )
  })
})
