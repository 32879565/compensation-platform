import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Modal,
  Space,
  Spin,
  Table,
  Tag,
  Tree,
  Typography,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { DataNode } from 'antd/es/tree'
import { useEffect, useRef, useState } from 'react'

import {
  applyDingTalkOrganization,
  fetchLatestDingTalkOrganization,
  previewDingTalkOrganization,
  type DingTalkOrganizationAction,
  type DingTalkOrganizationChangeField,
  type DingTalkOrganizationNodeItem,
  type DingTalkOrganizationNodeKind,
  type DingTalkOrganizationPreview,
  type DingTalkOrganizationReviewerItem,
  type DingTalkOrganizationSyncItemStatus,
} from '../api/dingtalk'
import { fetchOrgTree, type OrgTreeNode } from '../api/masterdata'
import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'

const TYPE_LABEL: Record<OrgTreeNode['type'], string> = {
  GROUP: '集团',
  REGION: '区域',
  STORE: '门店',
}

const DEPARTMENT_LABEL: Record<DingTalkOrganizationReviewerItem['department'], string> = {
  DINING: '厅面',
  KITCHEN: '厨房',
}

const ORGANIZATION_ACTION_COLOR: Record<DingTalkOrganizationAction, string> = {
  LINK: 'blue',
  CREATE: 'green',
  ACTIVATE: 'cyan',
  UPDATE: 'geekblue',
  DEACTIVATE: 'orange',
  ASSIGN_SCOPE: 'blue',
  REMOVE_SCOPE: 'orange',
  NO_CHANGE: 'default',
}

const ORGANIZATION_CHANGE_FIELD_LABEL: Record<DingTalkOrganizationChangeField, string> = {
  name: '名称',
  parent_id: '上级组织',
  dingtalk_dept_id: '钉钉部门 ID',
}

const STATUS_LABEL: Record<DingTalkOrganizationSyncItemStatus, string> = {
  READY: '待应用',
  CONFLICT: '冲突',
  APPLIED: '已应用',
  IGNORED: '已忽略',
}

const STATUS_COLOR: Record<DingTalkOrganizationSyncItemStatus, string> = {
  READY: 'blue',
  CONFLICT: 'red',
  APPLIED: 'green',
  IGNORED: 'default',
}

function organizationActionLabel(
  action: DingTalkOrganizationAction,
  kind?: DingTalkOrganizationNodeKind,
): string {
  switch (action) {
    case 'LINK':
      return kind === 'REGION' ? '关联已有区域' : '关联已有门店'
    case 'CREATE':
      return kind === 'REGION' ? '创建新区域' : '创建新门店'
    case 'ACTIVATE':
      return kind === 'REGION' ? '启用区域' : '启用门店'
    case 'UPDATE':
      return kind === 'REGION' ? '更新区域' : '更新门店'
    case 'DEACTIVATE':
      return kind === 'REGION' ? '停用区域' : '停用门店'
    case 'ASSIGN_SCOPE':
      return '分配负责人权限'
    case 'REMOVE_SCOPE':
      return '撤销负责人权限'
    case 'NO_CHANGE':
      return '无变更'
  }
}

function toTreeData(nodes: OrgTreeNode[]): DataNode[] {
  return nodes.map((node) => ({
    key: node.id,
    title: `${node.name}（${TYPE_LABEL[node.type]}${node.city ? ' · ' + node.city : ''}）`,
    children: node.children.length ? toTreeData(node.children) : undefined,
  }))
}

function errorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    if (typeof detail === 'string') return detail
  }
  return error instanceof Error ? error.message : '钉钉组织同步失败，请稍后重试。'
}

function isHttp404(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'response' in error &&
    (error as { response?: { status?: number } }).response?.status === 404
  )
}

function statusTag(status: DingTalkOrganizationSyncItemStatus) {
  return <Tag color={STATUS_COLOR[status]}>{STATUS_LABEL[status]}</Tag>
}

function remoteDepartmentLabel(name: string, departmentId: number | null): string {
  return departmentId === null ? `${name}（钉钉中未找到）` : `${name}（${departmentId}）`
}

function localTargetLabel(name: string | null, id: number | null): string {
  if (!name) return '—'
  return id === null ? name : `${name}（ID ${id}）`
}

function changeFieldsLabel(changeFields: DingTalkOrganizationChangeField[]): string {
  return changeFields.length === 0
    ? '—'
    : changeFields.map((field) => ORGANIZATION_CHANGE_FIELD_LABEL[field]).join('、')
}

function organizationColumns(
  kind: DingTalkOrganizationNodeKind,
): ColumnsType<DingTalkOrganizationNodeItem> {
  const organizationLabel = kind === 'REGION' ? '区域' : '门店'
  return [
    {
      title: '动作',
      render: (_value, item) => (
        <Tag color={ORGANIZATION_ACTION_COLOR[item.action]}>
          {organizationActionLabel(item.action, kind)}
        </Tag>
      ),
    },
    {
      title: `钉钉${organizationLabel}`,
      render: (_value, item) =>
        remoteDepartmentLabel(item.remote_department_name, item.remote_department_id),
    },
    { title: '钉钉完整路径', dataIndex: 'remote_department_path' },
    { title: '匹配方式', dataIndex: 'match_method' },
    { title: '变更字段', render: (_value, item) => changeFieldsLabel(item.change_fields) },
    {
      title: `本地${organizationLabel}`,
      render: (_value, item) =>
        localTargetLabel(item.proposed_org_unit_name, item.proposed_org_unit_id),
    },
    {
      title: '上级组织',
      render: (_value, item) =>
        localTargetLabel(item.proposed_parent_org_unit_name, item.proposed_parent_org_unit_id),
    },
    { title: '状态', render: (_value, item) => statusTag(item.status) },
    { title: '冲突代码', render: (_value, item) => item.conflict_code ?? '—' },
  ]
}

const reviewerColumns: ColumnsType<DingTalkOrganizationReviewerItem> = [
  {
    title: '动作',
    render: (_value, item) => (
      <Tag color={ORGANIZATION_ACTION_COLOR[item.action]}>
        {organizationActionLabel(item.action)}
      </Tag>
    ),
  },
  {
    title: '钉钉门店',
    render: (_value, item) =>
      remoteDepartmentLabel(item.remote_department_name, item.remote_department_id),
  },
  { title: '部门', render: (_value, item) => DEPARTMENT_LABEL[item.department] },
  { title: '当前负责人', render: (_value, item) => item.current_reviewer_name ?? '—' },
  { title: '钉钉负责人', render: (_value, item) => item.dingtalk_name ?? '—' },
  {
    title: '拟匹配本地员工',
    render: (_value, item) =>
      localTargetLabel(item.proposed_employee_name, item.proposed_employee_id),
  },
  { title: '匹配方式', dataIndex: 'match_method' },
  {
    title: '变更说明',
    render: (_value, item) =>
      item.action === 'REMOVE_SCOPE' ? (
        <Typography.Text type="warning">
          将撤销旧负责人：{item.current_reviewer_name ?? '未知'}
        </Typography.Text>
      ) : (
        '—'
      ),
  },
  { title: '状态', render: (_value, item) => statusTag(item.status) },
  { title: '冲突代码', render: (_value, item) => item.conflict_code ?? '—' },
]

function totalReadyItems(preview: DingTalkOrganizationPreview): number {
  return preview.ready_regions + preview.ready_stores + preview.ready_reviewers
}

function totalConflicts(preview: DingTalkOrganizationPreview): number {
  return preview.region_conflicts + preview.store_conflicts + preview.reviewer_conflicts
}

function isExpired(preview: DingTalkOrganizationPreview, now: number): boolean {
  const expiry = Date.parse(preview.expires_at)
  return !Number.isFinite(expiry) || expiry <= now
}

function hasReadyItem(preview: DingTalkOrganizationPreview): boolean {
  return (
    preview.region_items.some((item) => item.status === 'READY') ||
    preview.store_items.some((item) => item.status === 'READY') ||
    preview.reviewer_items.some((item) => item.status === 'READY')
  )
}

function canApply(preview: DingTalkOrganizationPreview, now: number): boolean {
  return (
    totalReadyItems(preview) > 0 &&
    hasReadyItem(preview) &&
    totalConflicts(preview) === 0 &&
    !isExpired(preview, now)
  )
}

interface OrganizationChangesSectionProps {
  kind: DingTalkOrganizationNodeKind
  items: DingTalkOrganizationNodeItem[]
}

function OrganizationChangesSection({ kind, items }: OrganizationChangesSectionProps) {
  const organizationLabel = kind === 'REGION' ? '区域' : '门店'
  const createCount = items.filter(
    (item) => item.action === 'CREATE' && item.status === 'READY',
  ).length

  return (
    <section aria-label={`${organizationLabel}变更（${items.length}）`}>
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {organizationLabel}变更（{items.length}）
        </Typography.Title>
        {kind === 'STORE' && createCount > 0 ? (
          <Alert
            type="info"
            showIcon
            message="新建门店的城市信息需人事后续配置"
            description={`本批次将创建 ${createCount} 家门店，应用后请在组织架构中补齐城市。`}
          />
        ) : null}
        <Table
          rowKey="id"
          size="small"
          columns={organizationColumns(kind)}
          dataSource={items}
          locale={{ emptyText: `无${organizationLabel}变更` }}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          scroll={{ x: 1200 }}
        />
      </Space>
    </section>
  )
}

interface ReviewerChangesSectionProps {
  title: string
  items: DingTalkOrganizationReviewerItem[]
  removal?: boolean
}

function ReviewerChangesSection({ title, items, removal = false }: ReviewerChangesSectionProps) {
  const accessibleTitle = `${title}（${items.length}）`
  return (
    <section aria-label={accessibleTitle}>
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {accessibleTitle}
        </Typography.Title>
        {removal && items.length > 0 ? (
          <Alert
            type="warning"
            showIcon
            message="以下变更会撤销旧负责人"
            description="应用后，旧负责人将不再拥有该门店对应部门的工资查看权限。"
          />
        ) : null}
        <Table
          rowKey="id"
          size="small"
          columns={reviewerColumns}
          dataSource={items}
          locale={{ emptyText: `无${title}` }}
          pagination={{ pageSize: 5, hideOnSinglePage: true }}
          scroll={{ x: 1500 }}
        />
      </Space>
    </section>
  )
}

export default function OrgTreePage() {
  const { user, hasGlobalPermission } = useAuth()
  const queryClient = useQueryClient()
  const queryScope = user?.username ?? 'anonymous'
  const latestQueryKey = ['dingtalkOrganizationLatest', queryScope] as const
  const canSyncDingTalkOrganization =
    hasGlobalPermission(Perm.DINGTALK_ORG_SYNC) && hasGlobalPermission(Perm.NOTIFICATION_MANAGE)
  const [syncPreview, setSyncPreview] = useState<DingTalkOrganizationPreview | null>(null)
  const [syncError, setSyncError] = useState<string | null>(null)
  const [postApplyWarning, setPostApplyWarning] = useState<string | null>(null)
  const [expiryClock, setExpiryClock] = useState(() => Date.now())
  const submittedBatchRef = useRef<string | null>(null)

  const orgTreeQuery = useQuery({
    queryKey: ['orgTree', queryScope],
    queryFn: fetchOrgTree,
  })
  const latestQuery = useQuery({
    queryKey: latestQueryKey,
    queryFn: async () => {
      try {
        return await fetchLatestDingTalkOrganization()
      } catch (error) {
        if (isHttp404(error)) return null
        throw error
      }
    },
    enabled: canSyncDingTalkOrganization,
    retry: false,
  })

  const previewMutation = useMutation({
    mutationFn: previewDingTalkOrganization,
    retry: false,
    onSuccess: (preview) => {
      setSyncError(null)
      setExpiryClock(Date.now())
      setSyncPreview(preview)
      queryClient.setQueryData(latestQueryKey, preview)
    },
    onError: (error) => setSyncError(errorMessage(error)),
  })
  const applyMutation = useMutation({
    mutationFn: (batchId: string) => applyDingTalkOrganization(batchId),
    retry: false,
    onSuccess: async (result) => {
      setSyncError(null)
      setSyncPreview(null)
      const [, refreshedTree] = await Promise.all([latestQuery.refetch(), orgTreeQuery.refetch()])
      if (refreshedTree.isError) {
        const refreshWarning =
          '应用已成功，但组织架构刷新失败。请刷新页面后核对最新区域、门店和负责人。'
        setPostApplyWarning(refreshWarning)
        message.warning(refreshWarning)
        return
      }
      setPostApplyWarning(null)
      message.success(
        result.already_applied
          ? '该批次此前已应用，组织架构已刷新。'
          : `已同步 ${result.applied_regions} 项区域变更、${result.applied_stores} 项门店变更、${result.applied_reviewers} 项负责人变更；${result.unresolved} 项冲突仍待处理。`,
      )
    },
    onError: (error) => {
      setSyncError(errorMessage(error))
    },
  })

  useEffect(() => {
    if (!syncPreview) return
    const now = Date.now()
    setExpiryClock(now)
    const expiresAt = Date.parse(syncPreview.expires_at)
    if (!Number.isFinite(expiresAt) || expiresAt <= now) return
    const timer = window.setTimeout(
      () => setExpiryClock(Date.now()),
      Math.min(expiresAt - now, 2_147_483_647),
    )
    return () => window.clearTimeout(timer)
  }, [syncPreview])

  function refreshPreview(): void {
    setSyncError(null)
    previewMutation.mutate()
  }

  function viewLatestPreview(): void {
    if (!latestQuery.data) return
    setSyncError(null)
    setExpiryClock(Date.now())
    setSyncPreview(latestQuery.data)
  }

  function closeSyncPreview(): void {
    if (applyMutation.isPending) return
    setSyncError(null)
    setSyncPreview(null)
  }

  function applyPreview(): void {
    const preview = syncPreview
    if (
      !preview ||
      submittedBatchRef.current === preview.batch_id ||
      applyMutation.isPending ||
      !canApply(preview, Date.now())
    ) {
      return
    }
    submittedBatchRef.current = preview.batch_id
    applyMutation.mutate(preview.batch_id)
  }

  if (orgTreeQuery.isLoading) return <Spin />

  const canApplyPreview =
    syncPreview !== null &&
    syncPreview.batch_id !== submittedBatchRef.current &&
    canApply(syncPreview, expiryClock)
  const reviewerAssignments =
    syncPreview?.reviewer_items.filter(
      (item) => item.status !== 'CONFLICT' && item.action === 'ASSIGN_SCOPE',
    ) ?? []
  const reviewerRemovals =
    syncPreview?.reviewer_items.filter(
      (item) => item.status !== 'CONFLICT' && item.action === 'REMOVE_SCOPE',
    ) ?? []
  const reviewerConflicts =
    syncPreview?.reviewer_items.filter((item) => item.status === 'CONFLICT') ?? []

  return (
    <>
      <Card title="组织架构">
        {canSyncDingTalkOrganization ? (
          <section
            role="region"
            aria-label="钉钉组织同步状态"
            style={{ marginBottom: 20, padding: 16, background: '#fafafa', borderRadius: 8 }}
          >
            <Space direction="vertical" size="small" style={{ width: '100%' }}>
              <Typography.Title level={4} style={{ margin: 0 }}>
                组织同步检查单
              </Typography.Title>
              {latestQuery.isLoading ? <Spin size="small" /> : null}
              {latestQuery.isError ? (
                <Alert
                  type="error"
                  showIcon
                  message="历史预览读取失败"
                  description={errorMessage(latestQuery.error)}
                />
              ) : null}
              {!latestQuery.isLoading && !latestQuery.isError && !latestQuery.data ? (
                <Typography.Text type="secondary">暂无历史预览</Typography.Text>
              ) : null}
              {latestQuery.data ? (
                <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }}>
                  <Descriptions.Item label="来源">
                    {latestQuery.data.trigger === 'SCHEDULED' ? '定时检查' : '手动检查'}
                  </Descriptions.Item>
                  <Descriptions.Item label="最近检查">
                    {new Date(latestQuery.data.last_checked_at).toLocaleString('zh-CN')}
                  </Descriptions.Item>
                  <Descriptions.Item label="到期">
                    {new Date(latestQuery.data.expires_at).toLocaleString('zh-CN')}
                  </Descriptions.Item>
                  <Descriptions.Item label="待处理">
                    {totalReadyItems(latestQuery.data)}
                  </Descriptions.Item>
                  <Descriptions.Item label="冲突">
                    {totalConflicts(latestQuery.data)}
                  </Descriptions.Item>
                  <Descriptions.Item label="提醒">{latestQuery.data.warnings}</Descriptions.Item>
                </Descriptions>
              ) : null}
              {syncError && !syncPreview ? <Alert type="error" showIcon message={syncError} /> : null}
              {postApplyWarning ? (
                <Alert type="warning" showIcon message={postApplyWarning} />
              ) : null}
              <Space wrap>
                <Button disabled={!latestQuery.data} onClick={viewLatestPreview}>
                  查看预览
                </Button>
                <Button
                  type="primary"
                  loading={previewMutation.isPending}
                  onClick={refreshPreview}
                >
                  刷新预览
                </Button>
              </Space>
            </Space>
          </section>
        ) : null}
        <Tree treeData={toTreeData(orgTreeQuery.data ?? [])} defaultExpandAll selectable={false} />
      </Card>

      <Modal
        title="钉钉组织同步预览"
        open={syncPreview !== null}
        width={1280}
        okText="确认应用变更"
        cancelText="取消"
        confirmLoading={applyMutation.isPending}
        okButtonProps={{ disabled: !canApplyPreview }}
        maskClosable={!applyMutation.isPending}
        closable={!applyMutation.isPending}
        onOk={applyPreview}
        onCancel={closeSyncPreview}
      >
        {syncPreview ? (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Alert
              type={totalConflicts(syncPreview) > 0 ? 'warning' : 'info'}
              showIcon
              message={`待应用：${syncPreview.ready_regions} 项区域变更、${syncPreview.ready_stores} 项门店变更、${syncPreview.ready_reviewers} 项负责人变更`}
              description={`冲突：${syncPreview.region_conflicts} 项区域、${syncPreview.store_conflicts} 项门店、${syncPreview.reviewer_conflicts} 项负责人；提醒 ${syncPreview.warnings} 项。`}
            />
            {isExpired(syncPreview, expiryClock) ? (
              <Alert type="error" showIcon message="此预览已过期，请刷新预览后再应用" />
            ) : null}
            {totalConflicts(syncPreview) > 0 ? (
              <Alert type="error" showIcon message="请先解决全部冲突，再刷新预览并应用" />
            ) : null}
            {totalReadyItems(syncPreview) === 0 ? (
              <Alert type="info" showIcon message="当前没有待应用的组织变更" />
            ) : null}
            {syncError ? <Alert type="error" showIcon message={syncError} /> : null}
            <Typography.Text type="secondary">
              {syncPreview.trigger === 'SCHEDULED' ? '定时检查' : '手动检查'} · 最近检查{' '}
              {new Date(syncPreview.last_checked_at).toLocaleString('zh-CN')} · 有效期至{' '}
              {new Date(syncPreview.expires_at).toLocaleString('zh-CN')}
            </Typography.Text>
            <OrganizationChangesSection kind="REGION" items={syncPreview.region_items} />
            <OrganizationChangesSection kind="STORE" items={syncPreview.store_items} />
            <section aria-label="负责人变更">
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <Typography.Title level={4} style={{ margin: 0 }}>
                  负责人变更
                </Typography.Title>
                <ReviewerChangesSection title="负责人分配" items={reviewerAssignments} />
                <ReviewerChangesSection title="负责人撤销" items={reviewerRemovals} removal />
                <ReviewerChangesSection title="负责人冲突" items={reviewerConflicts} />
              </Space>
            </section>
          </Space>
        ) : null}
      </Modal>
    </>
  )
}
