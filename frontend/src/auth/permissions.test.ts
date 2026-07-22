import { describe, expect, it } from 'vitest'

import { Perm } from './permissions'

describe('frontend permission catalog', () => {
  it('contains the attendance schedule, expected-day adjustment, and import permissions exposed by the backend', () => {
    expect(Perm.ATTENDANCE_SCHEDULE_READ).toBe('attendance_schedule:read')
    expect(Perm.ATTENDANCE_SCHEDULE_WRITE).toBe('attendance_schedule:write')
    expect(Perm.ATTENDANCE_EXPECTED_DAYS_ADJUST).toBe('attendance:expected_days:adjust')
    expect(Perm.IMPORT_RUN).toBe('import:run')
    expect(Perm.DINGTALK_ORG_SYNC).toBe('dingtalk_org:sync')
  })
})
