import type { ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { AccountPage } from "./pages/Account";
import { AppsPage } from "./pages/Apps";
import { ApprovalsPage } from "./pages/Approvals";
import { AuditPage } from "./pages/Audit";
import { DashboardPage } from "./pages/Dashboard";
import { LoginPage } from "./pages/Login";
import { UsageLogsPage } from "./pages/UsageLogs";
import { ServerDetailPage } from "./pages/ServerDetail";
import { ServersPage } from "./pages/Servers";
import { UserDetailPage } from "./pages/UserDetail";
import { UsersPage } from "./pages/Users";
import { WorkflowsPage } from "./pages/Workflows";

function RequireAuth({ children }: { children: ReactNode }) {
  const { state } = useAuth();
  if (state.loading) {
    return (
      <div className="min-h-screen flex items-center justify-center text-slate-500">
        Loading…
      </div>
    );
  }
  if (!state.user) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

function RequireAdmin({ children }: { children: ReactNode }) {
  const { state } = useAuth();
  if (state.user?.role !== "admin") {
    return <Navigate to="/account" replace />;
  }
  return <>{children}</>;
}

function HomeRedirect() {
  const { state } = useAuth();
  if (state.user?.role === "admin") {
    return <Navigate to="/dashboard" replace />;
  }
  return <Navigate to="/account" replace />;
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<HomeRedirect />} />
        <Route
          path="dashboard"
          element={
            <RequireAdmin>
              <DashboardPage />
            </RequireAdmin>
          }
        />
        <Route path="account" element={<AccountPage />} />
        <Route path="apps" element={<AppsPage />} />
        <Route path="usage" element={<UsageLogsPage />} />
        <Route
          path="servers"
          element={
            <RequireAdmin>
              <ServersPage />
            </RequireAdmin>
          }
        />
        <Route
          path="workflows"
          element={
            <RequireAdmin>
              <WorkflowsPage />
            </RequireAdmin>
          }
        />
        <Route
          path="servers/:id"
          element={
            <RequireAdmin>
              <ServerDetailPage />
            </RequireAdmin>
          }
        />
        <Route
          path="users"
          element={
            <RequireAdmin>
              <UsersPage />
            </RequireAdmin>
          }
        />
        <Route
          path="users/:id"
          element={
            <RequireAdmin>
              <UserDetailPage />
            </RequireAdmin>
          }
        />
        <Route
          path="approvals"
          element={
            <RequireAdmin>
              <ApprovalsPage />
            </RequireAdmin>
          }
        />
        <Route
          path="audit"
          element={
            <RequireAdmin>
              <AuditPage />
            </RequireAdmin>
          }
        />
      </Route>
      <Route path="*" element={<HomeRedirect />} />
    </Routes>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}
