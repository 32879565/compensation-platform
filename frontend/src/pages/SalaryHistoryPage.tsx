import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Descriptions, Input, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'

import { fetchSalaryRecords, type SalaryRecord } from '../api/salaryRecords'
import { useAuth } from '../auth/AuthContext'

interface SalaryFilters {
  period: string
  name: string
  store: string
}

const EMPTY_FILTERS: SalaryFilters = { period: '', name: '', store: '' }
const COUNT_FORMATTER = new Intl.NumberFormat('zh-CN')

function fieldText(record: SalaryRecord, key: string): string {
  const value = record.fields[key]
  return value === undefined || value === null || value === '' ? '—' : String(value)
}

function recordDetails(record: SalaryRecord) {
  const items = Object.entries(record.fields)
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([key, value]) => ({ key, label: key, children: String(value) }))

  return items.length ? (
    <Descriptions size="small" bordered column={3} items={items} />
  ) : (
    <Typography.Text type="secondary">该历史记录没有附加工资字段。</Typography.Text>
  )
}

export default function SalaryHistoryPage() {
  const { user } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const [draftFilters, setDraftFilters] = useState<SalaryFilters>(EMPTY_FILTERS)
  const [filters, setFilters] = useState<SalaryFilters>(EMPTY_FILTERS)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)

  const salaryQuery = useQuery({
    queryKey: ['salaryRecords', queryScope, filters, page, pageSize],
    queryFn: () =>
      fetchSalaryRecords({
        ...filters,
        page,
        page_size: pageSize,
      }),
  })

  function applyFilters() {
    setPage(1)
    setFilters({
      period: draftFilters.period.trim(),
      name: draftFilters.name.trim(),
      store: draftFilters.store.trim(),
    })
  }

  function resetFilters() {
    setDraftFilters(EMPTY_FILTERS)
    setFilters(EMPTY_FILTERS)
    setPage(1)
  }

  const columns = [
    { title: '月份', dataIndex: 'period', width: 100, fixed: 'left' as const },
    { title: '姓名', dataIndex: 'name', width: 110, fixed: 'left' as const },
    { title: '门店', dataIndex: 'store_name', width: 180 },
    {
      title: '来源',
      dataIndex: 'source',
      width: 100,
      render: (source: string) => (
        <Tag color={source === 'HISTORICAL' ? 'blue' : 'green'}>
          {source === 'HISTORICAL' ? '旧系统' : source}
        </Tag>
      ),
    },
    { title: '合计工资', width: 120, render: (_: unknown, row: SalaryRecord) => fieldText(row, '合计工资') },
    { title: '应发工资', width: 120, render: (_: unknown, row: SalaryRecord) => fieldText(row, '应发工资') },
    { title: '实发工资', width: 120, render: (_: unknown, row: SalaryRecord) => fieldText(row, '实发工资') },
    { title: '个税', width: 100, render: (_: unknown, row: SalaryRecord) => fieldText(row, '个税') },
    { title: '社保', width: 100, render: (_: unknown, row: SalaryRecord) => fieldText(row, '社保') },
    { title: '公积金', width: 100, render: (_: unknown, row: SalaryRecord) => fieldText(row, '公积金') },
  ]

  return (
    <div>
      <Typography.Title level={3} style={{ marginTop: 0 }}>
        历史薪资查询
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        此处展示从旧系统迁移的历史工资记录；“员工”页面仅用于维护新系统员工主数据。
      </Typography.Paragraph>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <Input
            aria-label="月份"
            type="month"
            value={draftFilters.period}
            onChange={(event) =>
              setDraftFilters((current) => ({ ...current, period: event.target.value }))
            }
            style={{ width: 150 }}
          />
          <Input
            placeholder="输入姓名"
            value={draftFilters.name}
            onChange={(event) =>
              setDraftFilters((current) => ({ ...current, name: event.target.value }))
            }
            onPressEnter={applyFilters}
            style={{ width: 180 }}
          />
          <Input
            placeholder="输入门店"
            value={draftFilters.store}
            onChange={(event) =>
              setDraftFilters((current) => ({ ...current, store: event.target.value }))
            }
            onPressEnter={applyFilters}
            style={{ width: 220 }}
          />
          <Button type="primary" onClick={applyFilters}>
            查询
          </Button>
          <Button onClick={resetFilters}>重置</Button>
        </Space>
      </Card>

      {salaryQuery.isError && (
        <Alert
          type="error"
          showIcon
          message="历史薪资加载失败"
          description="请检查后端服务后重试。"
          style={{ marginBottom: 16 }}
        />
      )}

      <Space style={{ marginBottom: 12 }}>
        <Typography.Text strong>
          共 {COUNT_FORMATTER.format(salaryQuery.data?.total ?? 0)} 条历史记录
        </Typography.Text>
        <Typography.Text type="secondary">展开一行可查看全部原始工资字段</Typography.Text>
      </Space>

      <Table<SalaryRecord>
        rowKey="id"
        loading={salaryQuery.isLoading || salaryQuery.isFetching}
        columns={columns}
        dataSource={salaryQuery.data?.items ?? []}
        scroll={{ x: 1150 }}
        expandable={{ expandedRowRender: recordDetails }}
        pagination={{
          current: page,
          pageSize,
          total: salaryQuery.data?.total ?? 0,
          showSizeChanger: true,
          pageSizeOptions: [20, 50, 100],
          showTotal: (total) => `共 ${COUNT_FORMATTER.format(total)} 条`,
          onChange: (nextPage, nextPageSize) => {
            setPage(nextPageSize !== pageSize ? 1 : nextPage)
            setPageSize(nextPageSize)
          },
        }}
      />
    </div>
  )
}
