import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Form, InputNumber, Modal, Space, Table, message } from 'antd'
import { useState } from 'react'

import { api } from '../api/client'
import { fetchEmployees, type Employee } from '../api/masterdata'
import { useAuth } from '../auth/AuthContext'

interface Attendance {
  employee_id: number
  period: string
  expected_days: string
  actual_days: string
  overtime_hours: string
  leave_days: string
}

function currentPeriod(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

export default function AttendancePage() {
  const { hasPermission } = useAuth()
  const canWrite = hasPermission('attendance:write')
  const qc = useQueryClient()
  const [period, setPeriod] = useState(currentPeriod())
  const [editing, setEditing] = useState<Employee | null>(null)
  const [form] = Form.useForm()

  const empQuery = useQuery({
    queryKey: ['attEmployees'],
    queryFn: () => fetchEmployees({ page_size: 200 }),
  })
  const attQuery = useQuery({
    queryKey: ['attendance', period],
    queryFn: async () =>
      (await api.get<Attendance[]>('/api/attendance', { params: { period } })).data,
  })

  const attByEmp = new Map((attQuery.data ?? []).map((a) => [a.employee_id, a]))

  const saveMutation = useMutation({
    mutationFn: async (v: Record<string, number>) => {
      await api.put(`/api/employees/${editing!.id}/attendance/${period}`, v)
    },
    onSuccess: () => {
      message.success('已保存')
      setEditing(null)
      void qc.invalidateQueries({ queryKey: ['attendance', period] })
    },
    onError: () => message.error('保存失败'),
  })

  function openEdit(emp: Employee) {
    setEditing(emp)
    const a = attByEmp.get(emp.id)
    form.setFieldsValue({
      expected_days: a ? Number(a.expected_days) : 22,
      actual_days: a ? Number(a.actual_days) : 22,
      overtime_hours: a ? Number(a.overtime_hours) : 0,
      leave_days: a ? Number(a.leave_days) : 0,
    })
  }

  const columns = [
    { title: '工号', dataIndex: 'emp_no' },
    { title: '姓名', dataIndex: 'name' },
    {
      title: '应出勤',
      render: (_: unknown, e: Employee) => attByEmp.get(e.id)?.expected_days ?? '—',
    },
    {
      title: '实出勤',
      render: (_: unknown, e: Employee) => attByEmp.get(e.id)?.actual_days ?? '—',
    },
    {
      title: '加班(时)',
      render: (_: unknown, e: Employee) => attByEmp.get(e.id)?.overtime_hours ?? '—',
    },
    ...(canWrite
      ? [
          {
            title: '操作',
            render: (_: unknown, e: Employee) => (
              <Button size="small" onClick={() => openEdit(e)}>
                录入
              </Button>
            ),
          },
        ]
      : []),
  ]

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <span>计薪周期：</span>
        <input
          value={period}
          onChange={(e) => setPeriod(e.target.value)}
          placeholder="YYYY-MM"
          style={{ padding: 4 }}
        />
      </Space>
      <Table
        rowKey="id"
        loading={empQuery.isLoading || attQuery.isLoading}
        columns={columns}
        dataSource={empQuery.data?.items ?? []}
        pagination={{ pageSize: 20 }}
      />
      <Modal
        title={`录入考勤 · ${editing?.name} · ${period}`}
        open={!!editing}
        onCancel={() => setEditing(null)}
        onOk={() => form.submit()}
        confirmLoading={saveMutation.isPending}
        destroyOnClose
      >
        <Form form={form} layout="vertical" onFinish={(v) => saveMutation.mutate(v)}>
          <Form.Item name="expected_days" label="应出勤天数" rules={[{ required: true }]}>
            <InputNumber min={0} max={31} style={{ width: '100%' }} />
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
        </Form>
      </Modal>
    </div>
  )
}
