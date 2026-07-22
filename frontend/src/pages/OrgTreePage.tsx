import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
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
import { useState } from 'react'

import {
  applyDingTalkOrganization,
  previewDingTalkOrganization,
  type DingTalkOrganizationNodeAction,
  type DingTalkOrganizationChangeField,
  type DingTalkOrganizationNodeItem,
  type DingTalkOrganizationNodeKind,
  type DingTalkOrganizationPreview,
  type DingTalkOrganizationReviewerAction,
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

const ORGANIZATION_ACTION_LABEL: Record<
  DingTalkOrganizationNodeKind,
  Record<DingTalkOrganizationNodeAction, string>
> = {
  REGION: {
    LINK: '关联已有区域',
    CREATE: '创建新区域',
    ACTIVATE: '启用区域',
    UPDATE: '更新区域',
    DEACTIVATE: '停用区域',
  },
  STORE: {
    LINK: '关联已有门店',
    CREATE: '创建新门店',
    ACTIVATE: '启用门店',
    UPDATE: '更新门店',
    DEACTIVATE: '停用门店',
  },
}

const ORGANIZATION_ACTION_COLOR: Record<DingTalkOrganizationNodeAction, string> = {
  LINK: 'blue',
  CREATE: 'green',
  ACTIVATE: 'cyan',
  UPDATE: 'geekblue',
  DEACTIVATE: 'orange',
}

const ORGANIZATION_CHANGE_FIELD_LABEL: Record<DingTalkOrganizationChangeField, string> = {
  name: '名称',
  parent_id: '上级组织',
  dingtalk_dept_id: '钉钉部门 ID',
}

const REVIEWER_ACTION_LABEL: Record<DingTalkOrganizationReviewerAction, string> = {
  ASSIGN: '分配',
  REMOVE: '撤销',
  CONFLICT: '冲突',
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

function statusTag(status: DingTalkOrganizationSyncItemStatus) {
  return status === 'READY' ? <Tag color="blue">待应用</Tag> : <Tag color="orange">冲突</Tag>
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
          {ORGANIZATION_ACTION_LABEL[kind][item.action]}
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
      <Tag
        color={item.action === 'REMOVE' ? 'orange' : item.action === 'CONFLICT' ? 'red' : 'blue'}
      >
        {REVIEWER_ACTION_LABEL[item.action]}
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
      item.action === 'REMOVE' ? (
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

function totalReadyItems(preview: DingTalkOrganizationPreview): number {
  return preview.ready_stores + preview.ready_reviewers
}

function totalConflicts(preview: DingTalkOrganizationPreview): number {
  return preview.region_conflicts + preview.store_conflicts + preview.reviewer_conflicts
}

function hasBlockedRegionChanges(preview: DingTalkOrganizationPreview): boolean {
  return (
    preview.ready_regions > 0 ||
    preview.region_conflicts > 0 ||
    preview.region_items.length > 0
  )
}

interface ReviewerChangesSectionProps {
  action: DingTalkOrganizationReviewerAction
  items: DingTalkOrganizationReviewerItem[]
}

function ReviewerChangesSection({ action, items }: ReviewerChangesSectionProps) {
  const title = `负责人${REVIEWER_ACTION_LABEL[action]}（${items.length}）`

  return (
    <section aria-label={title}>
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {title}
        </Typography.Title>
        {action === 'REMOVE' && items.length > 0 ? (
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
          locale={{ emptyText: `无负责人${REVIEWER_ACTION_LABEL[action]}项` }}
          pagination={{ pageSize: 5, hideOnSinglePage: true }}
          scroll={{ x: 1500 }}
        />
      </Space>
    </section>
  )
}

export default function OrgTreePage() {
  const { user, hasGlobalPermission } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const canSyncDingTalkOrganization =
    hasGlobalPermission(Perm.DINGTALK_ORG_SYNC) && hasGlobalPermission(Perm.NOTIFICATION_MANAGE)
  const [syncPreview, setSyncPreview] = useState<DingTalkOrganizationPreview | null>(null)
  const [syncError, setSyncError] = useState<string | null>(null)
  const orgTreeQuery = useQuery({
    queryKey: ['orgTree', queryScope],
    queryFn: fetchOrgTree,
  })

  const previewMutation = useMutation({
    mutationFn: () => previewDingTalkOrganization(),
    onSuccess: (preview) => {
      setSyncError(null)
      setSyncPreview(preview)
    },
    onError: (error) => setSyncError(errorMessage(error)),
  })
  const applyMutation = useMutation({
    mutationFn: (batchId: string) => applyDingTalkOrganization(batchId),
    onSuccess: async (result) => {
      setSyncError(null)
      const refreshedTree = await orgTreeQuery.refetch()
      setSyncPreview(null)
      if (refreshedTree.isError) {
        const refreshError =
          '钉钉组织变更已应用，但组织架构刷新失败。请刷新页面后核对最新门店和负责人。'
        setSyncError(refreshError)
        message.warning(refreshError)
        return
      }
      message.success(
        result.already_applied
          ? '该批次此前已应用，组织架构已刷新。'
          : `已同步 ${result.applied_stores} 项门店变更、${result.applied_reviewers} 项负责人变更；${result.unresolved} 项冲突仍待处理。`,
      )
    },
    onError: (error) => setSyncError(errorMessage(error)),
  })

  function openSyncPreview(): void {
    setSyncError(null)
    previewMutation.mutate()
  }

  function closeSyncPreview(): void {
    if (applyMutation.isPending) return
    setSyncError(null)
    setSyncPreview(null)
  }

  function applyPreview(): void {
    if (
      !syncPreview ||
      hasBlockedRegionChanges(syncPreview) ||
      totalConflicts(syncPreview) > 0 ||
      totalReadyItems(syncPreview) === 0
    ) {
      return
    }
    applyMutation.mutate(syncPreview.batch_id)
  }

  if (orgTreeQuery.isLoading) return <Spin />

  const hasApplicableItems = syncPreview !== null && totalReadyItems(syncPreview) > 0
  const canApplyPreview =
    syncPreview !== null &&
    hasApplicableItems &&
    !hasBlockedRegionChanges(syncPreview) &&
    totalConflicts(syncPreview) === 0
  const reviewerAssignments =
    syncPreview?.reviewer_items.filter((item) => item.action === 'ASSIGN') ?? []
  const reviewerRemovals =
    syncPreview?.reviewer_items.filter((item) => item.action === 'REMOVE') ?? []
  const reviewerConflicts =
    syncPreview?.reviewer_items.filter((item) => item.action === 'CONFLICT') ?? []

  return (
    <>
      <Card
        title="组织架构"
        extra={
          canSyncDingTalkOrganization ? (
            <Button loading={previewMutation.isPending} onClick={openSyncPreview}>
              同步钉钉门店与负责人
            </Button>
          ) : null
        }
      >
        {syncError && !syncPreview ? (
          <Alert type="error" showIcon message={syncError} style={{ marginBottom: 16 }} />
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
              type="info"
              showIcon
              message={`待应用：${syncPreview.ready_regions} 项区域变更、${syncPreview.ready_stores} 项门店变更、${syncPreview.ready_reviewers} 项负责人变更`}
              description="当前可确认门店变更、负责人分配和负责人撤销。"
            />
            {hasBlockedRegionChanges(syncPreview) ? (
              <Alert
                type="warning"
                showIcon
                message="区域变更暂不可确认"
                description="区域变更将在组织层级应用支持完成后可确认。"
              />
            ) : null}
            <Alert
              type={
                totalConflicts(syncPreview) > 0 ? 'warning' : 'info'
              }
              showIcon
              message={`冲突项：${syncPreview.region_conflicts} 项区域冲突、${syncPreview.store_conflicts} 项门店冲突、${syncPreview.reviewer_conflicts} 项负责人冲突`}
              description={`钉钉区域 ${syncPreview.remote_regions} 个，本地区域 ${syncPreview.local_regions} 个；钉钉门店 ${syncPreview.remote_stores} 家，本地门店 ${syncPreview.local_stores} 家。`}
            />
            {syncPreview.reviewer_conflicts > 0 ? (
              <Alert
                type="error"
                showIcon
                message="请先修正钉钉负责人或员工身份信息后重新预览"
                description="存在负责人冲突时不允许确认，避免工资查看权限分配给错误人员。"
              />
            ) : null}
            {syncError ? <Alert type="error" showIcon message={syncError} /> : null}
            <Typography.Text type="secondary">
              本预览批次有效期至 {new Date(syncPreview.expires_at).toLocaleString('zh-CN')}。
            </Typography.Text>
            <OrganizationChangesSection kind="REGION" items={syncPreview.region_items} />
            <OrganizationChangesSection kind="STORE" items={syncPreview.store_items} />
            <ReviewerChangesSection action="ASSIGN" items={reviewerAssignments} />
            <ReviewerChangesSection action="REMOVE" items={reviewerRemovals} />
            <ReviewerChangesSection action="CONFLICT" items={reviewerConflicts} />
          </Space>
        ) : null}
      </Modal>
    </>
  )
}
