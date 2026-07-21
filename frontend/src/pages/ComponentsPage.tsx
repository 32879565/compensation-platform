import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import { useEffect, useRef, useState } from 'react'

import {
  createComponent,
  deactivateComponent,
  fetchComponents,
  normalizeComponentCreateInput,
  restoreComponent,
  updateComponent,
  type AllowanceKind,
  type ComponentCatalogStatus,
  type ComponentCreateFormInput,
  type ComponentType,
  type SalaryComponent,
  type UpdateComponentInput,
} from '../api/comp'
import { useAuth } from '../auth/AuthContext'
import LegacyCatalogEvidencePanel from '../components/LegacyCatalogEvidencePanel'
import LegacyCatalogReviewDrawer from '../components/LegacyCatalogReviewDrawer'

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

const CALCULATION_ROLES: Record<ComponentType, { label: string; detail: string; color: string }> = {
  COMPREHENSIVE: {
    label: '出勤计薪基数',
    detail: '作为出勤工资的计薪基数。',
    color: 'blue',
  },
  PERFORMANCE: {
    label: '绩效来源项',
    detail: '由员工当月绩效记录进入工资计算。',
    color: 'purple',
  },
  ALLOWANCE: {
    label: '补贴来源项',
    detail: '按固定或浮动补贴规则进入工资计算，可配置出勤折算。',
    color: 'cyan',
  },
  HOUSING: {
    label: '房补规则项',
    detail: '由房补规则按员工当月在职及出勤情况计算。',
    color: 'geekblue',
  },
  DEDUCTION: {
    label: '扣款来源项',
    detail: '作为工资扣减项目进入核算。',
    color: 'red',
  },
  OVERTIME: {
    label: '考勤派生项',
    detail: '加班金额由考勤记录派生，不直接采用薪资结构金额。',
    color: 'orange',
  },
  BASE: {
    label: '结构参考项',
    detail: '保留在员工薪资结构中供分析使用，不直接作为应发工资行。',
    color: 'default',
  },
  POSITION: {
    label: '结构参考项',
    detail: '保留在员工薪资结构中供分析使用，不直接作为应发工资行。',
    color: 'default',
  },
}

interface ComponentEditValues {
  name: string
  allowance_kind?: AllowanceKind
  reason?: string
  prorate_by_attendance: boolean
  taxable: boolean
  in_social_base: boolean
  in_housing_base: boolean
  sort_order: number
}

type LifecycleAction = 'deactivate' | 'restore'

interface LifecycleState {
  action: LifecycleAction
  component: SalaryComponent
}

interface LifecycleFormValues {
  reason: string
}

function errorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    if (typeof detail === 'string') return detail
  }
  return '操作失败，请稍后重试'
}

function isConflict(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'response' in error &&
    (error as { response?: { status?: number } }).response?.status === 409
  )
}

export default function ComponentsPage() {
  const { user, hasPermission, hasGlobalPermission } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const canWrite = hasPermission('salary_structure:write')
  const canReviewLegacy =
    hasGlobalPermission('salary_structure:write') && hasGlobalPermission('import:run')
  const qc = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<ComponentCatalogStatus>('active')
  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<SalaryComponent | null>(null)
  const [lifecycle, setLifecycle] = useState<LifecycleState | null>(null)
  const [legacyReviewOpen, setLegacyReviewOpen] = useState(false)
  const [createForm] = Form.useForm<ComponentCreateFormInput>()
  const [editForm] = Form.useForm<ComponentEditValues>()
  const [lifecycleForm] = Form.useForm<LifecycleFormValues>()
  const createSubmissionInFlight = useRef(false)
  const editSubmissionInFlight = useRef(false)
  const lifecycleSubmissionInFlight = useRef(false)
  const createComponentType = Form.useWatch('component_type', createForm)

  const { data, error, isError, isFetching, isLoading } = useQuery({
    queryKey: ['components', queryScope, statusFilter],
    queryFn: () => fetchComponents({ status: statusFilter }),
  })
  const componentReadUnavailable = isLoading || isFetching || isError || data === undefined
  const invalidateCatalog = () => qc.invalidateQueries({ queryKey: ['components', queryScope] })

  const createMutation = useMutation({
    mutationFn: (values: Parameters<typeof createComponent>[0]) => {
      if (componentReadUnavailable) {
        throw new Error('薪资组件尚未完整读取，已禁止新增')
      }
      return createComponent(values)
    },
    onSuccess: async () => {
      message.success('已创建薪资组件')
      createForm.resetFields()
      setCreateOpen(false)
      await invalidateCatalog()
    },
    onError: (mutationError: unknown) => {
      message.error(isConflict(mutationError) ? '组件编码已存在' : errorMessage(mutationError))
    },
    onSettled: () => {
      createSubmissionInFlight.current = false
    },
  })

  const editMutation = useMutation({
    mutationFn: ({
      componentId,
      payload,
    }: {
      componentId: number
      payload: UpdateComponentInput
    }) => {
      if (componentReadUnavailable) {
        throw new Error('薪资组件尚未完整读取，已禁止编辑')
      }
      return updateComponent(componentId, payload)
    },
    onSuccess: async () => {
      message.success('薪资组件已更新')
      editForm.resetFields()
      setEditing(null)
      await invalidateCatalog()
    },
    onError: async (mutationError: unknown) => {
      if (isConflict(mutationError)) {
        editForm.resetFields()
        setEditing(null)
        message.error('薪资组件已被其他人修改，已刷新最新数据')
        await invalidateCatalog()
        return
      }
      message.error(errorMessage(mutationError))
    },
    onSettled: () => {
      editSubmissionInFlight.current = false
    },
  })

  const updateProrationMutation = useMutation({
    mutationFn: ({ component, value }: { component: SalaryComponent; value: boolean }) =>
      updateComponent(component.id, {
        prorate_by_attendance: value,
        expected_updated_at: component.updated_at,
      }),
    onSuccess: async () => {
      await invalidateCatalog()
      message.success('已更新按出勤折算设置')
    },
    onError: async (mutationError: unknown) => {
      if (isConflict(mutationError)) {
        message.error('薪资组件已被其他人修改，已刷新最新数据')
        await invalidateCatalog()
        return
      }
      message.error('更新按出勤折算设置失败')
    },
  })

  const lifecycleMutation = useMutation({
    mutationFn: async ({ action, component, reason }: LifecycleState & { reason: string }) => {
      if (componentReadUnavailable) {
        throw new Error('薪资组件尚未完整读取，已禁止变更状态')
      }
      const payload = {
        reason: reason.trim(),
        expected_updated_at: component.updated_at,
      }
      return action === 'deactivate'
        ? deactivateComponent(component.id, payload)
        : restoreComponent(component.id, payload)
    },
    onSuccess: async (_result, variables) => {
      message.success(variables.action === 'deactivate' ? '薪资组件已停用' : '薪资组件已恢复')
      setLifecycle(null)
      lifecycleForm.resetFields()
      await invalidateCatalog()
    },
    onError: async (mutationError: unknown) => {
      if (isConflict(mutationError)) {
        lifecycleForm.resetFields()
        setLifecycle(null)
        message.error('薪资组件已被其他人修改，已刷新最新数据')
        await invalidateCatalog()
        return
      }
      message.error(errorMessage(mutationError))
    },
    onSettled: () => {
      lifecycleSubmissionInFlight.current = false
    },
  })

  useEffect(() => {
    if (!editing) return
    editForm.setFieldsValue({
      name: editing.name,
      allowance_kind: editing.allowance_kind ?? undefined,
      prorate_by_attendance: editing.prorate_by_attendance,
      taxable: editing.taxable,
      in_social_base: editing.in_social_base,
      in_housing_base: editing.in_housing_base,
      sort_order: editing.sort_order,
    })
  }, [editForm, editing])

  const openEdit = (component: SalaryComponent) => {
    editForm.resetFields()
    setEditing(component)
  }

  const openLifecycle = (component: SalaryComponent, action: LifecycleAction) => {
    lifecycleForm.resetFields()
    setLifecycle({ component, action })
  }

  const columns = [
    { title: '编码', dataIndex: 'code' },
    { title: '名称', dataIndex: 'name' },
    {
      title: '类型',
      dataIndex: 'component_type',
      render: (type: ComponentType) => <Tag>{TYPE_LABELS[type]}</Tag>,
    },
    {
      title: '计算角色',
      dataIndex: 'component_type',
      render: (type: ComponentType, component: SalaryComponent) => {
        const role = CALCULATION_ROLES[type]
        return (
          <Space size={4} wrap>
            <Tooltip title={role.detail}>
              <Tag color={role.color}>{role.label}</Tag>
            </Tooltip>
            {component.calculation_locked && <Tag color="gold">计算属性已锁定</Tag>}
          </Space>
        )
      },
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
        if (!canWrite || !component.is_active) return value ? '是' : '否'
        return (
          <Switch
            aria-label={`${component.code} 按出勤折算`}
            checked={value}
            loading={updateProrationMutation.isPending}
            disabled={componentReadUnavailable || component.calculation_locked}
            onChange={(checked) => updateProrationMutation.mutate({ component, value: checked })}
          />
        )
      },
    },
    { title: '计税', dataIndex: 'taxable', render: (value: boolean) => (value ? '是' : '否') },
    {
      title: '计社保基数',
      dataIndex: 'in_social_base',
      render: (value: boolean) => (value ? '是' : '否'),
    },
    {
      title: '计公积金基数',
      dataIndex: 'in_housing_base',
      render: (value: boolean) => (value ? '是' : '否'),
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      render: (active: boolean) => (active ? <Tag color="green">启用中</Tag> : <Tag>已停用</Tag>),
    },
    ...(canWrite
      ? [
          {
            title: '操作',
            key: 'actions',
            render: (_: unknown, component: SalaryComponent) => {
              const needsClassification =
                component.component_type === 'ALLOWANCE' && component.allowance_kind === null
              return (
                <Space>
                  <Button
                    type="link"
                    disabled={
                      componentReadUnavailable || (!component.is_active && !needsClassification)
                    }
                    onClick={() => openEdit(component)}
                  >
                    {needsClassification ? '补齐分类' : '编辑'}
                  </Button>
                  <Button
                    type="link"
                    danger={component.is_active}
                    disabled={componentReadUnavailable || needsClassification}
                    onClick={() =>
                      openLifecycle(component, component.is_active ? 'deactivate' : 'restore')
                    }
                  >
                    {component.is_active ? '停用' : '恢复'}
                  </Button>
                </Space>
              )
            },
          },
        ]
      : []),
  ]

  const lifecycleLabel = lifecycle?.action === 'deactivate' ? '停用' : '恢复'
  const canClassifyLegacyAllowance =
    editing?.component_type === 'ALLOWANCE' && editing.allowance_kind === null

  return (
    <Card title="薪资组件">
      <Space wrap style={{ marginBottom: 16 }}>
        {canWrite && (
          <Button
            type="primary"
            disabled={componentReadUnavailable}
            onClick={() => {
              if (componentReadUnavailable) return
              setCreateOpen(true)
            }}
          >
            新增组件
          </Button>
        )}
        <Typography.Text>
          <label htmlFor="component-status-filter">组件状态</label>
        </Typography.Text>
        <Select<ComponentCatalogStatus>
          id="component-status-filter"
          value={statusFilter}
          style={{ width: 128 }}
          options={[
            { value: 'active', label: '启用中' },
            { value: 'inactive', label: '已停用' },
            { value: 'all', label: '全部' },
          ]}
          onChange={setStatusFilter}
        />
      </Space>

      {isError ? (
        <Alert type="error" showIcon message="薪资组件加载失败" description={errorMessage(error)} />
      ) : (
        <Table<SalaryComponent>
          rowKey="id"
          loading={isLoading}
          columns={columns}
          dataSource={data ?? []}
          pagination={false}
          locale={{ emptyText: '当前状态下暂无薪资组件' }}
        />
      )}

      <Modal
        title="新增薪资组件"
        open={createOpen}
        onCancel={() => {
          if (createMutation.isPending || createSubmissionInFlight.current) return
          createForm.resetFields()
          setCreateOpen(false)
        }}
        onOk={() => {
          if (createMutation.isPending || createSubmissionInFlight.current) return
          createForm.submit()
        }}
        confirmLoading={createMutation.isPending}
        okButtonProps={{ disabled: componentReadUnavailable || createMutation.isPending }}
        cancelButtonProps={{ disabled: createMutation.isPending }}
        closable={!createMutation.isPending}
        maskClosable={!createMutation.isPending}
        keyboard={!createMutation.isPending}
        destroyOnHidden
      >
        <Form
          form={createForm}
          layout="vertical"
          clearOnDestroy
          initialValues={{
            taxable: true,
            in_social_base: false,
            in_housing_base: false,
            prorate_by_attendance: false,
            sort_order: 0,
          }}
          onFinish={(values: ComponentCreateFormInput) => {
            if (
              componentReadUnavailable ||
              createMutation.isPending ||
              createSubmissionInFlight.current
            ) {
              return
            }
            const payload = normalizeComponentCreateInput({
              ...values,
              code: values.code.trim(),
              name: values.name.trim(),
            })
            createSubmissionInFlight.current = true
            createMutation.mutate(payload)
          }}
        >
          <Form.Item name="code" label="编码" rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={32} />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item name="component_type" label="类型" rules={[{ required: true }]}>
            <Select
              options={Object.entries(TYPE_LABELS).map(([value, label]) => ({ value, label }))}
              onChange={(value: ComponentType) => {
                if (value !== 'ALLOWANCE') {
                  createForm.setFieldValue('allowance_kind', undefined)
                  createForm.setFieldValue('prorate_by_attendance', false)
                }
              }}
            />
          </Form.Item>
          {createComponentType === 'ALLOWANCE' && (
            <>
              <Form.Item
                name="allowance_kind"
                label="补贴方式"
                rules={[{ required: true, message: '请选择补贴方式' }]}
                preserve={false}
              >
                <Select
                  options={Object.entries(ALLOWANCE_KIND_LABELS).map(([value, label]) => ({
                    value,
                    label,
                  }))}
                />
              </Form.Item>
              <Form.Item name="prorate_by_attendance" valuePropName="checked" preserve={false}>
                <Checkbox>按实际计薪出勤天数折算</Checkbox>
              </Form.Item>
            </>
          )}
          <Form.Item name="taxable" valuePropName="checked">
            <Checkbox>计税</Checkbox>
          </Form.Item>
          <Form.Item name="in_social_base" valuePropName="checked">
            <Checkbox>计入社保基数</Checkbox>
          </Form.Item>
          <Form.Item name="in_housing_base" valuePropName="checked">
            <Checkbox>计入公积金基数</Checkbox>
          </Form.Item>
          <Form.Item name="sort_order" label="排序">
            <InputNumber precision={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="编辑薪资组件"
        open={editing !== null}
        onCancel={() => {
          if (editMutation.isPending || editSubmissionInFlight.current) return
          editForm.resetFields()
          setEditing(null)
        }}
        onOk={() => {
          if (editMutation.isPending || editSubmissionInFlight.current) return
          editForm.submit()
        }}
        confirmLoading={editMutation.isPending}
        okButtonProps={{ disabled: componentReadUnavailable || editMutation.isPending }}
        cancelButtonProps={{ disabled: editMutation.isPending }}
        closable={!editMutation.isPending}
        maskClosable={!editMutation.isPending}
        keyboard={!editMutation.isPending}
        destroyOnHidden
      >
        <Form<ComponentEditValues>
          form={editForm}
          layout="vertical"
          clearOnDestroy
          onFinish={(values) => {
            if (
              !editing ||
              componentReadUnavailable ||
              editMutation.isPending ||
              editSubmissionInFlight.current
            ) {
              return
            }
            const payload: UpdateComponentInput = {
              name: values.name.trim(),
              sort_order: values.sort_order,
              expected_updated_at: editing.updated_at,
            }
            if (!editing.calculation_locked) {
              payload.taxable = values.taxable
              payload.in_social_base = values.in_social_base
              payload.in_housing_base = values.in_housing_base
              if (editing.component_type === 'ALLOWANCE') {
                payload.allowance_kind = values.allowance_kind
                payload.prorate_by_attendance = values.prorate_by_attendance
              }
              if (canClassifyLegacyAllowance) {
                payload.reason = values.reason?.trim()
              }
            } else if (canClassifyLegacyAllowance) {
              payload.allowance_kind = values.allowance_kind
              payload.reason = values.reason?.trim()
            }
            editSubmissionInFlight.current = true
            editMutation.mutate({ componentId: editing.id, payload })
          }}
        >
          {(editing?.calculation_locked || canClassifyLegacyAllowance) && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message={canClassifyLegacyAllowance ? '历史补贴分类待补齐' : '计算属性已锁定'}
              description={
                canClassifyLegacyAllowance
                  ? '该历史补贴尚未分类，可填写依据后补充固定/浮动分类；其余计算属性仍保持锁定。'
                  : (editing.calculation_lock_reason ??
                    '该组件已用于历史工资，仅允许修改名称和排序。')
              }
            />
          )}
          <Form.Item label="组件编码" htmlFor="component-edit-code">
            <Input id="component-edit-code" value={editing?.code ?? ''} disabled />
          </Form.Item>
          <Form.Item label="组件类型" htmlFor="component-edit-type">
            <Input
              id="component-edit-type"
              value={editing ? TYPE_LABELS[editing.component_type] : ''}
              disabled
            />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={64} />
          </Form.Item>
          {editing?.component_type === 'ALLOWANCE' && (
            <>
              <Form.Item
                name="allowance_kind"
                label="补贴方式"
                rules={[{ required: true, message: '请选择补贴方式' }]}
              >
                <Select
                  disabled={editing.calculation_locked && !canClassifyLegacyAllowance}
                  options={Object.entries(ALLOWANCE_KIND_LABELS).map(([value, label]) => ({
                    value,
                    label,
                  }))}
                />
              </Form.Item>
              {canClassifyLegacyAllowance && (
                <Form.Item
                  name="reason"
                  label="历史补贴分类原因"
                  rules={[{ required: true, whitespace: true, message: '请填写历史补贴分类原因' }]}
                >
                  <Input.TextArea rows={3} maxLength={1000} showCount />
                </Form.Item>
              )}
              <Form.Item name="prorate_by_attendance" valuePropName="checked">
                <Checkbox disabled={editing.calculation_locked}>按实际计薪出勤天数折算</Checkbox>
              </Form.Item>
            </>
          )}
          <Form.Item name="taxable" valuePropName="checked">
            <Checkbox disabled={editing?.calculation_locked}>计税</Checkbox>
          </Form.Item>
          <Form.Item name="in_social_base" valuePropName="checked">
            <Checkbox disabled={editing?.calculation_locked}>计入社保基数</Checkbox>
          </Form.Item>
          <Form.Item name="in_housing_base" valuePropName="checked">
            <Checkbox disabled={editing?.calculation_locked}>计入公积金基数</Checkbox>
          </Form.Item>
          <Form.Item name="sort_order" label="排序" rules={[{ required: true }]}>
            <InputNumber precision={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`${lifecycleLabel}薪资组件`}
        open={lifecycle !== null}
        onCancel={() => {
          if (lifecycleMutation.isPending || lifecycleSubmissionInFlight.current) return
          lifecycleForm.resetFields()
          setLifecycle(null)
        }}
        onOk={() => {
          if (lifecycleMutation.isPending || lifecycleSubmissionInFlight.current) return
          lifecycleForm.submit()
        }}
        confirmLoading={lifecycleMutation.isPending}
        okButtonProps={{ disabled: componentReadUnavailable || lifecycleMutation.isPending }}
        cancelButtonProps={{ disabled: lifecycleMutation.isPending }}
        closable={!lifecycleMutation.isPending}
        maskClosable={!lifecycleMutation.isPending}
        keyboard={!lifecycleMutation.isPending}
        destroyOnHidden
      >
        <Alert
          type={lifecycle?.action === 'deactivate' ? 'warning' : 'info'}
          showIcon
          style={{ marginBottom: 16 }}
          message={
            lifecycle?.action === 'deactivate'
              ? '停用后不可用于新的薪资结构或调薪申请，历史数据仍保留。'
              : '恢复后可重新用于新的薪资配置。'
          }
        />
        <Form<LifecycleFormValues>
          form={lifecycleForm}
          layout="vertical"
          clearOnDestroy
          onFinish={({ reason }) => {
            if (
              !lifecycle ||
              componentReadUnavailable ||
              lifecycleMutation.isPending ||
              lifecycleSubmissionInFlight.current
            ) {
              return
            }
            lifecycleSubmissionInFlight.current = true
            lifecycleMutation.mutate({ ...lifecycle, reason })
          }}
        >
          <Form.Item
            name="reason"
            label={`${lifecycleLabel}原因`}
            rules={[{ required: true, whitespace: true, message: `请填写${lifecycleLabel}原因` }]}
          >
            <Input.TextArea rows={4} maxLength={1000} showCount />
          </Form.Item>
        </Form>
      </Modal>

      {canReviewLegacy && (
        <LegacyCatalogEvidencePanel
          mode="components"
          catalogReadUnavailable={componentReadUnavailable}
          onReview={() => {
            if (!componentReadUnavailable) setLegacyReviewOpen(true)
          }}
        />
      )}
      <LegacyCatalogReviewDrawer
        open={legacyReviewOpen}
        mode="components"
        catalogReadUnavailable={componentReadUnavailable}
        onClose={() => setLegacyReviewOpen(false)}
        onApplied={() => {
          setLegacyReviewOpen(false)
          void invalidateCatalog()
        }}
      />
    </Card>
  )
}
