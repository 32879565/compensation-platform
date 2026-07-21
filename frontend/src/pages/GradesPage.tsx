import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Drawer,
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
import { useEffect, useRef, useState } from 'react'

import {
  createGrade,
  createSalaryBand,
  deactivateGrade,
  fetchGradeBands,
  fetchGrades,
  restoreGrade,
  updateGrade,
  type GradeCatalogStatus,
  type JobGrade,
  type SalaryBand,
} from '../api/masterdata'
import { useAuth } from '../auth/AuthContext'
import LegacyCatalogEvidencePanel from '../components/LegacyCatalogEvidencePanel'
import LegacyCatalogReviewDrawer from '../components/LegacyCatalogReviewDrawer'

interface GradeFormValues {
  code: string
  name: string
  rank: number
}

interface BandFormValues {
  effective_from: string
  band_min: string
  band_mid: string
  band_max: string
}

type LifecycleAction = 'deactivate' | 'restore'

interface LifecycleState {
  action: LifecycleAction
  grade: JobGrade
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

function moneyText(value: string): string {
  const [whole, fraction = ''] = value.split('.')
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${grouped}.${fraction.padEnd(2, '0').slice(0, 2)}`
}

function moneyPayload(value: string): string {
  const normalized = String(value).trim()
  const [whole, fraction = ''] = normalized.split('.')
  return `${whole}.${fraction.padEnd(2, '0').slice(0, 2)}`
}

function SalaryRail({ band }: { band: SalaryBand }) {
  const points = [
    { label: '最低', value: band.band_min, align: 'flex-start' },
    { label: '中位', value: band.band_mid, align: 'center' },
    { label: '最高', value: band.band_max, align: 'flex-end' },
  ] as const

  return (
    <div
      aria-label="薪档轨道"
      style={{
        border: '1px solid #d9e2f2',
        borderRadius: 12,
        padding: '16px 20px',
        marginBottom: 16,
        background: 'linear-gradient(135deg, #f7faff 0%, #eef5ff 100%)',
      }}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Space wrap>
          <Typography.Text strong>薪档轨道</Typography.Text>
          <Tag color="blue">生效日 {band.effective_from}</Tag>
          {band.effective_to && <Tag>失效日 {band.effective_to}</Tag>}
        </Space>
        <div style={{ position: 'relative', paddingTop: 12 }}>
          <div
            aria-hidden="true"
            style={{
              position: 'absolute',
              left: 8,
              right: 8,
              top: 17,
              height: 4,
              borderRadius: 4,
              background: 'linear-gradient(90deg, #91caff 0%, #1677ff 50%, #0958d9 100%)',
            }}
          />
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
            {points.map((point) => (
              <Space
                key={point.label}
                direction="vertical"
                size={2}
                style={{ alignItems: point.align, zIndex: 1 }}
              >
                <span
                  aria-hidden="true"
                  style={{
                    width: 14,
                    height: 14,
                    borderRadius: '50%',
                    background: '#fff',
                    border: '4px solid #1677ff',
                    boxSizing: 'border-box',
                  }}
                />
                <Typography.Text type="secondary">{point.label}</Typography.Text>
                <Typography.Text strong>{moneyText(point.value)}</Typography.Text>
              </Space>
            ))}
          </div>
        </div>
      </Space>
    </div>
  )
}

export default function GradesPage() {
  const { user, hasPermission } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const canWrite = hasPermission('grade:write')
  const canReviewLegacy = canWrite && hasPermission('import:run')
  const qc = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<GradeCatalogStatus>('active')
  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<JobGrade | null>(null)
  const [selectedGradeId, setSelectedGradeId] = useState<number | null>(null)
  const [bandOpen, setBandOpen] = useState(false)
  const [lifecycle, setLifecycle] = useState<LifecycleState | null>(null)
  const [legacyReviewOpen, setLegacyReviewOpen] = useState(false)
  const [createForm] = Form.useForm<GradeFormValues>()
  const [editForm] = Form.useForm<GradeFormValues>()
  const [bandForm] = Form.useForm<BandFormValues>()
  const [lifecycleForm] = Form.useForm<LifecycleFormValues>()
  const createSubmissionInFlight = useRef(false)
  const editSubmissionInFlight = useRef(false)
  const lifecycleSubmissionInFlight = useRef(false)
  const bandSubmissionInFlight = useRef(false)

  const { data, error, isError, isFetching, isLoading } = useQuery({
    queryKey: ['grades', queryScope, statusFilter],
    queryFn: () => fetchGrades({ status: statusFilter }),
  })
  const gradeReadUnavailable = isLoading || isFetching || isError || data === undefined
  const selectedGrade = data?.find((grade) => grade.id === selectedGradeId) ?? null
  const invalidateGrades = () => qc.invalidateQueries({ queryKey: ['grades', queryScope] })

  const {
    data: bands,
    error: bandError,
    isError: isBandError,
    isFetching: isBandFetching,
    isLoading: isBandLoading,
  } = useQuery({
    queryKey: ['grade-bands', queryScope, selectedGradeId],
    queryFn: () => fetchGradeBands(selectedGradeId!),
    enabled: selectedGradeId !== null,
  })
  const bandReadReady =
    selectedGrade !== null &&
    !gradeReadUnavailable &&
    !isBandLoading &&
    !isBandFetching &&
    !isBandError &&
    bands !== undefined
  const invalidateBands = (gradeId: number | null = selectedGradeId) =>
    qc.invalidateQueries({ queryKey: ['grade-bands', queryScope, gradeId] })

  const createMutation = useMutation({
    mutationFn: (payload: Parameters<typeof createGrade>[0]) => {
      if (gradeReadUnavailable) throw new Error('职级目录尚未完整读取，已禁止新增')
      return createGrade(payload)
    },
    onSuccess: async () => {
      message.success('职级已创建')
      createForm.resetFields()
      setCreateOpen(false)
      await invalidateGrades()
    },
    onError: (mutationError: unknown) => {
      message.error(isConflict(mutationError) ? '职级编码已存在' : errorMessage(mutationError))
    },
    onSettled: () => {
      createSubmissionInFlight.current = false
    },
  })

  const editMutation = useMutation({
    mutationFn: ({ gradeId, values }: { gradeId: number; values: GradeFormValues }) => {
      if (gradeReadUnavailable) throw new Error('职级目录尚未完整读取，已禁止编辑')
      return updateGrade(gradeId, {
        name: values.name.trim(),
        rank: values.rank,
        expected_version: editing!.version,
      })
    },
    onSuccess: async () => {
      message.success('职级已更新')
      editForm.resetFields()
      setEditing(null)
      await invalidateGrades()
    },
    onError: async (mutationError: unknown) => {
      if (isConflict(mutationError)) {
        editForm.resetFields()
        setEditing(null)
        message.error('职级已被其他人修改，已刷新最新数据')
        await invalidateGrades()
        return
      }
      message.error(errorMessage(mutationError))
    },
    onSettled: () => {
      editSubmissionInFlight.current = false
    },
  })

  const lifecycleMutation = useMutation({
    mutationFn: async ({ action, grade, reason }: LifecycleState & { reason: string }) => {
      if (gradeReadUnavailable) throw new Error('职级目录尚未完整读取，已禁止变更状态')
      const payload = { reason: reason.trim(), expected_version: grade.version }
      return action === 'deactivate'
        ? deactivateGrade(grade.id, payload)
        : restoreGrade(grade.id, payload)
    },
    onSuccess: async (_result, variables) => {
      message.success(variables.action === 'deactivate' ? '职级已停用' : '职级已恢复')
      setLifecycle(null)
      lifecycleForm.resetFields()
      await invalidateGrades()
    },
    onError: async (mutationError: unknown) => {
      if (isConflict(mutationError)) {
        setLifecycle(null)
        lifecycleForm.resetFields()
        message.error('职级已被其他人修改，已刷新最新数据')
        await invalidateGrades()
        return
      }
      message.error(errorMessage(mutationError))
    },
    onSettled: () => {
      lifecycleSubmissionInFlight.current = false
    },
  })

  const bandMutation = useMutation({
    mutationFn: ({ gradeId, values }: { gradeId: number; values: BandFormValues }) => {
      if (gradeReadUnavailable) {
        throw new Error('职级目录尚未完整读取，已禁止新增薪档')
      }
      const currentGrade = data?.find((grade) => grade.id === gradeId)
      if (!currentGrade?.is_active) {
        throw new Error('职级已停用或不再可用，请刷新后重试')
      }
      return createSalaryBand(gradeId, {
        effective_from: values.effective_from,
        band_min: moneyPayload(values.band_min),
        band_mid: moneyPayload(values.band_mid),
        band_max: moneyPayload(values.band_max),
      })
    },
    onSuccess: async (_result, variables) => {
      message.success('薪档已新增')
      bandForm.resetFields()
      setBandOpen(false)
      await invalidateBands(variables.gradeId)
    },
    onError: async (mutationError: unknown, variables) => {
      if (isConflict(mutationError)) {
        bandForm.resetFields()
        setBandOpen(false)
        setSelectedGradeId(null)
        message.error(errorMessage(mutationError))
        await Promise.all([invalidateGrades(), invalidateBands(variables.gradeId)])
        return
      }
      message.error(errorMessage(mutationError))
    },
    onSettled: () => {
      bandSubmissionInFlight.current = false
    },
  })

  useEffect(() => {
    if (!editing) return
    editForm.setFieldsValue({ code: editing.code, name: editing.name, rank: editing.rank })
  }, [editForm, editing])

  useEffect(() => {
    if (gradeReadUnavailable || selectedGradeId === null || selectedGrade !== null) return
    bandForm.resetFields()
    setBandOpen(false)
    setSelectedGradeId(null)
  }, [bandForm, gradeReadUnavailable, selectedGrade, selectedGradeId])

  const openEdit = (grade: JobGrade) => {
    editForm.resetFields()
    setEditing(grade)
  }

  const openLifecycle = (grade: JobGrade, action: LifecycleAction) => {
    lifecycleForm.resetFields()
    setLifecycle({ grade, action })
  }

  const columns = [
    { title: '编码', dataIndex: 'code' },
    { title: '名称', dataIndex: 'name' },
    { title: '级别', dataIndex: 'rank' },
    {
      title: '状态',
      dataIndex: 'is_active',
      render: (active: boolean) => (active ? <Tag color="green">启用中</Tag> : <Tag>已停用</Tag>),
    },
    {
      title: '操作',
      key: 'actions',
      render: (_: unknown, grade: JobGrade) => (
        <Space>
          <Button
            type="link"
            disabled={gradeReadUnavailable}
            onClick={() => setSelectedGradeId(grade.id)}
          >
            查看薪档
          </Button>
          {canWrite && (
            <>
              <Button
                type="link"
                disabled={gradeReadUnavailable || !grade.is_active}
                onClick={() => openEdit(grade)}
              >
                编辑
              </Button>
              <Button
                type="link"
                danger={grade.is_active}
                disabled={gradeReadUnavailable}
                onClick={() => openLifecycle(grade, grade.is_active ? 'deactivate' : 'restore')}
              >
                {grade.is_active ? '停用' : '恢复'}
              </Button>
            </>
          )}
        </Space>
      ),
    },
  ]

  const lifecycleLabel = lifecycle?.action === 'deactivate' ? '停用' : '恢复'

  return (
    <Card title="职级体系">
      <Space wrap style={{ marginBottom: 16 }}>
        {canWrite && (!isLoading || isError) && (
          <Button
            type="primary"
            disabled={gradeReadUnavailable}
            onClick={() => {
              if (gradeReadUnavailable) return
              createForm.resetFields()
              setCreateOpen(true)
            }}
          >
            新增职级
          </Button>
        )}
        {canReviewLegacy && (
          <Button disabled={gradeReadUnavailable} onClick={() => setLegacyReviewOpen(true)}>
            审阅旧系统真实数据
          </Button>
        )}
        <Typography.Text>
          <label htmlFor="grade-status-filter">职级状态</label>
        </Typography.Text>
        <Select<GradeCatalogStatus>
          id="grade-status-filter"
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
        <Alert type="error" showIcon message="职级体系加载失败" description={errorMessage(error)} />
      ) : (
        <Table<JobGrade>
          rowKey="id"
          loading={isLoading}
          columns={columns}
          dataSource={data ?? []}
          pagination={false}
          locale={{ emptyText: '当前状态下暂无职级' }}
        />
      )}

      <Modal
        title="新增职级"
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
        okButtonProps={{ disabled: gradeReadUnavailable || createMutation.isPending }}
        cancelButtonProps={{ disabled: createMutation.isPending }}
        closable={!createMutation.isPending}
        maskClosable={!createMutation.isPending}
        keyboard={!createMutation.isPending}
        destroyOnHidden
      >
        <Form<GradeFormValues>
          form={createForm}
          layout="vertical"
          clearOnDestroy
          initialValues={{ rank: 0 }}
          onFinish={(values) => {
            if (
              gradeReadUnavailable ||
              createMutation.isPending ||
              createSubmissionInFlight.current
            ) {
              return
            }
            createSubmissionInFlight.current = true
            createMutation.mutate({
              code: values.code.trim(),
              name: values.name.trim(),
              rank: values.rank,
            })
          }}
        >
          <Form.Item name="code" label="职级编码" rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={32} />
          </Form.Item>
          <Form.Item name="name" label="职级名称" rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item name="rank" label="级别" rules={[{ required: true }]}>
            <InputNumber precision={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="编辑职级"
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
        okButtonProps={{ disabled: gradeReadUnavailable || editMutation.isPending }}
        cancelButtonProps={{ disabled: editMutation.isPending }}
        closable={!editMutation.isPending}
        maskClosable={!editMutation.isPending}
        keyboard={!editMutation.isPending}
        destroyOnHidden
      >
        <Form<GradeFormValues>
          form={editForm}
          layout="vertical"
          clearOnDestroy
          onFinish={(values) => {
            if (
              !editing ||
              gradeReadUnavailable ||
              editMutation.isPending ||
              editSubmissionInFlight.current
            ) {
              return
            }
            editSubmissionInFlight.current = true
            editMutation.mutate({ gradeId: editing.id, values })
          }}
        >
          <Form.Item name="code" label="职级编码">
            <Input disabled />
          </Form.Item>
          <Form.Item name="name" label="职级名称" rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item name="rank" label="级别" rules={[{ required: true }]}>
            <InputNumber precision={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>

      {!bandOpen && (
        <Drawer
          title={selectedGrade ? `${selectedGrade.code} 薪档` : '薪档'}
          open={selectedGradeId !== null && selectedGrade !== null}
          width={680}
          onClose={() => setSelectedGradeId(null)}
          destroyOnHidden
          extra={
            canWrite && !gradeReadUnavailable && selectedGrade?.is_active && bandReadReady ? (
              <Button
                type="primary"
                onClick={() => {
                  setBandOpen(true)
                }}
              >
                新增薪档
              </Button>
            ) : null
          }
        >
          {isBandError ? (
            <Alert
              type="error"
              showIcon
              message="薪档加载失败"
              description={errorMessage(bandError)}
            />
          ) : (
            <>
              {bands?.[0] && <SalaryRail band={bands[0]} />}
              <Table<SalaryBand>
                rowKey="id"
                loading={isBandLoading}
                dataSource={bands ?? []}
                pagination={false}
                locale={{ emptyText: '该职级尚未设置薪档' }}
                columns={[
                  {
                    title: '生效区间',
                    key: 'effective_range',
                    render: (_: unknown, band: SalaryBand) =>
                      `${band.effective_from} 至 ${band.effective_to ?? '长期'}`,
                  },
                  {
                    title: '最低薪资',
                    dataIndex: 'band_min',
                    render: (value: string) => moneyText(value),
                  },
                  {
                    title: '中位薪资',
                    dataIndex: 'band_mid',
                    render: (value: string) => moneyText(value),
                  },
                  {
                    title: '最高薪资',
                    dataIndex: 'band_max',
                    render: (value: string) => moneyText(value),
                  },
                ]}
              />
            </>
          )}
        </Drawer>
      )}

      <Modal
        title="新增薪档"
        open={bandOpen}
        onCancel={() => {
          if (bandMutation.isPending || bandSubmissionInFlight.current) return
          bandForm.resetFields()
          setBandOpen(false)
        }}
        onOk={() => {
          if (bandMutation.isPending || bandSubmissionInFlight.current) return
          bandForm.submit()
        }}
        confirmLoading={bandMutation.isPending}
        closable={!bandMutation.isPending}
        maskClosable={!bandMutation.isPending}
        cancelButtonProps={{ disabled: bandMutation.isPending }}
        okButtonProps={{
          disabled:
            bandMutation.isPending ||
            gradeReadUnavailable ||
            !selectedGrade?.is_active ||
            !bandReadReady,
        }}
        destroyOnHidden
      >
        <Form<BandFormValues>
          form={bandForm}
          layout="vertical"
          clearOnDestroy
          onFinish={(values) => {
            if (
              !selectedGrade ||
              !selectedGrade.is_active ||
              gradeReadUnavailable ||
              !bandReadReady ||
              bandMutation.isPending ||
              bandSubmissionInFlight.current
            ) {
              return
            }
            const minimum = Number(values.band_min)
            const midpoint = Number(values.band_mid)
            const maximum = Number(values.band_max)
            if (!(minimum <= midpoint && midpoint <= maximum)) {
              message.error('薪档金额必须满足最低 ≤ 中位 ≤ 最高')
              return
            }
            bandSubmissionInFlight.current = true
            bandMutation.mutate({ gradeId: selectedGrade.id, values })
          }}
        >
          <Form.Item
            name="effective_from"
            label="生效日期"
            rules={[{ required: true, message: '请选择生效日期' }]}
          >
            <Input type="date" />
          </Form.Item>
          <Form.Item
            name="band_min"
            label="最低薪资"
            rules={[{ required: true, message: '请填写最低薪资' }]}
          >
            <InputNumber<string>
              min="0"
              max="999999999999.99"
              precision={2}
              stringMode
              style={{ width: '100%' }}
            />
          </Form.Item>
          <Form.Item
            name="band_mid"
            label="中位薪资"
            rules={[{ required: true, message: '请填写中位薪资' }]}
          >
            <InputNumber<string>
              min="0"
              max="999999999999.99"
              precision={2}
              stringMode
              style={{ width: '100%' }}
            />
          </Form.Item>
          <Form.Item
            name="band_max"
            label="最高薪资"
            rules={[{ required: true, message: '请填写最高薪资' }]}
          >
            <InputNumber<string>
              min="0"
              max="999999999999.99"
              precision={2}
              stringMode
              style={{ width: '100%' }}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`${lifecycleLabel}职级`}
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
        okButtonProps={{ disabled: gradeReadUnavailable || lifecycleMutation.isPending }}
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
              ? '停用后不可分配给新员工，也不能新增薪档；历史员工和薪档仍保留。'
              : '恢复后可重新用于员工职级分配和薪档维护。'
          }
        />
        <Form<LifecycleFormValues>
          form={lifecycleForm}
          layout="vertical"
          clearOnDestroy
          onFinish={({ reason }) => {
            if (
              !lifecycle ||
              gradeReadUnavailable ||
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
            <Input.TextArea rows={4} maxLength={500} showCount />
          </Form.Item>
        </Form>
      </Modal>

      {canReviewLegacy && (
        <LegacyCatalogEvidencePanel mode="grades" onReview={() => setLegacyReviewOpen(true)} />
      )}
      <LegacyCatalogReviewDrawer
        open={legacyReviewOpen}
        mode="grades"
        onClose={() => setLegacyReviewOpen(false)}
        onApplied={() => {
          setLegacyReviewOpen(false)
          void invalidateGrades()
        }}
      />
    </Card>
  )
}
