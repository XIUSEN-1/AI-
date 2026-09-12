import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { api, getToken } from "@/lib/api";
import { isRole, type Role } from "@/lib/role";
import AssessmentPage from "@/pages/AssessmentPage";
import AdminPage from "@/pages/AdminPage";
import HomePage from "@/pages/HomePage";
import LoginPage from "@/pages/LoginPage";
import PracticePage from "@/pages/PracticePage";
import ReportPage from "@/pages/ReportPage";
import TeacherPage from "@/pages/TeacherPage";

function RequireAuth({ children }: { children: React.ReactNode }) {
  if (!getToken()) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** 角色路由守卫：/api/auth/me 服务端校验，角色不符回首页（教师页对 admin 放行，与后端一致）。 */
function RequireRole({ roles, children }: { roles: Role[]; children: React.ReactNode }) {
  const [me, setMe] = useState<{ role: string } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api<{ role: string }>("/api/auth/me")
      .then(setMe)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "身份校验失败"));
  }, []);
  if (!getToken()) return <Navigate to="/login" replace />;
  if (error) return <div className="p-8 text-red-600">{error}</div>;
  if (!me) return <div className="p-8">身份校验中…</div>;
  if (!isRole(me.role) || !roles.includes(me.role)) return <Navigate to="/" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<RequireAuth><HomePage /></RequireAuth>} />
      <Route path="/assess" element={<RequireAuth><AssessmentPage /></RequireAuth>} />
      <Route path="/practice" element={<RequireAuth><PracticePage /></RequireAuth>} />
      <Route path="/report/:id" element={<RequireAuth><ReportPage /></RequireAuth>} />
      <Route
        path="/teacher"
        element={
          <RequireAuth>
            <RequireRole roles={["teacher", "admin"]}>
              <TeacherPage />
            </RequireRole>
          </RequireAuth>
        }
      />
      <Route
        path="/admin"
        element={
          <RequireAuth>
            <RequireRole roles={["admin"]}>
              <AdminPage />
            </RequireRole>
          </RequireAuth>
        }
      />
    </Routes>
  );
}
