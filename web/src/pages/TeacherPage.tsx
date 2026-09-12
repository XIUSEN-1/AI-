import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  CartesianGrid,
  Line,
  LineChart,
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
  XAxis,
  YAxis,
} from "recharts";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError, getToken } from "@/lib/api";

interface ClassOut {
  id: number;
  name: string;
  invite_code: string;
  student_count: number;
}

interface Analytics {
  student_count: number;
  radar: { dimension: string; label: string; avg_percent: number }[];
  dimension_rank: { dimension: string; label: string; avg_percent: number }[];
  weakness_top3: { dimension: string; tags: string[] }[];
  students: {
    id: number;
    name: string;
    student_no: string;
    last_level_name: string | null;
    reports: number;
    trend_slope: number;
  }[];
  timeline: { date: string; avg_percent: number }[];
}

/** 趋势斜率（分/次）转中文箭头文案 */
function trendText(slope: number): string {
  if (slope > 0) return `↑ +${slope}`;
  if (slope < 0) return `↓ ${slope}`;
  return "→ 持平";
}

export default function TeacherPage() {
  const [classes, setClasses] = useState<ClassOut[] | null>(null);
  const [classId, setClassId] = useState<number | null>(null);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [creating, setCreating] = useState(false); // 建班表单展开
  const [newName, setNewName] = useState("");
  const [creatingClass, setCreatingClass] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api<ClassOut[]>("/api/teacher/classes")
      .then((list) => {
        setClasses(list);
        if (list.length > 0) setClassId(list[0].id);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "班级列表加载失败"));
  }, []);

  useEffect(() => {
    if (classId === null) return;
    setAnalytics(null);
    api<Analytics>(`/api/teacher/classes/${classId}/analytics`)
      .then(setAnalytics)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "看板数据加载失败"));
  }, [classId]);

  async function createClass() {
    if (creatingClass) return;
    const name = newName.trim();
    if (!name) {
      setError("请输入班级名称");
      return;
    }
    setCreatingClass(true);
    setError("");
    try {
      const k = await api<ClassOut>("/api/teacher/classes", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      setClasses((prev) => [...(prev ?? []), k]);
      setClassId(k.id);
      setNewName("");
      setCreating(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "建班失败，请重试");
    } finally {
      setCreatingClass(false);
    }
  }

  // 导出 CSV：带鉴权头取 blob 再触发浏览器下载（后端为 UTF-8 BOM，Excel 可直接打开）
  async function exportCsv() {
    if (classId === null || exporting) return;
    setExporting(true);
    setError("");
    try {
      const headers = new Headers();
      const token = getToken();
      if (token) headers.set("Authorization", `Bearer ${token}`);
      const resp = await fetch(`/api/teacher/classes/${classId}/export.csv`, { headers });
      if (!resp.ok) {
        let detail = `导出失败（${resp.status}）`;
        try {
          const body = (await resp.json()) as { detail?: string };
          if (body?.detail) detail = String(body.detail);
        } catch {
          // 非 JSON 响应，用默认文案
        }
        throw new ApiError(resp.status, detail);
      }
      const url = URL.createObjectURL(await resp.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = `class_${classId}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出失败，请重试");
    } finally {
      setExporting(false);
    }
  }

  const current = classes?.find((c) => c.id === classId) ?? null;

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4">
      <header className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">教师班级看板</h1>
        <Link to="/" className="text-sm text-slate-500 hover:text-slate-700">返回首页</Link>
      </header>
      {error && <p className="text-sm text-red-600">{error}</p>}

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">选择班级</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {classes === null ? (
            <p className="text-sm text-slate-500">班级列表加载中…</p>
          ) : classes.length === 0 ? (
            <p className="text-sm text-slate-500">还没有班级，先创建一个并把邀请码告诉学员。</p>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <select
                className="rounded-md border bg-white px-3 py-2 text-sm"
                value={classId ?? ""}
                onChange={(e) => setClassId(Number(e.target.value))}
              >
                {classes.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}（{c.student_count} 人）
                  </option>
                ))}
              </select>
              {current && <Badge variant="secondary">邀请码 {current.invite_code}</Badge>}
            </div>
          )}
          {creating ? (
            <div className="flex flex-wrap items-end gap-2">
              <div className="space-y-1">
                <Label htmlFor="class-name">班级名称</Label>
                <Input
                  id="class-name"
                  value={newName}
                  maxLength={32}
                  placeholder="如：2026 秋季 1 班"
                  onChange={(e) => setNewName(e.target.value)}
                />
              </div>
              <Button onClick={createClass} disabled={creatingClass}>
                {creatingClass ? "创建中…" : "确认创建"}
              </Button>
              <Button variant="ghost" onClick={() => setCreating(false)}>
                取消
              </Button>
            </div>
          ) : (
            <Button variant="outline" onClick={() => setCreating(true)}>
              创建班级
            </Button>
          )}
        </CardContent>
      </Card>

      {classId !== null && analytics === null && <p className="text-sm text-slate-500">看板数据加载中…</p>}

      {analytics && (
        <>
          <div className="grid gap-4 md:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">六维均值雷达（学员最新报告）</CardTitle>
              </CardHeader>
              <CardContent className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <RadarChart data={analytics.radar.map((r) => ({ subject: r.label, value: r.avg_percent }))}>
                    <PolarGrid />
                    <PolarAngleAxis dataKey="subject" />
                    <PolarRadiusAxis domain={[0, 100]} />
                    <Radar dataKey="value" stroke="#6366f1" fill="#6366f1" fillOpacity={0.45} />
                  </RadarChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="text-base">维度排名</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                {analytics.dimension_rank.map((d, i) => (
                  <div key={d.dimension} className="flex items-center gap-2">
                    <span className="w-24 shrink-0 text-sm">{d.label}</span>
                    <div className="h-2.5 flex-1 overflow-hidden rounded bg-slate-100">
                      <div className="h-full rounded bg-indigo-500" style={{ width: `${d.avg_percent}%` }} />
                    </div>
                    <span className="w-14 shrink-0 text-right text-xs text-slate-500">{d.avg_percent}%</span>
                    {i === 0 && <Badge>最强</Badge>}
                  </div>
                ))}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="text-base">共性短板 TOP3</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {analytics.weakness_top3.length === 0 && (
                  <p className="text-sm text-slate-500">暂无学员错题数据</p>
                )}
                {analytics.weakness_top3.map((w) => (
                  <div key={w.dimension}>
                    <p className="text-sm font-medium">
                      {analytics.radar.find((r) => r.dimension === w.dimension)?.label ?? w.dimension}
                    </p>
                    {w.tags.length === 0 ? (
                      <p className="text-xs text-slate-500">该维度暂无明显薄弱考点</p>
                    ) : (
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        {w.tags.map((t) => (
                          <Badge key={t} variant="secondary">{t}</Badge>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="text-base">班级成长曲线（按日综合均值）</CardTitle>
              </CardHeader>
              <CardContent className="h-64">
                {analytics.timeline.length === 0 ? (
                  <p className="py-6 text-center text-sm text-slate-500">暂无报告数据</p>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={analytics.timeline}>
                      <CartesianGrid strokeDasharray="3 3" />
                      <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                      <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} />
                      <Line type="monotone" dataKey="avg_percent" stroke="#6366f1" strokeWidth={2} dot />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-base">学员概览（{analytics.student_count} 人）</CardTitle>
              <Button variant="outline" size="sm" onClick={exportCsv} disabled={exporting}>
                {exporting ? "导出中…" : "导出 CSV"}
              </Button>
            </CardHeader>
            <CardContent className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-left text-xs text-slate-500">
                    <th className="py-2 pr-3 font-medium">姓名</th>
                    <th className="py-2 pr-3 font-medium">学号</th>
                    <th className="py-2 pr-3 font-medium">最近等级</th>
                    <th className="py-2 pr-3 font-medium">测评次数</th>
                    <th className="py-2 font-medium">成长趋势</th>
                  </tr>
                </thead>
                <tbody>
                  {analytics.students.length === 0 && (
                    <tr>
                      <td colSpan={5} className="py-3 text-slate-500">
                        班级暂无学员，把邀请码 {current?.invite_code ?? "—"} 发给学员注册即可加入
                      </td>
                    </tr>
                  )}
                  {analytics.students.map((s) => (
                    <tr key={s.id} className="border-b last:border-0">
                      <td className="py-2 pr-3">{s.name}</td>
                      <td className="py-2 pr-3 text-slate-500">{s.student_no}</td>
                      <td className="py-2 pr-3">
                        {s.last_level_name ? <Badge variant="secondary">{s.last_level_name}</Badge> : "—"}
                      </td>
                      <td className="py-2 pr-3">{s.reports}</td>
                      <td className="py-2">{trendText(s.trend_slope)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
