import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const importsApi = vi.hoisted(() => ({
  confirmSalaryImport: vi.fn(),
  fetchSalaryImportPublishTargets: vi.fn(),
  fetchSalaryImportRows: vi.fn(),
  publishSalaryImport: vi.fn(),
  uploadSalaryImport: vi.fn(),
}))
const auth = vi.hoisted(() => ({
  permissions: ['import:run', 'payroll:run', 'payroll:read'] as string[],
  globalPermissions: ['import:run', 'payroll:run', 'payroll:read'] as string[],
}))

vi.mock('../api/imports', () => importsApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'importer' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
    hasGlobalPermission: (permission: string) => auth.globalPermissions.includes(permission),
  }),
}))

import ImportsPage from './ImportsPage'

const publishTargets = [
  {
    store_id: 101,
    store_name: '一店',
    employee_count: 1,
    departments: ['DINING'],
  },
  {
    store_id: 202,
    store_name: '二店',
    employee_count: 1,
    departments: ['KITCHEN'],
  },
  {
    store_id: 303,
    store_name: '三店',
    employee_count: 1,
    departments: ['DINING', 'KITCHEN'],
  },
]

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={queryClient}>
        <ImportsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

async function uploadWorkbook() {
  fireEvent.change(screen.getByLabelText('薪资月份'), { target: { value: '2026-07' } })
  const file = new File(['workbook'], '七月薪资.xlsx')
  fireEvent.change(screen.getByLabelText('选择薪酬导入文件'), { target: { files: [file] } })
  fireEvent.click(screen.getByRole('button', { name: '上传并校验' }))
  await waitFor(() => expect(importsApi.uploadSalaryImport).toHaveBeenCalledWith('2026-07', file))
}

describe('ImportsPage salary workbook workflow', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.permissions = ['import:run', 'payroll:run', 'payroll:read']
    auth.globalPermissions = ['import:run', 'payroll:run', 'payroll:read']
    importsApi.confirmSalaryImport.mockResolvedValue({ written: 2 })
    importsApi.fetchSalaryImportPublishTargets.mockResolvedValue(publishTargets)
    importsApi.publishSalaryImport.mockResolvedValue({
      import_batch_id: 8,
      payroll_batch_id: 19,
      batch_version: 3,
      employees: 2,
      scopes: 2,
      routed: 2,
      configuration_failures: 0,
      existing: 0,
      sandbox: true,
    })
  })

  afterEach(cleanup)

  it('shows staging errors and blocks confirmation until the workbook is corrected', async () => {
    importsApi.uploadSalaryImport.mockResolvedValue({
      id: 8,
      filename: '七月薪资.xlsx',
      period: '2026-07',
      status: 'PARSED',
      total_rows: 2,
      error_rows: 1,
    })
    importsApi.fetchSalaryImportRows.mockResolvedValue([
      {
        row_index: 0,
        period: '2026-07',
        emp_no: 'E001',
        name: '林月',
        store_name: '一店',
        parsed_fields: { 应发工资: '5200.00' },
        errors: [],
        status: 'OK',
      },
      {
        row_index: 1,
        period: '2026-07',
        emp_no: null,
        name: '周青',
        store_name: '二店',
        parsed_fields: {},
        errors: ['缺少工号'],
        status: 'ERROR',
      },
    ])
    renderPage()

    expect(screen.getByRole('link', { name: '下载薪资导入模板' }).getAttribute('href')).toBe(
      '/payroll-import-template.xlsx',
    )
    await uploadWorkbook()

    expect(await screen.findByText('缺少工号')).toBeTruthy()
    expect(await screen.findByText('存在 1 行错误，请修正文件后重新上传')).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: '确认写入薪资记录' }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(importsApi.confirmSalaryImport).not.toHaveBeenCalled()
    expect((screen.getByRole('button', { name: '上传并校验' }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    fireEvent.click(screen.getByRole('button', { name: '上传并校验' }))
    expect(importsApi.uploadSalaryImport).toHaveBeenCalledTimes(1)
  })

  it('fails closed when the staging preview does not contain the full batch', async () => {
    importsApi.uploadSalaryImport.mockResolvedValue({
      id: 8,
      filename: '七月薪资.xlsx',
      period: '2026-07',
      status: 'PARSED',
      total_rows: 2,
      error_rows: 0,
    })
    importsApi.fetchSalaryImportRows.mockResolvedValue([
      {
        row_index: 0,
        period: '2026-07',
        emp_no: 'E001',
        name: '林月',
        store_name: '一店',
        parsed_fields: { 应发工资: '5200.00' },
        errors: [],
        status: 'OK',
      },
    ])
    renderPage()

    await uploadWorkbook()

    expect(await screen.findByText('暂存数据读取不完整，已禁止确认')).toBeTruthy()
    expect(screen.getByText('批次应有 2 行，当前只读取到 1 行。')).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: '确认写入薪资记录' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('loads unselected stores after confirmation and locks the checked stores after publishing', async () => {
    importsApi.uploadSalaryImport.mockResolvedValue({
      id: 8,
      filename: '七月薪资.xlsx',
      period: '2026-07',
      status: 'PARSED',
      total_rows: 3,
      error_rows: 0,
    })
    importsApi.fetchSalaryImportRows.mockResolvedValue([
      {
        row_index: 0,
        period: '2026-07',
        emp_no: 'E001',
        name: '林月',
        store_name: '一店',
        parsed_fields: { 应发工资: '5200.00' },
        errors: [],
        status: 'OK',
      },
      {
        row_index: 1,
        period: '2026-07',
        emp_no: 'E002',
        name: '周青',
        store_name: '二店',
        parsed_fields: { 应发工资: '4800.00' },
        errors: [],
        status: 'OK',
      },
      {
        row_index: 2,
        period: '2026-07',
        emp_no: 'E003',
        name: '陈晓',
        store_name: '三店',
        parsed_fields: { 应发工资: '5000.00' },
        errors: [],
        status: 'OK',
      },
    ])
    importsApi.confirmSalaryImport.mockResolvedValue({ written: 3 })
    importsApi.fetchSalaryImportPublishTargets.mockResolvedValueOnce(publishTargets)
    importsApi.publishSalaryImport.mockResolvedValueOnce({
      import_batch_id: 8,
      payroll_batch_id: 19,
      batch_version: 3,
      employees: 2,
      scopes: 2,
      routed: 2,
      configuration_failures: 0,
      existing: 0,
      sandbox: true,
    })
    renderPage()

    await uploadWorkbook()
    expect(await screen.findByText('5200.00')).toBeTruthy()
    expect(importsApi.fetchSalaryImportPublishTargets).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认写入薪资记录' }))

    await waitFor(() => expect(importsApi.confirmSalaryImport).toHaveBeenCalledWith(8))
    expect(await screen.findByText('已确认写入 3 条薪资记录')).toBeTruthy()
    await waitFor(() => expect(importsApi.fetchSalaryImportPublishTargets).toHaveBeenCalledWith(8))

    const firstStore = await screen.findByRole('checkbox', { name: /一店/ })
    const secondStore = screen.getByRole('checkbox', { name: /二店/ })
    const thirdStore = screen.getByRole('checkbox', { name: /三店/ })
    expect((firstStore as HTMLInputElement).checked).toBe(false)
    expect((secondStore as HTMLInputElement).checked).toBe(false)
    expect((thirdStore as HTMLInputElement).checked).toBe(false)

    const publish = screen.getByRole('button', { name: '推送给店长和厨房经理' })
    expect((publish as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(publish)
    expect(importsApi.publishSalaryImport).not.toHaveBeenCalled()

    fireEvent.click(firstStore)
    fireEvent.click(secondStore)
    expect((publish as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(publish)

    expect(importsApi.publishSalaryImport).not.toHaveBeenCalled()
    expect(await screen.findByText('确认推送薪资复核？')).toBeTruthy()
    expect(screen.getByText(/一店、二店/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '确认推送' }))

    await waitFor(() =>
      expect(importsApi.publishSalaryImport).toHaveBeenNthCalledWith(1, 8, [101, 202]),
    )
    expect(await screen.findByText('复核任务已生成（沙箱模式未实际发送）')).toBeTruthy()
    expect(screen.getByText(/薪资批次 #19.*版本 3.*员工 2 人.*复核范围 2 个/)).toBeTruthy()
    expect(screen.getByText(/通知 2 人.*配置失败 0 人.*沙箱模式/)).toBeTruthy()
    expect(screen.getByRole('link', { name: '查看薪资批次' }).getAttribute('href')).toBe('/payroll')
    expect(importsApi.fetchSalaryImportPublishTargets).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('checkbox', { name: /一店/ })).toBeNull()
    expect(screen.queryByRole('checkbox', { name: /二店/ })).toBeNull()
    expect(screen.queryByRole('checkbox', { name: /三店/ })).toBeNull()
    expect(screen.queryByRole('button', { name: '推送给店长和厨房经理' })).toBeNull()
  })

  it('keeps an idempotent retry action when reviewer notification configuration fails', async () => {
    importsApi.uploadSalaryImport.mockResolvedValue({
      id: 8,
      filename: '七月薪资.xlsx',
      period: '2026-07',
      status: 'PARSED',
      total_rows: 1,
      error_rows: 0,
    })
    importsApi.fetchSalaryImportRows.mockResolvedValue([
      {
        row_index: 0,
        period: '2026-07',
        emp_no: 'E001',
        name: '林月',
        store_name: '一店',
        parsed_fields: { 应发工资: '5200.00' },
        errors: [],
        status: 'OK',
      },
    ])
    importsApi.fetchSalaryImportPublishTargets.mockResolvedValue([publishTargets[0]])
    importsApi.publishSalaryImport
      .mockResolvedValueOnce({
        import_batch_id: 8,
        payroll_batch_id: 19,
        batch_version: 3,
        employees: 1,
        scopes: 1,
        routed: 0,
        configuration_failures: 1,
        existing: 0,
        already_published: false,
        sandbox: true,
      })
      .mockResolvedValueOnce({
        import_batch_id: 8,
        payroll_batch_id: 19,
        batch_version: 3,
        employees: 1,
        scopes: 1,
        routed: 0,
        configuration_failures: 1,
        existing: 1,
        already_published: true,
        sandbox: true,
      })
      .mockResolvedValueOnce({
        import_batch_id: 8,
        payroll_batch_id: 19,
        batch_version: 3,
        employees: 1,
        scopes: 1,
        routed: 1,
        configuration_failures: 0,
        existing: 1,
        already_published: true,
        sandbox: true,
      })
    renderPage()

    await uploadWorkbook()
    fireEvent.click(await screen.findByRole('button', { name: '确认写入薪资记录' }))
    fireEvent.click(await screen.findByRole('checkbox', { name: /一店/ }))
    fireEvent.click(await screen.findByRole('button', { name: '推送给店长和厨房经理' }))

    expect(await screen.findByText('复核任务已生成，部分通知配置失败')).toBeTruthy()
    expect(importsApi.publishSalaryImport).toHaveBeenNthCalledWith(1, 8, [101])
    expect(screen.queryByRole('checkbox', { name: /一店/ })).toBeNull()
    fireEvent.click(await screen.findByRole('button', { name: '修复配置后重试推送' }))

    await waitFor(() => expect(importsApi.publishSalaryImport).toHaveBeenCalledTimes(2))
    expect(importsApi.publishSalaryImport).toHaveBeenNthCalledWith(2, 8, [101])
    expect(await screen.findByText('复核任务已生成，部分通知配置失败')).toBeTruthy()
    const retry = screen.getByRole('button', { name: '修复配置后重试推送' })
    expect(retry).toBeTruthy()
    fireEvent.click(retry)

    await waitFor(() => expect(importsApi.publishSalaryImport).toHaveBeenCalledTimes(3))
    expect(importsApi.publishSalaryImport).toHaveBeenNthCalledWith(3, 8, [101])
    expect(await screen.findByText('该导入批次此前已发布，本次未重复创建薪资批次')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '修复配置后重试推送' })).toBeNull()
  })

  it('does not expose the push action without payroll run permission', async () => {
    auth.permissions = ['import:run']
    auth.globalPermissions = ['import:run']
    importsApi.uploadSalaryImport.mockResolvedValue({
      id: 8,
      filename: '七月薪资.xlsx',
      period: '2026-07',
      status: 'PARSED',
      total_rows: 1,
      error_rows: 0,
    })
    importsApi.fetchSalaryImportRows.mockResolvedValue([
      {
        row_index: 0,
        period: '2026-07',
        emp_no: 'E001',
        name: '林月',
        store_name: '一店',
        parsed_fields: { 应发工资: '5200.00' },
        errors: [],
        status: 'OK',
      },
    ])
    renderPage()

    await uploadWorkbook()
    fireEvent.click(await screen.findByRole('button', { name: '确认写入薪资记录' }))
    expect(await screen.findByText('已确认写入 2 条薪资记录')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '推送给店长和厨房经理' })).toBeNull()
    expect(screen.getByText('需要薪资核算权限才能推送复核任务')).toBeTruthy()
  })

  it('does not expose the push action when payroll run is only locally scoped', async () => {
    auth.permissions = ['import:run', 'payroll:run']
    auth.globalPermissions = ['import:run']
    importsApi.uploadSalaryImport.mockResolvedValue({
      id: 8,
      filename: '七月薪资.xlsx',
      period: '2026-07',
      status: 'PARSED',
      total_rows: 1,
      error_rows: 0,
    })
    importsApi.fetchSalaryImportRows.mockResolvedValue([
      {
        row_index: 0,
        period: '2026-07',
        emp_no: 'E001',
        name: '林月',
        store_name: '一店',
        parsed_fields: { 应发工资: '5200.00' },
        errors: [],
        status: 'OK',
      },
    ])
    renderPage()

    await uploadWorkbook()
    fireEvent.click(await screen.findByRole('button', { name: '确认写入薪资记录' }))
    expect(await screen.findByText('已确认写入 2 条薪资记录')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '推送给店长和厨房经理' })).toBeNull()
  })

  it('recovers when confirmation succeeded but its first response was lost', async () => {
    importsApi.uploadSalaryImport.mockResolvedValue({
      id: 8,
      filename: '七月薪资.xlsx',
      period: '2026-07',
      status: 'PARSED',
      total_rows: 1,
      error_rows: 0,
    })
    importsApi.fetchSalaryImportRows.mockResolvedValue([
      {
        row_index: 0,
        period: '2026-07',
        emp_no: 'E001',
        name: '林月',
        store_name: '一店',
        parsed_fields: { 应发工资: '5200.00' },
        errors: [],
        status: 'OK',
      },
    ])
    importsApi.confirmSalaryImport
      .mockRejectedValueOnce(new Error('确认响应丢失'))
      .mockRejectedValueOnce({ response: { status: 409, data: { detail: '批次已确认' } } })
    renderPage()

    await uploadWorkbook()
    const confirm = await screen.findByRole('button', { name: '确认写入薪资记录' })
    fireEvent.click(confirm)
    expect(await screen.findByText('确认响应丢失')).toBeTruthy()
    await waitFor(() => expect((confirm as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(confirm)

    expect(await screen.findByText('服务端已确认该批次，已恢复 1 条记录的后续流程')).toBeTruthy()
    expect(screen.getByRole('button', { name: '推送给店长和厨房经理' })).toBeTruthy()
    expect(importsApi.confirmSalaryImport).toHaveBeenCalledTimes(2)
  })
})
