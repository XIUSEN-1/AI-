import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer } from "recharts";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";

interface DimensionDetail {
  dimension: string;
  name: string;
  theta: number;
  level: number;
  level_name: string;
  answered: number;
  correct: number;
  percent: number;
}

interface ReportOut {
  id: number;
  created_at: string;
  dimensions: DimensionDetail[];
  total_level: number;
  total_level_name: string;
  radar: { dimension: string; label: string; value: number }[];
  strengths: string[];
  gaps: string[];
  advice: string[];
}

export default function ReportPage() {
  const { id } = useParams<{ id: string }>();
  const [report, setReport] = useState<ReportOut | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!id) return;
    api<ReportOut>(`/api/reports/${id}`)
      .then(setReport)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "报告加载失败"));
  }, [id]);

  if (error) return <div className="p-8 text-red-600">{error}</div>;
  if (!report) return <div className="p-8">报告加载中…</div>;

  const byDim = Object.fromEntries(report.dimensions.map((d) => [d.dimension, d]));
  const radarData = report.radar.map((r) => ({ subject: r.label, value: r.value }));

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold">你的 AI 能力报告</h1>
        <p className="text-sm text-slate-500">
          {new Date(report.created_at).toLocaleString("zh-CN")} · 总评 <Badge>{report.total_level_name}</Badge>
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">六维能力雷达</CardTitle>
        </CardHeader>
        <CardContent className="h-80">
          <ResponsiveContainer width="100%" height="100%">
            <RadarChart data={radarData}>
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
          <CardTitle className="text-base">维度明细</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {report.dimensions.map((d) => (
            <div key={d.dimension} className="flex items-center justify-between border-b py-2 last:border-0">
              <div>
                <p className="text-sm font-medium">{d.name}</p>
                <p className="text-xs text-slate-500">
                  答对 {d.correct}/{d.answered} · 能力值 {d.theta.toFixed(1)}
                </p>
              </div>
              <Badge variant={d.level >= 3 ? "default" : "secondary"}>{d.level_name}</Badge>
            </div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">学习建议</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <p className="text-xs text-slate-500">
            优势维度：{report.strengths.map((d) => byDim[d]?.name).filter(Boolean).join("、")}
          </p>
          {report.advice.map((a) => (
            <p key={a} className="rounded bg-slate-50 p-3 text-sm leading-relaxed">{a}</p>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
