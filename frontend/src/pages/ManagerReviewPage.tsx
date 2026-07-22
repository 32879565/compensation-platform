import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'

import {
  confirmManagerReview,
  createManagerDispute,
  exchangeManagerSession,
  fetchManagerReview,
  fetchManagerReviewConfig,
  type ManagerEmployeePayroll,
  type ManagerReview,
  type ManagerSalaryLine,
} from '../api/managerReview'
import { requestDingTalkAuthCode } from '../dingtalk/bridge'

const DEPARTMENT_LABEL = {
  DINING: '厅面',
  KITCHEN: '厨房',
  OTHER: '其他',
} as const

type DisputeTarget = {
  employee: ManagerEmployeePayroll
  line: ManagerSalaryLine
}

function errorText(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response
    if (typeof response?.data?.detail === 'string') return response.data.detail
  }
  return '钉钉身份验证失败，请从最新的工资复核通知重新打开。'
}

function money(value: string): string {
  const number = Number(value)
  return Number.isFinite(number)
    ? `¥${number.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : '--'
}

export default function ManagerReviewPage() {
  const { reviewId = '' } = useParams<{ reviewId: string }>()
  const mutating = useRef(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [token, setToken] = useState<string | null>(null)
  const [review, setReview] = useState<ManagerReview | null>(null)
  const [target, setTarget] = useState<DisputeTarget | null>(null)
  const [opinion, setOpinion] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    let active = true

    async function authenticate() {
      try {
        if (!/^[0-9a-f]{32}$/.test(reviewId)) throw new Error('invalid review id')
        const config = await fetchManagerReviewConfig()
        if (!config.enabled || !config.client_id || !config.corp_id) {
          if (active) setError('请从钉钉工作通知中打开此页面')
          return
        }
        const authCode = await requestDingTalkAuthCode({
          clientId: config.client_id,
          corpId: config.corp_id,
        })
        const session = await exchangeManagerSession({
          review_id: reviewId,
          auth_code: authCode,
        })
        const detail = await fetchManagerReview(reviewId, session.access_token)
        if (active) {
          setToken(session.access_token)
          setReview(detail)
        }
      } catch (caught) {
        if (active) setError(errorText(caught))
      } finally {
        if (active) setLoading(false)
      }
    }

    void authenticate()
    return () => {
      active = false
    }
  }, [reviewId])

  async function submitDispute() {
    const normalized = opinion.trim()
    if (!target || !token || !normalized || mutating.current) return
    mutating.current = true
    setSubmitting(true)
    try {
      await createManagerDispute(reviewId, token, {
        employee_id: target.employee.employee_id,
        salary_item: target.line.code,
        opinion: normalized,
      })
      setReview((current) => (current ? { ...current, confirmation_status: 'DISPUTED' } : current))
      setTarget(null)
      setOpinion('')
      message.success('异议已提交，人事会在系统内处理')
    } catch (caught) {
      message.error(errorText(caught))
    } finally {
      mutating.current = false
      setSubmitting(false)
    }
  }

  async function confirmReview() {
    if (!token || mutating.current) return
    mutating.current = true
    setSubmitting(true)
    try {
      const result = await confirmManagerReview(reviewId, token)
      setReview((current) =>
        current ? { ...current, confirmation_status: result.confirmation_status } : current,
      )
      message.success('本部门工资已确认')
    } catch (caught) {
      message.error(errorText(caught))
    } finally {
      mutating.current = false
      setSubmitting(false)
    }
  }

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 160 }}>
        <Spin size="large" aria-label="正在通过钉钉验证身份" />
      </div>
    )
  }

  if (error || !review) {
    return (
      <div style={{ maxWidth: 560, margin: '80px auto', padding: 16 }}>
        <Alert
          type="warning"
          showIcon
          message={error ?? '工资复核任务不可用'}
          description="为保护员工薪资，本页面不能通过普通浏览器登录，也不能转发给其他人查看。"
        />
      </div>
    )
  }

  const lineColumns = (
    employee: ManagerEmployeePayroll,
  ): TableProps<ManagerSalaryLine>['columns'] => [
    { title: '工资项目', dataIndex: 'name' },
    {
      title: '金额',
      dataIndex: 'amount',
      align: 'right',
      render: (value: string) => money(value),
    },
    {
      title: '操作',
      key: 'action',
      width: 108,
      render: (_value, line) => (
        <Button
          type="link"
          aria-label={`对 ${line.name} 提出异议`}
          disabled={review.confirmation_status === 'CONFIRMED'}
          onClick={() => setTarget({ employee, line })}
        >
          提出异议
        </Button>
      ),
    },
  ]

  return (
    <div style={{ maxWidth: 920, margin: '0 auto', padding: '20px 12px 40px' }}>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <div>
          <Typography.Title level={3} style={{ marginBottom: 4 }}>
            {review.period} 工资复核
          </Typography.Title>
          <Typography.Text type="secondary">
            {review.store_name} · {DEPARTMENT_LABEL[review.department]}
          </Typography.Text>
        </div>
        <Alert type="warning" showIcon message="薪资信息仅限本人负责范围查看，请勿截图或转发" />
        <Space wrap>
          <Tag color={review.confirmation_status === 'CONFIRMED' ? 'success' : 'processing'}>
            {review.confirmation_status === 'CONFIRMED'
              ? '已确认'
              : review.confirmation_status === 'DISPUTED'
                ? '已提异议'
                : '待复核'}
          </Tag>
          <Typography.Text type="secondary">共 {review.employees.length} 名员工</Typography.Text>
        </Space>

        {review.employees.map((employee) => (
          <Card
            key={employee.employee_id}
            title={
              <Space>
                <span>{employee.employee_name}</span>
                {employee.emp_no ? (
                  <Typography.Text type="secondary">{employee.emp_no}</Typography.Text>
                ) : null}
              </Space>
            }
          >
            <Descriptions size="small" column={{ xs: 2, sm: 4 }} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="计薪出勤">
                {employee.actual_attendance_days} 天
              </Descriptions.Item>
              <Descriptions.Item label="法定出勤">
                {employee.statutory_holiday_worked_days} 天
              </Descriptions.Item>
              <Descriptions.Item label="应发">{money(employee.gross)}</Descriptions.Item>
              <Descriptions.Item label="实发">{money(employee.net)}</Descriptions.Item>
            </Descriptions>
            <Table<ManagerSalaryLine>
              size="small"
              rowKey="code"
              pagination={false}
              columns={lineColumns(employee)}
              dataSource={employee.lines}
              scroll={{ x: 520 }}
            />
          </Card>
        ))}

        {review.confirmation_status === 'PENDING' ? (
          <Popconfirm
            title="确认本部门所有员工工资无误？"
            description="确认后将进入人事终审。"
            okText="确认无误"
            cancelText="再检查一下"
            onConfirm={() => void confirmReview()}
          >
            <Button type="primary" size="large" block loading={submitting}>
              确认本部门工资
            </Button>
          </Popconfirm>
        ) : null}
      </Space>

      <Modal
        open={target !== null}
        title={target ? `${target.employee.employee_name} · ${target.line.name}` : '提出异议'}
        footer={null}
        destroyOnHidden
        onCancel={() => {
          if (!submitting) {
            setTarget(null)
            setOpinion('')
          }
        }}
      >
        <Form layout="vertical" onFinish={() => void submitDispute()}>
          <Form.Item label="当前金额">
            <Typography.Text strong>{target ? money(target.line.amount) : '--'}</Typography.Text>
          </Form.Item>
          <Form.Item label="异议说明" required>
            <Input.TextArea
              aria-label="异议说明"
              value={opinion}
              maxLength={1000}
              showCount
              rows={4}
              autoComplete="off"
              onChange={(event) => setOpinion(event.target.value)}
            />
          </Form.Item>
          <Button
            type="primary"
            htmlType="submit"
            block
            loading={submitting}
            disabled={!opinion.trim() || submitting}
          >
            提交异议
          </Button>
        </Form>
      </Modal>
    </div>
  )
}
