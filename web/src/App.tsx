import { Navigate, Route, Routes } from "react-router-dom";

import { getToken } from "@/lib/api";
import AssessmentPage from "@/pages/AssessmentPage";
import HomePage from "@/pages/HomePage";
import LoginPage from "@/pages/LoginPage";
import ReportPage from "@/pages/ReportPage";

function RequireAuth({ children }: { children: React.ReactNode }) {
  if (!getToken()) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<RequireAuth><HomePage /></RequireAuth>} />
      <Route path="/assess" element={<RequireAuth><AssessmentPage /></RequireAuth>} />
      <Route path="/report/:id" element={<RequireAuth><ReportPage /></RequireAuth>} />
    </Routes>
  );
}
