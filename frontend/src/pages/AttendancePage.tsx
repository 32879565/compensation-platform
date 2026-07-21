import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useMemo, useRef, useState } from 'react'

import {
  createAttendanceSchedule,
  fetchAttendanceSchedules,
  fetchPerformance,
  generateExpectedAttendance,
  importPerformance,
  isPerformanceImportFile,
  PERFORMANCE_IMPORT_ACCEPT,
  updateAttendanceSchedule,
  type AttendanceDepartment,
  type AttendanceEmploymentType,
  type AttendanceScheduleRule,
  type AttendanceScheduleWrite,
  type ExpectedAttendanceGenerationResult,
  type PerformanceImportResult,
  type PerformanceRecord,
} from '../api/attendance'
import { api } from '../api/client'
import {
  fetchDingTalkAttendanceSnapshot,
  fetchDingTalkIntegration,
  refreshDingTalkAttendance,
  type DingTalkAttendancePreviewRow,
} from '../api/dingtalk'
import { fetchEmployees, fetchOrgUnits, type Employee } from '../api/masterdata'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'
import { validateHttpUrl } from '../utils/safeExternalUrl'

interface Attendance {
  employee_id: number
  period: string
  expected_days: string
  expected_days_adjust_reason: string | null
  actual_days: string
  worked_hours: string | null
  rest_days: string
  overtime_hours: string
  holiday_worked_days: string
  leave_days: string
}

type PerformanceImportFeedback =
  | { type: 'success'; period: string; result: PerformanceImportResult }
  | { type: 'error'; period: string; message: string }

interface PerformanceImportVariables {
  file: File
  period: string
}

interface AttendanceScheduleFormValues {
  name: string
  org_unit_id?: number
  employment_type?: AttendanceEmploymentType
  department?: AttendanceDepartment
  position_title?: string
  is_special_position?: boolean
  weekly_rest_days?: number[]
  monthly_expected_days?: number | null
  effective_from: string
  effective_to?: string
  priority: number
  is_active: boolean
}

type ScheduleGenerationFeedback =
  | { type: 'success'; result: ExpectedAttendanceGenerationResult }
  | { type: 'error'; period: string; message: string; errors: string[] }

interface AttendanceListRow {
  id: number
  emp_no: string
  name: string
}

const WEEKDAY_OPTIONS = [
  { value: 0, label: '周一' },
  { value: 1, label: '周二' },
  { value: 2, label: '周三' },
  { value: 3, label: '周四' },
  { value: 4, label: '周五' },
  { value: 5, label: '周六' },
  { value: 6, label: '周日' },
]

const EMPLOYMENT_LABELS: Record<AttendanceEmploymentType, string> = {
  FULL_TIME: '全职月薪',
  PART_TIME_HOURLY: '兼职小时工',
  LABOR: '劳务',
}

const DEPARTMENT_LABELS: Record<AttendanceDepartment, string> = {
  DINING: '厅面',
  KITCHEN: '厨房',
  OTHER: '其他',
}

function currentPeriod(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
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
  if (error instanceof Error && error.message) return error.message
  return '操作失败，请稍后重试'
}

function scheduleGenerationError(
  error: unknown,
): Omit<Extract<ScheduleGenerationFeedback, { type: 'error' }>, 'type' | 'period'> {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (
      error as {
        response?: { data?: { detail?: { message?: unknown; errors?: unknown } | unknown } }
      }
    ).response
    const detail = response?.data?.detail
    if (typeof detail === 'object' && detail !== null) {
      const message =
        'message' in detail && typeof detail.message === 'string'
          ? detail.message
          : '应出勤生成失败'
      const errors =
        'errors' in detail && Array.isArray(detail.errors)
          ? detail.errors.filter((entry): entry is string => typeof entry === 'string')
          : []
      return { message, errors }
    }
  }
  return { message: errorMessage(error), errors: [] }
}

function schedulePayload(values: AttendanceScheduleFormValues): AttendanceScheduleWrite {
  return {
    name: values.name.trim(),
    org_unit_id: values.org_unit_id ?? null,
    employment_type: values.employment_type ?? null,
    department: values.department ?? null,
    position_title: values.position_title?.trim() || null,
    is_special_position: values.is_special_position ?? null,
    weekly_rest_days: values.weekly_rest_days ?? [],
    monthly_expected_days:
      values.monthly_expected_days == null ? null : String(values.monthly_expected_days),
    effective_from: values.effective_from,
    effective_to: values.effective_to || null,
    priority: values.priority,
    is_active: values.is_active,
  }
}

function rulePayload(rule: AttendanceScheduleRule): AttendanceScheduleWrite {
  return {
    name: rule.name,
    org_unit_id: rule.org_unit_id,
    employment_type: rule.employment_type,
    department: rule.department,
    position_title: rule.position_title,
    is_special_position: rule.is_special_position,
    weekly_rest_days: rule.weekly_rest_days,
    monthly_expected_days: rule.monthly_expected_days,
    effective_from: rule.effective_from,
    effective_to: rule.effective_to,
    priority: rule.priority,
    is_active: rule.is_active,
  }
}

async function fetchAllEmployees(): Promise<Employee[]> {
  const firstPage = await fetchEmployees({ page: 1, page_size: 200 })
  const pageSize = Math.max(firstPage.page_size, 1)
  const pageCount = Math.ceil(firstPage.total / pageSize)
  if (pageCount <= 1) return firstPage.items

  const remainingPages = await Promise.all(
    Array.from({ length: pageCount - 1 }, (_, index) =>
      fetchEmployees({ page: index + 2, page_size: pageSize }),
    ),
  )
  return [...firstPage.items, ...remainingPages.flatMap((page) => page.items)]
}

export default function AttendancePage() {
  const { user, hasPermission } = useAuth()
  const canRead = hasPermission(Perm.ATTENDANCE_READ)
  const canReadEmployees = hasPermission(Perm.EMPLOYEE_READ)
  const canWrite = hasPermission(Perm.ATTENDANCE_WRITE)
  const canManageSchedules = hasPermission(Perm.ATTENDANCE_SCHEDULE_WRITE)
  const canReadSchedules = canManageSchedules || hasPermission(Perm.ATTENDANCE_SCHEDULE_READ)
  const canReadOrgUnits = hasPermission(Perm.ORG_READ)
  const canAdjustExpectedDays = hasPermission(Perm.ATTENDANCE_EXPECTED_DAYS_ADJUST)
  const canSyncDingTalk = hasPermission(Perm.NOTIFICATION_MANAGE)
  const queryScope = user?.username ?? 'anonymous'
  const qc = useQueryClient()
  const [period, setPeriod] = useState(currentPeriod())
  const [editing, setEditing] = useState<AttendanceListRow | null>(null)
  const [scheduleModalOpen, setScheduleModalOpen] = useState(false)
  const [scheduleEditing, setScheduleEditing] = useState<AttendanceScheduleRule | null>(null)
  const [scheduleFeedback, setScheduleFeedback] = useState<ScheduleGenerationFeedback | null>(null)
  const [performanceFeedback, setPerformanceFeedback] = useState<PerformanceImportFeedback | null>(
    null,
  )
  const performanceFileInputRef = useRef<HTMLInputElement>(null)
  const [form] = Form.useForm()
  const [scheduleForm] = Form.useForm<AttendanceScheduleFormValues>()

  const empQuery = useQuery({
    queryKey: ['attEmployees', queryScope],
    queryFn: fetchAllEmployees,
    enabled: canRead && canReadEmployees,
  })
  const attQuery = useQuery({
    queryKey: ['attendance', queryScope, period],
    queryFn: async () =>
      (await api.get<Attendance[]>('/api/attendance', { params: { period } })).data,
    enabled: canRead,
  })
  const performanceQuery = useQuery({
    queryKey: ['performance', queryScope, period],
    queryFn: () => fetchPerformance(period),
    enabled: canRead,
  })
  const scheduleQuery = useQuery({
    queryKey: ['attendanceSchedules', queryScope],
    queryFn: fetchAttendanceSchedules,
    enabled: canReadSchedules,
  })
  const scheduleOrgQuery = useQuery({
    queryKey: ['attendanceScheduleOrgUnits', queryScope],
    queryFn: fetchOrgUnits,
    enabled: canReadSchedules && canReadOrgUnits,
  })
  const dingtalkIntegrationQuery = useQuery({
    queryKey: ['dingtalkIntegration', queryScope],
    queryFn: fetchDingTalkIntegration,
    enabled: canSyncDingTalk,
  })
  const dingtalkSnapshotQuery = useQuery({
    queryKey: ['dingtalkAttendanceSnapshot', queryScope, period],
    queryFn: () => fetchDingTalkAttendanceSnapshot(period),
    enabled:
      canRead &&
      canReadEmployees &&
      canSyncDingTalk &&
      dingtalkIntegrationQuery.data?.read_sync_ready === true,
    refetchInterval: (query) =>
      query.state.data?.status === 'QUEUED' || query.state.data?.status === 'RUNNING'
        ? 3000
        : false,
  })

  const attByEmp = new Map((attQuery.data ?? []).map((a) => [a.employee_id, a]))
  const employeeById = new Map((empQuery.data ?? []).map((employee) => [employee.id, employee]))
  const attendanceRows = useMemo(() => {
    const rows = new Map<number, AttendanceListRow>()
    for (const employee of empQuery.data ?? []) {
      rows.set(employee.id, {
        id: employee.id,
        emp_no: employee.emp_no,
        name: employee.name,
      })
    }
    for (const attendance of attQuery.data ?? []) {
      if (!rows.has(attendance.employee_id)) {
        rows.set(attendance.employee_id, {
          id: attendance.employee_id,
          emp_no: `员工 ID #${attendance.employee_id}`,
          name: '—',
        })
      }
    }
    return [...rows.values()]
  }, [attQuery.data, empQuery.data])
  const orgById = useMemo(
    () => new Map((scheduleOrgQuery.data ?? []).map((org) => [org.id, org.name])),
    [scheduleOrgQuery.data],
  )
  const attendanceReadUnavailable = attQuery.isLoading || attQuery.isFetching || attQuery.isError
  const scheduleReadUnavailable =
    scheduleQuery.isLoading ||
    scheduleQuery.isFetching ||
    scheduleQuery.isError ||
    scheduleQuery.data === undefined

  const saveMutation = useMutation({
    mutationFn: async (v: Record<string, unknown>) => {
      if (attendanceReadUnavailable || editing === null) {
        throw new Error('考勤来源尚未完整读取')
      }
      const payload = { ...v }
      const currentAttendance = attByEmp.get(editing.id)
      const requestedExpectedDays = Number(payload.expected_days)
      if (
        canAdjustExpectedDays &&
        currentAttendance &&
        requestedExpectedDays !== Number(currentAttendance.expected_days) &&
        (typeof payload.expected_days_adjust_reason !== 'string' ||
          !payload.expected_days_adjust_reason.trim())
      ) {
        throw new Error('调整应出勤天数必须填写新的调整原因')
      }
      if (!canAdjustExpectedDays) {
        if (currentAttendance) payload.expected_days = Number(currentAttendance.expected_days)
        delete payload.expected_days_adjust_reason
      }
      await api.put(`/api/employees/${editing.id}/attendance/${period}`, payload)
    },
    onSuccess: () => {
      message.success('已保存')
      form.resetFields()
      setEditing(null)
      void qc.invalidateQueries({ queryKey: ['attendance', queryScope, period] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const scheduleSaveMutation = useMutation({
    mutationFn: (values: AttendanceScheduleFormValues) => {
      const payload = schedulePayload(values)
      return scheduleEditing
        ? updateAttendanceSchedule(scheduleEditing.id, payload)
        : createAttendanceSchedule(payload)
    },
    onSuccess: async () => {
      message.success(scheduleEditing ? '应出勤规则已更新' : '应出勤规则已创建')
      scheduleForm.resetFields()
      setScheduleEditing(null)
      setScheduleModalOpen(false)
      await qc.invalidateQueries({ queryKey: ['attendanceSchedules', queryScope] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const scheduleDeactivateMutation = useMutation({
    mutationFn: (rule: AttendanceScheduleRule) =>
      updateAttendanceSchedule(rule.id, { ...rulePayload(rule), is_active: false }),
    onSuccess: async () => {
      message.success('应出勤规则已停用')
      await qc.invalidateQueries({ queryKey: ['attendanceSchedules', queryScope] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const scheduleGenerateMutation = useMutation({
    mutationFn: (generationPeriod: string) => {
      if (scheduleReadUnavailable) {
        throw new Error('应出勤规则尚未完整读取，已禁止生成')
      }
      return generateExpectedAttendance(generationPeriod)
    },
    onMutate: () => setScheduleFeedback(null),
    onSuccess: async (result) => {
      setScheduleFeedback({ type: 'success', result })
      await qc.invalidateQueries({ queryKey: ['attendance', queryScope, result.period] })
    },
    onError: (error, generationPeriod) => {
      const parsed = scheduleGenerationError(error)
      setScheduleFeedback({ type: 'error', period: generationPeriod, ...parsed })
    },
  })
  const importMutation = useMutation({
    mutationFn: ({ file, period: importPeriod }: PerformanceImportVariables) =>
      importPerformance(importPeriod, file),
    onSuccess: async (result, { period: importPeriod }) => {
      setPerformanceFeedback({ type: 'success', period: importPeriod, result })
      await qc.invalidateQueries({ queryKey: ['performance', queryScope, importPeriod] })
    },
    onError: (error, { period: importPeriod }) => {
      setPerformanceFeedback({ type: 'error', period: importPeriod, message: errorMessage(error) })
    },
  })
  const dingtalkRefreshMutation = useMutation({
    mutationFn: (refreshPeriod: string) => refreshDingTalkAttendance(refreshPeriod),
    onSuccess: async (snapshot, refreshPeriod) => {
      qc.setQueryData(['dingtalkAttendanceSnapshot', queryScope, refreshPeriod], snapshot)
      message.success('已开始刷新钉钉考勤，页面会自动更新')
      await qc.invalidateQueries({
        queryKey: ['dingtalkAttendanceSnapshot', queryScope, refreshPeriod],
      })
    },
    onError: (error: unknown) => message.error(errorMessage(error)),
  })
  const visiblePerformanceFeedback =
    performanceFeedback?.period === period ? performanceFeedback : null
  const visibleScheduleFeedback =
    scheduleFeedback?.type === 'success'
      ? scheduleFeedback.result.period === period
        ? scheduleFeedback
        : null
      : scheduleFeedback?.period === period
        ? scheduleFeedback
        : null

  function openEdit(emp: AttendanceListRow) {
    if (attendanceReadUnavailable) return
    // AntD preserves form values by default. Reset correction metadata so a
    // proof/reason for one employee cannot be submitted for another employee.
    form.resetFields()
    setEditing(emp)
    const a = attByEmp.get(emp.id)
    form.setFieldsValue({
      expected_days: a ? Number(a.expected_days) : 22,
      // A day-count adjustment must include a new, auditable reason.  Do not
      // carry the prior reason into the form where it could be reused silently.
      expected_days_adjust_reason: undefined,
      correction_reason: undefined,
      attachment_url: undefined,
      actual_days: a ? Number(a.actual_days) : 0,
      worked_hours: a?.worked_hours == null ? undefined : Number(a.worked_hours),
      rest_days: a ? Number(a.rest_days) : 0,
      overtime_hours: a ? Number(a.overtime_hours) : 0,
      holiday_worked_days: a ? Number(a.holiday_worked_days) : 0,
      leave_days: a ? Number(a.leave_days) : 0,
    })
  }

  function selectPerformanceImport(file: File | undefined) {
    if (!file) return
    if (!isPerformanceImportFile(file)) {
      setPerformanceFeedback({ type: 'error', period, message: '仅支持 .xlsx/.xlsm 文件' })
      return
    }
    setPerformanceFeedback(null)
    importMutation.mutate({ file, period })
  }

  function openScheduleCreate() {
    scheduleForm.resetFields()
    scheduleForm.setFieldsValue({
      weekly_rest_days: [5, 6],
      effective_from: `${period}-01`,
      priority: 0,
      is_active: true,
    })
    setScheduleEditing(null)
    setScheduleModalOpen(true)
  }

  function openScheduleEdit(rule: AttendanceScheduleRule) {
    scheduleForm.resetFields()
    scheduleForm.setFieldsValue({
      ...rule,
      org_unit_id: rule.org_unit_id ?? undefined,
      employment_type: rule.employment_type ?? undefined,
      department: rule.department ?? undefined,
      position_title: rule.position_title ?? undefined,
      is_special_position: rule.is_special_position ?? undefined,
      monthly_expected_days:
        rule.monthly_expected_days === null ? undefined : Number(rule.monthly_expected_days),
      effective_to: rule.effective_to ?? undefined,
    })
    setScheduleEditing(rule)
    setScheduleModalOpen(true)
  }

  function closeScheduleModal() {
    scheduleForm.resetFields()
    setScheduleEditing(null)
    setScheduleModalOpen(false)
  }

  function saveSchedule(values: AttendanceScheduleFormValues) {
    if (!(values.weekly_rest_days?.length ?? 0) && values.monthly_expected_days == null) {
      scheduleForm.setFields([
        {
          name: 'monthly_expected_days',
          errors: ['每周休息日与固定月应出勤至少填写一项'],
        },
      ])
      return
    }
    scheduleSaveMutation.mutate(values)
  }

  const scheduleColumns: TableProps<AttendanceScheduleRule>['columns'] = [
    {
      title: '规则',
      dataIndex: 'name',
      render: (name: string, rule) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{name}</Typography.Text>
          <Typography.Text type="secondary">
            {rule.org_unit_id === null
              ? '全部组织'
              : (orgById.get(rule.org_unit_id) ?? `组织 #${rule.org_unit_id}`)}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '匹配条件',
      render: (_, rule) => {
        const conditions = [
          rule.employment_type ? EMPLOYMENT_LABELS[rule.employment_type] : null,
          rule.department ? DEPARTMENT_LABELS[rule.department] : null,
          rule.position_title,
          rule.is_special_position === true
            ? '特殊岗位'
            : rule.is_special_position === false
              ? '普通岗位'
              : null,
        ].filter((entry): entry is string => Boolean(entry))
        return conditions.length ? conditions.join(' · ') : '不限'
      },
    },
    {
      title: '生成方式',
      render: (_, rule) => {
        const parts: string[] = []
        if (rule.monthly_expected_days !== null) {
          parts.push(`固定 ${rule.monthly_expected_days} 天/月`)
        }
        if (rule.weekly_rest_days.length) {
          const weekdays = new Map(WEEKDAY_OPTIONS.map((option) => [option.value, option.label]))
          parts.push(`每周休 ${rule.weekly_rest_days.map((day) => weekdays.get(day)).join('、')}`)
        }
        return parts.join(' · ')
      },
    },
    {
      title: '有效期',
      render: (_, rule) => `${rule.effective_from} 至 ${rule.effective_to ?? '长期'}`,
    },
    { title: '优先级', dataIndex: 'priority', width: 88 },
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 80,
      render: (active: boolean) => (
        <Tag color={active ? 'green' : 'default'}>{active ? '生效中' : '已停用'}</Tag>
      ),
    },
    ...(canManageSchedules
      ? [
          {
            title: '操作',
            key: 'actions',
            width: 150,
            render: (_: unknown, rule: AttendanceScheduleRule) => (
              <Space>
                <Button size="small" onClick={() => openScheduleEdit(rule)}>
                  编辑
                </Button>
                {rule.is_active ? (
                  <Popconfirm
                    title="停用后将不再参与应出勤生成"
                    okText="确认停用"
                    cancelText="取消"
                    onConfirm={() => scheduleDeactivateMutation.mutate(rule)}
                  >
                    <Button danger size="small">
                      停用
                    </Button>
                  </Popconfirm>
                ) : null}
              </Space>
            ),
          },
        ]
      : []),
  ]

  const columns: TableProps<AttendanceListRow>['columns'] = [
    { title: '工号', dataIndex: 'emp_no' },
    { title: '姓名', dataIndex: 'name' },
    {
      title: '应出勤',
      render: (_: unknown, e: AttendanceListRow) => attByEmp.get(e.id)?.expected_days ?? '—',
    },
    {
      title: '实出勤',
      render: (_: unknown, e: AttendanceListRow) => attByEmp.get(e.id)?.actual_days ?? '—',
    },
    {
      title: '加班(时)',
      render: (_: unknown, e: AttendanceListRow) => attByEmp.get(e.id)?.overtime_hours ?? '—',
    },
    ...(canWrite
      ? [
          {
            title: '操作',
            render: (_: unknown, e: AttendanceListRow) => (
              <Button size="small" disabled={attendanceReadUnavailable} onClick={() => openEdit(e)}>
                录入
              </Button>
            ),
          },
        ]
      : []),
  ]
  const performanceColumns = [
    {
      title: '工号',
      render: (_: unknown, record: PerformanceRecord) =>
        employeeById.get(record.employee_id)?.emp_no ?? `员工 ID #${record.employee_id}`,
    },
    {
      title: '姓名',
      render: (_: unknown, record: PerformanceRecord) =>
        employeeById.get(record.employee_id)?.name ?? '—',
    },
    { title: '绩效系数', dataIndex: 'coefficient' },
    {
      title: '绩效得分',
      dataIndex: 'score',
      render: (score: string | null) => score ?? '—',
    },
    {
      title: '备注',
      dataIndex: 'remark',
      render: (remark: string | null) => remark ?? '—',
    },
  ]

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <label htmlFor="attendance-period">计薪周期：</label>
        <input
          id="attendance-period"
          type="month"
          value={period}
          onChange={(e) => setPeriod(e.target.value)}
          placeholder="YYYY-MM"
          style={{ padding: 4 }}
        />
      </Space>
      {canReadSchedules ? (
        <Card
          title="应出勤规则"
          style={{ marginBottom: 16 }}
          extra={
            canManageSchedules ? (
              <Space wrap>
                <Button onClick={openScheduleCreate}>新建规则</Button>
                <Button
                  type="primary"
                  loading={scheduleGenerateMutation.isPending}
                  disabled={scheduleReadUnavailable}
                  onClick={() => scheduleGenerateMutation.mutate(period)}
                >
                  生成 {period} 应出勤
                </Button>
              </Space>
            ) : undefined
          }
        >
          <Typography.Paragraph type="secondary">
            先按组织、用工类型和岗位匹配规则，再为当月在职员工生成应出勤基线。已有审批调整会保留。
          </Typography.Paragraph>
          {visibleScheduleFeedback?.type === 'success' ? (
            <Alert
              closable
              type="success"
              showIcon
              message={`已生成 ${visibleScheduleFeedback.result.generated} 人，保留人工调整 ${visibleScheduleFeedback.result.adjusted_preserved} 人。`}
              style={{ marginBottom: 16 }}
              onClose={() => setScheduleFeedback(null)}
            />
          ) : visibleScheduleFeedback?.type === 'error' ? (
            <Alert
              closable
              type="error"
              showIcon
              message={visibleScheduleFeedback.message}
              description={
                visibleScheduleFeedback.errors.length ? (
                  <ul style={{ margin: 0, paddingLeft: 20 }}>
                    {visibleScheduleFeedback.errors.map((entry) => (
                      <li key={entry}>{entry}</li>
                    ))}
                  </ul>
                ) : undefined
              }
              style={{ marginBottom: 16 }}
              onClose={() => setScheduleFeedback(null)}
            />
          ) : null}
          {scheduleQuery.isError ? (
            <Alert
              type="error"
              showIcon
              message="应出勤规则加载失败"
              description={errorMessage(scheduleQuery.error)}
              style={{ marginBottom: 16 }}
            />
          ) : null}
          <Table<AttendanceScheduleRule>
            rowKey="id"
            size="small"
            loading={scheduleQuery.isLoading}
            dataSource={scheduleQuery.data ?? []}
            columns={scheduleColumns}
            pagination={{ pageSize: 10 }}
            scroll={{ x: 960 }}
            locale={{ emptyText: '暂无规则，请先新建规则再生成应出勤。' }}
          />
        </Card>
      ) : null}
      {canSyncDingTalk &&
      dingtalkIntegrationQuery.data &&
      !dingtalkIntegrationQuery.data.read_sync_ready ? (
        <Alert
          message="钉钉只读同步尚未启用；不会请求钉钉，也不会推送消息。"
          showIcon
          style={{ marginBottom: 16 }}
          type="info"
        />
      ) : null}
      {canRead ? (
        <>
          {!canReadEmployees ? (
            <Alert
              message="当前账号没有员工目录权限；考勤和绩效记录将仅显示员工 ID。"
              showIcon
              style={{ marginBottom: 16 }}
              type="info"
            />
          ) : empQuery.isError ? (
            <Alert
              description={errorMessage(empQuery.error)}
              message="员工信息加载失败；考勤和绩效列表将仅显示员工 ID。"
              showIcon
              style={{ marginBottom: 16 }}
              type="warning"
            />
          ) : null}
          {attQuery.isError ? (
            <Alert
              description={errorMessage(attQuery.error)}
              message="考勤来源加载失败，已停用考勤录入。"
              showIcon
              style={{ marginBottom: 16 }}
              type="error"
            />
          ) : null}
          {canSyncDingTalk && dingtalkIntegrationQuery.data?.read_sync_ready ? (
            <Card
              title="钉钉考勤"
              style={{ marginBottom: 16 }}
              extra={
                <Button
                  loading={
                    dingtalkRefreshMutation.isPending ||
                    dingtalkSnapshotQuery.data?.status === 'QUEUED' ||
                    dingtalkSnapshotQuery.data?.status === 'RUNNING'
                  }
                  disabled={
                    dingtalkSnapshotQuery.data?.status === 'QUEUED' ||
                    dingtalkSnapshotQuery.data?.status === 'RUNNING'
                  }
                  onClick={() => dingtalkRefreshMutation.mutate(period)}
                >
                  刷新钉钉考勤
                </Button>
              }
            >
              <Alert
                message="这里展示钉钉返回的只读打卡状态，不会写入计薪考勤。"
                showIcon
                style={{ marginBottom: 16 }}
                type="info"
              />
              {dingtalkSnapshotQuery.isError ? (
                <Alert
                  message={`钉钉考勤加载失败：${errorMessage(dingtalkSnapshotQuery.error)}`}
                  showIcon
                  style={{ marginBottom: 16 }}
                  type="error"
                />
              ) : null}
              {dingtalkSnapshotQuery.data?.status === 'NOT_STARTED' ? (
                <Alert
                  message="本周期尚未同步钉钉考勤，请点击“刷新钉钉考勤”。"
                  showIcon
                  style={{ marginBottom: 16 }}
                  type="warning"
                />
              ) : null}
              {dingtalkSnapshotQuery.data?.status === 'QUEUED' ||
              dingtalkSnapshotQuery.data?.status === 'RUNNING' ? (
                <Alert
                  message="正在后台读取钉钉考勤，完成后本页会自动更新。"
                  showIcon
                  style={{ marginBottom: 16 }}
                  type="info"
                />
              ) : null}
              {dingtalkSnapshotQuery.data?.status === 'FAILED' ? (
                <Alert
                  message="最近一次钉钉考勤刷新失败，可稍后重新刷新；已有缓存仍保留。"
                  showIcon
                  style={{ marginBottom: 16 }}
                  type="error"
                />
              ) : null}
              {dingtalkSnapshotQuery.data ? (
                <>
                  <Space wrap style={{ marginBottom: 12 }}>
                    <span>已匹配 {dingtalkSnapshotQuery.data.matched_employees} 名员工</span>
                    <span>{dingtalkSnapshotQuery.data.employees_with_records} 人有记录</span>
                    <span>{`共 ${dingtalkSnapshotQuery.data.total_records} 条打卡结果`}</span>
                    {dingtalkSnapshotQuery.data.refreshed_at ? (
                      <span>
                        更新时间：
                        {new Date(dingtalkSnapshotQuery.data.refreshed_at).toLocaleString('zh-CN')}
                      </span>
                    ) : null}
                  </Space>
                  <Table<DingTalkAttendancePreviewRow>
                    rowKey="employee_id"
                    size="small"
                    loading={dingtalkSnapshotQuery.isLoading}
                    pagination={{ pageSize: 20 }}
                    dataSource={dingtalkSnapshotQuery.data.items}
                    columns={[
                      { title: '工号', dataIndex: 'emp_no' },
                      { title: '姓名', dataIndex: 'name' },
                      { title: '记录数', dataIndex: 'record_count' },
                      { title: '正常', dataIndex: 'normal_count' },
                      { title: '迟到', dataIndex: 'late_count' },
                      { title: '早退', dataIndex: 'early_count' },
                      { title: '旷工', dataIndex: 'absent_count' },
                      { title: '缺卡', dataIndex: 'not_signed_count' },
                      { title: '其他', dataIndex: 'other_count' },
                    ]}
                  />
                </>
              ) : null}
            </Card>
          ) : null}
          <Table
            rowKey="id"
            loading={empQuery.isLoading || attQuery.isLoading}
            columns={columns}
            dataSource={attendanceRows}
            pagination={{ pageSize: 20 }}
          />
          <Card
            title="绩效列表"
            style={{ marginTop: 16 }}
            extra={
              canWrite ? (
                <>
                  <input
                    ref={performanceFileInputRef}
                    aria-label="选择绩效导入文件"
                    type="file"
                    accept={PERFORMANCE_IMPORT_ACCEPT}
                    disabled={importMutation.isPending}
                    onChange={(event) => {
                      const file = event.currentTarget.files?.[0]
                      event.currentTarget.value = ''
                      selectPerformanceImport(file)
                    }}
                    style={{ display: 'none' }}
                  />
                  <Button
                    aria-label="导入绩效"
                    loading={importMutation.isPending}
                    onClick={() => performanceFileInputRef.current?.click()}
                  >
                    导入绩效
                  </Button>
                </>
              ) : null
            }
          >
            {visiblePerformanceFeedback?.type === 'success' ? (
              <Alert
                closable
                description={
                  visiblePerformanceFeedback.result.skipped.length
                    ? `跳过工号：${visiblePerformanceFeedback.result.skipped.join('、')}`
                    : undefined
                }
                message={`绩效导入完成：成功匹配 ${visiblePerformanceFeedback.result.matched} 条，跳过 ${visiblePerformanceFeedback.result.skipped.length} 条。`}
                showIcon
                style={{ marginBottom: 16 }}
                type="success"
                onClose={() => setPerformanceFeedback(null)}
              />
            ) : visiblePerformanceFeedback?.type === 'error' ? (
              <Alert
                closable
                message={`导入失败：${visiblePerformanceFeedback.message}`}
                showIcon
                style={{ marginBottom: 16 }}
                type="error"
                onClose={() => setPerformanceFeedback(null)}
              />
            ) : null}
            {performanceQuery.isError ? (
              <Alert
                message={`绩效列表加载失败：${errorMessage(performanceQuery.error)}`}
                showIcon
                style={{ marginBottom: 16 }}
                type="error"
              />
            ) : null}
            <Table<PerformanceRecord>
              rowKey={(record) => `${record.employee_id}-${record.period}`}
              loading={performanceQuery.isLoading}
              columns={performanceColumns}
              dataSource={performanceQuery.data ?? []}
              pagination={{ pageSize: 20 }}
              size="small"
            />
          </Card>
        </>
      ) : (
        <Alert message="当前账号没有查看考勤和绩效的权限。" showIcon type="warning" />
      )}
      <Modal
        title={scheduleEditing ? '编辑应出勤规则' : '新建应出勤规则'}
        open={scheduleModalOpen}
        okText="保存规则"
        cancelText="取消"
        width={760}
        confirmLoading={scheduleSaveMutation.isPending}
        onCancel={closeScheduleModal}
        onOk={() => scheduleForm.submit()}
        destroyOnHidden
      >
        <Form<AttendanceScheduleFormValues>
          form={scheduleForm}
          layout="vertical"
          preserve={false}
          onFinish={saveSchedule}
        >
          <Alert
            type="info"
            showIcon
            message="优先级越高越先匹配；同级时优先使用条件更具体的规则。"
            style={{ marginBottom: 16 }}
          />
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
              columnGap: 16,
            }}
          >
            <Form.Item
              name="name"
              label="规则名称"
              rules={[{ required: true, message: '请填写规则名称' }, { max: 64 }]}
            >
              <Input maxLength={64} placeholder="例：厅面全职双休" />
            </Form.Item>
            <Form.Item name="org_unit_id" label="适用组织">
              <Select
                allowClear
                showSearch
                disabled={!canReadOrgUnits}
                optionFilterProp="label"
                placeholder={canReadOrgUnits ? '留空表示全部组织' : '全部组织'}
                options={(scheduleOrgQuery.data ?? [])
                  .filter((org) => org.type === 'STORE')
                  .map((org) => ({
                    value: org.id,
                    label: `${org.name} (${org.code})`,
                  }))}
              />
            </Form.Item>
            <Form.Item name="employment_type" label="用工类型">
              <Select
                allowClear
                placeholder="不限"
                options={Object.entries(EMPLOYMENT_LABELS).map(([value, label]) => ({
                  value,
                  label,
                }))}
              />
            </Form.Item>
            <Form.Item name="department" label="所属部门">
              <Select
                allowClear
                placeholder="不限"
                options={Object.entries(DEPARTMENT_LABELS).map(([value, label]) => ({
                  value,
                  label,
                }))}
              />
            </Form.Item>
            <Form.Item name="position_title" label="职位名称">
              <Input maxLength={64} placeholder="留空表示不限" />
            </Form.Item>
            <Form.Item name="is_special_position" label="特殊岗位">
              <Select
                allowClear
                placeholder="不限"
                options={[
                  { value: true, label: '仅特殊岗位' },
                  { value: false, label: '仅普通岗位' },
                ]}
              />
            </Form.Item>
            <Form.Item name="weekly_rest_days" label="每周休息日">
              <Select
                mode="multiple"
                allowClear
                options={WEEKDAY_OPTIONS}
                placeholder="例：周六、周日"
              />
            </Form.Item>
            <Form.Item
              name="monthly_expected_days"
              label="固定月应出勤天数"
              tooltip="填写后优先按固定天数计算，入离职月按自然日比例折算。"
            >
              <InputNumber min={0.01} max={31} precision={2} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item
              name="effective_from"
              label="生效日期"
              rules={[{ required: true, message: '请选择生效日期' }]}
            >
              <Input type="date" />
            </Form.Item>
            <Form.Item name="effective_to" label="失效日期">
              <Input type="date" />
            </Form.Item>
            <Form.Item
              name="priority"
              label="优先级"
              rules={[{ required: true, message: '请填写优先级' }]}
            >
              <InputNumber min={-1000} max={1000} precision={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="is_active" label="规则状态" valuePropName="checked">
              <Switch checkedChildren="启用" unCheckedChildren="停用" />
            </Form.Item>
          </div>
        </Form>
      </Modal>
      <Modal
        title={`录入考勤 · ${editing?.name} · ${period}`}
        open={!!editing}
        onCancel={() => {
          form.resetFields()
          setEditing(null)
        }}
        onOk={() => form.submit()}
        confirmLoading={saveMutation.isPending}
        okButtonProps={{ disabled: attendanceReadUnavailable }}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          preserve={false}
          onFinish={(v) => saveMutation.mutate(v)}
        >
          <Alert
            type="info"
            showIcon
            message="已解锁薪资批次的更正必须填写更正原因；调整应出勤天数还必须填写调整原因。"
            style={{ marginBottom: 16 }}
          />
          <Form.Item name="expected_days" label="应出勤天数" rules={[{ required: true }]}>
            <InputNumber
              min={0}
              max={31}
              disabled={!canAdjustExpectedDays}
              style={{ width: '100%' }}
            />
          </Form.Item>
          <Form.Item name="actual_days" label="实出勤天数" rules={[{ required: true }]}>
            <InputNumber min={0} max={31} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="overtime_hours" label="加班时长(小时)">
            <InputNumber min={0} max={744} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="leave_days" label="请假天数">
            <InputNumber min={0} max={31} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            name="expected_days_adjust_reason"
            label="应出勤调整原因"
            dependencies={['expected_days']}
            rules={[
              ({ getFieldValue }) => ({
                validator: async (_, value) => {
                  const currentAttendance = editing ? attByEmp.get(editing.id) : undefined
                  const originalExpectedDays = currentAttendance
                    ? Number(currentAttendance.expected_days)
                    : 22
                  const requestedExpectedDays = Number(getFieldValue('expected_days'))
                  const changed =
                    Number.isFinite(requestedExpectedDays) &&
                    requestedExpectedDays !== originalExpectedDays
                  if (
                    canAdjustExpectedDays &&
                    changed &&
                    (typeof value !== 'string' || !value.trim())
                  ) {
                    throw new Error('调整应出勤天数必须填写新的调整原因')
                  }
                },
              }),
            ]}
          >
            <Input.TextArea rows={2} maxLength={255} disabled={!canAdjustExpectedDays} />
          </Form.Item>
          <Form.Item name="worked_hours" label="出勤工时">
            <InputNumber min={0} max={744} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="rest_days" label="休息天数">
            <InputNumber min={0} max={31} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="holiday_worked_days" label="法定节假日出勤天数">
            <InputNumber min={0} max={31} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="correction_reason" label="已解锁批次更正原因">
            <Input.TextArea rows={2} maxLength={1000} />
          </Form.Item>
          <Form.Item
            name="attachment_url"
            label="证明附件地址（已解锁批次更正必填）"
            dependencies={['correction_reason']}
            rules={[
              ({ getFieldValue }) => ({
                validator: async (_, value) => {
                  const reason = getFieldValue('correction_reason')
                  if (
                    typeof reason === 'string' &&
                    reason.trim() &&
                    (typeof value !== 'string' || !value.trim())
                  ) {
                    throw new Error('已解锁批次更正必须填写证明附件地址')
                  }
                },
              }),
              { max: 512 },
              { validator: validateHttpUrl },
            ]}
          >
            <Input maxLength={512} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
