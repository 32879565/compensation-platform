import { Perm } from './permissions'

export type RoutePermission = string | readonly string[]

export interface NavigationItem {
  key: string
  path: string
  label: string
  permission: RoutePermission
  requiresGlobalScope?: boolean
}

export const NAV_ITEMS: readonly NavigationItem[] = [
  { key: 'dashboard', path: '/dashboard', label: '看板', permission: Perm.DASHBOARD_READ },
  { key: 'org', path: '/org', label: '组织', permission: Perm.ORG_READ },
  { key: 'employees', path: '/employees', label: '员工', permission: Perm.EMPLOYEE_READ },
  {
    key: 'salary-history',
    path: '/salary-history',
    label: '历史薪资',
    permission: Perm.SALARY_READ,
  },
  {
    key: 'imports',
    path: '/imports',
    label: '薪酬导入',
    permission: Perm.IMPORT_RUN,
    requiresGlobalScope: true,
  },
  { key: 'grades', path: '/grades', label: '职级体系', permission: Perm.GRADE_READ },
  { key: 'components', path: '/components', label: '薪资组件', permission: Perm.STRUCTURE_READ },
  {
    key: 'comp-appeals',
    path: '/comp-appeals',
    label: '薪酬申诉',
    permission: [Perm.PAYROLL_REVIEW, Perm.ADJUSTMENT_READ, Perm.NOTIFICATION_MANAGE],
  },
  { key: 'attendance', path: '/attendance', label: '考勤', permission: Perm.ATTENDANCE_READ },
  {
    key: 'holiday-calendar',
    path: '/holiday-calendar',
    label: '法定日历',
    permission: Perm.HOLIDAY_CALENDAR_READ,
  },
  { key: 'policies', path: '/policies', label: '薪税政策', permission: Perm.POLICY_READ },
  { key: 'payroll', path: '/payroll', label: '核算', permission: Perm.PAYROLL_READ },
  {
    key: 'payroll-adjustments',
    path: '/payroll-adjustments',
    label: '补发补扣',
    permission: Perm.PAYROLL_CORRECT,
  },
  {
    key: 'adjustment',
    path: '/adjustment',
    label: '调薪申请',
    permission: [Perm.ADJUSTMENT_READ, Perm.ADJUSTMENT_CREATE, Perm.ADJUSTMENT_APPROVE],
  },
  { key: 'budget', path: '/budget', label: '预算', permission: Perm.BUDGET_READ },
  { key: 'payslip', path: '/payslip', label: '我的工资条', permission: Perm.PAYSLIP_READ_SELF },
  { key: 'audit', path: '/audit', label: '审计日志', permission: Perm.AUDIT_READ },
  { key: 'export', path: '/export', label: '数据导出', permission: Perm.EXPORT_DATA },
  { key: 'users', path: '/users', label: '用户权限', permission: Perm.USER_MANAGE },
] as const

export function hasRoutePermission(
  permissions: readonly string[],
  required: RoutePermission,
): boolean {
  return typeof required === 'string'
    ? permissions.includes(required)
    : required.some((permission) => permissions.includes(permission))
}

export function navigationItemForPath(path: string): NavigationItem | undefined {
  return NAV_ITEMS.find((item) => item.path === path)
}

export function canAccessNavigationItem(
  permissions: readonly string[],
  globalPermissions: readonly string[],
  item: NavigationItem,
): boolean {
  const required = typeof item.permission === 'string' ? [item.permission] : item.permission
  return required.some(
    (permission) =>
      permissions.includes(permission) &&
      (!item.requiresGlobalScope || globalPermissions.includes(permission)),
  )
}

export function firstAccessiblePath(
  permissions: readonly string[],
  globalPermissions: readonly string[] = [],
): string | null {
  return (
    NAV_ITEMS.find((item) => canAccessNavigationItem(permissions, globalPermissions, item))?.path ??
    null
  )
}
