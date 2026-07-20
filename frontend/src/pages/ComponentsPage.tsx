import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Checkbox, Form, Input, Modal, Select, Space, Table, Tag, message } from 'antd'
import { useState } from 'react'

import {
  createComponent,
  fetchComponents,
  type ComponentType,
  type SalaryComponent,
} from '../api/comp'
import { useAuth } from '../auth/AuthContext'

const TYPE_LABELS: Record<ComponentType, string> = {
  BASE: '基本',
  PERFORMANCE: '绩效',
  POSITION: '岗位',
  ALLOWANCE: '补贴',
  OVERTIME: '加班',
  DEDUCTION: '扣款',
}

export default function ComponentsPage() {
  const { hasPermission } = useAuth()
  const canWrite = hasPermission('salary_structure:write')
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()

  const { data, isLoading } = useQuery({ queryKey: ['components'], queryFn: fetchComponents })

  const createMutation = useMutation({
    mutationFn: createComponent,
    onSuccess: () => {
      message.success('已创建')
      setOpen(false)
      void qc.invalidateQueries({ queryKey: ['components'] })
    },
    onError: (e: unknown) => {
      const status = (e as { response?: { status?: number } }).response?.status
      message.error(status === 409 ? '组件编码已存在' : '创建失败')
    },
  })

  const columns = [
    { title: '编码', dataIndex: 'code' },
    { title: '名称', dataIndex: 'name' },
    {
      title: '类型',
      dataIndex: 'component_type',
      render: (t: ComponentType) => <Tag>{TYPE_LABELS[t]}</Tag>,
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
            onClick={() => {
              form.resetFields()
              setOpen(true)
            }}
          >
            新增组件
          </Button>
        </Space>
      )}
      <Table<SalaryComponent>
        rowKey="id"
        loading={isLoading}
        columns={columns}
        dataSource={data ?? []}
        pagination={false}
      />
      <Modal
        title="新增薪资组件"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={createMutation.isPending}
        destroyOnClose
      >
        <Form form={form} layout="vertical" onFinish={(v) => createMutation.mutate(v)}>
          <Form.Item name="code" label="编码" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="component_type" label="类型" rules={[{ required: true }]}>
            <Select
              options={Object.entries(TYPE_LABELS).map(([v, l]) => ({ value: v, label: l }))}
            />
          </Form.Item>
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
