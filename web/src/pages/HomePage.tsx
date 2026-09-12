import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, clearToken } from "@/lib/api";

interface Me {
  id: number;
  name: string;
  role: string;
}

interface ReportBrief {
  id: number;
  created_at: string;
  total_level: number;
  total_level_name: string;
}

export default function HomePage() {
  const navigate = useNavigate();
  const [me, setMe] = useState<Me | null>(null);
  const [reports, setReports] = useState<ReportBrief[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Me>("/api/auth/me").then(setMe).catch((e: unknown) => setError(e instanceof Error ? e.message : "加载失败"));
    api<ReportBrief[]>("/api/reports/mine").then(setReports).catch(() => setReports([]));
  }, []);

  return (
    <div className="mx-auto max-w-2xl space-y-4 p-4">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">AI 能力罗盘</h1>
          <p className="text-sm text-slate-500">{me ? `${me.name}，欢迎回来` : "加载中…"}</p>
        </div>
        <Button
          variant="ghost"
          onClick={() => {
            clearToken();
            navigate("/login");
          }}
        >
          退出
        </Button>
      </header>
      {error && <p className="text-sm text-red-600">{error}</p>}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">开始一次测评</CardTitle>
        </CardHeader>
        <CardContent className="flex items-center justify-between">
          <p className="text-sm text-slate-500">六维自适应出题，约 25 分钟，可随时查看能力轨迹</p>
          <Button onClick={() => navigate("/assess")}>开始测评</Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">历史报告</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {reports.length === 0 && <p className="text-sm text-slate-500">还没有测评记录</p>}
          {reports.map((r) => (
            <Link key={r.id} to={`/report/${r.id}`} className="flex items-center justify-between rounded border p-3 hover:bg-slate-50">
              <span className="text-sm">{new Date(r.created_at).toLocaleString("zh-CN")}</span>
              <Badge>{r.total_level_name}</Badge>
            </Link>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
