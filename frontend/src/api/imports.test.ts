import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))

vi.mock('./client', () => ({ api: client }))

import {
  confirmSalaryImport,
  fetchSalaryImportPublishTargets,
  fetchSalaryImportRows,
  publishSalaryImport,
  uploadSalaryImport,
} from './imports'

describe('salary import API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('uploads the workbook as multipart data with the selected period', async () => {
    client.post.mockResolvedValueOnce({
      data: {
        id: 8,
        filename: 'salary.xlsx',
        period: '2026-07',
        status: 'PARSED',
        total_rows: 2,
        error_rows: 0,
      },
    })
    const file = new File(['workbook'], 'salary.xlsx')

    await uploadSalaryImport('2026-07', file)

    expect(client.post).toHaveBeenCalledTimes(1)
    const [path, body, config] = client.post.mock.calls[0]
    expect(path).toBe('/api/imports')
    expect(config).toEqual({ params: { period: '2026-07' } })
    expect(body).toBeInstanceOf(FormData)
    expect((body as FormData).get('file')).toBe(file)
  })

  it('reads staging rows and publish targets, then publishes only the selected stores', async () => {
    client.get.mockResolvedValueOnce({ data: [] }).mockResolvedValueOnce({
      data: [
        {
          store_id: 11,
          store_name: '一店',
          employee_count: 1,
          departments: ['DINING'],
        },
        {
          store_id: 22,
          store_name: '二店',
          employee_count: 1,
          departments: ['KITCHEN'],
        },
      ],
    })
    client.post.mockResolvedValueOnce({ data: { written: 2 } }).mockResolvedValueOnce({
      data: {
        import_batch_id: 8,
        payroll_batch_id: 19,
        batch_version: 1,
        employees: 2,
        scopes: 2,
        routed: 2,
        configuration_failures: 0,
        existing: 0,
        sandbox: true,
      },
    })

    await fetchSalaryImportRows(8)
    await confirmSalaryImport(8)
    await fetchSalaryImportPublishTargets(8)
    await publishSalaryImport(8, [11, 22])

    expect(client.get).toHaveBeenNthCalledWith(1, '/api/imports/8')
    expect(client.get).toHaveBeenNthCalledWith(2, '/api/imports/8/publish-targets')
    expect(client.post).toHaveBeenNthCalledWith(1, '/api/imports/8/confirm')
    expect(client.post).toHaveBeenNthCalledWith(2, '/api/imports/8/publish', {
      store_ids: [11, 22],
    })
  })
})
