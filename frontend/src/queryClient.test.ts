import { afterEach, describe, expect, it } from 'vitest'

import { clearSessionQueries, queryClient } from './queryClient'

describe('session query isolation', () => {
  afterEach(() => clearSessionQueries())

  it('removes a prior principal payroll result before another session can render it', () => {
    const priorKey = ['payrollResults', 'group-hr', 17]
    const nextKey = ['payrollResults', 'store-manager', 17]
    queryClient.setQueryData(priorKey, [{ employee_name: 'sensitive payroll' }])

    clearSessionQueries()

    expect(queryClient.getQueryData(priorKey)).toBeUndefined()
    expect(queryClient.getQueryData(nextKey)).toBeUndefined()
  })
})
