import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DingTalkOrganizationPreview } from '../api/dingtalk'

const masterdataApi = vi.hoisted(() => ({ fetchOrgTree: vi.fn() }))
const dingtalkApi = vi.hoisted(() => ({
  applyDingTalkOrganization: vi.fn(),
  previewDingTalkOrganization: vi.fn(),
}))
const auth = vi.hoisted(() => ({
  permissions: [] as string[],
  globalPermissions: [] as string[],
}))

vi.mock('../api/masterdata', () => masterdataApi)
vi.mock('../api/dingtalk', () => dingtalkApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'group-hr' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
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

const preview: DingTalkOrganizationPreview = {
  batch_id: '3fe80f532f184247b477694427bad0ce',
  expires_at: '2026-07-22T04:00:00Z',
  remote_regions: 2,
  local_regions: 1,
  ready_regions: 1,
  region_conflicts: 0,
  remote_stores: 4,
  local_stores: 5,
  ready_stores: 5,
  store_conflicts: 0,
  ready_reviewers: 2,
  reviewer_conflicts: 1,
  region_items: [
    {
      id: 100,
      kind: 'REGION',
      remote_department_id: 8001,
      remote_department_name: '广州区域',
      remote_department_path: '集团 / 广州区域',
      action: 'CREATE',
      change_fields: ['name', 'dingtalk_dept_id'],
      match_method: 'REMOTE_ONLY',
      proposed_org_unit_id: null,
      proposed_org_unit_name: '广州区域',
      proposed_parent_org_unit_id: 1,
      proposed_parent_org_unit_name: '集团',
      status: 'READY',
      conflict_code: null,
    },
  ],
  store_items: [
    {
      id: 101,
      kind: 'STORE',
      remote_department_id: 9001,
      remote_department_name: '天河店',
      remote_department_path: '集团 / 潮发运营中心 / 天河店',
      action: 'LINK',
      change_fields: [],
      match_method: 'STABLE_DEPARTMENT_ID',
      proposed_org_unit_id: 11,
      proposed_org_unit_name: '天河店',
      proposed_parent_org_unit_id: 1,
      proposed_parent_org_unit_name: '广州区域',
      status: 'READY',
      conflict_code: null,
    },
    {
      id: 102,
      kind: 'STORE',
      remote_department_id: 9002,
      remote_department_name: '新DNA店',
      remote_department_path: '集团 / 九亩地 / 新DNA店',
      action: 'CREATE',
      change_fields: [],
      match_method: 'REMOTE_ONLY',
      proposed_org_unit_id: null,
      proposed_org_unit_name: '新DNA店',
      proposed_parent_org_unit_id: 2,
      proposed_parent_org_unit_name: '佛山区域',
      status: 'READY',
      conflict_code: null,
    },
    {
      id: 103,
      kind: 'STORE',
      remote_department_id: 9003,
      remote_department_name: '北城店',
      remote_department_path: '集团 / 中山 / 北城店',
      action: 'ACTIVATE',
      change_fields: [],
      match_method: 'STABLE_DEPARTMENT_ID',
      proposed_org_unit_id: 13,
      proposed_org_unit_name: '北城店',
      proposed_parent_org_unit_id: 3,
      proposed_parent_org_unit_name: '中山区域',
      status: 'READY',
      conflict_code: null,
    },
    {
      id: 104,
      kind: 'STORE',
      remote_department_id: 9004,
      remote_department_name: '西城店',
      remote_department_path: '集团 / 潮发运营中心 / 西城店',
      action: 'UPDATE',
      change_fields: ['name'],
      match_method: 'STABLE_DEPARTMENT_ID',
      proposed_org_unit_id: 14,
      proposed_org_unit_name: '西城店',
      proposed_parent_org_unit_id: 1,
      proposed_parent_org_unit_name: '广州区域',
      status: 'READY',
      conflict_code: null,
    },
    {
      id: 105,
      kind: 'STORE',
      remote_department_id: null,
      remote_department_name: '旧城店',
      remote_department_path: '本地 / 旧城店',
      action: 'DEACTIVATE',
      change_fields: [],
      match_method: 'LOCAL_STORE_NOT_VISIBLE',
      proposed_org_unit_id: 15,
      proposed_org_unit_name: '旧城店',
      proposed_parent_org_unit_id: 1,
      proposed_parent_org_unit_name: '广州区域',
      status: 'READY',
      conflict_code: null,
    },
  ],
  reviewer_items: [
    {
      id: 201,
      remote_department_id: 9001,
      remote_department_name: '天河店',
      remote_department_path: '集团 / 潮发运营中心 / 天河店',
      department: 'DINING',
      action: 'ASSIGN',
      dingtalk_name: '店长甲',
      proposed_employee_id: 31,
      proposed_employee_name: '店长甲（M001）',
      match_method: 'JOB_NUMBER',
      current_reviewer_name: null,
      status: 'READY',
      conflict_code: null,
    },
    {
      id: 202,
      remote_department_id: 9001,
      remote_department_name: '天河店',
      remote_department_path: '集团 / 潮发运营中心 / 天河店',
      department: 'KITCHEN',
      action: 'REMOVE',
      dingtalk_name: null,
      proposed_employee_id: null,
      proposed_employee_name: null,
      match_method: 'CLEAR_MISSING_MANAGER',
      current_reviewer_name: '旧厨房经理',
      status: 'READY',
      conflict_code: null,
    },
    {
      id: 203,
      remote_department_id: 9004,
      remote_department_name: '西城店',
      remote_department_path: '集团 / 潮发运营中心 / 西城店',
      department: 'DINING',
      action: 'CONFLICT',
      dingtalk_name: '店长丙',
      proposed_employee_id: null,
      proposed_employee_name: null,
      match_method: 'NONE',
      current_reviewer_name: '旧店长',
      status: 'CONFLICT',
      conflict_code: 'EMPLOYEE_NOT_FOUND',
    },
  ],
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

describe('OrgTreePage DingTalk organization sync', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.permissions = ['dingtalk_org:sync', 'notification:manage']
    auth.globalPermissions = ['dingtalk_org:sync', 'notification:manage']
    masterdataApi.fetchOrgTree.mockResolvedValue(originalTree)
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue(preview)
    dingtalkApi.applyDingTalkOrganization.mockResolvedValue({
      applied_stores: 4,
      applied_reviewers: 2,
      unresolved: 1,
      already_applied: false,
    })
  })

  afterEach(cleanup)

  it('shows the sync entry only to HR users with the dedicated permission', async () => {
    auth.permissions = []
    auth.globalPermissions = []

    renderPage()

    expect(await screen.findByText('天河店（门店 · 广州）')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '同步钉钉门店与负责人' })).toBeNull()
    expect(dingtalkApi.previewDingTalkOrganization).not.toHaveBeenCalled()
  })

  it('hides the sync entry when either permission is only locally scoped', async () => {
    auth.globalPermissions = ['dingtalk_org:sync']

    renderPage()

    expect(await screen.findByText('天河店（门店 · 广州）')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '同步钉钉门店与负责人' })).toBeNull()
  })

  it('separates assign, removal and conflict changes and blocks apply on reviewer conflicts', async () => {
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '同步钉钉门店与负责人' }))

    const dialog = await screen.findByRole('dialog', { name: '钉钉组织同步预览' })
    const assignments = within(dialog).getByRole('region', { name: '负责人分配（1）' })
    const removals = within(dialog).getByRole('region', { name: '负责人撤销（1）' })
    const conflicts = within(dialog).getByRole('region', { name: '负责人冲突（1）' })

    expect(within(assignments).getByText('店长甲（M001）（ID 31）')).toBeTruthy()
    expect(within(assignments).getByText('JOB_NUMBER')).toBeTruthy()
    expect(within(removals).getByText('将撤销旧负责人：旧厨房经理')).toBeTruthy()
    expect(within(removals).queryByText(/安全唯一匹配/)).toBeNull()
    expect(within(conflicts).getByText('EMPLOYEE_NOT_FOUND')).toBeTruthy()
    expect(within(dialog).getByText('请先修正钉钉负责人或员工身份信息后重新预览')).toBeTruthy()

    const applyButton = within(dialog).getByRole('button', { name: '确认应用变更' })
    expect((applyButton as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(applyButton)
    expect(dingtalkApi.applyDingTalkOrganization).not.toHaveBeenCalled()
  })

  it('renders safe region changes but blocks them until hierarchy apply is supported', async () => {
    const regionOnlyPreview = {
      ...preview,
      ready_stores: 0,
      store_items: [],
      ready_reviewers: 0,
      reviewer_conflicts: 0,
      reviewer_items: [],
    }
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue(regionOnlyPreview)

    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '同步钉钉门店与负责人' }))

    const dialog = await screen.findByRole('dialog', { name: '钉钉组织同步预览' })
    const regionChanges = within(dialog).getByRole('region', { name: '区域变更（1）' })
    expect(within(regionChanges).getByText('创建新区域')).toBeTruthy()
    expect(within(regionChanges).getByText('集团 / 广州区域')).toBeTruthy()
    expect(within(regionChanges).getByText('名称、钉钉部门 ID')).toBeTruthy()

    const applyButton = within(dialog).getByRole('button', { name: '确认应用变更' })
    expect(within(dialog).getByText('区域变更暂不可确认')).toBeTruthy()
    expect(
      within(dialog).getByText('区域变更将在组织层级应用支持完成后可确认。'),
    ).toBeTruthy()
    expect((applyButton as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(applyButton)
    expect(dingtalkApi.applyDingTalkOrganization).not.toHaveBeenCalled()
  })

  it('blocks apply when the preview contains a region conflict', async () => {
    const regionConflictPreview = {
      ...preview,
      region_conflicts: 1,
      ready_stores: 1,
      store_items: [preview.store_items[0]],
      ready_reviewers: 0,
      reviewer_conflicts: 0,
      reviewer_items: [],
      region_items: [
        ...preview.region_items,
        {
          ...preview.region_items[0],
          id: 106,
          status: 'CONFLICT' as const,
          conflict_code: 'ORG_PATH_AMBIGUOUS',
        },
      ],
    }
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue(regionConflictPreview)

    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '同步钉钉门店与负责人' }))

    const dialog = await screen.findByRole('dialog', { name: '钉钉组织同步预览' })
    const applyButton = within(dialog).getByRole('button', { name: '确认应用变更' })
    expect((applyButton as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(applyButton)
    expect(dingtalkApi.applyDingTalkOrganization).not.toHaveBeenCalled()
  })

  it.each([
    ['a pending region count', { ready_regions: 1, region_conflicts: 0, region_items: [] }],
    ['a region conflict count', { ready_regions: 0, region_conflicts: 1, region_items: [] }],
    ['region preview items', { ready_regions: 0, region_conflicts: 0, region_items: preview.region_items }],
  ])('fails closed for %s even when no other conflict is present', async (_reason, regionState) => {
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue({
      ...preview,
      ...regionState,
      ready_stores: 1,
      store_conflicts: 0,
      store_items: [preview.store_items[0]],
      ready_reviewers: 0,
      reviewer_conflicts: 0,
      reviewer_items: [],
    })

    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '同步钉钉门店与负责人' }))

    const dialog = await screen.findByRole('dialog', { name: '钉钉组织同步预览' })
    const applyButton = within(dialog).getByRole('button', { name: '确认应用变更' })
    expect((applyButton as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(applyButton)
    expect(dingtalkApi.applyDingTalkOrganization).not.toHaveBeenCalled()
  })

  it('shows every store action and applies the exact staged batch once conflicts are resolved', async () => {
    const resolvedPreview: DingTalkOrganizationPreview = {
      ...preview,
      ready_regions: 0,
      region_conflicts: 0,
      region_items: [],
      reviewer_conflicts: 0,
      reviewer_items: preview.reviewer_items.filter((item) => item.action !== 'CONFLICT'),
    }
    const refreshedTree = [
      {
        ...originalTree[0],
        children: [
          ...originalTree[0].children,
          {
            id: 12,
            code: 'STORE-12',
            name: '新DNA店',
            type: 'STORE',
            parent_id: 1,
            city: null,
            status: 'ACTIVE',
            children: [],
          },
        ],
      },
    ]
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue(resolvedPreview)
    masterdataApi.fetchOrgTree
      .mockResolvedValueOnce(originalTree)
      .mockResolvedValueOnce(refreshedTree)

    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '同步钉钉门店与负责人' }))

    const dialog = await screen.findByRole('dialog', { name: '钉钉组织同步预览' })
    const storeChanges = within(dialog).getByRole('region', { name: '门店变更（5）' })
    expect(within(storeChanges).getByText('关联已有门店')).toBeTruthy()
    expect(within(storeChanges).getByText('创建新门店')).toBeTruthy()
    expect(within(storeChanges).getByText('启用门店')).toBeTruthy()
    expect(within(storeChanges).getByText('更新门店')).toBeTruthy()
    expect(within(storeChanges).getByText('停用门店')).toBeTruthy()
    expect(within(dialog).getByText('新建门店的城市信息需人事后续配置')).toBeTruthy()
    expect(within(storeChanges).getByText('本地 / 旧城店')).toBeTruthy()

    fireEvent.click(within(dialog).getByRole('button', { name: '确认应用变更' }))

    await waitFor(() =>
      expect(dingtalkApi.applyDingTalkOrganization).toHaveBeenCalledWith(
        '3fe80f532f184247b477694427bad0ce',
      ),
    )
    expect(await screen.findByText('新DNA店（门店）')).toBeTruthy()
    expect(masterdataApi.fetchOrgTree).toHaveBeenCalledTimes(2)
  })

  it('reports when apply succeeds but the organization tree cannot refresh', async () => {
    const resolvedPreview: DingTalkOrganizationPreview = {
      ...preview,
      ready_regions: 0,
      region_conflicts: 0,
      region_items: [],
      reviewer_conflicts: 0,
      reviewer_items: preview.reviewer_items.filter((item) => item.action !== 'CONFLICT'),
    }
    dingtalkApi.previewDingTalkOrganization.mockResolvedValue(resolvedPreview)
    masterdataApi.fetchOrgTree
      .mockResolvedValueOnce(originalTree)
      .mockRejectedValueOnce(new Error('refresh failed'))

    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '同步钉钉门店与负责人' }))
    const dialog = await screen.findByRole('dialog', { name: '钉钉组织同步预览' })
    fireEvent.click(within(dialog).getByRole('button', { name: '确认应用变更' }))

    expect(
      await screen.findByText(
        '钉钉组织变更已应用，但组织架构刷新失败。请刷新页面后核对最新门店和负责人。',
      ),
    ).toBeTruthy()
    expect(screen.queryByRole('dialog', { name: '钉钉组织同步预览' })).toBeNull()
  })
})
