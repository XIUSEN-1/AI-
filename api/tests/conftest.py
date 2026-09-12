"""测试库隔离与通用夹具。

COMPASS_DB 绑定到 tmp_path_factory 的基目录（pytest 按运行编号管理、自动清理，
替代裸 tempfile.mkdtemp 防目录泄漏）。必须在收集导入任何 app 模块之前完成，
故放在 pytest_sessionstart（全部插件就绪后、收集开始前，只调用一次）。
同处结构性禁用真实 LLM 调用（见 pytest_sessionstart 内注释）。
"""

import json
import os
import time
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).parent / "fixtures" / "test_bank.json"


def finish_and_wait(client, headers: dict, session_id: int, timeout: float = 15.0) -> dict:
    """POST finish（异步判题 202）并轮询 status 至 finished，返回 finish 响应并入 report_id。
    轮询到 finished 时全部 LLM 调用已结束，monkeypatch 拆卸不会再与后台线程竞态。"""
    resp = client.post(f"/api/sessions/{session_id}/finish", headers=headers)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/sessions/{session_id}/status", headers=headers).json()
        if status["status"] == "finished":
            return body | {"report_id": status["report_id"]}
        time.sleep(0.02)
    raise AssertionError(f"finish 判题轮询超时（{timeout}s），session={session_id}")


def pytest_sessionstart(session):
    db_dir = session.config._tmp_path_factory.getbasetemp() / "compass-test"
    db_dir.mkdir(parents=True, exist_ok=True)  # sqlite 不会自建中间目录
    os.environ["COMPASS_DB"] = str(db_dir / "test.db")
    # 为什么强制空 Key：环境变量优先于 api/.env（app.config.pick），空值使 provider 直接抛
    # ProviderUnavailableError。worktree 无 .env 时多余但无害；合并回主仓（.env 带真实 Key）后，
    # 这行保证整套测试绝不发起真实 API 调用，报告 advice_source=="template" 断言不翻转。
    # 守护测试：tests/test_llm_provider.py::test_suite_never_reads_real_api_key。
    os.environ["DEEPSEEK_API_KEY"] = ""


@pytest.fixture(scope="session", autouse=True)
def seeded_db():
    from app.db import SessionLocal, init_db
    from app.seed import ensure_base_accounts, import_questions

    init_db()
    with SessionLocal() as db:
        ensure_base_accounts(db)
        import_questions(json.loads(_FIXTURES.read_text(encoding="utf-8"))["questions"], db)
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

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
