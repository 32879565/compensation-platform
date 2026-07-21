import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Typography,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useMemo, useState } from 'react'

import {
  createBudget,
  deleteBudget,
  fetchBudgets,
  updateBudget,
  type BudgetWrite,
  type LaborBudget,
} from '../api/budgets'
import { fetchOrgUnits, type OrgUnit } from '../api/masterdata'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'

interface BudgetFormValues {
  org_unit_id: number
  period: string
  headcount_budget: number
  labor_cost_budget: number
  note?: string
}

function currentMonth(): string {
  const now = new Date()
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 7)
}

function monthStart(month: string): string {
  return `${month}-01`
}

function displayMonth(period: string): string {
  return period.slice(0, 7)
}

function errorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response
    if (typeof response?.data?.detail === 'string') return response.data.detail
  }
  return '预算操作失败，请稍后重试。'
}

function amount(value: string): string {
  const numeric = Number(value)
  return Number.isFinite(numeric)
    ? numeric.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : value
}

export default function BudgetPage() {
  const { user, hasPermission } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const canWrite = hasPermission(Perm.BUDGET_WRITE)
  const queryClient = useQueryClient()
  const [month, setMonth] = useState(currentMonth)
  const [orgUnitId, setOrgUnitId] = useState<number | undefined>()
  const [editing, setEditing] = useState<LaborBudget | null>(null)
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm<BudgetFormValues>()

  const orgQuery = useQuery({
    queryKey: ['budgetOrgUnits', queryScope],
    queryFn: fetchOrgUnits,
  })
  const budgetQuery = useQuery({
    queryKey: ['laborBudgets', queryScope, month, orgUnitId],
    queryFn: () =>
      fetchBudgets({
        period: monthStart(month),
        org_unit_id: orgUnitId,
        page_size: 500,
      }),
  })

  const orgById = useMemo(
    () => new Map((orgQuery.data ?? []).map((org) => [org.id, org])),
    [orgQuery.data],
  )
  const stores = useMemo(
    () => (orgQuery.data ?? []).filter((org) => org.type === 'STORE'),
    [orgQuery.data],
  )

  const closeModal = () => {
    form.resetFields()
    setEditing(null)
    setOpen(false)
  }

  const openCreate = () => {
    form.resetFields()
    form.setFieldsValue({ period: month, org_unit_id: orgUnitId })
    setEditing(null)
    setOpen(true)
  }

  const openEdit = (budget: LaborBudget) => {
    form.resetFields()
    form.setFieldsValue({
      org_unit_id: budget.org_unit_id,
      period: displayMonth(budget.period),
      headcount_budget: budget.headcount_budget,
      labor_cost_budget: Number(budget.labor_cost_budget),
      note: budget.note ?? undefined,
    })
    setEditing(budget)
    setOpen(true)
  }

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ['laborBudgets', queryScope] })
  }

  const saveMutation = useMutation({
    mutationFn: (values: BudgetFormValues) => {
      const payload: BudgetWrite = {
        ...values,
        period: monthStart(values.period),
        note: values.note?.trim() || undefined,
      }
      return editing ? updateBudget(editing.id, { ...payload, version: editing.version }) : createBudget(payload)
    },
    onSuccess: async () => {
      message.success(editing ? '预算已更新' : '预算已创建')
      closeModal()
      await invalidate()
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const deleteMutation = useMutation({
    mutationFn: ({ id, version }: { id: number; version: number }) => deleteBudget(id, version),
    onSuccess: async () => {
      message.success('预算已删除')
      await invalidate()
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const columns: TableProps<LaborBudget>['columns'] = [
    {
      title: '组织',
      dataIndex: 'org_unit_id',
      render: (id: number) => orgById.get(id)?.name ?? `组织 #${id}`,
    },
    { title: '周期', dataIndex: 'period', render: displayMonth },
    { title: '编制人数', dataIndex: 'headcount_budget' },
    { title: '人力成本预算', dataIndex: 'labor_cost_budget', render: amount },
    { title: '备注', dataIndex: 'note', render: (value: string | null) => value ?? '—' },
    ...(canWrite
      ? [
          {
            title: '操作',
            key: 'actions',
            render: (_: unknown, budget: LaborBudget) => (
              <Space>
                <Button size="small" onClick={() => openEdit(budget)}>
                  编辑
                </Button>
                <Popconfirm
                  title="确认删除此预算吗？"
                  okText="删除"
                  cancelText="取消"
                  onConfirm={() => deleteMutation.mutate({ id: budget.id, version: budget.version })}
                >
                  <Button danger size="small" loading={deleteMutation.isPending}>
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]
      : []),
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Typography.Title level={3} style={{ margin: 0 }}>
        人力成本预算
      </Typography.Title>
      <Space wrap>
        <label>
          周期
          <Input
            aria-label="预算周期"
            type="month"
            value={month}
            onChange={(event) => setMonth(event.target.value || currentMonth())}
            style={{ width: 150, marginLeft: 8 }}
          />
        </label>
        <Select
          allowClear
          placeholder="全部可见组织"
          value={orgUnitId}
          loading={orgQuery.isLoading}
          style={{ width: 240 }}
          onChange={setOrgUnitId}
          options={stores.map((org: OrgUnit) => ({
            value: org.id,
            label: `${org.code} · ${org.name}`,
          }))}
        />
        {canWrite && (
          <Button type="primary" onClick={openCreate}>
            新增预算
          </Button>
        )}
      </Space>
      {budgetQuery.isError && <Alert type="error" showIcon message={errorMessage(budgetQuery.error)} />}
      <Table<LaborBudget>
        rowKey="id"
        loading={budgetQuery.isLoading || budgetQuery.isFetching}
        columns={columns}
        dataSource={budgetQuery.data?.items ?? []}
        pagination={false}
        locale={{ emptyText: '当前筛选条件下没有预算记录。' }}
      />
      {canWrite && (
        <Modal
          title={editing ? '编辑人力成本预算' : '新增人力成本预算'}
          open={open}
          onCancel={closeModal}
          onOk={() => form.submit()}
          confirmLoading={saveMutation.isPending}
          destroyOnClose
        >
          <Form form={form} layout="vertical" preserve={false} onFinish={saveMutation.mutate}>
            <Form.Item name="org_unit_id" label="组织" rules={[{ required: true, message: '请选择组织' }]}>
              <Select
                showSearch
                optionFilterProp="label"
                options={stores.map((org: OrgUnit) => ({
                  value: org.id,
                  label: `${org.code} · ${org.name}`,
                }))}
              />
            </Form.Item>
            <Form.Item name="period" label="周期" rules={[{ required: true, message: '请选择周期' }]}>
              <Input type="month" />
            </Form.Item>
            <Form.Item
              name="headcount_budget"
              label="编制人数"
              rules={[{ required: true, message: '请输入编制人数' }]}
            >
              <InputNumber min={0} precision={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item
              name="labor_cost_budget"
              label="人力成本预算"
              rules={[{ required: true, message: '请输入人力成本预算' }]}
            >
              <InputNumber min={0} precision={2} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="note" label="备注">
              <Input.TextArea maxLength={500} showCount rows={3} />
            </Form.Item>
          </Form>
        </Modal>
      )}
    </Space>
  )
}
