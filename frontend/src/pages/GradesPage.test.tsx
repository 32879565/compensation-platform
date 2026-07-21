import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
  default: ({ open, mode, onApplied }: { open: boolean; mode: string; onApplied: () => void }) => {
    legacyReview.onApplied = onApplied
    return open ? (
      <div role="dialog" aria-label={`旧系统真实数据-${mode}`}>
        <button onClick={onApplied}>模拟应用真实数据</button>
      </div>
    ) : null
  },
}))
vi.mock('../components/LegacyCatalogEvidencePanel', () => ({
  default: ({ mode, onReview }: { mode: string; onReview: () => void }) => (
    <section aria-label={`默认展示旧系统真实数据-${mode}`}>
      <button onClick={onReview}>从真实数据区创建正式职级</button>
    </section>
  ),
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

function pressEscape(dialog: HTMLElement) {
  const wrapper = dialog.closest<HTMLElement>('.ant-modal-wrap')
  if (!wrapper) throw new Error('modal wrapper did not render')
  const event = new KeyboardEvent('keydown', { bubbles: true, key: 'Escape', code: 'Escape' })
  Object.defineProperty(event, 'keyCode', { value: 27 })
  fireEvent(wrapper, event)
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

  it('does not close the create modal with Escape while creation is pending', async () => {
    let resolveCreate: ((value: Record<string, never>) => void) | undefined
    let form: HTMLFormElement | null = null
    let replayedSubmit = false
    gradeApi.createGrade.mockImplementation(() => {
      if (!replayedSubmit) {
        replayedSubmit = true
        if (!form) throw new Error('grade create form was not captured')
        fireEvent.submit(form)
      }
      return new Promise<Record<string, never>>((resolve) => {
        resolveCreate = resolve
      })
    })
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '新增职级' }))
    const dialog = await screen.findByRole('dialog', { name: '新增职级' })
    fireEvent.change(within(dialog).getByLabelText('职级编码'), { target: { value: 'M2' } })
    fireEvent.change(within(dialog).getByLabelText('职级名称'), { target: { value: '高级主管' } })
    fireEvent.change(within(dialog).getByLabelText('级别'), { target: { value: '20' } })
    form = dialog.querySelector('form')
    if (!form) throw new Error('grade create form did not render')
    const submit = within(dialog).getByRole('button', { name: /OK|确\s*定/i })
    fireEvent.click(submit)
    fireEvent.click(submit)

    await waitFor(() => expect(gradeApi.createGrade).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(
        (within(dialog).getByRole('button', { name: /Cancel|取\s*消/i }) as HTMLButtonElement)
          .disabled,
      ).toBe(true),
    )
    pressEscape(dialog)
    expect(screen.getByRole('dialog', { name: '新增职级' })).toBeTruthy()
    expect(screen.queryByRole('dialog', { name: '编辑职级' })).toBeNull()

    if (!resolveCreate) throw new Error('grade creation did not start')
    resolveCreate({})
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '新增职级' })).toBeNull())
  })

  it('shows legacy evidence and opens its review for import-capable writers', async () => {
    auth.permissions = ['grade:write', 'import:run']
    const { queryClient } = renderPage()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    await screen.findByText('门店主管')
    expect(
      screen.getByRole('region', { name: '默认展示旧系统真实数据-grades' }),
    ).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '从真实数据区创建正式职级' }))

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
    expect(
      screen.queryByRole('region', { name: '默认展示旧系统真实数据-grades' }),
    ).toBeNull()
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

  it('does not close the edit modal with Escape while updating is pending', async () => {
    let resolveUpdate: ((value: Record<string, never>) => void) | undefined
    let form: HTMLFormElement | null = null
    let replayedSubmit = false
    gradeApi.updateGrade.mockImplementation(() => {
      if (!replayedSubmit) {
        replayedSubmit = true
        if (!form) throw new Error('grade edit form was not captured')
        fireEvent.submit(form)
      }
      return new Promise<Record<string, never>>((resolve) => {
        resolveUpdate = resolve
      })
    })
    renderPage()

    const row = (await screen.findByText('门店主管')).closest('tr') as HTMLTableRowElement
    fireEvent.click(within(row).getByRole('button', { name: '编辑' }))
    const dialog = await screen.findByRole('dialog', { name: '编辑职级' })
    form = dialog.querySelector('form')
    if (!form) throw new Error('grade edit form did not render')
    const submit = within(dialog).getByRole('button', { name: /OK|确\s*定/i })
    fireEvent.click(submit)
    fireEvent.click(submit)

    await waitFor(() => expect(gradeApi.updateGrade).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(
        (within(dialog).getByRole('button', { name: /Cancel|取\s*消/i }) as HTMLButtonElement)
          .disabled,
      ).toBe(true),
    )
    pressEscape(dialog)
    expect(screen.getByRole('dialog', { name: '编辑职级' })).toBeTruthy()
    expect(screen.queryByRole('dialog', { name: '停用职级' })).toBeNull()

    if (!resolveUpdate) throw new Error('grade update did not start')
    resolveUpdate({})
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑职级' })).toBeNull())
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

  it('uses the refreshed grade state and blocks new bands after concurrent deactivation', async () => {
    gradeApi.fetchGrades
      .mockResolvedValueOnce([activeGrade])
      .mockResolvedValue([{ ...activeGrade, is_active: false, version: 3 }])
    const { queryClient } = renderPage()

    const row = (await screen.findByText('门店主管')).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '查看薪档' }))
    expect(await screen.findByRole('button', { name: '新增薪档' })).toBeTruthy()

    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['grades', 'grade-editor'] })
    })

    await waitFor(() => expect(screen.queryByRole('button', { name: '新增薪档' })).toBeNull())
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

  it('closes stale band context and refreshes grades after a 409 conflict', async () => {
    gradeApi.createSalaryBand.mockRejectedValue({
      response: { status: 409, data: { detail: '职级已停用，请刷新后重试' } },
    })
    renderPage()

    const row = (await screen.findByText('门店主管')).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '查看薪档' }))
    fireEvent.click(await screen.findByRole('button', { name: '新增薪档' }))
    const dialog = await screen.findByRole('dialog', { name: '新增薪档' })
    fireEvent.change(within(dialog).getByLabelText('生效日期'), {
      target: { value: '2026-11-01' },
    })
    fireEvent.change(within(dialog).getByLabelText('最低薪资'), { target: { value: '6000' } })
    fireEvent.change(within(dialog).getByLabelText('中位薪资'), { target: { value: '8000' } })
    fireEvent.change(within(dialog).getByLabelText('最高薪资'), { target: { value: '10000' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /OK|确\s*定/i }))

    await waitFor(() => expect(gradeApi.createSalaryBand).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '新增薪档' })).toBeNull())
    expect(screen.queryByText('M1 薪档')).toBeNull()
    expect(await screen.findByText('职级已停用，请刷新后重试')).toBeTruthy()
    await waitFor(() => expect(gradeApi.fetchGrades.mock.calls.length).toBeGreaterThan(1))
  })

  it('clears salary band amounts and date after cancelling the modal', async () => {
    renderPage()

    const row = (await screen.findByText('门店主管')).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '查看薪档' }))
    fireEvent.click(await screen.findByRole('button', { name: '新增薪档' }))
    const firstDialog = await screen.findByRole('dialog', { name: '新增薪档' })
    fireEvent.change(within(firstDialog).getByLabelText('生效日期'), {
      target: { value: '2026-09-01' },
    })
    fireEvent.change(within(firstDialog).getByLabelText('最低薪资'), { target: { value: '6100' } })
    fireEvent.change(within(firstDialog).getByLabelText('中位薪资'), { target: { value: '8100' } })
    fireEvent.change(within(firstDialog).getByLabelText('最高薪资'), { target: { value: '10100' } })
    fireEvent.click(within(firstDialog).getByRole('button', { name: /Cancel|取\s*消/i }))
    fireEvent.click(screen.getByRole('button', { name: '新增薪档' }))
    const nextDialog = await screen.findByRole('dialog', { name: '新增薪档' })

    ;['生效日期', '最低薪资', '中位薪资', '最高薪资'].forEach((label) => {
      expect((within(nextDialog).getByLabelText(label) as HTMLInputElement).value).toBe('')
    })
    expect(gradeApi.createSalaryBand).not.toHaveBeenCalled()
  })

  it('locks the salary band modal and prevents duplicate finish events while creation is pending', async () => {
    let resolveCreate: ((value: Record<string, never>) => void) | undefined
    gradeApi.createSalaryBand.mockImplementation(
      () =>
        new Promise<Record<string, never>>((resolve) => {
          resolveCreate = resolve
        }),
    )
    renderPage()

    const row = (await screen.findByText('门店主管')).closest('tr')
    fireEvent.click(within(row as HTMLTableRowElement).getByRole('button', { name: '查看薪档' }))
    fireEvent.click(await screen.findByRole('button', { name: '新增薪档' }))
    const dialog = await screen.findByRole('dialog', { name: '新增薪档' })
    fireEvent.change(within(dialog).getByLabelText('生效日期'), {
      target: { value: '2026-10-01' },
    })
    fireEvent.change(within(dialog).getByLabelText('最低薪资'), { target: { value: '6200' } })
    fireEvent.change(within(dialog).getByLabelText('中位薪资'), { target: { value: '8200' } })
    fireEvent.change(within(dialog).getByLabelText('最高薪资'), { target: { value: '10200' } })
    const cancel = within(dialog).getByRole('button', { name: /Cancel|取\s*消/i })
    const submit = within(dialog).getByRole('button', { name: /OK|确\s*定/i })
    fireEvent.click(submit)

    await waitFor(() => expect(gradeApi.createSalaryBand).toHaveBeenCalledTimes(1))
    await waitFor(() => expect((cancel as HTMLButtonElement).disabled).toBe(true))
    expect((submit as HTMLButtonElement).disabled).toBe(true)
    expect(within(dialog).queryByRole('button', { name: /Close|关闭/i })).toBeNull()

    fireEvent.click(cancel)
    const form = dialog.querySelector('form')
    if (!form) throw new Error('salary band form did not render')
    fireEvent.submit(form)
    await new Promise<void>((resolve) => setTimeout(resolve, 0))

    expect(gradeApi.createSalaryBand).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('dialog', { name: '新增薪档' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: '新增薪档' })).toBeNull()

    if (!resolveCreate) throw new Error('salary band mutation did not start')
    resolveCreate({})
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '新增薪档' })).toBeNull())
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

  it('does not close the lifecycle modal with Escape while its mutation is pending', async () => {
    let resolveDeactivate: ((value: Record<string, never>) => void) | undefined
    let form: HTMLFormElement | null = null
    let replayedSubmit = false
    gradeApi.deactivateGrade.mockImplementation(() => {
      if (!replayedSubmit) {
        replayedSubmit = true
        if (!form) throw new Error('grade lifecycle form was not captured')
        fireEvent.submit(form)
      }
      return new Promise<Record<string, never>>((resolve) => {
        resolveDeactivate = resolve
      })
    })
    renderPage()

    const row = (await screen.findByText('门店主管')).closest('tr') as HTMLTableRowElement
    fireEvent.click(within(row).getByRole('button', { name: '停用' }))
    const dialog = await screen.findByRole('dialog', { name: '停用职级' })
    fireEvent.change(within(dialog).getByLabelText('停用原因'), {
      target: { value: '等待服务端确认' },
    })
    form = dialog.querySelector('form')
    if (!form) throw new Error('grade lifecycle form did not render')
    const submit = within(dialog).getByRole('button', { name: /OK|确\s*定|确认/i })
    fireEvent.click(submit)
    fireEvent.click(submit)

    await waitFor(() => expect(gradeApi.deactivateGrade).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(
        (within(dialog).getByRole('button', { name: /Cancel|取\s*消/i }) as HTMLButtonElement)
          .disabled,
      ).toBe(true),
    )
    pressEscape(dialog)
    expect(screen.getByRole('dialog', { name: '停用职级' })).toBeTruthy()
    expect(screen.queryByRole('dialog', { name: '编辑职级' })).toBeNull()

    if (!resolveDeactivate) throw new Error('grade lifecycle mutation did not start')
    resolveDeactivate({})
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '停用职级' })).toBeNull())
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
