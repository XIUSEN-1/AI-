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

interface AnswerItem {
  seq: number;
  dimension: string;
  type: string;
  stem_head: string;
  is_correct: boolean | null;
  score: number | null;
  explanation: string | null;
  theta_after: number;
  rationale?: string | null; // 开放题/实操题：判题理由（旧报告可能没有）
  process_score?: number | null; // 实操题：过程分
  artifact_score?: number | null; // 实操题：产物分
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
  advice_source: "llm" | "template";
  answers: AnswerItem[];
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
  const answerGroups = report.answers.reduce<Record<string, AnswerItem[]>>((acc, a) => {
    (acc[a.dimension] ??= []).push(a);
    return acc;
  }, {});

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
          <CardTitle className="flex items-center gap-2 text-base">
            学习建议
            <Badge variant={report.advice_source === "llm" ? "default" : "secondary"}>
              {report.advice_source === "llm" ? "AI 生成" : "基础模板"}
            </Badge>
          </CardTitle>
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

      {report.answers.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">逐题回显</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {Object.entries(answerGroups).map(([dim, items]) => {
              const objective = items.filter((a) => a.is_correct !== null);
              const open = items.filter((a) => a.is_correct === null); // 开放题/实操题按得分制
              const countParts: string[] = [];
              if (objective.length > 0) {
                countParts.push(`客观答对 ${objective.filter((a) => a.is_correct).length}/${objective.length}`);
              }
              if (open.length > 0) {
                countParts.push(`开放题得分 ${open.reduce((s, a) => s + (a.score ?? 0), 0).toFixed(2)}/${open.length * 4}`);
              }
              return (
                <details key={dim} className="rounded-lg border" open={report.gaps.includes(dim)}>
                  <summary className="cursor-pointer select-none px-3 py-2 text-sm font-medium">
                    {byDim[dim]?.name ?? dim}
                    <span className="ml-2 text-xs font-normal text-slate-500">
                      {countParts.join(" · ") || `${items.length} 题`}
                    </span>
                  </summary>
                  <div className="space-y-2 border-t px-3 py-2">
                    {items.map((a) => {
                      const skipped = a.rationale === "学员跳过";
                      return (
                        <div key={`${a.dimension}-${a.seq}`} className="rounded bg-slate-50 p-3">
                          <div className="flex items-start justify-between gap-2">
                            <p className="text-sm">第 {a.seq} 题 · {a.stem_head}</p>
                            {skipped ? (
                              <Badge variant="secondary">已跳过</Badge>
                            ) : a.is_correct === null ? (
                              <Badge variant="secondary">得分 {(a.score ?? 0).toFixed(2)}/4</Badge>
                            ) : (
                              <Badge variant={a.is_correct ? "default" : "destructive"}>
                                {a.is_correct ? "答对" : "答错"}
                              </Badge>
                            )}
                          </div>
                          {a.type === "practical" && (a.process_score != null || a.artifact_score != null) && (
                            <p className="mt-1 text-xs text-slate-500">
                              过程分 {(a.process_score ?? 0).toFixed(2)} · 产物分 {(a.artifact_score ?? 0).toFixed(2)}
                            </p>
                          )}
                          {a.type === "open" || a.type === "practical" ? (
                            a.rationale && !skipped && (
                              <p className="mt-1 text-xs leading-relaxed text-slate-500">判题理由：{a.rationale}</p>
                            )
                          ) : (
                            a.explanation && (
                              <p className="mt-1 text-xs leading-relaxed text-slate-500">解析：{a.explanation}</p>
                            )
                          )}
                          <p className="mt-1 text-xs text-slate-400">作答后能力值 {a.theta_after.toFixed(3)}</p>
                        </div>
                      );
                    })}
                  </div>
                </details>
              );
            })}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
