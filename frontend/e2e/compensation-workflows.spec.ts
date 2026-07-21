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
})
