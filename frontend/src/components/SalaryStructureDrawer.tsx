import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'

import {
  fetchComponents,
  fetchSalaryStructure,
  fetchSalaryStructureHistory,
  setInitialSalaryStructure,
  type InitialSalaryStructureInput,
  type SalaryComponent,
  type SalaryStructureHistoryItem,
  type SalaryStructureItem,
} from '../api/comp'
import type { Employee } from '../api/masterdata'
import { useAuth } from '../auth/AuthContext'

export interface SalaryStructureDrawerProps {
  employee: Employee | null
  open: boolean
  onClose: () => void
}

interface InitialComponentValues {
  amount?: number
  reason?: string
  attachment_url?: string
}

interface InitialStructureValues {
  effective_from: string
  items?: Record<string, InitialComponentValues>
}

const BAND_STATUS: Record<string, { label: string; color: string }> = {
  IN_BAND: { label: '薪档内', color: 'green' },
  OVER: { label: '高于薪档', color: 'orange' },
  UNDER: { label: '低于薪档', color: 'gold' },
  NO_BAND: { label: '未匹配薪档', color: 'default' },
}

function localToday(): string {
  const date = new Date()
  const year = String(date.getFullYear()).padStart(4, '0')
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/** Format a decimal string for display without converting payroll money to a float. */
function moneyText(value: string): string {
  const normalized = value.trim()
  const matched = /^([+-]?)(\d+)(?:\.(\d*))?$/.exec(normalized)
  if (!matched) return normalized
  const [, sign, whole, rawFraction = ''] = matched
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const fraction = rawFraction.padEnd(2, '0').slice(0, 2)
  return `${sign}${grouped}.${fraction}`
}

function errorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    if (typeof detail === 'string') return detail
  }
  if (error instanceof Error && error.message) return error.message
  return '读取薪资结构时发生错误，请刷新后重试'
}

function isInitialStructureConflict(error: unknown): boolean {
  if (typeof error !== 'object' || error === null || !('response' in error)) return false
  const status = (error as { response?: { status?: number } }).response?.status
  return status === 404 || status === 409
}

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

function isSafeEvidenceUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return (
      url.protocol === 'https:' &&
      Boolean(url.hostname) &&
      !url.username &&
      !url.password &&
      !value.includes('\\') &&
      !/\s/.test(value)
    )
  } catch {
    return false
  }
}

function requiresEvidence(component: SalaryComponent): boolean {
  return component.component_type === 'ALLOWANCE' || component.component_type === 'HOUSING'
}

function effectiveRange(from: string, to: string | null): string {
  return `${from} 至 ${to ?? '今'}`
}

export default function SalaryStructureDrawer({
  employee,
  open,
  onClose,
}: SalaryStructureDrawerProps) {
  const { user, hasPermission } = useAuth()
  const queryClient = useQueryClient()
  const [viewDate, setViewDate] = useState(localToday)
  const [initialOpen, setInitialOpen] = useState(false)
  const [selectedComponentIds, setSelectedComponentIds] = useState<Set<number>>(new Set())
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [initialForm] = Form.useForm<InitialStructureValues>()
  const initialSubmissionInFlight = useRef(false)
  const queryScope = user?.username ?? 'anonymous'
  const canWrite = hasPermission('salary_structure:write')
  const employeeId = employee?.id

  useEffect(() => {
    setInitialOpen(false)
    setSelectedComponentIds(new Set())
    setSubmitError(null)
    if (open) setViewDate(localToday())
  }, [employeeId, open])

  useEffect(() => {
    if (!initialOpen) return
    initialForm.resetFields()
    initialForm.setFieldsValue({ effective_from: viewDate, items: {} })
  }, [initialForm, initialOpen, viewDate])

  const componentsQuery = useQuery({
    queryKey: ['salary-structure-components', queryScope, employeeId],
    queryFn: () => fetchComponents({ status: 'all' }),
    enabled: open && employeeId !== undefined,
    refetchOnMount: 'always',
  })
  const currentQuery = useQuery({
    queryKey: ['salary-structure', queryScope, employeeId, viewDate],
    queryFn: () => {
      if (employeeId === undefined) throw new Error('未选择员工')
      return fetchSalaryStructure(employeeId, viewDate)
    },
    enabled: open && employeeId !== undefined,
    refetchOnMount: 'always',
  })
  const historyQuery = useQuery({
    queryKey: ['salary-structure-history', queryScope, employeeId],
    queryFn: () => {
      if (employeeId === undefined) throw new Error('未选择员工')
      return fetchSalaryStructureHistory(employeeId)
    },
    enabled: open && employeeId !== undefined,
    refetchOnMount: 'always',
  })

  const readError = componentsQuery.error ?? currentQuery.error ?? historyQuery.error
  const readLoading =
    componentsQuery.data === undefined ||
    currentQuery.data === undefined ||
    historyQuery.data === undefined
  const readsSettled =
    !readError &&
    !readLoading &&
    !componentsQuery.isFetching &&
    !currentQuery.isFetching &&
    !historyQuery.isFetching
  const hasAnyStructure =
    (currentQuery.data?.items.length ?? 0) > 0 || (historyQuery.data?.length ?? 0) > 0

  const componentById = useMemo(
    () => new Map((componentsQuery.data ?? []).map((component) => [component.id, component])),
    [componentsQuery.data],
  )
  const activeComponents = useMemo(
    () =>
      (componentsQuery.data ?? [])
        .filter((component) => component.is_active)
        .slice()
        .sort(
          (left, right) =>
            left.sort_order - right.sort_order ||
            left.code.localeCompare(right.code) ||
            left.id - right.id,
        ),
    [componentsQuery.data],
  )

  const initialMutation = useMutation({
    mutationFn: (payload: InitialSalaryStructureInput) => {
      if (employeeId === undefined) throw new Error('未选择员工')
      return setInitialSalaryStructure(employeeId, payload)
    },
    onSuccess: async () => {
      setInitialOpen(false)
      setSelectedComponentIds(new Set())
      setSubmitError(null)
      initialForm.resetFields()
      message.success('初始薪资结构已建立')
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ['salary-structure', queryScope, employeeId],
        }),
        queryClient.invalidateQueries({
          queryKey: ['salary-structure-history', queryScope, employeeId],
        }),
      ])
    },
    onError: async (error: unknown) => {
      if (isInitialStructureConflict(error)) {
        setInitialOpen(false)
        setSelectedComponentIds(new Set())
        setSubmitError(null)
        initialForm.resetFields()
        message.error('薪资结构证据已变化，请基于最新数据重新初始化')
        await Promise.all([
          queryClient.invalidateQueries({
            queryKey: ['salary-structure-components', queryScope, employeeId],
          }),
          queryClient.invalidateQueries({
            queryKey: ['salary-structure', queryScope, employeeId],
          }),
          queryClient.invalidateQueries({
            queryKey: ['salary-structure-history', queryScope, employeeId],
          }),
        ])
        return
      }

      setSubmitError(errorMessage(error))
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ['salary-structure', queryScope, employeeId],
        }),
        queryClient.invalidateQueries({
          queryKey: ['salary-structure-history', queryScope, employeeId],
        }),
      ])
    },
    onSettled: () => {
      initialSubmissionInFlight.current = false
    },
  })

  const openInitialForm = () => {
    setSubmitError(null)
    setSelectedComponentIds(new Set())
    setInitialOpen(true)
  }

  const toggleComponent = (componentId: number, checked: boolean) => {
    setSubmitError(null)
    setSelectedComponentIds((previous) => {
      const next = new Set(previous)
      if (checked) next.add(componentId)
      else next.delete(componentId)
      return next
    })
    if (!checked) initialForm.setFieldValue(['items', String(componentId)], undefined)
  }

  const submitInitialStructure = (values: InitialStructureValues) => {
    if (
      initialSubmissionInFlight.current ||
      initialMutation.isPending ||
      employeeId === undefined ||
      !canWrite ||
      !readsSettled ||
      hasAnyStructure
    ) {
      return
    }
    if (selectedComponentIds.size === 0) {
      setSubmitError('至少选择一个薪资组件')
      return
    }

    const items = activeComponents
      .filter((component) => selectedComponentIds.has(component.id))
      .map((component) => {
        const input = values.items?.[String(component.id)]
        const item: InitialSalaryStructureInput['items'][number] = {
          component_id: component.id,
          amount: input?.amount as number,
        }
        if (requiresEvidence(component)) {
          item.reason = input?.reason?.trim()
          item.attachment_url = input?.attachment_url?.trim()
        }
        return item
      })

    setSubmitError(null)
    initialSubmissionInFlight.current = true
    initialMutation.mutate({ effective_from: values.effective_from, items })
  }

  const currentColumns = [
    {
      title: '薪资组件',
      key: 'component',
      render: (_: unknown, item: SalaryStructureItem) => {
        const component = componentById.get(item.component_id)
        return (
          <Space size={8} wrap>
            <Typography.Text strong>
              {component?.name ?? `组件 #${item.component_id}`}
            </Typography.Text>
            {component && !component.is_active && <Tag color="default">已停用</Tag>}
          </Space>
        )
      },
    },
    {
      title: '金额（元）',
      dataIndex: 'amount',
      width: 150,
      align: 'right' as const,
      render: (value: string) => moneyText(value),
    },
    {
      title: '生效区间',
      key: 'effective',
      width: 230,
      render: (_: unknown, item: SalaryStructureItem) =>
        effectiveRange(item.effective_from, item.effective_to),
    },
  ]

  const historyColumns = [
    {
      title: '版本',
      dataIndex: 'revision',
      width: 80,
      render: (value: number) => <Tag>v{value}</Tag>,
    },
    {
      title: '组件',
      key: 'component',
      render: (_: unknown, item: SalaryStructureHistoryItem) => (
        <Space size={8} wrap>
          <Typography.Text>{item.component_name}</Typography.Text>
          {!item.component_is_active && <Tag color="default">组件已停用</Tag>}
        </Space>
      ),
    },
    {
      title: '金额（元）',
      dataIndex: 'amount',
      width: 130,
      align: 'right' as const,
      render: (value: string) => moneyText(value),
    },
    {
      title: '生效区间',
      key: 'effective',
      width: 210,
      render: (_: unknown, item: SalaryStructureHistoryItem) =>
        effectiveRange(item.effective_from, item.effective_to),
    },
    {
      title: '调薪单',
      dataIndex: 'source_adjustment_id',
      width: 100,
      render: (value: number | null) => (value === null ? '初始配置' : `#${value}`),
    },
    {
      title: '变更依据',
      dataIndex: 'source_reason',
      render: (value: string | null) => value ?? '—',
    },
    {
      title: '证明附件',
      dataIndex: 'source_attachment_url',
      width: 110,
      render: (value: string | null) =>
        value && isSafeEvidenceUrl(value) ? (
          <a href={value} target="_blank" rel="noopener noreferrer">
            查看附件
          </a>
        ) : value ? (
          <Typography.Text type="danger">附件地址无效</Typography.Text>
        ) : (
          '—'
        ),
    },
  ]

  const compa = currentQuery.data?.compa
  const bandStatus = compa ? (BAND_STATUS[compa.band_status] ?? BAND_STATUS.NO_BAND) : null

  if (employee === null) {
    return (
      <Drawer title="薪资结构" open={open} width={1040} destroyOnHidden onClose={onClose}>
        <Alert type="info" showIcon message="请选择员工后查看薪资结构" />
      </Drawer>
    )
  }

  return (
    <>
      <Drawer
        title={`${employee.name}（${employee.emp_no}）· 薪资结构`}
        open={open}
        width={1040}
        destroyOnHidden
        closable={!initialMutation.isPending}
        maskClosable={!initialMutation.isPending}
        onClose={() => {
          if (!initialMutation.isPending) onClose()
        }}
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 16,
              flexWrap: 'wrap',
              padding: '12px 16px',
              border: '1px solid #d8e2f0',
              borderRadius: 12,
              background: 'linear-gradient(105deg, #f7faff 0%, #edf4ff 100%)',
            }}
          >
            <div>
              <Typography.Text type="secondary">核算坐标</Typography.Text>
              <br />
              <Typography.Text strong>按指定日期还原当时有效的薪资结构</Typography.Text>
            </div>
            <div>
              <label htmlFor="salary-structure-view-date" style={{ marginRight: 8 }}>
                查看日期
              </label>
              <Input
                id="salary-structure-view-date"
                type="date"
                value={viewDate}
                onChange={(event) => setViewDate(event.target.value)}
                style={{ width: 160 }}
              />
            </div>
          </div>

          {readError ? (
            <Alert
              type="error"
              showIcon
              message="薪资结构加载失败"
              description={errorMessage(readError)}
            />
          ) : readLoading ? (
            <Skeleton active paragraph={{ rows: 7 }} />
          ) : (
            <>
              {compa && bandStatus && (
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: 12,
                    flexWrap: 'wrap',
                    padding: '14px 18px',
                    borderLeft: '4px solid #1677ff',
                    background: '#fafcff',
                  }}
                >
                  <Typography.Text strong style={{ fontSize: 18 }}>
                    合计 {moneyText(compa.total)} 元
                  </Typography.Text>
                  <Space wrap>
                    <Tag color={bandStatus.color}>{bandStatus.label}</Tag>
                    <Typography.Text>Compa {compa.compa_ratio ?? '—'}</Typography.Text>
                    {compa.band_min && compa.band_mid && compa.band_max && (
                      <Typography.Text type="secondary">
                        薪档 {moneyText(compa.band_min)} / {moneyText(compa.band_mid)} /{' '}
                        {moneyText(compa.band_max)}
                      </Typography.Text>
                    )}
                  </Space>
                </div>
              )}

              {!hasAnyStructure ? (
                <Alert
                  type="info"
                  showIcon
                  message="尚未建立薪资结构"
                  description={
                    canWrite
                      ? '可一次选择多个组件建立员工的完整初始结构。'
                      : '当前账号没有薪资结构写入权限。'
                  }
                  action={
                    canWrite && readsSettled ? (
                      <Button type="primary" onClick={openInitialForm}>
                        初始化薪资结构
                      </Button>
                    ) : undefined
                  }
                />
              ) : (
                <Alert
                  type="warning"
                  showIcon
                  message="已有薪资结构不能直接修改，请通过调薪审批变更。"
                  action={
                    <Button type="primary" href="/adjustment">
                      发起调薪审批
                    </Button>
                  }
                />
              )}

              <Typography.Title level={5} style={{ marginBottom: 0 }}>
                当前有效结构
              </Typography.Title>
              <Table<SalaryStructureItem>
                rowKey={(item) => `${item.component_id}-${item.effective_from}`}
                columns={currentColumns}
                dataSource={currentQuery.data?.items ?? []}
                pagination={false}
                size="small"
                locale={{ emptyText: '该日期没有有效的薪资组件' }}
              />

              <Typography.Title level={5} style={{ marginBottom: 0 }}>
                完整变更历史
              </Typography.Title>
              <div
                role="region"
                aria-label="薪资结构变更历史"
                tabIndex={0}
                style={{ overflowX: 'auto' }}
                onKeyDown={handleHorizontalRegionKeyDown}
              >
                <div style={{ minWidth: 900 }}>
                  <Table<SalaryStructureHistoryItem>
                    rowKey="id"
                    columns={historyColumns}
                    dataSource={historyQuery.data ?? []}
                    pagination={false}
                    size="small"
                    locale={{ emptyText: '暂无薪资结构历史' }}
                  />
                </div>
              </div>
            </>
          )}
        </Space>
      </Drawer>

      {open && initialOpen && (
        <Modal
          title="初始化薪资结构"
          open
          width={760}
          destroyOnHidden
          okText="确认初始化"
          confirmLoading={initialMutation.isPending}
          okButtonProps={{ disabled: initialMutation.isPending || !readsSettled }}
          cancelButtonProps={{ disabled: initialMutation.isPending }}
          closable={!initialMutation.isPending}
          maskClosable={!initialMutation.isPending}
          onOk={() => initialForm.submit()}
          onCancel={() => {
            if (!initialMutation.isPending) {
              initialForm.resetFields()
              setSelectedComponentIds(new Set())
              setSubmitError(null)
              setInitialOpen(false)
            }
          }}
        >
          <Alert
            type="warning"
            showIcon
            message="初始结构将作为一个整体写入"
            description="建立后不得在此直接修改，后续增减组件或调整金额必须发起调薪审批。"
            style={{ marginBottom: 16 }}
          />
          {submitError && (
            <Alert
              type="error"
              showIcon
              message="初始化失败"
              description={submitError}
              style={{ marginBottom: 16 }}
            />
          )}
          <Form<InitialStructureValues>
            form={initialForm}
            layout="vertical"
            preserve={false}
            initialValues={{ effective_from: viewDate, items: {} }}
            onFinish={submitInitialStructure}
          >
            <Form.Item
              name="effective_from"
              label="生效日期"
              rules={[{ required: true, message: '请选择生效日期' }]}
            >
              <Input type="date" />
            </Form.Item>

            <Typography.Text strong>选择薪资组件</Typography.Text>
            <Space direction="vertical" size={12} style={{ width: '100%', marginTop: 10 }}>
              {activeComponents.map((component) => {
                const selected = selectedComponentIds.has(component.id)
                const evidenceRequired = requiresEvidence(component)
                return (
                  <div
                    key={component.id}
                    style={{
                      border: '1px solid #e2e8f0',
                      borderRadius: 10,
                      padding: '12px 14px',
                      background: selected ? '#f6f9ff' : '#fff',
                    }}
                  >
                    <Checkbox
                      aria-label={`选择${component.name}`}
                      checked={selected}
                      onChange={(event) => toggleComponent(component.id, event.target.checked)}
                    >
                      {component.name}
                    </Checkbox>
                    <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
                      {component.code}
                    </Typography.Text>

                    {selected && (
                      <div
                        style={{
                          display: 'grid',
                          gridTemplateColumns: evidenceRequired
                            ? 'minmax(150px, 0.8fr) minmax(180px, 1fr) minmax(220px, 1.3fr)'
                            : 'minmax(180px, 280px)',
                          gap: 12,
                          marginTop: 12,
                        }}
                      >
                        <Form.Item
                          name={['items', String(component.id), 'amount']}
                          label={`${component.name}金额`}
                          rules={[{ required: true, message: `${component.name}必须填写金额` }]}
                          style={{ marginBottom: 0 }}
                        >
                          <InputNumber
                            min={0}
                            max={999_999_999_999.99}
                            precision={2}
                            style={{ width: '100%' }}
                          />
                        </Form.Item>

                        {evidenceRequired && (
                          <>
                            <Form.Item
                              name={['items', String(component.id), 'reason']}
                              label={`${component.name}原因`}
                              rules={[
                                {
                                  required: true,
                                  whitespace: true,
                                  message: `${component.name}必须填写原因`,
                                },
                              ]}
                              style={{ marginBottom: 0 }}
                            >
                              <Input maxLength={1000} />
                            </Form.Item>
                            <Form.Item
                              name={['items', String(component.id), 'attachment_url']}
                              label={`${component.name}附件`}
                              rules={[
                                {
                                  validator: (_, value: string | undefined) =>
                                    value && isSafeEvidenceUrl(value.trim())
                                      ? Promise.resolve()
                                      : Promise.reject(
                                          new Error(`${component.name}附件必须为无凭据 HTTPS 地址`),
                                        ),
                                },
                              ]}
                              style={{ marginBottom: 0 }}
                            >
                              <Input placeholder="https://…" maxLength={512} />
                            </Form.Item>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </Space>
          </Form>
        </Modal>
      )}
    </>
  )
}
