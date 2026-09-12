"""测试库隔离与通用夹具。

COMPASS_DB 绑定到 tmp_path_factory 的基目录（pytest 按运行编号管理、自动清理，
替代裸 tempfile.mkdtemp 防目录泄漏）。必须在收集导入任何 app 模块之前完成，
故放在 pytest_sessionstart（全部插件就绪后、收集开始前，只调用一次）。
"""

import json
import os
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).parent / "fixtures" / "test_bank.json"


def pytest_sessionstart(session):
    db_dir = session.config._tmp_path_factory.getbasetemp() / "compass-test"
    db_dir.mkdir(parents=True, exist_ok=True)  # sqlite 不会自建中间目录
    os.environ["COMPASS_DB"] = str(db_dir / "test.db")


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
