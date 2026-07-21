import { Button, Layout, Menu, Typography } from 'antd'
import type { ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { useAuth } from '../auth/AuthContext'
import { NAV_ITEMS } from '../auth/navigation'

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout, hasPermission } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const items = NAV_ITEMS.filter((m) =>
    typeof m.permission === 'string'
      ? hasPermission(m.permission)
      : m.permission.some((permission) => hasPermission(permission)),
  ).map((m) => ({
    key: m.key,
    label: <span data-testid={`nav-${m.key}`}>{m.label}</span>,
  }))
  const selectedKey = location.pathname.split('/')[1] || 'dashboard'

  return (
    <Layout data-testid="app-shell" style={{ minHeight: '100vh' }}>
      <Layout.Header
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
      >
        <Typography.Text style={{ color: '#fff', fontSize: 18 }}>薪酬一体化平台</Typography.Text>
        <span data-testid="current-username" style={{ color: '#fff' }}>
          {user?.username}
          <Button
            data-testid="logout"
            type="link"
            style={{ color: '#fff' }}
            onClick={() => void logout().catch(() => undefined)}
          >
            退出
          </Button>
        </span>
      </Layout.Header>
      <Layout>
        <Layout.Sider width={200} theme="light">
          <Menu
            mode="inline"
            items={items}
            selectedKeys={[selectedKey]}
            onClick={({ key }) => navigate(`/${key}`)}
            style={{ height: '100%' }}
          />
        </Layout.Sider>
        <Layout.Content style={{ padding: 24 }}>{children}</Layout.Content>
      </Layout>
    </Layout>
  )
}
