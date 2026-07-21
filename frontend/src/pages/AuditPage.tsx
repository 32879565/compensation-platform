import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Input, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'

import { fetchAuditLogs, type AuditLogEntry } from '../api/audit'
import { useAuth } from '../auth/AuthContext'

function requestErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response
    if (typeof response?.data?.detail === 'string') return response.data.detail
  }
  return '暂时无法读取审计日志，请稍后重试。'
}

function detailText(detail: Record<string, unknown> | null): string {
  if (detail === null) return '—'
  return JSON.stringify(detail)
}

export default function AuditPage() {
  const { user } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const [actionInput, setActionInput] = useState('')
  const [actorInput, setActorInput] = useState('')
  const [action, setAction] = useState('')
  const [actor, setActor] = useState('')
  const [page, setPage] = useState(1)
  const pageSize = 50

  const query = useQuery({
    queryKey: ['auditLogs', queryScope, action, actor, page, pageSize],
    queryFn: () =>
      fetchAuditLogs({
        page,
        page_size: pageSize,
        action: action || undefined,
        actor_username: actor || undefined,
      }),
  })

  const applyFilters = () => {
    setAction(actionInput.trim())
    setActor(actorInput.trim())
    setPage(1)
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Typography.Title level={3} style={{ margin: 0 }}>
        审计日志
      </Typography.Title>
      <Space wrap>
        <Input
          aria-label="操作类型"
          placeholder="操作类型（精确匹配）"
          value={actionInput}
          onChange={(event) => setActionInput(event.target.value)}
          onPressEnter={applyFilters}
          style={{ width: 220 }}
        />
        <Input
          aria-label="操作人"
          placeholder="操作人（精确匹配）"
          value={actorInput}
          onChange={(event) => setActorInput(event.target.value)}
          onPressEnter={applyFilters}
          style={{ width: 180 }}
        />
        <Button type="primary" onClick={applyFilters}>
          查询
        </Button>
        <Button
          onClick={() => {
            setActionInput('')
            setActorInput('')
            setAction('')
            setActor('')
            setPage(1)
          }}
        >
          重置
        </Button>
      </Space>
      {query.isError && <Alert type="error" showIcon message={requestErrorMessage(query.error)} />}
      <Table<AuditLogEntry>
        rowKey="id"
        loading={query.isLoading || query.isFetching}
        dataSource={query.data?.items ?? []}
        columns={[
          { title: '时间', dataIndex: 'ts', width: 190 },
          { title: '操作人', dataIndex: 'actor_username', render: (value: string | null) => value ?? '—' },
          { title: '操作', dataIndex: 'action', width: 210 },
          {
            title: '结果',
            dataIndex: 'result',
            render: (value: string) => <Tag color={value === 'SUCCESS' ? 'green' : 'red'}>{value}</Tag>,
          },
          {
            title: '对象',
            render: (_: unknown, row: AuditLogEntry) =>
              row.target_type ? `${row.target_type}${row.target_id ? ` #${row.target_id}` : ''}` : '—',
          },
          {
            title: '详情（已脱敏）',
            dataIndex: 'detail',
            ellipsis: true,
            render: (detail: Record<string, unknown> | null) => detailText(detail),
          },
        ]}
        pagination={{
          current: page,
          pageSize,
          total: query.data?.total ?? 0,
          onChange: setPage,
          showSizeChanger: false,
          showTotal: (total) => `共 ${total} 条`,
        }}
      />
    </Space>
  )
}
