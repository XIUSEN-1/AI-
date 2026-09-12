import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Line, LineChart, ResponsiveContainer, YAxis } from "recharts";

import ChatPanel from "@/components/ChatPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError } from "@/lib/api";
import { pollJudging } from "@/lib/judging";

interface Option {
  key: string;
  text: string;
}

interface QuestionOut {
  id: number;
  code: string;
  dimension: string;
  dimension_name: string;
  type: "single" | "multi" | "judge" | "open" | "practical";
  difficulty: number;
  stem: string;
  options: Option[] | null;
  est_seconds: number;
  tags: string[];
  dialog_turns_taken?: number;
}

interface DimensionProgress {
  name: string;
  theta: number;
  level: number;
  level_name: string;
  n: number;
  done: boolean;
}

type Stage = "objective" | "dialog" | "practical" | "ready" | "finished";

interface SessionView {
  session_id: number;
  status: string;
  stage: Stage;
  question: QuestionOut | null;
  next_dimension: string | null;
  reason: string;
  progress: Record<string, DimensionProgress>;
  just?: { is_correct: boolean; explanation: string | null; dimension: string; theta: number };
}

interface DialogStartOut {
  question: QuestionOut;
  opening: string;
}

interface PracticalTaskOut {
  question: QuestionOut;
  artifact_min: number;
  artifact_max: number;
}

type AnswerValue = string | string[] | boolean | null;

const STAGE_STEPS = ["客观题", "对话式测评", "实操任务", "报告"];
const DIALOG_MAX_TURNS = 3;
const PRACTICAL_MAX_TURNS = 20;
const STAGE_ADVANCED_HINT = "当前阶段不支持对话式测评"; // 对话题全部结束时 dialog/* 的 400 详情

function StageIndicator({ stage }: { stage: Stage }) {
  const current = stage === "objective" ? 0 : stage === "dialog" ? 1 : stage === "practical" ? 2 : 3;
  return (
    <ol className="flex flex-wrap items-center gap-1 text-xs">
      {STAGE_STEPS.map((label, i) => (
        <li key={label} className="flex items-center gap-1">
          {i > 0 && <span className="text-slate-300">→</span>}
          <span
            className={
              i === current ? "font-medium text-indigo-600" : i < current ? "text-slate-400" : "text-slate-300"
            }
          >
            {i < current ? `✓ ${label}` : label}
          </span>
        </li>
      ))}
    </ol>
  );
}

export default function AssessmentPage() {
  const navigate = useNavigate();
  const [view, setView] = useState<SessionView | null>(null);
  const [answer, setAnswer] = useState<AnswerValue>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [finishing, setFinishing] = useState(false);
  const [traces, setTraces] = useState<Record<string, { t: number; v: number }[]>>({});
  const [dialog, setDialog] = useState<DialogStartOut | null>(null); // 当前对话题（含开场白）
  const [dialogBusy, setDialogBusy] = useState(false); // start/切题/结束本题进行中
  const [dialogFullTurns, setDialogFullTurns] = useState(false); // 当前题已满 3 轮，等待进入下一题
  const [dialogStartTick, setDialogStartTick] = useState(0); // 手动重试开场
  const [task, setTask] = useState<PracticalTaskOut | null>(null);
  const [taskLoading, setTaskLoading] = useState(false);
  const [taskTick, setTaskTick] = useState(0); // 手动重试拉取任务
  const [artifact, setArtifact] = useState("");
  const [submittingArtifact, setSubmittingArtifact] = useState(false);
  const [skippingPractical, setSkippingPractical] = useState(false);
  const [judging, setJudging] = useState<{ step: number; total: number } | null>(null); // 判题进度轮询中
  const questionShownAt = useRef<number>(Date.now());
  const started = useRef(false);
  const dialogStartedFor = useRef<number>(0); // 已 dialog/start 的对话题 id（防重复开场）
  const pollStopRef = useRef<(() => void) | null>(null); // 判题轮询定时器清理句柄

  useEffect(() => () => pollStopRef.current?.(), []); // 卸载清轮询定时器

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

  // 进入/切换对话题：先 start 拿考官开场白（turn 前必须先 start）
  useEffect(() => {
    if (!view || view.stage !== "dialog" || !view.question) return;
    if (dialogStartedFor.current === view.question.id || dialogBusy) return;
    dialogStartedFor.current = view.question.id;
    setDialogBusy(true);
    api<DialogStartOut>(`/api/sessions/${view.session_id}/dialog/start`, { method: "POST" })
      .then((d) => {
        setDialog(d);
        setDialogFullTurns(false);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "考官开场失败，请重试"))
      .finally(() => setDialogBusy(false));
  }, [view, dialogBusy, dialogStartTick]);

  // 进入实操阶段：拉取任务说明（题面 + 产物字数要求）
  useEffect(() => {
    if (!view || view.stage !== "practical" || task || taskLoading) return;
    setTaskLoading(true);
    api<PracticalTaskOut>(`/api/sessions/${view.session_id}/practical/task`)
      .then(setTask)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "实操任务加载失败，请重试"))
      .finally(() => setTaskLoading(false));
  }, [view, task, taskLoading, taskTick]);

  function retryDialogStart() {
    if (!view?.question) return;
    dialogStartedFor.current = 0; // 允许 effect 重新发起 start
    setError("");
    setDialogStartTick((n) => n + 1);
  }

  function retryTask() {
    setError("");
    setTaskTick((n) => n + 1);
  }

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
      if (next.stage === "objective" && next.question) recordTheta(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败，请重试");
    } finally {
      setSubmitting(false);
    }
  }

  // 学员主动结束当前对话题（3 轮内）：落结束标记并切下一题/进实操
  async function finishDialogQuestion() {
    if (!view || dialogBusy) return;
    setDialogBusy(true);
    setError("");
    try {
      const next = await api<SessionView>(`/api/sessions/${view.session_id}/dialog/finish-question`, {
        method: "POST",
      });
      setDialog(null);
      setDialogFullTurns(false);
      setView(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败，请重试");
    } finally {
      setDialogBusy(false);
    }
  }

  // 学员主动跳过当前对话题：不判分切下一题/翻阶段（点击即生效，不判分不回灌能力值）
  async function skipDialogQuestion() {
    if (!view || dialogBusy) return;
    setDialogBusy(true);
    setError("");
    try {
      const next = await api<SessionView>(`/api/sessions/${view.session_id}/dialog/skip`, { method: "POST" });
      setDialog(null);
      setDialogFullTurns(false);
      setView(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "跳过失败，请重试");
    } finally {
      setDialogBusy(false);
    }
  }

  // 满 3 轮后题目已在服务端自动关闭：start 下一道；若对话题全部结束则进入实操
  async function nextDialogQuestion() {
    if (!view || dialogBusy) return;
    setDialogBusy(true);
    setError("");
    try {
      const d = await api<DialogStartOut>(`/api/sessions/${view.session_id}/dialog/start`, { method: "POST" });
      dialogStartedFor.current = d.question.id;
      setDialog(d);
      setDialogFullTurns(false);
      setView((v) => (v ? { ...v, question: d.question } : v));
    } catch (err) {
      const message = err instanceof Error ? err.message : "";
      if (!message.includes(STAGE_ADVANCED_HINT)) {
        setError(message || "无法进入下一题，请重试");
        return;
      }
      try {
        const t = await api<PracticalTaskOut>(`/api/sessions/${view.session_id}/practical/task`);
        setTask(t);
        setDialog(null);
        setView((v) => (v ? { ...v, stage: "practical", question: null } : v));
      } catch (err2) {
        setError(err2 instanceof Error ? err2.message : "无法进入实操任务");
      }
    } finally {
      setDialogBusy(false);
    }
  }

  async function submitArtifact() {
    if (!view || !task || submittingArtifact) return;
    setSubmittingArtifact(true);
    setError("");
    try {
      await api(`/api/sessions/${view.session_id}/practical/submit`, {
        method: "POST",
        body: JSON.stringify({ artifact }),
      });
      setView({ ...view, stage: "ready", question: null, reason: "" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "产物提交失败，请重试");
    } finally {
      setSubmittingArtifact(false);
    }
  }

  // 学员主动跳过实操任务：不提交产物直接进入 ready（报告中标注"已跳过"）
  async function skipPractical() {
    if (!view || skippingPractical) return;
    setSkippingPractical(true);
    setError("");
    try {
      await api(`/api/sessions/${view.session_id}/practical/skip`, { method: "POST" });
      setView({ ...view, stage: "ready", question: null, reason: "" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "跳过失败，请重试");
    } finally {
      setSkippingPractical(false);
    }
  }

  // 生成报告三态：202 新受理 → 轮询判题进度；200 幂等 → 直接看报告；409 → 已在判题，进同一轮询
  async function finish() {
    if (!view || finishing || judging) return;
    setFinishing(true);
    setError("");
    try {
      const resp = await api<{ report_id?: number; judging_total?: number }>(
        `/api/sessions/${view.session_id}/finish`,
        { method: "POST" },
      );
      if (resp.report_id != null) {
        navigate(`/report/${resp.report_id}`);
        return;
      }
      startJudgingPoll(resp.judging_total ?? 0);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        startJudgingPoll(0); // 上一请求仍在判题：跟随其进度（首轮轮询即得 x/y）
        return;
      }
      setError(err instanceof Error ? err.message : "生成报告失败，请重试");
      setFinishing(false);
    }
  }

  function startJudgingPoll(total: number) {
    if (!view) return;
    pollStopRef.current?.();
    setJudging({ step: 0, total });
    pollStopRef.current = pollJudging(view.session_id, {
      onProgress: (step, t) => setJudging({ step, total: t }),
      onDone: (reportId) => navigate(`/report/${reportId}`),
      onError: (message) => {
        pollStopRef.current = null;
        setJudging(null);
        setFinishing(false);
        setError(message);
      },
    });
  }

  if (error && !view) return <div className="p-8 text-red-600">{error}</div>;
  if (!view) return <div className="p-8">正在准备测评…</div>;

  const q = view.question;
  const dimCodes = Object.keys(view.progress);
  const answeredTotal = dimCodes.reduce((sum, d) => sum + view.progress[d].n, 0);
  const doneCount = dimCodes.filter((d) => view.progress[d].done).length;
  const currentDim = view.next_dimension ?? "";
  const trace = traces[currentDim] ?? [];
  const artifactLen = artifact.length;
  const artifactValid =
    !!task && artifactLen >= task.artifact_min && artifactLen <= task.artifact_max;

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      <header className="space-y-2">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-semibold">AI 能力测评</h1>
          <Badge variant="outline">
            {doneCount}/{dimCodes.length} 维度完成
          </Badge>
        </div>
        <StageIndicator stage={view.stage} />
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

      {error && <p className="text-sm text-red-600">{error}</p>}

      {view.stage === "objective" && q && (
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
            <Button onClick={submitAnswer} disabled={answer === null || submitting}>
              {submitting ? "判分中…" : "提交答案"}
            </Button>
          </CardContent>
        </Card>
      )}

      {view.stage === "dialog" && q && (
        <Card>
          <CardHeader className="space-y-1">
            <div className="flex items-center gap-2 text-sm text-slate-500">
              <Badge variant="secondary">对话式测评 · {q.dimension_name}</Badge>
              <span>难度 {"★".repeat(q.difficulty)}</span>
            </div>
            <CardTitle className="text-base leading-relaxed">{q.stem}</CardTitle>
            <p className="text-xs text-slate-400">{view.reason}</p>
          </CardHeader>
          <CardContent className="space-y-3">
            {dialog ? (
              <>
                <ChatPanel
                  key={dialog.question.id}
                  endpoint={`/api/sessions/${view.session_id}/dialog/turn`}
                  counterpartLabel="考官"
                  initialMessages={[{ role: "counterpart", content: dialog.opening }]}
                  initialTurns={dialog.question.dialog_turns_taken ?? 0}
                  maxTurns={DIALOG_MAX_TURNS}
                  inputPlaceholder="结合你的实际经验回答考官的问题（可多行输入）"
                  streamingNote="考官追问中…"
                  disabled={dialogFullTurns}
                  disabledHint="本题追问已满 3 轮"
                  onTurnDone={(t) => {
                    if (t >= DIALOG_MAX_TURNS) setDialogFullTurns(true);
                  }}
                />
                {dialogFullTurns ? (
                  <Button onClick={nextDialogQuestion} disabled={dialogBusy}>
                    进入下一题
                  </Button>
                ) : (
                  <div className="flex gap-2">
                    <Button variant="outline" onClick={finishDialogQuestion} disabled={dialogBusy}>
                      结束本题
                    </Button>
                    <Button variant="ghost" onClick={skipDialogQuestion} disabled={dialogBusy}>
                      跳过本题
                    </Button>
                  </div>
                )}
              </>
            ) : dialogBusy ? (
              <p className="py-6 text-center text-sm text-slate-500">考官准备中…</p>
            ) : (
              <div className="space-y-2 py-6 text-center">
                <p className="text-sm text-slate-500">考官开场未成功</p>
                <Button variant="outline" onClick={retryDialogStart}>
                  重试
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {view.stage === "practical" &&
        (task ? (
          <>
            <Card>
              <CardHeader className="space-y-1">
                <div className="flex items-center gap-2 text-sm text-slate-500">
                  <Badge variant="secondary">实操任务 · {task.question.dimension_name}</Badge>
                  <span>难度 {"★".repeat(task.question.difficulty)}</span>
                </div>
                <CardTitle className="text-base leading-relaxed">{task.question.stem}</CardTitle>
                <p className="text-xs text-slate-400">
                  在下方协作窗与 AI 真实协作完成任务，完成后把最终产物（{task.artifact_min}~{task.artifact_max} 字）粘贴提交。
                </p>
              </CardHeader>
              <CardContent>
                <ChatPanel
                  endpoint={`/api/sessions/${view.session_id}/practical/chat`}
                  counterpartLabel="AI 助手"
                  initialMessages={[]}
                  initialTurns={0}
                  maxTurns={PRACTICAL_MAX_TURNS}
                  inputPlaceholder="向 AI 描述需求、追问方案、迭代修正（可多行输入）"
                  streamingNote="AI 回复中…"
                  disabledHint="协作窗已达 20 轮上限，请整理最终产物并提交"
                />
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">提交最终产物</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <Textarea
                  rows={8}
                  maxLength={task.artifact_max}
                  value={artifact}
                  placeholder="把与 AI 协作完成的最终成果整理到这里…"
                  onChange={(e) => setArtifact(e.target.value)}
                  disabled={submittingArtifact}
                />
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className={`text-xs ${artifactValid || artifactLen === 0 ? "text-slate-500" : "text-red-600"}`}>
                    已输入 {artifactLen} 字（要求 {task.artifact_min}~{task.artifact_max} 字）
                  </span>
                  <div className="flex gap-2">
                    <Button
                      variant="ghost"
                      onClick={skipPractical}
                      disabled={skippingPractical || submittingArtifact}
                    >
                      {skippingPractical ? "跳过中…" : "跳过实操任务"}
                    </Button>
                    <Button onClick={submitArtifact} disabled={!artifactValid || submittingArtifact}>
                      {submittingArtifact ? "提交中…" : "提交产物"}
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          </>
        ) : taskLoading ? (
          <Card>
            <CardContent className="py-6 text-center text-sm text-slate-500">实操任务加载中…</CardContent>
          </Card>
        ) : (
          <Card>
            <CardContent className="space-y-2 py-6 text-center">
              <p className="text-sm text-slate-500">实操任务加载失败</p>
              <Button variant="outline" onClick={retryTask}>
                重试
              </Button>
            </CardContent>
          </Card>
        ))}

      {(view.stage === "ready" || view.stage === "finished" || (view.stage === "objective" && !q)) && (
        <Card>
          <CardContent className="space-y-3 py-6 text-center">
            <p className="font-medium">全部测评完成！</p>
            {judging ? (
              <>
                <p className="text-sm text-slate-500">
                  {judging.total > 0 ? `智能判题中 ${judging.step}/${judging.total}…` : "智能判题中…"}
                </p>
                <Progress value={judging.total > 0 ? (judging.step / judging.total) * 100 : 0} />
              </>
            ) : (
              <>
                <p className="text-sm text-slate-500">点击下方按钮生成你的能力雷达报告</p>
                <Button onClick={finish} disabled={finishing}>
                  {finishing ? "生成中…" : "生成报告"}
                </Button>
              </>
            )}
          </CardContent>
        </Card>
      )}

      {view.stage === "objective" && trace.length >= 2 && (
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
