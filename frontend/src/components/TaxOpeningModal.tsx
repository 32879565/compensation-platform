import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  message,
} from 'antd'
import { useEffect, useState } from 'react'

import {
  createTaxOpening,
  fetchTaxOpenings,
  finalizeTaxOpening,
  supersedeTaxOpening,
  updateTaxOpening,
  type TaxOpening,
  type TaxOpeningInput,
} from '../api/payrollPolicies'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'
import type { Employee } from '../api/masterdata'

type OpeningEmployee = Pick<Employee, 'id' | 'emp_no' | 'name' | 'hire_date'>
type EditMode = 'create' | 'edit' | 'supersede'

interface EditState {
  mode: EditMode
  opening: TaxOpening | null
}

interface TaxOpeningModalProps {
  employee: OpeningEmployee | null
  open: boolean
  onClose: () => void
}

function currentPeriod(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

function employmentMonths(employee: OpeningEmployee, period: string): number {
  const [yearText, monthText] = period.split('-')
  const year = Number(yearText)
  const month = Number(monthText)
  const hire = employee.hire_date ? new Date(`${employee.hire_date}T00:00:00`) : null
  if (!hire || !Number.isInteger(year) || !Number.isInteger(month)) return 0
  const startYear = Math.max(hire.getFullYear(), year)
  const startMonth = startYear === hire.getFullYear() ? hire.getMonth() + 1 : 1
  if (startYear !== year || startMonth > month) return 0
  return month - startMonth + 1
}

function errorMessage(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
  return typeof detail === 'string' ? detail : fallback
}

function initialOpeningValues(employee: OpeningEmployee, opening?: TaxOpening | null): TaxOpeningInput {
  if (opening) {
    return {
      tax_year: opening.tax_year,
      through_period: opening.through_period,
      employment_months_to_date: opening.employment_months_to_date,
      taxable_income: opening.taxable_income,
      employee_contribution: opening.employee_contribution,
      special_deduction: opening.special_deduction,
      tax_withheld: opening.tax_withheld,
      evidence_ref: opening.evidence_ref,
    }
  }
  const through_period = currentPeriod()
  return {
    tax_year: Number(through_period.slice(0, 4)),
    through_period,
    employment_months_to_date: employmentMonths(employee, through_period),
    taxable_income: '0',
    employee_contribution: '0',
    special_deduction: '0',
    tax_withheld: '0',
    evidence_ref: '',
  }
}

function toOpeningInput(values: TaxOpeningInput): TaxOpeningInput {
  return {
    tax_year: Number(values.tax_year),
    through_period: values.through_period,
    employment_months_to_date: Number(values.employment_months_to_date),
    taxable_income: String(values.taxable_income).trim(),
    employee_contribution: String(values.employee_contribution).trim(),
    special_deduction: String(values.special_deduction).trim(),
    tax_withheld: String(values.tax_withheld).trim(),
    evidence_ref: values.evidence_ref.trim(),
  }
}

export function TaxOpeningModal({ employee, open, onClose }: TaxOpeningModalProps) {
  const { user, hasPermission } = useAuth()
  const queryClient = useQueryClient()
  const queryScope = user?.username ?? 'anonymous'
  const canWrite = hasPermission(Perm.POLICY_WRITE)
  const [editor, setEditor] = useState<EditState | null>(null)
  const [form] = Form.useForm<TaxOpeningInput>()

  const openingsQuery = useQuery({
    queryKey: ['taxOpenings', queryScope, employee?.id],
    queryFn: () => fetchTaxOpenings(employee!.id),
    enabled: open && !!employee && canWrite,
  })

  useEffect(() => {
    if (editor && employee) form.setFieldsValue(initialOpeningValues(employee, editor.opening))
  }, [editor, employee, form])

  const invalidate = () => {
    if (employee) {
      return queryClient.invalidateQueries({ queryKey: ['taxOpenings', queryScope, employee.id] })
    }
    return Promise.resolve()
  }

  const createMutation = useMutation({
    mutationFn: (payload: TaxOpeningInput) => createTaxOpening(employee!.id, payload),
    onSuccess: () => {
      message.success('个税累计开账草稿已创建')
      setEditor(null)
      void invalidate()
    },
    onError: (error) => message.error(errorMessage(error, '创建个税开账失败')),
  })
  const updateMutation = useMutation({
    mutationFn: ({ openingId, payload }: { openingId: number; payload: TaxOpeningInput }) =>
      updateTaxOpening(employee!.id, openingId, payload),
    onSuccess: () => {
      message.success('个税累计开账草稿已保存')
      setEditor(null)
      void invalidate()
    },
    onError: (error) => message.error(errorMessage(error, '保存个税开账失败')),
  })
  const finalizeMutation = useMutation({
    mutationFn: (openingId: number) => finalizeTaxOpening(employee!.id, openingId),
    onSuccess: () => {
      message.success('个税累计开账已定稿')
      void invalidate()
    },
    onError: (error) => message.error(errorMessage(error, '个税累计开账无法定稿')),
  })
  const supersedeMutation = useMutation({
    mutationFn: ({ openingId, payload }: { openingId: number; payload: TaxOpeningInput }) =>
      supersedeTaxOpening(employee!.id, openingId, payload),
    onSuccess: () => {
      message.success('更正草稿已创建，请复核后定稿')
      setEditor(null)
      void invalidate()
    },
    onError: (error) => message.error(errorMessage(error, '创建更正草稿失败')),
  })

  const saving = createMutation.isPending || updateMutation.isPending || supersedeMutation.isPending

  function close() {
    setEditor(null)
    onClose()
  }

  function submit(values: TaxOpeningInput) {
    if (!editor || !employee) return
    const payload = toOpeningInput(values)
    if (editor.mode === 'create') createMutation.mutate(payload)
    else if (editor.mode === 'edit' && editor.opening) {
      updateMutation.mutate({ openingId: editor.opening.id, payload })
    } else if (editor.mode === 'supersede' && editor.opening) {
      supersedeMutation.mutate({ openingId: editor.opening.id, payload })
    }
  }

  return (
    <>
      <Modal
        title={employee ? `个税累计开账：${employee.emp_no} ${employee.name}` : '个税累计开账'}
        open={open}
        width={1020}
        footer={<Button onClick={close}>关闭</Button>}
        onCancel={close}
        destroyOnClose
      >
        <Alert
          type="warning"
          showIcon
          message="仅录入有凭据的年内累计值"
          description="定稿后不可直接改写；如需更正，请创建新修订草稿。若受影响月份已开始核算，系统会要求先重开相应工资批次。"
          style={{ marginBottom: 16 }}
        />
        <Space style={{ marginBottom: 16 }}>
          <Button type="primary" disabled={!canWrite || !employee} onClick={() => setEditor({ mode: 'create', opening: null })}>
            新建开账草稿
          </Button>
        </Space>
        <Table<TaxOpening>
          rowKey="id"
          loading={openingsQuery.isLoading}
          dataSource={openingsQuery.data ?? []}
          pagination={false}
          columns={[
            { title: '税务年度', dataIndex: 'tax_year' },
            { title: '截至月份', dataIndex: 'through_period' },
            { title: '累计任职月数', dataIndex: 'employment_months_to_date' },
            { title: '累计应税收入', dataIndex: 'taxable_income' },
            { title: '已扣个税', dataIndex: 'tax_withheld' },
            { title: '凭据', dataIndex: 'evidence_ref', ellipsis: true },
            {
              title: '状态',
              render: (_, opening) => {
                if (!opening.is_finalized) return <Tag color="gold">草稿 r{opening.revision}</Tag>
                if (opening.superseded_at) return <Tag>已被更正 r{opening.revision}</Tag>
                return <Tag color="green">已定稿 r{opening.revision}</Tag>
              },
            },
            {
              title: '操作',
              render: (_, opening) => {
                if (!canWrite) return '—'
                if (!opening.is_finalized) {
                  return (
                    <Space>
                      <Button size="small" onClick={() => setEditor({ mode: 'edit', opening })}>
                        编辑
                      </Button>
                      <Popconfirm
                        title="确认定稿该累计开账？定稿后将不可编辑。"
                        onConfirm={() => finalizeMutation.mutate(opening.id)}
                      >
                        <Button size="small" type="primary" loading={finalizeMutation.isPending}>
                          定稿
                        </Button>
                      </Popconfirm>
                    </Space>
                  )
                }
                if (!opening.superseded_at) {
                  return (
                    <Button size="small" onClick={() => setEditor({ mode: 'supersede', opening })}>
                      创建更正草稿
                    </Button>
                  )
                }
                return '—'
              },
            },
          ]}
        />
      </Modal>
      <Modal
        title={
          editor?.mode === 'create'
            ? '新建个税累计开账草稿'
            : editor?.mode === 'supersede'
              ? `更正累计开账 r${editor.opening?.revision ?? ''}`
              : `编辑累计开账 r${editor?.opening?.revision ?? ''}`
        }
        open={!!editor}
        onCancel={() => setEditor(null)}
        onOk={() => form.submit()}
        okText={editor?.mode === 'supersede' ? '创建更正草稿' : '保存草稿'}
        confirmLoading={saving}
        destroyOnClose
      >
        <Form form={form} layout="vertical" onFinish={submit}>
          <Space align="start" wrap style={{ width: '100%' }}>
            <Form.Item name="tax_year" label="税务年度" rules={[{ required: true }]}>
              <Input disabled style={{ width: 140 }} />
            </Form.Item>
            <Form.Item name="through_period" label="截至月份" rules={[{ required: true }]}>
              <Input
                type="month"
                style={{ width: 170 }}
                onChange={(event) => {
                  if (!employee) return
                  const period = event.target.value
                  form.setFieldValue('tax_year', Number(period.slice(0, 4)))
                  form.setFieldValue('employment_months_to_date', employmentMonths(employee, period))
                }}
              />
            </Form.Item>
            <Form.Item
              name="employment_months_to_date"
              label="累计任职月数"
              rules={[{ required: true }]}
            >
              <Input type="number" min={0} max={12} style={{ width: 160 }} />
            </Form.Item>
          </Space>
          <Form.Item name="taxable_income" label="累计应税收入" rules={[{ required: true }]}>
            <Input inputMode="decimal" />
          </Form.Item>
          <Form.Item name="employee_contribution" label="累计个人社保/公积金缴费" rules={[{ required: true }]}>
            <Input inputMode="decimal" />
          </Form.Item>
          <Form.Item name="special_deduction" label="累计专项附加扣除" rules={[{ required: true }]}>
            <Input inputMode="decimal" />
          </Form.Item>
          <Form.Item name="tax_withheld" label="累计已扣个税" rules={[{ required: true }]}>
            <Input inputMode="decimal" />
          </Form.Item>
          <Form.Item name="evidence_ref" label="凭据引用" rules={[{ required: true, whitespace: true }]}>
            <Input placeholder="例如：受控归档路径、编号或工单链接" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
