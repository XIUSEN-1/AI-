# M2e 题库扩容·分级建议·错题本 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 题库批次 1（114→300，含查重测试）；分级建议库 30 格（报告命中+LLM 组装）；错题本与薄弱练习卷（不回灌 θ）；实操判题再提速（量表与产物并发）。

**Architecture:** 纯内容+独立模块批次：题库走 seeds 增量与既有校验器（新增题干 n-gram 查重测试）；建议库 `seeds/advice_matrix.json` 由 `generate_llm_advice` 消费（格子命中→LLM 组装，无 LLM 直渲染格子文案+资源）；错题本/练习卷为只读派生（从 session_answers 聚合错题 → 按维度/标签抽未作答题 → 独立 practice 端点即时判分，不写 theta_snapshot/不进报告）；提速杠杆为 `_judge_all_parallel` 内实操过程/产物两个 future 并发。

**Tech Stack:** 同前。Spec：`docs/superpowers/specs/2026-09-12-m2-assessment-core-design.md` §15 增补（用户 2026-09-12 第二/三轮 grilling 定案）。

## Global Constraints

- 题库配额（精确）：每维度 +31 题 = 客观 25（难度金字塔 d1×5/d2×6/d3×6/d4×5/d5×3）+ 主观 6（4 open + 2 practical）；6 维合计 +186 → 总 300（客观 234/主观 66 ≈ 8:2）。id 续号：客观 `D{n}-B11..`（基础续）与 `D{n}-H05..`（高难续），主观 `D{n}-A06..`；difficulty 与 tier 规则照旧（basic 1-3 / advanced 4-5）。
- **查重**：新增 `api/tests/test_bank_dedup.py`——题干 3-gram Jaccard 相似度 >0.6 的题对即失败（报告相似对供修订）；标签级：同维度同 tags 题对需题干相似度 <0.4。
- 内容纪律：干扰项来自"看起来合理的半对认知"；explanation 必填讲清半对为何错；主观题 rubric 五档锚定可区分；practical 任务须真实可做（学员用通用 AI 即可完成）；2025 前事实共识。
- 建议库 30 格每格：`{summary: 针对性文案, resources: [{title, type, note}](2-4 项真实可寻资源), exercises: [练习任务 2-3 项], promotion: 晋级标准一句话}`；不编造具体 URL（写可检索的名称+平台）。
- 练习卷语义：`GET /api/me/wrongbook`（错题聚合：维度/考点标签/题号/次数）；`POST /api/practice/sessions {dimension?, tags?, size:5..10}`（从"答错维度/标签"抽未作答题，不足放宽到同维度任意未作答题）；`POST /api/practice/sessions/{id}/answer`（即时判分反馈对错+解析，**不更新 θ、不进正式报告**）；练习记录落 `practice_answers` 表。
- 提速：实操过程量表与产物判题两个调用并发（ThreadPoolExecutor(2)），第三跑/建议逻辑不变。
- 每任务独立 commit；全量 pytest+vitest+build 门禁；文案简体中文；密钥不入库。

---

### Task 1: 题库批次 1 · D1~D3（+93）

**Files:** Modify `seeds/questions/D1.json`~`D3.json`（各 +31）；Create `api/tests/test_bank_dedup.py`
**Interfaces:** 查重函数 `dedup_pairs(questions) -> list[tuple[code, code, score]]`（3-gram Jaccard，中英文按字符 n-gram）供测试与后续批次复用。
- [ ] 先落地查重测试（对现有 114 题跑，应绿）→ 写 D1~D3 各 +31（基础续 B/H 续号、主观 A 续号，配额精确）→ `pytest tests/test_bank_content.py tests/test_bank_dedup.py` 全绿 + `python -m app.seed` 增量 `{'created': 93, 'updated': 0}` → commit `feat: 题库批次1·D1~D3（207 题）`

### Task 2: 题库批次 1 · D4~D6（+93）

**Files:** Modify `seeds/questions/D4.json`~`D6.json`（各 +31）
- [ ] 同 Task 1 流程 → 总 300，`{'created': 93}`（累计验证 300）→ commit `feat: 题库批次1·D4~D6（300 题达成）`

### Task 3: 分级建议库与报告融合

**Files:** Create `seeds/advice_matrix.json`（30 格）；Modify `api/app/report/generate.py`（加载+命中格子：`_advice_cell(dim, level)`；`generate_llm_advice` 输入追加格子内容，prompt 要求基于格子资源/任务个性化组装；无 LLM/失败回退=格子 summary+resources+exercises 直渲染）；Modify `session_routes.py`（advice 装配处传入格子）
**Test:** `api/tests/test_advice_matrix.py`（30 格 schema 校验、报告命中正确格子、LLM 成功路径 prompt 含格子资源、回退路径渲染格子内容）
- [ ] TDD → commit `feat: 分级建议库 30 格与报告融合`

### Task 4: 错题本与练习卷

**Files:** Modify `api/app/models.py`（+`PracticeSession(id, user_id, question_ids json, created_at)`、`PracticeAnswer(id, practice_session_id, question_id, answer, is_correct, created_at)`，迁移自动）；Create `api/app/api/practice_routes.py`（三端点，见 Global Constraints）；Modify `main.py`
**Test:** `api/tests/test_practice.py`（错题聚合正确；抽题避开已作答；即时判分；θ/正式报告零影响——断言 theta_snapshot 与 reports 不变）
- [ ] TDD → commit `feat: 错题本与薄弱考点练习卷`

### Task 5: 前端入口与练习页 + 提速杠杆

**Files:** Modify `web/src/pages/HomePage.tsx`（错题本入口卡片：错题数徽章）；Create `web/src/pages/PracticePage.tsx`（轻量练习视图：题目+选项+即时对错反馈+解析+进度+结束总结，复用答题交互模式）；Modify `web/src/App.tsx`（路由 `/practice`）；Modify `api/app/api/session_routes.py`（实操过程/产物判题并发）
**Test:** vitest 练习页冒烟 + build；后端并发测试（mock 两调用各 sleep 0.4s，断言 <0.6s）
- [ ] 门禁绿 → commit `feat: 错题本前端与练习页` + `fix: 实操判题双通道并发提速`

---

## Self-Review

- Spec §15 覆盖：题库批次1→T1/T2；建议库→T3；错题本→T4/T5；提速杠杆→T5。
- 依赖：T1/T2 独立可并行审；T3 依赖 seeds 加载模式；T4/T5 前后端契约在 Constraints 定义。
- 真实验收：控制者 E2E（练习一卷、报告建议含格子资源、真实计时实操路径 <35s）。
