import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
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
import { useMemo, useState } from 'react'

import {
  fetchMonthlyPayrollAdjustments,
  upsertMonthlyPayrollAdjustment,
  type MonthlyPayrollAdjustment,
  type MonthlyPayrollAdjustmentInput,
  type MonthlyPayrollAdjustmentType,
} from '../api/payrollAdjustments'
import { fetchEmployees, type Employee } from '../api/masterdata'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'
import { safeHttpUrl, validateHttpUrl } from '../utils/safeExternalUrl'

interface AdjustmentFormValues extends MonthlyPayrollAdjustmentInput {
  employee_id: number
  adjustment_type: MonthlyPayrollAdjustmentType
}

const typeLabels: Record<MonthlyPayrollAdjustmentType, string> = {
  PREV_MAKEUP: '上月补发',
  PREV_DEDUCT: '上月补扣',
}

const booleanOptions = [
  { value: true, label: '是' },
  { value: false, label: '否' },
]

function classificationTag(value: boolean | null) {
  if (value === null) return <Tag color="warning">待复核</Tag>
  return <Tag color={value ? 'blue' : 'default'}>{value ? '是' : '否'}</Tag>
}

function currentPeriod(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

async function fetchAllEmployees(): Promise<Employee[]> {
  const firstPage = await fetchEmployees({ page: 1, page_size: 500 })
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

function errorDetail(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
  return typeof detail === 'string' ? detail : '保存失败，请检查该月份是否已进入核算或锁定'
}

export default function MonthlyPayrollAdjustmentsPage() {
  const { user, hasPermission } = useAuth()
  const queryClient = useQueryClient()
  const [period, setPeriod] = useState(currentPeriod)
  const [employeeFilter, setEmployeeFilter] = useState<number | undefined>()
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<MonthlyPayrollAdjustment | null>(null)
  const [form] = Form.useForm<AdjustmentFormValues>()
  const canCorrect = hasPermission(Perm.PAYROLL_CORRECT)
  const canReadEmployees = hasPermission(Perm.EMPLOYEE_READ)

  const adjustmentsQuery = useQuery({
    queryKey: ['monthly-payroll-adjustments', user?.username, period, employeeFilter],
    queryFn: () => fetchMonthlyPayrollAdjustments(period, employeeFilter),
    enabled: canCorrect,
  })
  const employeesQuery = useQuery({
    queryKey: ['monthly-payroll-adjustments', 'employees', user?.username],
    queryFn: fetchAllEmployees,
    enabled: canReadEmployees,
  })
  const sourceMutationBlocked =
    adjustmentsQuery.isLoading ||
    adjustmentsQuery.isError ||
    (canReadEmployees && (employeesQuery.isLoading || employeesQuery.isError))

  const employeesById = useMemo(
    () => new Map((employeesQuery.data ?? []).map((employee) => [employee.id, employee])),
    [employeesQuery.data],
  )
  const employeeOptions = (employeesQuery.data ?? []).map((employee) => ({
    value: employee.id,
    label: `${employee.emp_no} · ${employee.name}`,
  }))

  const saveMutation = useMutation({
    mutationFn: (values: AdjustmentFormValues) => {
      if (sourceMutationBlocked) throw new Error('计薪来源尚未完整读取')
      const { employee_id, adjustment_type, ...input } = values
      return upsertMonthlyPayrollAdjustment(
        editing?.employee_id ?? employee_id,
        period,
        editing?.adjustment_type ?? adjustment_type,
        input,
      )
    },
    onSuccess: async () => {
      setOpen(false)
      setEditing(null)
      form.resetFields()
      await queryClient.invalidateQueries({ queryKey: ['monthly-payroll-adjustments'] })
      message.success('补发/补扣来源已保存并将进入本月核算')
    },
    onError: (error) => message.error(errorDetail(error)),
  })

  const openCreate = () => {
    form.resetFields()
    setEditing(null)
    if (employeeFilter !== undefined) form.setFieldValue('employee_id', employeeFilter)
    setOpen(true)
  }

  const openEdit = (record: MonthlyPayrollAdjustment) => {
    setEditing(record)
    form.setFieldsValue({
      employee_id: record.employee_id,
      adjustment_type: record.adjustment_type,
      amount: Number(record.amount),
      reason: record.reason,
      attachment_url: record.attachment_url,
      taxable: record.taxable ?? undefined,
      in_social_base: record.in_social_base ?? undefined,
      in_housing_base: record.in_housing_base ?? undefined,
    })
    setOpen(true)
  }

  const columns = [
    {
      title: '员工',
      dataIndex: 'employee_id',
      width: 180,
      render: (employeeId: number) => {
        const employee = employeesById.get(employeeId)
        return employee ? `${employee.emp_no} · ${employee.name}` : `员工 #${employeeId}`
      },
    },
    {
      title: '项目',
      dataIndex: 'adjustment_type',
      width: 120,
      render: (value: MonthlyPayrollAdjustmentType) => (
        <Tag color={value === 'PREV_MAKEUP' ? 'green' : 'orange'}>{typeLabels[value]}</Tag>
      ),
    },
    { title: '金额', dataIndex: 'amount', width: 120 },
    { title: '原因', dataIndex: 'reason', minWidth: 220 },
    {
      title: '计入个税',
      dataIndex: 'taxable',
      width: 100,
      render: classificationTag,
    },
    {
      title: '计入社保',
      dataIndex: 'in_social_base',
      width: 100,
      render: classificationTag,
    },
    {
      title: '计入公积金',
      dataIndex: 'in_housing_base',
      width: 110,
      render: classificationTag,
    },
    {
      title: '依据',
      dataIndex: 'attachment_url',
      width: 110,
      render: (url: string) => {
        const safeUrl = safeHttpUrl(url)
        return safeUrl ? (
          <Typography.Link href={safeUrl} target="_blank" rel="noreferrer">
            查看依据
          </Typography.Link>
        ) : (
          <Typography.Text type="danger">无效依据地址</Typography.Text>
        )
      },
    },
    {
      title: '创建人',
      key: 'created_by',
      width: 220,
      render: (_: unknown, record: MonthlyPayrollAdjustment) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>创建人 #{record.created_by}</Typography.Text>
          <Typography.Text type="secondary">
            {new Date(record.created_at).toLocaleString()}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '最后修改人',
      key: 'updated_by',
      width: 220,
      render: (_: unknown, record: MonthlyPayrollAdjustment) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>最后修改人 #{record.updated_by}</Typography.Text>
          <Typography.Text type="secondary">
            {new Date(record.updated_at).toLocaleString()}
          </Typography.Text>
        </Space>
      ),
    },
    ...(canCorrect
      ? [
          {
            title: '操作',
            key: 'action',
            width: 90,
            render: (_: unknown, record: MonthlyPayrollAdjustment) => (
              <Button
                size="small"
                disabled={sourceMutationBlocked}
                onClick={() => openEdit(record)}
              >
                修改
              </Button>
            ),
          },
        ]
      : []),
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card style={{ borderTop: '3px solid #6f42c1' }}>
        <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
          <div>
            <Typography.Text type="secondary">人工计薪来源 · 原因与依据必填</Typography.Text>
            <Typography.Title level={3} style={{ margin: '4px 0 0' }}>
              上月补发 / 补扣
            </Typography.Title>
          </div>
          {canCorrect && canReadEmployees ? (
            <Button type="primary" disabled={sourceMutationBlocked} onClick={openCreate}>
              登记补发或补扣
            </Button>
          ) : null}
        </Space>
      </Card>

      <Alert
        type="info"
        showIcon
        message="应发工资 = 出勤工资 + 加班工资 + 法定工资 + 补贴 + 房补 + 上月补发 − 上月补扣"
        description="这里登记的是进入所选月份核算的来源数据；系统保留操作人、原因和证明依据，不允许直接改最终工资。"
      />

      {adjustmentsQuery.isError ? (
        <Alert type="error" showIcon message="无法读取补发补扣来源，已停用登记和修改" />
      ) : null}
      {canReadEmployees && employeesQuery.isError ? (
        <Alert type="error" showIcon message="无法读取员工目录，已停用登记和修改" />
      ) : null}

      <Card title="月度调整来源">
        <Space wrap style={{ marginBottom: 16 }}>
          <Input
            aria-label="计薪月份"
            type="month"
            value={period}
            onChange={(event) => setPeriod(event.target.value)}
            style={{ width: 150 }}
          />
          {canReadEmployees ? (
            <Select
              aria-label="筛选员工"
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="全部员工"
              value={employeeFilter}
              onChange={(value) => setEmployeeFilter(value)}
              options={employeeOptions}
              style={{ width: 300 }}
            />
          ) : null}
        </Space>
        <div role="region" aria-label="上月补发补扣来源账本" tabIndex={0}>
          <Table<MonthlyPayrollAdjustment>
            rowKey="id"
            loading={adjustmentsQuery.isLoading}
            dataSource={adjustmentsQuery.data ?? []}
            columns={columns}
            pagination={false}
            scroll={{ x: 1580 }}
            locale={{ emptyText: '本月尚无补发或补扣来源' }}
          />
        </div>
      </Card>

      <Modal
        title={`登记 ${period} 补发或补扣`}
        open={open}
        onCancel={() => {
          setOpen(false)
          setEditing(null)
          form.resetFields()
        }}
        onOk={() => form.submit()}
        confirmLoading={saveMutation.isPending}
        okButtonProps={{ disabled: sourceMutationBlocked }}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" onFinish={(values) => saveMutation.mutate(values)}>
          <Form.Item name="employee_id" label="员工" rules={[{ required: true }]}>
            <Select
              showSearch
              optionFilterProp="label"
              options={employeeOptions}
              disabled={editing !== null}
            />
          </Form.Item>
          <Form.Item name="adjustment_type" label="项目" rules={[{ required: true }]}>
            <Select
              disabled={editing !== null}
              options={Object.entries(typeLabels).map(([value, label]) => ({ value, label }))}
            />
          </Form.Item>
          <Form.Item name="amount" label="金额" rules={[{ required: true }]}>
            <InputNumber min={0.01} precision={2} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="reason" label="原因" rules={[{ required: true, max: 2000 }]}>
            <Input.TextArea rows={3} maxLength={2000} />
          </Form.Item>
          <Form.Item
            name="taxable"
            label="计入个税计税"
            rules={[{ required: true, message: '请选择是否计入个税计税' }]}
          >
            <Select placeholder="请选择" options={booleanOptions} />
          </Form.Item>
          <Form.Item
            name="in_social_base"
            label="计入社保基数"
            rules={[{ required: true, message: '请选择是否计入社保基数' }]}
          >
            <Select placeholder="请选择" options={booleanOptions} />
          </Form.Item>
          <Form.Item
            name="in_housing_base"
            label="计入公积金基数"
            rules={[{ required: true, message: '请选择是否计入公积金基数' }]}
          >
            <Select placeholder="请选择" options={booleanOptions} />
          </Form.Item>
          <Form.Item
            name="attachment_url"
            label="证明依据地址"
            rules={[{ required: true }, { max: 512 }, { validator: validateHttpUrl }]}
          >
            <Input placeholder="https://…" />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
