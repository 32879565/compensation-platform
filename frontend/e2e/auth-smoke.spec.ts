import { randomUUID } from 'node:crypto'

import { getE2ECredentials } from '../src/e2eTargetSafety'

import { expect, signInToE2E, test } from './guardedTest'

const credentials = getE2ECredentials()

function uniqueE2EIdentifier(): string {
  // Keep the UUID-derived value below the API's 32-character city limit while
  // retaining far more entropy than timestamp/worker/retry combinations.
  return randomUUID().replaceAll('-', '').slice(0, 20).toUpperCase()
}

test('super administrator can log in, create a policy draft, and log out', async ({
  page,
  e2eTargetGuard,
}) => {
  const policyCity = `E2E-City-${uniqueE2EIdentifier()}`

  await signInToE2E(page, e2eTargetGuard, credentials)
  await expect(page.getByTestId('app-shell')).toBeVisible()
  await expect(page.getByTestId('current-username')).toContainText(credentials.username)
  await expect(page.getByTestId('dashboard-page')).toBeVisible()

  e2eTargetGuard.assertPageOrigin(page)
  await page.getByTestId('nav-policies').click()
  await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/policies`)
  e2eTargetGuard.assertPageOrigin(page)
  await expect(page.getByTestId('payroll-policies-page')).toBeVisible()

  await page.getByTestId('policy-create-draft').click()
  await expect(page.getByTestId('policy-form')).toBeVisible()
  e2eTargetGuard.assertPageOrigin(page)
  await page.getByTestId('policy-city').fill(policyCity)
  const createResponse = page.waitForResponse(
    (response) =>
      response.url().includes('/api/payroll-policies') && response.request().method() === 'POST',
  )
  e2eTargetGuard.assertPageOrigin(page)
  await page.getByTestId('policy-save-draft').click()
  expect((await createResponse).status()).toBe(201)
  e2eTargetGuard.assertPageOrigin(page)
  await expect(page.getByText(policyCity, { exact: true })).toBeVisible()

  // globalSetup verifies the dedicated target marker before any writing test.
  // CI destroys that disposable stack, so product API cleanup is intentionally
  // not used and cannot mask a target-safety failure.
  e2eTargetGuard.assertPageOrigin(page)
  await page.getByTestId('logout').click()
  await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/login`)
  e2eTargetGuard.assertPageOrigin(page)
  await expect(page.getByTestId('login-form')).toBeVisible()
})
