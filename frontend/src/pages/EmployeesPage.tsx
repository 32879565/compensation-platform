import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Button,
  Checkbox,
  Alert,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  message,
} from 'antd'
import { useMemo, useRef, useState, type KeyboardEvent } from 'react'

import {
  createEmployee,
  deleteEmployee,
  fetchEmployees,
  fetchGrades,
  fetchOrgUnits,
  updateEmployee,
  type Employee,
  type EmployeeCreateInput,
  type EmployeeUpdateFields,
  type OrgUnit,
} from '../api/masterdata'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'
import SalaryStructureDrawer from '../components/SalaryStructureDrawer'
import { TaxOpeningModal } from '../components/TaxOpeningModal'
import {
  applyDingTalkEmployeeMatches,
  fetchDingTalkIntegration,
  previewDingTalkEmployees,
  type DingTalkEmployeePreview,
} from '../api/dingtalk'

const EMPLOYMENT_LABELS: Record<Employee['employment_type'], string> = {
  FULL_TIME: '全职',
  PART_TIME_HOURLY: '兼职小时工',
  LABOR: '劳务',
}

type EmployeeFormValues = Partial<EmployeeCreateInput> & Pick<EmployeeUpdateFields, 'status'>
type EmployeeSaveRequest = {
  values: EmployeeFormValues
  editing: Employee | null
}

const PII_FIELDS = new Set<keyof EmployeeUpdateFields>(['id_card', 'bank_account'])
const EMPLOYEE_UPDATE_FIELDS = [
  'name',
  'org_unit_id',
  'job_grade_id',
  'employment_type',
  'department',
  'position_title',
  'is_special_position',
  'status',
  'hire_date',
  'probation_end',
  'leave_date',
  'social_city',
  'id_card',
  'bank_account',
] as const satisfies readonly (keyof EmployeeUpdateFields)[]

const EMPLOYEE_CREATE_OPTIONAL_FIELDS = [
  'job_grade_id',
  'employment_type',
  'department',
  'position_title',
  'is_special_position',
  'probation_end',
  'leave_date',
  'social_city',
  'id_card',
  'bank_account',
] as const satisfies readonly (keyof EmployeeCreateInput)[]

const HORIZONTAL_SCROLL_STEP = 80

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

function normalizedFormValue(value: unknown): unknown {
  return value === '' ? null : value
}

function isMaskedPii(value: unknown): boolean {
  return typeof value === 'string' && /[*•]/.test(value)
}

function employeeApiError(error: unknown): { status?: number; detail?: string } {
  if (typeof error !== 'object' || error === null) return {}
  const response = (error as { response?: unknown }).response
  if (typeof response !== 'object' || response === null) return {}
  const { status, data } = response as { status?: unknown; data?: unknown }
  const detail =
    typeof data === 'object' && data !== null ? (data as { detail?: unknown }).detail : undefined
  return {
    status: typeof status === 'number' ? status : undefined,
    detail: typeof detail === 'string' ? detail : undefined,
  }
}

export function isKnownSpecialPosition(value: unknown): boolean {
  if (typeof value !== 'string') return false
  const normalized = value.replace(/\s/g, '').replaceAll('（', '(').replaceAll('）', ')')
  if (normalized.includes('洗碗') || normalized === '寒假工' || normalized === '暑假工') return true
  return (
    (normalized.includes('店长') || normalized.includes('厨师长')) &&
    (normalized.includes('实习') || normalized.includes('储备'))
  )
}

export function buildEmployeeUpdatePayload(
  employee: Employee,
  values: EmployeeFormValues,
): EmployeeUpdateFields {
  const changed: EmployeeUpdateFields = {}
  for (const key of EMPLOYEE_UPDATE_FIELDS) {
    const value = values[key]
    if (value === undefined) continue
    if (PII_FIELDS.has(key)) {
      if (value === null || value === '' || isMaskedPii(value)) continue
    } else if (normalizedFormValue(value) === normalizedFormValue(employee[key])) {
      continue
    }
    Object.assign(changed, { [key]: value })
  }
  return changed
}

function buildEmployeeCreatePayload(values: EmployeeFormValues): EmployeeCreateInput {
  const { emp_no: empNo, name, org_unit_id: orgUnitId, hire_date: hireDate } = values
  if (!empNo || !name || orgUnitId === undefined || !hireDate) {
    throw new Error('请完整填写工号、姓名、所属组织和入职日期')
  }

  const payload: EmployeeCreateInput = {
    emp_no: empNo,
    name,
    org_unit_id: orgUnitId,
    hire_date: hireDate,
  }
  for (const key of EMPLOYEE_CREATE_OPTIONAL_FIELDS) {
    const value = values[key]
    if (value !== undefined) Object.assign(payload, { [key]: value })
  }
  return payload
}

export default function EmployeesPage() {
  const { user, hasPermission } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const canManageEmployees = hasPermission(Perm.EMPLOYEE_WRITE)
  const canManageTaxOpenings = hasPermission(Perm.POLICY_WRITE)
  const canSyncDingTalk = hasPermission(Perm.NOTIFICATION_MANAGE)
  const canReadGrades = hasPermission(Perm.GRADE_READ)
  const canReadSalaryStructures = hasPermission(Perm.STRUCTURE_READ)
  const qc = useQueryClient()

  const [name, setName] = useState('')
  const [page, setPage] = useState(1)
  const [editing, setEditing] = useState<Employee | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [taxOpeningEmployee, setTaxOpeningEmployee] = useState<Employee | null>(null)
  const [salaryStructureEmployee, setSalaryStructureEmployee] = useState<Employee | null>(null)
  const [directoryPreview, setDirectoryPreview] = useState<DingTalkEmployeePreview | null>(null)
  const [form] = Form.useForm<EmployeeFormValues>()
  const saveInFlightRef = useRef(false)

  const orgsQuery = useQuery({ queryKey: ['orgUnits', queryScope], queryFn: fetchOrgUnits })
  const orgName = useMemo(() => {
    const map = new Map<number, string>()
    ;(orgsQuery.data ?? []).forEach((o: OrgUnit) => map.set(o.id, o.name))
    return map
  }, [orgsQuery.data])

  const empQuery = useQuery({
    queryKey: ['employees', queryScope, name, page],
    queryFn: () => fetchEmployees({ name: name || undefined, page, page_size: 20 }),
  })
  const employeesReadReady =
    !empQuery.isLoading && !empQuery.isFetching && !empQuery.isError && empQuery.data !== undefined
  const gradesQuery = useQuery({
    queryKey: ['grades', queryScope, 'all'],
    queryFn: () => fetchGrades({ status: 'all' }),
    enabled: canReadGrades,
  })
  const gradeById = useMemo(
    () => new Map((gradesQuery.data ?? []).map((grade) => [grade.id, grade])),
    [gradesQuery.data],
  )
  const gradesReadReady =
    !canReadGrades ||
    (!gradesQuery.isLoading &&
      !gradesQuery.isFetching &&
      !gradesQuery.isError &&
      gradesQuery.data !== undefined)
  const dingtalkIntegrationQuery = useQuery({
    queryKey: ['dingtalkIntegration', queryScope],
    queryFn: fetchDingTalkIntegration,
    enabled: canSyncDingTalk,
  })

  const saveMutation = useMutation({
    mutationFn: async ({ values, editing: editingSnapshot }: EmployeeSaveRequest) => {
      if (!employeesReadReady) {
        throw new Error('员工目录正在刷新，已禁止使用旧数据提交')
      }
      if (editingSnapshot) {
        const changes = buildEmployeeUpdatePayload(editingSnapshot, values)
        if (Object.hasOwn(changes, 'job_grade_id') && !gradesReadReady) {
          throw new Error('职级目录尚未完整读取，已禁止修改员工职级')
        }
        return updateEmployee(editingSnapshot.id, {
          ...changes,
          expected_version: editingSnapshot.version,
        })
      }
      if (values.job_grade_id != null && !gradesReadReady) {
        throw new Error('职级目录尚未完整读取，已禁止分配员工职级')
      }
      return createEmployee(buildEmployeeCreatePayload(values))
    },
    onSuccess: async () => {
      message.success('已保存')
      setModalOpen(false)
      setEditing(null)
      form.resetFields()
      await qc.invalidateQueries({ queryKey: ['employees', queryScope] })
    },
    onError: async (e: unknown, request) => {
      const { status, detail } = employeeApiError(e)
      const isEditingConflict = status === 409 && request.editing !== null
      if (isEditingConflict) {
        form.resetFields()
        setModalOpen(false)
        setEditing(null)
      }
      message.error(
        detail ?? (e instanceof Error ? e.message : status === 404 ? '所属组织不可见' : '保存失败'),
      )
      if (isEditingConflict) {
        await qc.invalidateQueries({ queryKey: ['employees', queryScope] })
      }
    },
    onSettled: () => {
      saveInFlightRef.current = false
    },
  })

  const deleteMutation = useMutation({
    mutationFn: async (employeeId: number) => {
      if (!employeesReadReady) {
        throw new Error('员工目录正在刷新，已禁止使用旧数据删除')
      }
      return deleteEmployee(employeeId)
    },
    onSuccess: async () => {
      message.success('已删除')
      await qc.invalidateQueries({ queryKey: ['employees', queryScope] })
    },
  })
  const directoryPreviewMutation = useMutation({
    mutationFn: previewDingTalkEmployees,
    onSuccess: setDirectoryPreview,
    onError: (error: unknown) => message.error(errorMessage(error)),
  })
  const directoryApplyMutation = useMutation({
    mutationFn: applyDingTalkEmployeeMatches,
    onSuccess: (result) => {
      message.success(`已建立 ${result.linked} 个钉钉员工绑定`)
      setDirectoryPreview(null)
      void qc.invalidateQueries({ queryKey: ['employees', queryScope] })
    },
    onError: (error: unknown) => message.error(errorMessage(error)),
  })

  function errorMessage(error: unknown): string {
    const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    return typeof detail === 'string' ? detail : '钉钉目录读取失败'
  }

  function openCreate() {
    if (!employeesReadReady) return
    setEditing(null)
    form.resetFields()
    setModalOpen(true)
  }

  function openEdit(emp: Employee) {
    if (!employeesReadReady) return
    setEditing(emp)
    const safeFields: EmployeeFormValues = {
      emp_no: emp.emp_no,
      name: emp.name,
      org_unit_id: emp.org_unit_id,
      job_grade_id: emp.job_grade_id,
      employment_type: emp.employment_type,
      department: emp.department,
      position_title: emp.position_title,
      is_special_position: emp.is_special_position,
      hire_date: emp.hire_date ?? undefined,
      probation_end: emp.probation_end,
      leave_date: emp.leave_date,
      social_city: emp.social_city,
    }
    form.resetFields()
    form.setFieldsValue({ ...safeFields, id_card: undefined, bank_account: undefined })
    setModalOpen(true)
  }

  const columns = [
    { title: '工号', dataIndex: 'emp_no' },
    { title: '姓名', dataIndex: 'name' },
    {
      title: '所属组织',
      dataIndex: 'org_unit_id',
      render: (id: number) => orgName.get(id) ?? id,
    },
    {
      title: '用工类型',
      dataIndex: 'employment_type',
      render: (t: Employee['employment_type']) => EMPLOYMENT_LABELS[t],
    },
    {
      title: '职位',
      dataIndex: 'position_title',
      render: (title: string | null) => title || '未填写',
    },
    ...(canReadGrades
      ? [
          {
            title: '职级',
            dataIndex: 'job_grade_id',
            render: (gradeId: number | null) => {
              if (gradeId === null) return <Tag>未分配</Tag>
              const grade = gradeById.get(gradeId)
              if (!grade) return <Tag color="default">职级目录不可用</Tag>
              return (
                <Space size={4}>
                  <span>
                    {grade.code} · {grade.name}
                  </span>
                  {!grade.is_active && <Tag color="default">已停用</Tag>}
                </Space>
              )
            },
          },
        ]
      : []),
    {
      title: '出勤核算',
      dataIndex: 'is_special_position',
      render: (special: boolean, employee: Employee) =>
        special ? (
          <Tag color="purple">特殊岗位 · 审批天数</Tag>
        ) : (
          <Tag>
            {employee.department === 'DINING'
              ? '厅面 · 9小时/天'
              : employee.department === 'KITCHEN'
                ? '厨房 · 9.5小时/天'
                : '按岗位规则'}
          </Tag>
        ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      render: (s: string) => <Tag color={s === 'ACTIVE' ? 'green' : 'default'}>{s}</Tag>,
    },
    {
      title: '钉钉',
      dataIndex: 'dingtalk_linked',
      render: (linked: boolean) => (
        <Tag color={linked ? 'blue' : 'default'}>{linked ? '已绑定' : '未绑定'}</Tag>
      ),
    },
    { title: '身份证', dataIndex: 'id_card' },
    ...(canManageEmployees || canManageTaxOpenings || canReadSalaryStructures
      ? [
          {
            title: '操作',
            render: (_: unknown, emp: Employee) => (
              <Space>
                {canManageEmployees && (
                  <>
                    <Button
                      size="small"
                      disabled={
                        !employeesReadReady || saveMutation.isPending || deleteMutation.isPending
                      }
                      onClick={() => openEdit(emp)}
                    >
                      编辑
                    </Button>
                    <Popconfirm title="确认删除？" onConfirm={() => deleteMutation.mutate(emp.id)}>
                      <Button
                        size="small"
                        danger
                        disabled={
                          !employeesReadReady || saveMutation.isPending || deleteMutation.isPending
                        }
                      >
                        删除
                      </Button>
                    </Popconfirm>
                  </>
                )}
                {canManageTaxOpenings && (
                  <Button size="small" onClick={() => setTaxOpeningEmployee(emp)}>
                    个税开账
                  </Button>
                )}
                {canReadSalaryStructures && (
                  <Button size="small" onClick={() => setSalaryStructureEmployee(emp)}>
                    薪资结构
                  </Button>
                )}
              </Space>
            ),
          },
        ]
      : []),
  ]

  return (
    <div>
      <Space wrap style={{ marginBottom: 16 }}>
        <Input.Search
          placeholder="按姓名搜索"
          allowClear
          onSearch={(v) => {
            setName(v)
            setPage(1)
          }}
          style={{ width: 240 }}
        />
        {canManageEmployees && (
          <Button
            type="primary"
            disabled={!employeesReadReady || saveMutation.isPending || deleteMutation.isPending}
            onClick={openCreate}
          >
            新增员工
          </Button>
        )}
        {canSyncDingTalk && dingtalkIntegrationQuery.data && (
          <Button
            disabled={!dingtalkIntegrationQuery.data.read_sync_ready}
            loading={directoryPreviewMutation.isPending}
            onClick={() => directoryPreviewMutation.mutate()}
          >
            预览钉钉员工
          </Button>
        )}
      </Space>
      {canSyncDingTalk &&
      dingtalkIntegrationQuery.data &&
      !dingtalkIntegrationQuery.data.read_sync_ready ? (
        <Alert
          message="钉钉只读同步尚未启用；消息推送仍保持 sandbox。"
          showIcon
          style={{ marginBottom: 16 }}
          type="info"
        />
      ) : null}
      <div
        role="region"
        aria-label="员工岗位目录"
        tabIndex={0}
        style={{ overflowX: 'auto' }}
        onKeyDown={handleHorizontalRegionKeyDown}
      >
        <div style={{ minWidth: 1380 }}>
          <Table
            rowKey="id"
            loading={empQuery.isLoading}
            columns={columns}
            dataSource={empQuery.data?.items ?? []}
            pagination={{
              current: page,
              pageSize: 20,
              total: empQuery.data?.total ?? 0,
              onChange: setPage,
              showTotal: (t) => `共 ${t} 人`,
            }}
          />
        </div>
      </div>
      <Modal
        title={editing ? '编辑员工' : '新增员工'}
        open={modalOpen}
        onCancel={() => {
          if (saveInFlightRef.current || saveMutation.isPending) return
          form.resetFields()
          setModalOpen(false)
          setEditing(null)
        }}
        onOk={() => {
          if (!saveInFlightRef.current) form.submit()
        }}
        confirmLoading={saveMutation.isPending}
        cancelButtonProps={{ disabled: saveMutation.isPending }}
        closable={!saveMutation.isPending}
        maskClosable={!saveMutation.isPending}
        destroyOnHidden
      >
        <Form<EmployeeFormValues>
          form={form}
          layout="vertical"
          clearOnDestroy
          onFinish={(v) => {
            if (saveInFlightRef.current) return
            saveInFlightRef.current = true
            saveMutation.mutate({ values: v, editing })
          }}
          onValuesChange={(changed) => {
            if (isKnownSpecialPosition(changed.position_title)) {
              form.setFieldValue('is_special_position', true)
            }
          }}
        >
          <Form.Item name="emp_no" label="工号" rules={[{ required: true }]}>
            <Input disabled={!!editing} />
          </Form.Item>
          <Form.Item name="name" label="姓名" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="org_unit_id" label="所属组织" rules={[{ required: true }]}>
            <Select
              options={(orgsQuery.data ?? [])
                .filter((o) => o.type === 'STORE')
                .map((o) => ({ value: o.id, label: o.name }))}
              showSearch
              optionFilterProp="label"
              virtual={false}
            />
          </Form.Item>
          <Form.Item name="employment_type" label="用工类型" initialValue="FULL_TIME">
            <Select
              options={Object.entries(EMPLOYMENT_LABELS).map(([v, l]) => ({ value: v, label: l }))}
            />
          </Form.Item>
          <Form.Item name="department" label="部门" initialValue="OTHER">
            <Select
              options={[
                { value: 'DINING', label: '厅面' },
                { value: 'KITCHEN', label: '厨房' },
                { value: 'OTHER', label: '其他' },
              ]}
            />
          </Form.Item>
          {canReadGrades && (
            <>
              {gradesQuery.isError && (
                <Alert
                  message="职级目录加载失败，已禁止修改职级"
                  showIcon
                  style={{ marginBottom: 16 }}
                  type="error"
                />
              )}
              <Form.Item name="job_grade_id" label="员工职级">
                <Select
                  allowClear
                  onChange={(value) =>
                    form.setFieldValue('job_grade_id', value === 0 ? null : (value ?? null))
                  }
                  disabled={!gradesReadReady}
                  loading={gradesQuery.isLoading || gradesQuery.isFetching}
                  options={[
                    { value: 0, label: '未分配职级' },
                    ...(gradesQuery.data ?? []).map((grade) => ({
                      value: grade.id,
                      label: `${grade.code} · ${grade.name}${grade.is_active ? '' : '（已停用）'}`,
                      disabled: !grade.is_active,
                      'aria-disabled': !grade.is_active,
                    })),
                  ]}
                  optionFilterProp="label"
                  placeholder={gradesQuery.isError ? '职级目录不可用' : '请选择启用职级'}
                  showSearch
                  virtual={false}
                />
              </Form.Item>
            </>
          )}
          <Form.Item
            name="position_title"
            label="职位名称"
            rules={[{ max: 64, message: '职位名称最多 64 个字符' }]}
            extra="请填写实际职位；店长（实习/储备）、厨师长（实习/储备）、洗碗岗位、寒假工、暑假工及公司指定的其他职位需同时勾选特殊岗位。"
          >
            <Input placeholder="例如：储备店长、洗碗、暑假工" maxLength={64} />
          </Form.Item>
          <Form.Item
            name="hire_date"
            label="入职日期"
            rules={[{ required: true, message: '请填写入职日期' }]}
          >
            <Input type="date" />
          </Form.Item>
          {editing && (
            <Form.Item name="leave_date" label="离职日期">
              <Input type="date" />
            </Form.Item>
          )}
          <Form.Item
            name="is_special_position"
            label="特殊岗位(按天数核算)"
            valuePropName="checked"
            initialValue={false}
          >
            <Checkbox>是，按审批确认的实际出勤天数核算</Checkbox>
          </Form.Item>
          <Form.Item name="social_city" label="社保城市">
            <Input />
          </Form.Item>
          <Form.Item name="id_card" label="身份证号">
            <Input />
          </Form.Item>
          <Form.Item name="bank_account" label="银行卡号">
            <Input />
          </Form.Item>
        </Form>
      </Modal>
      <TaxOpeningModal
        employee={taxOpeningEmployee}
        open={taxOpeningEmployee !== null}
        onClose={() => setTaxOpeningEmployee(null)}
      />
      <SalaryStructureDrawer
        employee={salaryStructureEmployee}
        open={salaryStructureEmployee !== null}
        onClose={() => setSalaryStructureEmployee(null)}
      />
      <Modal
        title="钉钉员工目录预览"
        open={directoryPreview !== null}
        width={900}
        onCancel={() => setDirectoryPreview(null)}
        footer={[
          <Button key="cancel" onClick={() => setDirectoryPreview(null)}>
            取消
          </Button>,
          <Button
            key="apply"
            type="primary"
            disabled={!directoryPreview?.matched || directoryPreview.truncated}
            loading={directoryApplyMutation.isPending}
            onClick={() => {
              if (directoryPreview && !directoryPreview.truncated) {
                directoryApplyMutation.mutate()
              }
            }}
          >
            确认绑定安全匹配项
          </Button>,
        ]}
      >
        {directoryPreview ? (
          <>
            <Alert
              message="只绑定已有稳定标识、唯一工号或唯一姓名；重名人员不会自动绑定。"
              showIcon
              style={{ marginBottom: 16 }}
              type="info"
            />
            {directoryPreview.truncated ? (
              <Alert
                message="预览结果不完整，已阻止确认绑定。请缩小钉钉目录范围或调整同步上限后重新预览。"
                showIcon
                style={{ marginBottom: 16 }}
                type="error"
              />
            ) : null}
            <p>
              钉钉共 {directoryPreview.total_remote_users} 人；可安全匹配 {directoryPreview.matched}{' '}
              人； 重名/冲突 {directoryPreview.ambiguous} 人；未匹配 {directoryPreview.unmatched}{' '}
              人。
            </p>
            <Table
              rowKey="employee_id"
              size="small"
              pagination={{ pageSize: 10 }}
              dataSource={directoryPreview.items}
              columns={[
                { title: '工号', dataIndex: 'emp_no' },
                { title: '本地姓名', dataIndex: 'local_name' },
                { title: '钉钉姓名', dataIndex: 'dingtalk_name' },
                { title: '钉钉工号', dataIndex: 'dingtalk_job_number', render: (v) => v ?? '—' },
                {
                  title: '匹配依据',
                  dataIndex: 'match_method',
                  render: (method) =>
                    ({ STABLE_ID: '已有绑定', JOB_NUMBER: '唯一工号', UNIQUE_NAME: '唯一姓名' })[
                      method as 'STABLE_ID' | 'JOB_NUMBER' | 'UNIQUE_NAME'
                    ],
                },
              ]}
            />
          </>
        ) : null}
      </Modal>
    </div>
  )
}
