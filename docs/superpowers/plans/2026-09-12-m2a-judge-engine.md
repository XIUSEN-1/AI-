# M2a 判题引擎与报告增强 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 LLM 接入层与判题管线（全 mock 可测）、复核队列、引擎触顶停止规则、题库扩容至 114 题，报告新增逐题回显与 LLM 个性化建议（模板兜底）。

**Architecture:** 在 M1 结构上新增 `app/llm/`（provider 抽象 + mock）与 `app/judge/`（判题管线：schema 校验→重试→降级→双跑一致性→复核入队）。判题函数接收注入的 `chat_fn`，单测零 token。引擎 `DimensionState` 增 `max_answered`，`should_stop` 增 `ceiling` 参数（连对停止需触顶）。报告生成末尾同步调 LLM 生成学习路径，失败回退 M1 模板。

**Tech Stack:** 同 M1（FastAPI + SQLAlchemy + pytest；React 19 + Vite + vitest）。LLM：OpenAI 兼容 HTTP（`urllib`/`httpx` 直连即可，不引 SDK——判题是简单 chat completion + SSE 两个端点）。

**Spec:** `docs/superpowers/specs/2026-09-12-m2-assessment-core-design.md`（§3 LLM 层、§4 判题、§5 难度升级、§7 报告增强）。

## Global Constraints

- 单测**零真实 LLM 调用**：`chat_fn` 一律注入 mock；真实调用的代码路径仅由独立冒烟脚本触达。
- `.env` 已含 `DEEPSEEK_API_KEY`（gitignored，绝不入库）；`git add` 后 `git grep --cached -nE "(sk-[A-Za-z0-9]{20,})"` 检查。
- 用户可见文案简体中文；async 链路 try/catch 上屏；引擎 copy-on-write 不变。
- 每任务独立 commit（`feat:/fix:` + 中文）；全量 pytest+vitest+build 门禁。
- 环境：Windows Git Bash；主仓 `C:\Users\tsbf-cjh1\Documents\ai-compass`；worktree 流程照 M1。

---

### Task 1: LLM 接入层（provider + mock）

**Files:**
- Create: `api/app/llm/__init__.py`、`api/app/llm/provider.py`、`api/app/llm/mock.py`
- Create: `api/app/config.py`
- Test: `api/tests/test_llm_provider.py`

**Interfaces（后续任务依赖）:**
- `config.get_settings() -> Settings`（dataclass 单例：`deepseek_api_key, base_url, model_judge, model_chat`；env：`DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL/DEEPSEEK_MODEL_JUDGE/DEEPSEEK_MODEL_CHAT`，默认 `https://api.deepseek.com` / `deepseek-v4-pro` / `deepseek-flash`）。
- `provider.chat_completion(messages: list[dict], *, model_role: Literal["judge","chat"], temperature=0.0, json_mode=False, timeout=60) -> str`（同步；判题用；无 key 抛 `ProviderUnavailableError`）。
- `provider.chat_stream(messages, *, model_role, temperature=0.7) -> Iterator[str]`（SSE 增量文本；M2b 用，本任务实现+测 mock 行为）。
- `mock.MockChat`：`__init__(responses: list[str])`，实现与 `chat_completion` 同签名并记录 `calls`；`MockStream` 类似返回分片。

- [ ] **Step 1 失败测试**（httpx mock transport 或 monkeypatch `httpx.post`；覆盖：messages 透传、json_mode 的 response_format 注入、无 key 抛错、MockChat 记录调用）

```python
# tests/test_llm_provider.py 关键用例（写全）
def test_chat_completion_sends_expected_payload(monkeypatch):
    captured = {}
    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        class R: status_code = 200
        def json(self): return {"choices": [{"message": {"content": "ok"}}, ]}
        return R()
    monkeypatch.setattr("app.llm.provider.httpx.post", fake_post)
    out = chat_completion([{"role": "user", "content": "hi"}], model_role="judge")
    assert out == "ok"
    assert captured["json"]["model"] == "deepseek-v4-pro"
    assert captured["json"]["temperature"] == 0.0

def test_json_mode_sets_response_format(monkeypatch): ...  # 断言 json={"type":"json_object"}
def test_no_key_raises(monkeypatch):  # settings.deepseek_api_key="" → ProviderUnavailableError
def test_mock_chat_records_calls(): ...
def test_chat_stream_yields_deltas(monkeypatch): ...  # SSE 分片解析
```

- [ ] **Step 2 RED** → `pytest tests/test_llm_provider.py -v` FAIL（ModuleNotFoundError）
- [ ] **Step 3 实现** provider.py（httpx.post 调 `{base_url}/chat/completions`，Bearer key；stream 用 `httpx.post(stream=True)` 逐行解析 `data: {...}` 提取 `delta.content`）；config.py（读 .env 用简单解析或 os.environ，`api/.env` 存在则加载——注意不覆盖已有 env）
- [ ] **Step 4 GREEN**；**Step 5 commit** `feat: LLM 接入层（provider 抽象与 mock）`

---

### Task 2: 判题管线

**Files:**
- Create: `api/app/judge/__init__.py`、`api/app/judge/pipeline.py`
- Test: `api/tests/test_judge_pipeline.py`

**Interfaces:**
- `JudgeResult(BaseModel)`: `score: int(0..4), hits: list[str], strengths: list[str], gaps: list[str], rationale: str, degraded: bool = False, needs_review: bool = False, runs: list[int] = []`（各次跑分）
- `judge_answer(question: dict, submission: str, chat_fn, *, learner_prompts: list[str] | None = None) -> JudgeResult`——`chat_fn(messages, **kw) -> str` 与 provider.chat_completion 同签名
- 提示词：system=判题官角色+评分规则+必须只输出 JSON；user=题面+rubric(points/anchors)+作答（+学员 prompts）
- 链路：解析失败重试≤2（重试附上次输出与错误）→ 降级 `关键词覆盖度`（hits 命中数/len(points)×4 四舍五入，rationale 标注降级）→ temperature=0 双跑（正常路径直接双跑：两次调 chat_fn）；|Δ|>1 → 三跑取中位；仍>1 → needs_review
- `update_open_result(theta_state, difficulty, score) -> DimensionState`：`adaptive.update(state, difficulty, score/4)`

- [ ] **Step 1 失败测试**（全 mock；用例：正常双跑一致→score、分差1内取均值？——设计定：一致（|Δ|≤1）取两次中**第一次**？No——取 `round((a+b)/2)`? spec 未定 → 定：|Δ|≤1 取均值四舍五入；schema 失败重试后成功；两次重试均失败→降级关键词；三跑中位；needs_review 标记；learner_prompts 拼装进 prompt 断言；score 越界 clip）

```python
def make_q():
    return {"id": "D2-A01", "stem": "题面", "rubric": {"points": ["要点A", "要点B", "要点C"], "anchors": {str(i): f"档{i}" for i in range(5)}}}

def test_consistent_double_run_returns_score():
    chat = MockChat(["{\"score\":3,\"hits\":[\"要点A\"],\"strengths\":[\"s\"],\"gaps\":[\"g\"],\"rationale\":\"理由\"}"] * 2)
    r = judge_answer(make_q(), "作答", chat)
    assert r.score == 3 and not r.needs_review and len(r.runs) == 2

def test_divergent_runs_third_median():  # 1,4,3 → 3, needs_review False(|3-中位|? 设计:取中位后 runs=[1,4,3], 若极差仍>1 → needs_review=True) → 定：三跑取中位且极差>1 → needs_review=True
def test_retry_on_bad_json_then_success():  # 第1次坏 JSON, 第2次好 → score 正常, runs 记录
def test_degrade_after_retries_exhausted():  # 全坏 → degraded=True, score=关键词覆盖度
def test_needs_review_true_when_wide_divergence(): ...
def test_open_result_feeds_theta():  # update_open_result(DimensionState(), 4.0, 3) ≈ update(...,0.75)
```

- [ ] **Step 2 RED** → **Step 3 实现** → **Step 4 GREEN**（含全量）→ **Step 5 commit** `feat: LLM 判题管线（校验/重试/降级/双跑一致性）`

---

### Task 3: 复核队列模型与入队

**Files:**
- Modify: `api/app/models.py`（+`ReviewQueue` 表：`id, question_code, session_id, answer_id, judge_raw: JSON, reason: str, status: str="open", resolved_score: int|None, created_at`）
- Modify: `api/app/db.py` 不动（create_all 自动建新表）
- Modify: `api/app/judge/pipeline.py`（`enqueue_review(db, question_code, session_id, answer_id, judge_raw, reason)` 薄封装）
- Test: `api/tests/test_review_queue.py`（建表、入队幂等场景、status 流转）

- [ ] Step 1-5 同 TDD 循环；commit `feat: 人工复核队列模型与入队`

---

### Task 4: 引擎触顶停止规则

**Files:**
- Modify: `api/app/engine/adaptive.py`（`DimensionState.max_answered: int = 0`；`update` 维护；`should_stop(state, ceiling: int | None = None)`；`from_dict` 兼容默认 0）
- Modify: `api/app/api/session_routes.py`（`_done_dimensions`/`_session_view` 计算每维度 ceiling=该维度已发布客观题最大难度，传入 should_stop）
- Test: `api/tests/test_engine.py`（新增：max_answered 维护、ceiling 未触顶时 streak≥2 不停、触顶后停、ceiling=None 保持 M1 行为、序列化 roundtrip 含新字段）；`test_session_flow.py`（强学员连对路径会出 d4+ 题目——fixture 需给每维度至少 1 道 d4 客观题）

- [ ] Step 1-5 TDD；commit `feat: 引擎连对停止增加难度触顶条件`

---

### Task 5: 题库扩容 24 道高难客观题（90 → 114）

**Files:**
- Modify: `seeds/questions/D1.json` ~ `D6.json`（各 +4 题：`D{n}-H01..H04`，tier=advanced，type=single/multi/judge，难度 4-5 且每维 ≥1 道 d5，explanation 必填）
- Modify: `api/tests/test_bank_content.py`（总数 114；advanced 断言放宽为 ≥5 且含 ≥1 practical + ≥4 客观高难题；难度分布断言）

**内容基调**（逐题对照，审查抽查）：多步辨析、反直觉边界、陷阱识别、跨概念取舍——例：思维链在单步任务上反而劣化的辨析；RAG vs 微调的成本/效果/时效取舍；温度与输出随机性/幻觉的关系辨析；系统提示词注入风险的识别与防御；多模型答案分歧时的仲裁策略；上下文窗口溢出对长文档任务的影响及处置。干扰项须来自"看起来合理的半对认知"。

- [ ] Step 1 更新测试跑 RED → Step 2 写题 → Step 3 `pytest tests/test_bank_content.py` GREEN + `python -m app.seed` 输出 `{'created': 24, 'updated': 0}`（增量导入）→ Step 4 全量 pytest → Step 5 commit `feat: 题库扩容 24 道高难客观题（114 题）`

---

### Task 6: 报告逐题回显与 LLM 学习路径

**Files:**
- Modify: `api/app/report/generate.py`（build_report 增 `answers` 回显数组：`{seq, dimension, type, stem_head(≤60字), is_correct|score, explanation|rationale, theta_after}`；末尾 `generate_llm_advice(dimensions, gaps, chat_fn=None) -> list[str]`——chat_fn 为 None 或调用/解析失败 → 返回既有模板；成功则返回 1-3 条个性化建议（中文，含短板归因+可执行行动））
- Modify: `api/app/api/report_routes.py`（ReportOut 加 `answers`、`advice_source: "llm"|"template"`）
- Modify: `api/app/api/session_routes.py`（finish 传入 provider 包装的 chat_fn；无 key/异常自动降级）
- Test: `api/tests/test_report.py`（mock chat_fn：LLM 建议成功路径 + 失败回退路径 + answers 数组形状）
- Modify: `web/src/pages/ReportPage.tsx`（逐题回显按维度折叠卡；建议区显示来源徽章）
- Test: `web/src/pages/ReportPage.test.tsx` 可选（vitest 组件冒烟）；门禁 `pnpm build`

- [ ] Step 1-5 TDD；commit `feat: 报告逐题回显与 LLM 个性化学习路径（模板兜底）`

---

### Task 7: 真实 LLM 冒烟脚本（不入 pytest）

**Files:**
- Create: `api/scripts/smoke_llm.py`（读 .env → 对 1 道 D2 进阶题跑真实 judge_answer → 打印 JudgeResult；`python scripts/smoke_llm.py` 手动执行）
- Create: `api/scripts/__init__.py`（若需要）

- [ ] 实现后手动执行一次并把输出贴进任务报告（这是唯一触达真实 API 的验证）；commit `chore: 真实 LLM 冒烟脚本`

---

## Self-Review 记录

- Spec 覆盖：spec §3→Task 1、§4→Task 2+3、§5.1→Task 5、§5.2→Task 4、§7（回显+LLM 建议）→Task 6；§5.3/§6/§8/§9/§10 属 M2b-d，不在本计划。
- 占位符：无（测试用例名描述了断言意图，代码块给关键用例；实现代码由执行者按接口写——本计划接口签名完整）。
- 类型一致性：`chat_fn` 签名在 Task 1/2/6 一致；`JudgeResult` 字段贯穿 Task 2/3/6；`ceiling` 语义 Task 4 内自洽（None=保持 M1 行为）。
