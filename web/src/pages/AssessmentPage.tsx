import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Line, LineChart, ResponsiveContainer, YAxis } from "recharts";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { api } from "@/lib/api";

interface Option {
  key: string;
  text: string;
}

interface QuestionOut {
  id: number;
  code: string;
  dimension: string;
  dimension_name: string;
  type: "single" | "multi" | "judge";
  difficulty: number;
  stem: string;
  options: Option[] | null;
  est_seconds: number;
  tags: string[];
}

interface DimensionProgress {
  name: string;
  theta: number;
  level: number;
  level_name: string;
  n: number;
  done: boolean;
}

interface SessionView {
  session_id: number;
  status: string;
  question: QuestionOut | null;
  next_dimension: string | null;
  reason: string;
  progress: Record<string, DimensionProgress>;
  just?: { is_correct: boolean; explanation: string | null; dimension: string; theta: number };
}

type AnswerValue = string | string[] | boolean | null;

export default function AssessmentPage() {
  const navigate = useNavigate();
  const [view, setView] = useState<SessionView | null>(null);
  const [answer, setAnswer] = useState<AnswerValue>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [finishing, setFinishing] = useState(false);
  const [traces, setTraces] = useState<Record<string, { t: number; v: number }[]>>({});
  const questionShownAt = useRef<number>(Date.now());
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return; // StrictMode 双挂载保护
    started.current = true;
    api<SessionView>("/api/sessions", { method: "POST", body: JSON.stringify({ mode: "full" }) })
      .then((v) => {
        setView(v);
        questionShownAt.current = Date.now();
        setAnswer(null);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "无法开始测评"));
  }, []);

  function recordTheta(v: SessionView) {
    if (!v.next_dimension) return;
    const dp = v.progress[v.next_dimension];
    setTraces((prev) => ({
      ...prev,
      [v.next_dimension!]: [...(prev[v.next_dimension!] ?? [{ t: 0, v: 3 }]), { t: dp.n, v: dp.theta }],
    }));
  }

  async function submitAnswer() {
    if (!view?.question || answer === null || submitting) return;
    setSubmitting(true);
    setError("");
    const timeSpent = Math.round((Date.now() - questionShownAt.current) / 1000);
    try {
      const next = await api<SessionView>(`/api/sessions/${view.session_id}/answer`, {
        method: "POST",
        body: JSON.stringify({ question_id: view.question.id, answer, time_spent: timeSpent }),
      });
      setAnswer(null);
      setView(next);
      questionShownAt.current = Date.now();
      if (next.question) recordTheta(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败，请重试");
    } finally {
      setSubmitting(false);
    }
  }

  async function finish() {
    if (!view || finishing) return;
    setFinishing(true);
    setError("");
    try {
      const resp = await api<{ report_id: number }>(`/api/sessions/${view.session_id}/finish`, {
        method: "POST",
      });
      navigate(`/report/${resp.report_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成报告失败");
      setFinishing(false);
    }
  }

  if (error && !view) return <div className="p-8 text-red-600">{error}</div>;
  if (!view) return <div className="p-8">正在准备测评…</div>;

  const q = view.question;
  const dimCodes = Object.keys(view.progress);
  const answeredTotal = dimCodes.reduce((sum, d) => sum + view.progress[d].n, 0);
  const doneCount = dimCodes.filter((d) => view.progress[d].done).length;
  const currentDim = view.next_dimension ?? "";
  const trace = traces[currentDim] ?? [];

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      <header className="space-y-2">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-semibold">AI 能力测评</h1>
          <Badge variant="outline">
            {doneCount}/{dimCodes.length} 维度完成
          </Badge>
        </div>
        <Progress value={(answeredTotal / (dimCodes.length * 2)) * 100} />
        <div className="flex flex-wrap gap-2">
          {dimCodes.map((d) => (
            <Badge key={d} variant={view.progress[d].done ? "default" : "secondary"}>
              {d} {view.progress[d].done ? "✓" : `L${view.progress[d].level}`}
            </Badge>
          ))}
        </div>
      </header>

      {view.just && (
        <Card>
          <CardContent className="space-y-1 py-3">
            <p className={view.just.is_correct ? "font-medium text-green-600" : "font-medium text-red-600"}>
              {view.just.is_correct ? "回答正确" : "回答错误"}
            </p>
            {view.just.explanation && <p className="text-sm text-slate-600">{view.just.explanation}</p>}
          </CardContent>
        </Card>
      )}

      {q ? (
        <Card>
          <CardHeader className="space-y-1">
            <div className="flex items-center gap-2 text-sm text-slate-500">
              <Badge variant="secondary">{q.dimension_name}</Badge>
              <span>难度 {"★".repeat(q.difficulty)}</span>
              <span>建议用时 {q.est_seconds}s</span>
            </div>
            <CardTitle className="text-base leading-relaxed">{q.stem}</CardTitle>
            <p className="text-xs text-slate-400">{view.reason}</p>
          </CardHeader>
          <CardContent className="space-y-4">
            {q.type === "single" && (
              <RadioGroup value={(answer as string) ?? ""} onValueChange={(v) => setAnswer(v)}>
                {q.options!.map((o) => (
                  <div key={o.key} className="flex items-center space-x-2">
                    <RadioGroupItem value={o.key} id={o.key} />
                    <Label htmlFor={o.key}>{o.text}</Label>
                  </div>
                ))}
              </RadioGroup>
            )}
            {q.type === "multi" && (
              <div className="space-y-2">
                {q.options!.map((o) => (
                  <div key={o.key} className="flex items-center space-x-2">
                    <Checkbox
                      id={o.key}
                      checked={(answer as string[])?.includes(o.key) ?? false}
                      onCheckedChange={(checked) => {
                        const current = (answer as string[]) ?? [];
                        setAnswer(checked ? [...current, o.key] : current.filter((k) => k !== o.key));
                      }}
                    />
                    <Label htmlFor={o.key}>{o.text}</Label>
                  </div>
                ))}
              </div>
            )}
            {q.type === "judge" && (
              <RadioGroup value={answer === null ? "" : String(answer)} onValueChange={(v) => setAnswer(v === "true")}>
                <div className="flex items-center space-x-2">
                  <RadioGroupItem value="true" id="judge-true" />
                  <Label htmlFor="judge-true">正确</Label>
                </div>
                <div className="flex items-center space-x-2">
                  <RadioGroupItem value="false" id="judge-false" />
                  <Label htmlFor="judge-false">错误</Label>
                </div>
              </RadioGroup>
            )}
            {error && <p className="text-sm text-red-600">{error}</p>}
            <Button onClick={submitAnswer} disabled={answer === null || submitting}>
              {submitting ? "判分中…" : "提交答案"}
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="space-y-3 py-6 text-center">
            <p className="font-medium">六维测评全部完成！</p>
            <p className="text-sm text-slate-500">点击下方按钮生成你的能力雷达报告</p>
            {error && <p className="text-sm text-red-600">{error}</p>}
            <Button onClick={finish} disabled={finishing}>
              {finishing ? "生成中…" : "生成报告"}
            </Button>
          </CardContent>
        </Card>
      )}

      {trace.length >= 2 && (
        <Card>
          <CardHeader className="pb-0">
            <CardTitle className="text-sm">当前维度能力估计轨迹（{view.progress[currentDim]?.name}）</CardTitle>
          </CardHeader>
          <CardContent className="h-28 pt-2">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={trace}>
                <YAxis domain={[1, 5]} hide />
                <Line type="monotone" dataKey="v" stroke="#6366f1" strokeWidth={2} dot />
              </LineChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
