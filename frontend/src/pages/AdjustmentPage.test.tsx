import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const approvalApi = vi.hoisted(() => ({
  createSalaryAdjustment: vi.fn(),
  decideApprovalInstance: vi.fn(),
  fetchApprovalInstance: vi.fn(),
  fetchApprovalTodos: vi.fn(),
  fetchSalaryAdjustment: vi.fn(),
  fetchSalaryAdjustments: vi.fn(),
  submitSalaryAdjustment: vi.fn(),
}))
const compApi = vi.hoisted(() => ({ fetchComponents: vi.fn() }))
const masterdataApi = vi.hoisted(() => ({ fetchEmployees: vi.fn() }))
const auth = vi.hoisted(() => ({ permissions: ['adjustment:create'] as string[] }))

vi.mock('../api/approval', () => approvalApi)
vi.mock('../api/comp', () => compApi)
vi.mock('../api/masterdata', () => masterdataApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'store-manager' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))

import AdjustmentPage from './AdjustmentPage'

function renderPage(seed?: (queryClient: QueryClient) => void) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  seed?.(queryClient)
  return render(
    <QueryClientProvider client={queryClient}>
      <AdjustmentPage />
    </QueryClientProvider>,
  )
}

describe('AdjustmentPage draft recovery', () => {
  afterEach(() => {
    cleanup()
  })

  beforeEach(() => {
    sessionStorage.clear()
    vi.clearAllMocks()
    auth.permissions = ['adjustment:create']
    approvalApi.fetchSalaryAdjustment.mockResolvedValue({ id: 42, status: 'DRAFT' })
    approvalApi.fetchApprovalTodos.mockResolvedValue([])
    approvalApi.fetchApprovalInstance.mockResolvedValue({})
    approvalApi.fetchSalaryAdjustments.mockResolvedValue([])
    approvalApi.submitSalaryAdjustment.mockRejectedValue({
      response: { data: { detail: 'No matching approval flow' } },
    })
    compApi.fetchComponents.mockResolvedValue([])
    masterdataApi.fetchEmployees.mockResolvedValue({ items: [] })
  })

  it('keeps a recovered draft available for retry after submission fails', async () => {
    sessionStorage.setItem('salary-adjustment-pending-draft:store-manager', '42')

    renderPage()

    expect(await screen.findByText('调薪草稿 #42 尚未提交审批')).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: '发起调薪申请' }) as HTMLButtonElement).disabled,
    ).toBe(true)
    const retry = screen.getByRole('button', { name: '重新提交' })
    await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(retry)

    await waitFor(() => expect(approvalApi.submitSalaryAdjustment.mock.calls[0]?.[0]).toBe(42))
    expect(screen.getByText('调薪草稿 #42 尚未提交审批')).toBeTruthy()
  })

  it('clears the recovered draft and re-enables creation after submission succeeds', async () => {
    sessionStorage.setItem('salary-adjustment-pending-draft:store-manager', '42')
    approvalApi.submitSalaryAdjustment.mockResolvedValue({ id: 42, status: 'PENDING' })

    renderPage()

    await screen.findByText('调薪草稿 #42 尚未提交审批')
    const retry = screen.getByRole('button', { name: '重新提交' })
    await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(retry)

    await waitFor(() => expect(screen.queryByText('调薪草稿 #42 尚未提交审批')).toBeNull())
    expect(
      (screen.getByRole('button', { name: '发起调薪申请' }) as HTMLButtonElement).disabled,
    ).toBe(false)
    expect(sessionStorage.getItem('salary-adjustment-pending-draft:store-manager')).toBeNull()
  })

  it.each([
    ['员工目录', 'employees'],
    ['薪资组件', 'components'],
  ] as const)('fails closed when the %s cannot be read', async (label, source) => {
    if (source === 'employees') {
      masterdataApi.fetchEmployees.mockRejectedValue(new Error('employee read failed'))
    } else {
      compApi.fetchComponents.mockRejectedValue(new Error('component read failed'))
    }

    renderPage()

    expect(await screen.findByText(`无法加载${label}，已停用调薪申请创建。`)).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: '发起调薪申请' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('disables stale approval actions when the todo refresh fails', async () => {
    auth.permissions = ['adjustment:approve']
    approvalApi.fetchApprovalTodos.mockRejectedValue(new Error('todo read failed'))
    const staleTodo = {
      id: 81,
      business_type: 'SALARY_ADJUSTMENT',
      business_id: 42,
      org_unit_id: 3,
      amount: '300.00',
      requester_id: 9,
      current_step_order: 1,
      current_step_name: '店长审批',
    }

    renderPage((queryClient) => {
      queryClient.setQueryData(['approvalTodos', 'store-manager'], [staleTodo])
    })

    expect(await screen.findByText('无法加载审批待办')).toBeTruthy()
    expect((screen.getByRole('button', { name: /审\s*批/ }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    expect((screen.getByRole('button', { name: /轨\s*迹/ }) as HTMLButtonElement).disabled).toBe(
      true,
    )
  })

  it('lets the user clear a locally recovered draft only after a verified 404', async () => {
    sessionStorage.setItem('salary-adjustment-pending-draft:store-manager', '404')
    approvalApi.fetchSalaryAdjustment.mockRejectedValue({ response: { status: 404 } })

    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '清除失效记录' }))

    await waitFor(() => expect(screen.queryByText('调薪草稿 #404 尚未提交审批')).toBeNull())
    expect(sessionStorage.getItem('salary-adjustment-pending-draft:store-manager')).toBeNull()
  })

  it('rechecks a recovered draft after a lost submit response', async () => {
    sessionStorage.setItem('salary-adjustment-pending-draft:store-manager', '42')
    approvalApi.fetchSalaryAdjustment
      .mockResolvedValueOnce({ id: 42, status: 'DRAFT' })
      .mockResolvedValueOnce({ id: 42, status: 'PENDING' })

    renderPage()

    const retry = await screen.findByRole('button', { name: '重新提交' })
    await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(retry)

    await waitFor(() => expect(screen.queryByText('调薪草稿 #42 尚未提交审批')).toBeNull())
    expect(approvalApi.fetchSalaryAdjustment).toHaveBeenCalledTimes(2)
  })
})
