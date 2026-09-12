from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Question, SessionAnswer


def _run_full_flow(client: TestClient, headers: dict, bank: dict, correct: bool = True) -> dict:
    # quick 模式：客观答完即止（M1 finish 链路）。full 模式客观完成后自 T2 起进入 dialog 阶段，
    # 不再终止于 question==null，阶段流转由 tests/test_stage_machine.py 覆盖。
    start = client.post("/api/sessions", json={"mode": "quick"}, headers=headers)
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


def test_strong_learner_climbs_to_difficulty_ceiling(client, auth_headers, bank):
    """题池存在 d4 客观题时，连对不应在 d3 提前停止，强学员应被推到难度触顶。"""
    view = _run_full_flow(client, auth_headers, bank, correct=True)
    with SessionLocal() as db:
        difficulties = db.scalars(
            select(Question.difficulty)
            .join(SessionAnswer, SessionAnswer.question_id == Question.id)
            .where(SessionAnswer.session_id == view["session_id"])
        ).all()
    assert max(difficulties) >= 4


def test_wrong_answers_lower_theta(client, auth_headers, bank):
    view = _run_full_flow(client, auth_headers, bank, correct=False)
    assert view["progress"]["D1"]["theta"] < 3.0


def test_double_answer_rejected(client, auth_headers, bank):
    start = client.post("/api/sessions", json={"mode": "full"}, headers=auth_headers).json()
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


def test_question_out_shuffles_options(client, auth_headers):
    """_question_out 下发前对选项随机洗牌：key 集合与文本不变、不改动题目原文、
    answer/rubric 不泄漏；多次构建至少出现两种顺序（4 选项 30 次全同序概率约 1e-53）。
    判分按选项 key 精确/集合匹配，不受顺序影响。"""
    from app.api import session_routes

    with SessionLocal() as db:
        choice = db.scalars(select(Question).where(Question.code == "D1-T01")).one()
        baseline = [(o["key"], o["text"]) for o in choice.options]
        orders = set()
        for _ in range(30):
            out = session_routes._question_out(choice)
            assert set((o["key"], o["text"]) for o in out["options"]) == set(baseline)
            assert "answer" not in out and "rubric" not in out
            orders.add(tuple(o["key"] for o in out["options"]))
        assert len(orders) >= 2  # 顺序确实随机变化，消除答案位置可猜性
        assert [(o["key"], o["text"]) for o in choice.options] == baseline  # 原对象未被原地改动
        open_q = db.scalars(select(Question).where(Question.type == "open")).first()
        assert session_routes._question_out(open_q)["options"] is None  # 无选项题型不受影响


def test_judge_answer_must_be_bool(client, auth_headers):
    """判断题答案传非布尔（如字符串）必须 400，而非被 bool() 强转静默判错。"""
    with SessionLocal() as db:
        judge = Question(
            code="D1-J01", dimension="D1", tier="advanced", type="judge",
            difficulty=5, stem="大模型的幻觉风险可以被完全消除。", answer=True, tags=[],
        )
        db.add(judge)
        db.commit()
        db.refresh(judge)
        question_id = judge.id
    try:
        start = client.post("/api/sessions", json={"mode": "full"}, headers=auth_headers).json()
        resp = client.post(
            f"/api/sessions/{start['session_id']}/answer",
            json={"question_id": question_id, "answer": "对", "time_spent": 10},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "判断题答案必须为布尔值"
    finally:
        with SessionLocal() as db:  # 清理插入的题目，避免污染共享测试库的题目计数
            row = db.get(Question, question_id)
            if row is not None:
                db.delete(row)
                db.commit()


def test_open_question_answer_rejected(client, auth_headers):
    """open/practical 题不支持线上客观作答，提交其 id 必须 400 而非 500。"""
    with SessionLocal() as db:
        open_q = Question(
            code="D1-O01", dimension="D1", tier="advanced", type="open",
            difficulty=5, stem="请描述你会如何评估一段 AI 生成的内容。", tags=[],
        )
        db.add(open_q)
        db.commit()
        db.refresh(open_q)
        question_id = open_q.id
    try:
        start = client.post("/api/sessions", json={"mode": "full"}, headers=auth_headers).json()
        resp = client.post(
            f"/api/sessions/{start['session_id']}/answer",
            json={"question_id": question_id, "answer": "随便答", "time_spent": 60},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "该题型不支持线上客观作答"
    finally:
        with SessionLocal() as db:  # 清理插入的题目，避免污染共享测试库的题目计数
            row = db.get(Question, question_id)
            if row is not None:
                db.delete(row)
                db.commit()
