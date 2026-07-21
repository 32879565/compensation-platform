import {
  getE2ECredentials,
  getE2EReviewerCredentials,
} from '../src/e2eTargetSafety'

import { expect, signInToE2E, test } from './guardedTest'

const adminCredentials = getE2ECredentials()
const reviewerCredentials = getE2EReviewerCredentials()

const period = '2026-05'
const employeeNo = 'E2E001'
const employeeName = 'E2E Payroll Employee'
const storeName = 'E2E Disposable Store'
const expectedGross = '5086.21'

interface CreatedBatch {
  id: number
  status: string
}

interface PayslipPayload {
  period: string
  gross: string
  net: string
  lines: Array<{ code: string; amount: string }>
}

test.describe('disposable payroll lifecycle', () => {
  // The scenario intentionally advances one fixed seeded payroll batch through
  // irreversible business states. Retrying it would hide a partial-run defect.
  test.describe.configure({ retries: 0 })

  test('covers master data, attendance, calculation, review, lock, query, and export', async ({
    page,
    e2eTargetGuard,
  }) => {
    test.setTimeout(90_000)
    await signInToE2E(page, e2eTargetGuard, adminCredentials)

    await page.getByTestId('nav-org').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/org`)
    e2eTargetGuard.assertPageOrigin(page)
    await expect(page.getByText(storeName, { exact: false })).toBeVisible()

    await page.getByTestId('nav-employees').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/employees`)
    e2eTargetGuard.assertPageOrigin(page)
    const employeeRow = page
      .getByRole('table')
      .getByRole('row')
      .filter({ has: page.getByText(employeeNo, { exact: true }) })
    await expect(employeeRow).toHaveCount(1)
    await expect(employeeRow).toContainText(employeeName)
    await expect(employeeRow).toContainText(storeName)

    await page.getByTestId('nav-attendance').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/attendance`)
    e2eTargetGuard.assertPageOrigin(page)
    await page.locator('#attendance-period').fill(period)
    const attendanceRow = page
      .getByRole('table')
      .getByRole('row')
      .filter({ has: page.getByText(employeeNo, { exact: true }) })
    await expect(attendanceRow).toHaveCount(1)
    // Ant Design visually spaces two-CJK-character button labels ("录 入")
    // while the source text remains "录入".
    await attendanceRow.getByRole('button', { name: /录\s*入/ }).click()

    const attendanceDialog = page.getByRole('dialog', {
      name: `录入考勤 · ${employeeName} · ${period}`,
      exact: true,
    })
    await expect(attendanceDialog).toBeVisible()
    e2eTargetGuard.assertPageOrigin(page)
    await attendanceDialog.getByLabel('加班时长(小时)', { exact: true }).fill('2')
    const attendanceSaveResponse = page.waitForResponse(
      (response) =>
        /\/api\/employees\/\d+\/attendance\/2026-05$/.test(new URL(response.url()).pathname) &&
        response.request().method() === 'PUT',
    )
    e2eTargetGuard.assertPageOrigin(page)
    await attendanceDialog.getByRole('button', { name: 'OK', exact: true }).click()
    expect((await attendanceSaveResponse).status()).toBe(200)
    await expect(attendanceDialog).toBeHidden()
    await expect(attendanceRow).toContainText('2.00')

    await page.getByTestId('nav-payroll').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/payroll`)
    e2eTargetGuard.assertPageOrigin(page)
    await page.getByRole('button', { name: '新建批次', exact: true }).click()
    const createDialog = page.getByRole('dialog', { name: '新建薪资批次', exact: true })
    await expect(createDialog).toBeVisible()
    e2eTargetGuard.assertPageOrigin(page)
    await createDialog.getByLabel('薪资月份', { exact: true }).fill(period)
    e2eTargetGuard.assertPageOrigin(page)
    await createDialog.getByLabel('考勤开始日期', { exact: true }).fill('2026-05-01')
    e2eTargetGuard.assertPageOrigin(page)
    await createDialog.getByLabel('考勤结束日期', { exact: true }).fill('2026-05-31')
    const createBatchResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === '/api/batches' &&
        response.request().method() === 'POST',
    )
    e2eTargetGuard.assertPageOrigin(page)
    await createDialog.getByRole('button', { name: 'OK', exact: true }).click()
    const createdResponse = await createBatchResponse
    expect(createdResponse.status()).toBe(201)
    const createdBatch = (await createdResponse.json()) as CreatedBatch
    expect(createdBatch.status).toBe('DRAFT')
    await expect(createDialog).toBeHidden()

    await expect(page.getByRole('button', { name: '执行核算', exact: true })).toBeVisible()
    e2eTargetGuard.assertPageOrigin(page)
    await page.getByRole('button', { name: '执行核算', exact: true }).click()
    const runBatchResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === `/api/batches/${createdBatch.id}/run` &&
        response.request().method() === 'POST',
    )
    e2eTargetGuard.assertPageOrigin(page)
    await page.getByRole('button', { name: 'OK', exact: true }).click()
    const runResponse = await runBatchResponse
    expect(runResponse.status()).toBe(200)
    expect(await runResponse.json()).toEqual({ employees: 1, status: 'PENDING_STORE_CONFIRM' })
    await expect(page.getByText('待门店确认', { exact: true }).first()).toBeVisible()
    const resultRow = page
      .getByRole('table')
      .getByRole('row')
      .filter({ has: page.getByText(employeeNo, { exact: true }) })
    await expect(resultRow).toHaveCount(1)
    await expect(resultRow).toContainText(employeeName)
    await expect(resultRow).toContainText(expectedGross)

    e2eTargetGuard.assertPageOrigin(page)
    await page.getByTestId('logout').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/login`)
    await signInToE2E(page, e2eTargetGuard, reviewerCredentials)
    await page.getByTestId('nav-payroll').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/payroll`)
    await expect(page.getByRole('button', { name: '确认无误', exact: true })).toBeVisible()
    e2eTargetGuard.assertPageOrigin(page)
    await page.getByRole('button', { name: '确认无误', exact: true }).click()
    const confirmResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === `/api/batches/${createdBatch.id}/confirm` &&
        response.request().method() === 'POST',
    )
    e2eTargetGuard.assertPageOrigin(page)
    await page.getByRole('button', { name: 'OK', exact: true }).click()
    const confirmedResponse = await confirmResponse
    expect(confirmedResponse.status()).toBe(200)
    expect(await confirmedResponse.json()).toMatchObject({ batch_status: 'PENDING_HR' })
    await expect(page.getByText('待人事处理', { exact: true }).first()).toBeVisible()

    e2eTargetGuard.assertPageOrigin(page)
    await page.getByTestId('logout').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/login`)
    await signInToE2E(page, e2eTargetGuard, adminCredentials)
    await page.getByTestId('nav-payroll').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/payroll`)
    await expect(page.getByRole('button', { name: '人事最终审核', exact: true })).toBeVisible()
    e2eTargetGuard.assertPageOrigin(page)
    await page.getByRole('button', { name: '人事最终审核', exact: true }).click()
    const approveResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === `/api/batches/${createdBatch.id}/approve` &&
        response.request().method() === 'POST',
    )
    e2eTargetGuard.assertPageOrigin(page)
    await page.getByRole('button', { name: 'OK', exact: true }).click()
    const approvedResponse = await approveResponse
    expect(approvedResponse.status()).toBe(200)
    expect(await approvedResponse.json()).toEqual({ status: 'CONFIRMED' })

    await expect(page.getByRole('button', { name: '锁定批次', exact: true })).toBeVisible()
    e2eTargetGuard.assertPageOrigin(page)
    await page.getByRole('button', { name: '锁定批次', exact: true }).click()
    const lockResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === `/api/batches/${createdBatch.id}/lock` &&
        response.request().method() === 'POST',
    )
    e2eTargetGuard.assertPageOrigin(page)
    await page.getByRole('button', { name: 'OK', exact: true }).click()
    const lockedResponse = await lockResponse
    expect(lockedResponse.status()).toBe(200)
    expect(await lockedResponse.json()).toEqual({ status: 'LOCKED' })
    await expect(page.getByText('已锁定', { exact: true }).first()).toBeVisible()

    await page.getByTestId('nav-export').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/export`)
    e2eTargetGuard.assertPageOrigin(page)
    await page.getByLabel('导出计薪周期', { exact: true }).fill(period)
    const exportResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === '/api/exports/payroll' &&
        response.request().method() === 'GET',
    )
    const exportDownload = page.waitForEvent('download')
    e2eTargetGuard.assertPageOrigin(page)
    await page.getByRole('button', { name: '导出 XLSX', exact: true }).click()
    const [workbookResponse, workbookDownload] = await Promise.all([
      exportResponse,
      exportDownload,
    ])
    expect(workbookResponse.status()).toBe(200)
    expect(workbookResponse.headers()['content-type']).toContain(
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    expect(workbookResponse.headers()['cache-control']).toContain('no-store')
    expect(workbookDownload.suggestedFilename()).toBe(`payroll-${period}.xlsx`)
    expect(await workbookDownload.failure()).toBeNull()

    e2eTargetGuard.assertPageOrigin(page)
    await page.getByTestId('logout').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/login`)
    await signInToE2E(page, e2eTargetGuard, reviewerCredentials)
    const payslipResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === '/api/payslips/me' &&
        response.request().method() === 'GET',
    )
    await page.getByTestId('nav-payslip').click()
    await expect(page).toHaveURL(`${e2eTargetGuard.verifiedOrigin}/payslip`)
    const payslipHttpResponse = await payslipResponse
    expect(payslipHttpResponse.status()).toBe(200)
    const payslip = (await payslipHttpResponse.json()) as PayslipPayload
    expect(payslip.period).toBe(period)
    expect(payslip.gross).toBe(expectedGross)
    expect(payslip.net).toBe(expectedGross)
    expect(payslip.lines.find((line) => line.code === 'OVERTIME')).toMatchObject({
      amount: '86.21',
    })
    e2eTargetGuard.assertPageOrigin(page)
    await expect(page.getByRole('heading', { name: '我的工资单', exact: true })).toBeVisible()
    await expect(page.getByText(`${period} 工资汇总`, { exact: true })).toBeVisible()
    await expect(page.getByText('OVERTIME', { exact: true })).toBeVisible()
    await expect(page.getByText(expectedGross, { exact: true })).toHaveCount(2)
  })
})
