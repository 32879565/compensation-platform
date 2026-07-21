import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState } from 'react'

import {
  type CompAppeal,
  type CompAppealStatus,
  type DingTalkDelivery,
  type DingTalkDeliveryKind,
  type DingTalkDeliveryStatus,
  createCompAppeal,
  fetchCompAppeals,
  fetchDingTalkDeliveries,
  fetchDingTalkIntegration,
  fetchDingTalkMode,
  retryDingTalkDelivery,
  stageReviewDeliveries,
  testDingTalkIntegration,
} from '../api/dingtalk'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'

interface AppealForm {
  reason: string
}

const DELIVERY_KIND_LABEL: Record<DingTalkDeliveryKind, string> = {
  PAYROLL_REVIEW: '薪酬复核',
  APPEAL_STATUS: '申诉状态',
}

const DELIVERY_STATUS_LABEL: Record<DingTalkDeliveryStatus, string> = {
  PENDING: '待处理',
  SANDBOXED: '已进入沙盒',
  SENT: '已发送',
  FAILED: '处理失败',
}

const APPEAL_STATUS_LABEL: Record<CompAppealStatus, string> = {
  PENDING: '审批中',
  UPHELD: '已维持原结果',
  CORRECTION_REQUIRED: '需要按流程更正',
}

function statusColor(status: DingTalkDeliveryStatus | CompAppealStatus): string {
  if (status === 'FAILED') return 'red'
  if (status === 'SANDBOXED' || status === 'CORRECTION_REQUIRED') return 'orange'
  if (status === 'SENT' || status === 'UPHELD') return 'green'
  return 'blue'
}

function errorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response
    const detail = response?.data?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) =>
          typeof item === 'object' && item !== null && 'msg' in item
            ? (item as { msg?: unknown }).msg
            : undefined,
        )
        .filter((message): message is string => typeof message === 'string')
      if (messages.length) return messages.join('；')
    }
  }
  return '操作失败，请稍后重试。'
}

function canAppealDelivery(delivery: DingTalkDelivery): boolean {
  return delivery.can_appeal === true
}

export default function CompAppealsPage() {
  const { user, hasPermission } = useAuth()
  const queryClient = useQueryClient()
  const queryScope = user?.username ?? 'anonymous'
  const canReview = hasPermission(Perm.PAYROLL_REVIEW)
  const canReadAppeals = canReview || hasPermission(Perm.ADJUSTMENT_READ)
  const canManageNotifications = hasPermission(Perm.NOTIFICATION_MANAGE)
  const canViewDeliveries = canReview || canManageNotifications
  const canAccessPage = canViewDeliveries || canReadAppeals
  const useReviewerModeEndpoint = canReview && !canManageNotifications
  const [batchId, setBatchId] = useState<number | null>(null)
  const [appealTarget, setAppealTarget] = useState<DingTalkDelivery | null>(null)
  const [appealSubmitting, setAppealSubmitting] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [appealForm] = Form.useForm<AppealForm>()
  const modeQuery = useQuery({
    queryKey: ['dingtalkMode', queryScope],
    queryFn: fetchDingTalkMode,
    enabled: useReviewerModeEndpoint,
  })
  const integrationQuery = useQuery({
    queryKey: ['dingtalkIntegration', queryScope],
    queryFn: fetchDingTalkIntegration,
    enabled: canManageNotifications,
  })
  const resolvedNotificationMode = canManageNotifications
    ? integrationQuery.data?.mode
    : modeQuery.data?.mode
  const modeReadError = canManageNotifications
    ? integrationQuery.isError
    : useReviewerModeEndpoint && modeQuery.isError
  const modeReadErrorDetail = canManageNotifications ? integrationQuery.error : modeQuery.error
  const modeReadUnavailable = canManageNotifications
    ? integrationQuery.isLoading ||
      integrationQuery.isFetching ||
      integrationQuery.isError ||
      resolvedNotificationMode === undefined
    : useReviewerModeEndpoint
      ? modeQuery.isLoading ||
        modeQuery.isFetching ||
        modeQuery.isError ||
        resolvedNotificationMode === undefined
      : true
  const notificationMode = modeReadUnavailable ? undefined : resolvedNotificationMode
  const isLive = notificationMode === 'live'

  const deliveriesQuery = useQuery({
    queryKey: ['dingtalkDeliveries', queryScope, batchId],
    queryFn: () => fetchDingTalkDeliveries(batchId ?? undefined),
    enabled: canViewDeliveries,
  })
  const appealsQuery = useQuery({
    queryKey: ['compAppeals', queryScope],
    queryFn: fetchCompAppeals,
    enabled: canReadAppeals,
  })

  const stageMutation = useMutation({
    mutationFn: (selectedBatchId: number) => {
      if (modeReadUnavailable) throw new Error('通知运行模式尚未可靠读取，已禁止发送')
      return stageReviewDeliveries(selectedBatchId)
    },
    onSuccess: async () => {
      setActionError(null)
      await queryClient.invalidateQueries({ queryKey: ['dingtalkDeliveries', queryScope] })
    },
    onError: (error) => setActionError(errorMessage(error)),
  })
  const retryMutation = useMutation({
    mutationFn: (deliveryId: number) => {
      if (modeReadUnavailable) throw new Error('通知运行模式尚未可靠读取，已禁止重试')
      return retryDingTalkDelivery(deliveryId)
    },
    onSuccess: async () => {
      setActionError(null)
      await queryClient.invalidateQueries({ queryKey: ['dingtalkDeliveries', queryScope] })
    },
    onError: (error) => setActionError(errorMessage(error)),
  })
  const connectionMutation = useMutation({
    mutationFn: testDingTalkIntegration,
    onSuccess: (result) => {
      setActionError(null)
      message.success(`钉钉连接正常，token 缓存剩余约 ${result.token_expires_in_seconds} 秒`)
    },
    onError: (error) => setActionError(errorMessage(error)),
  })

  const queryError =
    modeReadError
      ? `通知运行模式加载失败：${errorMessage(modeReadErrorDetail)}`
      : deliveriesQuery.isError && canViewDeliveries
        ? errorMessage(deliveriesQuery.error)
        : appealsQuery.isError && canReadAppeals
          ? errorMessage(appealsQuery.error)
          : null
  const visibleError = actionError ?? queryError

  function closeAppealModal(): void {
    appealForm.resetFields()
    setAppealTarget(null)
  }

  async function submitAppeal(values: AppealForm): Promise<void> {
    if (!appealTarget) return

    setActionError(null)
    setAppealSubmitting(true)
    try {
      await createCompAppeal({
        delivery_id: appealTarget.id,
        reason: values.reason.trim(),
      })
      closeAppealModal()
      await queryClient.invalidateQueries({ queryKey: ['compAppeals', queryScope] })
    } catch (error) {
      setActionError(errorMessage(error))
    } finally {
      setAppealSubmitting(false)
    }
  }

  const deliveryColumns: ColumnsType<DingTalkDelivery> = [
    {
      title: '投递',
      render: (_: unknown, delivery) => '投递 #' + delivery.id,
    },
    { title: '批次', dataIndex: 'batch_id' },
    { title: '版本', dataIndex: 'batch_version' },
    { title: '组织范围', dataIndex: 'org_unit_id' },
    { title: '部门', dataIndex: 'department' },
    {
      title: '类型',
      render: (_: unknown, delivery) => DELIVERY_KIND_LABEL[delivery.kind],
    },
    {
      title: '状态',
      render: (_: unknown, delivery) => (
        <Tag color={statusColor(delivery.status)}>{DELIVERY_STATUS_LABEL[delivery.status]}</Tag>
      ),
    },
    {
      title: '尝试次数',
      dataIndex: 'attempt_count',
    },
    {
      title: '错误代码',
      render: (_: unknown, delivery) => delivery.error_code ?? '—',
    },
    {
      title: '操作',
      render: (_: unknown, delivery) => (
        <Space wrap>
          {canReview && canAppealDelivery(delivery) ? (
            <Button
              size="small"
              onClick={() => {
                appealForm.resetFields()
                setAppealTarget(delivery)
              }}
            >
              发起申诉
            </Button>
          ) : null}
          {canManageNotifications ? (
            <Button
              aria-label={'重试投递 ' + delivery.id}
              size="small"
              loading={retryMutation.isPending && retryMutation.variables === delivery.id}
              disabled={
                modeReadUnavailable ||
                stageMutation.isPending ||
                retryMutation.isPending
              }
              onClick={() => {
                setActionError(null)
                retryMutation.mutate(delivery.id)
              }}
            >
              {notificationMode === 'live'
                ? '重新发送'
                : notificationMode === 'sandbox'
                  ? '沙盒重试'
                  : '等待模式确认'}
            </Button>
          ) : null}
        </Space>
      ),
    },
  ]

  const appealColumns: ColumnsType<CompAppeal> = [
    {
      title: '申诉',
      render: (_: unknown, appeal) => '申诉 #' + appeal.id,
    },
    { title: '关联投递', dataIndex: 'delivery_id' },
    { title: '批次', dataIndex: 'batch_id' },
    { title: '版本', dataIndex: 'batch_version' },
    { title: '组织范围', dataIndex: 'org_unit_id' },
    { title: '部门', dataIndex: 'department' },
    {
      title: '状态',
      render: (_: unknown, appeal) => (
        <Tag color={statusColor(appeal.status)}>{APPEAL_STATUS_LABEL[appeal.status]}</Tag>
      ),
    },
    {
      title: '审批编号',
      render: (_: unknown, appeal) => appeal.approval_instance_id ?? '—',
    },
  ]

  if (!canAccessPage) {
    return (
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          薪酬申诉与钉钉通知
        </Typography.Title>
        <Alert
          type="warning"
          showIcon
          message="薪酬申诉与钉钉通知需要审核、申诉查看或通知管理权限。"
        />
      </Space>
    )
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Typography.Title level={3} style={{ margin: 0 }}>
        薪酬申诉与钉钉通知
      </Typography.Title>
      {canViewDeliveries ? (
        <Alert
          type={modeReadError ? 'error' : isLive ? 'error' : notificationMode ? 'warning' : 'info'}
          showIcon
          message={
            modeReadError
              ? '通知运行模式无法确认，通知操作已停用。'
              : notificationMode === 'live'
                ? '真实推送模式已开启。'
                : notificationMode === 'sandbox'
                  ? '沙盒通知：不会真实发送。'
                  : '正在确认通知运行模式。'
          }
          description="页面只展示投递状态和范围元数据，不展示或缓存薪资金额、人员标识、申诉内容或处理说明。"
        />
      ) : null}
      {canManageNotifications && integrationQuery.data && !modeReadUnavailable ? (
        <Alert
          type="info"
          showIcon
          message={
            integrationQuery.data.credentials_configured
              ? '企业应用凭证已在后端配置'
              : '企业应用凭证尚未配置'
          }
          description={
            integrationQuery.data.ready_for_live
              ? '发送前置配置已齐全；是否发送仍由服务器模式控制。'
              : '仍缺少凭证或钉钉可访问的 HTTPS 申诉地址。'
          }
          action={
            integrationQuery.data.credentials_configured ? (
              <Button
                size="small"
                loading={connectionMutation.isPending}
                onClick={() => connectionMutation.mutate()}
              >
                检测凭证连接
              </Button>
            ) : undefined
          }
        />
      ) : null}
      {visibleError ? (
        <Alert
          type="error"
          showIcon
          closable
          message={visibleError}
          onClose={() => setActionError(null)}
        />
      ) : null}
      {canViewDeliveries ? (
        <Card title="服务器过滤的投递记录">
          <Space wrap style={{ marginBottom: 16 }}>
            <InputNumber
              aria-label="批次 ID"
              min={1}
              placeholder="按批次筛选"
              value={batchId}
              onChange={(value) => setBatchId(typeof value === 'number' ? value : null)}
            />
            <Button
              loading={deliveriesQuery.isFetching}
              onClick={() => void deliveriesQuery.refetch()}
            >
              刷新投递
            </Button>
            {canManageNotifications ? (
              <Button
                aria-label={
                  notificationMode === 'live'
                    ? '手工发送钉钉通知'
                    : notificationMode === 'sandbox'
                      ? '手工分发沙盒通知'
                      : '等待通知模式确认'
                }
                type="primary"
                disabled={
                  modeReadUnavailable || batchId === null || retryMutation.isPending
                }
                loading={stageMutation.isPending}
                onClick={() => {
                  if (batchId === null || modeReadUnavailable) return
                  setActionError(null)
                  stageMutation.mutate(batchId)
                }}
              >
                {notificationMode === 'live'
                  ? '手工发送钉钉通知'
                  : notificationMode === 'sandbox'
                    ? '手工分发沙盒通知'
                    : '等待通知模式确认'}
              </Button>
            ) : null}
          </Space>
          {canManageNotifications ? (
            <Alert
              type="info"
              showIcon
              message="手工分发和重试仅限集团范围的通知管理权限；服务端会再次校验。"
              style={{ marginBottom: 16 }}
            />
          ) : null}
          <Table<DingTalkDelivery>
            rowKey="id"
            size="small"
            loading={deliveriesQuery.isLoading}
            columns={deliveryColumns}
            dataSource={deliveriesQuery.data ?? []}
            pagination={false}
          />
        </Card>
      ) : null}
      {canReadAppeals ? (
        <Card title="服务器过滤的薪酬申诉">
          <Alert
            type="info"
            showIcon
            message="申诉列表不显示申诉理由、处理说明或人员标识。"
            style={{ marginBottom: 16 }}
          />
          <Table<CompAppeal>
            rowKey="id"
            size="small"
            loading={appealsQuery.isLoading}
            columns={appealColumns}
            dataSource={appealsQuery.data ?? []}
            pagination={false}
          />
        </Card>
      ) : null}
      <Modal
        title={'从投递 #' + (appealTarget?.id ?? '') + ' 发起申诉'}
        open={appealTarget !== null}
        okText="提交申诉"
        onCancel={closeAppealModal}
        onOk={() => appealForm.submit()}
        confirmLoading={appealSubmitting}
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          message="请勿填写工资金额、员工姓名、工号或其他个人信息。"
          style={{ marginBottom: 16 }}
        />
        <Form<AppealForm>
          form={appealForm}
          layout="vertical"
          preserve={false}
          autoComplete="off"
          onFinish={(values) => void submitAppeal(values)}
        >
          <Form.Item
            name="reason"
            label="申诉说明"
            rules={[
              { required: true, whitespace: true, message: '请填写申诉说明。' },
              { max: 2000, message: '申诉说明不能超过 2000 个字符。' },
            ]}
          >
            <Input.TextArea aria-label="申诉说明" rows={4} maxLength={2000} autoComplete="off" />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
