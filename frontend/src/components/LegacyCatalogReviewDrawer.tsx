import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { useEffect, useRef, useState, type KeyboardEvent } from 'react'

import {
  applyLegacyComponent,
  applyLegacyGrade,
  fetchLegacyCatalogPreview,
  type LegacyComponentCandidate,
  type LegacyComponentDefinition,
  type LegacyGradeCandidate,
} from '../api/legacyCatalog'

type ReviewMode = 'components' | 'grades'
type ComponentType = LegacyComponentDefinition['component_type']
type AllowanceKind = NonNullable<LegacyComponentDefinition['allowance_kind']>

export interface LegacyCatalogReviewDrawerProps {
  open: boolean
  mode: ReviewMode
  onClose: () => void
  onApplied?: () => void
  catalogReadUnavailable?: boolean
}

interface ComponentReviewValues {
  code: string
  name: string
  component_type: ComponentType
  allowance_kind?: AllowanceKind
  taxable: boolean
  in_social_base: boolean
  in_housing_base: boolean
  prorate_by_attendance: boolean
  sort_order: number
  reason: string
  hr_confirmed: boolean
}

interface GradeReviewValues {
  code: string
  name: string
  rank: number
  band_min: string | number
  band_mid: string | number
  band_max: string | number
  effective_from: string
  reason: string
  policy_confirmation: string
}

interface ReviewContext<TCandidate> {
  candidate: TCandidate
  snapshotId: string
}

const COMPONENT_TYPES: ComponentType[] = [
  'BASE',
  'COMPREHENSIVE',
  'PERFORMANCE',
  'POSITION',
  'ALLOWANCE',
  'HOUSING',
  'OVERTIME',
  'DEDUCTION',
]

const COMPONENT_TYPE_LABELS: Record<ComponentType, string> = {
  BASE: '基本工资',
  COMPREHENSIVE: '综合薪资',
  PERFORMANCE: '绩效',
  POSITION: '岗位工资',
  ALLOWANCE: '补贴',
  HOUSING: '房补',
  OVERTIME: '加班',
  DEDUCTION: '扣款',
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

const HORIZONTAL_SCROLL_STEP = 80
const LEGACY_PREVIEW_STALE_TIME_MS = 5 * 60 * 1000

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

function suggestedType(value: string | null): ComponentType | undefined {
  return COMPONENT_TYPES.find((type) => type === value)
}

function periodText(periodFrom: string | null, periodTo: string | null): string {
  if (!periodFrom && !periodTo) return '旧系统未提供期间'
  return `${periodFrom ?? '未知'} 至 ${periodTo ?? '未知'}`
}

function moneyText(value: string | null): string {
  if (value === null) return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return value
  return parsed.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function moneyPayload(value: string | number): string {
  const parsed = Number(value)
  return parsed.toFixed(2)
}

export default function LegacyCatalogReviewDrawer({
  open,
  mode,
  onClose,
  onApplied,
  catalogReadUnavailable = false,
}: LegacyCatalogReviewDrawerProps) {
  const queryClient = useQueryClient()
  const [componentForm] = Form.useForm<ComponentReviewValues>()
  const [gradeForm] = Form.useForm<GradeReviewValues>()
  const componentSubmissionInFlight = useRef(false)
  const gradeSubmissionInFlight = useRef(false)
  const [componentReview, setComponentReview] =
    useState<ReviewContext<LegacyComponentCandidate> | null>(null)
  const [gradeReview, setGradeReview] = useState<ReviewContext<LegacyGradeCandidate> | null>(null)
  const [componentApplyError, setComponentApplyError] = useState<string | null>(null)
  const [gradeApplyError, setGradeApplyError] = useState<string | null>(null)
  const selectedComponent = componentReview?.candidate ?? null
  const selectedGrade = gradeReview?.candidate ?? null

  const previewQuery = useQuery({
    queryKey: ['legacy-catalog-preview'],
    queryFn: fetchLegacyCatalogPreview,
    enabled: open,
    staleTime: LEGACY_PREVIEW_STALE_TIME_MS,
    refetchOnMount: true,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  useEffect(() => {
    if (open) return
    setComponentReview(null)
    setGradeReview(null)
    setComponentApplyError(null)
    setGradeApplyError(null)
  }, [open])

  const componentMutation = useMutation({
    mutationFn: (payload: Parameters<typeof applyLegacyComponent>[0]) =>
      applyLegacyComponent(payload),
    onSuccess: async () => {
      message.success('薪资组件已按 HR 确认结果创建')
      setComponentReview(null)
      setComponentApplyError(null)
      componentForm.resetFields()
      await queryClient.invalidateQueries({ queryKey: ['legacy-catalog-preview'] })
      onApplied?.()
    },
    onError: async (error: unknown) => {
      if (isConflict(error)) {
        setComponentReview(null)
        setComponentApplyError(null)
        componentForm.resetFields()
        message.error(errorMessage(error))
        await queryClient.invalidateQueries({ queryKey: ['legacy-catalog-preview'] })
        return
      }
      setComponentApplyError(errorMessage(error))
    },
    onSettled: () => {
      componentSubmissionInFlight.current = false
    },
  })

  const gradeMutation = useMutation({
    mutationFn: (payload: Parameters<typeof applyLegacyGrade>[0]) => applyLegacyGrade(payload),
    onSuccess: async () => {
      message.success('正式职级与薪档已按 HR 确认结果创建')
      setGradeReview(null)
      setGradeApplyError(null)
      gradeForm.resetFields()
      await queryClient.invalidateQueries({ queryKey: ['legacy-catalog-preview'] })
      onApplied?.()
    },
    onError: async (error: unknown) => {
      if (isConflict(error)) {
        setGradeReview(null)
        setGradeApplyError(null)
        gradeForm.resetFields()
        message.error(errorMessage(error))
        await queryClient.invalidateQueries({ queryKey: ['legacy-catalog-preview'] })
        return
      }
      setGradeApplyError(errorMessage(error))
    },
    onSettled: () => {
      gradeSubmissionInFlight.current = false
    },
  })

  const pending = componentMutation.isPending || gradeMutation.isPending
  const previewUnavailable =
    !open ||
    catalogReadUnavailable ||
    previewQuery.isLoading ||
    previewQuery.isFetching ||
    previewQuery.isError ||
    previewQuery.data === undefined

  const openComponentReview = (candidate: LegacyComponentCandidate) => {
    const snapshotId = previewQuery.data?.source.snapshot_id
    if (!candidate.importable || candidate.applied || previewUnavailable || !snapshotId) return
    setComponentApplyError(null)
    setComponentReview({ candidate, snapshotId })
  }

  const openGradeReview = (candidate: LegacyGradeCandidate) => {
    const snapshotId = previewQuery.data?.source.snapshot_id
    if (
      candidate.suppressed_for_privacy ||
      candidate.applied ||
      previewUnavailable ||
      !snapshotId
    ) {
      return
    }
    setGradeApplyError(null)
    setGradeReview({ candidate, snapshotId })
  }

  const submitComponent = (values: ComponentReviewValues) => {
    if (
      !componentReview ||
      componentReview.candidate.applied ||
      componentSubmissionInFlight.current ||
      componentMutation.isPending ||
      previewUnavailable
    ) {
      return
    }
    const { candidate, snapshotId } = componentReview
    setComponentApplyError(null)
    componentSubmissionInFlight.current = true
    componentMutation.mutate({
      source_field: candidate.source_field,
      expected_record_count: candidate.record_count,
      expected_source_snapshot_id: snapshotId,
      confirmed_by_hr: true,
      reason: values.reason.trim(),
      component: {
        code: values.code.trim(),
        name: values.name.trim(),
        component_type: values.component_type,
        allowance_kind: values.component_type === 'ALLOWANCE' ? values.allowance_kind : undefined,
        taxable: values.taxable,
        in_social_base: values.in_social_base,
        in_housing_base: values.in_housing_base,
        prorate_by_attendance:
          values.component_type === 'ALLOWANCE' ? values.prorate_by_attendance : false,
        sort_order: values.sort_order,
      },
    })
  }

  const submitGrade = (values: GradeReviewValues) => {
    if (
      !gradeReview ||
      gradeReview.candidate.applied ||
      gradeSubmissionInFlight.current ||
      gradeMutation.isPending ||
      previewUnavailable
    ) {
      return
    }
    const { candidate, snapshotId } = gradeReview
    const minimum = Number(values.band_min)
    const midpoint = Number(values.band_mid)
    const maximum = Number(values.band_max)
    if (!(minimum <= midpoint && midpoint <= maximum)) {
      setGradeApplyError('正式薪档必须满足最低值 ≤ 中位值 ≤ 最高值')
      return
    }
    setGradeApplyError(null)
    gradeSubmissionInFlight.current = true
    gradeMutation.mutate({
      source_position: candidate.position,
      expected_record_count: candidate.record_count,
      expected_source_snapshot_id: snapshotId,
      policy_confirmation: 'HR_CONFIRMED',
      reason: values.reason.trim(),
      grade: {
        code: values.code.trim(),
        name: values.name.trim(),
        rank: values.rank,
      },
      band: {
        band_min: moneyPayload(values.band_min),
        band_mid: moneyPayload(values.band_mid),
        band_max: moneyPayload(values.band_max),
        effective_from: values.effective_from,
      },
    })
  }

  const componentColumns = [
    {
      title: '旧系统字段',
      dataIndex: 'source_field',
      render: (value: string, candidate: LegacyComponentCandidate) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{value}</Typography.Text>
          <Typography.Text type="secondary">{candidate.note}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '真实字段计数',
      dataIndex: 'record_count',
      width: 120,
    },
    {
      title: '非零数',
      dataIndex: 'nonzero_count',
      width: 90,
    },
    {
      title: '旧数据期间',
      key: 'period',
      width: 190,
      render: (_: unknown, candidate: LegacyComponentCandidate) =>
        periodText(candidate.period_from, candidate.period_to),
    },
    {
      title: '建议类型',
      dataIndex: 'suggested_component_type',
      width: 135,
      render: (value: string | null) => {
        const type = suggestedType(value)
        return type ? COMPONENT_TYPE_LABELS[type] : '待 HR 判断'
      },
    },
    {
      title: '证据分类',
      dataIndex: 'classification',
      width: 155,
      render: (classification: LegacyComponentCandidate['classification']) =>
        classification === 'DERIVED_NOT_CATALOG_COMPONENT' ? (
          <Tag color="orange">派生结果，仅供核对</Tag>
        ) : (
          <Tag color="blue">需 HR 确认</Tag>
        ),
    },
    {
      title: '操作',
      key: 'action',
      width: 125,
      render: (_: unknown, candidate: LegacyComponentCandidate) =>
        candidate.applied ? (
          <Button type="link" disabled>
            已完成
          </Button>
        ) : candidate.importable ? (
          <Button type="link" onClick={() => openComponentReview(candidate)}>
            审阅并导入
          </Button>
        ) : (
          <Button type="link" disabled>
            不可导入
          </Button>
        ),
    },
  ]

  const gradeColumns = [
    {
      title: '历史职位名称',
      dataIndex: 'position',
      render: (value: string) => <Typography.Text strong>{value}</Typography.Text>,
    },
    {
      title: '记录数',
      dataIndex: 'record_count',
      width: 90,
      render: (value: number, candidate: LegacyGradeCandidate) =>
        candidate.suppressed_for_privacy ? '—' : value,
    },
    {
      title: '独立员工数',
      dataIndex: 'contributor_count',
      width: 110,
      render: (value: number, candidate: LegacyGradeCandidate) =>
        candidate.suppressed_for_privacy ? '—' : value,
    },
    {
      title: '薪资样本数',
      dataIndex: 'salary_sample_count',
      width: 110,
      render: (value: number, candidate: LegacyGradeCandidate) =>
        candidate.suppressed_for_privacy ? '—' : value,
    },
    {
      title: '历史 P25',
      dataIndex: 'observed_p25',
      width: 110,
      render: (value: string | null, candidate: LegacyGradeCandidate) =>
        candidate.suppressed_for_privacy ? '—' : moneyText(value),
    },
    {
      title: '历史中位',
      dataIndex: 'observed_median',
      width: 110,
      render: (value: string | null, candidate: LegacyGradeCandidate) =>
        candidate.suppressed_for_privacy ? '—' : moneyText(value),
    },
    {
      title: '历史 P75',
      dataIndex: 'observed_p75',
      width: 110,
      render: (value: string | null, candidate: LegacyGradeCandidate) =>
        candidate.suppressed_for_privacy ? '—' : moneyText(value),
    },
    {
      title: '证据状态',
      key: 'privacy',
      width: 130,
      render: (_: unknown, candidate: LegacyGradeCandidate) =>
        candidate.suppressed_for_privacy ? (
          <Tag color="red">低于隐私阈值</Tag>
        ) : (
          <Tag color="gold">非官方职级</Tag>
        ),
    },
    {
      title: '操作',
      key: 'action',
      width: 135,
      render: (_: unknown, candidate: LegacyGradeCandidate) =>
        candidate.applied ? (
          <Button type="link" disabled>
            已完成
          </Button>
        ) : candidate.suppressed_for_privacy ? (
          <Button type="link" disabled>
            不可应用
          </Button>
        ) : (
          <Button type="link" onClick={() => openGradeReview(candidate)}>
            制定正式职级
          </Button>
        ),
    },
  ]

  return (
    <>
      <Drawer
        title={`旧系统真实数据审阅 · ${mode === 'components' ? '薪资组件' : '职级体系'}`}
        open={open}
        width={1120}
        destroyOnHidden
        closable={!pending}
        maskClosable={!pending}
        onClose={() => {
          if (pending) return
          setComponentReview(null)
          setGradeReview(null)
          onClose()
        }}
      >
        {catalogReadUnavailable ? (
          <Alert
            type="error"
            showIcon
            message="正式目录当前不可用，已禁止导入"
            description="请先重新读取正式薪资目录，确认现有项目后再继续。"
          />
        ) : previewQuery.isError ? (
          <Alert
            type="error"
            showIcon
            message="历史数据证据加载失败"
            description={errorMessage(previewQuery.error)}
          />
        ) : previewUnavailable ? (
          <Skeleton active paragraph={{ rows: 6 }} />
        ) : (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions bordered size="small" column={2}>
              <Descriptions.Item label="来源规模">
                {previewQuery.data.source.record_count.toLocaleString('zh-CN')} 条来源记录
              </Descriptions.Item>
              <Descriptions.Item label="数据期间">
                {periodText(
                  previewQuery.data.source.period_from,
                  previewQuery.data.source.period_to,
                )}
              </Descriptions.Item>
            </Descriptions>

            <Alert
              type="info"
              showIcon
              message="隐私安全的汇总审阅"
              description="页面仅展示字段与岗位汇总，不展示任何员工个人明细。"
            />

            {previewQuery.data.warnings.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message="旧系统数据提示"
                description={
                  <ul style={{ margin: 0, paddingInlineStart: 20 }}>
                    {previewQuery.data.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                }
              />
            )}

            {mode === 'components' ? (
              <div
                role="region"
                aria-label="旧系统薪资组件候选"
                tabIndex={0}
                style={{ overflowX: 'auto' }}
                onKeyDown={handleHorizontalRegionKeyDown}
              >
                <div style={{ minWidth: 1040 }}>
                  <Table<LegacyComponentCandidate>
                    rowKey="source_field"
                    columns={componentColumns}
                    dataSource={previewQuery.data.component_candidates}
                    pagination={false}
                    locale={{ emptyText: '旧系统中没有可审阅的薪资字段汇总' }}
                  />
                </div>
              </div>
            ) : (
              <>
                <Alert
                  type="error"
                  showIcon
                  message="旧系统没有官方职级主表"
                  description="不能把历史职位名称直接当作职级；必须由 HR 按现行制度创建正式职级。"
                />
                <Alert
                  type="warning"
                  showIcon
                  message="历史薪资分位不是薪档"
                  description="历史薪资分位仅是观察结果，不是公司正式薪档，也不会被自动采用。"
                />
                <div
                  role="region"
                  aria-label="旧系统历史职位候选"
                  tabIndex={0}
                  style={{ overflowX: 'auto' }}
                  onKeyDown={handleHorizontalRegionKeyDown}
                >
                  <div style={{ minWidth: 1130 }}>
                    <Table<LegacyGradeCandidate>
                      rowKey="position"
                      columns={gradeColumns}
                      dataSource={previewQuery.data.grade_candidates}
                      pagination={false}
                      locale={{ emptyText: '旧系统中没有达到展示阈值的岗位汇总' }}
                    />
                  </div>
                </div>
              </>
            )}
          </Space>
        )}
      </Drawer>

      {open && mode === 'components' && selectedComponent !== null && (
        <Modal
          title="确认薪资组件"
          open
          destroyOnHidden
          maskClosable={!componentMutation.isPending}
          closable={!componentMutation.isPending}
          confirmLoading={componentMutation.isPending}
          okText="确认导入"
          cancelButtonProps={{ disabled: componentMutation.isPending }}
          okButtonProps={{ disabled: componentMutation.isPending || previewUnavailable }}
          onCancel={() => {
            if (!componentMutation.isPending) setComponentReview(null)
          }}
          onOk={() => componentForm.submit()}
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={`历史数据证据：${selectedComponent?.source_field ?? ''}`}
            description={
              selectedComponent
                ? `${selectedComponent.record_count} 条记录，${selectedComponent.nonzero_count} 条非零；系统建议仅供参考。`
                : undefined
            }
          />
          {componentApplyError && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              message="导入失败"
              description={componentApplyError}
            />
          )}
          <Form<ComponentReviewValues>
            key={selectedComponent.source_field}
            form={componentForm}
            layout="vertical"
            clearOnDestroy
            initialValues={{
              code: '',
              name: selectedComponent.source_field,
              component_type: suggestedType(selectedComponent.suggested_component_type),
              allowance_kind: undefined,
              taxable: false,
              in_social_base: false,
              in_housing_base: false,
              prorate_by_attendance: false,
              sort_order: 0,
              reason: '',
              hr_confirmed: false,
            }}
            onFinish={submitComponent}
          >
            <Form.Item
              name="code"
              label="组件编码"
              rules={[{ required: true, whitespace: true, message: '请填写 HR 确认的组件编码' }]}
            >
              <Input maxLength={32} autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="name"
              label="组件名称"
              rules={[{ required: true, whitespace: true, message: '请填写 HR 确认的组件名称' }]}
            >
              <Input maxLength={64} />
            </Form.Item>
            <Form.Item
              name="component_type"
              label="组件类型"
              rules={[{ required: true, message: '请确认组件类型' }]}
            >
              <Select
                options={COMPONENT_TYPES.map((value) => ({
                  value,
                  label: COMPONENT_TYPE_LABELS[value],
                }))}
              />
            </Form.Item>
            <Form.Item
              noStyle
              shouldUpdate={(before, after) => before.component_type !== after.component_type}
            >
              {({ getFieldValue }) =>
                getFieldValue('component_type') === 'ALLOWANCE' ? (
                  <Form.Item
                    name="allowance_kind"
                    label="补贴方式"
                    preserve={false}
                    rules={[{ required: true, message: '请由 HR 确认固定或浮动补贴' }]}
                  >
                    <Select
                      options={[
                        { value: 'FIXED', label: '固定补贴' },
                        { value: 'FLOATING', label: '浮动补贴' },
                      ]}
                    />
                  </Form.Item>
                ) : null
              }
            </Form.Item>
            <Typography.Text strong>计薪标志（逐项由 HR 核对）</Typography.Text>
            <Space direction="vertical" size={0} style={{ marginBlock: 8, width: '100%' }}>
              <Form.Item name="taxable" valuePropName="checked" noStyle>
                <Checkbox>计税</Checkbox>
              </Form.Item>
              <Form.Item name="in_social_base" valuePropName="checked" noStyle>
                <Checkbox>计入社保基数</Checkbox>
              </Form.Item>
              <Form.Item name="in_housing_base" valuePropName="checked" noStyle>
                <Checkbox>计入公积金基数</Checkbox>
              </Form.Item>
              <Form.Item
                noStyle
                shouldUpdate={(before, after) => before.component_type !== after.component_type}
              >
                {({ getFieldValue }) =>
                  getFieldValue('component_type') === 'ALLOWANCE' ? (
                    <Form.Item
                      name="prorate_by_attendance"
                      valuePropName="checked"
                      preserve={false}
                      noStyle
                    >
                      <Checkbox>按实际计薪出勤天数折算</Checkbox>
                    </Form.Item>
                  ) : null
                }
              </Form.Item>
            </Space>
            <Form.Item name="sort_order" label="显示排序" rules={[{ required: true }]}>
              <InputNumber precision={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item
              name="reason"
              label="导入依据与原因"
              rules={[{ required: true, whitespace: true, message: '请填写导入依据与原因' }]}
            >
              <Input.TextArea rows={3} maxLength={500} showCount />
            </Form.Item>
            <Form.Item
              name="hr_confirmed"
              valuePropName="checked"
              rules={[
                {
                  validator: (_, value: boolean) =>
                    value
                      ? Promise.resolve()
                      : Promise.reject(new Error('必须由 HR 完成全部字段确认')),
                },
              ]}
            >
              <Checkbox>HR 已核对组件定义、补贴方式及全部计薪标志</Checkbox>
            </Form.Item>
          </Form>
        </Modal>
      )}

      {open && mode === 'grades' && selectedGrade !== null && (
        <Modal
          title="确认正式职级与薪档"
          open
          destroyOnHidden
          maskClosable={!gradeMutation.isPending}
          closable={!gradeMutation.isPending}
          confirmLoading={gradeMutation.isPending}
          okText="确认应用"
          cancelButtonProps={{ disabled: gradeMutation.isPending }}
          okButtonProps={{ disabled: gradeMutation.isPending || previewUnavailable }}
          onCancel={() => {
            if (!gradeMutation.isPending) setGradeReview(null)
          }}
          onOk={() => gradeForm.submit()}
        >
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message={`历史职位证据：${selectedGrade?.position ?? ''}`}
            description="下方正式职级与薪档必须依据现行政策填写，不得直接复制历史分位。"
          />
          {gradeApplyError && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              message="应用失败"
              description={gradeApplyError}
            />
          )}
          <Form<GradeReviewValues>
            key={selectedGrade.position}
            form={gradeForm}
            layout="vertical"
            clearOnDestroy
            initialValues={{
              code: '',
              name: '',
              rank: 0,
              band_min: '',
              band_mid: '',
              band_max: '',
              effective_from: '',
              reason: '',
              policy_confirmation: '',
            }}
            onFinish={submitGrade}
          >
            <Form.Item
              name="code"
              label="职级编码"
              rules={[{ required: true, whitespace: true, message: '请填写正式职级编码' }]}
            >
              <Input maxLength={32} autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="name"
              label="职级名称"
              rules={[{ required: true, whitespace: true, message: '请填写正式职级名称' }]}
            >
              <Input maxLength={64} />
            </Form.Item>
            <Form.Item name="rank" label="级别序号" rules={[{ required: true }]}>
              <InputNumber precision={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item
              name="band_min"
              label="正式薪档最低值"
              rules={[{ required: true, message: '请填写正式薪档最低值' }]}
            >
              <InputNumber min={0} precision={2} stringMode style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item
              name="band_mid"
              label="正式薪档中位值"
              rules={[{ required: true, message: '请填写正式薪档中位值' }]}
            >
              <InputNumber min={0} precision={2} stringMode style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item
              name="band_max"
              label="正式薪档最高值"
              rules={[{ required: true, message: '请填写正式薪档最高值' }]}
            >
              <InputNumber min={0} precision={2} stringMode style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item
              name="effective_from"
              label="薪档生效日期"
              rules={[{ required: true, message: '请填写薪档生效日期' }]}
            >
              <Input type="date" />
            </Form.Item>
            <Form.Item
              name="reason"
              label="制定依据与原因"
              rules={[{ required: true, whitespace: true, message: '请填写制定依据与原因' }]}
            >
              <Input.TextArea rows={3} maxLength={500} showCount />
            </Form.Item>
            <Form.Item
              name="policy_confirmation"
              label="政策确认口令"
              extra="请输入 HR_CONFIRMED，确认该职级与薪档来自 HR 正式政策判断。"
              rules={[
                {
                  validator: (_, value: string) =>
                    value === 'HR_CONFIRMED'
                      ? Promise.resolve()
                      : Promise.reject(new Error('请输入 HR_CONFIRMED 完成政策确认')),
                },
              ]}
            >
              <Input autoComplete="off" />
            </Form.Item>
          </Form>
        </Modal>
      )}
    </>
  )
}
