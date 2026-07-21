import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import { applyLegacyComponent, applyLegacyGrade, fetchLegacyCatalogPreview } from './legacyCatalog'

describe('legacy catalog review API', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.post.mockReset()
    client.get.mockResolvedValue({ data: {} })
    client.post.mockResolvedValue({ data: {} })
  })

  it('loads only the privacy-safe aggregate preview', async () => {
    await fetchLegacyCatalogPreview()

    expect(client.get).toHaveBeenCalledWith('/api/legacy-catalog/preview')
  })

  it('sends the selected legacy field with explicit HR confirmation', async () => {
    const payload = {
      source_field: '综合薪资',
      expected_record_count: 68245,
      confirmed_by_hr: true as const,
      reason: '经薪酬负责人核对旧系统字段',
      component: {
        code: 'COMPREHENSIVE',
        name: '综合薪资',
        component_type: 'COMPREHENSIVE' as const,
        taxable: true,
        in_social_base: true,
        in_housing_base: true,
        prorate_by_attendance: false,
        sort_order: 10,
      },
    }

    await applyLegacyComponent(payload)

    expect(client.post).toHaveBeenCalledWith('/api/legacy-catalog/components/apply', payload)
  })

  it('keeps observed history separate from the HR-confirmed policy band', async () => {
    const payload = {
      source_position: '服务员',
      expected_record_count: 19486,
      policy_confirmation: 'HR_CONFIRMED' as const,
      reason: '人事确认职位映射及现行薪档政策',
      grade: { code: 'STORE-P1', name: '门店一职级', rank: 10 },
      band: {
        band_min: '3800.00',
        band_mid: '4300.00',
        band_max: '5000.00',
        effective_from: '2026-07-01',
      },
    }

    await applyLegacyGrade(payload)

    expect(client.post).toHaveBeenCalledWith('/api/legacy-catalog/grades/apply', payload)
  })
})
