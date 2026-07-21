import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Skeleton, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { KeyboardEvent } from 'react'

import {
  fetchLegacyCatalogPreview,
  type LegacyComponentCandidate,
  type LegacyGradeCandidate,
} from '../api/legacyCatalog'

export interface LegacyCatalogEvidencePanelProps {
  mode: 'components' | 'grades'
  onReview: () => void
}

const HORIZONTAL_SCROLL_STEP = 80

const COMPONENT_TYPE_LABELS: Record<string, string> = {
  BASE: '基本薪资',
  COMPREHENSIVE: '综合薪资',
  PERFORMANCE: '绩效/奖励',
  POSITION: '岗位工资',
  ALLOWANCE: '补贴',
  HOUSING: '房补',
  OVERTIME: '加班',
  DEDUCTION: '扣减',
}

function handleHorizontalRegionKeyDown(event: KeyboardEvent<HTMLDivElement>) {
  if (event.key === 'ArrowRight') {
    event.preventDefault()
    event.currentTarget.scrollLeft += HORIZONTAL_SCROLL_STEP
  } else if (event.key === 'ArrowLeft') {
    event.preventDefault()
    event.currentTarget.scrollLeft = Math.max(
      0,
      event.currentTarget.scrollLeft - HORIZONTAL_SCROLL_STEP,
    )
  }
}

function moneyText(value: string | null): string {
  if (value === null) return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return value
  return parsed.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function componentStatus(candidate: LegacyComponentCandidate) {
  if (candidate.applied) return <Tag color="green">已创建正式组件</Tag>
  if (!candidate.importable) return <Tag>核算结果，不导入</Tag>
  return <Tag color="gold">待人事确认</Tag>
}

function gradeStatus(candidate: LegacyGradeCandidate) {
  if (candidate.applied) return <Tag color="green">已创建正式职级</Tag>
  if (candidate.suppressed_for_privacy) return <Tag>隐私阈值不足</Tag>
  return <Tag color="gold">待人事确认</Tag>
}

const componentColumns: ColumnsType<LegacyComponentCandidate> = [
  { title: '旧系统字段', dataIndex: 'source_field', width: 180 },
  {
    title: '建议归类',
    dataIndex: 'suggested_component_type',
    width: 120,
    render: (value: string | null) => (value ? (COMPONENT_TYPE_LABELS[value] ?? value) : '—'),
  },
  {
    title: '真实记录数',
    dataIndex: 'record_count',
    width: 120,
    render: (value: number) => value.toLocaleString('zh-CN'),
  },
  {
    title: '非零记录',
    dataIndex: 'nonzero_count',
    width: 110,
    render: (value: number) => value.toLocaleString('zh-CN'),
  },
  {
    title: '覆盖期间',
    key: 'period',
    width: 170,
    render: (_value, candidate) => `${candidate.period_from} 至 ${candidate.period_to}`,
  },
  { title: '目录状态', key: 'status', width: 160, render: (_value, candidate) => componentStatus(candidate) },
]

const gradeColumns: ColumnsType<LegacyGradeCandidate> = [
  { title: '旧系统职位', dataIndex: 'position', width: 180 },
  {
    title: '独立员工',
    dataIndex: 'contributor_count',
    width: 110,
    render: (value: number) => `${value.toLocaleString('zh-CN')} 人`,
  },
  {
    title: '月度记录',
    dataIndex: 'record_count',
    width: 110,
    render: (value: number) => value.toLocaleString('zh-CN'),
  },
  {
    title: '历史薪资 P25 / 中位 / P75',
    key: 'observed_band',
    width: 260,
    render: (_value, candidate) =>
      `${moneyText(candidate.observed_p25)} / ${moneyText(candidate.observed_median)} / ${moneyText(candidate.observed_p75)}`,
  },
  {
    title: '覆盖期间',
    key: 'period',
    width: 170,
    render: (_value, candidate) => `${candidate.period_from} 至 ${candidate.period_to}`,
  },
  { title: '目录状态', key: 'status', width: 160, render: (_value, candidate) => gradeStatus(candidate) },
]

export default function LegacyCatalogEvidencePanel({
  mode,
  onReview,
}: LegacyCatalogEvidencePanelProps) {
  const previewQuery = useQuery({
    queryKey: ['legacy-catalog-preview'],
    queryFn: fetchLegacyCatalogPreview,
  })

  const reviewLabel = mode === 'components' ? '审核并创建正式组件' : '审核并创建正式职级'

  return (
    <Card
      title={
        <Space wrap>
          <span>旧系统真实数据</span>
          <Tag color="gold">待确认目录</Tag>
        </Space>
      }
      extra={
        <Button type="primary" disabled={!previewQuery.data} onClick={onReview}>
          {reviewLabel}
        </Button>
      }
    >
      {previewQuery.isError ? (
        <Alert
          type="error"
          showIcon
          message="旧系统真实数据加载失败"
          description="未读取到历史汇总，正式目录没有被自动填充。"
          action={
            <Button size="small" onClick={() => void previewQuery.refetch()}>
              重新加载
            </Button>
          }
        />
      ) : previewQuery.isLoading || !previewQuery.data ? (
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <div
            style={{
              borderLeft: '4px solid #1677ff',
              background: '#f0f7ff',
              padding: '12px 16px',
              borderRadius: '0 8px 8px 0',
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            <Space wrap split={<Typography.Text type="secondary">·</Typography.Text>}>
              <Typography.Text strong>
                {previewQuery.data.source.record_count.toLocaleString('zh-CN')} 条真实工资记录
              </Typography.Text>
              <Typography.Text>
                {previewQuery.data.source.period_from ?? '未知'} 至{' '}
                {previewQuery.data.source.period_to ?? '未知'}
              </Typography.Text>
              <Typography.Text>
                {mode === 'components'
                  ? `${previewQuery.data.component_candidates.length} 项历史字段`
                  : `${previewQuery.data.grade_candidates.length} 个历史职位`}
              </Typography.Text>
            </Space>
          </div>

          <Alert
            type="warning"
            showIcon
            message="以下内容直接来自旧系统，不是演示数据"
            description="历史名称与金额可以直接核对；计税、社保、固定/浮动及正式职级口径必须经人事确认后才参与计薪。"
          />

          <div
            role="region"
            aria-label={
              mode === 'components' ? '旧系统真实薪资组件数据' : '旧系统真实职级数据'
            }
            tabIndex={0}
            style={{ overflowX: 'auto' }}
            onKeyDown={handleHorizontalRegionKeyDown}
          >
            <div style={{ minWidth: mode === 'components' ? 900 : 1000 }}>
              {mode === 'components' ? (
                <Table<LegacyComponentCandidate>
                  rowKey="source_field"
                  size="small"
                  columns={componentColumns}
                  dataSource={previewQuery.data.component_candidates}
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  locale={{ emptyText: '旧系统没有可展示的薪资字段' }}
                />
              ) : (
                <Table<LegacyGradeCandidate>
                  rowKey="position"
                  size="small"
                  columns={gradeColumns}
                  dataSource={previewQuery.data.grade_candidates}
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  locale={{ emptyText: '旧系统没有达到隐私阈值的历史职位' }}
                />
              )}
            </div>
          </div>
        </Space>
      )}
    </Card>
  )
}
