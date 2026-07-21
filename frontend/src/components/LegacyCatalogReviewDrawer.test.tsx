import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const legacyApi = vi.hoisted(() => ({
  fetchLegacyCatalogPreview: vi.fn(),
  applyLegacyComponent: vi.fn(),
  applyLegacyGrade: vi.fn(),
}))

vi.mock('../api/legacyCatalog', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/legacyCatalog')>()),
  fetchLegacyCatalogPreview: legacyApi.fetchLegacyCatalogPreview,
  applyLegacyComponent: legacyApi.applyLegacyComponent,
  applyLegacyGrade: legacyApi.applyLegacyGrade,
}))

import LegacyCatalogReviewDrawer from './LegacyCatalogReviewDrawer'

const preview = {
  source: {
    record_count: 128,
    period_from: '2024-01-01',
    period_to: '2025-12-31',
    snapshot_id: 'legacy-snapshot-001',
  },
  component_candidates: [
    {
      source_field: '综合薪资',
      record_count: 128,
      nonzero_count: 127,
      period_from: '2024-01-01',
      period_to: '2025-12-31',
      suggested_component_type: 'COMPREHENSIVE',
      suggested_allowance_kind: null,
      classification: 'NEEDS_HR_CONFIRMATION' as const,
      importable: true,
      applied: false,
      applied_target_id: null,
      note: '请由薪酬负责人确认正式组件定义。',
    },
    {
      source_field: '餐补',
      record_count: 96,
      nonzero_count: 72,
      period_from: '2024-03-01',
      period_to: '2025-12-31',
      suggested_component_type: 'ALLOWANCE',
      suggested_allowance_kind: null,
      classification: 'NEEDS_HR_CONFIRMATION' as const,
      importable: true,
      applied: false,
      applied_target_id: null,
      note: '旧系统字段不含固定或浮动属性。',
    },
    {
      source_field: '加班工资',
      record_count: 128,
      nonzero_count: 24,
      period_from: '2024-01-01',
      period_to: '2025-12-31',
      suggested_component_type: 'OVERTIME',
      suggested_allowance_kind: null,
      classification: 'DERIVED_NOT_CATALOG_COMPONENT' as const,
      importable: false,
      applied: false,
      applied_target_id: null,
      note: '该字段由考勤派生，只能作为历史核对证据。',
    },
    {
      source_field: '岗位津贴',
      record_count: 80,
      nonzero_count: 80,
      period_from: '2024-01-01',
      period_to: '2025-12-31',
      suggested_component_type: 'POSITION',
      suggested_allowance_kind: null,
      classification: 'NEEDS_HR_CONFIRMATION' as const,
      importable: true,
      applied: true,
      applied_target_id: 21,
      note: '该候选已应用为正式组件。',
    },
  ],
  grade_source_status: 'OFFICIAL_MASTER_NOT_PRESENT' as const,
  grade_candidates: [
    {
      position: '服务员',
      record_count: 18,
      contributor_count: 9,
      salary_sample_count: 15,
      period_from: '2024-01-01',
      period_to: '2025-12-31',
      observed_p25: '3600.00',
      observed_median: '4200.00',
      observed_p75: '4800.00',
      suppressed_for_privacy: false,
      applied: false,
      applied_target_id: null,
      is_official_grade: false as const,
    },
    {
      position: '店长',
      record_count: 12,
      contributor_count: 7,
      salary_sample_count: 10,
      period_from: '2024-01-01',
      period_to: '2025-12-31',
      observed_p25: '6800.00',
      observed_median: '7500.00',
      observed_p75: '8600.00',
      suppressed_for_privacy: false,
      applied: false,
      applied_target_id: null,
      is_official_grade: false as const,
    },
    {
      position: '稀有岗位',
      record_count: 2,
      contributor_count: 2,
      salary_sample_count: 2,
      period_from: '2024-01-01',
      period_to: '2025-12-31',
      observed_p25: null,
      observed_median: null,
      observed_p75: null,
      suppressed_for_privacy: true,
      applied: false,
      applied_target_id: null,
      is_official_grade: false as const,
    },
    {
      position: '收银员',
      record_count: 14,
      contributor_count: 8,
      salary_sample_count: 12,
      period_from: '2024-01-01',
      period_to: '2025-12-31',
      observed_p25: '3900.00',
      observed_median: '4300.00',
      observed_p75: '4700.00',
      suppressed_for_privacy: false,
      applied: true,
      applied_target_id: 31,
      is_official_grade: false as const,
    },
  ],
  warnings: ['旧系统没有正式薪资组件编码。', '历史职位名称不等于职级。'],
}

function renderDrawer(props: Partial<React.ComponentProps<typeof LegacyCatalogReviewDrawer>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const defaultProps: React.ComponentProps<typeof LegacyCatalogReviewDrawer> = {
    open: true,
    mode: 'components',
    onClose: vi.fn(),
    onApplied: vi.fn(),
  }
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <LegacyCatalogReviewDrawer {...defaultProps} {...props} />
    </QueryClientProvider>,
  )
  return { ...rendered, queryClient, props: { ...defaultProps, ...props } }
}

async function findDialogByTitle(title: string): Promise<HTMLElement> {
  const dialog = (await screen.findByText(title, { selector: '.ant-modal-title' })).closest(
    '[role="dialog"]',
  )
  if (!dialog) throw new Error(`dialog ${title} did not render`)
  return dialog as HTMLElement
}

function expectArrowKeyScrolling(region: HTMLElement) {
  region.focus()
  expect(document.activeElement).toBe(region)
  region.scrollLeft = 0
  fireEvent.keyDown(region, { key: 'ArrowRight', code: 'ArrowRight' })
  expect(region.scrollLeft).toBeGreaterThan(0)
  const afterRight = region.scrollLeft
  fireEvent.keyDown(region, { key: 'ArrowLeft', code: 'ArrowLeft' })
  expect(region.scrollLeft).toBeLessThan(afterRight)
}

describe('LegacyCatalogReviewDrawer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    legacyApi.fetchLegacyCatalogPreview.mockResolvedValue(preview)
    legacyApi.applyLegacyComponent.mockResolvedValue({ id: 9 })
    legacyApi.applyLegacyGrade.mockResolvedValue({ grade: { id: 6 }, band: { id: 7 } })
  })

  afterEach(cleanup)

  it('queries only after opening and shows aggregate source evidence without personal details', async () => {
    const rendered = renderDrawer({ open: false })

    expect(legacyApi.fetchLegacyCatalogPreview).not.toHaveBeenCalled()

    rendered.rerender(
      <QueryClientProvider client={rendered.queryClient}>
        <LegacyCatalogReviewDrawer
          open
          mode="components"
          onClose={rendered.props.onClose}
          onApplied={rendered.props.onApplied}
        />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('128 条来源记录')).toBeTruthy()
    expect(screen.getAllByText('2024-01-01 至 2025-12-31').length).toBeGreaterThan(0)
    expect(screen.getByText('旧系统没有正式薪资组件编码。')).toBeTruthy()
    expect(screen.getByText('页面仅展示字段与岗位汇总，不展示任何员工个人明细。')).toBeTruthy()
    expect(screen.getByText('127')).toBeTruthy()
    expect(screen.queryByText(/姓名|身份证|手机号/)).toBeNull()
    const componentRegion = screen.getByRole('region', { name: '旧系统薪资组件候选' })
    expect(componentRegion.tabIndex).toBe(0)
    expect(within(componentRegion).getByText('综合薪资', { selector: 'strong' })).toBeTruthy()
    expectArrowKeyScrolling(componentRegion)
    expect(legacyApi.fetchLegacyCatalogPreview).toHaveBeenCalledTimes(1)
  })

  it('marks derived component evidence as non-importable', async () => {
    renderDrawer()

    const row = (await screen.findByText('加班工资')).closest('tr')
    expect(row).not.toBeNull()
    expect(within(row as HTMLTableRowElement).getByText('派生结果，仅供核对')).toBeTruthy()
    expect(
      (
        within(row as HTMLTableRowElement).getByRole('button', {
          name: '不可导入',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
  })

  it('requires HR to confirm a complete allowance definition before applying it', async () => {
    const onApplied = vi.fn()
    renderDrawer({ onApplied })

    const row = (await screen.findByText('餐补')).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '审阅并导入' }))

    const dialog = await findDialogByTitle('确认薪资组件')
    fireEvent.change(within(dialog).getByLabelText('组件编码'), {
      target: { value: 'MEAL_ALLOWANCE' },
    })
    fireEvent.change(within(dialog).getByLabelText('组件名称'), {
      target: { value: '工作餐补贴' },
    })
    fireEvent.mouseDown(within(dialog).getByLabelText('补贴方式'))
    fireEvent.click(await screen.findByText('固定补贴'))
    fireEvent.click(within(dialog).getByLabelText('计税'))
    fireEvent.click(within(dialog).getByLabelText('计入社保基数'))
    fireEvent.click(within(dialog).getByLabelText('按实际计薪出勤天数折算'))
    fireEvent.change(within(dialog).getByLabelText('导入依据与原因'), {
      target: { value: '薪酬负责人已对照旧系统字段口径及现行制度确认。' },
    })
    fireEvent.click(within(dialog).getByLabelText('HR 已核对组件定义、补贴方式及全部计薪标志'))
    fireEvent.click(within(dialog).getByRole('button', { name: '确认导入' }))

    await waitFor(() =>
      expect(legacyApi.applyLegacyComponent).toHaveBeenCalledWith({
        source_field: '餐补',
        expected_record_count: 96,
        expected_source_snapshot_id: 'legacy-snapshot-001',
        confirmed_by_hr: true,
        reason: '薪酬负责人已对照旧系统字段口径及现行制度确认。',
        component: {
          code: 'MEAL_ALLOWANCE',
          name: '工作餐补贴',
          component_type: 'ALLOWANCE',
          allowance_kind: 'FIXED',
          taxable: true,
          in_social_base: true,
          in_housing_base: false,
          prorate_by_attendance: true,
          sort_order: 0,
        },
      }),
    )
    expect(onApplied).toHaveBeenCalledTimes(1)
  })

  it('marks applied component candidates complete and prevents reopening them', async () => {
    renderDrawer()

    const row = (await screen.findByText('岗位津贴', { selector: 'strong' })).closest('tr')
    const completed = within(row as HTMLTableRowElement).getByRole('button', { name: '已完成' })

    expect((completed as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(completed)
    expect(screen.queryByText('确认薪资组件', { selector: '.ant-modal-title' })).toBeNull()
    expect(legacyApi.applyLegacyComponent).not.toHaveBeenCalled()
  })

  it('shows attendance proration only for allowances and clears it on a type switch', async () => {
    renderDrawer()

    const ordinaryRow = (await screen.findByText('综合薪资', { selector: 'strong' })).closest('tr')
    fireEvent.click(
      within(ordinaryRow as HTMLTableRowElement).getByRole('button', { name: '审阅并导入' }),
    )
    const ordinaryDialog = await findDialogByTitle('确认薪资组件')
    expect(within(ordinaryDialog).queryByLabelText('按实际计薪出勤天数折算')).toBeNull()
    fireEvent.click(within(ordinaryDialog).getByRole('button', { name: /Cancel|取\s*消/i }))
    await waitFor(() =>
      expect(screen.queryByText('确认薪资组件', { selector: '.ant-modal-title' })).toBeNull(),
    )

    const allowanceRow = screen.getByText('餐补').closest('tr')
    fireEvent.click(
      within(allowanceRow as HTMLTableRowElement).getByRole('button', { name: '审阅并导入' }),
    )
    const allowanceDialog = await findDialogByTitle('确认薪资组件')
    const prorate = within(allowanceDialog).getByLabelText('按实际计薪出勤天数折算')
    fireEvent.click(prorate)
    expect((prorate as HTMLInputElement).checked).toBe(true)

    fireEvent.mouseDown(within(allowanceDialog).getByLabelText('组件类型'))
    fireEvent.click(
      await screen.findByText('基本工资', { selector: '.ant-select-item-option-content' }),
    )
    expect(within(allowanceDialog).queryByLabelText('按实际计薪出勤天数折算')).toBeNull()

    fireEvent.mouseDown(within(allowanceDialog).getByLabelText('组件类型'))
    fireEvent.click(
      await screen.findByText('补贴', { selector: '.ant-select-item-option-content' }),
    )
    expect(
      (within(allowanceDialog).getByLabelText('按实际计薪出勤天数折算') as HTMLInputElement)
        .checked,
    ).toBe(false)
  })

  it('clears component review values before opening another candidate', async () => {
    renderDrawer()

    const firstRow = (await screen.findByText('综合薪资', { selector: 'strong' })).closest('tr')
    fireEvent.click(
      within(firstRow as HTMLTableRowElement).getByRole('button', { name: '审阅并导入' }),
    )
    const firstDialog = await findDialogByTitle('确认薪资组件')
    fireEvent.change(within(firstDialog).getByLabelText('组件编码'), {
      target: { value: 'OLD-CODE' },
    })
    fireEvent.change(within(firstDialog).getByLabelText('导入依据与原因'), {
      target: { value: '不应保留到下一个候选的原因' },
    })
    fireEvent.click(within(firstDialog).getByLabelText('HR 已核对组件定义、补贴方式及全部计薪标志'))
    fireEvent.click(within(firstDialog).getByRole('button', { name: /Cancel|取\s*消/i }))
    await waitFor(() =>
      expect(screen.queryByText('确认薪资组件', { selector: '.ant-modal-title' })).toBeNull(),
    )

    const nextRow = screen.getByText('餐补').closest('tr')
    fireEvent.click(
      within(nextRow as HTMLTableRowElement).getByRole('button', { name: '审阅并导入' }),
    )
    const nextDialog = await findDialogByTitle('确认薪资组件')

    expect((within(nextDialog).getByLabelText('组件编码') as HTMLInputElement).value).toBe('')
    expect((within(nextDialog).getByLabelText('组件名称') as HTMLInputElement).value).toBe('餐补')
    expect((within(nextDialog).getByLabelText('导入依据与原因') as HTMLTextAreaElement).value).toBe(
      '',
    )
    expect(
      (
        within(nextDialog).getByLabelText(
          'HR 已核对组件定义、补贴方式及全部计薪标志',
        ) as HTMLInputElement
      ).checked,
    ).toBe(false)
  })

  it('fails closed when the aggregate preview cannot be loaded', async () => {
    legacyApi.fetchLegacyCatalogPreview.mockRejectedValue({
      response: { data: { detail: '旧系统数据源暂不可用' } },
    })

    renderDrawer()

    expect(await screen.findByText('历史数据证据加载失败')).toBeTruthy()
    expect(screen.getByText('旧系统数据源暂不可用')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '审阅并导入' })).toBeNull()
  })

  it('shows import failures, keeps the form open, and does not report success', async () => {
    legacyApi.applyLegacyComponent.mockRejectedValue({
      response: { status: 422, data: { detail: '组件编码不符合当前规则' } },
    })
    const onApplied = vi.fn()
    renderDrawer({ onApplied })

    const row = (await screen.findByText('综合薪资', { selector: 'strong' })).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '审阅并导入' }))
    const dialog = await findDialogByTitle('确认薪资组件')
    fireEvent.change(within(dialog).getByLabelText('组件编码'), {
      target: { value: 'COMPREHENSIVE' },
    })
    fireEvent.change(within(dialog).getByLabelText('组件名称'), {
      target: { value: '综合薪资' },
    })
    fireEvent.change(within(dialog).getByLabelText('导入依据与原因'), {
      target: { value: '经薪酬负责人核对后确认。' },
    })
    fireEvent.click(within(dialog).getByLabelText('HR 已核对组件定义、补贴方式及全部计薪标志'))
    fireEvent.click(within(dialog).getByRole('button', { name: '确认导入' }))

    expect(await within(dialog).findByText('组件编码不符合当前规则')).toBeTruthy()
    expect(screen.getByText('确认薪资组件', { selector: '.ant-modal-title' })).toBeTruthy()
    expect((within(dialog).getByLabelText('组件编码') as HTMLInputElement).value).toBe(
      'COMPREHENSIVE',
    )
    expect((within(dialog).getByLabelText('导入依据与原因') as HTMLTextAreaElement).value).toBe(
      '经薪酬负责人核对后确认。',
    )
    expect(
      (
        within(dialog).getByLabelText(
          'HR 已核对组件定义、补贴方式及全部计薪标志',
        ) as HTMLInputElement
      ).checked,
    ).toBe(true)
    expect(legacyApi.fetchLegacyCatalogPreview).toHaveBeenCalledTimes(1)
    expect(onApplied).not.toHaveBeenCalled()
  })

  it('forces a fresh selection after a 409 snapshot drift even when record counts match', async () => {
    const refreshedPreview = {
      ...preview,
      source: { ...preview.source, snapshot_id: 'legacy-snapshot-002' },
    }
    legacyApi.fetchLegacyCatalogPreview
      .mockResolvedValueOnce(preview)
      .mockResolvedValueOnce(refreshedPreview)
      .mockResolvedValue(refreshedPreview)
    legacyApi.applyLegacyComponent
      .mockRejectedValueOnce({
        response: { status: 409, data: { detail: '来源快照已变化，请重新审阅' } },
      })
      .mockResolvedValue({ id: 9 })
    renderDrawer()

    const firstRow = (await screen.findByText('综合薪资', { selector: 'strong' })).closest('tr')
    fireEvent.click(
      within(firstRow as HTMLTableRowElement).getByRole('button', { name: '审阅并导入' }),
    )
    const firstDialog = await findDialogByTitle('确认薪资组件')
    fireEvent.change(within(firstDialog).getByLabelText('组件编码'), {
      target: { value: 'SNAPSHOT-S1' },
    })
    fireEvent.change(within(firstDialog).getByLabelText('导入依据与原因'), {
      target: { value: '基于 S1 快照的确认' },
    })
    fireEvent.click(within(firstDialog).getByLabelText('HR 已核对组件定义、补贴方式及全部计薪标志'))
    fireEvent.click(within(firstDialog).getByRole('button', { name: '确认导入' }))

    await waitFor(() => expect(legacyApi.applyLegacyComponent).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(legacyApi.fetchLegacyCatalogPreview).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(screen.queryByText('确认薪资组件', { selector: '.ant-modal-title' })).toBeNull(),
    )

    const refreshedRow = (await screen.findByText('综合薪资', { selector: 'strong' })).closest('tr')
    expect(within(refreshedRow as HTMLTableRowElement).getByText('128')).toBeTruthy()
    fireEvent.click(
      within(refreshedRow as HTMLTableRowElement).getByRole('button', { name: '审阅并导入' }),
    )
    const refreshedDialog = await findDialogByTitle('确认薪资组件')
    expect((within(refreshedDialog).getByLabelText('组件编码') as HTMLInputElement).value).toBe('')
    expect(
      (within(refreshedDialog).getByLabelText('导入依据与原因') as HTMLTextAreaElement).value,
    ).toBe('')
    expect(
      (
        within(refreshedDialog).getByLabelText(
          'HR 已核对组件定义、补贴方式及全部计薪标志',
        ) as HTMLInputElement
      ).checked,
    ).toBe(false)

    fireEvent.change(within(refreshedDialog).getByLabelText('组件编码'), {
      target: { value: 'SNAPSHOT-S2' },
    })
    fireEvent.change(within(refreshedDialog).getByLabelText('导入依据与原因'), {
      target: { value: '重新审阅 S2 快照后确认' },
    })
    fireEvent.click(
      within(refreshedDialog).getByLabelText('HR 已核对组件定义、补贴方式及全部计薪标志'),
    )
    fireEvent.click(within(refreshedDialog).getByRole('button', { name: '确认导入' }))

    await waitFor(() => expect(legacyApi.applyLegacyComponent).toHaveBeenCalledTimes(2))
    expect(legacyApi.applyLegacyComponent).toHaveBeenLastCalledWith(
      expect.objectContaining({
        expected_record_count: 128,
        expected_source_snapshot_id: 'legacy-snapshot-002',
      }),
    )
  })

  it('freezes the component review snapshot across a same-count background refresh', async () => {
    const refreshedPreview = {
      ...preview,
      source: { ...preview.source, snapshot_id: 'legacy-snapshot-002' },
    }
    legacyApi.fetchLegacyCatalogPreview
      .mockResolvedValueOnce(preview)
      .mockResolvedValue(refreshedPreview)
    const { queryClient } = renderDrawer()

    const row = (await screen.findByText('综合薪资', { selector: 'strong' })).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '审阅并导入' }))
    const dialog = await findDialogByTitle('确认薪资组件')

    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['legacy-catalog-preview'] })
    })
    await waitFor(() => expect(legacyApi.fetchLegacyCatalogPreview).toHaveBeenCalledTimes(2))

    fireEvent.change(within(dialog).getByLabelText('组件编码'), {
      target: { value: 'SNAPSHOT-S1-COMPONENT' },
    })
    fireEvent.change(within(dialog).getByLabelText('导入依据与原因'), {
      target: { value: '仍按打开表单时审阅的 S1 快照确认' },
    })
    fireEvent.click(within(dialog).getByLabelText('HR 已核对组件定义、补贴方式及全部计薪标志'))
    fireEvent.click(within(dialog).getByRole('button', { name: '确认导入' }))

    await waitFor(() => expect(legacyApi.applyLegacyComponent).toHaveBeenCalledTimes(1))
    expect(legacyApi.applyLegacyComponent).toHaveBeenCalledWith(
      expect.objectContaining({
        source_field: '综合薪资',
        expected_record_count: 128,
        expected_source_snapshot_id: 'legacy-snapshot-001',
      }),
    )
    expect(legacyApi.applyLegacyComponent).not.toHaveBeenCalledWith(
      expect.objectContaining({ expected_source_snapshot_id: 'legacy-snapshot-002' }),
    )
  })

  it('warns that historical position distributions are not official grades and suppresses small groups', async () => {
    renderDrawer({ mode: 'grades' })

    expect(await screen.findByText('旧系统没有官方职级主表')).toBeTruthy()
    expect(
      screen.getByText('历史薪资分位仅是观察结果，不是公司正式薪档，也不会被自动采用。'),
    ).toBeTruthy()
    const gradeRegion = screen.getByRole('region', { name: '旧系统历史职位候选' })
    expect(gradeRegion.tabIndex).toBe(0)
    expect(within(gradeRegion).getByText('服务员')).toBeTruthy()
    expectArrowKeyScrolling(gradeRegion)
    const suppressedRow = screen.getByText('稀有岗位').closest('tr')
    expect(suppressedRow).not.toBeNull()
    expect(within(suppressedRow as HTMLTableRowElement).getByText('低于隐私阈值')).toBeTruthy()
    expect(
      (
        within(suppressedRow as HTMLTableRowElement).getByRole('button', {
          name: '不可应用',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
    expect(within(suppressedRow as HTMLTableRowElement).queryByText('2')).toBeNull()
  })

  it('applies an HR-confirmed grade and formal band separately from observed history', async () => {
    const onApplied = vi.fn()
    renderDrawer({ mode: 'grades', onApplied })

    const row = (await screen.findByText('服务员')).closest('tr')
    fireEvent.click(
      within(row as HTMLTableRowElement).getByRole('button', { name: '制定正式职级' }),
    )

    const dialog = await findDialogByTitle('确认正式职级与薪档')
    fireEvent.change(within(dialog).getByLabelText('职级编码'), {
      target: { value: 'STORE-P1' },
    })
    fireEvent.change(within(dialog).getByLabelText('职级名称'), {
      target: { value: '门店一职级' },
    })
    fireEvent.change(within(dialog).getByLabelText('级别序号'), { target: { value: '10' } })
    fireEvent.change(within(dialog).getByLabelText('正式薪档最低值'), {
      target: { value: '3800' },
    })
    fireEvent.change(within(dialog).getByLabelText('正式薪档中位值'), {
      target: { value: '4300' },
    })
    fireEvent.change(within(dialog).getByLabelText('正式薪档最高值'), {
      target: { value: '5000' },
    })
    fireEvent.change(within(dialog).getByLabelText('薪档生效日期'), {
      target: { value: '2026-07-01' },
    })
    fireEvent.change(within(dialog).getByLabelText('制定依据与原因'), {
      target: { value: '人事依据现行薪酬制度确认职位映射及正式薪档。' },
    })
    fireEvent.change(within(dialog).getByLabelText('政策确认口令'), {
      target: { value: 'HR_CONFIRMED' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: '确认应用' }))

    await waitFor(() =>
      expect(legacyApi.applyLegacyGrade).toHaveBeenCalledWith({
        source_position: '服务员',
        expected_record_count: 18,
        expected_source_snapshot_id: 'legacy-snapshot-001',
        policy_confirmation: 'HR_CONFIRMED',
        reason: '人事依据现行薪酬制度确认职位映射及正式薪档。',
        grade: { code: 'STORE-P1', name: '门店一职级', rank: 10 },
        band: {
          band_min: '3800.00',
          band_mid: '4300.00',
          band_max: '5000.00',
          effective_from: '2026-07-01',
        },
      }),
    )
    expect(onApplied).toHaveBeenCalledTimes(1)
  })

  it('freezes the grade review snapshot across a same-count background refresh', async () => {
    const refreshedPreview = {
      ...preview,
      source: { ...preview.source, snapshot_id: 'legacy-snapshot-002' },
    }
    legacyApi.fetchLegacyCatalogPreview
      .mockResolvedValueOnce(preview)
      .mockResolvedValue(refreshedPreview)
    const { queryClient } = renderDrawer({ mode: 'grades' })

    const row = (await screen.findByText('服务员')).closest('tr')
    fireEvent.click(
      within(row as HTMLTableRowElement).getByRole('button', { name: '制定正式职级' }),
    )
    const dialog = await findDialogByTitle('确认正式职级与薪档')

    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['legacy-catalog-preview'] })
    })
    await waitFor(() => expect(legacyApi.fetchLegacyCatalogPreview).toHaveBeenCalledTimes(2))

    const values: Record<string, string> = {
      职级编码: 'SNAPSHOT-S1-GRADE',
      职级名称: 'S1 审阅职级',
      级别序号: '10',
      正式薪档最低值: '3800',
      正式薪档中位值: '4300',
      正式薪档最高值: '5000',
      薪档生效日期: '2026-07-01',
      制定依据与原因: '仍按打开表单时审阅的 S1 快照确认',
      政策确认口令: 'HR_CONFIRMED',
    }
    Object.entries(values).forEach(([label, value]) => {
      fireEvent.change(within(dialog).getByLabelText(label), { target: { value } })
    })
    fireEvent.click(within(dialog).getByRole('button', { name: '确认应用' }))

    await waitFor(() => expect(legacyApi.applyLegacyGrade).toHaveBeenCalledTimes(1))
    expect(legacyApi.applyLegacyGrade).toHaveBeenCalledWith(
      expect.objectContaining({
        source_position: '服务员',
        expected_record_count: 18,
        expected_source_snapshot_id: 'legacy-snapshot-001',
      }),
    )
    expect(legacyApi.applyLegacyGrade).not.toHaveBeenCalledWith(
      expect.objectContaining({ expected_source_snapshot_id: 'legacy-snapshot-002' }),
    )
  })

  it('marks applied grade candidates complete and prevents reopening them', async () => {
    renderDrawer({ mode: 'grades' })

    const row = (await screen.findByText('收银员')).closest('tr')
    const completed = within(row as HTMLTableRowElement).getByRole('button', { name: '已完成' })

    expect((completed as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(completed)
    expect(screen.queryByText('确认正式职级与薪档', { selector: '.ant-modal-title' })).toBeNull()
    expect(legacyApi.applyLegacyGrade).not.toHaveBeenCalled()
  })

  it('clears grade policy confirmation and formal values before opening another candidate', async () => {
    renderDrawer({ mode: 'grades' })

    const firstRow = (await screen.findByText('服务员')).closest('tr')
    fireEvent.click(
      within(firstRow as HTMLTableRowElement).getByRole('button', { name: '制定正式职级' }),
    )
    const firstDialog = await findDialogByTitle('确认正式职级与薪档')
    const staleValues: Record<string, string> = {
      职级编码: 'OLD-GRADE',
      职级名称: '旧职级',
      级别序号: '99',
      正式薪档最低值: '1000',
      正式薪档中位值: '2000',
      正式薪档最高值: '3000',
      薪档生效日期: '2026-01-01',
      制定依据与原因: '不应保留的旧原因',
      政策确认口令: 'HR_CONFIRMED',
    }
    Object.entries(staleValues).forEach(([label, value]) => {
      fireEvent.change(within(firstDialog).getByLabelText(label), { target: { value } })
    })
    fireEvent.click(within(firstDialog).getByRole('button', { name: /Cancel|取\s*消/i }))
    await waitFor(() =>
      expect(screen.queryByText('确认正式职级与薪档', { selector: '.ant-modal-title' })).toBeNull(),
    )

    const nextRow = screen.getByText('店长').closest('tr')
    fireEvent.click(
      within(nextRow as HTMLTableRowElement).getByRole('button', { name: '制定正式职级' }),
    )
    const nextDialog = await findDialogByTitle('确认正式职级与薪档')

    ;[
      '职级编码',
      '职级名称',
      '正式薪档最低值',
      '正式薪档中位值',
      '正式薪档最高值',
      '薪档生效日期',
      '制定依据与原因',
      '政策确认口令',
    ].forEach((label) => {
      expect((within(nextDialog).getByLabelText(label) as HTMLInputElement).value).toBe('')
    })
    expect((within(nextDialog).getByLabelText('级别序号') as HTMLInputElement).value).toBe('0')
  })

  it('prevents duplicate submission while an import is pending', async () => {
    let resolveApply: ((value: { id: number }) => void) | undefined
    let form: HTMLFormElement | null = null
    let replayedSubmit = false
    legacyApi.applyLegacyComponent.mockImplementation(() => {
      if (!replayedSubmit) {
        replayedSubmit = true
        if (!form) throw new Error('component review form was not captured')
        fireEvent.submit(form)
      }
      return new Promise<{ id: number }>((resolve) => {
        resolveApply = resolve
      })
    })
    renderDrawer()

    const row = (await screen.findByText('综合薪资', { selector: 'strong' })).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '审阅并导入' }))
    const dialog = await findDialogByTitle('确认薪资组件')
    fireEvent.change(within(dialog).getByLabelText('组件编码'), { target: { value: 'TOTAL' } })
    fireEvent.change(within(dialog).getByLabelText('组件名称'), { target: { value: '综合薪资' } })
    fireEvent.change(within(dialog).getByLabelText('导入依据与原因'), {
      target: { value: '人事确认旧字段可作为正式组件。' },
    })
    fireEvent.click(within(dialog).getByLabelText('HR 已核对组件定义、补贴方式及全部计薪标志'))
    const submit = within(dialog).getByRole('button', { name: '确认导入' })
    form = dialog.querySelector('form')
    if (!form) throw new Error('component review form did not render')
    fireEvent.click(submit)
    fireEvent.click(submit)

    await waitFor(() => expect(legacyApi.applyLegacyComponent).toHaveBeenCalled())
    await new Promise<void>((resolve) => setTimeout(resolve, 0))
    expect(legacyApi.applyLegacyComponent).toHaveBeenCalledTimes(1)
    expect((submit as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(submit)
    expect(legacyApi.applyLegacyComponent).toHaveBeenCalledTimes(1)

    if (!resolveApply) throw new Error('apply mutation did not start')
    resolveApply({ id: 9 })
  })

  it('prevents duplicate grade submissions in the same render frame', async () => {
    let resolveApply: ((value: { grade: { id: number }; band: { id: number } }) => void) | undefined
    let form: HTMLFormElement | null = null
    let replayedSubmit = false
    legacyApi.applyLegacyGrade.mockImplementation(() => {
      if (!replayedSubmit) {
        replayedSubmit = true
        if (!form) throw new Error('grade review form was not captured')
        fireEvent.submit(form)
      }
      return new Promise<{ grade: { id: number }; band: { id: number } }>((resolve) => {
        resolveApply = resolve
      })
    })
    renderDrawer({ mode: 'grades' })

    const row = (await screen.findByText('服务员')).closest('tr')
    fireEvent.click(
      within(row as HTMLTableRowElement).getByRole('button', { name: '制定正式职级' }),
    )
    const dialog = await findDialogByTitle('确认正式职级与薪档')
    const values: Record<string, string> = {
      职级编码: 'STORE-P1',
      职级名称: '门店一职级',
      级别序号: '10',
      正式薪档最低值: '3800',
      正式薪档中位值: '4300',
      正式薪档最高值: '5000',
      薪档生效日期: '2026-07-01',
      制定依据与原因: '人事确认本次正式职级与薪档。',
      政策确认口令: 'HR_CONFIRMED',
    }
    Object.entries(values).forEach(([label, value]) => {
      fireEvent.change(within(dialog).getByLabelText(label), { target: { value } })
    })
    form = dialog.querySelector('form')
    if (!form) throw new Error('grade review form did not render')
    const submit = within(dialog).getByRole('button', { name: '确认应用' })

    fireEvent.click(submit)
    fireEvent.click(submit)

    await waitFor(() => expect(legacyApi.applyLegacyGrade).toHaveBeenCalled())
    await new Promise<void>((resolve) => setTimeout(resolve, 0))
    expect(legacyApi.applyLegacyGrade).toHaveBeenCalledTimes(1)

    if (!resolveApply) throw new Error('grade apply mutation did not start')
    resolveApply({ grade: { id: 6 }, band: { id: 7 } })
  })
})
