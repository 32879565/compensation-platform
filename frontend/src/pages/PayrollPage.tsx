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
  Popconfirm,
  Select,
  Space,
  Steps,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { useEffect, useState } from 'react'

import {
  type AttendanceChanges,
  type AttendanceField,
  type BatchConfirmation,
  type BatchCreateInput,
  type CalculationStatus,
  type Department,
  type DisputeCorrectionOption,
  type HrReviewStatus,
  type LockStatus,
  type PayrollAdjustment,
  type PayrollDispute,
  type PayrollResult,
  type ResolveDecision,
  type SourceCorrection,
  type StoreConfirmationStatus,
  approveBatch,
  confirmScope,
  createBatch,
  createDispute,
  fetchAdjustments,
  fetchBatches,
  fetchConfirmations,
  fetchDisputes,
  fetchResults,
  lockBatch,
  reopenBatch,
  resolveDispute,
  runBatch,
  supplementDispute,
  unlockBatch,
} from '../api/batches'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'
import { safeHttpUrl, validateHttpUrl } from '../utils/safeExternalUrl'

type CreateBatchForm = BatchCreateInput

interface DisputeForm {
  salary_item: string
  opinion: string
}

interface ResolveForm {
  decision: ResolveDecision
  resolution: string
  attachment_url?: string
  expected_days?: number
  actual_days?: number
  worked_hours?: number
  rest_days?: number
  overtime_hours?: number
  holiday_date?: string
  holiday_worked?: boolean
  performance_coefficient?: number
  performance_score?: number | null
  performance_remark?: string
  monthly_amount?: number
  taxable?: boolean
  in_social_base?: boolean
  in_housing_base?: boolean
  structure_component_id?: number
  structure_amount?: number
}

interface SupplementForm {
  note: string
  attachment_url: string
}

interface UnlockForm {
  reason: string
}

const DEPARTMENT_LABEL: Record<Department, string> = {
  DINING: '厅面',
  KITCHEN: '厨房',
  OTHER: '其他',
}

const STATUS_LABEL: Record<string, string> = {
  DRAFT: '草稿',
  CALCULATING: '核算中',
  PENDING_STORE_CONFIRM: '待门店确认',
  HAS_DISPUTE: '存在异议',
  PENDING_HR: '待人事处理',
  CONFIRMED: '已确认',
  LOCKED: '已锁定',
  PENDING: '待确认',
  DISPUTED: '存在异议',
  OPEN: '待处理',
  APPROVED: '已同意',
  REJECTED: '已驳回',
  NEED_MORE: '待补充材料',
}

const CALCULATION_STATUS_LABEL: Record<CalculationStatus, string> = {
  PENDING: '待核算',
  CALCULATING: '核算中',
  CALCULATED: '已核算',
}

const STORE_STATUS_LABEL: Record<StoreConfirmationStatus, string> = {
  NOT_STARTED: '未开始',
  PENDING: '待门店确认',
  HAS_DISPUTE: '存在异议',
  CONFIRMED: '已确认',
}

const HR_STATUS_LABEL: Record<HrReviewStatus, string> = {
  NOT_STARTED: '未开始',
  PENDING: '待人事处理',
  APPROVED: '已审核',
}

const LOCK_STATUS_LABEL: Record<LockStatus, string> = {
  UNLOCKED: '未锁定',
  LOCKED: '已锁定',
}

const ADJUSTMENT_ITEM_LABEL: Record<string, string> = {
  ACTUAL_ATTENDANCE_DAYS: '实际出勤天数',
  EXPECTED_ATTENDANCE_DAYS: '应出勤天数',
  ATTEND_WAGE: '出勤工资',
  OVERTIME: '加班工资',
  LEGAL_HOLIDAY: '法定节假日工资',
  HOLIDAY_WORK_SOURCE: '法定节假日出勤记录',
  ALLOWANCE: '补贴',
  HOUSING_ALLOWANCE: '房补',
}

const DISPUTE_EVENT_LABEL: Record<string, string> = {
  RAISED: '提交异议',
  NEED_MORE: '要求补充材料',
  SUPPLEMENTED: '已补充材料',
  APPROVED: '同意异议',
  REJECTED: '驳回异议',
}

function formatDateTime(value: string | null): string {
  if (!value) return '—'
  const offset = value.endsWith('Z') ? ' UTC' : (value.match(/[+-]\d{2}:\d{2}$/)?.[0] ?? '')
  return `${value.slice(0, 16).replace('T', ' ')}${offset}`
}

function processTag(label: string, state: 'wait' | 'process' | 'finish' | 'error') {
  const color =
    state === 'finish'
      ? 'green'
      : state === 'error'
        ? 'orange'
        : state === 'process'
          ? 'blue'
          : 'default'
  return <Tag color={color}>{label}</Tag>
}

function calculationStepStatus(status: CalculationStatus): 'wait' | 'process' | 'finish' {
  if (status === 'CALCULATED') return 'finish'
  return status === 'CALCULATING' ? 'process' : 'wait'
}

function storeStepStatus(status: StoreConfirmationStatus): 'wait' | 'process' | 'finish' | 'error' {
  if (status === 'CONFIRMED') return 'finish'
  if (status === 'HAS_DISPUTE') return 'error'
  return status === 'PENDING' ? 'process' : 'wait'
}

function hrStepStatus(status: HrReviewStatus): 'wait' | 'process' | 'finish' {
  if (status === 'APPROVED') return 'finish'
  return status === 'PENDING' ? 'process' : 'wait'
}

function lockStepStatus(status: LockStatus): 'wait' | 'finish' {
  return status === 'LOCKED' ? 'finish' : 'wait'
}

function auditValue(value: Record<string, unknown>): string {
  const entries = Object.entries(value)
  if (entries.length === 0) return '—'
  return entries
    .map(
      ([key, item]) =>
        `${key}: ${typeof item === 'object' && item !== null ? JSON.stringify(item) : String(item)}`,
    )
    .join('；')
}

function recomputeLabel(value: Record<string, unknown> | null): string {
  if (!value) return '未记录'
  const version = typeof value.batch_version === 'number' ? ` · v${value.batch_version}` : ''
  if (value.status === 'COMPLETED') return `已重算${version}`
  if (value.status === 'PENDING_RERUN') return `待重算${version}`
  return auditValue(value)
}

function correctionOptionFor(dispute: PayrollDispute | null): DisputeCorrectionOption | null {
  if (!dispute) return null
  const option = dispute.correction_options?.[0]
  if (option) return option
  if (dispute.allowed_attendance_fields?.length) {
    return {
      kind: 'ATTENDANCE',
      label: '考勤源数据',
      fields: dispute.allowed_attendance_fields,
    }
  }
  return null
}

function statusTag(status: string) {
  const color =
    status === 'LOCKED' || status === 'CONFIRMED'
      ? 'green'
      : status === 'HAS_DISPUTE' || status === 'DISPUTED' || status === 'OPEN'
        ? 'orange'
        : status === 'DRAFT'
          ? 'default'
          : 'blue'
  return <Tag color={color}>{STATUS_LABEL[status] ?? status}</Tag>
}

function operationError(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response
    if (typeof response?.data?.detail === 'string') return response.data.detail
  }
  return '操作失败，请稍后重试'
}

function makeAttendanceChanges(
  values: ResolveForm,
  allowedFields: readonly AttendanceField[],
): AttendanceChanges {
  const possible: AttendanceChanges = {
    expected_days: values.expected_days,
    actual_days: values.actual_days,
    worked_hours: values.worked_hours,
    rest_days: values.rest_days,
    overtime_hours: values.overtime_hours,
  }
  return Object.fromEntries(
    Object.entries(possible).filter(
      ([field, value]) =>
        allowedFields.includes(field as AttendanceField) && value !== undefined && value !== null,
    ),
  ) as AttendanceChanges
}

function makeSourceCorrection(
  values: ResolveForm,
  option: Exclude<DisputeCorrectionOption, { kind: 'ATTENDANCE' | 'WORKFLOW' }>,
): SourceCorrection {
  if (option.kind === 'HOLIDAY_WORK') {
    if (!values.holiday_date || typeof values.holiday_worked !== 'boolean') {
      throw new Error('请选择法定节假日及更正后的出勤状态')
    }
    return {
      kind: 'HOLIDAY_WORK',
      holiday_date: values.holiday_date,
      worked: values.holiday_worked,
    }
  }
  if (option.kind === 'PERFORMANCE') {
    if (values.performance_coefficient === undefined) throw new Error('请填写绩效系数')
    return {
      kind: 'PERFORMANCE',
      coefficient: values.performance_coefficient,
      score: values.performance_score ?? null,
      remark: values.performance_remark?.trim() || null,
    }
  }
  if (option.kind === 'MONTHLY_ADJUSTMENT') {
    if (
      values.monthly_amount === undefined ||
      typeof values.taxable !== 'boolean' ||
      typeof values.in_social_base !== 'boolean' ||
      typeof values.in_housing_base !== 'boolean'
    ) {
      throw new Error('请完整填写月度补发/补扣来源及计税分类')
    }
    return {
      kind: 'MONTHLY_ADJUSTMENT',
      amount: values.monthly_amount,
      taxable: values.taxable,
      in_social_base: values.in_social_base,
      in_housing_base: values.in_housing_base,
    }
  }
  if (values.structure_component_id === undefined || values.structure_amount === undefined) {
    throw new Error('请选择薪资组件并填写更正金额')
  }
  return {
    kind: 'SALARY_STRUCTURE',
    component_id: values.structure_component_id,
    amount: values.structure_amount,
  }
}

export default function PayrollPage() {
  const { user, hasPermission } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const canRun = hasPermission(Perm.PAYROLL_RUN)
  const canReview = hasPermission(Perm.PAYROLL_REVIEW)
  const canApprove = hasPermission(Perm.PAYROLL_APPROVE)
  const canCorrect = hasPermission(Perm.PAYROLL_CORRECT)
  const queryClient = useQueryClient()
  const [selectedBatchId, setSelectedBatchId] = useState<number | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [disputeTarget, setDisputeTarget] = useState<PayrollResult | null>(null)
  const [resolveTarget, setResolveTarget] = useState<PayrollDispute | null>(null)
  const [supplementTarget, setSupplementTarget] = useState<PayrollDispute | null>(null)
  const [unlockOpen, setUnlockOpen] = useState(false)
  const [correctionMode, setCorrectionMode] = useState<'unlock' | 'reopen'>('unlock')
  const [resolveAttendanceError, setResolveAttendanceError] = useState<string | null>(null)
  const [createForm] = Form.useForm<CreateBatchForm>()
  const [disputeForm] = Form.useForm<DisputeForm>()
  const [resolveForm] = Form.useForm<ResolveForm>()
  const [supplementForm] = Form.useForm<SupplementForm>()
  const [unlockForm] = Form.useForm<UnlockForm>()
  const resolutionDecision = Form.useWatch('decision', resolveForm)
  const resolveCorrectionOption = correctionOptionFor(resolveTarget)
  const allowedResolveAttendanceFields =
    resolveCorrectionOption?.kind === 'ATTENDANCE' ? resolveCorrectionOption.fields : []
  const canApproveResolveTarget =
    resolveCorrectionOption !== null &&
    resolveCorrectionOption.kind !== 'WORKFLOW' &&
    (resolveCorrectionOption.kind !== 'ATTENDANCE' || allowedResolveAttendanceFields.length > 0)

  const batchesQuery = useQuery({
    queryKey: ['payrollBatches', queryScope],
    queryFn: fetchBatches,
  })
  const selectedBatch = batchesQuery.data?.find((batch) => batch.id === selectedBatchId) ?? null
  const canRaiseDispute =
    canReview &&
    (selectedBatch?.status === 'PENDING_STORE_CONFIRM' || selectedBatch?.status === 'HAS_DISPUTE')
  const canConfirmScope = canReview && selectedBatch?.status === 'PENDING_STORE_CONFIRM'

  const closeCreate = () => {
    createForm.resetFields()
    setCreateOpen(false)
  }
  const closeDispute = () => {
    disputeForm.resetFields()
    setDisputeTarget(null)
  }
  const resetResolveForm = () => {
    resolveForm.resetFields()
    resolveForm.setFieldsValue({ decision: 'REJECTED' })
    setResolveAttendanceError(null)
  }
  const openResolve = (dispute: PayrollDispute) => {
    resetResolveForm()
    const option = correctionOptionFor(dispute)
    if (option?.kind === 'HOLIDAY_WORK') {
      const first = option.holiday_dates[0]
      resolveForm.setFieldsValue({
        holiday_date: first?.holiday_date,
        holiday_worked: first?.worked,
      })
    } else if (option?.kind === 'PERFORMANCE') {
      resolveForm.setFieldsValue({
        performance_coefficient: Number(option.coefficient),
        performance_score: option.score === null ? null : Number(option.score),
        performance_remark: option.remark ?? undefined,
      })
    } else if (option?.kind === 'MONTHLY_ADJUSTMENT') {
      resolveForm.setFieldsValue({
        monthly_amount: Number(option.amount),
        taxable: option.taxable,
        in_social_base: option.in_social_base,
        in_housing_base: option.in_housing_base,
      })
    } else if (option?.kind === 'SALARY_STRUCTURE') {
      const first = option.components[0]
      resolveForm.setFieldsValue({
        structure_component_id: first?.component_id,
        structure_amount: first ? Number(first.amount) : undefined,
      })
    }
    setResolveTarget(dispute)
  }
  const closeResolve = () => {
    resetResolveForm()
    setResolveTarget(null)
  }
  const closeSupplement = () => {
    supplementForm.resetFields()
    setSupplementTarget(null)
  }
  const closeUnlock = () => {
    unlockForm.resetFields()
    setUnlockOpen(false)
  }

  useEffect(() => {
    setSelectedBatchId(null)
  }, [queryScope])

  useEffect(() => {
    if (selectedBatchId === null && batchesQuery.data?.length) {
      setSelectedBatchId(batchesQuery.data[0].id)
    }
  }, [batchesQuery.data, selectedBatchId])

  const resultsQuery = useQuery({
    queryKey: ['payrollResults', queryScope, selectedBatchId],
    queryFn: () => fetchResults(selectedBatchId!),
    enabled: selectedBatchId !== null,
  })
  const confirmationsQuery = useQuery({
    queryKey: ['payrollConfirmations', queryScope, selectedBatchId],
    queryFn: () => fetchConfirmations(selectedBatchId!),
    enabled: selectedBatchId !== null,
  })
  const disputesQuery = useQuery({
    queryKey: ['payrollDisputes', queryScope, selectedBatchId],
    queryFn: () => fetchDisputes(selectedBatchId!),
    enabled: selectedBatchId !== null,
  })
  const adjustmentsQuery = useQuery({
    queryKey: ['payrollAdjustments', queryScope, selectedBatchId],
    queryFn: () => fetchAdjustments(selectedBatchId!),
    enabled: selectedBatchId !== null,
  })
  const batchReadUnavailable =
    batchesQuery.isLoading || batchesQuery.isFetching || batchesQuery.isError
  const reviewReadsUnavailable =
    batchReadUnavailable ||
    resultsQuery.isLoading ||
    resultsQuery.isFetching ||
    resultsQuery.isError ||
    confirmationsQuery.isLoading ||
    confirmationsQuery.isFetching ||
    confirmationsQuery.isError ||
    disputesQuery.isLoading ||
    disputesQuery.isFetching ||
    disputesQuery.isError
  const disputeResolveUnavailable =
    batchReadUnavailable ||
    disputesQuery.isLoading ||
    disputesQuery.isFetching ||
    disputesQuery.isError

  const refreshBatch = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['payrollBatches', queryScope] }),
      queryClient.invalidateQueries({ queryKey: ['payrollResults', queryScope, selectedBatchId] }),
      queryClient.invalidateQueries({
        queryKey: ['payrollConfirmations', queryScope, selectedBatchId],
      }),
      queryClient.invalidateQueries({ queryKey: ['payrollDisputes', queryScope, selectedBatchId] }),
      queryClient.invalidateQueries({
        queryKey: ['payrollAdjustments', queryScope, selectedBatchId],
      }),
    ])
  }

  const createMutation = useMutation({
    mutationFn: (input: BatchCreateInput) => {
      if (batchReadUnavailable) throw new Error('薪资批次尚未完整读取')
      return createBatch(input)
    },
    onSuccess: async (batch) => {
      message.success('已创建薪资批次')
      closeCreate()
      setSelectedBatchId(batch.id)
      await queryClient.invalidateQueries({ queryKey: ['payrollBatches', queryScope] })
    },
    onError: (error) => message.error(operationError(error)),
  })
  const runMutation = useMutation({
    mutationFn: (batchId: number) => {
      if (batchReadUnavailable) throw new Error('薪资批次尚未完整读取')
      return runBatch(batchId)
    },
    onSuccess: async (result) => {
      message.success(`已核算 ${result.employees} 名员工`)
      await refreshBatch()
    },
    onError: (error) => message.error(operationError(error)),
  })
  const confirmMutation = useMutation({
    mutationFn: ({ batchId, scope }: { batchId: number; scope: BatchConfirmation }) => {
      if (reviewReadsUnavailable) throw new Error('关键复核来源尚未完整读取')
      return confirmScope(batchId, {
        org_unit_id: scope.org_unit_id,
        department: scope.department,
      })
    },
    onSuccess: async () => {
      message.success('复核范围已确认')
      await refreshBatch()
    },
    onError: (error) => message.error(operationError(error)),
  })
  const disputeMutation = useMutation({
    mutationFn: ({
      batchId,
      result,
      form,
    }: {
      batchId: number
      result: PayrollResult
      form: DisputeForm
    }) => {
      if (reviewReadsUnavailable) throw new Error('关键复核来源尚未完整读取')
      return createDispute(batchId, { employee_id: result.employee_id, ...form })
    },
    onSuccess: async () => {
      message.success('异议已提交')
      closeDispute()
      await refreshBatch()
    },
    onError: (error) => message.error(operationError(error)),
  })
  const resolveMutation = useMutation({
    mutationFn: ({ disputeId, form }: { disputeId: number; form: ResolveForm }) => {
      if (disputeResolveUnavailable) throw new Error('工资异议来源尚未完整读取')
      const attachment_url = form.attachment_url?.trim() || undefined
      if (form.decision === 'APPROVED') {
        if (!attachment_url) throw new Error('同意异议必须上传证明附件')
        if (!resolveCorrectionOption || resolveCorrectionOption.kind === 'WORKFLOW') {
          throw new Error('该工资项必须转专用来源流程核验')
        }
        if (resolveCorrectionOption.kind !== 'ATTENDANCE') {
          return resolveDispute(disputeId, {
            decision: 'APPROVED',
            resolution: form.resolution,
            attachment_url,
            source_correction: makeSourceCorrection(form, resolveCorrectionOption),
          })
        }
        return resolveDispute(disputeId, {
          decision: 'APPROVED',
          resolution: form.resolution,
          attachment_url,
          attendance_changes: makeAttendanceChanges(form, allowedResolveAttendanceFields),
        })
      }
      return resolveDispute(disputeId, {
        decision: form.decision,
        resolution: form.resolution,
        attachment_url,
      })
    },
    onSuccess: async () => {
      message.success('异议处理结果已保存')
      closeResolve()
      await refreshBatch()
    },
    onError: (error) => message.error(operationError(error)),
  })
  const supplementMutation = useMutation({
    mutationFn: ({ disputeId, values }: { disputeId: number; values: SupplementForm }) => {
      if (disputeResolveUnavailable) throw new Error('工资异议来源尚未完整读取')
      return supplementDispute(disputeId, {
        note: values.note.trim(),
        attachment_url: values.attachment_url.trim(),
      })
    },
    onSuccess: async () => {
      message.success('补充材料已提交，异议已恢复待处理')
      closeSupplement()
      await refreshBatch()
    },
    onError: (error) => message.error(operationError(error)),
  })
  const lockMutation = useMutation({
    mutationFn: (batchId: number) => {
      if (reviewReadsUnavailable) throw new Error('关键复核来源尚未完整读取')
      return lockBatch(batchId)
    },
    onSuccess: async () => {
      message.success('批次已锁定')
      await refreshBatch()
    },
    onError: (error) => message.error(operationError(error)),
  })
  const approveMutation = useMutation({
    mutationFn: (batchId: number) => {
      if (reviewReadsUnavailable) throw new Error('关键复核来源尚未完整读取')
      return approveBatch(batchId)
    },
    onSuccess: async () => {
      message.success('人事审核已完成，可执行最终锁定')
      await refreshBatch()
    },
    onError: (error) => message.error(operationError(error)),
  })
  const unlockMutation = useMutation({
    mutationFn: ({ batchId, reason }: { batchId: number; reason: string }) =>
      batchReadUnavailable
        ? Promise.reject(new Error('薪资批次尚未完整读取'))
        : unlockBatch(batchId, reason),
    onSuccess: async () => {
      message.success('批次已解锁，需重新复核')
      closeUnlock()
      await refreshBatch()
    },
    onError: (error) => message.error(operationError(error)),
  })
  const reopenMutation = useMutation({
    mutationFn: ({ batchId, reason }: { batchId: number; reason: string }) =>
      batchReadUnavailable
        ? Promise.reject(new Error('薪资批次尚未完整读取'))
        : reopenBatch(batchId, reason),
    onSuccess: async () => {
      message.success('批次已退回草稿，请更正源数据后重新核算')
      closeUnlock()
      await refreshBatch()
    },
    onError: (error) => message.error(operationError(error)),
  })

  const results = resultsQuery.data ?? []
  const confirmations = confirmationsQuery.data ?? []
  const disputes = disputesQuery.data ?? []
  const adjustments = adjustmentsQuery.data ?? []
  const errorResults = results.filter((result) => result.has_error)
  const resultColumns = [
    { title: '工号', dataIndex: 'emp_no' },
    { title: '姓名', dataIndex: 'employee_name' },
    { title: '门店', dataIndex: 'org_unit_id', render: (id: number | null) => id ?? '—' },
    {
      title: '部门',
      dataIndex: 'department',
      render: (department: Department) => DEPARTMENT_LABEL[department],
    },
    { title: '实际出勤', dataIndex: 'actual_attendance_days' },
    { title: '法定出勤天数', dataIndex: 'statutory_holiday_worked_days' },
    { title: '应发工资', dataIndex: 'gross' },
    { title: '法定工资', dataIndex: 'statutory_holiday_pay' },
    { title: '押金', dataIndex: 'deposit' },
    { title: '实发工资', dataIndex: 'net' },
    { title: '结转', dataIndex: 'carry_forward' },
    { title: '待扣款结转', dataIndex: 'deferred_deductions' },
    { title: '待扣押金', dataIndex: 'deferred_deposit' },
    { title: '结果版本', dataIndex: 'version' },
    {
      title: '状态',
      render: (_: unknown, result: PayrollResult) =>
        result.has_error ? <Tag color="red">存在异常</Tag> : <Tag color="green">可复核</Tag>,
    },
    ...(canRaiseDispute
      ? [
          {
            title: '操作',
            render: (_: unknown, result: PayrollResult) => (
              <Button
                size="small"
                disabled={reviewReadsUnavailable}
                onClick={() => {
                  disputeForm.resetFields()
                  setDisputeTarget(result)
                }}
              >
                提异议
              </Button>
            ),
          },
        ]
      : []),
  ]

  const confirmationColumns = [
    { title: '门店', dataIndex: 'org_unit_id' },
    {
      title: '部门',
      dataIndex: 'department',
      render: (department: Department) => DEPARTMENT_LABEL[department],
    },
    { title: '状态', dataIndex: 'status', render: (value: string) => statusTag(value) },
    {
      title: '确认人',
      dataIndex: 'confirmed_by',
      render: (value: number | null) => value ?? '—',
    },
    {
      title: '确认时间',
      dataIndex: 'confirmed_at',
      render: (value: string | null) => formatDateTime(value),
    },
    {
      title: '操作',
      render: (_: unknown, scope: BatchConfirmation) =>
        canConfirmScope && selectedBatchId !== null && scope.status === 'PENDING' ? (
          <Popconfirm
            title="确认该门店和部门的工资数据无误？"
            onConfirm={() => confirmMutation.mutate({ batchId: selectedBatchId, scope })}
          >
            <Button
              size="small"
              loading={confirmMutation.isPending}
              disabled={reviewReadsUnavailable}
            >
              确认无误
            </Button>
          </Popconfirm>
        ) : (
          '—'
        ),
    },
  ]

  const disputeColumns = [
    { title: '员工', dataIndex: 'employee_id' },
    { title: '门店', dataIndex: 'org_unit_id', render: (id: number | null) => id ?? '—' },
    {
      title: '部门',
      dataIndex: 'department',
      render: (department: Department) => DEPARTMENT_LABEL[department],
    },
    { title: '工资项', dataIndex: 'salary_item' },
    { title: '意见', dataIndex: 'opinion', ellipsis: true },
    { title: '状态', dataIndex: 'status', render: (value: string) => statusTag(value) },
    {
      title: '操作',
      render: (_: unknown, dispute: PayrollDispute) => {
        const canHandle = canCorrect && dispute.status === 'OPEN'
        const canSupplement = canReview && dispute.status === 'NEED_MORE'
        if (!canHandle && !canSupplement) return '—'
        return (
          <Space>
            {canHandle ? (
              <Button
                size="small"
                disabled={disputeResolveUnavailable}
                onClick={() => {
                  openResolve(dispute)
                }}
              >
                人事处理
              </Button>
            ) : null}
            {canSupplement ? (
              <Button
                size="small"
                type="primary"
                disabled={disputeResolveUnavailable}
                onClick={() => {
                  supplementForm.resetFields()
                  setSupplementTarget(dispute)
                }}
              >
                补充材料
              </Button>
            ) : null}
          </Space>
        )
      },
    },
  ]

  const adjustmentColumns = [
    {
      title: '批次版本',
      dataIndex: 'batch_version',
      fixed: 'left' as const,
      render: (version: number, adjustment: PayrollAdjustment) => (
        <Tag color={adjustment.is_current_version ? 'blue' : 'default'}>
          v{version} · {adjustment.is_current_version ? '当前' : '历史'}
        </Tag>
      ),
    },
    { title: '员工', dataIndex: 'employee_id', fixed: 'left' as const },
    {
      title: '修改项目',
      dataIndex: 'item',
      render: (item: string) => ADJUSTMENT_ITEM_LABEL[item] ?? item,
    },
    {
      title: '修改前',
      dataIndex: 'before_value',
      render: (value: PayrollAdjustment['before_value']) => (
        <Typography.Text code>{auditValue(value)}</Typography.Text>
      ),
    },
    {
      title: '修改后',
      dataIndex: 'after_value',
      render: (value: PayrollAdjustment['after_value']) => (
        <Typography.Text code>{auditValue(value)}</Typography.Text>
      ),
    },
    { title: '修改原因', dataIndex: 'reason', width: 220 },
    {
      title: '申请人',
      dataIndex: 'applicant_id',
      render: (id: number | null) => (id === null ? '系统/直接更正' : `申请人 #${id}`),
    },
    {
      title: '审批人',
      dataIndex: 'approver_id',
      render: (id: number) => `审批人 #${id}`,
    },
    {
      title: '修改时间',
      dataIndex: 'created_at',
      render: (value: string) => formatDateTime(value),
    },
    {
      title: '证明附件',
      dataIndex: 'attachment_url',
      render: (url: string | null) =>
        (() => {
          const safeUrl = safeHttpUrl(url)
          if (safeUrl) {
            return (
              <Typography.Link href={safeUrl} target="_blank" rel="noreferrer">
                查看附件
              </Typography.Link>
            )
          }
          return url ? <Typography.Text type="danger">无效附件地址</Typography.Text> : '—'
        })(),
    },
    {
      title: '重新计算结果',
      dataIndex: 'recompute_result',
      render: (value: PayrollAdjustment['recompute_result']) => recomputeLabel(value),
    },
  ]

  const isLoading =
    resultsQuery.isLoading ||
    resultsQuery.isFetching ||
    confirmationsQuery.isLoading ||
    confirmationsQuery.isFetching ||
    disputesQuery.isLoading ||
    disputesQuery.isFetching

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          薪资核算与复核
        </Typography.Title>
        <Space>
          {canRun && (
            <Button
              type="primary"
              disabled={batchReadUnavailable}
              onClick={() => {
                createForm.resetFields()
                setCreateOpen(true)
              }}
            >
              新建批次
            </Button>
          )}
          <Button onClick={() => void refreshBatch()}>刷新</Button>
        </Space>
      </Space>

      {batchesQuery.isError && (
        <Alert type="error" message="无法读取薪资批次，批次操作已停用" showIcon />
      )}
      <Card size="small" title="薪资批次">
        <Table
          rowKey="id"
          loading={batchesQuery.isLoading || batchesQuery.isFetching}
          dataSource={batchesQuery.data ?? []}
          pagination={false}
          rowSelection={{
            type: 'radio',
            selectedRowKeys: selectedBatchId === null ? [] : [selectedBatchId],
            onChange: (keys) => setSelectedBatchId(Number(keys[0])),
          }}
          columns={[
            { title: '月份', dataIndex: 'period' },
            {
              title: '考勤区间',
              render: (_, batch) => `${batch.attendance_start} 至 ${batch.attendance_end}`,
            },
            { title: '状态', dataIndex: 'status', render: (value) => statusTag(value) },
            { title: '批次修订', dataIndex: 'version' },
          ]}
          scroll={{ x: 720 }}
        />
      </Card>

      {selectedBatch && (
        <>
          {resultsQuery.isError && (
            <Alert type="error" showIcon message="无法读取工资结果，关键复核操作已停用" />
          )}
          {confirmationsQuery.isError && (
            <Alert type="error" showIcon message="无法读取门店复核范围，关键复核操作已停用" />
          )}
          {disputesQuery.isError && (
            <Alert type="error" showIcon message="无法读取工资异议，关键复核操作已停用" />
          )}
          <Card
            size="small"
            title={`当前批次 · ${selectedBatch.period}`}
            extra={
              <Space>
                {canRun && selectedBatch.status === 'DRAFT' && (
                  <Popconfirm
                    title="将按当前人员、薪资结构和考勤生成不可覆盖的初始结果，确认执行？"
                    onConfirm={() => runMutation.mutate(selectedBatch.id)}
                  >
                    <Button
                      type="primary"
                      loading={runMutation.isPending}
                      disabled={batchReadUnavailable}
                    >
                      执行核算
                    </Button>
                  </Popconfirm>
                )}
                {canApprove && selectedBatch.status === 'CONFIRMED' && (
                  <Popconfirm
                    title="确认锁定？锁定后源数据不能直接修改。"
                    onConfirm={() => lockMutation.mutate(selectedBatch.id)}
                  >
                    <Button
                      type="primary"
                      loading={lockMutation.isPending}
                      disabled={reviewReadsUnavailable}
                    >
                      锁定批次
                    </Button>
                  </Popconfirm>
                )}
                {canApprove && selectedBatch.status === 'PENDING_HR' && (
                  <Popconfirm
                    title="确认已完成最终人事审核？"
                    onConfirm={() => approveMutation.mutate(selectedBatch.id)}
                  >
                    <Button
                      type="primary"
                      loading={approveMutation.isPending}
                      disabled={reviewReadsUnavailable}
                    >
                      人事最终审核
                    </Button>
                  </Popconfirm>
                )}
                {canCorrect && selectedBatch.status === 'LOCKED' && (
                  <Button
                    danger
                    disabled={batchReadUnavailable}
                    onClick={() => {
                      unlockForm.resetFields()
                      setCorrectionMode('unlock')
                      setUnlockOpen(true)
                    }}
                  >
                    解锁批次
                  </Button>
                )}
                {canCorrect &&
                  selectedBatch.status !== 'DRAFT' &&
                  selectedBatch.status !== 'CALCULATING' &&
                  selectedBatch.status !== 'LOCKED' && (
                    <Button
                      danger
                      disabled={batchReadUnavailable}
                      onClick={() => {
                        unlockForm.resetFields()
                        setCorrectionMode('reopen')
                        setUnlockOpen(true)
                      }}
                    >
                      退回更正
                    </Button>
                  )}
              </Space>
            }
          >
            <Steps
              size="small"
              responsive
              items={[
                {
                  title: '核算状态',
                  status: calculationStepStatus(selectedBatch.calculation_status),
                  description: (
                    <Space direction="vertical" size={0}>
                      {processTag(
                        CALCULATION_STATUS_LABEL[selectedBatch.calculation_status],
                        calculationStepStatus(selectedBatch.calculation_status),
                      )}
                      <Typography.Text type="secondary">
                        {formatDateTime(selectedBatch.calculated_at)}
                      </Typography.Text>
                    </Space>
                  ),
                },
                {
                  title: '门店确认',
                  status: storeStepStatus(selectedBatch.store_confirmation_status),
                  description: processTag(
                    STORE_STATUS_LABEL[selectedBatch.store_confirmation_status],
                    storeStepStatus(selectedBatch.store_confirmation_status),
                  ),
                },
                {
                  title: '人事审核',
                  status: hrStepStatus(selectedBatch.hr_review_status),
                  description: (
                    <Space direction="vertical" size={0}>
                      {processTag(
                        HR_STATUS_LABEL[selectedBatch.hr_review_status],
                        hrStepStatus(selectedBatch.hr_review_status),
                      )}
                      <Typography.Text type="secondary">
                        {selectedBatch.hr_reviewed_by === null
                          ? '审核人 —'
                          : `审核人 #${selectedBatch.hr_reviewed_by}`}
                      </Typography.Text>
                      <Typography.Text type="secondary">
                        {formatDateTime(selectedBatch.hr_reviewed_at)}
                      </Typography.Text>
                    </Space>
                  ),
                },
                {
                  title: '最终锁定',
                  status: lockStepStatus(selectedBatch.lock_status),
                  description: (
                    <Space direction="vertical" size={0}>
                      {processTag(
                        LOCK_STATUS_LABEL[selectedBatch.lock_status],
                        lockStepStatus(selectedBatch.lock_status),
                      )}
                      <Typography.Text type="secondary">
                        {selectedBatch.locked_by === null
                          ? '锁定人 —'
                          : `锁定人 #${selectedBatch.locked_by}`}
                      </Typography.Text>
                      <Typography.Text type="secondary">
                        {formatDateTime(selectedBatch.locked_at)}
                      </Typography.Text>
                    </Space>
                  ),
                },
              ]}
            />
            <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} style={{ marginTop: 16 }}>
              <Descriptions.Item label="总流程状态">
                {statusTag(selectedBatch.status)}
              </Descriptions.Item>
              <Descriptions.Item label="考勤起止">
                {selectedBatch.attendance_start} 至 {selectedBatch.attendance_end}
              </Descriptions.Item>
              <Descriptions.Item label="批次修订">v{selectedBatch.version}</Descriptions.Item>
            </Descriptions>
            {errorResults.length > 0 && (
              <Alert
                style={{ marginTop: 12 }}
                type="error"
                showIcon
                message={`有 ${errorResults.length} 名员工的核算输入异常，批次不能锁定`}
              />
            )}
          </Card>

          <Card size="small" title="工资结果（仅显示你被授权的门店和部门）">
            <div role="region" aria-label="工资结果账本" tabIndex={0} style={{ overflowX: 'auto' }}>
              <Table<PayrollResult>
                rowKey={(result) => `${result.employee_id}-${result.version}`}
                loading={isLoading}
                columns={resultColumns}
                dataSource={results}
                pagination={{ pageSize: 20 }}
                scroll={{ x: 1640 }}
                expandable={{
                  expandedRowRender: (result) => (
                    <Space direction="vertical" style={{ width: '100%' }}>
                      {result.exceptions.length > 0 && (
                        <Alert type="error" message={result.exceptions.join('；')} showIcon />
                      )}
                      {result.warnings.length > 0 && (
                        <Alert type="warning" message={result.warnings.join('；')} showIcon />
                      )}
                      <Table
                        rowKey="code"
                        size="small"
                        pagination={false}
                        scroll={{ x: 720 }}
                        dataSource={result.lines}
                        columns={[
                          { title: '项目', dataIndex: 'category' },
                          { title: '编码', dataIndex: 'code' },
                          { title: '公式', dataIndex: 'formula' },
                          { title: '金额', dataIndex: 'amount' },
                        ]}
                      />
                    </Space>
                  ),
                }}
              />
            </div>
          </Card>

          <Card size="small" title="门店 / 部门复核">
            <Table<BatchConfirmation>
              rowKey={(scope) => `${scope.org_unit_id}-${scope.department}`}
              loading={confirmationsQuery.isLoading}
              columns={confirmationColumns}
              dataSource={confirmations}
              pagination={false}
              scroll={{ x: 760 }}
            />
          </Card>

          <Card size="small" title="异议与处理记录">
            <Table<PayrollDispute>
              rowKey="id"
              loading={disputesQuery.isLoading}
              columns={disputeColumns}
              dataSource={disputes}
              pagination={{ pageSize: 10 }}
              scroll={{ x: 980 }}
              expandable={{
                rowExpandable: (dispute) => (dispute.events?.length ?? 0) > 0,
                expandedRowRender: (dispute) => (
                  <Table
                    rowKey="id"
                    size="small"
                    pagination={false}
                    dataSource={dispute.events ?? []}
                    columns={[
                      {
                        title: '事件',
                        dataIndex: 'event_type',
                        render: (value: string) => DISPUTE_EVENT_LABEL[value] ?? value,
                      },
                      { title: '说明', dataIndex: 'note' },
                      {
                        title: '操作人',
                        dataIndex: 'actor_id',
                        render: (value: number) => `用户 #${value}`,
                      },
                      {
                        title: '附件',
                        dataIndex: 'attachment_url',
                        render: (value: string | null) => {
                          const safeUrl = safeHttpUrl(value)
                          return safeUrl ? (
                            <Typography.Link href={safeUrl} target="_blank" rel="noreferrer">
                              查看附件
                            </Typography.Link>
                          ) : value ? (
                            <Typography.Text type="danger">无效附件地址</Typography.Text>
                          ) : (
                            '—'
                          )
                        },
                      },
                      {
                        title: '时间',
                        dataIndex: 'created_at',
                        render: (value: string) => formatDateTime(value),
                      },
                    ]}
                  />
                ),
              }}
            />
          </Card>

          <Card size="small" title="修改记录（保留历史版本）">
            {adjustmentsQuery.isError && (
              <Alert
                type="error"
                showIcon
                message="无法读取工资修改记录"
                style={{ marginBottom: 12 }}
              />
            )}
            <div role="region" aria-label="工资修改记录" tabIndex={0} style={{ overflowX: 'auto' }}>
              <Table<PayrollAdjustment>
                rowKey="id"
                size="small"
                loading={adjustmentsQuery.isLoading}
                columns={adjustmentColumns}
                dataSource={adjustments}
                pagination={{ pageSize: 10 }}
                scroll={{ x: 1900 }}
              />
            </div>
          </Card>
        </>
      )}

      <Modal
        title="新建薪资批次"
        open={createOpen}
        onCancel={closeCreate}
        onOk={() => createForm.submit()}
        confirmLoading={createMutation.isPending}
        okButtonProps={{ disabled: batchReadUnavailable }}
        destroyOnHidden
      >
        <Form
          form={createForm}
          layout="vertical"
          preserve={false}
          onFinish={(values) => createMutation.mutate(values)}
        >
          <Form.Item
            name="period"
            label="薪资月份"
            rules={[{ required: true, pattern: /^\d{4}-\d{2}$/, message: '请输入 YYYY-MM' }]}
          >
            <Input placeholder="2026-05" />
          </Form.Item>
          <Form.Item name="attendance_start" label="考勤开始日期" rules={[{ required: true }]}>
            <Input placeholder="2026-04-26" />
          </Form.Item>
          <Form.Item name="attendance_end" label="考勤结束日期" rules={[{ required: true }]}>
            <Input placeholder="2026-05-25" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`提交异议 · ${disputeTarget?.employee_name ?? ''}`}
        open={disputeTarget !== null}
        onCancel={closeDispute}
        onOk={() => disputeForm.submit()}
        confirmLoading={disputeMutation.isPending}
        okButtonProps={{ disabled: reviewReadsUnavailable }}
        destroyOnHidden
      >
        <Form
          form={disputeForm}
          layout="vertical"
          preserve={false}
          onFinish={(values) => {
            if (selectedBatchId !== null && disputeTarget) {
              disputeMutation.mutate({
                batchId: selectedBatchId,
                result: disputeTarget,
                form: values,
              })
            }
          }}
        >
          <Alert
            type="info"
            showIcon
            message="可对任一工资明细项目提交异议；人事审核后须到对应源数据模块更正，不得直接修改最终工资金额。"
            style={{ marginBottom: 16 }}
          />
          <Form.Item name="salary_item" label="具体工资项" rules={[{ required: true }]}>
            <Select
              options={(disputeTarget?.lines ?? []).map((line) => ({
                value: line.code,
                label: `${line.category}（${line.code}）`,
              }))}
            />
          </Form.Item>
          <Form.Item name="opinion" label="修改意见" rules={[{ required: true, max: 1000 }]}>
            <Input.TextArea rows={4} maxLength={1000} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`处理异议 #${resolveTarget?.id ?? ''}`}
        open={resolveTarget !== null}
        onCancel={closeResolve}
        onOk={() => resolveForm.submit()}
        confirmLoading={resolveMutation.isPending}
        okButtonProps={{ disabled: disputeResolveUnavailable }}
        destroyOnHidden
      >
        <Form
          form={resolveForm}
          layout="vertical"
          preserve={false}
          onValuesChange={() => setResolveAttendanceError(null)}
          onFinish={(values) => {
            const attendanceChanges = makeAttendanceChanges(values, allowedResolveAttendanceFields)
            if (values.decision === 'APPROVED' && !canApproveResolveTarget) {
              resolveForm.setFieldValue('decision', 'REJECTED')
              return
            }
            if (
              values.decision === 'APPROVED' &&
              resolveCorrectionOption?.kind === 'ATTENDANCE' &&
              Object.keys(attendanceChanges).length === 0
            ) {
              setResolveAttendanceError('同意异议时，至少填写一项要更正的源考勤数据。')
              return
            }
            if (resolveTarget) resolveMutation.mutate({ disputeId: resolveTarget.id, form: values })
          }}
        >
          {resolveCorrectionOption?.kind === 'WORKFLOW' && (
            <Alert
              type="warning"
              showIcon
              message={resolveCorrectionOption.label}
              description={resolveCorrectionOption.reason}
              style={{ marginBottom: 16 }}
            />
          )}
          {resolveTarget !== null && resolveCorrectionOption === null && (
            <Alert
              type="warning"
              showIcon
              message="该工资项目没有可安全自动更正的来源，请要求补充材料或核验后驳回。"
              style={{ marginBottom: 16 }}
            />
          )}
          <Form.Item name="decision" label="处理结论" rules={[{ required: true }]}>
            <Select
              options={[
                ...(canApproveResolveTarget
                  ? [
                      {
                        value: 'APPROVED',
                        label:
                          resolveCorrectionOption?.kind === 'ATTENDANCE'
                            ? '同意并改考勤后重算'
                            : '同意并更正来源后重算',
                      },
                    ]
                  : []),
                { value: 'REJECTED', label: '驳回异议' },
                { value: 'NEED_MORE', label: '要求补充材料' },
              ]}
            />
          </Form.Item>
          {canApproveResolveTarget && resolutionDecision === 'APPROVED' && (
            <Alert
              type="info"
              showIcon
              message={
                resolveCorrectionOption?.kind === 'ATTENDANCE'
                  ? '至少填写一项源考勤修改。系统会保留旧结果并生成新版本。'
                  : '系统会原子更正基础来源、保留旧结果并生成重算版本。'
              }
              style={{ marginBottom: 16 }}
            />
          )}
          {resolveCorrectionOption?.kind === 'ATTENDANCE' &&
            resolutionDecision === 'APPROVED' &&
            resolveAttendanceError && (
              <Alert
                type="error"
                showIcon
                message={resolveAttendanceError}
                style={{ marginBottom: 16 }}
              />
            )}
          {resolveCorrectionOption?.kind === 'ATTENDANCE' &&
            resolutionDecision === 'APPROVED' && (
            <Space direction="vertical" style={{ width: '100%' }}>
              {allowedResolveAttendanceFields.includes('expected_days') && (
                <Form.Item name="expected_days" label="应出勤天数">
                  <InputNumber min={0} max={31} style={{ width: '100%' }} />
                </Form.Item>
              )}
              {allowedResolveAttendanceFields.includes('actual_days') && (
                <Form.Item name="actual_days" label="实际出勤天数">
                  <InputNumber min={0} max={31} style={{ width: '100%' }} />
                </Form.Item>
              )}
              {allowedResolveAttendanceFields.includes('worked_hours') && (
                <Form.Item name="worked_hours" label="出勤工时">
                  <InputNumber min={0} max={744} style={{ width: '100%' }} />
                </Form.Item>
              )}
              {allowedResolveAttendanceFields.includes('rest_days') && (
                <Form.Item name="rest_days" label="休息天数">
                  <InputNumber min={0} max={31} style={{ width: '100%' }} />
                </Form.Item>
              )}
              {allowedResolveAttendanceFields.includes('overtime_hours') && (
                <Form.Item name="overtime_hours" label="加班工时">
                  <InputNumber min={0} max={744} style={{ width: '100%' }} />
                </Form.Item>
              )}
            </Space>
          )}
          {resolveCorrectionOption?.kind === 'HOLIDAY_WORK' &&
            resolutionDecision === 'APPROVED' && (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Form.Item name="holiday_date" label="法定节假日" rules={[{ required: true }]}>
                  <Select
                    options={resolveCorrectionOption.holiday_dates.map((item) => ({
                      value: item.holiday_date,
                      label: `${item.holiday_date}（当前${item.worked ? '已出勤' : '未出勤'}）`,
                    }))}
                    onChange={(value: string) => {
                      const selected = resolveCorrectionOption.holiday_dates.find(
                        (item) => item.holiday_date === value,
                      )
                      resolveForm.setFieldValue('holiday_worked', selected?.worked)
                    }}
                  />
                </Form.Item>
                <Form.Item name="holiday_worked" label="是否出勤" rules={[{ required: true }]}>
                  <Select
                    options={[
                      { value: true, label: '已出勤' },
                      { value: false, label: '未出勤' },
                    ]}
                  />
                </Form.Item>
              </Space>
            )}
          {resolveCorrectionOption?.kind === 'PERFORMANCE' &&
            resolutionDecision === 'APPROVED' && (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Form.Item
                  name="performance_coefficient"
                  label="绩效系数"
                  rules={[{ required: true }]}
                >
                  <InputNumber min={0} max={5} precision={3} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="performance_score" label="绩效得分">
                  <InputNumber min={0} max={100} precision={2} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="performance_remark" label="绩效备注">
                  <Input maxLength={255} />
                </Form.Item>
              </Space>
            )}
          {resolveCorrectionOption?.kind === 'MONTHLY_ADJUSTMENT' &&
            resolutionDecision === 'APPROVED' && (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Form.Item name="monthly_amount" label="补发/补扣金额" rules={[{ required: true }]}>
                  <InputNumber min={0.01} precision={2} style={{ width: '100%' }} />
                </Form.Item>
                {(
                  [
                    ['taxable', '计入个税'],
                    ['in_social_base', '计入社保基数'],
                    ['in_housing_base', '计入公积金基数'],
                  ] as const
                ).map(([name, label]) => (
                  <Form.Item key={name} name={name} label={label} rules={[{ required: true }]}>
                    <Select
                      options={[
                        { value: true, label: '是' },
                        { value: false, label: '否' },
                      ]}
                    />
                  </Form.Item>
                ))}
              </Space>
            )}
          {resolveCorrectionOption?.kind === 'SALARY_STRUCTURE' &&
            resolutionDecision === 'APPROVED' && (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Form.Item name="structure_component_id" label="薪资组件" rules={[{ required: true }]}>
                  <Select
                    options={resolveCorrectionOption.components.map((component) => ({
                      value: component.component_id,
                      label: `${component.name}（${component.code}，当前 ${component.amount}）`,
                    }))}
                    onChange={(componentId: number) => {
                      const component = resolveCorrectionOption.components.find(
                        (item) => item.component_id === componentId,
                      )
                      resolveForm.setFieldValue(
                        'structure_amount',
                        component ? Number(component.amount) : undefined,
                      )
                    }}
                  />
                </Form.Item>
                <Form.Item name="structure_amount" label="组件金额" rules={[{ required: true }]}>
                  <InputNumber min={0} precision={2} style={{ width: '100%' }} />
                </Form.Item>
              </Space>
            )}
          <Form.Item name="resolution" label="处理说明" rules={[{ required: true, max: 1000 }]}>
            <Input.TextArea rows={3} maxLength={1000} />
          </Form.Item>
          <Form.Item
            name="attachment_url"
            label={resolutionDecision === 'APPROVED' ? '证明附件地址' : '证明附件地址（可选）'}
            rules={[
              {
                validator: (_, value: string | undefined) =>
                  resolutionDecision === 'APPROVED' && !value?.trim()
                    ? Promise.reject(new Error('同意异议必须上传证明附件'))
                    : Promise.resolve(),
              },
              { max: 512 },
              { validator: validateHttpUrl },
            ]}
          >
            <Input />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`补充异议材料 #${supplementTarget?.id ?? ''}`}
        open={supplementTarget !== null}
        onCancel={closeSupplement}
        onOk={() => supplementForm.submit()}
        confirmLoading={supplementMutation.isPending}
        okButtonProps={{ disabled: disputeResolveUnavailable }}
        destroyOnHidden
      >
        <Form
          form={supplementForm}
          layout="vertical"
          preserve={false}
          onFinish={(values) => {
            if (supplementTarget) {
              supplementMutation.mutate({ disputeId: supplementTarget.id, values })
            }
          }}
        >
          <Alert
            type="info"
            showIcon
            message="补充材料提交后，异议将恢复为待人事处理。"
            style={{ marginBottom: 16 }}
          />
          <Form.Item
            name="note"
            label="补充说明"
            rules={[{ required: true, whitespace: true, max: 1000 }]}
          >
            <Input.TextArea rows={3} maxLength={1000} />
          </Form.Item>
          <Form.Item
            name="attachment_url"
            label="证明附件地址"
            rules={[
              { required: true, whitespace: true },
              { max: 512 },
              { validator: validateHttpUrl },
            ]}
          >
            <Input placeholder="https://…" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={correctionMode === 'unlock' ? '解锁薪资批次' : '退回薪资批次以更正'}
        open={unlockOpen}
        onCancel={closeUnlock}
        onOk={() => unlockForm.submit()}
        confirmLoading={unlockMutation.isPending || reopenMutation.isPending}
        okButtonProps={{ disabled: batchReadUnavailable }}
        destroyOnHidden
      >
        <Form
          form={unlockForm}
          layout="vertical"
          preserve={false}
          onFinish={(values) => {
            if (selectedBatchId === null) return
            if (correctionMode === 'unlock') {
              unlockMutation.mutate({ batchId: selectedBatchId, ...values })
            } else {
              reopenMutation.mutate({ batchId: selectedBatchId, ...values })
            }
          }}
        >
          <Alert
            type="warning"
            showIcon
            message={
              correctionMode === 'unlock'
                ? '解锁会保留历史工资结果。请由人事更正源数据后重新核算。'
                : '退回更正会保留历史工资结果。请更正异常源数据后重新核算。'
            }
            style={{ marginBottom: 16 }}
          />
          <Form.Item
            name="reason"
            label={correctionMode === 'unlock' ? '解锁原因' : '退回更正原因'}
            rules={[{ required: true, max: 500 }]}
          >
            <Input.TextArea rows={3} maxLength={500} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
