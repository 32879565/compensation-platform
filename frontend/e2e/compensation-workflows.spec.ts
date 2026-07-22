import { randomUUID } from 'node:crypto'

import { getE2ECredentials } from '../src/e2eTargetSafety'

import { expect, signInToE2E, test } from './guardedTest'

const credentials = getE2ECredentials()

const UI = {
  addComponent: '新增组件',
  addComponentDialog: '新增薪资组件',
  code: '编码',
  name: '名称',
  type: '类型',
  baseComponent: '基本',
} as const

function uniqueSuffix(): string {
  // The UUID-derived suffix remains valid for the 32-character component code
  // field and avoids collisions across workers, retries, and CI processes.
  return randomUUID().replaceAll('-', '').slice(0, 20).toUpperCase()
}

test.describe('compensation workflows', () => {
  test.beforeEach(async ({ page, e2eTargetGuard }) => {
    await signInToE2E(page, e2eTargetGuard, credentials)
  })

  test('super administrator can create and list a unique salary component', async ({
    page,
    e2eTargetGuard,
  }) => {
    const suffix = uniqueSuffix()
    const componentCode = `E2E-COMP-${suffix}`
    const componentName = `E2E component ${suffix}`

    e2eTargetGuard.assertPageOrigin(page)
    await page.getByTestId('nav-components').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/components`)
    e2eTargetGuard.assertPageOrigin(page)

    await page.getByRole('button', { name: UI.addComponent, exact: true }).click()
    const dialog = page.getByRole('dialog', { name: UI.addComponentDialog, exact: true })
    await expect(dialog).toBeVisible()

    e2eTargetGuard.assertPageOrigin(page)
    await dialog.getByLabel(UI.code, { exact: true }).fill(componentCode)
    e2eTargetGuard.assertPageOrigin(page)
    await dialog.getByLabel(UI.name, { exact: true }).fill(componentName)
    e2eTargetGuard.assertPageOrigin(page)
    await dialog.getByLabel(UI.type, { exact: true }).click()
    // Ant Design rc-select renders its clickable popup rows without option
    // roles; the semantic role points at its hidden accessibility mirror. Keep
    // this unavoidable framework selector limited to the currently open popup,
    // then use an exact user-visible label for the actual option.
    const openSelectDropdown = page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden)')
    const baseComponentOption = openSelectDropdown.getByText(UI.baseComponent, { exact: true })
    await expect(baseComponentOption).toBeVisible()
    e2eTargetGuard.assertPageOrigin(page)
    await baseComponentOption.click()

    const createResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/salary-components') && response.request().method() === 'POST',
    )
    e2eTargetGuard.assertPageOrigin(page)
    await dialog.getByRole('button', { name: 'OK', exact: true }).click()

    const response = await createResponse
    expect(response.status()).toBe(201)
    const createdComponent = (await response.json()) as { component_type?: unknown }
    expect(createdComponent.component_type).toBe('BASE')
    e2eTargetGuard.assertPageOrigin(page)

    const componentRow = page
      .getByRole('table')
      .getByRole('row')
      .filter({ has: page.getByText(componentCode, { exact: true }) })
    await expect(componentRow).toHaveCount(1)
    await expect(componentRow).toContainText(componentCode)
    await expect(componentRow).toContainText(componentName)
    await expect(componentRow).toContainText(UI.baseComponent)

    // globalSetup has verified this is a disposable E2E stack. The CI stack is
    // destroyed after the run, so this test deliberately has no product API cleanup.
  })

  test('reviews a scheduled organization preview and applies one conflict-free refresh', async ({
    page,
    e2eTargetGuard,
  }) => {
    test.setTimeout(60_000)

    const scheduledBatch = '11111111111111111111111111111111'
    const manualBatch = '22222222222222222222222222222222'
    const farFuture = {
      created_at: '2099-07-22T00:00:00Z',
      last_checked_at: '2099-07-22T00:01:00Z',
      expires_at: '2099-07-22T00:16:00Z',
    }
    const regionItem = {
      id: 90001,
      kind: 'REGION',
      remote_department_id: 91001,
      remote_department_name: 'E2E Mock 新区域',
      remote_department_path: 'E2E Mock 集团 / E2E Mock 新区域',
      source_path: 'E2E Mock 集团 / E2E Mock 新区域',
      local_target_path: 'E2E Mock 集团 / E2E Mock 新区域',
      explanation: '创建本地组织',
      action: 'CREATE',
      change_fields: ['name', 'dingtalk_dept_id'],
      match_method: 'E2E_MOCK_REMOTE_ONLY',
      proposed_org_unit_id: null,
      proposed_org_unit_name: 'E2E Mock 新区域',
      proposed_parent_org_unit_id: 92001,
      proposed_parent_org_unit_name: 'E2E Mock 集团',
      status: 'READY',
      conflict_code: null,
    }
    const storeItem = {
      id: 90002,
      kind: 'STORE',
      remote_department_id: 91002,
      remote_department_name: 'E2E Mock 新门店',
      remote_department_path: 'E2E Mock 集团 / E2E Mock 新区域 / E2E Mock 新门店',
      source_path: 'E2E Mock 集团 / E2E Mock 新区域 / E2E Mock 新门店',
      local_target_path: null,
      explanation: '创建本地组织',
      action: 'CREATE',
      change_fields: ['name', 'parent_id', 'dingtalk_dept_id'],
      match_method: 'E2E_MOCK_REMOTE_ONLY',
      proposed_org_unit_id: null,
      proposed_org_unit_name: 'E2E Mock 新门店',
      proposed_parent_org_unit_id: null,
      proposed_parent_org_unit_name: 'E2E Mock 新区域',
      status: 'READY',
      conflict_code: null,
    }
    const assignedReviewer = {
      id: 90003,
      remote_department_id: 91002,
      remote_department_name: 'E2E Mock 新门店',
      remote_department_path: 'E2E Mock 集团 / E2E Mock 新区域 / E2E Mock 新门店',
      source_path: 'E2E Mock 集团 / E2E Mock 新区域 / E2E Mock 新门店',
      local_target_path: null,
      explanation: '分配负责人复核权限',
      department: 'DINING',
      action: 'ASSIGN',
      dingtalk_name: 'E2E Mock 厅面负责人',
      proposed_employee_id: 93001,
      proposed_employee_name: 'E2E Mock 厅面员工',
      match_method: 'E2E_MOCK_JOB_NUMBER',
      current_reviewer_name: null,
      status: 'READY',
      conflict_code: null,
    }
    const removedReviewer = {
      id: 90004,
      remote_department_id: 91002,
      remote_department_name: 'E2E Mock 新门店',
      remote_department_path: 'E2E Mock 集团 / E2E Mock 新区域 / E2E Mock 新门店',
      source_path: 'E2E Mock 集团 / E2E Mock 新区域 / E2E Mock 新门店',
      local_target_path: 'E2E Mock 集团 / E2E Mock 既有区域',
      explanation: '撤销当前负责人复核权限',
      department: 'KITCHEN',
      action: 'REMOVE',
      dingtalk_name: null,
      proposed_employee_id: null,
      proposed_employee_name: null,
      match_method: 'E2E_MOCK_MISSING_MANAGER',
      current_reviewer_name: 'E2E Mock 旧厨房负责人',
      status: 'READY',
      conflict_code: null,
    }
    const conflictingReviewer = {
      id: 90005,
      remote_department_id: 91003,
      remote_department_name: 'E2E Mock 冲突门店',
      remote_department_path: 'E2E Mock 集团 / E2E Mock 冲突门店',
      source_path: 'E2E Mock 集团 / E2E Mock 冲突门店',
      local_target_path: null,
      explanation: '负责人匹配存在冲突',
      department: 'DINING',
      action: 'CONFLICT',
      dingtalk_name: 'E2E Mock 冲突负责人',
      proposed_employee_id: null,
      proposed_employee_name: null,
      match_method: 'E2E_MOCK_AMBIGUOUS',
      current_reviewer_name: null,
      status: 'CONFLICT',
      conflict_code: 'ORG_MANAGER_AMBIGUOUS',
    }
    const manualPreview = {
      batch_id: manualBatch,
      trigger: 'MANUAL',
      ...farFuture,
      remote_regions: 1,
      local_regions: 0,
      ready_regions: 1,
      region_conflicts: 0,
      remote_stores: 1,
      local_stores: 0,
      ready_stores: 1,
      store_conflicts: 0,
      ready_reviewers: 2,
      reviewer_conflicts: 0,
      warnings: 1,
      region_items: [regionItem],
      store_items: [storeItem],
      reviewer_items: [assignedReviewer, removedReviewer],
    }
    const scheduledPreview = {
      ...manualPreview,
      batch_id: scheduledBatch,
      trigger: 'SCHEDULED',
      reviewer_conflicts: 1,
      reviewer_items: [assignedReviewer, removedReviewer, conflictingReviewer],
    }
    const oldTree = [
      {
        id: 92001,
        code: 'E2E-MOCK-GROUP',
        name: 'E2E Mock 集团',
        type: 'GROUP',
        parent_id: null,
        city: null,
        status: 'ACTIVE',
        children: [
          {
            id: 92003,
            code: 'E2E-MOCK-EXISTING-REGION',
            name: 'E2E Mock 既有区域',
            type: 'REGION',
            parent_id: 92001,
            city: null,
            status: 'ACTIVE',
            children: [],
          },
        ],
      },
    ]
    const newTree = [
      {
        ...oldTree[0],
        children: [
          ...oldTree[0].children,
          {
            id: 92002,
            code: 'E2E-MOCK-REGION',
            name: 'E2E Mock 新区域',
            type: 'REGION',
            parent_id: 92001,
            city: null,
            status: 'ACTIVE',
            children: [],
          },
        ],
      },
    ]

    const counters = { latestReads: 0, treeReads: 0, previewPosts: 0, applyPosts: 0 }
    const unexpectedOrgSyncRequests: string[] = []
    let applied = false
    let previewResponseArmed = false
    let applyResponseArmed = false
    let latestPreview = scheduledPreview

    await page.route('**/api/dingtalk/sync/organization/**', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      const requestKey = `${request.method()} ${url.pathname}`
      if (url.origin !== e2eTargetGuard.verifiedOrigin) {
        unexpectedOrgSyncRequests.push(requestKey)
        await route.abort('blockedbyclient')
        return
      }
      if (
        request.method() === 'GET' &&
        url.pathname === '/api/dingtalk/sync/organization/latest'
      ) {
        counters.latestReads += 1
        await route.fulfill({ status: 200, contentType: 'application/json', json: latestPreview })
        return
      }
      if (
        request.method() === 'POST' &&
        url.pathname === '/api/dingtalk/sync/organization/preview' &&
        previewResponseArmed
      ) {
        previewResponseArmed = false
        counters.previewPosts += 1
        latestPreview = manualPreview
        await route.fulfill({ status: 200, contentType: 'application/json', json: manualPreview })
        return
      }
      if (
        request.method() === 'POST' &&
        url.pathname === `/api/dingtalk/sync/organization/${manualBatch}/apply` &&
        applyResponseArmed
      ) {
        applyResponseArmed = false
        counters.applyPosts += 1
        applied = true
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          json: {
            applied_regions: 1,
            applied_stores: 1,
            applied_reviewers: 2,
            unresolved: 0,
            already_applied: false,
          },
        })
        return
      }
      unexpectedOrgSyncRequests.push(requestKey)
      await route.fulfill({ status: 500, contentType: 'application/json', json: { detail: 'blocked' } })
    })
    await page.route('**/api/org/tree', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      if (
        url.origin !== e2eTargetGuard.verifiedOrigin ||
        request.method() !== 'GET' ||
        url.pathname !== '/api/org/tree'
      ) {
        await route.abort('blockedbyclient')
        return
      }
      counters.treeReads += 1
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        json: applied ? newTree : oldTree,
      })
    })

    e2eTargetGuard.assertPageOrigin(page)
    await page.getByTestId('nav-org').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/org`)
    e2eTargetGuard.assertPageOrigin(page)

    const status = page.getByRole('region', { name: '钉钉组织同步状态' })
    await expect(status.getByText('定时检查', { exact: true })).toBeVisible()
    await expect(page.getByRole('dialog', { name: '钉钉组织同步预览' })).toHaveCount(0)
    await status.getByRole('button', { name: '查看预览', exact: true }).click()

    const scheduledDialog = page.getByRole('dialog', { name: '钉钉组织同步预览' })
    await expect(scheduledDialog).toBeVisible()
    await expect(scheduledDialog.getByRole('region', { name: '区域变更（1）' })).toBeVisible()
    await expect(scheduledDialog.getByRole('region', { name: '门店变更（1）' })).toBeVisible()
    await expect(scheduledDialog.getByRole('region', { name: '负责人分配（1）' })).toBeVisible()
    await expect(scheduledDialog.getByRole('region', { name: '负责人撤销（1）' })).toBeVisible()
    await expect(scheduledDialog.getByRole('region', { name: '负责人冲突（1）' })).toContainText(
      'ORG_MANAGER_AMBIGUOUS',
    )
    for (const removedColumn of [
      '钉钉完整路径',
      '匹配方式',
      '当前负责人',
      '钉钉负责人',
      '拟匹配本地员工',
    ]) {
      await expect(
        scheduledDialog.getByRole('columnheader', { name: removedColumn, exact: true }),
      ).toHaveCount(0)
    }
    await expect(scheduledDialog).not.toContainText('E2E Mock 厅面负责人')
    await expect(scheduledDialog).not.toContainText('E2E Mock 厅面员工')
    await expect(scheduledDialog).not.toContainText('E2E Mock 旧厨房负责人')
    await expect(scheduledDialog).not.toContainText('E2E_MOCK_JOB_NUMBER')
    await expect(scheduledDialog).not.toContainText('91002')
    await expect(
      scheduledDialog.getByRole('button', { name: '确认应用变更', exact: true }),
    ).toBeDisabled()
    expect(counters.applyPosts).toBe(0)
    await scheduledDialog.getByRole('button', { name: /取\s*消/ }).click()

    previewResponseArmed = true
    const previewResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === '/api/dingtalk/sync/organization/preview' &&
        response.request().method() === 'POST',
    )
    await status.getByRole('button', { name: '刷新预览', exact: true }).click()
    expect((await previewResponse).status()).toBe(200)

    const manualDialog = page.getByRole('dialog', { name: '钉钉组织同步预览' })
    await expect(manualDialog.getByText('手动检查', { exact: false })).toBeVisible()
    const applyButton = manualDialog.getByRole('button', {
      name: '确认应用变更',
      exact: true,
    })
    await expect(applyButton).toBeEnabled()

    applyResponseArmed = true
    const applyResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname ===
          `/api/dingtalk/sync/organization/${manualBatch}/apply` &&
        response.request().method() === 'POST',
    )
    await applyButton.click()
    expect((await applyResponse).status()).toBe(200)

    await expect(manualDialog).toHaveCount(0)
    await expect.poll(() => counters.latestReads).toBeGreaterThanOrEqual(2)
    await expect.poll(() => counters.treeReads).toBeGreaterThanOrEqual(2)
    await expect(page.getByText('E2E Mock 新区域（区域）', { exact: true })).toBeVisible()
    expect(counters.previewPosts).toBe(1)
    expect(counters.applyPosts).toBe(1)
    expect(unexpectedOrgSyncRequests).toEqual([])
  })
})
