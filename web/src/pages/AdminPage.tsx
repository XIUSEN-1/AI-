import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";

type QType = "single" | "multi" | "judge" | "open" | "practical";

interface QuestionOut {
  id: number;
  code: string;
  dimension: string;
  tier: string;
  type: QType;
  difficulty: number;
  stem: string;
  options: { key: string; text: string }[] | null;
  answer: unknown;
  tags: string[];
  est_seconds: number;
  explanation: string | null;
  rubric: object | null;
  status: string;
  version: number;
}

interface QuestionListOut {
  total: number;
  page: number;
  page_size: number;
  items: QuestionOut[];
}

interface ReviewItem {
  id: number;
  question_code: string;
  question_stem: string | null;
  dimension: string | null;
  student_no: string | null;
  session_id: number;
  answer_id: number;
  answer: string | null;
  judge_raw: object | null;
  reason: string | null;
  status: string;
  resolved_score: number | null;
  created_at: string;
}

const PAGE_SIZE = 20;
const DIMENSIONS = ["D1", "D2", "D3", "D4", "D5", "D6"];
const TIERS: { value: string; label: string }[] = [
  { value: "basic", label: "基础" },
  { value: "advanced", label: "进阶" },
];
const TYPES: { value: QType; label: string }[] = [
  { value: "single", label: "单选" },
  { value: "multi", label: "多选" },
  { value: "judge", label: "判断" },
  { value: "open", label: "开放" },
  { value: "practical", label: "实操" },
];
const STATUS_LABELS: Record<string, string> = { published: "已发布", retired: "已下架", draft: "草稿" };
const TYPE_LABELS: Record<QType, string> = { single: "单选", multi: "多选", judge: "判断", open: "开放", practical: "实操" };

// ---------- 题目表单（校验与后端 validate_question 对齐）----------

interface OptionRow {
  key: string;
  text: string;
}

interface QuestionForm {
  code: string;
  dimension: string;
  tier: string;
  type: QType;
  difficulty: number;
  stem: string;
  options: OptionRow[];
  answerSingle: string;
  answerMulti: string[];
  answerJudge: "true" | "false";
  rubricText: string;
  tagsText: string;
  estSeconds: number;
  explanation: string;
}

function emptyForm(): QuestionForm {
  return {
    code: "",
    dimension: "D1",
    tier: "basic",
    type: "single",
    difficulty: 2,
    stem: "",
    options: [
      { key: "A", text: "" },
      { key: "B", text: "" },
    ],
    answerSingle: "A",
    answerMulti: [],
    answerJudge: "true",
    rubricText: '{\n  "points": [""],\n  "anchors": { "0": "", "1": "", "2": "", "3": "", "4": "" }\n}',
    tagsText: "",
    estSeconds: 60,
    explanation: "",
  };
}

function formFromQuestion(q: QuestionOut): QuestionForm {
  const options = q.options ?? [];
  const answer = q.answer;
  return {
    code: q.code,
    dimension: q.dimension,
    tier: q.tier,
    type: q.type,
    difficulty: q.difficulty,
    stem: q.stem,
    options: options.length > 0 ? options.map((o) => ({ ...o })) : emptyForm().options,
    answerSingle: typeof answer === "string" ? answer : (options[0]?.key ?? "A"),
    answerMulti: Array.isArray(answer) ? (answer as string[]) : [],
    answerJudge: answer === true ? "true" : "false",
    rubricText: q.rubric ? JSON.stringify(q.rubric, null, 2) : emptyForm().rubricText,
    tagsText: q.tags.join("，"),
    estSeconds: q.est_seconds,
    explanation: q.explanation ?? "",
  };
}

/** 校验并组装种子格式题目（与后端 validate_question 同规则，提前拦截给出行内中文报错） */
function buildPayload(form: QuestionForm): { payload: Record<string, unknown> } | { error: string } {
  const id = form.code.trim() || "<无 code>";
  if (!DIMENSIONS.includes(form.dimension)) return { error: `${id}: dimension 非法` };
  if (form.tier !== "basic" && form.tier !== "advanced") return { error: `${id}: tier 非法` };
  const difficulty = Number(form.difficulty);
  if (!Number.isInteger(difficulty) || difficulty < 1 || difficulty > 5) {
    return { error: `${id}: difficulty 必须为 1..5` };
  }
  if (form.tier === "basic" && difficulty > 3) return { error: `${id}: 基础题难度不得超过 3` };
  if (form.tier === "advanced" && difficulty < 4) return { error: `${id}: 进阶题难度不得低于 4` };
  if (!form.stem.trim()) return { error: `${id}: stem 不能为空` };
  const est = Number(form.estSeconds);
  if (!Number.isInteger(est) || est < 15 || est > 600) return { error: `${id}: est_seconds 必须为 15..600` };

  const tags = form.tagsText.split(/[，,]/).map((t) => t.trim()).filter(Boolean);
  const explanation = form.explanation.trim();

  if (form.type === "single" || form.type === "multi") {
    const options = form.options;
    if (options.length < 2) return { error: `${id}: 客观选择题 options 至少 2 项` };
    const keys = new Set(options.map((o) => o.key.trim()));
    if (keys.size !== options.length || options.some((o) => !o.key.trim() || !o.text.trim())) {
      return { error: `${id}: options 的 key/text 不合规` };
    }
    const cleanOptions = options.map((o) => ({ key: o.key.trim(), text: o.text.trim() }));
    if (form.type === "single") {
      if (!keys.has(form.answerSingle)) return { error: `${id}: 单选 answer 必须是选项 key` };
      return {
        payload: { dimension: form.dimension, tier: form.tier, type: form.type, difficulty, stem: form.stem.trim(), options: cleanOptions, answer: form.answerSingle, tags, est_seconds: est, explanation: explanation || null, rubric: null },
      };
    }
    if (form.answerMulti.length === 0 || !form.answerMulti.every((k) => keys.has(k))) {
      return { error: `${id}: 多选 answer 必须是选项 key 的非空子集` };
    }
    return {
      payload: { dimension: form.dimension, tier: form.tier, type: form.type, difficulty, stem: form.stem.trim(), options: cleanOptions, answer: [...form.answerMulti], tags, est_seconds: est, explanation: explanation || null, rubric: null },
    };
  }
  if (form.type === "judge") {
    return {
      payload: { dimension: form.dimension, tier: form.tier, type: form.type, difficulty, stem: form.stem.trim(), options: null, answer: form.answerJudge === "true", tags, est_seconds: est, explanation: explanation || null, rubric: null },
    };
  }
  // open | practical：answer 置空，rubric 必须含非空 points 与 0-4 五档 anchors
  let rubric: unknown;
  try {
    rubric = JSON.parse(form.rubricText);
  } catch {
    return { error: `${id}: rubric 不是合法 JSON` };
  }
  const r = rubric as { points?: unknown; anchors?: unknown };
  if (typeof rubric !== "object" || rubric === null || !Array.isArray(r.points) || r.points.length === 0) {
    return { error: `${id}: rubric.points 不能为空` };
  }
  if (typeof r.anchors !== "object" || r.anchors === null || Object.keys(r.anchors).sort().join("") !== "01234") {
    return { error: `${id}: rubric.anchors 必须含 0-4 五档锚定` };
  }
  return {
    payload: { dimension: form.dimension, tier: form.tier, type: form.type, difficulty, stem: form.stem.trim(), options: null, answer: null, tags, est_seconds: est, explanation: explanation || null, rubric },
  };
}

// ---------- 子组件 ----------

function QuestionDrawer(props: {
  editing: QuestionOut | null; // null = 新建
  onClose: () => void;
  onSaved: () => void;
}) {
  const { editing, onClose, onSaved } = props;
  const [form, setForm] = useState<QuestionForm>(editing ? formFromQuestion(editing) : emptyForm());
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");

  function set<K extends keyof QuestionForm>(key: K, value: QuestionForm[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function save() {
    if (saving) return;
    setFormError("");
    if (!editing && !form.code.trim()) {
      setFormError("编号不能为空"); // code 是题库唯一标识，前置拦截避免后端 400 才发现
      return;
    }
    const built = buildPayload(form);
    if ("error" in built) {
      setFormError(built.error);
      return;
    }
    setSaving(true);
    try {
      if (editing) {
        await api<QuestionOut>(`/api/admin/questions/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(built.payload),
        });
      } else {
        await api<QuestionOut>("/api/admin/questions", {
          method: "POST",
          body: JSON.stringify({ ...built.payload, id: form.code.trim() }),
        });
      }
      onSaved();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "保存失败，请重试");
    } finally {
      setSaving(false);
    }
  }

  const isObjective = form.type === "single" || form.type === "multi";

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={onClose}>
      <div
        className="h-full w-full max-w-xl space-y-3 overflow-y-auto bg-white p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold">{editing ? `编辑题目 ${editing.code}` : "新建题目"}</h2>
          <Button variant="ghost" size="sm" onClick={onClose}>关闭</Button>
        </div>
        {formError && <p className="text-sm text-red-600">{formError}</p>}

        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1">
            <Label htmlFor="q-code">题目 code{editing ? "（不可改）" : ""}</Label>
            <Input
              id="q-code"
              value={form.code}
              disabled={!!editing}
              placeholder="如 D1-B01"
              onChange={(e) => set("code", e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="q-dimension">维度</Label>
            <select
              id="q-dimension"
              className="w-full rounded-md border bg-white px-3 py-2 text-sm"
              value={form.dimension}
              onChange={(e) => set("dimension", e.target.value)}
            >
              {DIMENSIONS.map((d) => (
                <option key={d} value={d}>{d}</option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="q-tier">层级</Label>
            <select
              id="q-tier"
              className="w-full rounded-md border bg-white px-3 py-2 text-sm"
              value={form.tier}
              onChange={(e) => set("tier", e.target.value)}
            >
              {TIERS.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="q-type">题型</Label>
            <select
              id="q-type"
              className="w-full rounded-md border bg-white px-3 py-2 text-sm"
              value={form.type}
              onChange={(e) => set("type", e.target.value as QType)}
            >
              {TYPES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="q-difficulty">难度 1-5{form.tier === "basic" ? "（基础≤3）" : "（进阶≥4）"}</Label>
            <Input
              id="q-difficulty"
              type="number"
              min={1}
              max={5}
              value={form.difficulty}
              onChange={(e) => set("difficulty", Number(e.target.value))}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="q-est">建议用时（秒，15-600）</Label>
            <Input
              id="q-est"
              type="number"
              min={15}
              max={600}
              value={form.estSeconds}
              onChange={(e) => set("estSeconds", Number(e.target.value))}
            />
          </div>
        </div>

        <div className="space-y-1">
          <Label htmlFor="q-stem">题干</Label>
          <Textarea id="q-stem" rows={3} value={form.stem} onChange={(e) => set("stem", e.target.value)} />
        </div>

        {isObjective && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>选项（key 唯一、text 非空）</Label>
              <Button
                variant="outline"
                size="sm"
                onClick={() => set("options", [...form.options, { key: "", text: "" }])}
              >
                添加选项
              </Button>
            </div>
            {form.options.map((o, i) => (
              <div key={i} className="flex items-center gap-2">
                <Input
                  className="w-16"
                  value={o.key}
                  placeholder="key"
                  onChange={(e) =>
                    set("options", form.options.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))
                  }
                />
                <Input
                  value={o.text}
                  placeholder="选项内容"
                  onChange={(e) =>
                    set("options", form.options.map((x, j) => (j === i ? { ...x, text: e.target.value } : x)))
                  }
                />
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => set("options", form.options.filter((_, j) => j !== i))}
                >
                  删除
                </Button>
              </div>
            ))}
          </div>
        )}

        {form.type === "single" && (
          <div className="space-y-1">
            <Label htmlFor="q-answer">正确答案（选项 key）</Label>
            <select
              id="q-answer"
              className="w-full rounded-md border bg-white px-3 py-2 text-sm"
              value={form.answerSingle}
              onChange={(e) => set("answerSingle", e.target.value)}
            >
              {form.options.map((o) => (
                <option key={o.key} value={o.key}>{o.key}</option>
              ))}
            </select>
          </div>
        )}
        {form.type === "multi" && (
          <div className="space-y-1">
            <Label>正确答案（多选 key）</Label>
            <div className="flex flex-wrap gap-2">
              {form.options.map((o) => (
                <label key={o.key} className="flex items-center gap-1 text-sm">
                  <input
                    type="checkbox"
                    checked={form.answerMulti.includes(o.key)}
                    onChange={(e) =>
                      set(
                        "answerMulti",
                        e.target.checked
                          ? [...form.answerMulti, o.key]
                          : form.answerMulti.filter((k) => k !== o.key),
                      )
                    }
                  />
                  {o.key || "(空 key)"}
                </label>
              ))}
            </div>
          </div>
        )}
        {form.type === "judge" && (
          <div className="space-y-1">
            <Label htmlFor="q-answer-judge">正确答案</Label>
            <select
              id="q-answer-judge"
              className="w-full rounded-md border bg-white px-3 py-2 text-sm"
              value={form.answerJudge}
              onChange={(e) => set("answerJudge", e.target.value as "true" | "false")}
            >
              <option value="true">正确</option>
              <option value="false">错误</option>
            </select>
          </div>
        )}
        {(form.type === "open" || form.type === "practical") && (
          <div className="space-y-1">
            <Label htmlFor="q-rubric">评分 rubric（JSON：points 非空 + anchors 0-4 锚定）</Label>
            <Textarea id="q-rubric" rows={8} value={form.rubricText} onChange={(e) => set("rubricText", e.target.value)} />
          </div>
        )}

        <div className="space-y-1">
          <Label htmlFor="q-tags">考点标签（逗号分隔）</Label>
          <Input id="q-tags" value={form.tagsText} onChange={(e) => set("tagsText", e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="q-explanation">解析（客观题作答后展示，选填）</Label>
          <Textarea id="q-explanation" rows={2} value={form.explanation} onChange={(e) => set("explanation", e.target.value)} />
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button onClick={save} disabled={saving}>
            {saving ? "保存中…" : "保存"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function QuestionsTab() {
  const [filters, setFilters] = useState<{ dimension: string; tier: string; difficulty: string; status: string }>({
    dimension: "",
    tier: "",
    difficulty: "",
    status: "",
  });
  const [page, setPage] = useState(1);
  const [reloadTick, setReloadTick] = useState(0); // 编辑/新建/下架后重拉列表
  const [list, setList] = useState<QuestionListOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<QuestionOut | null>(null); // null 隐藏；新建用特殊标记
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    setLoading(true);
    const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
    for (const [key, value] of Object.entries(filters)) {
      if (value) params.set(key, value);
    }
    api<QuestionListOut>(`/api/admin/questions?${params.toString()}`)
      .then(setList)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "题库加载失败"))
      .finally(() => setLoading(false));
  }, [filters, page, reloadTick]);

  function updateFilter(key: keyof typeof filters, value: string) {
    setFilters((prev) => ({ ...prev, [key]: value }));
    setPage(1);
  }

  async function retire(q: QuestionOut) {
    if (!window.confirm(`确认下架题目 ${q.code}？下架后不再进入新测评（软删，可筛选查看）。`)) return;
    setError("");
    try {
      await api(`/api/admin/questions/${q.id}`, { method: "DELETE" });
      setReloadTick((n) => n + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "下架失败，请重试");
    }
  }

  const totalPages = list ? Math.max(1, Math.ceil(list.total / list.page_size)) : 1;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <div className="space-y-1">
          <Label htmlFor="f-dimension">维度</Label>
          <select
            id="f-dimension"
            className="rounded-md border bg-white px-2 py-1.5 text-sm"
            value={filters.dimension}
            onChange={(e) => updateFilter("dimension", e.target.value)}
          >
            <option value="">全部</option>
            {DIMENSIONS.map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="f-tier">层级</Label>
          <select
            id="f-tier"
            className="rounded-md border bg-white px-2 py-1.5 text-sm"
            value={filters.tier}
            onChange={(e) => updateFilter("tier", e.target.value)}
          >
            <option value="">全部</option>
            {TIERS.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="f-difficulty">难度</Label>
          <select
            id="f-difficulty"
            className="rounded-md border bg-white px-2 py-1.5 text-sm"
            value={filters.difficulty}
            onChange={(e) => updateFilter("difficulty", e.target.value)}
          >
            <option value="">全部</option>
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="f-status">状态</Label>
          <select
            id="f-status"
            className="rounded-md border bg-white px-2 py-1.5 text-sm"
            value={filters.status}
            onChange={(e) => updateFilter("status", e.target.value)}
          >
            <option value="">全部</option>
            <option value="published">已发布</option>
            <option value="retired">已下架</option>
          </select>
        </div>
        <Button
          onClick={() => {
            setEditing(null);
            setCreating(true);
          }}
        >
          新建题目
        </Button>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}
      {loading && !list ? (
        <p className="text-sm text-slate-500">题库加载中…</p>
      ) : list ? (
        <>
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-slate-50 text-left text-xs text-slate-500">
                  <th className="p-2 font-medium">code</th>
                  <th className="p-2 font-medium">维度</th>
                  <th className="p-2 font-medium">层级</th>
                  <th className="p-2 font-medium">题型</th>
                  <th className="p-2 font-medium">难度</th>
                  <th className="p-2 font-medium">题干</th>
                  <th className="p-2 font-medium">状态</th>
                  <th className="p-2 font-medium">v</th>
                  <th className="p-2 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {list.items.length === 0 && (
                  <tr>
                    <td colSpan={9} className="p-3 text-center text-slate-500">没有符合条件的题目</td>
                  </tr>
                )}
                {list.items.map((q) => (
                  <tr key={q.id} className="border-b last:border-0">
                    <td className="p-2 font-mono text-xs">{q.code}</td>
                    <td className="p-2">{q.dimension}</td>
                    <td className="p-2">{q.tier === "basic" ? "基础" : "进阶"}</td>
                    <td className="p-2">{TYPE_LABELS[q.type] ?? q.type}</td>
                    <td className="p-2">{q.difficulty}</td>
                    <td className="max-w-48 truncate p-2 text-slate-600" title={q.stem}>{q.stem}</td>
                    <td className="p-2">
                      <Badge variant={q.status === "published" ? "default" : "secondary"}>
                        {STATUS_LABELS[q.status] ?? q.status}
                      </Badge>
                    </td>
                    <td className="p-2 text-slate-400">{q.version}</td>
                    <td className="whitespace-nowrap p-2">
                      <Button variant="ghost" size="sm" onClick={() => setEditing(q)}>编辑</Button>
                      {q.status === "published" && (
                        <Button variant="ghost" size="sm" className="text-red-600" onClick={() => retire(q)}>
                          下架
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between text-sm text-slate-500">
            <span>共 {list.total} 题</span>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                上一页
              </Button>
              <span>
                {list.page}/{totalPages}
              </span>
              <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
                下一页
              </Button>
            </div>
          </div>
        </>
      ) : null}

      {creating && (
        <QuestionDrawer
          editing={null}
          onClose={() => setCreating(false)}
          onSaved={() => {
            setCreating(false);
            setReloadTick((n) => n + 1);
          }}
        />
      )}
      {editing && (
        <QuestionDrawer
          editing={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            setReloadTick((n) => n + 1);
          }}
        />
      )}
    </div>
  );
}

function ReviewTab() {
  const [status, setStatus] = useState("open");
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [scores, setScores] = useState<Record<number, number>>({});
  const [resolvingId, setResolvingId] = useState<number | null>(null);
  const [reloadTick, setReloadTick] = useState(0); // 裁定后重拉队列
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    setItems(null);
    setError("");
    // status: open/resolved/all（all 由后端枚举支持＝不过滤；省略参数会落到后端缺省 open）
    api<{ items: ReviewItem[]; total: number }>(`/api/admin/review-queue?status=${status}`)
      .then((out) => setItems(out.items))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "复核队列加载失败"));
  }, [status, reloadTick]);

  async function resolve(item: ReviewItem) {
    const score = scores[item.id] ?? 3;
    if (resolvingId !== null) return;
    setResolvingId(item.id);
    setError("");
    setNotice("");
    try {
      await api(`/api/admin/review-queue/${item.id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ final_score: score }),
      });
      setNotice(`条目 #${item.id} 已裁定为 ${score} 分`);
      setReloadTick((n) => n + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "裁定失败，请重试");
    } finally {
      setResolvingId(null);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {[
          { value: "open", label: "待复核" },
          { value: "resolved", label: "已复核" },
          { value: "all", label: "全部" },
        ].map((s) => (
          <Button key={s.value} size="sm" variant={status === s.value ? "default" : "outline"} onClick={() => setStatus(s.value)}>
            {s.label}
          </Button>
        ))}
        {notice && <span className="text-sm text-green-700">{notice}</span>}
      </div>
      {error && <p className="text-sm text-red-600">{error}</p>}
      {items === null ? (
        <p className="text-sm text-slate-500">复核队列加载中…</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-slate-500">当前筛选下没有复核条目</p>
      ) : (
        items.map((item) => (
          <Card key={item.id}>
            <CardHeader className="pb-2">
              <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
                <Badge variant="secondary">{item.dimension ?? "?"}</Badge>
                <span className="font-mono text-xs text-slate-500">{item.question_code}</span>
                <span className="text-slate-500">学员 {item.student_no ?? "?"}</span>
                <Badge variant={item.status === "open" ? "destructive" : "default"}>
                  {item.status === "open" ? "待复核" : `已裁定 ${item.resolved_score ?? "—"}`}
                </Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <p className="text-sm leading-relaxed">{item.question_stem ?? "（题目已不存在）"}</p>
              {item.reason && <p className="text-xs text-amber-700">入队原因：{item.reason}</p>}
              {item.answer && (
                <p className="line-clamp-3 whitespace-pre-wrap rounded bg-slate-50 p-2 text-xs text-slate-600">
                  学员作答：{item.answer}
                </p>
              )}
              {item.judge_raw && (
                <details className="rounded border">
                  <summary className="cursor-pointer select-none px-2 py-1 text-xs text-slate-500">判题原始输出</summary>
                  <pre className="max-h-48 overflow-auto px-2 pb-2 text-xs text-slate-500">
                    {JSON.stringify(item.judge_raw, null, 2)}
                  </pre>
                </details>
              )}
              {item.status === "open" && (
                <div className="flex items-center gap-2">
                  <Label htmlFor={`score-${item.id}`} className="text-xs">人工终评（0-4）</Label>
                  <select
                    id={`score-${item.id}`}
                    className="rounded-md border bg-white px-2 py-1 text-sm"
                    value={scores[item.id] ?? 3}
                    onChange={(e) => setScores((prev) => ({ ...prev, [item.id]: Number(e.target.value) }))}
                  >
                    {[0, 1, 2, 3, 4].map((n) => (
                      <option key={n} value={n}>{n}</option>
                    ))}
                  </select>
                  <Button size="sm" onClick={() => resolve(item)} disabled={resolvingId === item.id}>
                    {resolvingId === item.id ? "裁定中…" : "确认裁定"}
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        ))
      )}
    </div>
  );
}

function TeacherTab() {
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function submit() {
    if (creating) return;
    setNotice("");
    setError("");
    if (!name.trim() || !username.trim()) {
      setError("姓名与用户名不能为空");
      return;
    }
    if (password.length < 6) {
      setError("密码至少 6 位");
      return;
    }
    setCreating(true);
    try {
      const t = await api<{ name: string; username: string }>("/api/admin/teachers", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), username: username.trim(), password }),
      });
      setNotice(`已创建教师账号：${t.name}（用户名 ${t.username}）`);
      setName("");
      setUsername("");
      setPassword("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败，请重试");
    } finally {
      setCreating(false);
    }
  }

  return (
    <Card className="max-w-md">
      <CardHeader>
        <CardTitle className="text-base">创建教师账号</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {notice && <p className="text-sm text-green-700">{notice}</p>}
        {error && <p className="text-sm text-red-600">{error}</p>}
        <div className="space-y-1">
          <Label htmlFor="t-name">姓名</Label>
          <Input id="t-name" value={name} maxLength={32} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="t-username">用户名（登录用）</Label>
          <Input id="t-username" value={username} maxLength={32} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="t-password">密码（至少 6 位）</Label>
          <Input id="t-password" type="password" value={password} maxLength={64} onChange={(e) => setPassword(e.target.value)} />
        </div>
        <Button onClick={submit} disabled={creating}>
          {creating ? "创建中…" : "创建教师"}
        </Button>
      </CardContent>
    </Card>
  );
}

// ---------- 页面 ----------

const TABS = [
  { value: "questions", label: "题库管理" },
  { value: "review", label: "复核队列" },
  { value: "teacher", label: "教师账号" },
];

export default function AdminPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "questions";

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-4">
      <header className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">管理后台</h1>
        <Link to="/" className="text-sm text-slate-500 hover:text-slate-700">返回首页</Link>
      </header>
      <div className="flex gap-2 border-b pb-2">
        {TABS.map((t) => (
          <Button
            key={t.value}
            size="sm"
            variant={tab === t.value ? "default" : "ghost"}
            onClick={() => setParams(t.value === "questions" ? {} : { tab: t.value })}
          >
            {t.label}
          </Button>
        ))}
      </div>
      {tab === "review" ? <ReviewTab /> : tab === "teacher" ? <TeacherTab /> : <QuestionsTab />}
    </div>
  );
}
