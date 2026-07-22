import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DingTalkOrganizationPreview } from '../api/dingtalk'

const masterdataApi = vi.hoisted(() => ({ fetchOrgTree: vi.fn() }))
const dingtalkApi = vi.hoisted(() => ({
  applyDingTalkOrganization: vi.fn(),
  fetchLatestDingTalkOrganization: vi.fn(),
  previewDingTalkOrganization: vi.fn(),
}))
const auth = vi.hoisted(() => ({ globalPermissions: [] as string[] }))

vi.mock('../api/masterdata', () => masterdataApi)
vi.mock('../api/dingtalk', () => dingtalkApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'group-hr' },
    hasGlobalPermission: (permission: string) => auth.globalPermissions.includes(permission),
  }),
}))

import OrgTreePage from './OrgTreePage'

const originalTree = [
  {
    id: 1,
    code: 'GROUP',
    name: '集团',
    type: 'GROUP',
    parent_id: null,
    city: null,
    status: 'ACTIVE',
    children: [
      {
        id: 11,
        code: 'STORE-11',
        name: '天河店',
        type: 'STORE',
        parent_id: 1,
        city: '广州',
        status: 'ACTIVE',
        children: [],
      },
    ],
  },
]

const basePreview: DingTalkOrganizationPreview = {
  batch_id: '3fe80f532f184247b477694427bad0ce',
  trigger: 'MANUAL',
  created_at: '2026-07-22T01:59:00Z',
  last_checked_at: '2026-07-22T02:00:00Z',
  expires_at: '2026-07-22T04:00:00Z',
  remote_regions: 2,
  local_regions: 1,
  ready_regions: 2,
  region_conflicts: 0,
  remote_stores: 2,
  local_stores: 2,
  ready_stores: 1,
  store_conflicts: 0,
  ready_reviewers: 2,
  reviewer_conflicts: 0,
  warnings: 1,
  region_items: [
    {
      id: 100,
      kind: 'REGION',
      action: 'CREATE',
      change_fields: ['name'],
      source_path: '集团 / 广州区域',
      local_target_path: '集团 / 广州区域',
      explanation: '创建本地组织',
      status: 'READY',
      conflict_code: null,
      remote_department_id: 8001,
      match_method: 'CANARY_MATCH_METHOD',
    } as DingTalkOrganizationPreview['region_items'][number],
    {
      id: 101,
      kind: 'REGION',
      action: 'UPDATE',
      change_fields: ['parent_id'],
      source_path: '集团 / 华南 / 佛山区域',
      local_target_path: '集团 / 华南大区 / 佛山区域',
      explanation: '更新本地组织',
      status: 'READY',
      conflict_code: null,
    },
  ],
  store_items: [
    {
      id: 110,
      kind: 'STORE',
      action: 'DEACTIVATE',
      change_fields: [],
      source_path: '集团 / 佛山区域 / 旧城店',
      local_target_path: '集团 / 佛山区域 / 旧城店',
      explanation: '停用本地组织',
      status: 'READY',
      conflict_code: null,
    },
  ],
  reviewer_items: [
    {
      id: 201,
      department: 'DINING',
      action: 'ASSIGN_SCOPE',
      source_path: '集团 / 广州区域 / 天河店',
      local_target_path: '集团 / 广州区域 / 天河店',
      explanation: '分配负责人复核权限',
      status: 'READY',
      conflict_code: null,
      dingtalk_name: 'CANARY_钉钉负责人姓名',
      proposed_employee_name: 'CANARY_本地员工姓名',
      current_reviewer_name: 'CANARY_当前负责人姓名',
    } as DingTalkOrganizationPreview['reviewer_items'][number],
    {
      id: 202,
      department: 'KITCHEN',
      action: 'REMOVE_SCOPE',
      source_path: '集团 / 广州区域 / 天河店',
      local_target_path: '集团 / 广州区域 / 天河店',
      explanation: '撤销当前负责人复核权限',
      status: 'READY',
      conflict_code: null,
    },
  ],
}

function previewWith(overrides: Partial<DingTalkOrganizationPreview> = {}) {
  return { ...basePreview, ...overrides }
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <OrgTreePage />
    </QueryClientProvider>,
  )
}

async function refreshAndOpen() {
  fireEvent.click(await screen.findByRole('button', { name: '刷新预览' }))
  return screen.findByRole('dialog', { name: '钉钉组织同步预览' })
}

describe('OrgTreePage DingTalk organization sync', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.setSystemTime(new Date('2026-07-22T02:00:00Z'))
    auth.globalPermissions = ['dingtalk_org:sync', 'notification:manage']
    masterdataApi.fetchOrgTree.mockResolvedValue(originalTree)
    dingtalkApi.fetchLatestDingTalkOrganization.mockResolvedValue(
      previewWith({ trigger: 'SCHEDULED' }),
    )
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue(basePreview)
    dingtalkApi.applyDingTalkOrganization.mockResolvedValue({
      applied_regions: 2,
      applied_stores: 1,
      applied_reviewers: 2,
      unresolved: 0,
      already_applied: false,
    })
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('gates both the status query and controls on both global permissions', async () => {
    auth.globalPermissions = ['dingtalk_org:sync']
    renderPage()

    expect(await screen.findByText('天河店（门店 · 广州）')).toBeTruthy()
    expect(screen.queryByRole('region', { name: '钉钉组织同步状态' })).toBeNull()
    expect(dingtalkApi.fetchLatestDingTalkOrganization).not.toHaveBeenCalled()
  })

  it('shows a scheduled latest summary without opening the modal', async () => {
    renderPage()

    const status = await screen.findByRole('region', { name: '钉钉组织同步状态' })
    expect(within(status).getByText('组织同步检查单')).toBeTruthy()
    expect(await within(status).findByText('定时检查')).toBeTruthy()
    expect(within(status).getByText('5')).toBeTruthy()
    expect(screen.queryByRole('dialog', { name: '钉钉组织同步预览' })).toBeNull()

    fireEvent.click(within(status).getByRole('button', { name: '查看预览' }))
    expect(await screen.findByRole('dialog', { name: '钉钉组织同步预览' })).toBeTruthy()
  })

  it('does not apply an APPLIED latest batch whose historical ready counters remain non-zero', async () => {
    dingtalkApi.fetchLatestDingTalkOrganization.mockResolvedValue(
      previewWith({
        trigger: 'SCHEDULED',
        region_items: basePreview.region_items.map((item) => ({ ...item, status: 'APPLIED' })),
        store_items: basePreview.store_items.map((item) => ({ ...item, status: 'IGNORED' })),
        reviewer_items: basePreview.reviewer_items.map((item) => ({ ...item, status: 'APPLIED' })),
      }),
    )
    renderPage()

    const status = await screen.findByRole('region', { name: '钉钉组织同步状态' })
    await within(status).findByText('定时检查')
    fireEvent.click(within(status).getByRole('button', { name: '查看预览' }))

    const dialog = await screen.findByRole('dialog', { name: '钉钉组织同步预览' })
    const applyButton = within(dialog).getByRole('button', { name: '确认应用变更' })
    expect((applyButton as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(applyButton)
    expect(dingtalkApi.applyDingTalkOrganization).not.toHaveBeenCalled()
  })

  it('treats only latest 404 as an empty history state and keeps the tree usable', async () => {
    dingtalkApi.fetchLatestDingTalkOrganization.mockRejectedValue({ response: { status: 404 } })
    renderPage()

    expect(await screen.findByText('暂无历史预览')).toBeTruthy()
    expect(screen.getByText('天河店（门店 · 广州）')).toBeTruthy()
    expect((screen.getByRole('button', { name: '查看预览' }) as HTMLButtonElement).disabled).toBe(
      true,
    )
  })

  it('shows non-404 latest errors inside the sync status without breaking the tree', async () => {
    dingtalkApi.fetchLatestDingTalkOrganization.mockRejectedValue({
      response: { status: 503, data: { detail: '钉钉组织服务暂不可用' } },
    })
    renderPage()

    expect(await screen.findByText('历史预览读取失败')).toBeTruthy()
    expect(screen.getByText('钉钉组织服务暂不可用')).toBeTruthy()
    expect(screen.getByText('天河店（门店 · 广州）')).toBeTruthy()
  })

  it('renders region create and move, store deactivation, and reviewer removal', async () => {
    renderPage()
    const dialog = await refreshAndOpen()

    const regions = within(dialog).getByRole('region', { name: '区域变更（2）' })
    expect(within(regions).getByText('创建新区域')).toBeTruthy()
    expect(within(regions).getByText('更新区域')).toBeTruthy()
    expect(within(regions).getAllByText('上级组织').length).toBeGreaterThan(0)
    expect(within(regions).getByText('集团 / 华南大区 / 佛山区域')).toBeTruthy()

    const stores = within(dialog).getByRole('region', { name: '门店变更（1）' })
    expect(within(stores).getByText('停用门店')).toBeTruthy()
    expect(within(stores).getAllByText('集团 / 佛山区域 / 旧城店')).toHaveLength(2)

    const removals = within(dialog).getByRole('region', { name: '负责人撤销（1）' })
    expect(within(removals).getByText('撤销负责人权限')).toBeTruthy()
    expect(within(removals).getByText('撤销当前负责人复核权限')).toBeTruthy()
    expect(within(dialog).queryByText('钉钉完整路径')).toBeNull()
    expect(within(dialog).queryByText('匹配方式')).toBeNull()
    expect(within(dialog).queryByText('当前负责人')).toBeNull()
    expect(within(dialog).queryByText('钉钉负责人')).toBeNull()
    expect(within(dialog).queryByText('拟匹配本地员工')).toBeNull()
    expect(dialog.textContent).not.toContain('CANARY_')
    expect(dialog.textContent).not.toContain('8001')
    expect((within(dialog).getByRole('button', { name: '确认应用变更' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('labels applied and ignored items without presenting them as conflicts', async () => {
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue(
      previewWith({
        ready_regions: 0,
        ready_stores: 0,
        ready_reviewers: 0,
        region_items: [{ ...basePreview.region_items[0], action: 'NO_CHANGE', status: 'APPLIED' }],
        store_items: [{ ...basePreview.store_items[0], action: 'NO_CHANGE', status: 'IGNORED' }],
        reviewer_items: [],
      }),
    )
    renderPage()
    const dialog = await refreshAndOpen()

    expect(
      within(within(dialog).getByRole('region', { name: '区域变更（1）' })).getByText('已应用'),
    ).toBeTruthy()
    expect(
      within(within(dialog).getByRole('region', { name: '门店变更（1）' })).getByText('已忽略'),
    ).toBeTruthy()
  })

  it('groups reviewer conflicts by status after raw action normalization', async () => {
    const reviewerConflict = {
      ...basePreview.reviewer_items[0],
      id: 203,
      action: 'NO_CHANGE' as const,
      status: 'CONFLICT' as const,
      conflict_code: 'EMPLOYEE_NOT_FOUND',
    }
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue(
      previewWith({ reviewer_conflicts: 1, reviewer_items: [reviewerConflict] }),
    )
    renderPage()
    const dialog = await refreshAndOpen()

    const conflicts = within(dialog).getByRole('region', { name: '负责人冲突（1）' })
    expect(within(conflicts).getByText('冲突')).toBeTruthy()
    expect(within(conflicts).getByText('EMPLOYEE_NOT_FOUND')).toBeTruthy()
    expect((within(dialog).getByRole('button', { name: '确认应用变更' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it.each([
    ['区域', { region_conflicts: 1 }],
    ['门店', { store_conflicts: 1 }],
    ['负责人', { reviewer_conflicts: 1 }],
  ])('blocks apply when %s changes contain conflicts', async (_category, conflicts) => {
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue(previewWith(conflicts))
    renderPage()
    const dialog = await refreshAndOpen()

    const applyButton = within(dialog).getByRole('button', { name: '确认应用变更' })
    expect((applyButton as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(applyButton)
    expect(dingtalkApi.applyDingTalkOrganization).not.toHaveBeenCalled()
  })

  it('disables an open modal as soon as its preview expires', async () => {
    vi.useRealTimers()
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date('2026-07-22T02:00:00Z'))
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue(
      previewWith({ expires_at: '2026-07-22T02:00:05Z' }),
    )
    renderPage()
    const dialog = await refreshAndOpen()
    expect((within(dialog).getByRole('button', { name: '确认应用变更' }) as HTMLButtonElement).disabled).toBe(false)

    act(() => vi.advanceTimersByTime(5_000))

    expect(await within(dialog).findByText('此预览已过期，请刷新预览后再应用')).toBeTruthy()
    expect((within(dialog).getByRole('button', { name: '确认应用变更' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('blocks apply when there are zero ready changes', async () => {
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue(
      previewWith({ ready_regions: 0, ready_stores: 0, ready_reviewers: 0 }),
    )
    renderPage()
    const dialog = await refreshAndOpen()

    expect(within(dialog).getByText('当前没有待应用的组织变更')).toBeTruthy()
    expect((within(dialog).getByRole('button', { name: '确认应用变更' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('applies exactly once and refreshes both latest preview and the tree', async () => {
    renderPage()
    const dialog = await refreshAndOpen()
    const applyButton = within(dialog).getByRole('button', { name: '确认应用变更' })

    fireEvent.click(applyButton)
    fireEvent.click(applyButton)

    await waitFor(() =>
      expect(dingtalkApi.applyDingTalkOrganization).toHaveBeenCalledWith(
        '3fe80f532f184247b477694427bad0ce',
      ),
    )
    await waitFor(() => expect(masterdataApi.fetchOrgTree).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(dingtalkApi.fetchLatestDingTalkOrganization).toHaveBeenCalledTimes(2),
    )
    expect(dingtalkApi.applyDingTalkOrganization).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog', { name: '钉钉组织同步预览' })).toBeNull()
  })

  it('does not reapply a submitted batch when latest refresh fails and leaves old READY cache', async () => {
    dingtalkApi.fetchLatestDingTalkOrganization
      .mockResolvedValueOnce(previewWith({ trigger: 'SCHEDULED' }))
      .mockRejectedValueOnce({ response: { status: 503, data: { detail: 'refresh failed' } } })
    renderPage()
    const dialog = await refreshAndOpen()
    fireEvent.click(within(dialog).getByRole('button', { name: '确认应用变更' }))

    await waitFor(() =>
      expect(dingtalkApi.fetchLatestDingTalkOrganization).toHaveBeenCalledTimes(2),
    )
    const status = screen.getByRole('region', { name: '钉钉组织同步状态' })
    fireEvent.click(within(status).getByRole('button', { name: '查看预览' }))
    const reopened = await screen.findByRole('dialog', { name: '钉钉组织同步预览' })
    const applyButton = within(reopened).getByRole('button', { name: '确认应用变更' })

    expect((applyButton as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(applyButton)
    await new Promise((resolve) => window.setTimeout(resolve, 0))
    expect(dingtalkApi.applyDingTalkOrganization).toHaveBeenCalledTimes(1)
  })

  it('locks an uncertain failed batch across reopen and same-batch refresh but allows a new batch', async () => {
    const newBatch = previewWith({ batch_id: '4fe80f532f184247b477694427bad0ce' })
    dingtalkApi.previewDingTalkOrganization
      .mockResolvedValueOnce(basePreview)
      .mockResolvedValueOnce(basePreview)
      .mockResolvedValueOnce(newBatch)
    dingtalkApi.applyDingTalkOrganization.mockRejectedValueOnce(new Error('network timeout'))
    renderPage()
    const dialog = await refreshAndOpen()
    fireEvent.click(within(dialog).getByRole('button', { name: '确认应用变更' }))
    expect(await within(dialog).findByText('network timeout')).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: /取\s*消/ }))

    const sameBatch = await refreshAndOpen()
    const lockedButton = within(sameBatch).getByRole('button', { name: '确认应用变更' })
    expect((lockedButton as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(lockedButton)
    expect(dingtalkApi.applyDingTalkOrganization).toHaveBeenCalledTimes(1)
    fireEvent.click(within(sameBatch).getByRole('button', { name: /取\s*消/ }))

    const nextBatch = await refreshAndOpen()
    expect(
      (within(nextBatch).getByRole('button', { name: '确认应用变更' }) as HTMLButtonElement)
        .disabled,
    ).toBe(false)
  })

  it('keeps a persistent success warning when the tree refresh fails and never resubmits', async () => {
    masterdataApi.fetchOrgTree
      .mockResolvedValueOnce(originalTree)
      .mockRejectedValueOnce(new Error('refresh failed'))
    renderPage()
    const dialog = await refreshAndOpen()
    fireEvent.click(within(dialog).getByRole('button', { name: '确认应用变更' }))

    const warning = await screen.findByText(
      '应用已成功，但组织架构刷新失败。请刷新页面后核对最新区域、门店和负责人。',
    )
    expect(warning).toBeTruthy()
    expect(dingtalkApi.fetchLatestDingTalkOrganization).toHaveBeenCalledTimes(2)
    expect(dingtalkApi.applyDingTalkOrganization).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: '查看预览' }))
    const reopened = await screen.findByRole('dialog', { name: '钉钉组织同步预览' })
    fireEvent.click(within(reopened).getByRole('button', { name: /取\s*消/ }))
    expect(screen.getAllByText(/应用已成功，但组织架构刷新失败/).length).toBeGreaterThan(0)
    expect(dingtalkApi.applyDingTalkOrganization).toHaveBeenCalledTimes(1)
  })
})
