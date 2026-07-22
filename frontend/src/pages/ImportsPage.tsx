import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Descriptions, Modal, Space, Table, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  confirmSalaryImport,
  fetchSalaryImportPublishTargets,
  fetchSalaryImportRows,
  publishSalaryImport,
  uploadSalaryImport,
  type SalaryImportBatchSummary,
  type SalaryImportConfirmResult,
  type SalaryImportPublishResult,
  type SalaryImportPublishTarget,
  type SalaryImportStagingRow,
} from '../api/imports'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'

const SALARY_IMPORT_ACCEPT = [
  '.xlsx',
  '.xlsm',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.ms-excel.sheet.macroEnabled.12',
].join(',')

function currentPeriod(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

function importApiError(error: unknown): { status?: number; message: string } {
  if (typeof error === 'object' && error !== null) {
    const response = (error as { response?: unknown }).response
    if (typeof response === 'object' && response !== null) {
      const { status, data } = response as { status?: unknown; data?: unknown }
      if (typeof data === 'object' && data !== null) {
        const detail = (data as { detail?: unknown }).detail
        if (typeof detail === 'string') {
          return { status: typeof status === 'number' ? status : undefined, message: detail }
        }
      }
    }
  }
  return {
    message: error instanceof Error && error.message ? error.message : '操作失败，请稍后重试',
  }
}

function importErrorMessage(error: unknown): string {
  return importApiError(error).message
}

function errorEntryText(entry: unknown): string {
  if (typeof entry === 'string') return entry
  try {
    return JSON.stringify(entry) ?? String(entry)
  } catch {
    return String(entry)
  }
}

function fieldValueText(value: unknown): string {
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  if (value === null || value === undefined) return '—'
  try {
    return JSON.stringify(value) ?? String(value)
  } catch {
    return String(value)
  }
}

function departmentLabel(department: SalaryImportPublishTarget['departments'][number]): string {
  return department === 'DINING' ? '厅面' : '厨房'
}

export default function ImportsPage() {
  const { user, hasPermission, hasGlobalPermission } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const canPublish = hasGlobalPermission(Perm.IMPORT_RUN) && hasGlobalPermission(Perm.PAYROLL_RUN)
  const canReadPayroll = hasPermission(Perm.PAYROLL_READ)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const uploadInFlightRef = useRef(false)
  const confirmInFlightRef = useRef(false)
  const publishInFlightRef = useRef(false)
  const [period, setPeriod] = useState(currentPeriod)
  const [file, setFile] = useState<File | null>(null)
  const [batch, setBatch] = useState<SalaryImportBatchSummary | null>(null)
  const [confirmation, setConfirmation] = useState<SalaryImportConfirmResult | null>(null)
  const [confirmationRecovered, setConfirmationRecovered] = useState(false)
  const [publishResult, setPublishResult] = useState<SalaryImportPublishResult | null>(null)
  const [selectedStoreIds, setSelectedStoreIds] = useState<number[]>([])
  const [pendingPublishStoreIds, setPendingPublishStoreIds] = useState<number[]>([])
  const [lockedPublishStoreIds, setLockedPublishStoreIds] = useState<number[] | null>(null)
  const [publishConfirmOpen, setPublishConfirmOpen] = useState(false)
  const [feedback, setFeedback] = useState<string | null>(null)

  const rowsQuery = useQuery({
    queryKey: ['salaryImportRows', queryScope, batch?.id],
    queryFn: () => fetchSalaryImportRows(batch!.id),
    enabled: batch !== null,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const rowCountMismatch =
    batch !== null && rowsQuery.data !== undefined && rowsQuery.data.length !== batch.total_rows
  const rowsReadUnavailable =
    batch !== null &&
    (rowsQuery.isLoading ||
      rowsQuery.isFetching ||
      rowsQuery.isError ||
      rowsQuery.data === undefined ||
      rowCountMismatch ||
      batch.total_rows === 0)

  const publishTargetsQuery = useQuery({
    queryKey: ['salaryImportPublishTargets', queryScope, batch?.id],
    queryFn: () => fetchSalaryImportPublishTargets(batch!.id),
    enabled: batch !== null && confirmation !== null && canPublish && publishResult === null,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const publishTargets = publishTargetsQuery.data ?? []
  const publishSelectionLocked =
    publishTargets.length > 0 && publishTargets.every((target) => target.locked)

  useEffect(() => {
    const targets = publishTargetsQuery.data
    if (!targets?.length || !targets.every((target) => target.locked)) return
    const storeIds = targets.map((target) => target.store_id)
    setSelectedStoreIds(storeIds)
    setLockedPublishStoreIds(storeIds)
  }, [publishTargetsQuery.data])

  function orderedStoreIds(storeIds: number[]): number[] {
    const requested = new Set(storeIds)
    return publishTargets
      .filter((target) => requested.has(target.store_id))
      .map((target) => target.store_id)
  }

  function targetNames(storeIds: number[]): string {
    const requested = new Set(storeIds)
    return publishTargets
      .filter((target) => requested.has(target.store_id))
      .map((target) => target.store_name)
      .join('、')
  }

  const uploadMutation = useMutation({
    mutationFn: ({ uploadPeriod, uploadFile }: { uploadPeriod: string; uploadFile: File }) =>
      uploadSalaryImport(uploadPeriod, uploadFile),
    onMutate: () => {
      setFeedback(null)
      setBatch(null)
      setConfirmation(null)
      setConfirmationRecovered(false)
      setPublishResult(null)
      setSelectedStoreIds([])
      setPendingPublishStoreIds([])
      setLockedPublishStoreIds(null)
      setPublishConfirmOpen(false)
    },
    onSuccess: setBatch,
    onError: (error) => setFeedback(importErrorMessage(error)),
    onSettled: () => {
      uploadInFlightRef.current = false
    },
  })
  const confirmMutation = useMutation({
    mutationFn: async (importBatch: SalaryImportBatchSummary) => {
      if (rowsReadUnavailable || importBatch.error_rows > 0) {
        throw new Error('暂存数据尚未通过校验，不能确认写入')
      }
      return confirmSalaryImport(importBatch.id)
    },
    onMutate: () => setFeedback(null),
    onSuccess: (result) => {
      setConfirmation(result)
      setConfirmationRecovered(false)
      setSelectedStoreIds([])
      setBatch((current) => (current ? { ...current, status: 'CONFIRMED' } : current))
    },
    onError: (error, importBatch) => {
      const parsed = importApiError(error)
      if (parsed.status === 409 && parsed.message === '批次已确认') {
        setConfirmation({ written: importBatch.total_rows })
        setConfirmationRecovered(true)
        setSelectedStoreIds([])
        setBatch((current) => (current ? { ...current, status: 'CONFIRMED' } : current))
        setFeedback(null)
        return
      }
      setFeedback(parsed.message)
    },
    onSettled: () => {
      confirmInFlightRef.current = false
    },
  })
  const publishMutation = useMutation({
    mutationFn: async ({ batchId, storeIds }: { batchId: number; storeIds: number[] }) => {
      if (!canPublish || confirmation === null || storeIds.length === 0) {
        throw new Error('当前批次尚未确认或账号缺少薪资核算权限')
      }
      return publishSalaryImport(batchId, storeIds)
    },
    onMutate: () => setFeedback(null),
    onSuccess: (result, request) => {
      setPublishResult(result)
      setLockedPublishStoreIds([...request.storeIds])
      setPendingPublishStoreIds([])
      setPublishConfirmOpen(false)
    },
    onError: (error) => setFeedback(importErrorMessage(error)),
    onSettled: () => {
      publishInFlightRef.current = false
    },
  })
  const workflowPending =
    uploadMutation.isPending || confirmMutation.isPending || publishMutation.isPending
  const sourceSelectionLocked = workflowPending || confirmation !== null

  function resetResult() {
    setBatch(null)
    setConfirmation(null)
    setConfirmationRecovered(false)
    setPublishResult(null)
    setSelectedStoreIds([])
    setPendingPublishStoreIds([])
    setLockedPublishStoreIds(null)
    setPublishConfirmOpen(false)
    setFeedback(null)
  }

  function selectFile(selected: File | undefined) {
    if (sourceSelectionLocked) return
    if (!selected) return
    if (!/\.(xlsx|xlsm)$/i.test(selected.name)) {
      setFile(null)
      resetResult()
      setFeedback('仅支持 .xlsx/.xlsm 文件')
      return
    }
    resetResult()
    setFile(selected)
  }

  function startNewImport() {
    if (workflowPending) return
    resetResult()
    setFile(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const columns: TableProps<SalaryImportStagingRow>['columns'] = [
    {
      title: '行号',
      dataIndex: 'row_index',
      width: 72,
      render: (rowIndex: number) => rowIndex + 1,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (status: string) => (
        <Tag color={status === 'OK' ? 'green' : 'red'}>{status === 'OK' ? '通过' : '错误'}</Tag>
      ),
    },
    { title: '工号', dataIndex: 'emp_no', width: 120, render: (value) => value ?? '—' },
    { title: '姓名', dataIndex: 'name', width: 120 },
    { title: '门店', dataIndex: 'store_name', width: 150 },
    {
      title: '解析字段',
      dataIndex: 'parsed_fields',
      render: (fields: Record<string, unknown>) => {
        const entries = Object.entries(fields)
        if (!entries.length) return '—'
        return (
          <Space direction="vertical" size={0}>
            {entries.slice(0, 6).map(([key, value]) => (
              <span key={key}>
                <Typography.Text type="secondary">{key}：</Typography.Text>
                <Typography.Text>{fieldValueText(value)}</Typography.Text>
              </span>
            ))}
            {entries.length > 6 ? (
              <Typography.Text type="secondary">另有 {entries.length - 6} 个字段</Typography.Text>
            ) : null}
          </Space>
        )
      },
    },
    {
      title: '校验错误',
      dataIndex: 'errors',
      width: 280,
      render: (errors: unknown[]) =>
        errors.length ? (
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {errors.map((entry, index) => (
              <li key={`${index}-${errorEntryText(entry)}`}>{errorEntryText(entry)}</li>
            ))}
          </ul>
        ) : (
          '—'
        ),
    },
  ]

  const publishTargetColumns: TableProps<SalaryImportPublishTarget>['columns'] = [
    {
      title: '门店',
      dataIndex: 'store_name',
      width: 220,
    },
    {
      title: '员工数',
      dataIndex: 'employee_count',
      width: 120,
      render: (count: number) => `${count} 人`,
    },
    {
      title: '复核部门',
      dataIndex: 'departments',
      width: 220,
      render: (departments: SalaryImportPublishTarget['departments']) =>
        departments.length ? (
          <Space size={[4, 4]} wrap>
            {departments.map((department) => (
              <Tag key={department} color={department === 'DINING' ? 'blue' : 'orange'}>
                {departmentLabel(department)}
              </Tag>
            ))}
          </Space>
        ) : (
          '—'
        ),
    },
  ]

  return (
    <div>
      <Typography.Title level={3}>薪酬导入</Typography.Title>
      <Typography.Paragraph type="secondary">
        按薪资月份上传旧系统或标准模板工资表，先核对暂存数据，再确认写入并推送门店复核。
      </Typography.Paragraph>

      <Card title="上传工资表" style={{ marginBottom: 16 }}>
        <Space wrap align="end">
          <label>
            <span style={{ display: 'block', marginBottom: 4 }}>薪资月份</span>
            <input
              aria-label="薪资月份"
              type="month"
              disabled={sourceSelectionLocked}
              value={period}
              onChange={(event) => {
                setPeriod(event.target.value)
                resetResult()
              }}
              style={{ padding: 5 }}
            />
          </label>
          <input
            ref={fileInputRef}
            aria-label="选择薪酬导入文件"
            type="file"
            accept={SALARY_IMPORT_ACCEPT}
            disabled={sourceSelectionLocked}
            style={{ display: 'none' }}
            onClick={(event) => {
              event.currentTarget.value = ''
            }}
            onChange={(event) => selectFile(event.target.files?.[0])}
          />
          <Button disabled={sourceSelectionLocked} onClick={() => fileInputRef.current?.click()}>
            选择 Excel 文件
          </Button>
          <Typography.Text>{file?.name ?? '尚未选择文件'}</Typography.Text>
          <Button
            type="primary"
            loading={uploadMutation.isPending}
            disabled={!period || file === null || batch !== null || workflowPending}
            onClick={() => {
              if (file && period && batch === null && !uploadInFlightRef.current) {
                uploadInFlightRef.current = true
                uploadMutation.mutate({ uploadPeriod: period, uploadFile: file })
              }
            }}
          >
            上传并校验
          </Button>
          <a href="/payroll-import-template.xlsx" download>
            下载薪资导入模板
          </a>
        </Space>
      </Card>

      {feedback ? (
        <Alert
          type="error"
          showIcon
          closable
          message={feedback}
          style={{ marginBottom: 16 }}
          onClose={() => setFeedback(null)}
        />
      ) : null}

      {batch ? (
        <Card title={`导入批次 #${batch.id}`} style={{ marginBottom: 16 }}>
          <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }}>
            <Descriptions.Item label="文件">{batch.filename}</Descriptions.Item>
            <Descriptions.Item label="薪资月份">{batch.period ?? period}</Descriptions.Item>
            <Descriptions.Item label="总行数">{batch.total_rows}</Descriptions.Item>
            <Descriptions.Item label="错误行">{batch.error_rows}</Descriptions.Item>
          </Descriptions>

          {batch.error_rows > 0 ? (
            <Alert
              type="error"
              showIcon
              message={`存在 ${batch.error_rows} 行错误，请修正文件后重新上传`}
              style={{ margin: '12px 0' }}
            />
          ) : null}
          {rowsQuery.isError ? (
            <Alert
              type="error"
              showIcon
              message="暂存数据读取失败"
              description={importErrorMessage(rowsQuery.error)}
              action={
                <Button
                  size="small"
                  loading={rowsQuery.isFetching}
                  onClick={() => void rowsQuery.refetch()}
                >
                  重新读取
                </Button>
              }
              style={{ margin: '12px 0' }}
            />
          ) : null}
          {rowCountMismatch ? (
            <Alert
              type="error"
              showIcon
              message="暂存数据读取不完整，已禁止确认"
              description={`批次应有 ${batch.total_rows} 行，当前只读取到 ${rowsQuery.data?.length ?? 0} 行。`}
              action={
                <Button
                  size="small"
                  loading={rowsQuery.isFetching}
                  onClick={() => void rowsQuery.refetch()}
                >
                  重新读取
                </Button>
              }
              style={{ margin: '12px 0' }}
            />
          ) : batch.total_rows === 0 ? (
            <Alert
              type="error"
              showIcon
              message="工作簿没有可导入的数据，已禁止确认"
              style={{ margin: '12px 0' }}
            />
          ) : null}

          <div
            role="region"
            aria-label="薪酬导入暂存数据"
            tabIndex={0}
            style={{ overflowX: 'auto', marginTop: 12 }}
          >
            <Table
              rowKey="row_index"
              loading={rowsQuery.isLoading || rowsQuery.isFetching}
              columns={columns}
              dataSource={rowsQuery.data ?? []}
              expandable={{
                columnTitle: '全部字段',
                rowExpandable: (record) => Object.keys(record.parsed_fields).length > 6,
                expandedRowRender: (record) => (
                  <Descriptions size="small" bordered column={{ xs: 1, md: 2, xl: 3 }}>
                    {Object.entries(record.parsed_fields).map(([key, value]) => (
                      <Descriptions.Item key={key} label={key}>
                        {fieldValueText(value)}
                      </Descriptions.Item>
                    ))}
                  </Descriptions>
                ),
              }}
              pagination={{ pageSize: 20, showSizeChanger: false }}
              scroll={{ x: 1100 }}
              size="small"
            />
          </div>

          {confirmation === null ? (
            <Button
              type="primary"
              loading={confirmMutation.isPending}
              disabled={batch.error_rows > 0 || rowsReadUnavailable || confirmMutation.isPending}
              onClick={() => {
                if (confirmInFlightRef.current) return
                confirmInFlightRef.current = true
                confirmMutation.mutate(batch)
              }}
            >
              确认写入薪资记录
            </Button>
          ) : (
            <Alert
              type="success"
              showIcon
              message={
                confirmationRecovered
                  ? `服务端已确认该批次，已恢复 ${confirmation.written} 条记录的后续流程`
                  : `已确认写入 ${confirmation.written} 条薪资记录`
              }
              style={{ marginTop: 16 }}
            />
          )}

          {confirmation && publishResult === null ? (
            canPublish ? (
              <Card size="small" title="选择推送门店" style={{ marginTop: 16 }}>
                {publishTargetsQuery.isError ? (
                  <Alert
                    type="error"
                    showIcon
                    message="可推送门店读取失败，已禁止推送"
                    description={importErrorMessage(publishTargetsQuery.error)}
                    action={
                      <Button
                        size="small"
                        loading={publishTargetsQuery.isFetching}
                        onClick={() => void publishTargetsQuery.refetch()}
                      >
                        重新读取
                      </Button>
                    }
                  />
                ) : publishTargetsQuery.isSuccess && publishTargets.length === 0 ? (
                  <Alert type="warning" showIcon message="该批次没有可推送的门店，已禁止推送" />
                ) : (
                  <>
                    {publishSelectionLocked ? (
                      <Alert
                        type="info"
                        showIcon
                        message="该导入批次的推送门店范围已锁定"
                        description="仅可按下方原范围幂等重试，不能追加或移除门店。"
                        style={{ marginBottom: 12 }}
                      />
                    ) : null}
                    <Space wrap style={{ marginBottom: 12 }}>
                      <Typography.Text>
                        已选择 {selectedStoreIds.length} / {publishTargets.length} 家门店
                      </Typography.Text>
                      <Button
                        size="small"
                        disabled={
                          publishTargetsQuery.isLoading ||
                          publishTargetsQuery.isFetching ||
                          publishTargets.length === 0 ||
                          publishSelectionLocked ||
                          selectedStoreIds.length === publishTargets.length
                        }
                        onClick={() =>
                          setSelectedStoreIds(publishTargets.map((target) => target.store_id))
                        }
                      >
                        全选
                      </Button>
                      <Button
                        size="small"
                        disabled={publishSelectionLocked || selectedStoreIds.length === 0}
                        onClick={() => setSelectedStoreIds([])}
                      >
                        清空
                      </Button>
                    </Space>
                    <div
                      role="region"
                      aria-label="可推送门店"
                      tabIndex={0}
                      style={{ overflowX: 'auto' }}
                    >
                      <Table
                        rowKey="store_id"
                        loading={publishTargetsQuery.isLoading || publishTargetsQuery.isFetching}
                        columns={publishTargetColumns}
                        dataSource={publishTargets}
                        pagination={false}
                        rowSelection={{
                          type: 'checkbox',
                          selectedRowKeys: selectedStoreIds,
                          onChange: (rowKeys) => {
                            if (publishSelectionLocked) return
                            setSelectedStoreIds(
                              orderedStoreIds(rowKeys.map((rowKey) => Number(rowKey))),
                            )
                          },
                          getCheckboxProps: (target) =>
                            ({
                              disabled: publishSelectionLocked,
                              title: `选择门店 ${target.store_name}`,
                              'aria-label': `选择门店 ${target.store_name}`,
                            }) as { disabled: boolean; title: string },
                        }}
                        scroll={{ x: 620 }}
                        size="small"
                      />
                    </div>
                    <Button
                      type="primary"
                      loading={publishMutation.isPending}
                      disabled={
                        publishTargetsQuery.isLoading ||
                        publishTargetsQuery.isFetching ||
                        publishTargetsQuery.isError ||
                        selectedStoreIds.length === 0 ||
                        publishMutation.isPending
                      }
                      style={{ marginTop: 16 }}
                      onClick={() => {
                        const storeIds = orderedStoreIds(selectedStoreIds)
                        if (storeIds.length === 0) return
                        setPendingPublishStoreIds(storeIds)
                        setPublishConfirmOpen(true)
                      }}
                    >
                      {publishSelectionLocked ? '按原范围重试推送' : '推送给店长和厨房经理'}
                    </Button>
                  </>
                )}
              </Card>
            ) : (
              <Alert
                type="info"
                showIcon
                message="需要薪资核算权限才能推送复核任务"
                style={{ marginTop: 16 }}
              />
            )
          ) : null}

          {publishResult ? (
            <>
              <Alert
                type={publishResult.configuration_failures > 0 ? 'warning' : 'success'}
                showIcon
                message={
                  publishResult.configuration_failures > 0
                    ? '复核任务已生成，部分通知配置失败'
                    : publishResult.already_published
                      ? '该导入批次此前已发布，本次未重复创建薪资批次'
                      : publishResult.sandbox
                        ? '复核任务已生成（沙箱模式未实际发送）'
                        : '复核任务已提交发送'
                }
                style={{ marginTop: 16 }}
                description={
                  <Space direction="vertical" size={2}>
                    <span>
                      薪资批次 #{publishResult.payroll_batch_id} · 版本{' '}
                      {publishResult.batch_version} · 员工 {publishResult.employees} 人 · 复核范围{' '}
                      {publishResult.scopes} 个
                    </span>
                    <span>
                      通知 {publishResult.routed} 人 · 配置失败{' '}
                      {publishResult.configuration_failures} 人 · 已存在 {publishResult.existing} 人
                      ·{publishResult.sandbox ? '沙箱模式' : '正式模式'}
                    </span>
                    {lockedPublishStoreIds ? (
                      <span>推送门店：{targetNames(lockedPublishStoreIds)}（范围已锁定）</span>
                    ) : null}
                    {canReadPayroll ? (
                      <Link to="/payroll">查看薪资批次</Link>
                    ) : (
                      <Typography.Text type="secondary">
                        当前账号无薪资批次读取权限，请由核算查看人员继续处理。
                      </Typography.Text>
                    )}
                  </Space>
                }
              />
              {publishResult.configuration_failures > 0 && canPublish ? (
                <Button
                  loading={publishMutation.isPending}
                  disabled={publishMutation.isPending}
                  style={{ marginTop: 12 }}
                  onClick={() => {
                    if (
                      publishInFlightRef.current ||
                      lockedPublishStoreIds === null ||
                      lockedPublishStoreIds.length === 0
                    )
                      return
                    publishInFlightRef.current = true
                    publishMutation.mutate({
                      batchId: batch.id,
                      storeIds: [...lockedPublishStoreIds],
                    })
                  }}
                >
                  修复配置后重试推送
                </Button>
              ) : null}
            </>
          ) : null}
          {confirmation ? (
            <Button
              disabled={workflowPending}
              style={{ marginTop: 16, marginLeft: 8 }}
              onClick={startNewImport}
            >
              开始新导入
            </Button>
          ) : null}
        </Card>
      ) : null}
      <Modal
        title="确认推送薪资复核？"
        open={publishConfirmOpen}
        okText="确认推送"
        cancelText="取消"
        confirmLoading={publishMutation.isPending}
        okButtonProps={{ disabled: pendingPublishStoreIds.length === 0 }}
        cancelButtonProps={{ disabled: publishMutation.isPending }}
        closable={!publishMutation.isPending}
        maskClosable={!publishMutation.isPending}
        onCancel={() => {
          if (publishMutation.isPending) return
          setPublishConfirmOpen(false)
          setPendingPublishStoreIds([])
        }}
        onOk={() => {
          if (batch === null || pendingPublishStoreIds.length === 0 || publishInFlightRef.current)
            return
          const storeIds = [...pendingPublishStoreIds]
          publishInFlightRef.current = true
          setPublishConfirmOpen(false)
          publishMutation.mutate({ batchId: batch.id, storeIds })
        }}
      >
        {publishSelectionLocked ? (
          <Alert
            type="info"
            showIcon
            message="已锁定范围幂等重试"
            description={`将按已锁定的 ${pendingPublishStoreIds.length} 家门店原范围重试，不会重复创建薪资批次。`}
            style={{ marginBottom: 16 }}
          />
        ) : (
          <Alert
            type="warning"
            showIcon
            message="首次推送后门店范围将立即锁定"
            description={`本次已选择 ${pendingPublishStoreIds.length} 家，未选择 ${Math.max(0, publishTargets.length - pendingPublishStoreIds.length)} 家。未选择的门店将不能再使用该导入批次追加推送。`}
            style={{ marginBottom: 16 }}
          />
        )}
        <Typography.Paragraph>将向以下门店的店长和厨房经理推送薪资复核：</Typography.Paragraph>
        <Typography.Text strong>{targetNames(pendingPublishStoreIds)}</Typography.Text>
      </Modal>
    </div>
  )
}
