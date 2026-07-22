import { lazy, Suspense } from 'react'
import { Alert, Spin } from 'antd'
import { BrowserRouter, Navigate, Outlet, Route, Routes } from 'react-router-dom'

import { AuthProvider, useAuth } from './auth/AuthContext'
import {
  canAccessNavigationItem,
  firstAccessiblePath,
  navigationItemForPath,
} from './auth/navigation'
import { ProtectedRoute } from './auth/ProtectedRoute'
import { AppShell } from './components/AppShell'

const AttendancePage = lazy(() => import('./pages/AttendancePage'))
const AdjustmentPage = lazy(() => import('./pages/AdjustmentPage'))
const AuditPage = lazy(() => import('./pages/AuditPage'))
const BudgetPage = lazy(() => import('./pages/BudgetPage'))
const ComponentsPage = lazy(() => import('./pages/ComponentsPage'))
const CompAppealsPage = lazy(() => import('./pages/CompAppealsPage'))
const DashboardPage = lazy(() => import('./pages/DashboardPage'))
const EmployeesPage = lazy(() => import('./pages/EmployeesPage'))
const ExportPage = lazy(() => import('./pages/ExportPage'))
const GradesPage = lazy(() => import('./pages/GradesPage'))
const HolidayCalendarPage = lazy(() => import('./pages/HolidayCalendarPage'))
const ImportsPage = lazy(() => import('./pages/ImportsPage'))
const Login = lazy(() => import('./pages/Login'))
const ManagerReviewPage = lazy(() => import('./pages/ManagerReviewPage'))
const MonthlyPayrollAdjustmentsPage = lazy(() => import('./pages/MonthlyPayrollAdjustmentsPage'))
const OrgTreePage = lazy(() => import('./pages/OrgTreePage'))
const PayrollPage = lazy(() => import('./pages/PayrollPage'))
const PayrollPoliciesPage = lazy(() => import('./pages/PayrollPoliciesPage'))
const PayslipPage = lazy(() => import('./pages/PayslipPage'))
const SalaryHistoryPage = lazy(() => import('./pages/SalaryHistoryPage'))
const UsersPage = lazy(() => import('./pages/UsersPage'))

function RouteLoading() {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 160 }}>
      <Spin size="large" />
    </div>
  )
}

function ProtectedLayout() {
  return (
    <ProtectedRoute>
      <AppShell>
        <Outlet />
      </AppShell>
    </ProtectedRoute>
  )
}

function HomeRedirect() {
  const { user } = useAuth()
  return (
    <Navigate
      to={
        firstAccessiblePath(user?.permissions ?? [], user?.globalPermissions ?? []) ?? '/no-access'
      }
      replace
    />
  )
}

function PermissionRoute({ path, children }: { path: string; children: React.ReactNode }) {
  const { user } = useAuth()
  const permissions = user?.permissions ?? []
  const globalPermissions = user?.globalPermissions ?? []
  const route = navigationItemForPath(path)

  if (route && canAccessNavigationItem(permissions, globalPermissions, route))
    return <>{children}</>
  return (
    <Navigate to={firstAccessiblePath(permissions, globalPermissions) ?? '/no-access'} replace />
  )
}

function NoAccessPage() {
  return (
    <Alert
      type="warning"
      showIcon
      message="当前账号没有可访问的功能模块"
      description="请联系管理员分配与你职责相符的权限。"
    />
  )
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter
        future={{
          v7_startTransition: true,
          v7_relativeSplatPath: true,
        }}
      >
        <Suspense fallback={<RouteLoading />}>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/manager-review/:reviewId" element={<ManagerReviewPage />} />
            <Route element={<ProtectedLayout />}>
              <Route path="/" element={<HomeRedirect />} />
              <Route path="/no-access" element={<NoAccessPage />} />
              <Route
                path="/dashboard"
                element={
                  <PermissionRoute path="/dashboard">
                    <DashboardPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/org"
                element={
                  <PermissionRoute path="/org">
                    <OrgTreePage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/employees"
                element={
                  <PermissionRoute path="/employees">
                    <EmployeesPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/salary-history"
                element={
                  <PermissionRoute path="/salary-history">
                    <SalaryHistoryPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/imports"
                element={
                  <PermissionRoute path="/imports">
                    <ImportsPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/grades"
                element={
                  <PermissionRoute path="/grades">
                    <GradesPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/components"
                element={
                  <PermissionRoute path="/components">
                    <ComponentsPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/comp-appeals"
                element={
                  <PermissionRoute path="/comp-appeals">
                    <CompAppealsPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/attendance"
                element={
                  <PermissionRoute path="/attendance">
                    <AttendancePage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/holiday-calendar"
                element={
                  <PermissionRoute path="/holiday-calendar">
                    <HolidayCalendarPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/policies"
                element={
                  <PermissionRoute path="/policies">
                    <PayrollPoliciesPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/payroll"
                element={
                  <PermissionRoute path="/payroll">
                    <PayrollPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/payroll-adjustments"
                element={
                  <PermissionRoute path="/payroll-adjustments">
                    <MonthlyPayrollAdjustmentsPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/adjustment"
                element={
                  <PermissionRoute path="/adjustment">
                    <AdjustmentPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/budget"
                element={
                  <PermissionRoute path="/budget">
                    <BudgetPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/payslip"
                element={
                  <PermissionRoute path="/payslip">
                    <PayslipPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/audit"
                element={
                  <PermissionRoute path="/audit">
                    <AuditPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/export"
                element={
                  <PermissionRoute path="/export">
                    <ExportPage />
                  </PermissionRoute>
                }
              />
              <Route
                path="/users"
                element={
                  <PermissionRoute path="/users">
                    <UsersPage />
                  </PermissionRoute>
                }
              />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </BrowserRouter>
    </AuthProvider>
  )
}
