# M2c 教师端·管理后台·复核队列 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 教师班级看板（批量报告/共性短板/成长曲线/CSV 导出）、管理员题库 CRUD 与人工复核队列界面、会话查询端点与断线续答（恢复），并清偿 M2b/M2b2/M2e 延后项。

**Architecture:** `Klass` 加 `teacher_id`；teacher/admin 角色路由守卫沿用 JWT role；复核 resolve 更新 answer.score/rationale（标注人工复核）——**θ 不重放**（裁定：复核为个位数题目，重放破坏性大收益小，报告以人工分为准）；会话查询端点 `GET /api/sessions/{id}`（SessionView+消息历史）与 `GET /api/sessions/active`（本人最近 in_progress）支撑前端恢复。

**Tech Stack:** 同前。Spec：M2 总体设计 §8（教师端/后台/复核）、§10（暂停续答）。

## Global Constraints

- 权限：teacher 只读本班学员聚合数据（不展示单个学员作答明细）；admin 全权；越权 403/404。
- 复核：仅 admin/teacher 可见队列；resolve 后 answer.score=final_score、rationale 追加"[人工复核]"、SessionAnswer.is_correct 同步（开放题保持 None）；review_queue.status='resolved'。
- 前端角色菜单：登录后按 role 展示（学员=测评/错题本；教师=班级看板；管理员=题库后台/复核队列/教师账号管理）。
- 断线续答：HomePage 检测 in_progress 会话 → "继续测评"卡片（展示已答进度）→ /assess 恢复（含对话历史气泡、实操轮次）。
- 清偿项：①错题计数统一为"去重题数"（wrong_count 改 wrong_questions 数组或加去重计数——后端聚合处一行+测试）②HomePage 报告卡片短板维度加"去练习"链接（/practice?dimension=Dx）③task effect 改 ref 防护（对齐 dialog）④finish 注解等既有代码不动。
- 每任务 commit；全量门禁；中文文案。

---

### Task 1: 后端教师端与管理端

**Files:** Modify `models.py`（Klass+teacher_id 迁移）；Create `api/app/api/teacher_routes.py`、`api/app/api/admin_routes.py`；Modify `session_routes.py`（+`GET /sessions/active`、`GET /sessions/{id}` 完整视图含消息历史）；Modify `main.py`
**Interfaces:**
- `POST /api/admin/teachers {name, username, password}`（admin 创建教师）；`GET /api/admin/review-queue?status=open`（含题目 stem/维度/学员代号/judge_raw）；`POST /api/admin/review-queue/{id}/resolve {final_score: 0..4}`（裁定语义见上）；题库 CRUD `GET/POST/PUT/DELETE /api/admin/questions`（GET 支持 dimension/tier/难度/status 筛选+分页；PUT 走 validate_question 且 version+1；DELETE 软删 status='retired'）。
- `POST /api/teacher/classes {name}`（建班得邀请码）；`GET /api/teacher/classes`；`GET /api/teacher/classes/{id}/analytics` → `{student_count, radar:[{dimension,label,avg_percent}], dimension_rank, weakness_top3:[{dimension, tags:[...]}], students:[{name, last_level_name, reports, trend_slope}], timeline:[{date, avg_percent}]}`；`GET /api/teacher/classes/{id}/export.csv`（学员×六维得分 CSV，中文表头，UTF-8 BOM）。
- `GET /api/sessions/active` → 本人最近 in_progress 的 `{session_id, stage, progress, started_at}` 或 null；`GET /api/sessions/{id}` → SessionView + `messages: [{channel, role, content, seq}]`（本人或 teacher/admin——教师端仅元数据不含消息）。
**Test:** `test_teacher.py`（建班/看板聚合数字正确/CSV BOM 与表头/越权 403）、`test_admin.py`（教师创建/CRUD/版本/软删/复核 resolve 落分与队列状态/越权）、`test_session_resume.py`（active 端点/完整视图消息历史/越权）。
- [ ] TDD 全链 → commit `feat: 教师端与管理端后端（看板/题库/复核/会话恢复）`

### Task 2: 前端教师看板·管理后台·续答入口

**Files:** Modify `App.tsx`（路由 `/teacher`、`/admin`，RequireRole 组件）、`HomePage.tsx`（角色菜单+续答卡片+短板"去练习"链接）、`PracticePage`（错题计数口径展示跟随后端）；Create `web/src/pages/TeacherPage.tsx`（班级选择/雷达/维度条形/短板 TOP3/学员表/成长曲线折线/CSV 导出按钮）、`web/src/pages/AdminPage.tsx`（题库表格+筛选+编辑抽屉+复核队列 tab+教师账号创建）；Create `web/src/lib/role.ts`（角色工具）
**Test:** vitest 组件冒烟（TeacherPage 渲染 mock 数据/AdminPage 复核操作）+ build。
- [ ] 门禁 → commit `feat: 教师看板与管理后台前端`

---

## Self-Review

- spec §8/§10 全覆盖；延后项承接（M2b①会话查询/M2c 口径/入口/task 防护归 T2；M2b2 spawn 失败回滚 guard 归 T1 一行加固）。
- CSV 用 UTF-8 BOM（Excel 中文兼容）。
- 真实验收：控制者建教师+班级+看板走查（学员数据来自既有 E2E 账号）。
