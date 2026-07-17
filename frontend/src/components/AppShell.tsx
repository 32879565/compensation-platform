import { Button, Layout, Menu, Typography } from 'antd'
import type { ReactNode } from 'react'

import { useAuth } from '../auth/AuthContext'
import { Perm } from '../auth/permissions'

interface MenuDef {
  key: string
  label: string
  permission: string
}

// 菜单项按权限过滤：无权限的模块不渲染（前端门禁，真正的授权在后端强制）。
const MENU: MenuDef[] = [
  { key: 'dashboard', label: '看板', permission: Perm.DASHBOARD_READ },
  { key: 'org', label: '组织', permission: Perm.ORG_READ },
  { key: 'employee', label: '员工', permission: Perm.EMPLOYEE_READ },
  { key: 'grade', label: '职级薪档', permission: Perm.GRADE_READ },
  { key: 'attendance', label: '考勤', permission: Perm.ATTENDANCE_READ },
  { key: 'payroll', label: '核算', permission: Perm.PAYROLL_READ },
  { key: 'adjustment', label: '调薪', permission: Perm.ADJUSTMENT_READ },
  { key: 'budget', label: '预算', permission: Perm.BUDGET_READ },
  { key: 'payslip', label: '我的工资条', permission: Perm.PAYSLIP_READ_SELF },
  { key: 'audit', label: '审计日志', permission: Perm.AUDIT_READ },
  { key: 'users', label: '用户权限', permission: Perm.USER_MANAGE },
]

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout, hasPermission } = useAuth()
  const items = MENU.filter((m) => hasPermission(m.permission)).map((m) => ({
    key: m.key,
    label: m.label,
  }))

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Header
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
      >
        <Typography.Text style={{ color: '#fff', fontSize: 18 }}>薪酬一体化平台</Typography.Text>
        <span style={{ color: '#fff' }}>
          {user?.username}
          <Button type="link" style={{ color: '#fff' }} onClick={() => void logout()}>
            退出
          </Button>
        </span>
      </Layout.Header>
      <Layout>
        <Layout.Sider width={200} theme="light">
          <Menu mode="inline" items={items} style={{ height: '100%' }} />
        </Layout.Sider>
        <Layout.Content style={{ padding: 24 }}>{children}</Layout.Content>
      </Layout>
    </Layout>
  )
}
