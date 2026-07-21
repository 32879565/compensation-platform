import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const salaryApi = vi.hoisted(() => ({ fetchSalaryRecords: vi.fn() }))

vi.mock('../api/salaryRecords', () => salaryApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'preview_admin' } }),
}))

import SalaryHistoryPage from './SalaryHistoryPage'

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <SalaryHistoryPage />
    </QueryClientProvider>,
  )
}

describe('SalaryHistoryPage', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    salaryApi.fetchSalaryRecords.mockResolvedValue({
      items: [
        {
          id: 1,
          period: '2026-06',
          emp_no: null,
          name: '张三',
          store_name: '北京路店',
          source: 'HISTORICAL',
          fields: { 合计工资: '8,888.00', 实发工资: '8,000.00', 个税: '188.00' },
        },
      ],
      total: 68245,
      page: 1,
      page_size: 20,
    })
  })

  afterEach(cleanup)

  it('shows migrated historical salary records instead of the empty employee master', async () => {
    renderPage()

    expect(await screen.findByText('张三')).toBeTruthy()
    expect(screen.getByText('北京路店')).toBeTruthy()
    expect(screen.getByText('8,888.00')).toBeTruthy()
    expect(screen.getByText('共 68,245 条历史记录')).toBeTruthy()
  })

  it('searches by month, employee name and store name', async () => {
    renderPage()
    await screen.findByText('张三')

    fireEvent.change(screen.getByLabelText('月份'), { target: { value: '2026-06' } })
    fireEvent.change(screen.getByPlaceholderText('输入姓名'), { target: { value: '张三' } })
    fireEvent.change(screen.getByPlaceholderText('输入门店'), { target: { value: '北京路店' } })
    fireEvent.click(screen.getByRole('button', { name: /查\s*询/ }))

    await waitFor(() =>
      expect(salaryApi.fetchSalaryRecords).toHaveBeenLastCalledWith({
        name: '张三',
        period: '2026-06',
        store: '北京路店',
        page: 1,
        page_size: 20,
      }),
    )
  })
})
