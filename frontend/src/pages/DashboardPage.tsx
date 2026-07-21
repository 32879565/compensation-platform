import { useQuery } from '@tanstack/react-query'
import { Alert, Card, Col, Input, Row, Space, Statistic, Table, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
import type { ReactNode } from 'react'
import { useState } from 'react'

import { fetchDashboard, type StoreRank } from '../api/dashboard'
import { useAuth } from '../auth/AuthContext'

function currentMonth(): string {
  const now = new Date()
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 7)
}

function money(value: string | null): string {
  if (value === null) return '未维护'
  const numeric = Number(value)
  return Number.isFinite(numeric)
    ? numeric.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : value
}

function requestError(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response
    if (typeof response?.data?.detail === 'string') return response.data.detail
  }
  return '暂时无法读取管理看板，请稍后重试。'
}

function varianceTag(value: string | null): ReactNode {
  if (value === null) return <Tag>未维护预算</Tag>
  const numeric = Number(value)
  return <Tag color={numeric > 0 ? 'red' : numeric < 0 ? 'green' : 'blue'}>{money(value)}</Tag>
}

export default function DashboardPage() {
  const { user } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const [period, setPeriod] = useState(currentMonth)
  const query = useQuery({
    queryKey: ['dashboard', queryScope, period],
    queryFn: () => fetchDashboard(period),
  })
  const metrics = query.data?.metrics
  const columns: TableProps<StoreRank>['columns'] = [
    { title: '门店', render: (_: unknown, row) => `${row.org_code} · ${row.org_name}` },
    { title: '人数', dataIndex: 'employee_count' },
    { title: '应发总额', dataIndex: 'actual_gross', render: money },
    { title: '平均应发', dataIndex: 'average_gross', render: money },
    { title: '预算', dataIndex: 'budget_cost', render: money },
    { title: '预算差异', dataIndex: 'cost_variance', render: varianceTag },
  ]

  return (
    <Space data-testid="dashboard-page" direction="vertical" size="large" style={{ width: '100%' }}>
      <Space wrap align="center">
        <Typography.Title level={3} style={{ margin: 0 }}>
          管理看板
        </Typography.Title>
        <label>
          计薪周期
          <Input
            aria-label="看板计薪周期"
            type="month"
            value={period}
            onChange={(event) => setPeriod(event.target.value || currentMonth())}
            style={{ width: 150, marginLeft: 8 }}
          />
        </label>
      </Space>
      {query.isError && <Alert type="error" showIcon message={requestError(query.error)} />}
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={6}>
          <Card loading={query.isLoading}>
            <Statistic title="已锁定员工数" value={metrics?.employee_count ?? 0} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card loading={query.isLoading}>
            <Statistic title="应发总额" value={money(metrics?.actual_gross ?? '0')} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card loading={query.isLoading}>
            <Statistic title="平均应发" value={money(metrics?.average_gross ?? '0')} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card loading={query.isLoading}>
            <Statistic title="预算差异（应发 − 预算）" value={money(metrics?.cost_variance ?? null)} />
          </Card>
        </Col>
      </Row>
      <Card
        title="统计口径"
        size="small"
        loading={query.isLoading}
      >
        <Typography.Paragraph style={{ margin: 0 }} type="secondary">
          实际值仅统计当前组织范围内、已锁定且属于当前复核轮次的员工最新薪资结果；人力成本按应发额汇总。预算仅允许按门店维护，区域和集团指标为门店预算之和。
        </Typography.Paragraph>
      </Card>
      <Card title="近六个已锁定周期趋势" loading={query.isLoading}>
        <Table
          rowKey="period"
          dataSource={query.data?.trend ?? []}
          pagination={false}
          columns={[
            { title: '周期', dataIndex: 'period' },
            { title: '人数', dataIndex: 'employee_count' },
            { title: '应发总额', dataIndex: 'actual_gross', render: money },
            { title: '预算', dataIndex: 'budget_cost', render: money },
          ]}
          locale={{ emptyText: '没有可见的已锁定薪资结果。' }}
        />
      </Card>
      <Card title="门店人力成本排行" loading={query.isLoading}>
        <Table<StoreRank>
          rowKey="org_unit_id"
          dataSource={query.data?.store_ranking ?? []}
          columns={columns}
          pagination={false}
          locale={{ emptyText: '没有可见的已锁定薪资结果。' }}
        />
      </Card>
    </Space>
  )
}
