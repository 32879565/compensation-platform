import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import {
  createAttendanceSchedule,
  fetchAttendanceSchedules,
  fetchPerformance,
  generateExpectedAttendance,
  importPerformance,
  updateAttendanceSchedule,
  type AttendanceScheduleWrite,
} from './attendance'

const schedule: AttendanceScheduleWrite = {
  name: '厅面全职规则',
  org_unit_id: 3,
  employment_type: 'FULL_TIME',
  department: 'DINING',
  position_title: null,
  is_special_position: false,
  weekly_rest_days: [5, 6],
  monthly_expected_days: null,
  effective_from: '2026-01-01',
  effective_to: null,
  priority: 10,
  is_active: true,
}

describe('performance API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.post.mockReset()
    client.put.mockReset()
  })

  it('fetches performance records for the selected payroll period', async () => {
    const records = [
      {
        employee_id: 7,
        period: '2026-07',
        coefficient: '1.250',
        score: '96.50',
        remark: '表现优秀',
      },
    ]
    client.get.mockResolvedValue({ data: records })

    await expect(fetchPerformance('2026-07')).resolves.toEqual(records)
    expect(client.get).toHaveBeenCalledWith('/api/performance', {
      params: { period: '2026-07' },
    })
  })

  it('uploads the selected workbook as the required file field', async () => {
    const file = new File(['workbook'], 'performance.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    client.post.mockResolvedValue({ data: { matched: 2, skipped: ['E404'] } })

    await expect(importPerformance('2026-07', file)).resolves.toEqual({
      matched: 2,
      skipped: ['E404'],
    })

    const [url, body, config] = client.post.mock.calls[0] as [string, FormData, unknown]
    expect(url).toBe('/api/performance/import')
    expect(body).toBeInstanceOf(FormData)
    expect(body.get('file')).toBe(file)
    expect(config).toEqual({ params: { period: '2026-07' } })
  })
})

describe('expected attendance schedule API client', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.post.mockReset()
    client.put.mockReset()
  })

  it('lists the configured rules', async () => {
    const rules = [{ id: 11, ...schedule }]
    client.get.mockResolvedValue({ data: rules })

    await expect(fetchAttendanceSchedules()).resolves.toEqual(rules)
    expect(client.get).toHaveBeenCalledWith('/api/attendance-schedules')
  })

  it('creates and fully replaces schedule rules', async () => {
    client.post.mockResolvedValue({ data: { id: 11, ...schedule } })
    client.put.mockResolvedValue({ data: { id: 11, ...schedule, is_active: false } })

    await expect(createAttendanceSchedule(schedule)).resolves.toMatchObject({ id: 11 })
    await expect(
      updateAttendanceSchedule(11, { ...schedule, is_active: false }),
    ).resolves.toMatchObject({ id: 11, is_active: false })

    expect(client.post).toHaveBeenCalledWith('/api/attendance-schedules', schedule)
    expect(client.put).toHaveBeenCalledWith('/api/attendance-schedules/11', {
      ...schedule,
      is_active: false,
    })
  })

  it('generates expected days for the selected payroll month', async () => {
    const result = { period: '2026-07', generated: 18, adjusted_preserved: 2 }
    client.post.mockResolvedValue({ data: result })

    await expect(generateExpectedAttendance('2026-07')).resolves.toEqual(result)
    expect(client.post).toHaveBeenCalledWith('/api/attendance-schedules/generate', undefined, {
      params: { period: '2026-07' },
    })
  })
})
