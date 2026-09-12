import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

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

interface PracticeQuestion {
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

interface WrongItem {
  question_id: number;
  question_code: string;
  dimension: string;
  dimension_name: string;
  tags: string[];
  wrong_count: number;
}

interface PracticeSessionOut {
  id: number;
  dimension: string | null;
  tags: string[];
  size: number;
  question_ids: number[];
  questions: PracticeQuestion[];
}

interface PracticeFeedback {
  question_id: number;
  is_correct: boolean;
  explanation: string | null;
  answered: number;
  total: number;
}

type AnswerValue = string | string[] | boolean | null;

const AUTO_DIM = "auto"; // RadioGroup 哨兵值：不指定维度，由服务端按错题量取薄弱维度

/** 可选维度与考点标签：错题本按维度聚合（去重题数降序）；URL ?dimension=Dx 不在错题维度中时补入。 */
function dimOptions(items: WrongItem[] | null, paramDim: string | null) {
  if (items === null) return [] as { dimension: string; name: string; wrong: number; tags: string[] }[];
  const byDim = new Map<string, { dimension: string; name: string; wrong: number; tags: Set<string> }>();
  for (const w of items) {
    const d = byDim.get(w.dimension) ?? { dimension: w.dimension, name: w.dimension_name, wrong: 0, tags: new Set<string>() };
    d.wrong += 1; // 每条记录即一道去重题目（与后端口径一致），维度内计数 = 题数
    for (const t of w.tags) d.tags.add(t);
    byDim.set(w.dimension, d);
  }
  if (paramDim && !byDim.has(paramDim)) {
    byDim.set(paramDim, { dimension: paramDim, name: paramDim, wrong: 0, tags: new Set<string>() });
  }
  return [...byDim.values()]
    .map((d) => ({ dimension: d.dimension, name: d.name, wrong: d.wrong, tags: [...d.tags] }))
    .sort((a, b) => b.wrong - a.wrong || a.dimension.localeCompare(b.dimension));
}

export default function PracticePage() {
  const [params] = useSearchParams();
  const paramDim = params.get("dimension");
  const [wrongItems, setWrongItems] = useState<WrongItem[] | null>(null);
  const [wrongError, setWrongError] = useState("");
  const [reloadTick, setReloadTick] = useState(0);
  const [dimPicked, setDimPicked] = useState<string>(paramDim ?? AUTO_DIM);
  const [tags, setTags] = useState<string[]>([]);
  const [size, setSize] = useState(5);
  const [session, setSession] = useState<PracticeSessionOut | null>(null);
  const [index, setIndex] = useState(0);
  const [answer, setAnswer] = useState<AnswerValue>(null);
  const [feedback, setFeedback] = useState<PracticeFeedback | null>(null);
  const [correctCount, setCorrectCount] = useState(0);
  const [done, setDone] = useState<{ correct: number; total: number } | null>(null);
  const [starting, setStarting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api<{ items: WrongItem[]; total: number }>("/api/me/wrongbook")
      .then((w) => setWrongItems(w.items))
      .catch((e: unknown) => setWrongError(e instanceof Error ? e.message : "错题本加载失败"));
  }, [reloadTick]);

  const options = dimOptions(wrongItems, paramDim);
  const dimension = dimPicked === AUTO_DIM ? null : dimPicked;
  // 当前选择下的可选考点标签：指定维度取该维度错题标签，自动取全部
  const tagChoices =
    dimension === null
      ? [...new Set(options.flatMap((d) => d.tags))]
      : (options.find((d) => d.dimension === dimension)?.tags ?? []);
  const q = session?.questions[index] ?? null;

  async function startPractice() {
    if (starting) return;
    setStarting(true);
    setError("");
    try {
      const s = await api<PracticeSessionOut>("/api/practice/sessions", {
        method: "POST",
        body: JSON.stringify({ dimension, tags, size }),
      });
      setSession(s);
      setIndex(0);
      setAnswer(null);
      setFeedback(null);
      setCorrectCount(0);
      setDone(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "练习卷生成失败，请重试");
    } finally {
      setStarting(false);
    }
  }

  async function submitAnswer() {
    if (!session || answer === null || submitting || feedback) return;
    setSubmitting(true);
    setError("");
    try {
      const f = await api<PracticeFeedback>(`/api/practice/sessions/${session.id}/answer`, {
        method: "POST",
        body: JSON.stringify({ question_id: session.questions[index].id, answer }),
      });
      setFeedback(f);
      if (f.is_correct) setCorrectCount((c) => c + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败，请重试");
    } finally {
      setSubmitting(false);
    }
  }

  function nextQuestion() {
    if (!session) return;
    setAnswer(null);
    setFeedback(null);
    if (index + 1 >= session.questions.length) {
      setDone({ correct: correctCount, total: session.questions.length });
      setSession(null);
      return;
    }
    setIndex(index + 1);
  }

  return (
    <div className="mx-auto max-w-2xl space-y-4 p-4">
      <header className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">错题练习</h1>
        <Link to="/" className="text-sm text-slate-500 hover:text-slate-700">返回首页</Link>
      </header>
      {error && <p className="text-sm text-red-600">{error}</p>}

      {done ? (
        <Card>
          <CardContent className="space-y-3 py-6 text-center">
            <p className="font-medium">练习完成！</p>
            <p className="text-sm text-slate-500">
              共 {done.total} 题 · 答对 {done.correct}/{done.total}
            </p>
            <div className="flex justify-center gap-2">
              <Button variant="outline" onClick={() => setDone(null)}>再来一卷</Button>
              <Button asChild>
                <Link to="/">返回首页</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : session && q ? (
        <div className="space-y-4">
          <Progress value={((index + (feedback ? 1 : 0)) / session.questions.length) * 100} />
          <p className="text-xs text-slate-500">
            第 {index + 1}/{session.questions.length} 题 · 已答对 {correctCount}
          </p>
          <Card>
            <CardHeader className="space-y-1">
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Badge variant="secondary">{q.dimension_name}</Badge>
                <span>难度 {"★".repeat(q.difficulty)}</span>
                {q.tags.length > 0 && <span>{q.tags.join(" / ")}</span>}
              </div>
              <CardTitle className="text-base leading-relaxed">{q.stem}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {q.type === "single" && (
                <RadioGroup value={(answer as string) ?? ""} onValueChange={(v) => !feedback && setAnswer(v)}>
                  {q.options!.map((o) => (
                    <div key={o.key} className="flex items-center space-x-2">
                      <RadioGroupItem value={o.key} id={o.key} disabled={!!feedback} />
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
                        disabled={!!feedback}
                        checked={(answer as string[])?.includes(o.key) ?? false}
                        onCheckedChange={(checked) => {
                          if (feedback) return;
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
                <RadioGroup
                  value={answer === null ? "" : String(answer)}
                  onValueChange={(v) => !feedback && setAnswer(v === "true")}
                >
                  <div className="flex items-center space-x-2">
                    <RadioGroupItem value="true" id="judge-true" disabled={!!feedback} />
                    <Label htmlFor="judge-true">正确</Label>
                  </div>
                  <div className="flex items-center space-x-2">
                    <RadioGroupItem value="false" id="judge-false" disabled={!!feedback} />
                    <Label htmlFor="judge-false">错误</Label>
                  </div>
                </RadioGroup>
              )}
              {!feedback && (
                <Button onClick={submitAnswer} disabled={answer === null || submitting}>
                  {submitting ? "判分中…" : "提交答案"}
                </Button>
              )}
            </CardContent>
          </Card>
          {feedback && (
            <Card>
              <CardContent className="space-y-2 py-3">
                <p className={feedback.is_correct ? "font-medium text-green-600" : "font-medium text-red-600"}>
                  {feedback.is_correct ? "回答正确" : "回答错误"}
                </p>
                {feedback.explanation && (
                  <p className="text-sm leading-relaxed text-slate-600">{feedback.explanation}</p>
                )}
                <Button onClick={nextQuestion}>
                  {index + 1 >= session.questions.length ? "查看总结" : "下一题"}
                </Button>
              </CardContent>
            </Card>
          )}
        </div>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">生成练习卷</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {wrongError ? (
              <div className="space-y-2 py-4 text-center">
                <p className="text-sm text-red-600">{wrongError}</p>
                <Button
                  variant="outline"
                  onClick={() => {
                    setWrongItems(null); // 重试回到加载态，随 reloadTick 重发请求
                    setWrongError("");
                    setReloadTick((n) => n + 1);
                  }}
                >
                  重试
                </Button>
              </div>
            ) : wrongItems === null ? (
              <p className="py-4 text-center text-sm text-slate-500">错题本加载中…</p>
            ) : options.length === 0 ? (
              <div className="space-y-2 py-4 text-center">
                <p className="text-sm text-slate-500">
                  暂无错题。完成一次正式测评后，这里会按你的错题考点生成针对性练习。
                </p>
                <Button asChild>
                  <Link to="/assess">去完成一次测评</Link>
                </Button>
              </div>
            ) : (
              <>
                <div className="space-y-2">
                  <p className="text-sm font-medium">练习维度</p>
                  <RadioGroup
                    value={dimPicked}
                    onValueChange={(v) => {
                      setDimPicked(v);
                      setTags([]); // 维度切换后标签归属变化，清空已选标签
                    }}
                  >
                    {wrongItems!.length > 0 && (
                      <div className="flex items-center space-x-2">
                        <RadioGroupItem value={AUTO_DIM} id="dim-auto" />
                        <Label htmlFor="dim-auto">自动（按错题量选薄弱维度）</Label>
                      </div>
                    )}
                    {options.map((d) => (
                      <div key={d.dimension} className="flex items-center space-x-2">
                        <RadioGroupItem value={d.dimension} id={`dim-${d.dimension}`} />
                        <Label htmlFor={`dim-${d.dimension}`}>
                          {d.name}
                          {d.wrong > 0 ? ` · 错题 ${d.wrong} 题` : ""}
                        </Label>
                      </div>
                    ))}
                  </RadioGroup>
                </div>
                {tagChoices.length > 0 && (
                  <div className="space-y-2">
                    <p className="text-sm font-medium">考点标签（可选，不选则覆盖该维度全部错题考点）</p>
                    <div className="flex flex-wrap gap-1.5">
                      {tagChoices.map((t) => (
                        <Badge
                          key={t}
                          variant={tags.includes(t) ? "default" : "secondary"}
                          className="cursor-pointer"
                          onClick={() =>
                            setTags((prev) => (prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t]))
                          }
                        >
                          {t}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}
                <div className="space-y-2">
                  <p className="text-sm font-medium">题量</p>
                  <div className="flex gap-2">
                    {[5, 10].map((n) => (
                      <Button key={n} variant={size === n ? "default" : "outline"} size="sm" onClick={() => setSize(n)}>
                        {n} 题
                      </Button>
                    ))}
                  </div>
                </div>
                <Button onClick={startPractice} disabled={starting}>
                  {starting ? "生成中…" : "开始练习"}
                </Button>
                <p className="text-xs text-slate-400">
                  练习从你未作答过的题目中抽卷，即时判分并给出解析；练习成绩不计入能力值与正式报告。
                </p>
              </>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
