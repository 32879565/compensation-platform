import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import {
  fetchHolidayCalendarPeriod,
  fetchHolidayDates,
  fetchHolidayWork,
  finalizeHolidayCalendar,
  setHolidayWork,
  unfinalizeHolidayCalendar,
  upsertHolidayDate,
} from './holidays'

describe('statutory holiday API client', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    client.get.mockResolvedValue({ data: [] })
    client.put.mockResolvedValue({ data: {} })
    client.post.mockResolvedValue({ data: {} })
  })

  it('reads the selected monthly ledger and employee day-level work records', async () => {
    await fetchHolidayDates('2026-07')
    await fetchHolidayCalendarPeriod('2026-07')
    await fetchHolidayWork(17, '2026-07')

    expect(client.get).toHaveBeenNthCalledWith(1, '/api/holiday-calendar/dates', {
      params: { period: '2026-07' },
    })
    expect(client.get).toHaveBeenNthCalledWith(2, '/api/holiday-calendar/periods/2026-07')
    expect(client.get).toHaveBeenNthCalledWith(
      3,
      '/api/holiday-calendar/employees/17/work',
      { params: { period: '2026-07' } },
    )
  })

  it('writes calendar definitions, finalization, and employee evidence to source endpoints', async () => {
    const holiday = {
      holiday_date: '2026-07-01',
      name: '法定假日',
      eligible_employment_types: ['FULL_TIME' as const],
    }
    const work = {
      worked: true,
      reason: '门店排班',
      evidence_url: 'https://evidence.example/shift',
    }

    await upsertHolidayDate(holiday)
    await finalizeHolidayCalendar('2026-07')
    await unfinalizeHolidayCalendar('2026-07')
    await setHolidayWork(17, '2026-07-01', work)

    expect(client.put).toHaveBeenNthCalledWith(
      1,
      '/api/holiday-calendar/dates/2026-07-01',
      holiday,
    )
    expect(client.post).toHaveBeenNthCalledWith(
      1,
      '/api/holiday-calendar/periods/2026-07/finalize',
    )
    expect(client.post).toHaveBeenNthCalledWith(
      2,
      '/api/holiday-calendar/periods/2026-07/unfinalize',
    )
    expect(client.put).toHaveBeenNthCalledWith(
      2,
      '/api/holiday-calendar/employees/17/work/2026-07-01',
      work,
    )
  })
})
