import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const dingtalkApi = vi.hoisted(() => ({
  createCompAppeal: vi.fn(),
  fetchCompAppeals: vi.fn(),
  fetchDingTalkDeliveries: vi.fn(),
  fetchDingTalkIntegration: vi.fn(),
  fetchDingTalkMode: vi.fn(),
  retryDingTalkDelivery: vi.fn(),
  stageReviewDeliveries: vi.fn(),
  testDingTalkIntegration: vi.fn(),
}))
const auth = vi.hoisted(() => ({
  permissions: [] as string[],
  username: 'dining-manager',
}))

vi.mock('../api/dingtalk', () => dingtalkApi)
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: { username: auth.username, permissions: auth.permissions },
    hasPermission: (permission: string) => auth.permissions.includes(permission),
  }),
}))

import CompAppealsPage from './CompAppealsPage'

const reviewDelivery = {
  id: 7,
  batch_id: 42,
  batch_version: 3,
  org_unit_id: 8,
  department: 'DINING',
  kind: 'PAYROLL_REVIEW',
  status: 'SANDBOXED',
  can_appeal: true,
  error_code: null,
  attempt_count: 1,
  dispatched_at: '2026-07-20T12:00:00+00:00',
}

const appeal = {
  id: 5,
  delivery_id: 7,
  batch_id: 42,
  batch_version: 3,
  org_unit_id: 8,
  department: 'DINING',
  status: 'PENDING',
  approval_instance_id: 31,
  created_at: '2026-07-20T12:00:00+00:00',
}

function renderPage(
  queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  }),
) {
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <CompAppealsPage />
    </QueryClientProvider>,
  )
  return { ...rendered, queryClient }
}

describe('CompAppealsPage', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    auth.permissions = ['payroll:review']
    dingtalkApi.fetchDingTalkDeliveries.mockResolvedValue([reviewDelivery])
    dingtalkApi.fetchCompAppeals.mockResolvedValue([appeal])
    dingtalkApi.fetchDingTalkIntegration.mockResolvedValue({
      mode: 'sandbox',
      credentials_configured: false,
      app_id_configured: false,
      public_base_url_configured: false,
      ready_for_live: false,
    })
    dingtalkApi.fetchDingTalkMode.mockResolvedValue({ mode: 'sandbox' })
    dingtalkApi.testDingTalkIntegration.mockResolvedValue({
      connected: true,
      token_expires_in_seconds: 7080,
    })
    dingtalkApi.createCompAppeal.mockResolvedValue(appeal)
    dingtalkApi.stageReviewDeliveries.mockResolvedValue({
      routed: 1,
      configuration_failures: 0,
      existing: 0,
      sandbox: true,
    })
    dingtalkApi.retryDingTalkDelivery.mockResolvedValue(reviewDelivery)
  })

  afterEach(() => {
    cleanup()
  })

  it('lets a payroll reviewer appeal a delivered review scope without exposing sensitive fields', async () => {
    renderPage()

    expect(await screen.findByText('沙盒通知：不会真实发送。')).toBeTruthy()
    expect(dingtalkApi.fetchDingTalkMode).toHaveBeenCalledTimes(1)
    expect(dingtalkApi.fetchDingTalkIntegration).not.toHaveBeenCalled()
    expect(await screen.findByText('投递 #7')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '手工分发沙盒通知' })).toBeNull()
    expect(screen.queryByRole('button', { name: '重试投递 7' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '发起申诉' }))
    fireEvent.change(screen.getByLabelText('申诉说明'), {
      target: { value: '请核验考勤来源。' },
    })
    fireEvent.click(screen.getByRole('button', { name: '提交申诉' }))

    await waitFor(() =>
      expect(dingtalkApi.createCompAppeal).toHaveBeenCalledWith({
        delivery_id: 7,
        reason: '请核验考勤来源。',
      }),
    )
  })

  it('only displays sandbox distribution and retry controls to notification managers', async () => {
    auth.permissions = ['notification:manage']

    renderPage()

    expect(await screen.findByText('投递 #7')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('批次 ID'), { target: { value: '42' } })
    expect(await screen.findByRole('button', { name: '重试投递 7' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '手工分发沙盒通知' }))

    await waitFor(() => expect(dingtalkApi.stageReviewDeliveries).toHaveBeenCalledWith(42))
    expect(dingtalkApi.fetchCompAppeals).not.toHaveBeenCalled()
    expect(dingtalkApi.fetchDingTalkIntegration).toHaveBeenCalledTimes(1)
    expect(dingtalkApi.fetchDingTalkMode).not.toHaveBeenCalled()
  })

  it('does not describe an unresolved reviewer mode as sandbox', async () => {
    let resolveMode: ((value: { mode: 'live' }) => void) | undefined
    dingtalkApi.fetchDingTalkMode.mockImplementation(
      () =>
        new Promise<{ mode: 'live' }>((resolve) => {
          resolveMode = resolve
        }),
    )

    renderPage()

    expect(screen.getByText('正在确认通知运行模式。')).toBeTruthy()
    expect(screen.queryByText('沙盒通知：不会真实发送。')).toBeNull()
    if (!resolveMode) throw new Error('mode request did not start')
    resolveMode({ mode: 'live' })

    expect(await screen.findByText('真实推送模式已开启。')).toBeTruthy()
  })

  it('disables notification actions when a cached mode refresh fails', async () => {
    auth.permissions = ['notification:manage']
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
    })
    queryClient.setQueryData(['dingtalkIntegration', auth.username], {
      mode: 'sandbox',
      credentials_configured: false,
      app_id_configured: false,
      public_base_url_configured: false,
      ready_for_live: false,
    })
    dingtalkApi.fetchDingTalkIntegration.mockRejectedValue({
      response: { data: { detail: '运行模式暂不可用' } },
    })

    renderPage(queryClient)

    expect(await screen.findByText('通知运行模式无法确认，通知操作已停用。')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('批次 ID'), { target: { value: '42' } })
    const stage = screen.getByRole('button', { name: '等待通知模式确认' })
    const retry = await screen.findByRole('button', { name: '重试投递 7' })
    expect((stage as HTMLButtonElement).disabled).toBe(true)
    expect((retry as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(stage)
    fireEvent.click(retry)
    expect(dingtalkApi.stageReviewDeliveries).not.toHaveBeenCalled()
    expect(dingtalkApi.retryDingTalkDelivery).not.toHaveBeenCalled()
  })

  it('does not let a global notification manager appeal another manager’s delivery', async () => {
    auth.permissions = ['notification:manage', 'payroll:review']
    dingtalkApi.fetchDingTalkDeliveries.mockResolvedValue([
      { ...reviewDelivery, id: 9, can_appeal: false },
    ])

    renderPage()

    expect(await screen.findByText('投递 #9')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '发起申诉' })).toBeNull()
    expect(screen.getByRole('button', { name: '重试投递 9' })).toBeTruthy()
  })

  it('shows a displayable backend error when an appeal cannot be created', async () => {
    dingtalkApi.createCompAppeal.mockRejectedValue({
      response: { data: { detail: 'Delivered review scope not found' } },
    })

    renderPage()
    await screen.findByText('投递 #7')
    fireEvent.click(screen.getByRole('button', { name: '发起申诉' }))
    fireEvent.change(screen.getByLabelText('申诉说明'), { target: { value: '请复核。' } })
    fireEvent.click(screen.getByRole('button', { name: '提交申诉' }))

    expect(await screen.findByText('Delivered review scope not found')).toBeTruthy()
  })

  it('does not request or show protected data without an eligible permission', () => {
    auth.permissions = []

    renderPage()

    expect(screen.getByText('薪酬申诉与钉钉通知需要审核、申诉查看或通知管理权限。')).toBeTruthy()
    expect(dingtalkApi.fetchDingTalkDeliveries).not.toHaveBeenCalled()
    expect(dingtalkApi.fetchCompAppeals).not.toHaveBeenCalled()
  })
})
