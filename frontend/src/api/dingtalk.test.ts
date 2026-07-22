import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import {
  createCompAppeal,
  applyDingTalkOrganization,
  fetchCompAppeal,
  fetchCompAppeals,
  fetchDingTalkDeliveries,
  fetchDingTalkAttendanceSnapshot,
  fetchDingTalkIntegration,
  fetchDingTalkMode,
  applyDingTalkEmployeeMatches,
  previewDingTalkAttendance,
  previewDingTalkEmployees,
  previewDingTalkOrganization,
  refreshDingTalkAttendance,
  retryDingTalkDelivery,
  stageReviewDeliveries,
  testDingTalkIntegration,
} from './dingtalk'

const delivery = {
  id: 7,
  batch_id: 42,
  batch_version: 3,
  org_unit_id: 8,
  department: 'DINING',
  recipient_user_id: 99,
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
  employee_id: 123,
  requester_id: 99,
  reason: 'Contains a private employee detail',
  status: 'PENDING',
  resolution: 'Contains a private resolution',
  approval_instance_id: 31,
  created_at: '2026-07-20T12:00:00+00:00',
}

describe('DingTalk and compensation appeal API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.post.mockReset()
  })

  it('uses the sandbox delivery and compensation appeal endpoints', async () => {
    client.get
      .mockResolvedValueOnce({
        data: {
          mode: 'sandbox',
          credentials_configured: true,
          app_id_configured: true,
          public_base_url_configured: false,
          ready_for_live: false,
          read_sync_enabled: true,
          read_sync_ready: true,
        },
      })
      .mockResolvedValueOnce({ data: [delivery] })
      .mockResolvedValueOnce({ data: [appeal] })
      .mockResolvedValueOnce({ data: appeal })
    client.post
      .mockResolvedValueOnce({ data: { connected: true, token_expires_in_seconds: 7080 } })
      .mockResolvedValueOnce({
        data: { routed: 2, configuration_failures: 1, existing: 0, sandbox: true },
      })
      .mockResolvedValueOnce({ data: delivery })
      .mockResolvedValueOnce({ data: appeal })
      .mockResolvedValueOnce({
        data: {
          total_remote_users: 4,
          matched: 2,
          stable_id_matches: 0,
          job_number_matches: 1,
          unique_name_matches: 1,
          ambiguous: 1,
          unmatched: 1,
          truncated: false,
          items: [],
        },
      })
      .mockResolvedValueOnce({
        data: { matched: 2, linked: 2, unchanged: 0, ambiguous: 1, unmatched: 1 },
      })
      .mockResolvedValueOnce({
        data: {
          period: '2026-07',
          matched_employees: 2,
          employees_with_records: 2,
          total_records: 3,
          ambiguous_directory_users: 1,
          unmatched_directory_users: 1,
          items: [],
        },
      })

    const integration = await fetchDingTalkIntegration()
    const connection = await testDingTalkIntegration()
    const deliveries = await fetchDingTalkDeliveries(42)
    const staged = await stageReviewDeliveries(42)
    const retried = await retryDingTalkDelivery(7)
    const created = await createCompAppeal({
      delivery_id: 7,
      employee_id: 123,
      reason: 'Please verify the attendance source.',
    })
    const appeals = await fetchCompAppeals()
    const detail = await fetchCompAppeal(5)
    const employeePreview = await previewDingTalkEmployees()
    const employeeApply = await applyDingTalkEmployeeMatches()
    const attendancePreview = await previewDingTalkAttendance('2026-07')

    expect(client.get).toHaveBeenNthCalledWith(1, '/api/dingtalk/integration')
    expect(client.get).toHaveBeenNthCalledWith(2, '/api/dingtalk/deliveries', {
      params: { batch_id: 42 },
    })
    expect(client.get).toHaveBeenNthCalledWith(3, '/api/comp-appeals')
    expect(client.get).toHaveBeenNthCalledWith(4, '/api/comp-appeals/5')
    expect(client.post).toHaveBeenNthCalledWith(1, '/api/dingtalk/integration/test')
    expect(client.post).toHaveBeenNthCalledWith(2, '/api/dingtalk/batches/42/review-deliveries')
    expect(client.post).toHaveBeenNthCalledWith(3, '/api/dingtalk/deliveries/7/retry')
    expect(client.post).toHaveBeenNthCalledWith(4, '/api/comp-appeals', {
      delivery_id: 7,
      employee_id: 123,
      reason: 'Please verify the attendance source.',
    })
    expect(client.post).toHaveBeenNthCalledWith(5, '/api/dingtalk/sync/employees/preview')
    expect(client.post).toHaveBeenNthCalledWith(6, '/api/dingtalk/sync/employees/apply')
    expect(client.post).toHaveBeenNthCalledWith(7, '/api/dingtalk/sync/attendance/preview', {
      period: '2026-07',
    })
    expect(staged).toEqual({ routed: 2, configuration_failures: 1, existing: 0, sandbox: true })
    expect(integration.mode).toBe('sandbox')
    expect(connection.connected).toBe(true)
    expect(deliveries).toHaveLength(1)
    expect(deliveries[0]?.can_appeal).toBe(true)
    expect(retried.id).toBe(7)
    expect(created.id).toBe(5)
    expect(appeals).toHaveLength(1)
    expect(detail.id).toBe(5)
    expect(employeePreview.matched).toBe(2)
    expect(employeeApply.linked).toBe(2)
    expect(attendancePreview.total_records).toBe(3)
    expect(retried).not.toHaveProperty('recipient_user_id')
    expect(created).not.toHaveProperty('reason')
    expect(detail).not.toHaveProperty('reason')
  })

  it('does not return personal identifiers or free text to query consumers', async () => {
    client.get.mockResolvedValueOnce({ data: [delivery] }).mockResolvedValueOnce({ data: [appeal] })

    const [listedDelivery] = await fetchDingTalkDeliveries()
    const [listedAppeal] = await fetchCompAppeals()

    expect(client.get).toHaveBeenNthCalledWith(1, '/api/dingtalk/deliveries')
    expect(listedDelivery).not.toHaveProperty('recipient_user_id')
    expect(listedAppeal).not.toHaveProperty('employee_id')
    expect(listedAppeal).not.toHaveProperty('requester_id')
    expect(listedAppeal).not.toHaveProperty('reason')
    expect(listedAppeal).not.toHaveProperty('resolution')
  })

  it('reads the minimal notification mode endpoint for payroll reviewers', async () => {
    client.get.mockResolvedValueOnce({ data: { mode: 'live' } })

    const result = await fetchDingTalkMode()

    expect(client.get).toHaveBeenCalledWith('/api/dingtalk/mode')
    expect(result).toEqual({ mode: 'live' })
  })

  it('reads and refreshes the cached DingTalk attendance snapshot', async () => {
    const snapshot = {
      period: '2026-07',
      status: 'COMPLETED',
      matched_employees: 2,
      employees_with_records: 2,
      total_records: 3,
      ambiguous_directory_users: 1,
      unmatched_directory_users: 1,
      source_start: '2026-07-01T00:00:00Z',
      source_end: '2026-07-31T23:59:59Z',
      started_at: '2026-07-21T12:00:00Z',
      refreshed_at: '2026-07-21T12:30:00Z',
      error_code: null,
      items: [],
    }
    client.get.mockResolvedValueOnce({ data: snapshot })
    client.post.mockResolvedValueOnce({ data: { ...snapshot, status: 'QUEUED' } })

    const cached = await fetchDingTalkAttendanceSnapshot('2026-07')
    const queued = await refreshDingTalkAttendance('2026-07')

    expect(client.get).toHaveBeenCalledWith('/api/dingtalk/sync/attendance/snapshot', {
      params: { period: '2026-07' },
    })
    expect(client.post).toHaveBeenCalledWith('/api/dingtalk/sync/attendance/refresh', {
      period: '2026-07',
    })
    expect(cached.total_records).toBe(3)
    expect(queued.status).toBe('QUEUED')
  })

  it('previews and applies a staged DingTalk organization sync without sending item data back', async () => {
    const preview = {
      batch_id: '3fe80f532f184247b477694427bad0ce',
      expires_at: '2026-07-22T04:00:00Z',
      remote_regions: 2,
      local_regions: 1,
      ready_regions: 1,
      region_conflicts: 0,
      remote_stores: 2,
      local_stores: 2,
      ready_stores: 1,
      store_conflicts: 0,
      ready_reviewers: 2,
      reviewer_conflicts: 1,
      region_items: [
        {
          id: 100,
          kind: 'REGION',
          remote_department_id: 8001,
          remote_department_name: '广州区域',
          remote_department_path: '集团 / 广州区域',
          action: 'CREATE',
          change_fields: ['name'],
          match_method: 'REMOTE_ONLY',
          proposed_org_unit_id: null,
          proposed_org_unit_name: '广州区域',
          proposed_parent_org_unit_id: 1,
          proposed_parent_org_unit_name: '集团',
          status: 'READY',
          conflict_code: null,
          provider_department_secret: 'must-not-reach-query-consumers',
        },
      ],
      store_items: [
        {
          id: 101,
          kind: 'STORE',
          remote_department_id: null,
          remote_department_name: '旧城店',
          remote_department_path: '本地 / 旧城店',
          action: 'DEACTIVATE',
          change_fields: [],
          match_method: 'LOCAL_STORE_NOT_VISIBLE',
          proposed_org_unit_id: 11,
          proposed_org_unit_name: '旧城店',
          proposed_parent_org_unit_id: 1,
          proposed_parent_org_unit_name: '广州区域',
          status: 'READY',
          conflict_code: null,
          provider_department_secret: 'must-not-reach-query-consumers',
        },
      ],
      reviewer_items: [
        {
          id: 201,
          remote_department_id: null,
          remote_department_name: '天河店',
          remote_department_path: '集团 / 潮发运营中心 / 天河店',
          department: 'DINING',
          action: 'REMOVE',
          dingtalk_name: null,
          proposed_employee_id: null,
          proposed_employee_name: null,
          match_method: 'CLEAR_MISSING_MANAGER',
          current_reviewer_name: '旧店长',
          status: 'READY',
          conflict_code: null,
          provider_userid: 'must-not-reach-query-consumers',
          remote_user_id: 'must-not-reach-query-consumers-either',
        },
      ],
    }
    const applied = {
      applied_stores: 1,
      applied_reviewers: 2,
      unresolved: 2,
      already_applied: false,
    }
    client.post.mockResolvedValueOnce({ data: preview }).mockResolvedValueOnce({ data: applied })

    const staged = await previewDingTalkOrganization()
    const result = await applyDingTalkOrganization(staged.batch_id)

    expect(client.post).toHaveBeenNthCalledWith(1, '/api/dingtalk/sync/organization/preview')
    expect(client.post).toHaveBeenNthCalledWith(
      2,
      '/api/dingtalk/sync/organization/3fe80f532f184247b477694427bad0ce/apply',
    )
    expect(staged.reviewer_items[0]).not.toHaveProperty('provider_userid')
    expect(staged.reviewer_items[0]).not.toHaveProperty('remote_user_id')
    expect(staged.region_items[0]).not.toHaveProperty('provider_department_secret')
    expect(staged.store_items[0]).not.toHaveProperty('provider_department_secret')
    expect(staged).toMatchObject({
      remote_regions: 2,
      local_regions: 1,
      ready_regions: 1,
      region_conflicts: 0,
    })
    expect(staged.region_items[0]).toEqual({
      id: 100,
      kind: 'REGION',
      remote_department_id: 8001,
      remote_department_name: '广州区域',
      remote_department_path: '集团 / 广州区域',
      action: 'CREATE',
      change_fields: ['name'],
      match_method: 'REMOTE_ONLY',
      proposed_org_unit_id: null,
      proposed_org_unit_name: '广州区域',
      proposed_parent_org_unit_id: 1,
      proposed_parent_org_unit_name: '集团',
      status: 'READY',
      conflict_code: null,
    })
    expect(staged.store_items[0]).toEqual({
      id: 101,
      kind: 'STORE',
      remote_department_id: null,
      remote_department_name: '旧城店',
      remote_department_path: '本地 / 旧城店',
      action: 'DEACTIVATE',
      change_fields: [],
      match_method: 'LOCAL_STORE_NOT_VISIBLE',
      proposed_org_unit_id: 11,
      proposed_org_unit_name: '旧城店',
      proposed_parent_org_unit_id: 1,
      proposed_parent_org_unit_name: '广州区域',
      status: 'READY',
      conflict_code: null,
    })
    expect(staged.reviewer_items[0]).toEqual({
      id: 201,
      remote_department_id: null,
      remote_department_name: '天河店',
      remote_department_path: '集团 / 潮发运营中心 / 天河店',
      department: 'DINING',
      action: 'REMOVE',
      dingtalk_name: null,
      proposed_employee_id: null,
      proposed_employee_name: null,
      match_method: 'CLEAR_MISSING_MANAGER',
      current_reviewer_name: '旧店长',
      status: 'READY',
      conflict_code: null,
    })
    expect(result).toEqual(applied)
  })
})
