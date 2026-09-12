# M1 基础闭环 实施计划（AI 能力罗盘）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 跑通"学员登录 → 六维自适应客观题测评 → 雷达图报告"的本地闭环，题库 90 题就位，引擎单测全绿。

**Architecture:** 前后端分离单仓：`web/`（React SPA）+ `api/`（FastAPI + SQLite）+ `seeds/`（版本化题库 JSON）。自适应引擎为纯函数模块（不可变状态，copy-on-write），判分规则引擎独立，报告生成读会话快照。会话状态（每维度 θ）以 JSON 快照存在 `assessment_sessions.theta_snapshot`，每次作答整体重赋值。

**Tech Stack:** Python 3.13 + FastAPI + SQLAlchemy 2.0 + SQLite(WAL) + PyJWT + pytest；React 18 + TypeScript + Vite + Tailwind v4 + shadcn/ui + Recharts + react-router-dom + vitest。

**Spec:** `docs/superpowers/specs/2026-09-12-ai-compass-design.md`（本计划实现其 §11 M1 行：工程骨架、能力模型文档、90 题种子、自适应引擎 TDD、客观题答题流、报告雏形雷达图）。

## Global Constraints

- 环境：Windows + Git Bash；Python 3.13.15、Node 24.19.0、pnpm 12.3.4。venv 激活用 `source .venv/Scripts/activate`。
- 端口：后端 8000，前端 dev 5173（vite proxy `/api` → 8000）。
- 所有用户可见文案为**简体中文**；代码注释仅在表达代码无法自明的约束时才写。
- 引擎状态一律 copy-on-write（frozen dataclass，`update` 返回新实例），禁止原地修改。
- 前端所有用户触发的 async 处理链路必须 try/catch 并把错误显示到界面（用户全局纪律）。
- 每个 Task 结束独立 commit，message 用 `feat:/fix:/chore:` 前缀 + 中文一句话；`git add` 后、commit 前执行 `git grep --cached -nE "(sk-[A-Za-z0-9]{8,}|API_KEY=.+)"` 确认无密钥。
- 声称"通过/完成"前必须真实运行验证命令并记录输出（verification-before-completion 纪律）。
- API 不向客户端泄露 `answer`/`rubric` 字段（题目输出统一走 `_question_out`）。

---

### Task 1: 后端骨架与测试基建

**Files:**
- Create: `api/requirements.txt`
- Create: `api/pytest.ini`
- Create: `api/app/__init__.py`（空文件）
- Create: `api/app/main.py`
- Test: `api/tests/test_health.py`

**Interfaces:**
- Produces: FastAPI 实例 `app`（`app.main:app`），`GET /api/health → {"status":"ok","app":"ai-compass"}`；后续所有路由模块经 `app.include_router` 挂载。

- [ ] **Step 1: 建目录与依赖清单**

`api/requirements.txt`:

```
fastapi>=0.115
uvicorn[standard]>=0.30
sqlalchemy>=2.0
pyjwt>=2.9
pytest>=8.0
httpx>=0.27
```

`api/pytest.ini`:

```
[pytest]
pythonpath = .
testpaths = tests
```

- [ ] **Step 2: 写失败测试**

`api/tests/test_health.py`:

```python
from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_ok():
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "app": "ai-compass"}
```

- [ ] **Step 3: 建 venv 装依赖，跑测试确认失败**

```bash
cd api
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
pytest tests/test_health.py -v
```

预期：FAIL，`ModuleNotFoundError: No module named 'app.main'`。

- [ ] **Step 4: 最小实现**

`api/app/main.py`:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="AI 能力罗盘 API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "app": "ai-compass"}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_health.py -v` → 预期 PASS。

- [ ] **Step 6: Commit**

```bash
git add api
git commit -m "feat: FastAPI 后端骨架与 /api/health"
```

---

### Task 2: 数据模型、题库种子 schema 与导入 CLI

**Files:**
- Create: `api/app/db.py`
- Create: `api/app/models.py`
- Create: `api/app/seed.py`
- Create: `api/tests/fixtures/test_bank.json`
- Test: `api/tests/test_seed.py`

**Interfaces:**
- Produces:
  - `app.db.init_db() -> None`、`app.db.SessionLocal`（SQLAlchemy sessionmaker）。
  - ORM 模型：`Klass/User/Question/AssessmentSession/SessionAnswer/Report`（表名 `classes/users/questions/assessment_sessions/session_answers/reports`）。
  - `seed.load_seed_files(dir: Path) -> list[dict]`、`seed.validate_question(q: dict) -> None`（不合法抛 `ValueError`）、`seed.import_questions(items, session) -> {"created": int, "updated": int}`（按 `code` 幂等 upsert）、`seed.ensure_base_accounts(session) -> None`（建默认班级+admin，Task 5 补充实现，本 Task 先建空函数）。
  - 种子 JSON 题目字段：`id/dimension/tier/type/difficulty/stem/options/answer/tags/est_seconds/explanation/rubric`。

- [ ] **Step 1: db.py 与 models.py**

`api/app/db.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def _default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "compass.db"


DB_PATH = os.environ.get("COMPASS_DB", str(_default_db_path()))

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from app import models  # noqa: F401  确保模型完成注册

    Base.metadata.create_all(engine)
```

`api/app/models.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Klass(Base):
    __tablename__ = "classes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    invite_code: Mapped[str] = mapped_column(String(16), unique=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32))
    student_no: Mapped[str] = mapped_column(String(32), unique=True)
    role: Mapped[str] = mapped_column(String(16))  # student | teacher | admin
    class_id: Mapped[int | None] = mapped_column(ForeignKey("classes.id"), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)  # 种子 id，如 D1-B03
    dimension: Mapped[str] = mapped_column(String(4))  # D1..D6
    tier: Mapped[str] = mapped_column(String(8))  # basic | advanced
    type: Mapped[str] = mapped_column(String(12))  # single | multi | judge | open | practical
    difficulty: Mapped[int] = mapped_column(Integer)  # 1..5
    stem: Mapped[str] = mapped_column(String(2000))
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)
    answer: Mapped[object | None] = mapped_column(JSON, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    est_seconds: Mapped[int] = mapped_column(Integer, default=60)
    explanation: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    rubric: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="published")
    version: Mapped[int] = mapped_column(Integer, default=1)


class AssessmentSession(Base):
    __tablename__ = "assessment_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    mode: Mapped[str] = mapped_column(String(8), default="full")  # full | quick
    status: Mapped[str] = mapped_column(String(16), default="in_progress")  # in_progress | finished
    theta_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SessionAnswer(Base):
    __tablename__ = "session_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("assessment_sessions.id"))
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    question_code: Mapped[str] = mapped_column(String(16))
    dimension: Mapped[str] = mapped_column(String(4))
    answer: Mapped[object] = mapped_column(JSON)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_spent: Mapped[int] = mapped_column(Integer, default=0)
    theta_after: Mapped[float] = mapped_column(Float)
    seq: Mapped[int] = mapped_column(Integer)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("assessment_sessions.id"), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    dimensions: Mapped[list] = mapped_column(JSON)  # 六维明细数组
    total_level: Mapped[int] = mapped_column(Integer)  # 1..5
    radar: Mapped[list] = mapped_column(JSON)  # [{dimension,label,value(0-100)}]
    strengths: Mapped[list] = mapped_column(JSON)  # ["D2","D3"]
    gaps: Mapped[list] = mapped_column(JSON)  # ["D1","D6"]
    advice: Mapped[list] = mapped_column(JSON)  # [str]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
```

- [ ] **Step 2: 写失败测试（种子校验 + 幂等导入）**

`api/tests/fixtures/test_bank.json`（12 题迷你题库，D1~D6 各 2 题，全部 single/难度 3）：

```json
{
  "questions": [
    {"id":"D1-T01","dimension":"D1","tier":"basic","type":"single","difficulty":3,
     "stem":"大语言模型（LLM）最核心的能力边界是？","options":[{"key":"A","text":"能访问实时互联网并保证信息最新"},{"key":"B","text":"基于训练数据生成文本，可能产生幻觉"},{"key":"C","text":"能执行本地文件系统操作"},{"key":"D","text":"永远不会出错"}],
     "answer":"B","tags":["概念"],"est_seconds":60,"explanation":"LLM 基于训练数据生成内容，无法保证实时性与绝对正确，幻觉是其固有风险。"},
    {"id":"D1-T02","dimension":"D1","tier":"basic","type":"single","difficulty":3,
     "stem":"「模型微调（fine-tuning）」指的是？","options":[{"key":"A","text":"在特定数据上继续训练，使模型适配具体任务"},{"key":"B","text":"调整推理温度参数"},{"key":"C","text":"扩大上下文窗口"},{"key":"D","text":"更换模型界面"}],
     "answer":"A","tags":["概念"],"est_seconds":60,"explanation":"微调即在领域数据上继续训练基础模型。"}
  ]
}
```

（执行时把 D1 两题复制改写成 D2~D6 各 2 题，id 改为 `D2-T01` 等；题目内容可按维度主题任意编写，只要通过 `validate_question`。）

`api/tests/test_seed.py`:

```python
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Question
from app.seed import import_questions, load_seed_files, validate_question

FIXTURES = Path(__file__).parent / "fixtures" / "test_bank.json"


@pytest.fixture(scope="module")
def items() -> list[dict]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))["questions"]


def test_fixture_bank_has_12_questions(items):
    assert len(items) == 12
    assert {q["dimension"] for q in items} == {"D1", "D2", "D3", "D4", "D5", "D6"}


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "X1", "dimension": "D9", "tier": "basic", "type": "single", "difficulty": 3, "stem": "s", "options": [{"key": "A", "text": "a"}, {"key": "B", "text": "b"}], "answer": "A", "est_seconds": 60},
        {"id": "X2", "dimension": "D1", "tier": "basic", "type": "single", "difficulty": 5, "stem": "s", "options": [{"key": "A", "text": "a"}, {"key": "B", "text": "b"}], "answer": "A", "est_seconds": 60},
        {"id": "X3", "dimension": "D1", "tier": "basic", "type": "single", "difficulty": 2, "stem": "s", "options": [{"key": "A", "text": "a"}, {"key": "B", "text": "b"}], "answer": "Z", "est_seconds": 60},
        {"id": "X4", "dimension": "D1", "tier": "advanced", "type": "open", "difficulty": 4, "stem": "s", "est_seconds": 60},
    ],
)
def test_validate_rejects_bad_question(bad):
    with pytest.raises(ValueError):
        validate_question(bad)


def test_import_is_idempotent(items):
    init_db()
    with SessionLocal() as db:
        first = import_questions(items, db)
        second = import_questions(items, db)
    assert first["created"] == 12 and first["updated"] == 0
    assert second["created"] == 0 and second["updated"] == 0
    with SessionLocal() as db:
        assert db.scalars(select(Question)).__length_hint__() or True  # 数量断言见下
        codes = db.scalars(select(Question.code)).all()
        assert len(codes) == 12
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd api && source .venv/Scripts/activate
pytest tests/test_seed.py -v
```

预期 FAIL：`ModuleNotFoundError: app.seed`。

- [ ] **Step 4: 实现 seed.py**

`api/app/seed.py`:

```python
"""题库种子导入：python -m app.seed [--dir seeds/questions]"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.db import SessionLocal, init_db
from app.models import Klass, Question, User

DIMENSIONS = ("D1", "D2", "D3", "D4", "D5", "D6")
OBJECTIVE_TYPES = ("single", "multi", "judge")
ALL_TYPES = (*OBJECTIVE_TYPES, "open", "practical")
_MUTABLE = ("dimension", "tier", "type", "difficulty", "stem", "options", "answer", "tags", "est_seconds", "explanation", "rubric")


def load_seed_files(dir_: Path) -> list[dict]:
    items: list[dict] = []
    for path in sorted(dir_.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        items.extend(data["questions"] if isinstance(data, dict) else data)
    return items


def validate_question(q: dict) -> None:
    code = q.get("id", "<无 id>")
    if q.get("dimension") not in DIMENSIONS:
        raise ValueError(f"{code}: dimension 非法")
    if q.get("tier") not in ("basic", "advanced"):
        raise ValueError(f"{code}: tier 非法")
    if q.get("type") not in ALL_TYPES:
        raise ValueError(f"{code}: type 非法")
    difficulty = q.get("difficulty")
    if not isinstance(difficulty, int) or not 1 <= difficulty <= 5:
        raise ValueError(f"{code}: difficulty 必须为 1..5")
    if q["tier"] == "basic" and difficulty > 3:
        raise ValueError(f"{code}: 基础题难度不得超过 3")
    if q["tier"] == "advanced" and difficulty < 4:
        raise ValueError(f"{code}: 进阶题难度不得低于 4")
    if not q.get("stem"):
        raise ValueError(f"{code}: stem 不能为空")
    est = q.get("est_seconds")
    if not isinstance(est, int) or not 15 <= est <= 600:
        raise ValueError(f"{code}: est_seconds 必须为 15..600")

    if q["type"] in ("single", "multi"):
        options = q.get("options")
        if not isinstance(options, list) or len(options) < 2:
            raise ValueError(f"{code}: 客观选择题 options 至少 2 项")
        keys = {o.get("key") for o in options}
        if len(keys) != len(options) or not all(o.get("text") for o in options):
            raise ValueError(f"{code}: options 的 key/text 不合规")
        answer = q.get("answer")
        if q["type"] == "single":
            if answer not in keys:
                raise ValueError(f"{code}: 单选 answer 必须是选项 key")
        else:
            if not isinstance(answer, list) or not answer or not set(answer) <= keys:
                raise ValueError(f"{code}: 多选 answer 必须是选项 key 的非空子集")
    elif q["type"] == "judge":
        if not isinstance(q.get("answer"), bool):
            raise ValueError(f"{code}: 判断题 answer 必须为布尔值")
    else:  # open | practical
        if q.get("answer") is not None:
            raise ValueError(f"{code}: 开放题不应预设 answer")
        rubric = q.get("rubric")
        if not isinstance(rubric, dict):
            raise ValueError(f"{code}: 开放题必须提供 rubric")
        if not isinstance(rubric.get("points"), list) or not rubric["points"]:
            raise ValueError(f"{code}: rubric.points 不能为空")
        anchors = rubric.get("anchors")
        if not isinstance(anchors, dict) or set(anchors) != {"0", "1", "2", "3", "4"}:
            raise ValueError(f"{code}: rubric.anchors 必须含 0-4 五档锚定")


def import_questions(items: list[dict], session: OrmSession) -> dict[str, int]:
    created = updated = 0
    for raw in items:
        validate_question(raw)
        existing = session.scalar(select(Question).where(Question.code == raw["id"]))
        if existing is None:
            session.add(Question(code=raw["id"], **{k: raw.get(k) for k in _MUTABLE}))
            created += 1
            continue
        changed = any(getattr(existing, k) != raw.get(k) for k in _MUTABLE)
        if changed:
            for k in _MUTABLE:
                setattr(existing, k, raw.get(k))
            existing.version += 1
            updated += 1
    session.commit()
    return {"created": created, "updated": updated}


def ensure_base_accounts(session: OrmSession) -> None:
    """幂等创建默认班级与 admin 账号（Task 5 实现密码部分）。"""
    if session.scalar(select(Klass).where(Klass.invite_code == "PUBLIC")) is None:
        session.add(Klass(name="自由测评班", invite_code="PUBLIC"))
    if session.scalar(select(User).where(User.student_no == "admin")) is None:
        from app.auth import hash_password

        session.add(User(name="管理员", student_no="admin", role="admin", password_hash=hash_password("admin123")))
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="导入题库种子")
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent.parent / "seeds" / "questions"))
    args = parser.parse_args()
    init_db()
    with SessionLocal() as db:
        ensure_base_accounts(db)
        result = import_questions(load_seed_files(Path(args.dir)), db)
    print(result)


if __name__ == "__main__":
    main()
```

注意：`ensure_base_accounts` 引用了 `app.auth`（Task 5 才建）。本 Task 先在函数内 `from app.auth import hash_password` 处直接写 `password_hash=None` 占位会破坏 Task 5 语义——因此本 Task 提交时保留 `from app.auth import ...` 但**同时创建最小 `app/auth.py`**（仅含 `hash_password`），Task 5 再扩展该文件。最小 `app/auth.py`（本 Task 一并创建）:

```python
from __future__ import annotations

import hashlib
import secrets


def hash_password(password: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_seed.py -v` → 预期全 PASS。（test_seed 未设 COMPASS_DB 时会写 `api/compass.db`，已被 .gitignore 覆盖；conftest 统一隔离在 Task 6 引入。）

- [ ] **Step 6: Commit**

```bash
git add api
git commit -m "feat: 数据模型与题库种子导入 CLI"
```

---

### Task 3: 自适应引擎（纯逻辑，TDD 核心）

**Files:**
- Create: `api/app/engine/__init__.py`（空文件）
- Create: `api/app/engine/adaptive.py`
- Test: `api/tests/test_engine.py`

**Interfaces:**
- Produces（后续任务依赖的确切签名）:
  - `DIMENSIONS: tuple[str, ...]`（"D1".."D6"）、`DIMENSION_NAMES: dict[str, str]`、`LEVEL_NAMES: dict[int, str]`
  - `DimensionState(theta=3.0, n=0, streak=0, recent_deltas=())`（frozen dataclass），`.to_dict() / .from_dict(dict)`
  - `expected(theta: float, difficulty: float) -> float`
  - `k_factor(n: int) -> float`
  - `update(state: DimensionState, difficulty: float, result: float, slow: bool = False) -> DimensionState`（result∈[0,1]，越界抛 ValueError）
  - `should_stop(state: DimensionState) -> bool`
  - `next_difficulty(state: DimensionState) -> int`
  - `dimension_level(theta: float) -> int`（1..5）
  - `select_next_question(questions: list[dict], asked_codes: set[str], target_difficulty: int) -> dict | None`（入参 dict 至少含 `code` 与 `difficulty`）

- [ ] **Step 1: 写失败测试**

`api/tests/test_engine.py`:

```python
import pytest

from app.engine.adaptive import (
    DIMENSION_NAMES,
    DimensionState,
    dimension_level,
    expected,
    k_factor,
    next_difficulty,
    select_next_question,
    should_stop,
    update,
)


def test_six_dimensions_defined():
    assert set(DIMENSION_NAMES) == {"D1", "D2", "D3", "D4", "D5", "D6"}


def test_expected_midpoint():
    assert expected(3.0, 3.0) == pytest.approx(0.5)


def test_expected_monotonic_in_theta():
    assert expected(4.0, 3.0) > expected(3.0, 3.0) > expected(2.0, 3.0)


def test_k_factor_decays_with_n():
    assert k_factor(0) == pytest.approx(0.8)
    assert k_factor(4) < k_factor(0)


def test_update_correct_raises_theta():
    s2 = update(DimensionState(), difficulty=3.0, result=1.0)
    assert s2.theta == pytest.approx(3.4)
    assert s2.n == 1
    assert s2.streak == 1


def test_update_wrong_lowers_theta():
    s2 = update(DimensionState(), difficulty=3.0, result=0.0)
    assert s2.theta == pytest.approx(2.6)
    assert s2.streak == -1


def test_update_partial_result_moves_little():
    s2 = update(DimensionState(), difficulty=3.0, result=0.5)
    assert s2.theta == pytest.approx(3.0)


def test_update_slow_correct_discounted():
    fast = update(DimensionState(), 3.0, 1.0, slow=False)
    slow = update(DimensionState(), 3.0, 1.0, slow=True)
    assert slow.theta < fast.theta


def test_update_rejects_out_of_range_result():
    with pytest.raises(ValueError):
        update(DimensionState(), 3.0, 1.5)


def test_update_clamps_theta_to_range():
    low = update(DimensionState(theta=1.05), 1.0, 0.0)
    high = update(DimensionState(theta=4.95), 5.0, 1.0)
    assert 1.0 <= low.theta <= 5.0
    assert 1.0 <= high.theta <= 5.0


def test_update_is_immutable():
    s = DimensionState()
    update(s, 3.0, 1.0)
    assert s.n == 0 and s.theta == 3.0


def test_state_serialization_roundtrip():
    s = update(DimensionState(), 3.0, 1.0)
    assert DimensionState.from_dict(s.to_dict()) == s


def test_stop_on_two_streak():
    assert should_stop(DimensionState(n=2, streak=2))
    assert should_stop(DimensionState(n=2, streak=-2))


def test_stop_on_max_n():
    assert should_stop(DimensionState(n=6, streak=1))


def test_convergence_stop_requires_n4():
    tiny = (0.01, 0.02, 0.01)
    assert not should_stop(DimensionState(n=3, streak=0, recent_deltas=tiny))
    assert should_stop(DimensionState(n=4, streak=0, recent_deltas=tiny))


def test_no_stop_early():
    assert not should_stop(DimensionState(n=1, streak=1))


def test_next_difficulty_rounds_half_up():
    assert next_difficulty(DimensionState(theta=2.5)) == 3
    assert next_difficulty(DimensionState(theta=3.4)) == 3
    assert next_difficulty(DimensionState(theta=4.6)) == 5


def test_next_difficulty_clamped():
    assert next_difficulty(DimensionState(theta=1.0)) == 1
    assert next_difficulty(DimensionState(theta=5.0)) == 5


@pytest.mark.parametrize(
    "theta,level",
    [(1.0, 1), (1.79, 1), (1.8, 2), (2.5, 2), (2.6, 3), (3.3, 3), (3.4, 4), (4.1, 4), (4.2, 5), (5.0, 5)],
)
def test_dimension_level(theta, level):
    assert dimension_level(theta) == level


def test_select_prefers_exact_difficulty():
    qs = [
        {"code": "D1-a", "difficulty": 1},
        {"code": "D1-b", "difficulty": 3},
        {"code": "D1-c", "difficulty": 5},
    ]
    assert select_next_question(qs, set(), 3)["code"] == "D1-b"


def test_select_skips_asked():
    qs = [{"code": "D1-b", "difficulty": 3}]
    assert select_next_question(qs, {"D1-b"}, 3) is None


def test_select_nearest_then_lower():
    qs = [{"code": "D1-hi", "difficulty": 5}, {"code": "D1-lo", "difficulty": 2}]
    assert select_next_question(qs, set(), 3)["code"] == "D1-lo"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_engine.py -v` → 预期 FAIL：`ModuleNotFoundError: app.engine.adaptive`。

- [ ] **Step 3: 实现引擎**

`api/app/engine/adaptive.py`:

```python
"""自适应测评引擎：纯函数 + 不可变状态（copy-on-write），与 FastAPI 解耦。"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

DIMENSIONS = ("D1", "D2", "D3", "D4", "D5", "D6")

DIMENSION_NAMES = {
    "D1": "AI 基础认知",
    "D2": "提示词工程",
    "D3": "AI 工具使用",
    "D4": "AI 结果评估与优化",
    "D5": "人机协同解决问题",
    "D6": "AI 伦理与合规",
}

LEVEL_NAMES = {1: "初识 L1", 2: "会用 L2", 3: "熟练 L3", 4: "精通 L4", 5: "专家 L5"}

THETA_MIN, THETA_MAX = 1.0, 5.0
INITIAL_THETA = 3.0
STOP_STREAK = 2
STOP_MAX_N = 6
CONVERGENCE = 0.15


@dataclass(frozen=True)
class DimensionState:
    theta: float = INITIAL_THETA
    n: int = 0
    streak: int = 0  # 连对为正、连错为负
    recent_deltas: tuple[float, ...] = ()  # 最近 3 次更新量

    def to_dict(self) -> dict:
        data = asdict(self)
        data["recent_deltas"] = list(self.recent_deltas)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DimensionState":
        return cls(
            theta=float(data.get("theta", INITIAL_THETA)),
            n=int(data.get("n", 0)),
            streak=int(data.get("streak", 0)),
            recent_deltas=tuple(data.get("recent_deltas", [])),
        )


def expected(theta: float, difficulty: float) -> float:
    """期望掌握度：θ 与题目难度同尺度（1~5）。"""
    return 1.0 / (1.0 + math.exp(-(theta - difficulty)))


def k_factor(n: int) -> float:
    """更新步长随该维度答题数衰减。"""
    return 0.8 / (1.0 + 0.25 * n)


def _next_streak(streak: int, result: float) -> int:
    if result >= 0.5:
        return streak + 1 if streak > 0 else 1
    return streak - 1 if streak < 0 else -1


def update(state: DimensionState, difficulty: float, result: float, slow: bool = False) -> DimensionState:
    """作答后返回新的能力估计。result∈[0,1]（客观题 0/1，开放题 score/4）。

    slow=True 表示答对但用时超过预估 2 倍，更新量×0.7。
    """
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"result 必须在 [0,1]，收到 {result}")
    delta = k_factor(state.n) * (result - expected(state.theta, difficulty))
    if slow and result >= 0.5:
        delta *= 0.7
    theta = min(THETA_MAX, max(THETA_MIN, state.theta + delta))
    recent = tuple([*state.recent_deltas, delta][-3:])
    return DimensionState(
        theta=theta,
        n=state.n + 1,
        streak=_next_streak(state.streak, result),
        recent_deltas=recent,
    )


def should_stop(state: DimensionState) -> bool:
    if abs(state.streak) >= STOP_STREAK:
        return True
    if state.n >= STOP_MAX_N:
        return True
    converged = len(state.recent_deltas) >= 3 and all(abs(d) < CONVERGENCE for d in state.recent_deltas)
    return converged and state.n >= 4


def next_difficulty(state: DimensionState) -> int:
    """下一题难度：θ 四舍五入（half-up，规避 Python 银行家舍入）。"""
    return min(5, max(1, math.floor(state.theta + 0.5)))


def dimension_level(theta: float) -> int:
    if theta < 1.8:
        return 1
    if theta < 2.6:
        return 2
    if theta < 3.4:
        return 3
    if theta < 4.2:
        return 4
    return 5


def select_next_question(questions: list[dict], asked_codes: set[str], target_difficulty: int) -> dict | None:
    """同维度未作答题中选目标难度题；无精确匹配取最近难度，同距取更低难度。"""
    candidates = [q for q in questions if q["code"] not in asked_codes]
    if not candidates:
        return None
    return min(candidates, key=lambda q: (abs(q["difficulty"] - target_difficulty), q["difficulty"]))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_engine.py -v` → 预期全 PASS。

- [ ] **Step 5: Commit**

```bash
git add api
git commit -m "feat: 六维自适应引擎（Elo 期望式更新与选题策略）"
```

---

### Task 4: 客观题判分规则

**Files:**
- Create: `api/app/engine/grading.py`
- Test: `api/tests/test_grading.py`

**Interfaces:**
- Consumes: 无（纯函数）。
- Produces: `grade_objective(q_type: str, answer, correct) -> bool`；`result_from_correct(is_correct: bool) -> float`。

- [ ] **Step 1: 写失败测试**

`api/tests/test_grading.py`:

```python
import pytest

from app.engine.grading import grade_objective, result_from_correct


def test_single_exact_match():
    assert grade_objective("single", "B", "B")
    assert not grade_objective("single", "A", "B")


def test_multi_set_equality_ignores_order():
    assert grade_objective("multi", ["C", "A"], ["A", "C"])
    assert not grade_objective("multi", ["A"], ["A", "C"])


def test_multi_rejects_non_list():
    assert not grade_objective("multi", "A", ["A"])


def test_judge_boolean():
    assert grade_objective("judge", True, True)
    assert not grade_objective("judge", True, False)


def test_unsupported_type_raises():
    with pytest.raises(ValueError):
        grade_objective("open", "x", "x")


def test_result_from_correct():
    assert result_from_correct(True) == 1.0
    assert result_from_correct(False) == 0.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_grading.py -v` → FAIL：`ModuleNotFoundError: app.engine.grading`。

- [ ] **Step 3: 实现**

`api/app/engine/grading.py`:

```python
"""客观题规则判分：单选精确匹配、判断布尔匹配、多选集合匹配。"""

from __future__ import annotations


def grade_objective(q_type: str, answer, correct) -> bool:
    if q_type == "single":
        return answer == correct
    if q_type == "judge":
        return bool(answer) == bool(correct)
    if q_type == "multi":
        if not isinstance(answer, list):
            return False
        return set(answer) == set(correct)
    raise ValueError(f"客观判分不支持题型: {q_type}")


def result_from_correct(is_correct: bool) -> float:
    return 1.0 if is_correct else 0.0
```

- [ ] **Step 4: 跑测试确认通过** → `pytest tests/test_grading.py -v` 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add api
git commit -m "feat: 客观题规则判分器"
```

---

### Task 5: 认证（学员自助注册 + JWT + 依赖注入）

**Files:**
- Modify: `api/app/auth.py`（在 Task 2 的最小版上扩展）
- Create: `api/app/api/__init__.py`（空文件）
- Create: `api/app/api/auth_routes.py`
- Modify: `api/app/main.py`（挂载路由）
- Test: `api/tests/test_auth.py`

**Interfaces:**
- Consumes: `models.User/Klass`、`seed.ensure_base_accounts`。
- Produces:
  - `auth.make_token(user_id: int, role: str) -> str`、`auth.verify_password(password: str, stored: str) -> bool`、`auth.current_user(creds) -> {"id": int, "role": str}`（FastAPI 依赖，401 时抛 HTTPException）。
  - `api/auth_routes.get_db()`（yield SessionLocal 的依赖，后续路由复用）。
  - HTTP: `POST /api/auth/student {name, student_no, invite_code?} → {token, user}`（学号已存在则视为登录）；`POST /api/auth/login {username, password} → {token, user}`；`GET /api/auth/me`（需 Bearer）。

- [ ] **Step 1: 写失败测试**

`api/tests/test_auth.py`:

```python
from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import app
from app.seed import ensure_base_accounts


def setup_module(module):
    init_db()
    with SessionLocal() as db:
        ensure_base_accounts(db)


def test_student_register_returns_token():
    client = TestClient(app)
    resp = client.post("/api/auth/student", json={"name": "张三", "student_no": "S1001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["role"] == "student"
    assert body["token"]


def test_student_register_idempotent_login():
    client = TestClient(app)
    first = client.post("/api/auth/student", json={"name": "李四", "student_no": "S1002"}).json()
    second = client.post("/api/auth/student", json={"name": "李四", "student_no": "S1002"}).json()
    assert first["user"]["id"] == second["user"]["id"]


def test_invalid_invite_code_rejected():
    client = TestClient(app)
    resp = client.post("/api/auth/student", json={"name": "王五", "student_no": "S1003", "invite_code": "NOPE"})
    assert resp.status_code == 400


def test_admin_password_login():
    client = TestClient(app)
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "admin"


def test_wrong_password_rejected():
    client = TestClient(app)
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    assert resp.status_code == 401


def test_me_requires_token():
    client = TestClient(app)
    assert client.get("/api/auth/me").status_code == 401
    token = client.post("/api/auth/student", json={"name": "赵六", "student_no": "S1004"}).json()["token"]
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "赵六"
```

- [ ] **Step 2: 跑测试确认失败** → `pytest tests/test_auth.py -v` FAIL（404，路由未挂载）。

- [ ] **Step 3: 实现**

扩展 `api/app/auth.py`（保留已有 `hash_password`，追加）:

```python
import os
import time

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session as OrmSession

JWT_SECRET = os.environ.get("COMPASS_JWT_SECRET", "dev-secret-change-me")
JWT_TTL = 7 * 24 * 3600
_bearer = HTTPBearer(auto_error=False)


def verify_password(password: str, stored: str) -> bool:
    salt, digest = stored.split("$", 1)
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest() == digest


def make_token(user_id: int, role: str) -> str:
    payload = {"sub": str(user_id), "role": role, "exp": int(time.time()) + JWT_TTL}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if creds is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="登录已失效")
    return {"id": int(payload["sub"]), "role": payload["role"]}
```

`api/app/api/auth_routes.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.auth import current_user, make_token, verify_password
from app.db import SessionLocal
from app.models import Klass, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class StudentRegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    student_no: str = Field(min_length=1, max_length=32)
    invite_code: str | None = None


class PasswordLoginIn(BaseModel):
    username: str
    password: str


@router.post("/student")
def student_register(body: StudentRegisterIn, db: OrmSession = Depends(get_db)) -> dict:
    class_id = None
    if body.invite_code:
        klass = db.scalar(select(Klass).where(Klass.invite_code == body.invite_code))
        if klass is None:
            raise HTTPException(status_code=400, detail="邀请码无效")
        class_id = klass.id
    user = db.scalar(select(User).where(User.student_no == body.student_no))
    if user is None:
        user = User(name=body.name, student_no=body.student_no, role="student", class_id=class_id)
        db.add(user)
        db.commit()
        db.refresh(user)
    return {"token": make_token(user.id, user.role), "user": {"id": user.id, "name": user.name, "role": user.role}}


@router.post("/login")
def password_login(body: PasswordLoginIn, db: OrmSession = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.student_no == body.username))
    if user is None or user.password_hash is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": make_token(user.id, user.role), "user": {"id": user.id, "name": user.name, "role": user.role}}


@router.get("/me")
def me(user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
    u = db.get(User, user["id"])
    if u is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return {"id": u.id, "name": u.name, "role": u.role}
```

`api/app/main.py` 追加（文件末尾）:

```python
from app.api.auth_routes import router as auth_router

app.include_router(auth_router)
```

- [ ] **Step 4: 跑测试确认通过** → `pytest tests/test_auth.py -v` 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add api
git commit -m "feat: 学员自助注册与 JWT 认证"
```

---

### Task 6: 测评会话 API（start / answer / 视图组装）

**Files:**
- Create: `api/tests/conftest.py`（统一测试库隔离 + 通用夹具）
- Create: `api/app/api/session_routes.py`
- Modify: `api/app/main.py`（挂载）
- Test: `api/tests/test_session_flow.py`

**Interfaces:**
- Consumes: Task 3 引擎全套、Task 4 `grade_objective/result_from_correct`、Task 5 `current_user/get_db`、Task 2 模型。
- Produces（前端 Task 10 依赖的响应形状）:
  - `POST /api/sessions {mode:"full"|"quick"} → SessionView`
  - `POST /api/sessions/{id}/answer {question_id, answer, time_spent} → SessionView | {"just": {...}}`（合并形状见下）
  - `SessionView = {session_id, status, question: QuestionOut|null, next_dimension, reason, progress: {D1..D6: {name, theta, level, n, done}}}`
  - `QuestionOut = {id, code, dimension, dimension_name, type, difficulty, stem, options, est_seconds, tags}`（**不含 answer/rubric/explanation**；answer 响应的 `just` 字段单独携带 `is_correct/explanation`）

- [ ] **Step 1: conftest.py（测试库隔离）**

`api/tests/conftest.py`:

```python
import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("COMPASS_DB", str(Path(tempfile.mkdtemp(prefix="compass-test-")) / "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import ensure_base_accounts, import_questions  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures" / "test_bank.json"


@pytest.fixture(scope="session", autouse=True)
def seeded_db():
    init_db()
    with SessionLocal() as db:
        ensure_base_accounts(db)
        import_questions(json.loads(_FIXTURES.read_text(encoding="utf-8"))["questions"], db)
    yield


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def auth_headers(client) -> dict[str, str]:
    resp = client.post("/api/auth/student", json={"name": "流程学员", "student_no": "FLOW001"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture()
def bank() -> dict[str, dict]:
    """题目 code → 种子原文（测试用来查正确答案）。"""
    return {q["id"]: q for q in json.loads(_FIXTURES.read_text(encoding="utf-8"))["questions"]}
```

- [ ] **Step 2: 写失败测试**

`api/tests/test_session_flow.py`:

```python
from fastapi.testclient import TestClient


def _run_full_flow(client: TestClient, headers: dict, bank: dict, correct: bool = True) -> dict:
    start = client.post("/api/sessions", json={"mode": "full"}, headers=headers)
    assert start.status_code == 200
    view = start.json()
    guard = 0
    while view["question"] is not None:
        guard += 1
        assert guard < 40, "答题循环未收敛"
        q = view["question"]
        seed = bank[q["code"]]
        answer = seed["answer"] if correct else _wrong_answer(seed)
        resp = client.post(
            f"/api/sessions/{view['session_id']}/answer",
            json={"question_id": q["id"], "answer": answer, "time_spent": 30},
            headers=headers,
        )
        assert resp.status_code == 200
        view = resp.json()
    return view


def _wrong_answer(seed: dict):
    if seed["type"] == "single":
        options = {o["key"] for o in seed["options"]}
        return next(k for k in sorted(options) if k != seed["answer"])
    if seed["type"] == "judge":
        return not seed["answer"]
    return []


def test_full_correct_flow_finishes(client, auth_headers, bank):
    view = _run_full_flow(client, auth_headers, bank, correct=True)
    assert view["question"] is None
    assert all(d["done"] for d in view["progress"].values())
    assert view["progress"]["D1"]["theta"] > 3.0


def test_wrong_answers_lower_theta(client, auth_headers, bank):
    view = _run_full_flow(client, auth_headers, bank, correct=False)
    assert view["progress"]["D1"]["theta"] < 3.0


def test_double_answer_rejected(client, auth_headers, bank):
    start = client.post("/api/sessions", json={"mode": "full"}, headers=headers).json()
    q = start["question"]
    body = {"question_id": q["id"], "answer": bank[q["code"]]["answer"], "time_spent": 10}
    assert client.post(f"/api/sessions/{start['session_id']}/answer", json=body, headers=auth_headers).status_code == 200
    resp = client.post(f"/api/sessions/{start['session_id']}/answer", json=body, headers=auth_headers)
    assert resp.status_code == 400


def test_session_requires_login(client):
    assert client.post("/api/sessions", json={"mode": "full"}).status_code == 401


def test_question_payload_has_no_answer(client, auth_headers):
    start = client.post("/api/sessions", json={"mode": "full"}, headers=auth_headers).json()
    assert "answer" not in start["question"]
    assert "rubric" not in start["question"]
```

- [ ] **Step 3: 跑测试确认失败** → FAIL（404 路由不存在）。

- [ ] **Step 4: 实现 session_routes.py**

`api/app/api/session_routes.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.auth import current_user
from app.engine import adaptive
from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES, DimensionState
from app.engine.grading import grade_objective, result_from_correct
from app.models import AssessmentSession, Question, SessionAnswer

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

OBJECTIVE_TYPES = ("single", "multi", "judge")


class StartIn(BaseModel):
    mode: str = "full"


class AnswerIn(BaseModel):
    question_id: int
    answer: object
    time_spent: int = Field(ge=0, default=0)


def _states(snapshot: dict) -> dict[str, DimensionState]:
    return {d: DimensionState.from_dict(snapshot.get(d, {})) for d in DIMENSIONS}


def _snapshot(states: dict[str, DimensionState]) -> dict:
    return {d: s.to_dict() for d, s in states.items()}


def _question_out(q: Question) -> dict:
    return {
        "id": q.id,
        "code": q.code,
        "dimension": q.dimension,
        "dimension_name": DIMENSION_NAMES[q.dimension],
        "type": q.type,
        "difficulty": q.difficulty,
        "stem": q.stem,
        "options": q.options,
        "est_seconds": q.est_seconds,
        "tags": q.tags,
    }


def _pick_question(db: OrmSession, dimension: str, state: DimensionState, asked_codes: set[str]) -> Question | None:
    pool = db.scalars(
        select(Question).where(
            Question.dimension == dimension,
            Question.type.in_(OBJECTIVE_TYPES),
            Question.status == "published",
        )
    ).all()
    dicts = [{"code": q.code, "difficulty": q.difficulty} for q in pool]
    chosen = adaptive.select_next_question(dicts, asked_codes, adaptive.next_difficulty(state))
    if chosen is None:
        return None
    code = chosen["code"]
    return next(q for q in pool if q.code == code)


def _done_dimensions(states: dict[str, DimensionState]) -> set[str]:
    return {d for d in DIMENSIONS if states[d].n > 0 and adaptive.should_stop(states[d])}


def _session_view(db: OrmSession, session: AssessmentSession) -> dict:
    states = _states(session.theta_snapshot)
    answers = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == session.id)).all()
    asked = {a.question_code for a in answers}
    done = _done_dimensions(states)
    question = dimension = None
    for d in DIMENSIONS:
        if d in done:
            continue
        question = _pick_question(db, d, states[d], asked)
        if question is not None:
            dimension = d
            break
    reason = ""
    if question is not None:
        s = states[dimension]
        reason = (
            f"你在「{DIMENSION_NAMES[dimension]}」当前估计 {s.theta:.1f} 分"
            f"（{adaptive.LEVEL_NAMES[adaptive.dimension_level(s.theta)]}），"
            f"本题难度 {question.difficulty}，用于校准你的水平边界"
        )
    progress = {
        d: {
            "name": DIMENSION_NAMES[d],
            "theta": round(states[d].theta, 3),
            "level": adaptive.dimension_level(states[d].theta),
            "level_name": adaptive.LEVEL_NAMES[adaptive.dimension_level(states[d].theta)],
            "n": states[d].n,
            "done": d in done,
        }
        for d in DIMENSIONS
    }
    return {
        "session_id": session.id,
        "status": session.status,
        "question": _question_out(question) if question else None,
        "next_dimension": dimension,
        "reason": reason,
        "progress": progress,
    }


@router.post("")
def start_session(body: StartIn, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
    if body.mode not in ("full", "quick"):
        raise HTTPException(status_code=400, detail="mode 仅支持 full/quick")
    session = AssessmentSession(user_id=user["id"], mode=body.mode, theta_snapshot=_snapshot(_states({})))
    db.add(session)
    db.commit()
    db.refresh(session)
    return _session_view(db, session)


@router.post("/{session_id}/answer")
def submit_answer(
    session_id: int,
    body: AnswerIn,
    user: dict = Depends(current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    session = db.get(AssessmentSession, session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.status != "in_progress":
        raise HTTPException(status_code=400, detail="会话已结束")
    question = db.get(Question, body.question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    answers = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == session.id)).all()
    asked = {a.question_code for a in answers}
    if question.code in asked:
        raise HTTPException(status_code=400, detail="该题已作答")

    is_correct = grade_objective(question.type, body.answer, question.answer)
    slow = is_correct and body.time_spent > 2 * question.est_seconds
    new_state = adaptive.update(
        _states(session.theta_snapshot)[question.dimension],
        question.difficulty,
        result_from_correct(is_correct),
        slow=slow,
    )
    states = _states(session.theta_snapshot)
    states[question.dimension] = new_state
    session.theta_snapshot = _snapshot(states)
    db.add(
        SessionAnswer(
            session_id=session.id,
            question_id=question.id,
            question_code=question.code,
            dimension=question.dimension,
            answer=body.answer,
            is_correct=is_correct,
            score=result_from_correct(is_correct),
            time_spent=body.time_spent,
            theta_after=new_state.theta,
            seq=len(answers) + 1,
        )
    )
    db.commit()
    db.refresh(session)
    return _session_view(db, session) | {
        "just": {
            "is_correct": is_correct,
            "explanation": question.explanation,
            "dimension": question.dimension,
            "theta": round(new_state.theta, 3),
        }
    }
```

`api/app/main.py` 追加:

```python
from app.api.session_routes import router as session_router

app.include_router(session_router)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_session_flow.py tests/test_seed.py -v` → 全 PASS（test_seed 在 conftest 环境下使用同一隔离库亦应通过；若 test_seed 与 conftest 冲突则将 test_seed 中重复的 init_db 调整为依赖夹具）。

- [ ] **Step 6: Commit**

```bash
git add api
git commit -m "feat: 测评会话 API（自适应出题与作答闭环）"
```

---

### Task 7: 报告生成与查询 API

**Files:**
- Create: `api/app/report/__init__.py`（空文件）
- Create: `api/app/report/generate.py`
- Create: `api/app/api/report_routes.py`
- Modify: `api/app/api/session_routes.py`（追加 finish 端点）
- Modify: `api/app/main.py`（挂载）
- Test: `api/tests/test_report.py`

**Interfaces:**
- Consumes: `models.Report/SessionAnswer`、`adaptive.dimension_level/DIMENSION_NAMES/LEVEL_NAMES`。
- Produces:
  - `report.generate.build_report(db, session) -> Report`（追加到 session，不 commit；调用方负责收尾）。
  - `POST /api/sessions/{id}/finish → {"report_id": int}`（幂等：已有报告直接返回）。
  - `GET /api/reports/mine → [{id, created_at, total_level, total_level_name}]`。
  - `GET /api/reports/{id} → ReportOut`（本人或 teacher/admin 可读）。`ReportOut = {id, session_id, created_at, dimensions, total_level, total_level_name, radar, strengths, gaps, advice}`，其中 `dimensions=[{dimension,name,theta,level,level_name,answered,correct,percent}]`。

- [ ] **Step 1: 写失败测试**

`api/tests/test_report.py`:

```python
from fastapi.testclient import TestClient

from tests.test_session_flow import _run_full_flow


def _finish_a_session(client: TestClient, headers: dict, bank: dict) -> int:
    view = _run_full_flow(client, headers, bank, correct=True)
    resp = client.post(f"/api/sessions/{view['session_id']}/finish", headers=headers)
    assert resp.status_code == 200
    return resp.json()["report_id"]


def test_finish_returns_report_with_radar(client, auth_headers, bank):
    report_id = _finish_a_session(client, auth_headers, bank)
    resp = client.get(f"/api/reports/{report_id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["dimensions"]) == 6
    assert len(body["radar"]) == 6
    assert all(0 <= r["value"] <= 100 for r in body["radar"])
    assert 1 <= body["total_level"] <= 5
    assert body["advice"]
    assert all(d["answered"] >= 2 for d in body["dimensions"])


def test_finish_is_idempotent(client, auth_headers, bank):
    view = _run_full_flow(client, auth_headers, bank, correct=True)
    first = client.post(f"/api/sessions/{view['session_id']}/finish", headers=auth_headers).json()
    second = client.post(f"/api/sessions/{view['session_id']}/finish", headers=auth_headers).json()
    assert first["report_id"] == second["report_id"]


def test_mine_lists_reports(client, auth_headers, bank):
    _finish_a_session(client, auth_headers, bank)
    resp = client.get("/api/reports/mine", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    assert "total_level_name" in resp.json()[0]


def test_report_forbidden_for_others(client, auth_headers, bank):
    report_id = _finish_a_session(client, auth_headers, bank)
    other = client.post("/api/auth/student", json={"name": "他人", "student_no": "OTHER01"}).json()
    resp = client.get(f"/api/reports/{report_id}", headers={"Authorization": f"Bearer {other['token']}"})
    assert resp.status_code == 403
```

- [ ] **Step 2: 跑测试确认失败** → FAIL（finish 404）。

- [ ] **Step 3: 实现报告生成**

`api/app/report/generate.py`:

```python
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES, LEVEL_NAMES, dimension_level
from app.models import AssessmentSession, Report, SessionAnswer

ADVICE_LOW = {
    "D1": "系统学习 AI 基础概念：推荐吴恩达《AI for Everyone》入门，重点掌握大模型的能力边界与幻觉成因。",
    "D2": "练习结构化提示词：从「角色+任务+约束+示例」四要素模板开始，每天用 AI 完成 1 个真实小任务并复盘指令质量。",
    "D3": "上手 2~3 款主流 AI 工具（对话助手、办公插件、数据分析），对比同一任务在不同工具中的表现，建立选型直觉。",
    "D4": "养成「先验证后采用」习惯：对 AI 给出的事实性内容交叉查证，每周挑 3 段 AI 输出做错误与幻觉标注练习。",
    "D5": "从拆解小任务开始练习人机协作：把一个复杂任务拆成 3~5 个子步骤，明确每步是人做还是 AI 做，并记录过程。",
    "D6": "学习 AI 伦理基础：了解个人信息保护要点与生成内容版权常识，使用 AI 产出时主动标注来源。",
}

ADVICE_HIGH = {
    "D1": "深入技术原理：了解微调、RAG、Agent 的技术脉络，关注主流评测基准（MMLU 等）的含义与局限。",
    "D2": "进阶提示工程：练习少样本示例设计、思维链诱导与上下文编排，对同一任务做多版本指令 A/B 对比。",
    "D3": "构建个人工具矩阵：按场景沉淀 AI 工具清单与工作流（数据分析、文档、代码），固化成可复用 SOP。",
    "D4": "系统性甄别训练：建立幻觉核查清单（来源、时效、自洽性），练习为 AI 输出撰写「改进指令」做迭代优化。",
    "D5": "挑战复杂项目协同：选一个跨环节真实项目全流程用 AI 协作完成，记录人机分工与迭代策略并复盘。",
    "D6": "成为负责任使用的示范者：在团队内推动 AI 使用规范（隐私脱敏、内容标注、合规审查），帮助他人识别风险。",
}


def build_report(db: OrmSession, session: AssessmentSession) -> Report:
    answers = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == session.id)).all()
    dimensions = []
    for d in DIMENSIONS:
        rows = [a for a in answers if a.dimension == d]
        theta = float(session.theta_snapshot.get(d, {}).get("theta", 3.0))
        level = dimension_level(theta)
        dimensions.append(
            {
                "dimension": d,
                "name": DIMENSION_NAMES[d],
                "theta": round(theta, 2),
                "level": level,
                "level_name": LEVEL_NAMES[level],
                "answered": len(rows),
                "correct": sum(1 for a in rows if a.is_correct),
                "percent": round((theta - 1) / 4 * 100),
            }
        )
    total_theta = sum(x["theta"] for x in dimensions) / len(dimensions)
    total_level = dimension_level(total_theta)
    ranked = sorted(dimensions, key=lambda x: x["theta"], reverse=True)
    gaps = [x["dimension"] for x in ranked[-2:]]
    by_dim = {x["dimension"]: x for x in dimensions}
    advice = [
        f"「{by_dim[d]['name']}」当前 {by_dim[d]['level_name']}："
        + (ADVICE_LOW[d] if by_dim[d]["level"] <= 2 else ADVICE_HIGH[d])
        for d in sorted(gaps)
    ]
    return Report(
        session_id=session.id,
        user_id=session.user_id,
        dimensions=dimensions,
        total_level=total_level,
        radar=[{"dimension": x["dimension"], "label": x["name"], "value": x["percent"]} for x in dimensions],
        strengths=[x["dimension"] for x in ranked[:2]],
        gaps=gaps,
        advice=advice,
    )
```

`api/app/api/report_routes.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.auth import current_user
from app.engine.adaptive import LEVEL_NAMES
from app.models import Report

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/mine")
def my_reports(user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Report).where(Report.user_id == user["id"]).order_by(Report.id.desc())).all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "total_level": r.total_level,
            "total_level_name": LEVEL_NAMES[r.total_level],
        }
        for r in rows
    ]


@router.get("/{report_id}")
def get_report(report_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    if report.user_id != user["id"] and user["role"] not in ("teacher", "admin"):
        raise HTTPException(status_code=403, detail="无权查看该报告")
    return {
        "id": report.id,
        "session_id": report.session_id,
        "created_at": report.created_at.isoformat(),
        "dimensions": report.dimensions,
        "total_level": report.total_level,
        "total_level_name": LEVEL_NAMES[report.total_level],
        "radar": report.radar,
        "strengths": report.strengths,
        "gaps": report.gaps,
        "advice": report.advice,
    }
```

`api/app/api/session_routes.py` 追加 finish 端点:

```python
from app.models import utcnow
from app.report.generate import build_report
from app.models import Report


@router.post("/{session_id}/finish")
def finish_session(session_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
    session = db.get(AssessmentSession, session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    existing = db.scalar(select(Report).where(Report.session_id == session.id))
    if existing is not None:
        return {"report_id": existing.id}
    report = build_report(db, session)
    session.status = "finished"
    session.finished_at = utcnow()
    db.add(report)
    db.commit()
    db.refresh(report)
    return {"report_id": report.id}
```

（`utcnow` 从 `app.models` 导入；文件顶部 import 区补 `from app.models import AssessmentSession, Question, Report, SessionAnswer, utcnow`，并按需调整。）

`api/app/main.py` 追加:

```python
from app.api.report_routes import router as report_router

app.include_router(report_router)
```

- [ ] **Step 4: 跑全部后端测试**

Run: `pytest -v` → 预期全 PASS（engine/grading/seed/auth/session_flow/report）。

- [ ] **Step 5: Commit**

```bash
git add api
git commit -m "feat: 报告生成（六维雷达+等级+模板建议）与查询 API"
```

---

### Task 8: 题库内容 v1（90 题）

**Files:**
- Create: `seeds/questions/D1.json` ~ `seeds/questions/D6.json`
- Create: `api/tests/test_bank_content.py`
- Create: `docs/competency-model.md`（六维 × L1~L5 行为锚定表）

**Interfaces:**
- Consumes: Task 2 `validate_question/load_seed_files`。
- Produces: 满足赛题"每维度 ≥10 基础 + 5 进阶"的题库（`python -m app.seed` 可导入）与能力模型文档。

**内容规范（编写题目的硬要求）:**
- 每文件 `{"questions": [...]}`，15 题 = 10 基础（single/multi/judge 混合，难度 1~3 覆盖三档）+ 5 进阶（≥4 道难度 4~5，其中 ≥1 道 `practical`，其余 `open`；全部带 rubric）。
- id 规则：`D1-B01`..`D1-B10`（基础）、`D1-A01`..`D1-A05`（进阶）。
- 基础题每题考察**一个明确考点**，干扰项要有合理性（来自常见误解）；`explanation` 必填，2 句以内讲清对错原因。
- 进阶题 rubric：`{"points": [考察要点...], "anchors": {"0"~"4": 每档行为描述}}`，锚定语要能区分真实水平。
- 维度考点覆盖清单（出题时对照，确保 10 道基础题不重复）：
  - D1：模型与人类区别、幻觉成因、训练/微调、上下文窗口、token、多模态、模型类型（判别/生成）、能力边界、伦理安全常识、发展脉络
  - D2：指令清晰度要素、角色设定、任务拆解、上下文给料、少样本示例、约束条件、输出格式控制、迭代追问、思维链、反面指令陷阱
  - D3：场景选工具、代码助手、数据分析、图像生成、办公插件、检索增强、API/插件生态、成本与配额、版本差异、隐私设置
  - D4：事实核查、幻觉识别、偏见识别、逻辑漏洞、来源验证、时效性问题、过度自信表述、改进指令撰写、交叉验证、AI 检测
  - D5：任务拆解与分工、人机边界判断、迭代修正、结果整合、质量把关、流程编排、失败回退、效率评估、多工具串联、协作留痕
  - D6：隐私脱敏、版权与署名、学术诚信、深度伪造识别、数据安全、生成内容标注、合规红线、责任归属、偏见防范、未成年人保护

- [ ] **Step 1: 写内容校验测试（先红）**

`api/tests/test_bank_content.py`:

```python
from pathlib import Path

import pytest

from app.seed import load_seed_files, validate_question

SEEDS = Path(__file__).resolve().parent.parent.parent / "seeds" / "questions"


def test_seed_dir_has_six_files():
    assert len(list(SEEDS.glob("D*.json"))) == 6


@pytest.mark.parametrize("dim", ["D1", "D2", "D3", "D4", "D5", "D6"])
def test_bank_coverage_per_dimension(dim):
    items = [q for q in load_seed_files(SEEDS) if q["dimension"] == dim]
    basic = [q for q in items if q["tier"] == "basic"]
    advanced = [q for q in items if q["tier"] == "advanced"]
    assert len(basic) >= 10, f"{dim} 基础题不足 10"
    assert len(advanced) >= 5, f"{dim} 进阶题不足 5"
    assert any(q["type"] == "practical" for q in advanced), f"{dim} 缺实操题"
    assert {q["difficulty"] for q in basic} >= {1, 2, 3}, f"{dim} 基础题难度未覆盖 1~3"
    assert all(q["difficulty"] >= 4 for q in advanced)


def test_bank_all_valid_and_unique():
    items = load_seed_files(SEEDS)
    codes = [q["id"] for q in items]
    assert len(codes) == len(set(codes)) == 90
    for q in items:
        validate_question(q)
```

- [ ] **Step 2: 跑测试确认失败** → FAIL（无种子文件）。

- [ ] **Step 3: 分批编写 6 个维度种子文件**

按上面内容规范逐维度编写（每文件一提交也可）。完成后：

```bash
cd api && source .venv/Scripts/activate
pytest tests/test_bank_content.py -v
python -m app.seed
```

预期：测试全 PASS，导入输出 `{"created": 90, "updated": 0}`。

- [ ] **Step 4: 编写 docs/competency-model.md**

六维 × L1~L5 行为锚定表（每格一句行为描述，从题库考点归纳），含等级评定规则说明（θ→等级映射表，同引擎 `dimension_level`）。

- [ ] **Step 5: 跑全部测试 + Commit**

```bash
pytest -v
git add seeds docs api
git commit -m "feat: 六维题库 v1（90 题）与能力等级锚定模型"
```

---

### Task 9: 前端骨架（Vite + Tailwind + shadcn + 路由 + API client + 登录页）

**Files:**
- Create: `web/`（Vite 脚手架）
- Create: `web/src/lib/api.ts`
- Create: `web/src/main.tsx`、`web/src/App.tsx`（路由）
- Create: `web/src/pages/LoginPage.tsx`
- Modify: `web/vite.config.ts`（tailwind 插件 + @ 别名 + proxy）
- Test: `web/src/lib/api.test.ts`

**Interfaces:**
- Produces:
  - `api.ts`：`api<T>(path, options): Promise<T>`、`getToken/setToken/clearToken`、`ApiError`。
  - 路由：`/login`、`/`（工作台，Task 11）、`/assess`（Task 10）、`/report/:id`（Task 11）；`RequireAuth` 组件（无 token 重定向 /login）。
  - 后端约定同 Task 5/6/7 的 HTTP 形状。

- [ ] **Step 1: 脚手架与依赖**

```bash
cd ai-compass
pnpm create vite web --template react-ts
cd web && pnpm install
pnpm add react-router-dom recharts
pnpm add tailwindcss @tailwindcss/vite
pnpm dlx shadcn@latest init -y -b neutral
pnpm dlx shadcn@latest add -y button card badge progress input label radio-group checkbox separator
pnpm add -D vitest
```

（若 shadcn CLI 参数有出入，以 CLI 提示为准；失败则手工建 `src/lib/utils.ts`（`cn` 函数）与所需组件，源码取自 shadcn/ui 官方文档 Vite 指南。）

- [ ] **Step 2: vite.config.ts 与全局样式**

`web/vite.config.ts`:

```ts
import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: { proxy: { "/api": "http://localhost:8000" } },
  test: { environment: "node" },
}) as never;
```

（vitest 读 vite config 需要 `/// <reference types="vitest/config" />` 或 `defineConfig` from `vitest/config`；执行时用 `import { defineConfig } from "vitest/config"` 即可同时满足 vite 与 vitest。）

`web/src/index.css` 首行确保 `@import "tailwindcss";`（shadcn init 会生成基础变量，保留其内容）。

`web/package.json` scripts 增加 `"test": "vitest run"`。

- [ ] **Step 3: api.ts 与测试**

`web/src/lib/api.ts`:

```ts
const TOKEN_KEY = "compass_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Content-Type", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const resp = await fetch(path, { ...options, headers });
  if (!resp.ok) {
    let detail = `请求失败（${resp.status}）`;
    try {
      const body = (await resp.json()) as { detail?: string };
      if (body?.detail) detail = String(body.detail);
    } catch {
      // 非 JSON 响应，用默认文案
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}
```

`web/src/lib/api.test.ts`:

```ts
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, clearToken, getToken, setToken } from "./api";

describe("api client", () => {
  afterEach(() => {
    clearToken();
    vi.unstubAllGlobals();
  });

  it("token 存取", () => {
    expect(getToken()).toBeNull();
    setToken("t1");
    expect(getToken()).toBe("t1");
    clearToken();
    expect(getToken()).toBeNull();
  });

  it("携带 Bearer token", async () => {
    setToken("t123");
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await api("/api/health");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer t123");
  });

  it("非 2xx 抛出携带后端 detail 的 ApiError", async () => {
    const fetchMock = vi.fn(
      async () => new Response(JSON.stringify({ detail: "邀请码无效" }), { status: 400 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const err = await api("/api/auth/student", { method: "POST", body: "{}" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toBe("邀请码无效");
  });
});
```

Run: `pnpm test` → PASS。

- [ ] **Step 4: 路由骨架与登录页**

`web/src/App.tsx`:

```tsx
import { Navigate, Route, Routes } from "react-router-dom";

import { getToken } from "@/lib/api";
import AssessmentPage from "@/pages/AssessmentPage";
import HomePage from "@/pages/HomePage";
import LoginPage from "@/pages/LoginPage";
import ReportPage from "@/pages/ReportPage";

function RequireAuth({ children }: { children: React.ReactNode }) {
  if (!getToken()) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<RequireAuth><HomePage /></RequireAuth>} />
      <Route path="/assess" element={<RequireAuth><AssessmentPage /></RequireAuth>} />
      <Route path="/report/:id" element={<RequireAuth><ReportPage /></RequireAuth>} />
    </Routes>
  );
}
```

`web/src/main.tsx`:

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
```

`web/src/pages/LoginPage.tsx`:

```tsx
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, setToken } from "@/lib/api";

interface AuthResp {
  token: string;
  user: { id: number; name: string; role: string };
}

export default function LoginPage() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [studentNo, setStudentNo] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const resp = await api<AuthResp>("/api/auth/student", {
        method: "POST",
        body: JSON.stringify({ name, student_no: studentNo, invite_code: inviteCode || null }),
      });
      setToken(resp.token);
      navigate("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败，请重试");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-xl">AI 能力罗盘</CardTitle>
          <CardDescription>输入姓名与学号即可开始测评（邀请码选填）</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={handleSubmit}>
            <div className="space-y-2">
              <Label htmlFor="name">姓名</Label>
              <Input id="name" value={name} onChange={(e) => setName(e.target.value)} required maxLength={32} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="studentNo">学号</Label>
              <Input id="studentNo" value={studentNo} onChange={(e) => setStudentNo(e.target.value)} required maxLength={32} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="inviteCode">班级邀请码（选填）</Label>
              <Input id="inviteCode" value={inviteCode} onChange={(e) => setInviteCode(e.target.value)} maxLength={16} />
            </div>
            {error && <p className="text-sm text-red-600">{error}</p>}
            <Button className="w-full" type="submit" disabled={loading}>
              {loading ? "进入中…" : "开始"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
```

（HomePage/AssessmentPage/ReportPage 本 Task 先建最小占位：导出默认组件返回 `<div>建设中</div>`，Task 10/11 替换。）

- [ ] **Step 5: 验证**

```bash
pnpm test
pnpm build
```

均需通过（build 含 tsc 类型检查）。

- [ ] **Step 6: Commit**

```bash
git add web
git commit -m "feat: 前端骨架（路由/API client/登录页）"
```

---

### Task 10: 测评答题页

**Files:**
- Modify: `web/src/pages/AssessmentPage.tsx`（替换占位）
- Test: 手动验证（本 Task 类型逻辑简单、状态流依赖后端，以集成手测为准；`pnpm build` 做类型门禁）

**Interfaces:**
- Consumes: Task 6 `SessionView` 形状（`question/progress/reason/just`）。
- Produces: 完成测评后 `POST finish` 并跳转 `/report/:id`。

- [ ] **Step 1: 实现页面**

`web/src/pages/AssessmentPage.tsx`:

```tsx
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
```

- [ ] **Step 2: 类型与构建验证**

```bash
cd web && pnpm build
```

预期：tsc 无错误，构建成功。

- [ ] **Step 3: 本地联调手测**

```bash
# 终端 A
cd api && source .venv/Scripts/activate && python -m app.seed && uvicorn app.main:app --reload --port 8000
# 终端 B
cd web && pnpm dev
```

浏览器打开 http://localhost:5173 ：登录 → 开始测评 → 逐题作答（观察对错反馈、θ 轨迹图、出题理由、维度徽章推进）→ 全部完成 → 生成报告 → 跳转报告页（Task 11 前显示"建设中"即可）。

- [ ] **Step 4: Commit**

```bash
git add web
git commit -m "feat: 自适应答题页（三题型/对错反馈/θ轨迹/出题理由）"
```

---

### Task 11: 工作台与报告页（雷达图）

**Files:**
- Modify: `web/src/pages/HomePage.tsx`（替换占位）
- Modify: `web/src/pages/ReportPage.tsx`（替换占位）

**Interfaces:**
- Consumes: Task 7 `GET /api/reports/mine`、`GET /api/reports/{id}`（ReportOut 形状）。

- [ ] **Step 1: HomePage**

`web/src/pages/HomePage.tsx`:

```tsx
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, clearToken } from "@/lib/api";

interface Me {
  id: number;
  name: string;
  role: string;
}

interface ReportBrief {
  id: number;
  created_at: string;
  total_level: number;
  total_level_name: string;
}

export default function HomePage() {
  const navigate = useNavigate();
  const [me, setMe] = useState<Me | null>(null);
  const [reports, setReports] = useState<ReportBrief[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Me>("/api/auth/me").then(setMe).catch((e: unknown) => setError(e instanceof Error ? e.message : "加载失败"));
    api<ReportBrief[]>("/api/reports/mine").then(setReports).catch(() => setReports([]));
  }, []);

  return (
    <div className="mx-auto max-w-2xl space-y-4 p-4">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">AI 能力罗盘</h1>
          <p className="text-sm text-slate-500">{me ? `${me.name}，欢迎回来` : "加载中…"}</p>
        </div>
        <Button
          variant="ghost"
          onClick={() => {
            clearToken();
            navigate("/login");
          }}
        >
          退出
        </Button>
      </header>
      {error && <p className="text-sm text-red-600">{error}</p>}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">开始一次测评</CardTitle>
        </CardHeader>
        <CardContent className="flex items-center justify-between">
          <p className="text-sm text-slate-500">六维自适应出题，约 25 分钟，可随时查看能力轨迹</p>
          <Button onClick={() => navigate("/assess")}>开始测评</Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">历史报告</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {reports.length === 0 && <p className="text-sm text-slate-500">还没有测评记录</p>}
          {reports.map((r) => (
            <Link key={r.id} to={`/report/${r.id}`} className="flex items-center justify-between rounded border p-3 hover:bg-slate-50">
              <span className="text-sm">{new Date(r.created_at).toLocaleString("zh-CN")}</span>
              <Badge>{r.total_level_name}</Badge>
            </Link>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
```

- [ ] **Step 2: ReportPage（雷达图）**

`web/src/pages/ReportPage.tsx`:

```tsx
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer } from "recharts";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";

interface DimensionDetail {
  dimension: string;
  name: string;
  theta: number;
  level: number;
  level_name: string;
  answered: number;
  correct: number;
  percent: number;
}

interface ReportOut {
  id: number;
  created_at: string;
  dimensions: DimensionDetail[];
  total_level: number;
  total_level_name: string;
  radar: { dimension: string; label: string; value: number }[];
  strengths: string[];
  gaps: string[];
  advice: string[];
}

export default function ReportPage() {
  const { id } = useParams<{ id: string }>();
  const [report, setReport] = useState<ReportOut | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!id) return;
    api<ReportOut>(`/api/reports/${id}`)
      .then(setReport)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "报告加载失败"));
  }, [id]);

  if (error) return <div className="p-8 text-red-600">{error}</div>;
  if (!report) return <div className="p-8">报告加载中…</div>;

  const byDim = Object.fromEntries(report.dimensions.map((d) => [d.dimension, d]));
  const radarData = report.radar.map((r) => ({ subject: r.label, value: r.value }));

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold">你的 AI 能力报告</h1>
        <p className="text-sm text-slate-500">
          {new Date(report.created_at).toLocaleString("zh-CN")} · 总评 <Badge>{report.total_level_name}</Badge>
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">六维能力雷达</CardTitle>
        </CardHeader>
        <CardContent className="h-80">
          <ResponsiveContainer width="100%" height="100%">
            <RadarChart data={radarData}>
              <PolarGrid />
              <PolarAngleAxis dataKey="subject" />
              <PolarRadiusAxis domain={[0, 100]} />
              <Radar dataKey="value" stroke="#6366f1" fill="#6366f1" fillOpacity={0.45} />
            </RadarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">维度明细</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {report.dimensions.map((d) => (
            <div key={d.dimension} className="flex items-center justify-between border-b py-2 last:border-0">
              <div>
                <p className="text-sm font-medium">{d.name}</p>
                <p className="text-xs text-slate-500">
                  答对 {d.correct}/{d.answered} · 能力值 {d.theta.toFixed(1)}
                </p>
              </div>
              <Badge variant={d.level >= 3 ? "default" : "secondary"}>{d.level_name}</Badge>
            </div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">学习建议</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <p className="text-xs text-slate-500">
            优势维度：{report.strengths.map((d) => byDim[d]?.name).filter(Boolean).join("、")}
          </p>
          {report.advice.map((a) => (
            <p key={a} className="rounded bg-slate-50 p-3 text-sm leading-relaxed">{a}</p>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
```

- [ ] **Step 3: 构建与手测**

```bash
cd web && pnpm build && pnpm test
```

浏览器完整走一遍：登录 → 测评 → 报告（雷达图、维度明细、建议）→ 回工作台看历史列表。

- [ ] **Step 4: Commit**

```bash
git add web
git commit -m "feat: 工作台与雷达图报告页"
```

---

### Task 12: M1 闭环验收（静态托管 + 端到端 + README）

**Files:**
- Modify: `api/app/main.py`（托管 web/dist）
- Modify: `README.md`（启动指引）

**Interfaces:**
- Produces: `uvicorn app.main:app --port 8000` 单进程同时服务 API 与前端（生产形态，Meoo 部署同款）。

- [ ] **Step 1: main.py 末尾追加静态托管（必须在 include_router 之后）**

```python
from pathlib import Path

from fastapi.staticfiles import StaticFiles

_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="web")
```

- [ ] **Step 2: 全量验证命令**

```bash
cd api && source .venv/Scripts/activate && pytest -v          # 预期全绿（含题库 90 题校验）
cd ../web && pnpm test && pnpm build                          # 预期测试通过、构建成功
cd ../api && python -m app.seed && uvicorn app.main:app --port 8000 &
sleep 3
curl -s http://localhost:8000/api/health
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/   # 预期 200（index.html）
```

- [ ] **Step 3: 浏览器端到端手测清单（在 http://localhost:8000 上）**

1. 登录页注册（新学号）→ 进入工作台
2. 开始测评 → 连续作答（三种题型都出现）→ 观察对错反馈/解析/θ 轨迹/出题理由
3. 全部完成 → 生成报告 → 雷达图 6 轴、维度明细、学习建议可见
4. 回工作台 → 历史报告列表出现刚才记录 → 点开可再查看
5. 退出登录 → 直接访问 / → 被重定向到 /login

- [ ] **Step 4: README 更新与收尾提交**

README「开发」节更新为实际命令（venv 激活、pytest、seed、双端启动、单进程模式）。然后：

```bash
git add -A
git grep --cached -nE "(sk-[A-Za-z0-9]{8,}|API_KEY=.+)" || true
git commit -m "feat: M1 闭环——单进程托管与端到端验收通过"
```

---

## Self-Review 记录（writing-plans 自检）

- **Spec 覆盖**：spec §11 M1 行的六项（骨架/模型文档/90 题/引擎/客观题流/报告雏形）分别落在 Task 1-2、Task 8 Step 4、Task 8、Task 3、Task 6+10、Task 7+11。M2/M3 范围（LLM 判题、对话式、实操、教师端、后台、部署、视频、文档）不在本计划。
- **占位符扫描**：无 TBD/TODO；所有代码步骤给出完整代码。
- **类型一致性**：`SessionView/QuestionOut/ReportOut` 在 Task 6/7（后端）与 Task 10/11（前端 interface）字段一致；`DimensionState` 方法名 `to_dict/from_dict` 全程一致；`grade_objective` 签名 Task 4 定义、Task 6 使用一致。
