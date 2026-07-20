import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Button,
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

const EMPLOYMENT_LABELS: Record<Employee['employment_type'], string> = {
  FULL_TIME: '全职',
  PART_TIME_HOURLY: '兼职小时工',
  LABOR: '劳务',
}

export default function EmployeesPage() {
  const { hasPermission } = useAuth()
  const canWrite = hasPermission(Perm.EMPLOYEE_WRITE)
  const qc = useQueryClient()

  const [name, setName] = useState('')
  const [page, setPage] = useState(1)
  const [editing, setEditing] = useState<Employee | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [form] = Form.useForm()

  const orgsQuery = useQuery({ queryKey: ['orgUnits'], queryFn: fetchOrgUnits })
  const orgName = useMemo(() => {
    const map = new Map<number, string>()
    ;(orgsQuery.data ?? []).forEach((o: OrgUnit) => map.set(o.id, o.name))
    return map
  }, [orgsQuery.data])

  const empQuery = useQuery({
    queryKey: ['employees', name, page],
    queryFn: () => fetchEmployees({ name: name || undefined, page, page_size: 20 }),
  })

  const saveMutation = useMutation({
    mutationFn: async (values: Partial<Employee>) => {
      if (editing) return updateEmployee(editing.id, values)
      return createEmployee(values)
    },
    onSuccess: () => {
      message.success('已保存')
      setModalOpen(false)
      setEditing(null)
      void qc.invalidateQueries({ queryKey: ['employees'] })
    },
    onError: (e: unknown) => {
      const status = (e as { response?: { status?: number } }).response?.status
      message.error(status === 409 ? '工号已存在' : status === 404 ? '所属组织不可见' : '保存失败')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: deleteEmployee,
    onSuccess: () => {
      message.success('已删除')
      void qc.invalidateQueries({ queryKey: ['employees'] })
    },
  })

  function openCreate() {
    setEditing(null)
    form.resetFields()
    setModalOpen(true)
  }

  function openEdit(emp: Employee) {
    setEditing(emp)
    form.setFieldsValue(emp)
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
      title: '状态',
      dataIndex: 'status',
      render: (s: string) => <Tag color={s === 'ACTIVE' ? 'green' : 'default'}>{s}</Tag>,
    },
    { title: '身份证', dataIndex: 'id_card' },
    ...(canWrite
      ? [
          {
            title: '操作',
            render: (_: unknown, emp: Employee) => (
              <Space>
                <Button size="small" onClick={() => openEdit(emp)}>
                  编辑
                </Button>
                <Popconfirm title="确认删除？" onConfirm={() => deleteMutation.mutate(emp.id)}>
                  <Button size="small" danger>
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
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Input.Search
          placeholder="按姓名搜索"
          allowClear
          onSearch={(v) => {
            setName(v)
            setPage(1)
          }}
          style={{ width: 240 }}
        />
        {canWrite && (
          <Button type="primary" onClick={openCreate}>
            新增员工
          </Button>
        )}
      </Space>
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
      <Modal
        title={editing ? '编辑员工' : '新增员工'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={saveMutation.isPending}
        destroyOnClose
      >
        <Form form={form} layout="vertical" onFinish={(v) => saveMutation.mutate(v)}>
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
          <Form.Item name="is_special_position" label="特殊岗位(按天数核算)" valuePropName="checked">
            <Select
              options={[
                { value: false, label: '否（按工时折算）' },
                { value: true, label: '是（应出勤−休息天数）' },
              ]}
            />
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
    </div>
  )
}
