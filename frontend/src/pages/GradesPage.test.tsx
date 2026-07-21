import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const gradeApi = vi.hoisted(() => ({
  fetchGrades: vi.fn(),
  createGrade: vi.fn(),
  updateGrade: vi.fn(),
  deactivateGrade: vi.fn(),
  restoreGrade: vi.fn(),
  fetchGradeBands: vi.fn(),
  createSalaryBand: vi.fn(),
}))
const auth = vi.hoisted(() => ({ permissions: ['grade:write'] as string[] }))
const legacyReview = vi.hoisted(() => ({ onApplied: null as (() => void) | null }))

vi.mock('../api/masterdata', () => gradeApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'grade-editor' },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))
vi.mock('../components/LegacyCatalogReviewDrawer', () => ({
  default: ({
    open,
    mode,
    onApplied,
  }: {
    open: boolean
    mode: string
    onApplied: () => void
  }) => {
    legacyReview.onApplied = onApplied
    return open ? (
      <div role="dialog" aria-label={`旧系统真实数据-${mode}`}>
        <button onClick={onApplied}>模拟应用真实数据</button>
      </div>
    ) : null
  },
}))

import GradesPage from './GradesPage'

const activeGrade = {
  id: 1,
  code: 'M1',
  name: '门店主管',
  rank: 10,
  version: 2,
  is_active: true,
  deactivated_at: null,
}

const inactiveGrade = {
  id: 2,
  code: 'M0',
  name: '旧门店主管',
  rank: 5,
  version: 4,
  is_active: false,
  deactivated_at: '2026-07-20T05:00:00Z',
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <GradesPage />
    </QueryClientProvider>,
  )
  return { ...rendered, queryClient }
}

async function submitLifecycleAction(row: HTMLElement, name: '停用' | '恢复', reason: string) {
  fireEvent.click(within(row).getByRole('button', { name }))
  const dialog = await screen.findByRole('dialog', { name: `${name}职级` })
  fireEvent.change(within(dialog).getByLabelText(`${name}原因`), { target: { value: reason } })
  fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定|确认/i }))
}

describe('GradesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.permissions = ['grade:write']
    gradeApi.fetchGrades.mockImplementation(
      ({ status }: { status: 'active' | 'inactive' | 'all' } = { status: 'active' }) =>
        Promise.resolve(
          status === 'inactive'
            ? [inactiveGrade]
            : status === 'all'
              ? [activeGrade, inactiveGrade]
              : [activeGrade],
        ),
    )
    gradeApi.fetchGradeBands.mockResolvedValue([
      {
        id: 11,
        job_grade_id: 1,
        band_min: '5000.00',
        band_mid: '7000.00',
        band_max: '9000.00',
        effective_from: '2026-07-01',
        effective_to: null,
      },
    ])
    gradeApi.createGrade.mockResolvedValue({})
    gradeApi.updateGrade.mockResolvedValue({})
    gradeApi.deactivateGrade.mockResolvedValue({})
    gradeApi.restoreGrade.mockResolvedValue({})
    gradeApi.createSalaryBand.mockResolvedValue({})
  })

  afterEach(cleanup)

  it('fails closed when grades cannot be loaded', async () => {
    gradeApi.fetchGrades.mockRejectedValue({
      response: { data: { detail: '职级服务暂不可用' } },
    })

    renderPage()

    expect(await screen.findByText('职级体系加载失败')).toBeTruthy()
    expect(screen.getByText('职级服务暂不可用')).toBeTruthy()
    const create = screen.getByRole('button', { name: '新增职级' })
    expect((create as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(create)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(gradeApi.createGrade).not.toHaveBeenCalled()
  })

  it('creates a grade through the write contract', async () => {
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '新增职级' }))
    const dialog = await screen.findByRole('dialog', { name: '新增职级' })
    fireEvent.change(within(dialog).getByLabelText('职级编码'), { target: { value: 'M2' } })
    fireEvent.change(within(dialog).getByLabelText('职级名称'), {
      target: { value: '高级门店主管' },
    })
    fireEvent.change(within(dialog).getByLabelText('级别'), { target: { value: '20' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(gradeApi.createGrade).toHaveBeenCalledWith({
        code: 'M2',
        name: '高级门店主管',
        rank: 20,
      }),
    )
  })

  it('opens reviewed legacy grades only for import-capable writers and refreshes after apply', async () => {
    auth.permissions = ['grade:write', 'import:run']
    const { queryClient } = renderPage()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    await screen.findByText('门店主管')
    fireEvent.click(screen.getByRole('button', { name: '审阅旧系统真实数据' }))

    expect(screen.getByRole('dialog', { name: '旧系统真实数据-grades' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '模拟应用真实数据' }))
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['grades', 'grade-editor'] }),
    )
    expect(screen.queryByRole('dialog', { name: '旧系统真实数据-grades' })).toBeNull()
  })

  it('does not expose legacy grade creation to import-only users', async () => {
    auth.permissions = ['import:run']
    renderPage()

    await screen.findByText('门店主管')
    expect(screen.queryByRole('button', { name: '审阅旧系统真实数据' })).toBeNull()
  })

  it('edits a grade with its current version', async () => {
    renderPage()

    const row = (await screen.findByText('门店主管')).closest('tr')
    expect(row).not.toBeNull()
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '编辑' }))
    const dialog = await screen.findByRole('dialog', { name: '编辑职级' })
    fireEvent.change(within(dialog).getByLabelText('职级名称'), {
      target: { value: '门店高级主管' },
    })
    fireEvent.change(within(dialog).getByLabelText('级别'), { target: { value: '12' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(gradeApi.updateGrade).toHaveBeenCalledWith(1, {
        name: '门店高级主管',
        rank: 12,
        expected_version: 2,
      }),
    )
  })

  it('loads salary bands only on demand and renders the min-mid-max salary rail', async () => {
    renderPage()

    expect(gradeApi.fetchGradeBands).not.toHaveBeenCalled()
    const row = (await screen.findByText('门店主管')).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '查看薪档' }))

    await waitFor(() => expect(gradeApi.fetchGradeBands).toHaveBeenCalledWith(1))
    expect(await screen.findByText('薪档轨道')).toBeTruthy()
    expect(screen.getByText('最低')).toBeTruthy()
    expect(screen.getByText('中位')).toBeTruthy()
    expect(screen.getByText('最高')).toBeTruthy()
    expect(screen.getAllByText(/5,?000\.00/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/7,?000\.00/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/9,?000\.00/).length).toBeGreaterThan(0)
  })

  it('adds an effective-dated salary band from the selected grade', async () => {
    renderPage()

    const row = (await screen.findByText('门店主管')).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '查看薪档' }))
    fireEvent.click(await screen.findByRole('button', { name: '新增薪档' }))
    const dialog = await screen.findByRole('dialog', { name: '新增薪档' })
    fireEvent.change(within(dialog).getByLabelText('生效日期'), {
      target: { value: '2026-08-01' },
    })
    fireEvent.change(within(dialog).getByLabelText('最低薪资'), { target: { value: '6000' } })
    fireEvent.change(within(dialog).getByLabelText('中位薪资'), { target: { value: '8000' } })
    fireEvent.change(within(dialog).getByLabelText('最高薪资'), { target: { value: '10000' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() =>
      expect(gradeApi.createSalaryBand).toHaveBeenCalledWith(1, {
        effective_from: '2026-08-01',
        band_min: '6000.00',
        band_mid: '8000.00',
        band_max: '10000.00',
      }),
    )
  })

  it('deactivates and restores grades with optimistic concurrency', async () => {
    renderPage()

    const activeRow = (await screen.findByText('门店主管')).closest('tr')
    await submitLifecycleAction(activeRow as HTMLTableRowElement, '停用', '旧职级停止新分配')
    await waitFor(() =>
      expect(gradeApi.deactivateGrade).toHaveBeenCalledWith(1, {
        reason: '旧职级停止新分配',
        expected_version: 2,
      }),
    )

    fireEvent.mouseDown(screen.getByLabelText('职级状态'))
    fireEvent.click(await screen.findByText('已停用'))
    await waitFor(() =>
      expect(gradeApi.fetchGrades).toHaveBeenLastCalledWith({ status: 'inactive' }),
    )
    const inactiveRow = (await screen.findByText('旧门店主管')).closest('tr')
    await submitLifecycleAction(inactiveRow as HTMLTableRowElement, '恢复', '经人事确认重新启用')
    await waitFor(() =>
      expect(gradeApi.restoreGrade).toHaveBeenCalledWith(2, {
        reason: '经人事确认重新启用',
        expected_version: 4,
      }),
    )
  })

  it('keeps maintenance actions away from read-only users while retaining band visibility', async () => {
    auth.permissions = []
    renderPage()

    const row = (await screen.findByText('门店主管')).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '查看薪档' }))
    await waitFor(() => expect(gradeApi.fetchGradeBands).toHaveBeenCalledWith(1))
    expect(screen.queryByRole('button', { name: '新增职级' })).toBeNull()
    expect(screen.queryByRole('button', { name: '编辑' })).toBeNull()
    expect(screen.queryByRole('button', { name: '停用' })).toBeNull()
    expect(screen.queryByRole('button', { name: '恢复' })).toBeNull()
    expect(await screen.findByText('薪档轨道')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '新增薪档' })).toBeNull()
  })
})
