# M2b-hotfix 跳过与异步判题 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ①对话题与实操题支持跳过（不判分不回灌 θ，报告标注"学员跳过"）；②finish 改异步并行判题（墙钟 ↓50%+），前端轮询分步进度，完成自动出报告。

**Architecture:** 跳过=对话题复用关闭语义（examiner 落一条 skip 标记消息，学员零发言沿用"不回灌"路径但 rationale 区分"学员跳过"）；实操加 `POST .../practical/skip` 直接置 ready（无 submit 行）。异步判题：finish 置 judging 后**立即 202 返回**，ThreadPoolExecutor 后台线程并行判题（结果内存收集），判完回主连接统一写库（θ 回灌/answers/Report/status=finished）；进度计数写 `AssessmentSession.judging_step/judging_total` 两列（幂等迁移已挂启动），新轻端点 `GET /api/sessions/{id}/status` 供轮询。

**Tech Stack:** 同前。Spec：本轮 grilling 用户定案（2026-09-12 第三轮），补记入 M2 spec §15 增补。

## Global Constraints

- 单测零真实 LLM（mock chat_fn / MockTransport）；判题并行用注入的 chat_fn 也须可 mock。
- 后台线程**只做 LLM 调用与内存收集**，全部 DB 写入在判题完成后由主线程执行（避免 SQLite 跨线程写锁）。
- 跳过语义：不判分、不回灌 θ、SessionAnswer 落库 `score=None`、rationale="学员跳过"（对话）/实操 skip 行同理；报告回显徽章"已跳过"。
- judging 态沿用防重入；judging 中进程重启 → status 停留 judging（现状已知限制，保持）。
- 每任务独立 commit；全量 pytest+vitest+build 门禁；文案简体中文。

---

### Task 1: 跳过功能（后端）

**Files:** Modify `api/app/api/dialog_routes.py`（`POST /api/sessions/{id}/dialog/skip`：当前未闭题落 examiner 消息 content=常量 `DIALOG_SKIPPED`，走既有切题/翻转逻辑）、`api/app/api/practical_routes.py`（`POST .../practical/skip`：stage=practical → 置 ready，无 submit 行）、`api/app/api/session_routes.py`（finish 对话题判定：该题 learner 消息为空且存在 DIALOG_SKIPPED 标记 → 不判分不回灌，answer 行 rationale="学员跳过"；实操无 submit 行且 skipped 标记同理——practical skip 落一条 role=learner?否，用 SessionMessage role="system"? 简化：SessionMessage channel=practical role=examiner content=PRACTICAL_SKIPPED 标记，finish 见此标记且无 submit → skip 语义）
**Test:** `api/tests/test_skip.py`：对话跳过切题与全跳直达 practical；实操跳过置 ready；全跳 finish 报告六维纯客观 θ、answers 含 rationale="学员跳过"、D5 不回灌；skip 后仍可正常走不跳路径。

- [ ] TDD → commit `feat: 对话与实操阶段支持跳过`

### Task 2: 异步并行判题（后端）

**Files:** Modify `api/app/models.py`（+`judging_step: int default 0`、`judging_total: int default 0`，迁移挂既有机制）、`api/app/api/session_routes.py`：
- finish：ready/quick 校验后置 judging（含 total=N 客观外待判题数+1 advice？total=主观题数）并 commit → **立即返回 `202 {"session_id", "judging_total"}`**；
- 后台 `threading.Thread` 跑 `_judge_all_parallel(session_id, plan)`：ThreadPoolExecutor(max_workers=4) 并行调 judge_answer/chat_fn（各题独立、无共享可变状态；chat_fn 每线程独立构建）；结果与异常内存收集；
- 完成后**新开 DB session** 顺序执行既有回灌/answers/Report/status=finished 写入；judging_step 在每题完成时用独立短连接更新（或仅在开始/结束更新+前端按 (step,total) 插值文案——取简：开始 0、每题完成 +1，短连接 UPDATE）；
- 任一判题异常：该题走降级链（judge_answer 内置），线程级致命异常 → status 回 in_progress + step 归零（可重试）。
- 新端点 `GET /api/sessions/{id}/status`（归属校验）→ `{status, stage, judging_step, judging_total, report_id?}`（finished 时带 report_id）。
**Test:** `api/tests/test_async_judging.py`：202 立即返回（耗时断言 <1s，mock 判题 sleep 0.5s×3 → 并行后总耗时 <1.5s 而串行需 1.5s+）；轮询 status 见 step 递增至 finished+report_id；judging 中重复 finish 409；跳过+混合路径回归。

- [ ] TDD → commit `feat: finish 异步并行判题与进度轮询`

### Task 3: 前端跳过与进度（含回归）

**Files:** Modify `web/src/pages/AssessmentPage.tsx`（对话视图+"跳过本题"按钮（次级样式，确认气泡防误触）；实操视图+"跳过实操任务"；客观完成 → 若全跳也直达 ready；ready 态"生成报告"点击后改**轮询模式**：调 finish 得 202 → setInterval 2s 轮询 status → 进度条文案"智能判题中 x/y"→ finished 自动跳报告页；409/失败错误上屏与重试）；`web/src/pages/ReportPage.tsx`（answer 条目 rationale=="学员跳过" → 徽章"已跳过"）；
**Test:** vitest 组件/逻辑冒烟（进度轮询 hook 用 fake timers 可选）+ build 门禁。

- [ ] TDD/门禁 → commit `feat: 前端跳过按钮与判题进度轮询`

---

## Self-Review

- 覆盖：用户定案①→T1+T3 跳过；②→T2+T3 进度。既有 173 测试行为回归由各 Task 保证（finish 返回值 200→202 变更需同步既有测试——test_finish* 系列改断言 202+轮询 helper）。
- 接口：status 端点契约 T2 定义 T3 消费；skip 端点 T1 定义 T3 消费。
- 真实计时验证：M2b-hotfix 终审后控制者真实 LLM E2E 复测墙钟时间（对照 40~90s 基线）。
