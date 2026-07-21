import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  message,
} from 'antd'
import { useState } from 'react'

import {
  createComponent,
  fetchComponents,
  normalizeComponentCreateInput,
  updateComponent,
  type AllowanceKind,
  type ComponentCreateFormInput,
  type ComponentType,
  type SalaryComponent,
} from '../api/comp'
import { useAuth } from '../auth/AuthContext'

const TYPE_LABELS: Record<ComponentType, string> = {
  BASE: '基本',
  COMPREHENSIVE: '综合薪资',
  PERFORMANCE: '绩效',
  POSITION: '岗位',
  ALLOWANCE: '补贴',
  HOUSING: '房补',
  OVERTIME: '加班',
  DEDUCTION: '扣款',
}

const ALLOWANCE_KIND_LABELS: Record<AllowanceKind, string> = {
  FIXED: '固定补贴',
  FLOATING: '浮动补贴（变量）',
}

function errorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    if (typeof detail === 'string') return detail
  }
  return '操作失败，请稍后重试'
}

export default function ComponentsPage() {
  const { user, hasPermission } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const canWrite = hasPermission('salary_structure:write')
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const componentType = Form.useWatch('component_type', form)

  const { data, error, isError, isFetching, isLoading } = useQuery({
    queryKey: ['components', queryScope],
    queryFn: fetchComponents,
  })
  const componentReadUnavailable =
    isLoading || isFetching || isError || data === undefined

  const createMutation = useMutation({
    mutationFn: (values: Parameters<typeof createComponent>[0]) => {
      if (componentReadUnavailable) {
        throw new Error('薪资组件尚未完整读取，已禁止新增')
      }
      return createComponent(values)
    },
    onSuccess: () => {
      message.success('已创建')
      setOpen(false)
      void qc.invalidateQueries({ queryKey: ['components', queryScope] })
    },
    onError: (e: unknown) => {
      const status = (e as { response?: { status?: number } }).response?.status
      message.error(status === 409 ? '组件编码已存在' : '创建失败')
    },
  })

  const updateProrationMutation = useMutation({
    mutationFn: ({ componentId, value }: { componentId: number; value: boolean }) =>
      updateComponent(componentId, { prorate_by_attendance: value }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ['components', queryScope] })
      message.success('已更新按出勤折算设置')
    },
    onError: () => message.error('更新按出勤折算设置失败'),
  })

  const columns = [
    { title: '编码', dataIndex: 'code' },
    { title: '名称', dataIndex: 'name' },
    {
      title: '类型',
      dataIndex: 'component_type',
      render: (t: ComponentType) => <Tag>{TYPE_LABELS[t]}</Tag>,
    },
    {
      title: '补贴方式',
      dataIndex: 'allowance_kind',
      render: (kind: AllowanceKind | null) =>
        kind ? <Tag>{ALLOWANCE_KIND_LABELS[kind]}</Tag> : '—',
    },
    {
      title: '按出勤折算',
      dataIndex: 'prorate_by_attendance',
      render: (value: boolean, component: SalaryComponent) => {
        if (component.component_type !== 'ALLOWANCE') return '—'
        if (!canWrite) return value ? '是' : '否'
        return (
          <Switch
            aria-label={`${component.code} 按出勤折算`}
            checked={value}
            loading={updateProrationMutation.isPending}
            disabled={componentReadUnavailable}
            onChange={(checked) =>
              updateProrationMutation.mutate({ componentId: component.id, value: checked })
            }
          />
        )
      },
    },
    { title: '计税', dataIndex: 'taxable', render: (v: boolean) => (v ? '是' : '否') },
    { title: '计社保基数', dataIndex: 'in_social_base', render: (v: boolean) => (v ? '是' : '否') },
    {
      title: '计公积金基数',
      dataIndex: 'in_housing_base',
      render: (v: boolean) => (v ? '是' : '否'),
    },
  ]

  return (
    <div>
      {canWrite && (
        <Space style={{ marginBottom: 16 }}>
          <Button
            type="primary"
            disabled={componentReadUnavailable}
            onClick={() => {
              if (componentReadUnavailable) return
              form.resetFields()
              setOpen(true)
            }}
          >
            新增组件
          </Button>
        </Space>
      )}
      {isError ? (
        <Alert
          type="error"
          showIcon
          message="薪资组件加载失败"
          description={errorMessage(error)}
        />
      ) : (
        <Table<SalaryComponent>
          rowKey="id"
          loading={isLoading}
          columns={columns}
          dataSource={data ?? []}
          pagination={false}
        />
      )}
      <Modal
        title="新增薪资组件"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={createMutation.isPending}
        okButtonProps={{ disabled: componentReadUnavailable }}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(values: ComponentCreateFormInput) =>
            createMutation.mutate(normalizeComponentCreateInput(values))
          }
        >
          <Form.Item name="code" label="编码" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="component_type" label="类型" rules={[{ required: true }]}>
            <Select
              options={Object.entries(TYPE_LABELS).map(([v, l]) => ({ value: v, label: l }))}
              onChange={(value: ComponentType) => {
                if (value !== 'ALLOWANCE') {
                  form.setFieldValue('allowance_kind', undefined)
                  form.setFieldValue('prorate_by_attendance', false)
                }
              }}
            />
          </Form.Item>
          {componentType === 'ALLOWANCE' && (
            <>
              <Form.Item
                name="allowance_kind"
                label="补贴方式"
                rules={[{ required: true, message: '请选择补贴方式' }]}
                preserve={false}
              >
                <Select
                  options={Object.entries(ALLOWANCE_KIND_LABELS).map(([v, l]) => ({
                    value: v,
                    label: l,
                  }))}
                />
              </Form.Item>
              <Form.Item
                name="prorate_by_attendance"
                valuePropName="checked"
                initialValue={false}
                preserve={false}
              >
                <Checkbox>按实际计薪出勤天数折算</Checkbox>
              </Form.Item>
            </>
          )}
          <Form.Item name="taxable" valuePropName="checked" initialValue={true}>
            <Checkbox>计税</Checkbox>
          </Form.Item>
          <Form.Item name="in_social_base" valuePropName="checked" initialValue={false}>
            <Checkbox>计入社保基数</Checkbox>
          </Form.Item>
          <Form.Item name="in_housing_base" valuePropName="checked" initialValue={false}>
            <Checkbox>计入公积金基数</Checkbox>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
