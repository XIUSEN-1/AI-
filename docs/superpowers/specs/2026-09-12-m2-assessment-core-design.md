# AI 能力罗盘 · M2 设计文档（Spec）

日期：2026-09-12 · 状态：待用户审阅 · 前置：`2026-09-12-ai-compass-design.md`（M1 总体设计，继续有效）

## 1. 范围与目标

M2 在 M1 闭环之上交付：**LLM 判题管线、对话式测评、实操任务（真实 DeepSeek 窗口）、四阶段编排、题库与引擎难度升级、教师端与题库后台、报告增强（逐题回显/LLM 建议/成长趋势/导出）、评分准确性校准实验、体验微修**。

不改变 M1 已验收的客观题核心逻辑（除停止规则升级，见 §5）。

## 2. 已定案约束

- **DeepSeek key 已配置**于 `api/.env`（gitignored），models 端点验证可用。
- 模型分工（env 可配）：判题 `deepseek-v4-pro`（`DEEPSEEK_MODEL_JUDGE`）；对话考官与实操窗口 `deepseek-flash`（`DEEPSEEK_MODEL_CHAT`）。
- **单测零 token**：LLM 调用一律经注入的 `chat_fn`，单测全 mock；真实调用仅联调/校准/演示。
- 用户已拍板：实操产物=对话+文本框；对话式评分时机=结束后统一判；校准=AI 代理标注+脚本算指标；难度升级=完整方案（24 高难题+触顶停止规则+考官追问加压）。

## 3. LLM 接入层（`app/llm/`）

- `provider.py`：OpenAI 兼容客户端工厂，`chat(messages, *, model_role: "judge"|"chat", temperature=0.0, json_mode=False, stream=False)`。
  - 配置：`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`（默认 `https://api.deepseek.com`）、模型名 env。
  - 流式：`chat_stream(messages, model_role)` 返回异步生成器（SSE 用）。
- `mock.py`：`MockChat`（队列式 canned 响应 + 调用记录），测试与本地无 key 演示兜底。
- key 缺失时：judge 走降级链（§4），对话/实操窗口返回明确中文错误。

## 4. LLM 判题管线（`app/judge/`）

- `pipeline.py`：`judge_answer(question, submission, chat_fn) -> JudgeResult`（pydantic）：
  - 输入拼装：题目 + rubric（points+anchors）+ 作答全文（对话式含学员侧 prompts 全记录）。
  - 输出 schema：`{score: int 0-4, hits: [str], strengths: [str], gaps: [str], rationale: str(中文)}`。
  - **校验链**：JSON 解析/pydantic 失败 → 重试 ≤2（重试 prompt 附上次错误）→ 仍失败 → 关键词覆盖度降级评分（hits 命中率×4 四舍五入）+ 标记 `degraded=True` 入复核队列。
  - **一致性**：temperature=0 双跑；|Δscore|>1 → 第三跑取中位；仍 >1 → `needs_review=True` 入复核队列。
- θ 回灌：开放类得分 `result = score/4`，走既有 `adaptive.update`（spec M1 §5.2 语义）。
- 复核队列：`review_queue` 表（question_id, session_id, judge_raw, reason, status, resolved_score）。管理后台处理。

## 5. 题库与引擎难度升级

### 5.1 高难客观题扩容（90 → 114 题）

- 每维度新增 4 道（id `D{n}-H01..H04`），`tier="advanced"`、`type ∈ single/multi/judge`、难度 4-5（每维度至少 1 道 d5）。
- 出题基调：多步辨析、反直觉边界、陷阱识别、跨概念取舍判断（例：思维链在何种任务上反而劣化的辨析、RAG 与微调的成本/效果取舍、温度与幻觉率的关系辨析、指令注入风险识别）。
- 校验器兼容：现有规则 `advanced → 难度≥4` 天然放行客观题型，无需改动；`test_bank_content` 更新总量与"advanced 含客观题"断言。
- 进入客观题池：`_pick_question` 现有 `type.in_(OBJECTIVE_TYPES)` 过滤自动纳入。

### 5.2 停止规则升级（引擎微调，含测试同步）

- `DimensionState` 新增字段 `max_answered: int = 0`（该维度已答最高难度，`update` 维护，`from_dict` 向后兼容默认 0）。
- `should_stop(state, ceiling: int | None = None)`：**连对停止（streak≥2）需满足 `ceiling is None or state.max_answered >= ceiling`**；连错停止、n≥6、收敛停止规则不变。
- `ceiling` 由会话层传入（该维度已发布客观题的最大难度）。
- 效果：强学员爬到 d4/d5 才停（每维 +1~2 题，总时长 +3~5 分钟）；弱学员路径零变化。

### 5.3 对话考官追问加压（并入 §6 实现细节）

考官 system prompt 内置三档追问策略：澄清追问（L2 校验）→ 深挖追问（原理/取舍）→ 反例挑战（边界条件、要求自我纠错）。按学员当轮回答长度与 θ 动态选档。

## 6. 四阶段编排与两种新模式

### 6.1 会话状态机扩展

`AssessmentSession.stage: objective → dialog → practical → finished`（quick 模式仍止于 objective）。客观阶段全部维度完成后自动进入 dialog；抽题规则：六维按 θ 升序排列，取**第 3、4 位**的两个维度各 1 道进阶情境题（中段维度信息增量最大——强维度已确认、弱维度客观题已充分探测）；实操固定取 D5 实操题（若 D5 恰为对话抽中维度则实操顺延取 D5 下一道或 D2 实操题）。

### 6.2 对话式测评

- `POST /api/sessions/{id}/dialog/start`：锁定题目，返回考官开场白（LLM 生成，模板兜底）。
- `POST /api/sessions/{id}/dialog/turn`：学员发言 → 考官回复（**SSE 流式**，flash 模型）；服务端记录全轮次；每题追问 ≤3 轮（学员可提前"完成回答"）。
- 两题完成后进入 practical。评分在 finish 时统一判（§4），submission=学员侧全部发言。

### 6.3 实操任务（真实 AI 协作）

- `GET /api/sessions/{id}/practical/task`：任务说明（题面+要求+产物格式说明）。
- `POST /api/sessions/{id}/practical/chat`：**SSE 代理 DeepSeek-flash**（服务端注入轻量 system：仅模拟通用助手，不泄题不判分），学员与真实 AI 自由对话，服务端全量留痕 `practical_logs`。
- `POST /api/sessions/{id}/practical/submit {artifact_text}`：提交最终产物（文本框，200~5000 字）。
- **双通道评分**：过程分（学员 prompts 的清晰度/任务拆解/迭代甄别行为，独立量表）+ 产物分（题 rubric），各 0-4；D5 的 `result = (过程分 × 0.6 + 产物分 × 0.4) / 4` 回灌 θ。

## 7. 报告增强

- **逐题回显**：`GET /api/reports/{id}` 扩展 `answers: [{seq, dimension, type, stem 摘要, is_correct|score, rationale, theta_after}]`；报告页按维度折叠展示（客观题显示对错+解析，开放题显示得分+评分理由）。
- **LLM 学习路径**：`build_report` 末尾同步调用 judge 模型生成个性化建议（输入：六维分数+短板+错题模式），失败/无 key 回退现有模板（M1 模板保留为兜底，不删）。
- **成长趋势**：`GET /api/reports/mine` 已有时间序列，前端报告页/工作台加历次对比折线。
- **导出图片**：报告页 html-to-image 一键导出 PNG（纯前端）。

## 8. 教师端与题库后台

### 8.1 教师端

- 教师账号：管理员后台创建（username+初始密码），或 `POST /api/admin/teachers`。
- `POST /api/teacher/classes`（建班→邀请码）、`GET /api/teacher/classes`。
- `GET /api/teacher/classes/{id}/analytics`：班级人数、六维聚合雷达（均分百分制）、维度均分排名、共性短板 TOP3（均分最低维度+错率最高考点 tags）、学员列表（最近等级/趋势斜率）、全期成长曲线（按周聚合）。
- `GET /api/teacher/classes/{id}/export.csv`。
- 前端：教师看板页（选班级→雷达+条形+短板榜+学员表+曲线+导出）。

### 8.2 题库后台（admin）

- `GET/POST/PUT/DELETE /api/admin/questions`（CRUD，走 `validate_question`，变更 version+1）。
- `GET /api/admin/review-queue` + `POST /api/admin/review-queue/{id}/resolve {final_score}`（复核落分回写 session_answer 并触发该维度 θ 重算——按相同 update 语义重放）。
- 前端：题库表格（维度/难度/标签筛选）+ 编辑抽屉 + 预览 + 复核队列页。

## 9. 校准实验（评分准确性 20% 验证数据）

- 样本：脚本生成 + 真实采集共 **50 份**开放题作答（覆盖 6 维度、分数档分布尽量均匀）。
- 标注：AI 代理按标注规程双盲打分（规程文档化：只看题面+rubric+作答）。
- 指标：Pearson/Spearman 相关系数、加权 Kappa（quadratic）、分档一致率；输出 `docs/评分准确性验证报告.md` + 原始数据 `docs/calibration/`。
- 脚本：`api/scripts/calibration.py`（生成→judge→指标→报告渲染）。

## 10. 体验微修（延后小项清偿）

multi 空答案禁提交（前端 disabled）、reports/mine 失败区分错误态与空态、答题页"暂停续答"入口（列出 in_progress 会话可继续）、README 部署节补 JWT_SECRET 等环境变量清单。

## 11. 数据模型增量

- 新表：`practical_logs(id, session_id, question_id, role, content, created_at)`、`review_queue(...)`、`dialog_turns` 并入 practical_logs（role=examiner/learner/assistant，question_type 区分）——统一一张 `session_messages` 表更简：`(id, session_id, question_id, channel: dialog|practical, role: learner|examiner|assistant, content, seq)`。
- `AssessmentSession` 加 `stage`；`DimensionState` 加 `max_answered`。
- `Question` 无 schema 变更（新题只是数据）。

## 12. API 面增量总览

```
POST /api/sessions/{id}/dialog/start|turn        SSE(后者)
GET  /api/sessions/{id}/practical/task
POST /api/sessions/{id}/practical/chat           SSE
POST /api/sessions/{id}/practical/submit
POST /api/admin/teachers                          [admin]
GET/POST/PUT/DELETE /api/admin/questions[...]     [admin]
GET  /api/admin/review-queue                      [admin]
POST /api/admin/review-queue/{id}/resolve         [admin]
POST /api/teacher/classes                         [teacher]
GET  /api/teacher/classes                         [teacher]
GET  /api/teacher/classes/{id}/analytics          [teacher]
GET  /api/teacher/classes/{id}/export.csv         [teacher]
```

## 13. 测试策略

- 引擎/判题/编排：pytest 全 mock `chat_fn`（覆盖：schema 失败重试、降级、双跑分歧、三跑中位、复核入队、θ 回灌、触顶停止、阶段流转、SSE 端点用 TestClient 流式读取断言 chunk 序列）。
- 前端：vitest 组件冒烟 + `pnpm build` 门禁；真实浏览器 E2E 由控制者走查（同 M1 流程）。
- 真实 LLM 冒烟与校准：独立脚本/手动，不进 pytest。

## 14. 里程碑（SDD 流水线：实现者→审查者→修复轮）

- **M2a** LLM 接入层 + 判题管线 + 题库扩容 114 题 + 引擎触顶停止 + 逐题回显 + LLM 建议（含 mock 全链路）
- **M2b** 对话式测评 + 实操任务 + 四阶段编排（SSE）
- **M2c** 教师端 + 题库后台 + 复核队列
- **M2d** 校准实验 + 成长趋势/导出 + 体验微修 + 部署清单

## 15. 风险与对策

| 风险 | 对策 |
| --- | --- |
| 判题不稳定/成本 | 双跑+降级+复核（§4）；flash/judge 分模型控成本 |
| SSE 在代理/部署环境的兼容性 | M2b 末提前在 Meoo 部署演练一次 |
| 触顶规则拉长测评时间 | 题量上限 n≥6 兜底不变，最多 +12 分钟 |
| 校准样本分布不均 | 生成脚本按分数档配额采样 |
