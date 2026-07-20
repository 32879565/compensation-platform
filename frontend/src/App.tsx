import { BrowserRouter, Navigate, Outlet, Route, Routes } from 'react-router-dom'

import { AuthProvider } from './auth/AuthContext'
import { ProtectedRoute } from './auth/ProtectedRoute'
import { AppShell } from './components/AppShell'
import EmployeesPage from './pages/EmployeesPage'
import GradesPage from './pages/GradesPage'
import Home from './pages/Home'
import Login from './pages/Login'
import OrgTreePage from './pages/OrgTreePage'
import Placeholder from './pages/Placeholder'

function ProtectedLayout() {
  return (
    <ProtectedRoute>
      <AppShell>
        <Outlet />
      </AppShell>
    </ProtectedRoute>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route element={<ProtectedLayout />}>
            <Route path="/" element={<Home />} />
            <Route path="/dashboard" element={<Home />} />
            <Route path="/org" element={<OrgTreePage />} />
            <Route path="/employees" element={<EmployeesPage />} />
            <Route path="/grades" element={<GradesPage />} />
            <Route path="/attendance" element={<Placeholder title="考勤" />} />
            <Route path="/payroll" element={<Placeholder title="核算" />} />
            <Route path="/adjustment" element={<Placeholder title="调薪" />} />
            <Route path="/budget" element={<Placeholder title="预算" />} />
            <Route path="/payslip" element={<Placeholder title="我的工资条" />} />
            <Route path="/audit" element={<Placeholder title="审计日志" />} />
            <Route path="/users" element={<Placeholder title="用户权限" />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  )
}
