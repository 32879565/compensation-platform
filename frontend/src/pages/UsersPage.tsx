import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Input,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useEffect, useMemo, useState } from 'react'

import { fetchOrgUnits, type OrgUnit } from '../api/masterdata'
import {
  fetchReviewScopes,
  fetchUsers,
  replaceDingTalkRecipient,
  replaceLoginEnabled,
  replaceReviewScopes,
  type ManagedUser,
  type ReviewDepartment,
  type ReviewScope,
} from '../api/users'
import { useAuth } from '../auth/AuthContext'

const DEPARTMENT_LABEL: Record<ReviewDepartment, string> = {
  DINING: '厅面',
  KITCHEN: '厨房',
  OTHER: '其他',
}

function errorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response
    if (typeof response?.data?.detail === 'string') return response.data.detail
  }
  return '用户复核范围操作失败，请稍后重试。'
}

export default function UsersPage() {
  const { user } = useAuth()
  const queryScope = user?.username ?? 'anonymous'
  const queryClient = useQueryClient()
  const [selectedUserId, setSelectedUserId] = useState<number | undefined>()
  const [storeId, setStoreId] = useState<number | undefined>()
  const [department, setDepartment] = useState<ReviewDepartment>('DINING')
  const [draftScopes, setDraftScopes] = useState<ReviewScope[]>([])
  const [dingtalkUserId, setDingtalkUserId] = useState('')
  const usersQuery = useQuery({ queryKey: ['managedUsers', queryScope], queryFn: fetchUsers })
  const orgQuery = useQuery({ queryKey: ['userScopeOrgUnits', queryScope], queryFn: fetchOrgUnits })
  const scopesQuery = useQuery({
    queryKey: ['userReviewScopes', queryScope, selectedUserId],
    queryFn: () => fetchReviewScopes(selectedUserId!),
    enabled: selectedUserId !== undefined,
  })
  const selectedUser = usersQuery.data?.find((candidate) => candidate.id === selectedUserId)
  const stores = useMemo(
    () => (orgQuery.data ?? []).filter((org) => org.type === 'STORE'),
    [orgQuery.data],
  )
  const orgById = useMemo(
    () => new Map((orgQuery.data ?? []).map((org) => [org.id, org])),
    [orgQuery.data],
  )

  useEffect(() => {
    setSelectedUserId(undefined)
    setStoreId(undefined)
    setDraftScopes([])
    setDingtalkUserId('')
  }, [queryScope])

  useEffect(() => {
    setDraftScopes(scopesQuery.data ?? [])
  }, [scopesQuery.data, selectedUserId])

  const saveMutation = useMutation({
    mutationFn: ({ userId, scopes }: { userId: number; scopes: ReviewScope[] }) =>
      replaceReviewScopes(userId, scopes),
    onSuccess: async (_scopes, variables) => {
      message.success('复核范围已保存')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['managedUsers', queryScope] }),
        queryClient.invalidateQueries({
          queryKey: ['userReviewScopes', queryScope, variables.userId],
        }),
      ])
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const recipientMutation = useMutation({
    mutationFn: ({ userId, providerUserId }: { userId: number; providerUserId: string | null }) =>
      replaceDingTalkRecipient(userId, providerUserId),
    onSuccess: async (result) => {
      setDingtalkUserId('')
      message.success(result.configured ? '钉钉收件人已加密保存' : '钉钉收件人已清除')
      await queryClient.invalidateQueries({ queryKey: ['managedUsers', queryScope] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })
  const loginEnabledMutation = useMutation({
    mutationFn: ({ userId, enabled }: { userId: number; enabled: boolean }) =>
      replaceLoginEnabled(userId, enabled),
    onSuccess: async (result) => {
      message.success(result.login_enabled ? '后台登录已启用' : '已设为仅钉钉复核')
      await queryClient.invalidateQueries({ queryKey: ['managedUsers', queryScope] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const addScope = () => {
    if (storeId === undefined) {
      message.warning('请先选择门店')
      return
    }
    if (
      draftScopes.some((scope) => scope.org_unit_id === storeId && scope.department === department)
    ) {
      message.warning('该门店和部门已在复核范围内')
      return
    }
    setDraftScopes((current) => [...current, { org_unit_id: storeId, department }])
    setStoreId(undefined)
  }

  const scopeColumns: TableProps<ReviewScope>['columns'] = [
    {
      title: '门店',
      dataIndex: 'org_unit_id',
      render: (id: number) => {
        const store = orgById.get(id)
        return store ? `${store.code} · ${store.name}` : `门店 #${id}`
      },
    },
    {
      title: '部门',
      dataIndex: 'department',
      render: (value: ReviewDepartment) => DEPARTMENT_LABEL[value],
    },
    {
      title: '操作',
      key: 'action',
      render: (_: unknown, scope: ReviewScope) => (
        <Button
          danger
          size="small"
          disabled={saveMutation.isPending}
          onClick={() =>
            setDraftScopes((current) =>
              current.filter(
                (candidate) =>
                  candidate.org_unit_id !== scope.org_unit_id ||
                  candidate.department !== scope.department,
              ),
            )
          }
        >
          移除
        </Button>
      ),
    },
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Typography.Title level={3} style={{ margin: 0 }}>
        用户复核范围
      </Typography.Title>
      <Alert
        type="info"
        showIcon
        message="门店薪资复核必须显式授权"
        description="这里维护的是“门店 + 部门”复核范围；它不会自动授予集团或区域工资查看权限。角色和账号生命周期仍由受控的管理员流程维护。"
      />
      {usersQuery.isError && (
        <Alert type="error" showIcon message={errorMessage(usersQuery.error)} />
      )}
      <Card title="选择账号">
        <Select
          showSearch
          optionFilterProp="label"
          placeholder="选择要配置的账号"
          loading={usersQuery.isLoading}
          style={{ width: 360, maxWidth: '100%' }}
          value={selectedUserId}
          disabled={saveMutation.isPending}
          onChange={setSelectedUserId}
          options={(usersQuery.data ?? []).map((candidate: ManagedUser) => ({
            value: candidate.id,
            label: `${candidate.username} · ${candidate.roles.join(', ') || '无角色'}`,
          }))}
        />
        {selectedUser && (
          <Space wrap style={{ marginLeft: 16, marginTop: 8 }}>
            <Tag color={selectedUser.status === 'ACTIVE' ? 'green' : 'default'}>
              {selectedUser.status}
            </Tag>
            {selectedUser.roles.map((role) => (
              <Tag key={role}>{role}</Tag>
            ))}
            <Tag color={selectedUser.dingtalk_recipient_configured ? 'blue' : 'default'}>
              {selectedUser.dingtalk_recipient_configured ? '钉钉收件人已配置' : '钉钉收件人未配置'}
            </Tag>
            <Tag color={selectedUser.login_enabled ? 'green' : 'purple'}>
              {selectedUser.login_enabled ? '可登录后台' : '仅钉钉复核'}
            </Tag>
          </Space>
        )}
      </Card>
      {selectedUser && (
        <Card title={`配置 ${selectedUser.username} 的登录方式`}>
          <Alert
            type="info"
            showIcon
            message="店长和厨房经理应设为仅钉钉复核"
            description="关闭后台登录后，账号仍可接收钉钉工资通知，并通过钉钉免登查看本人负责范围；普通密码和历史刷新会话会立即失效。"
            style={{ marginBottom: 16 }}
          />
          <Popconfirm
            title={
              selectedUser.login_enabled ? '确认关闭该账号的后台登录？' : '确认重新启用后台登录？'
            }
            okText="确认"
            cancelText="取消"
            onConfirm={() =>
              loginEnabledMutation.mutate({
                userId: selectedUser.id,
                enabled: !selectedUser.login_enabled,
              })
            }
          >
            <Button
              loading={loginEnabledMutation.isPending}
              disabled={selectedUser.username === user?.username || loginEnabledMutation.isPending}
            >
              {selectedUser.login_enabled ? '设为仅钉钉复核' : '启用后台登录'}
            </Button>
          </Popconfirm>
        </Card>
      )}
      {selectedUser && (
        <Card title={`配置 ${selectedUser.username} 的钉钉收件人`}>
          <Alert
            type="warning"
            showIcon
            message="这里只保存钉钉 userid，不会触发任何消息。当前系统仍为沙箱模式。"
            description="userid 会在数据库中加密，保存后只显示是否已配置，不会从接口读回明文。"
            style={{ marginBottom: 16 }}
          />
          <Space wrap>
            <Input
              aria-label="钉钉 userid"
              value={dingtalkUserId}
              maxLength={256}
              autoComplete="off"
              placeholder="输入该账号对应的钉钉 userid"
              disabled={recipientMutation.isPending}
              onChange={(event) => setDingtalkUserId(event.target.value)}
              style={{ width: 360, maxWidth: '100%' }}
            />
            <Button
              type="primary"
              loading={recipientMutation.isPending}
              disabled={!dingtalkUserId.trim() || recipientMutation.isPending}
              onClick={() =>
                recipientMutation.mutate({
                  userId: selectedUser.id,
                  providerUserId: dingtalkUserId.trim(),
                })
              }
            >
              加密保存 userid
            </Button>
            {selectedUser.dingtalk_recipient_configured ? (
              <Popconfirm
                title="确认清除该账号的钉钉收件人吗？"
                okText="清除"
                cancelText="取消"
                onConfirm={() =>
                  recipientMutation.mutate({ userId: selectedUser.id, providerUserId: null })
                }
              >
                <Button danger disabled={recipientMutation.isPending}>
                  清除配置
                </Button>
              </Popconfirm>
            ) : null}
          </Space>
        </Card>
      )}
      {selectedUser && (
        <Card title={`配置 ${selectedUser.username} 的复核范围`} loading={scopesQuery.isLoading}>
          <Space wrap style={{ marginBottom: 16 }}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择门店"
              value={storeId}
              disabled={saveMutation.isPending}
              onChange={setStoreId}
              style={{ width: 260 }}
              options={stores.map((store: OrgUnit) => ({
                value: store.id,
                label: `${store.code} · ${store.name}`,
              }))}
            />
            <Select<ReviewDepartment>
              value={department}
              disabled={saveMutation.isPending}
              onChange={setDepartment}
              style={{ width: 150 }}
              options={(Object.entries(DEPARTMENT_LABEL) as [ReviewDepartment, string][]).map(
                ([value, label]) => ({ value, label }),
              )}
            />
            <Button disabled={saveMutation.isPending} onClick={addScope}>
              添加范围
            </Button>
          </Space>
          {scopesQuery.isError && (
            <Alert type="error" showIcon message={errorMessage(scopesQuery.error)} />
          )}
          <Table<ReviewScope>
            rowKey={(scope) => `${scope.org_unit_id}-${scope.department}`}
            columns={scopeColumns}
            dataSource={draftScopes}
            pagination={false}
            locale={{ emptyText: '尚未分配任何复核范围。' }}
          />
          <Popconfirm
            title="确认保存这组复核范围吗？"
            okText="保存"
            cancelText="取消"
            disabled={saveMutation.isPending}
            onConfirm={() => {
              if (selectedUserId !== undefined) {
                saveMutation.mutate({ userId: selectedUserId, scopes: draftScopes })
              }
            }}
          >
            <Button
              type="primary"
              disabled={saveMutation.isPending}
              loading={saveMutation.isPending}
              style={{ marginTop: 16 }}
            >
              保存复核范围
            </Button>
          </Popconfirm>
        </Card>
      )}
    </Space>
  )
}
