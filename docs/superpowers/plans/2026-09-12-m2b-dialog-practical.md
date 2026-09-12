# M2b 对话式测评与实操任务 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 主观题接入测评流程——四阶段编排（客观→对话式→实操→报告）、DeepSeek 考官 SSE 追问、实操真实 AI 协作窗（留痕+双通道评分）、统一判题与 θ 回灌。

**Architecture:** `AssessmentSession` 增 `stage` 状态机（objective→dialog→practical→finished）；新表 `session_messages` 统一留痕（channel: dialog|practical，role: learner|examiner|assistant）；对话/实操经 `app/llm/provider.chat_stream`（SSE）；finish 时对对话题跑 `judge_answer`（submission=学员全部发言）、实操跑双通道（过程 0.6+产物 0.4），`update_open_result` 回灌 θ。LLM 调用侧承接 `ProviderUnavailableError`（中文错误/降级），单测零真实调用照旧。

**Tech Stack:** 同前（FastAPI StreamingResponse SSE + React EventSource/fetch-stream + SQLAlchemy + pytest/vitest）。

**Spec:** `docs/superpowers/specs/2026-09-12-m2-assessment-core-design.md` §5.3（考官追问三档）、§6（四阶段/对话式/实操）、§11（session_messages）、§12（API 面）；延后承接：SSE `data:` 空格容错、.env BOM 剥离、冒烟退出码、ProviderUnavailableError 调用侧承接。

## Global Constraints

- 单测零真实 LLM 调用（conftest 已强制空 key；新端点测试注入 mock chat_fn / chat_stream）。
- SSE 响应格式统一 `text/event-stream`，事件载荷为纯文本增量（`data: {json}\n\n`，json=`{"delta": "..."}`，结束事件 `{"done": true}`）。
- 抽题规则（spec §6.1 逐字）：六维按 θ 升序取**第 3、4 位**两维度各 1 道进阶情境题（open，未作答过的）；实操固定 D5 practical（若 D5 为对话抽中维度则取 D5 下一道 practical，仍无则 D2 practical）。
- 考官 system prompt 三档追问策略：澄清（L2 校验）→ 深挖（原理/取舍）→ 反例挑战（边界/自我纠错）；按轮次递进（第 1 轮澄清、第 2 轮深挖、第 3 轮反例），每题追问 ≤3 轮，学员可提前结束。
- 实操窗 system（服务端注入，不随题面下发）：通用助手角色、不泄题、不代学员做任务拆解判断之外的引导。
- 每任务独立 commit（`feat:/fix:` + 中文）；`git add` 后密钥扫描；全量 pytest+vitest+build 门禁；文案简体中文。

---

### Task 1: provider 加固（延后项清偿）

**Files:** Modify `api/app/llm/provider.py`、`api/app/config.py`、`api/scripts/smoke_llm.py`
**Test:** `api/tests/test_llm_provider.py`（扩展）

**Interfaces:** 签名不变。行为变更：① SSE 解析 `removeprefix("data:")`+strip（容无空格）；② `.env` 读取 `utf-8-sig`（BOM 剥离）；③ provider 统一包网络异常（`httpx.HTTPError/Timeout` → `ProviderUnavailableError`，保留 `__cause__`），冒烟脚本退出码语义随之归一（0 成功 / 1 降级 / 2 网络-不可用）。

- [ ] TDD：三处行为各先 RED（`data:x` 无空格行、BOM .env、`httpx.ConnectError` → ProviderUnavailableError）→ 实现 → GREEN → commit `fix: LLM provider SSE/BOM/网络异常加固`

### Task 2: 会话阶段状态机与抽题

**Files:** Modify `api/app/models.py`（AssessmentSession + `stage: str default "objective"`；新表 `SessionMessage(id, session_id FK, question_id, channel, role, content, seq, created_at)`）；Modify `api/app/api/session_routes.py`
**Test:** `api/tests/test_stage_machine.py`（新）

**Interfaces:**
- `_stage_plan(db, session) -> {"dialog": [q1, q2], "practical": qp} | None`：按 §6.1 抽题；客观未全完 → None。
- `_session_view` 扩展：`stage` 字段；`stage=="dialog"` 时 question=对话题（带 `dialog_turns_taken`），`stage=="practical"` 时返回实操题摘要；客观完成且 stage 仍 objective 时自动置 dialog 并返回首道对话题。
- 答完客观题后前端 `next==null` 不再直接 finish：改为返回 `{"stage": "dialog", "question": ...}`。`finish` 仅在 practical 提交后（或 quick 模式）允许。

- [ ] TDD：客观全完 → stage 翻转 + 抽题符合升序第 3/4 位规则（构造 θ 顺序断言维度选择）+ D5 冲突顺延 → quick 模式跳过 → commit `feat: 会话四阶段状态机与主观题抽题`

### Task 3: 对话式测评端点（SSE）

**Files:** Create `api/app/api/dialog_routes.py`；Modify `api/app/main.py`
**Test:** `api/tests/test_dialog.py`

**Interfaces:**
- `POST /api/sessions/{id}/dialog/start` → `{question: QuestionOut, opening: str}`（考官开场白：LLM 生成，失败回退模板"请结合你的实际经验，谈谈……"；不消耗追问轮次）。
- `POST /api/sessions/{id}/dialog/turn` body `{message: str(1..2000)}` → **SSE**：本轮学员发言落库 → 考官流式回复（chat_stream，三档策略按已轮次选 system）→ 完整回复落库，`{"done": true, "turns": n}` 收尾。轮次 ≥3 或学员 `POST .../dialog/finish-question` → 该题结束切下一题/下一阶段。
- 校验：会话归属、stage=="dialog"、当前题未判分、ProviderUnavailableError → SSE 首事件 `{"error": "AI 服务暂不可用，请稍后重试或跳过本题"}` + done（不中断会话，可跳过）。
- TestClient 流式断言：mock chat_stream 逐 chunk、消息落库（role/channel/seq）、轮次上限、错误事件。

- [ ] TDD → commit `feat: 对话式测评考官 SSE 端点与留痕`

### Task 4: 实操任务端点（真实 AI 协作窗）

**Files:** Create `api/app/api/practical_routes.py`；Modify `main.py`
**Test:** `api/tests/test_practical.py`

**Interfaces:**
- `GET /api/sessions/{id}/practical/task` → `{question: QuestionOut(题面+产物要求), artifact_min: 200, artifact_max: 5000}`
- `POST /api/sessions/{id}/practical/chat` body `{message}` → **SSE 代理** chat_stream（服务端注入实操 system，不含题面与判分信息）；学员与 assistant 消息全量落库；无 key → 错误事件同 Task 3。
- `POST /api/sessions/{id}/practical/submit` body `{artifact: str(200..5000)}` → 落库（role=learner, channel=practical, 标记 submit）→ stage 置 finished 前置状态（"ready"）。
- 防滥用：单会话 chat 轮次 ≤20。

- [ ] TDD → commit `feat: 实操任务真实 AI 协作窗与产物提交`

### Task 5: finish 统一判题与 θ 回灌

**Files:** Modify `api/app/api/session_routes.py`（finish）、`api/app/report/generate.py`
**Test:** `api/tests/test_finish_judging.py`

**Interfaces:**
- finish 流程：quick → 直接报告（现状）；full → ①两道对话题各跑 `judge_answer(question, submission=学员全部发言拼接, learner_prompts=学员发言列表, chat_fn)` ②实操双通道：过程分（量表固定 5 点：指令清晰/任务拆解/上下文给料/迭代甄别/结果整合，各 0-4 取均值）+ 产物分（题 rubric），`result=(过程×0.6+产物×0.4)/4` ③每题 `update_open_result` 回灌该维度 θ（对话题回灌其所属维度，实操回灌 D5）④judge 的 degraded/needs_review 入复核队列 ⑤报告 answers 回显扩展 rationale/score（开放题）。
- ProviderUnavailableError → 对该题走关键词降级（judge_answer 内置）并标记；全部判完才写 Report。
- 报告页"答对 X/Y"对开放题改口径：显示"得分 X/4"（前端小改并入 Task 6）。

- [ ] TDD（mock chat_fn 全链路：判分→回灌→报告 answers 含 rationale）→ commit `feat: finish 统一判题双通道评分与 θ 回灌`

### Task 6: 前端四阶段 UI

**Files:** Modify `web/src/pages/AssessmentPage.tsx`（阶段指示器客观→对话→实操；对话视图：气泡流+SSE 渐显+输入框+"结束本题"；实操视图：任务卡+聊天窗+产物提交；客观完成不再显示"生成报告"而是"进入对话式测评"）；`web/src/pages/ReportPage.tsx`（开放题回显得分/4+rationale）；Create `web/src/lib/sse.ts`（fetch 流解析，EventSource 不支持 POST）
**Test:** `web/src/lib/sse.test.ts`（chunk 解析）；门禁 build。

- [ ] sse.ts TDD → 页面改造 → build 绿 → commit `feat: 前端四阶段测评 UI（对话/实操/报告开放题回显）`

---

## Self-Review 记录

- Spec 覆盖：§6.1→T2、§6.2→T3、§6.3→T4、判题融合→T5、§5.3 三档追问→T3、延后承接（SSE 前缀/BOM/退出码→T1、ProviderUnavailableError→T3/T4/T5）。
- 接口一致性：chat_fn/chat_stream 签名沿用 M2a；session_messages 表三端点（T3/T4/T5）共用；answers 回显扩展向后兼容（旧报告无新键）。
- 范围外（M2e+）：千题批次 1、分级建议库、错题本；M2c/d 不动。
