import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const legacyApi = vi.hoisted(() => ({ fetchLegacyCatalogPreview: vi.fn() }))

vi.mock('../api/legacyCatalog', () => legacyApi)

import LegacyCatalogEvidencePanel from './LegacyCatalogEvidencePanel'

const preview = {
  source: {
    record_count: 68245,
    period_from: '2024-01',
    period_to: '2026-06',
    snapshot_id: 'real-source-snapshot',
  },
  component_candidates: [
    {
      source_field: '综合薪资',
      record_count: 68211,
      nonzero_count: 68211,
      period_from: '2024-01',
      period_to: '2026-06',
      suggested_component_type: 'COMPREHENSIVE',
      suggested_allowance_kind: null,
      classification: 'NEEDS_HR_CONFIRMATION' as const,
      importable: true,
      applied: false,
      applied_target_id: null,
      note: '历史金额是真实证据，政策仍需确认。',
    },
    {
      source_field: '出勤工资',
      record_count: 68245,
      nonzero_count: 68245,
      period_from: '2024-01',
      period_to: '2026-06',
      suggested_component_type: null,
      suggested_allowance_kind: null,
      classification: 'DERIVED_NOT_CATALOG_COMPONENT' as const,
      importable: false,
      applied: false,
      applied_target_id: null,
      note: '核算结果不能作为薪资结构组件。',
    },
  ],
  grade_source_status: 'OFFICIAL_MASTER_NOT_PRESENT' as const,
  grade_candidates: [
    {
      position: '服务员',
      record_count: 9200,
      contributor_count: 625,
      salary_sample_count: 625,
      period_from: '2024-01',
      period_to: '2026-06',
      observed_p25: '3800.00',
      observed_median: '4200.00',
      observed_p75: '4800.00',
      suppressed_for_privacy: false,
      applied: false,
      applied_target_id: null,
      is_official_grade: false as const,
    },
  ],
  warnings: ['旧系统没有正式薪资组件编码。'],
}

function renderPanel(mode: 'components' | 'grades', onReview = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    onReview,
    ...render(
      <QueryClientProvider client={queryClient}>
        <LegacyCatalogEvidencePanel mode={mode} onReview={onReview} />
      </QueryClientProvider>,
    ),
  }
}

describe('LegacyCatalogEvidencePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    legacyApi.fetchLegacyCatalogPreview.mockResolvedValue(preview)
  })

  afterEach(cleanup)

  it('shows real legacy component evidence directly on the page', async () => {
    const { onReview } = renderPanel('components')

    expect(await screen.findByText('68,245 条真实工资记录')).toBeTruthy()
    expect(screen.getAllByText('2024-01 至 2026-06').length).toBeGreaterThan(0)
    expect(screen.getByText('2 项历史字段')).toBeTruthy()
    expect(screen.getAllByText('综合薪资').length).toBeGreaterThan(0)
    expect(screen.getByText('出勤工资')).toBeTruthy()
    expect(screen.getByText('待人事确认')).toBeTruthy()
    expect(screen.getByText('核算结果，不导入')).toBeTruthy()

    const region = screen.getByRole('region', { name: '旧系统真实薪资组件数据' })
    region.scrollLeft = 0
    fireEvent.keyDown(region, { key: 'ArrowRight', code: 'ArrowRight' })
    expect(region.scrollLeft).toBeGreaterThan(0)

    fireEvent.click(screen.getByRole('button', { name: '审核并创建正式组件' }))
    expect(onReview).toHaveBeenCalledTimes(1)
  })

  it('shows real legacy positions and observed salary bands directly on the page', async () => {
    const { onReview } = renderPanel('grades')

    expect(await screen.findByText('1 个历史职位')).toBeTruthy()
    expect(screen.getByText('服务员')).toBeTruthy()
    expect(screen.getByText('625 人')).toBeTruthy()
    expect(screen.getByText('3,800.00 / 4,200.00 / 4,800.00')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '审核并创建正式职级' }))
    expect(onReview).toHaveBeenCalledTimes(1)
  })

  it('keeps the failure visible and allows a retry', async () => {
    legacyApi.fetchLegacyCatalogPreview
      .mockRejectedValueOnce(new Error('legacy evidence unavailable'))
      .mockResolvedValueOnce(preview)
    renderPanel('components')

    expect(await screen.findByText('旧系统真实数据加载失败')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '重新加载' }))

    await waitFor(() => expect(legacyApi.fetchLegacyCatalogPreview).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('68,245 条真实工资记录')).toBeTruthy()
  })
})
