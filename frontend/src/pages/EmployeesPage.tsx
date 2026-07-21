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
import { useMemo, useState } from 'react'

import {
  createEmployee,
  deleteEmployee,
  fetchEmployees,
  fetchOrgUnits,
  updateEmployee,
  type Employee,
  type OrgUnit,
} from '../api/masterdata'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'
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

const PII_FIELDS = new Set<keyof Employee>(['id_card', 'bank_account'])

function normalizedFormValue(value: unknown): unknown {
  return value === '' ? null : value
}

function isMaskedPii(value: unknown): boolean {
  return typeof value === 'string' && /[*•]/.test(value)
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
  values: Partial<Employee>,
): Partial<Employee> {
  const changed: Partial<Employee> = {}
  for (const [rawKey, value] of Object.entries(values)) {
    const key = rawKey as keyof Employee
    if (PII_FIELDS.has(key)) {
      if (value === undefined || value === null || value === '' || isMaskedPii(value)) continue
    } else if (normalizedFormValue(value) === normalizedFormValue(employee[key])) {
      continue
    }
    Object.assign(changed, { [key]: value })
  }
  return changed
}

export default function EmployeesPage() {
  const { user, hasPermission } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const canManageEmployees = hasPermission(Perm.EMPLOYEE_WRITE)
  const canManageTaxOpenings = hasPermission(Perm.POLICY_WRITE)
  const canSyncDingTalk = hasPermission(Perm.NOTIFICATION_MANAGE)
  const qc = useQueryClient()

  const [name, setName] = useState('')
  const [page, setPage] = useState(1)
  const [editing, setEditing] = useState<Employee | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [taxOpeningEmployee, setTaxOpeningEmployee] = useState<Employee | null>(null)
  const [directoryPreview, setDirectoryPreview] = useState<DingTalkEmployeePreview | null>(null)
  const [form] = Form.useForm()

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
  const dingtalkIntegrationQuery = useQuery({
    queryKey: ['dingtalkIntegration', queryScope],
    queryFn: fetchDingTalkIntegration,
    enabled: canSyncDingTalk,
  })

  const saveMutation = useMutation({
    mutationFn: async (values: Partial<Employee>) => {
      if (editing) return updateEmployee(editing.id, buildEmployeeUpdatePayload(editing, values))
      return createEmployee(values)
    },
    onSuccess: () => {
      message.success('已保存')
      setModalOpen(false)
      setEditing(null)
      void qc.invalidateQueries({ queryKey: ['employees', queryScope] })
    },
    onError: (e: unknown) => {
      const response = (e as { response?: { status?: number; data?: { detail?: string } } })
        .response
      message.error(
        response?.data?.detail ?? (response?.status === 404 ? '所属组织不可见' : '保存失败'),
      )
    },
  })

  const deleteMutation = useMutation({
    mutationFn: deleteEmployee,
    onSuccess: () => {
      message.success('已删除')
      void qc.invalidateQueries({ queryKey: ['employees', queryScope] })
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
    setEditing(null)
    form.resetFields()
    setModalOpen(true)
  }

  function openEdit(emp: Employee) {
    setEditing(emp)
    const safeFields: Partial<Employee> = { ...emp }
    delete safeFields.id_card
    delete safeFields.bank_account
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
    ...(canManageEmployees || canManageTaxOpenings
      ? [
          {
            title: '操作',
            render: (_: unknown, emp: Employee) => (
              <Space>
                {canManageEmployees && (
                  <>
                    <Button size="small" onClick={() => openEdit(emp)}>
                      编辑
                    </Button>
                    <Popconfirm title="确认删除？" onConfirm={() => deleteMutation.mutate(emp.id)}>
                      <Button size="small" danger>
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
          <Button type="primary" onClick={openCreate}>
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
      <div role="region" aria-label="员工岗位目录" tabIndex={0} style={{ overflowX: 'auto' }}>
        <Table
          rowKey="id"
          loading={empQuery.isLoading}
          columns={columns}
          dataSource={empQuery.data?.items ?? []}
          scroll={{ x: 1380 }}
          pagination={{
            current: page,
            pageSize: 20,
            total: empQuery.data?.total ?? 0,
            onChange: setPage,
            showTotal: (t) => `共 ${t} 人`,
          }}
        />
      </div>
      <Modal
        title={editing ? '编辑员工' : '新增员工'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={saveMutation.isPending}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(v) => saveMutation.mutate(v)}
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
