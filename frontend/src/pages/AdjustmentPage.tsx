import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useEffect, useMemo, useState } from 'react'

import {
  type ApprovalAction,
  type ApprovalDecision,
  type ApprovalInstance,
  type ApprovalTodo,
  type SalaryAdjustment,
  type SalaryAdjustmentCreateInput,
  createSalaryAdjustment,
  decideApprovalInstance,
  fetchSalaryAdjustment,
  fetchApprovalInstance,
  fetchApprovalTodos,
  fetchSalaryAdjustments,
  submitSalaryAdjustment,
} from '../api/approval'
import { fetchComponents, type SalaryComponent } from '../api/comp'
import { fetchEmployees, type Employee } from '../api/masterdata'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'
import { validateHttpUrl } from '../utils/safeExternalUrl'

interface AdjustmentFormValues {
  employee_id: number
  component_id: number
  amount: number
  effective_from: string
  reason: string
  attachment_url: string
}

interface DecisionFormValues {
  decision: ApprovalDecision
  comment?: string
}

const ADJUSTMENT_STATUS_LABEL: Record<SalaryAdjustment['status'], string> = {
  DRAFT: '草稿',
  PENDING: '审批中',
  APPROVED: '已批准',
  REJECTED: '已驳回',
  CANCELLED: '已取消',
}

const INSTANCE_STATUS_LABEL: Record<ApprovalInstance['status'], string> = {
  PENDING: '审批中',
  APPROVED: '已批准',
  REJECTED: '已驳回',
  CANCELLED: '已取消',
}

const ACTION_LABEL: Record<ApprovalAction['action'], string> = {
  APPROVE: '同意',
  REJECT: '驳回',
  CANCEL: '取消',
}

const PENDING_DRAFT_STORAGE_PREFIX = 'salary-adjustment-pending-draft:'

function pendingDraftStorageKey(queryScope: string): string {
  return `${PENDING_DRAFT_STORAGE_PREFIX}${queryScope}`
}

function loadPendingDraftId(queryScope: string): number | null {
  if (typeof window === 'undefined') return null
  try {
    const id = Number(window.sessionStorage.getItem(pendingDraftStorageKey(queryScope)))
    return Number.isSafeInteger(id) && id > 0 ? id : null
  } catch {
    return null
  }
}

function savePendingDraftId(queryScope: string, id: number): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.setItem(pendingDraftStorageKey(queryScope), String(id))
  } catch {
    // A browser may deny session storage; the in-memory retry state still works.
  }
}

function removePendingDraftId(queryScope: string): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.removeItem(pendingDraftStorageKey(queryScope))
  } catch {
    // A browser may deny session storage; there is nothing else to clear.
  }
}

function today(): string {
  const now = new Date()
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 10)
}

function optionalText(value: string | undefined): string | undefined {
  const trimmed = value?.trim()
  return trimmed || undefined
}

function formatAmount(value: string | number): string {
  const amount = Number(value)
  return Number.isFinite(amount)
    ? amount.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : String(value)
}

function errorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response
    const detail = response?.data?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      const messages = detail
        .map((entry) => {
          if (typeof entry === 'object' && entry !== null && 'msg' in entry) {
            const message = (entry as { msg?: unknown }).msg
            return typeof message === 'string' ? message : null
          }
          return null
        })
        .filter((entry): entry is string => entry !== null)
      if (messages.length) return messages.join('；')
    }
  }
  return '操作失败，请稍后重试'
}

function isNotFound(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'response' in error &&
    (error as { response?: { status?: number } }).response?.status === 404
  )
}

function statusTag(status: SalaryAdjustment['status'] | ApprovalInstance['status']) {
  const color =
    status === 'APPROVED'
      ? 'green'
      : status === 'REJECTED' || status === 'CANCELLED'
        ? 'red'
        : status === 'PENDING'
          ? 'orange'
          : 'default'
  const label =
    status in ADJUSTMENT_STATUS_LABEL
      ? ADJUSTMENT_STATUS_LABEL[status as SalaryAdjustment['status']]
      : INSTANCE_STATUS_LABEL[status as ApprovalInstance['status']]
  return <Tag color={color}>{label}</Tag>
}

export default function AdjustmentPage() {
  const { user, hasPermission } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const canRead = hasPermission(Perm.ADJUSTMENT_READ)
  const canCreate = hasPermission(Perm.ADJUSTMENT_CREATE)
  const canApprove = hasPermission(Perm.ADJUSTMENT_APPROVE)
  const queryClient = useQueryClient()
  const [employeeSearch, setEmployeeSearch] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [pendingDraftId, setPendingDraftId] = useState<number | null>(() =>
    loadPendingDraftId(queryScope),
  )
  const [detailInstanceId, setDetailInstanceId] = useState<number | null>(null)
  const [decisionTarget, setDecisionTarget] = useState<ApprovalTodo | null>(null)
  const [adjustmentForm] = Form.useForm<AdjustmentFormValues>()
  const [decisionForm] = Form.useForm<DecisionFormValues>()

  useEffect(() => {
    setEmployeeSearch('')
    setCreateOpen(false)
    setPendingDraftId(loadPendingDraftId(queryScope))
    setDetailInstanceId(null)
    setDecisionTarget(null)
    adjustmentForm.resetFields()
    decisionForm.resetFields()
  }, [adjustmentForm, decisionForm, queryScope])

  const adjustmentsQuery = useQuery({
    queryKey: ['salaryAdjustments', queryScope],
    queryFn: () => fetchSalaryAdjustments(),
    enabled: canRead,
  })
  const todosQuery = useQuery({
    queryKey: ['approvalTodos', queryScope],
    queryFn: fetchApprovalTodos,
    enabled: canApprove,
  })
  const employeesQuery = useQuery({
    queryKey: ['adjustmentEmployees', queryScope, employeeSearch],
    queryFn: async (): Promise<Employee[]> => {
      const keyword = employeeSearch.trim()
      if (!keyword) return (await fetchEmployees({ page_size: 50 })).items

      const [byName, byEmpNo] = await Promise.all([
        fetchEmployees({ name: keyword, page_size: 50 }),
        fetchEmployees({ emp_no: keyword, page_size: 50 }),
      ])
      return Array.from(
        new Map(
          [...byName.items, ...byEmpNo.items].map((employee) => [employee.id, employee]),
        ).values(),
      )
    },
    enabled: canCreate,
  })
  const componentsQuery = useQuery({
    queryKey: ['adjustmentComponents', queryScope],
    queryFn: fetchComponents,
    enabled: canCreate,
  })
  const detailQuery = useQuery({
    queryKey: ['approvalInstance', queryScope, detailInstanceId],
    queryFn: () => fetchApprovalInstance(detailInstanceId!),
    enabled: detailInstanceId !== null,
  })
  const pendingDraftQuery = useQuery({
    queryKey: ['salaryAdjustmentDraft', queryScope, pendingDraftId],
    queryFn: () => fetchSalaryAdjustment(pendingDraftId!),
    enabled: pendingDraftId !== null,
    retry: false,
  })
  const createSourceUnavailable =
    employeesQuery.isLoading ||
    employeesQuery.isFetching ||
    employeesQuery.isError ||
    componentsQuery.isLoading ||
    componentsQuery.isFetching ||
    componentsQuery.isError
  const approvalTodosUnavailable =
    todosQuery.isLoading || todosQuery.isFetching || todosQuery.isError
  const pendingDraftMissing = pendingDraftQuery.isError && isNotFound(pendingDraftQuery.error)

  useEffect(() => {
    if (pendingDraftQuery.data && pendingDraftQuery.data.status !== 'DRAFT') {
      setPendingDraftId(null)
      removePendingDraftId(queryScope)
    }
  }, [pendingDraftQuery.data, queryScope])

  const employeeById = useMemo(
    () => new Map((employeesQuery.data ?? []).map((employee) => [employee.id, employee])),
    [employeesQuery.data],
  )
  const componentById = useMemo(
    () => new Map((componentsQuery.data ?? []).map((component) => [component.id, component])),
    [componentsQuery.data],
  )

  const refreshWorkflow = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['salaryAdjustments', queryScope] }),
      queryClient.invalidateQueries({ queryKey: ['approvalTodos', queryScope] }),
      queryClient.invalidateQueries({ queryKey: ['approvalInstance', queryScope] }),
    ])
  }

  const closeCreate = () => {
    adjustmentForm.resetFields()
    setCreateOpen(false)
  }
  const openCreate = () => {
    if (createSourceUnavailable) return
    adjustmentForm.resetFields()
    adjustmentForm.setFieldsValue({ effective_from: today() })
    setCreateOpen(true)
  }
  const closeDecision = () => {
    decisionForm.resetFields()
    setDecisionTarget(null)
  }
  const openDecision = (todo: ApprovalTodo) => {
    if (approvalTodosUnavailable) return
    decisionForm.resetFields()
    decisionForm.setFieldsValue({ decision: 'APPROVE' })
    setDecisionTarget(todo)
  }

  const rememberPendingDraft = (id: number) => {
    setPendingDraftId(id)
    savePendingDraftId(queryScope, id)
  }
  const clearPendingDraft = (id?: number) => {
    if (id !== undefined && pendingDraftId !== id) return
    setPendingDraftId(null)
    removePendingDraftId(queryScope)
  }

  const submitMutation = useMutation({
    mutationFn: submitSalaryAdjustment,
    onSuccess: async (_adjustment, id) => {
      clearPendingDraft(id)
      message.success('调薪申请已提交审批')
      await refreshWorkflow()
    },
    onError: async (error, id) => {
      message.error(`草稿 #${id} 已创建，但提交审批失败：${errorMessage(error)}`)
      await refreshWorkflow()
      await queryClient.invalidateQueries({
        queryKey: ['salaryAdjustmentDraft', queryScope, id],
      })
    },
  })

  const createMutation = useMutation({
    mutationFn: (values: AdjustmentFormValues) => {
      if (createSourceUnavailable) throw new Error('调薪申请来源尚未完整读取')
      const payload: SalaryAdjustmentCreateInput = {
        ...values,
        reason: values.reason.trim(),
        attachment_url: values.attachment_url.trim(),
      }
      return createSalaryAdjustment(payload)
    },
    onSuccess: async (draft) => {
      rememberPendingDraft(draft.id)
      closeCreate()
      await refreshWorkflow()
      submitMutation.mutate(draft.id)
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const decisionMutation = useMutation({
    mutationFn: ({ instanceId, values }: { instanceId: number; values: DecisionFormValues }) => {
      if (approvalTodosUnavailable) throw new Error('审批待办尚未完整读取')
      return decideApprovalInstance(instanceId, {
        decision: values.decision,
        comment: optionalText(values.comment),
      })
    },
    onSuccess: async () => {
      message.success('审批决定已提交')
      closeDecision()
      await refreshWorkflow()
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const applicationColumns: TableProps<SalaryAdjustment>['columns'] = [
    { title: '申请编号', dataIndex: 'id' },
    {
      title: '员工',
      dataIndex: 'employee_id',
      render: (employeeId: number) => {
        const employee = employeeById.get(employeeId)
        return employee ? `${employee.emp_no} · ${employee.name}` : `员工 #${employeeId}`
      },
    },
    {
      title: '薪资组件',
      dataIndex: 'component_id',
      render: (componentId: number) => {
        const component = componentById.get(componentId)
        return component ? `${component.code} · ${component.name}` : `组件 #${componentId}`
      },
    },
    { title: '调整金额', dataIndex: 'amount', render: formatAmount },
    { title: '生效日期', dataIndex: 'effective_from' },
    {
      title: '状态',
      dataIndex: 'status',
      render: (status: SalaryAdjustment['status']) => statusTag(status),
    },
    { title: '原因', dataIndex: 'reason', ellipsis: true },
    {
      title: '审批轨迹',
      dataIndex: 'approval_instance_id',
      render: (instanceId: number | null) =>
        instanceId === null ? (
          '尚未提交'
        ) : (
          <Button size="small" onClick={() => setDetailInstanceId(instanceId)}>
            查看轨迹
          </Button>
        ),
    },
  ]

  const todoColumns: TableProps<ApprovalTodo>['columns'] = [
    { title: '审批编号', dataIndex: 'id' },
    { title: '调薪申请', dataIndex: 'business_id', render: (id: number) => `#${id}` },
    { title: '组织', dataIndex: 'org_unit_id', render: (id: number) => `组织 #${id}` },
    { title: '金额', dataIndex: 'amount', render: formatAmount },
    { title: '当前步骤', dataIndex: 'current_step_name' },
    { title: '步骤序号', dataIndex: 'current_step_order' },
    {
      title: '操作',
      key: 'actions',
      render: (_: unknown, todo: ApprovalTodo) => (
        <Space>
          <Button
            size="small"
            disabled={approvalTodosUnavailable}
            onClick={() => setDetailInstanceId(todo.id)}
          >
            轨迹
          </Button>
          <Button
            size="small"
            type="primary"
            disabled={approvalTodosUnavailable}
            onClick={() => openDecision(todo)}
          >
            审批
          </Button>
        </Space>
      ),
    },
  ]

  const actionColumns: TableProps<ApprovalAction>['columns'] = [
    { title: '步骤', dataIndex: 'step_order' },
    {
      title: '决定',
      dataIndex: 'action',
      render: (action: ApprovalAction['action']) => (
        <Tag color={action === 'APPROVE' ? 'green' : action === 'REJECT' ? 'red' : 'default'}>
          {ACTION_LABEL[action]}
        </Tag>
      ),
    },
    { title: '处理人', dataIndex: 'actor_id', render: (id: number) => `用户 #${id}` },
    { title: '意见', dataIndex: 'comment', render: (comment: string | null) => comment ?? '—' },
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          调薪申请与审批
        </Typography.Title>
        {canCreate && (
          <Button
            type="primary"
            disabled={
              pendingDraftId !== null || submitMutation.isPending || createSourceUnavailable
            }
            onClick={openCreate}
          >
            发起调薪申请
          </Button>
        )}
      </Space>

      {!canRead && (
        <Alert type="info" showIcon message="当前账号可发起调薪申请；提交后由审批流程处理。" />
      )}
      {canCreate && employeesQuery.isError && (
        <Alert type="error" showIcon message="无法加载员工目录，已停用调薪申请创建。" />
      )}
      {canCreate && componentsQuery.isError && (
        <Alert type="error" showIcon message="无法加载薪资组件，已停用调薪申请创建。" />
      )}
      {pendingDraftId !== null && (
        <Alert
          type="warning"
          showIcon
          message={`调薪草稿 #${pendingDraftId} 尚未提交审批`}
          description={
            pendingDraftMissing
              ? '服务器已确认该草稿不存在，可清除本地失效记录后重新创建。'
              : pendingDraftQuery.isError
                ? '无法确认草稿状态；请重试提交，后端会校验草稿状态。'
                : '草稿编号已保留，可在本次会话或刷新页面后继续提交。完成当前草稿前不能新建申请。'
          }
          action={
            pendingDraftMissing ? (
              <Button size="small" danger onClick={() => clearPendingDraft(pendingDraftId)}>
                清除失效记录
              </Button>
            ) : (
              <Button
                size="small"
                type="primary"
                loading={submitMutation.isPending}
                disabled={
                  pendingDraftQuery.isLoading ||
                  pendingDraftQuery.isFetching ||
                  pendingDraftQuery.isError
                }
                onClick={() => submitMutation.mutate(pendingDraftId)}
              >
                重新提交
              </Button>
            )
          }
        />
      )}
      {canRead && !canCreate && (
        <Alert type="info" showIcon message="当前账号可查看调薪申请，但不能发起新的调薪申请。" />
      )}
      {canRead && adjustmentsQuery.isError && (
        <Alert
          type="error"
          showIcon
          message="无法加载调薪申请"
          description={errorMessage(adjustmentsQuery.error)}
        />
      )}
      {canRead && (
        <Card title="我的 / 可见调薪申请">
          <Table<SalaryAdjustment>
            rowKey="id"
            loading={adjustmentsQuery.isLoading || adjustmentsQuery.isFetching}
            columns={applicationColumns}
            dataSource={adjustmentsQuery.data ?? []}
            pagination={{ pageSize: 20 }}
            locale={{ emptyText: '暂无可见调薪申请' }}
          />
        </Card>
      )}

      {canApprove && (
        <Card title="我的审批待办">
          {todosQuery.isError && (
            <Alert
              type="error"
              showIcon
              message="无法加载审批待办"
              description={errorMessage(todosQuery.error)}
              style={{ marginBottom: 16 }}
            />
          )}
          <Table<ApprovalTodo>
            rowKey="id"
            loading={todosQuery.isLoading || todosQuery.isFetching}
            columns={todoColumns}
            dataSource={todosQuery.data ?? []}
            pagination={false}
            locale={{ emptyText: '当前没有待处理审批' }}
          />
        </Card>
      )}

      <Modal
        title="发起调薪申请"
        open={createOpen}
        onCancel={closeCreate}
        onOk={() => adjustmentForm.submit()}
        confirmLoading={createMutation.isPending}
        okButtonProps={{ disabled: createSourceUnavailable }}
        forceRender
        destroyOnHidden
      >
        <Form
          form={adjustmentForm}
          layout="vertical"
          preserve={false}
          onFinish={(values: AdjustmentFormValues) => createMutation.mutate(values)}
        >
          <Alert
            type="info"
            showIcon
            message="提交后会进入审批流程；审批通过前不会修改薪资结构。"
            style={{ marginBottom: 16 }}
          />
          <Form.Item
            name="employee_id"
            label="员工"
            rules={[{ required: true, message: '请选择员工' }]}
          >
            <Select
              showSearch
              filterOption={false}
              loading={employeesQuery.isLoading || employeesQuery.isFetching}
              disabled={employeesQuery.isError}
              onSearch={setEmployeeSearch}
              options={(employeesQuery.data ?? []).map((employee) => ({
                value: employee.id,
                label: `${employee.emp_no} · ${employee.name}`,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="component_id"
            label="薪资组件"
            rules={[{ required: true, message: '请选择薪资组件' }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              loading={componentsQuery.isLoading || componentsQuery.isFetching}
              disabled={componentsQuery.isError}
              options={(componentsQuery.data ?? []).map((component: SalaryComponent) => ({
                value: component.id,
                label: `${component.code} · ${component.name}`,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="amount"
            label="调整后金额"
            rules={[
              { required: true, message: '请输入调整后金额' },
              { type: 'number', min: 0, message: '金额不能小于 0' },
              { type: 'number', max: 999_999_999_999.99, message: '金额超出允许范围' },
            ]}
          >
            <InputNumber min={0} max={999_999_999_999.99} precision={2} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            name="effective_from"
            label="生效日期"
            rules={[{ required: true, message: '请选择生效日期' }]}
          >
            <Input type="date" />
          </Form.Item>
          <Form.Item
            name="reason"
            label="调薪原因"
            rules={[{ required: true, whitespace: true, max: 2000, message: '请填写调薪原因' }]}
          >
            <Input.TextArea rows={3} maxLength={2000} showCount />
          </Form.Item>
          <Form.Item
            name="attachment_url"
            label="证明附件地址"
            rules={[
              { required: true, whitespace: true, message: '请填写证明附件地址' },
              { max: 512 },
              { validator: validateHttpUrl },
            ]}
          >
            <Input maxLength={512} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={detailInstanceId === null ? '审批轨迹' : `审批轨迹 #${detailInstanceId}`}
        open={detailInstanceId !== null}
        onCancel={() => setDetailInstanceId(null)}
        footer={null}
        destroyOnHidden
      >
        {detailQuery.isLoading && <Typography.Text>正在加载审批轨迹…</Typography.Text>}
        {detailQuery.isError && (
          <Alert
            type="error"
            showIcon
            message="无法加载审批轨迹"
            description={errorMessage(detailQuery.error)}
          />
        )}
        {detailQuery.data && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Descriptions bordered size="small" column={1}>
              <Descriptions.Item label="状态">
                {statusTag(detailQuery.data.status)}
              </Descriptions.Item>
              <Descriptions.Item label="调薪申请">
                #{detailQuery.data.business_id}
              </Descriptions.Item>
              <Descriptions.Item label="申请人">
                用户 #{detailQuery.data.requester_id}
              </Descriptions.Item>
              <Descriptions.Item label="金额">
                {formatAmount(detailQuery.data.amount)}
              </Descriptions.Item>
              <Descriptions.Item label="当前步骤">
                {detailQuery.data.current_step_order ?? '已结束'}
              </Descriptions.Item>
            </Descriptions>
            <div>
              <Typography.Text strong>审批步骤</Typography.Text>
              <Table
                rowKey="step_order"
                size="small"
                pagination={false}
                dataSource={detailQuery.data.flow_snapshot.steps ?? []}
                columns={[
                  { title: '步骤', dataIndex: 'step_order' },
                  { title: '名称', dataIndex: 'name' },
                  { title: '审批角色', dataIndex: 'role_code' },
                ]}
              />
            </div>
            <div>
              <Typography.Text strong>审批处理记录</Typography.Text>
              <Table<ApprovalAction>
                rowKey="step_order"
                size="small"
                pagination={false}
                dataSource={detailQuery.data.actions}
                columns={actionColumns}
                locale={{ emptyText: '尚无处理记录' }}
              />
            </div>
          </Space>
        )}
      </Modal>

      <Modal
        title={
          decisionTarget === null ? '审批调薪申请' : `审批调薪申请 #${decisionTarget.business_id}`
        }
        open={decisionTarget !== null}
        onCancel={closeDecision}
        onOk={() => decisionForm.submit()}
        confirmLoading={decisionMutation.isPending}
        okButtonProps={{ disabled: approvalTodosUnavailable }}
        forceRender
        destroyOnHidden
      >
        {decisionTarget && (
          <Alert
            type="info"
            showIcon
            message={`当前步骤：${decisionTarget.current_step_name}（第 ${decisionTarget.current_step_order} 步）`}
            style={{ marginBottom: 16 }}
          />
        )}
        <Form
          form={decisionForm}
          layout="vertical"
          preserve={false}
          onFinish={(values: DecisionFormValues) => {
            if (decisionTarget) {
              decisionMutation.mutate({ instanceId: decisionTarget.id, values })
            }
          }}
        >
          <Form.Item
            name="decision"
            label="审批决定"
            rules={[{ required: true, message: '请选择审批决定' }]}
          >
            <Select
              options={[
                { value: 'APPROVE', label: '同意' },
                { value: 'REJECT', label: '驳回' },
              ]}
            />
          </Form.Item>
          <Form.Item name="comment" label="审批意见（可选）" rules={[{ max: 2000 }]}>
            <Input.TextArea rows={3} maxLength={2000} showCount />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
