import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))

vi.mock('./client', () => ({ api: client }))

import {
  approveBatch,
  confirmScope,
  createBatch,
  createDispute,
  fetchBatches,
  fetchAdjustments,
  fetchConfirmations,
  fetchDisputes,
  fetchResults,
  lockBatch,
  reopenBatch,
  resolveDispute,
  runBatch,
  supplementDispute,
  unlockBatch,
} from './batches'

describe('payroll batch API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.post.mockReset()
    client.get.mockResolvedValue({ data: [] })
    client.post.mockResolvedValue({ data: {} })
  })

  it('uses the scoped read endpoints for batch data', async () => {
    await fetchBatches()
    await fetchResults(12)
    await fetchConfirmations(12)
    await fetchDisputes(12)
    await fetchAdjustments(12)

    expect(client.get).toHaveBeenNthCalledWith(1, '/api/batches')
    expect(client.get).toHaveBeenNthCalledWith(2, '/api/batches/12/results')
    expect(client.get).toHaveBeenNthCalledWith(3, '/api/batches/12/confirmations')
    expect(client.get).toHaveBeenNthCalledWith(4, '/api/batches/12/disputes')
    expect(client.get).toHaveBeenNthCalledWith(5, '/api/batches/12/adjustments')
  })

  it('sends workflow commands and their auditable payloads to the batch API', async () => {
    await createBatch({
      period: '2026-05',
      attendance_start: '2026-04-26',
      attendance_end: '2026-05-25',
    })
    await runBatch(12)
    await confirmScope(12, { org_unit_id: 9, department: 'DINING' })
    await createDispute(12, {
      employee_id: 7,
      salary_item: 'HOLIDAY',
      opinion: 'Check holiday pay',
    })
    await supplementDispute(4, {
      note: 'Attached approved roster',
      attachment_url: 'https://files.example.test/roster.pdf',
    })
    await resolveDispute(4, {
      decision: 'APPROVED',
      resolution: 'Corrected source attendance',
      attachment_url: 'https://files.example.test/approval.pdf',
      attendance_changes: { actual_days: 22, worked_hours: 176 },
    })
    await approveBatch(12)
    await lockBatch(12)
    await unlockBatch(12, 'Correct an approved attendance record')
    await reopenBatch(12, 'Return an unlocked review round for correction')

    expect(client.post).toHaveBeenNthCalledWith(1, '/api/batches', {
      period: '2026-05',
      attendance_start: '2026-04-26',
      attendance_end: '2026-05-25',
    })
    expect(client.post).toHaveBeenNthCalledWith(2, '/api/batches/12/run')
    expect(client.post).toHaveBeenNthCalledWith(3, '/api/batches/12/confirm', {
      org_unit_id: 9,
      department: 'DINING',
    })
    expect(client.post).toHaveBeenNthCalledWith(4, '/api/batches/12/disputes', {
      employee_id: 7,
      salary_item: 'HOLIDAY',
      opinion: 'Check holiday pay',
    })
    expect(client.post).toHaveBeenNthCalledWith(5, '/api/batches/disputes/4/supplements', {
      note: 'Attached approved roster',
      attachment_url: 'https://files.example.test/roster.pdf',
    })
    expect(client.post).toHaveBeenNthCalledWith(6, '/api/batches/disputes/4/resolve', {
      decision: 'APPROVED',
      resolution: 'Corrected source attendance',
      attachment_url: 'https://files.example.test/approval.pdf',
      attendance_changes: { actual_days: 22, worked_hours: 176 },
    })
    expect(client.post).toHaveBeenNthCalledWith(7, '/api/batches/12/approve')
    expect(client.post).toHaveBeenNthCalledWith(8, '/api/batches/12/lock')
    expect(client.post).toHaveBeenNthCalledWith(9, '/api/batches/12/unlock', {
      reason: 'Correct an approved attendance record',
    })
    expect(client.post).toHaveBeenNthCalledWith(10, '/api/batches/12/reopen', {
      reason: 'Return an unlocked review round for correction',
    })
  })
})
