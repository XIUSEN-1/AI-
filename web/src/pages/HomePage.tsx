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

interface WrongItem {
  dimension: string;
  dimension_name: string;
  wrong_count: number;
}

interface WrongbookBrief {
  total: number;
  dims: { dimension: string; name: string; count: number }[];
}

interface ActiveSession {
  session_id: number;
  stage: string;
  progress: Record<string, { n: number }>;
  started_at: string;
}

const STAGE_LABELS: Record<string, string> = {
  objective: "客观题",
  dialog: "对话式测评",
  practical: "实操任务",
  ready: "完成待生成报告",
};

// 错题本按维度聚合：每条错题记录即一道去重题目（与后端口径一致），维度内计数 = 题数
function aggregateWrongbook(items: WrongItem[]): WrongbookBrief {
  const byDim = new Map<string, { dimension: string; name: string; count: number }>();
  for (const it of items) {
    const d = byDim.get(it.dimension) ?? { dimension: it.dimension, name: it.dimension_name, count: 0 };
    d.count += 1;
    byDim.set(it.dimension, d);
  }
  return {
    total: items.length,
    dims: [...byDim.values()].sort((a, b) => b.count - a.count || a.dimension.localeCompare(b.dimension)),
  };
}

export default function HomePage() {
  const navigate = useNavigate();
  const [me, setMe] = useState<Me | null>(null);
  const [reports, setReports] = useState<ReportBrief[]>([]);
  const [wrongbook, setWrongbook] = useState<WrongbookBrief | null>(null);
  const [wrongFailed, setWrongFailed] = useState(false);
  const [active, setActive] = useState<ActiveSession | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Me>("/api/auth/me").then(setMe).catch((e: unknown) => setError(e instanceof Error ? e.message : "加载失败"));
  }, []);

  // 学员首页数据（角色就绪后再取，教师/管理员首页不展示学员卡片）
  useEffect(() => {
    if (me?.role !== "student") return;
    api<ReportBrief[]>("/api/reports/mine").then(setReports).catch(() => setReports([]));
    api<{ items: WrongItem[]; total: number }>("/api/me/wrongbook")
      .then((w) => setWrongbook(aggregateWrongbook(w.items)))
      .catch(() => setWrongFailed(true));
    api<ActiveSession | null>("/api/sessions/active")
      .then(setActive)
      .catch(() => setActive(null)); // 续答检测失败不阻断首页：进入测评页会再次检测并恢复
  }, [me]);

  const isStudent = me?.role === "student";
  const answeredCount = active
    ? Object.values(active.progress).reduce((sum, p) => sum + p.n, 0)
    : 0;

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

      {isStudent && active && (
        <Card className="border-indigo-200 bg-indigo-50">
          <CardHeader>
            <CardTitle className="text-base">有一次未完成的测评</CardTitle>
          </CardHeader>
          <CardContent className="flex items-center justify-between">
            <p className="text-sm text-slate-600">
              进行到「{STAGE_LABELS[active.stage] ?? active.stage}」阶段 · 已答 {answeredCount} 题
            </p>
            <Button onClick={() => navigate("/assess")}>继续测评</Button>
          </CardContent>
        </Card>
      )}

      {isStudent && (
        <>
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
              <CardTitle className="flex items-center gap-2 text-base">
                错题本与练习
                {wrongbook && wrongbook.total > 0 && <Badge>错题 {wrongbook.total} 题</Badge>}
              </CardTitle>
            </CardHeader>
            <CardContent className="flex items-center justify-between gap-3">
              {wrongFailed ? (
                <p className="text-sm text-slate-500">错题本加载失败，请稍后刷新重试</p>
              ) : wrongbook === null ? (
                <p className="text-sm text-slate-500">错题本加载中…</p>
              ) : wrongbook.total === 0 ? (
                <p className="text-sm text-slate-500">暂无错题。完成一次正式测评后，这里会按考点汇总错题并生成针对练习。</p>
              ) : (
                <>
                  <div className="flex flex-wrap gap-1.5">
                    {wrongbook.dims.map((d) => (
                      <Link key={d.dimension} to={`/practice?dimension=${d.dimension}`}>
                        <Badge variant="secondary" className="cursor-pointer hover:bg-slate-200">
                          {d.name} {d.count} 题 · 去练习
                        </Badge>
                      </Link>
                    ))}
                  </div>
                  <Button onClick={() => navigate("/practice")}>去练习</Button>
                </>
              )}
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
        </>
      )}

      {me?.role === "teacher" && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">班级看板</CardTitle>
          </CardHeader>
          <CardContent className="flex items-center justify-between">
            <p className="text-sm text-slate-500">查看所带班级的六维均值、共性短板、学员等级与成长曲线</p>
            <Button onClick={() => navigate("/teacher")}>进入看板</Button>
          </CardContent>
        </Card>
      )}

      {me?.role === "admin" && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">管理后台</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            <Button onClick={() => navigate("/admin?tab=questions")}>题库后台</Button>
            <Button variant="outline" onClick={() => navigate("/admin?tab=review")}>复核队列</Button>
            <Button variant="outline" onClick={() => navigate("/admin?tab=teacher")}>创建教师</Button>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
