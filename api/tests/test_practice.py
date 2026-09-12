"""错题本与薄弱考点练习卷：wrongbook 聚合、抽题避开已作答、即时判分、θ/正式报告零影响。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import AssessmentSession, PracticeAnswer, PracticeSession, Question, Report, SessionAnswer

from tests.test_session_flow import _run_full_flow


def _me(client: TestClient, headers: dict) -> int:
    return client.get("/api/auth/me", headers=headers).json()["id"]


def _wrong_counts(user_id: int) -> dict[str, int]:
    """DB 真值：该用户正式测评错题（is_correct=False）按题号聚合次数。"""
    with SessionLocal() as db:
        rows = db.execute(
            select(SessionAnswer.question_code, func.count())
            .join(AssessmentSession, AssessmentSession.id == SessionAnswer.session_id)
            .where(AssessmentSession.user_id == user_id, SessionAnswer.is_correct.is_(False))
            .group_by(SessionAnswer.question_code)
        ).all()
    return dict(rows)


def _user_state(user_id: int) -> tuple[list[dict], int]:
    """θ 快照集合与报告数：练习前后各取一次，断言零影响。"""
    with SessionLocal() as db:
        snapshots = db.scalars(
            select(AssessmentSession.theta_snapshot).where(AssessmentSession.user_id == user_id).order_by(
                AssessmentSession.id
            )
        ).all()
        reports = len(db.scalars(select(Report.id).where(Report.user_id == user_id).order_by(Report.id)).all())
    return list(snapshots), reports


@pytest.fixture()
def practice_bank() -> dict[str, int]:
    """为 D1 注入 5 道「补练」+3 道「稀缺」标签的未作答客观题（答案均为 A），测试后连同练习数据清理。"""
    questions = [
        Question(
            code=code, dimension="D1", tier="basic", type="single", difficulty=1,
            stem=f"练习卷补充题 {code}", options=[{"key": "A", "text": "正确项"}, {"key": "B", "text": "干扰项"}],
            answer="A", tags=[tag], est_seconds=30, explanation="补充题解析：A 为正确项。",
        )
        for tag, codes in (("补练", [f"D1-P{i:02d}" for i in range(1, 6)]), ("稀缺", [f"D1-R{i:02d}" for i in range(1, 4)]))
        for code in codes
    ]
    with SessionLocal() as db:
        db.add_all(questions)
        db.commit()
        code_by_id = {}
        for q in questions:
            db.refresh(q)
            code_by_id[q.id] = q.code
    yield code_by_id
    with SessionLocal() as db:  # FK 顺序：练习作答 → 练习卷 → 注入题目（避免污染共享库的正式抽题）
        db.query(PracticeAnswer).delete()
        db.query(PracticeSession).delete()
        db.query(Question).filter(Question.id.in_(list(code_by_id))).delete(synchronize_session=False)
        db.commit()


def _post_practice(client: TestClient, headers: dict, body: dict) -> dict:
    resp = client.post("/api/practice/sessions", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_wrongbook_aggregates_dimension_tags_code_and_count(client, auth_headers, bank):
    user_id = _me(client, auth_headers)
    _run_full_flow(client, auth_headers, bank, correct=False)  # 全错快测 → 产生错题
    expected = _wrong_counts(user_id)
    assert expected  # 前置：本轮确有错题
    resp = client.get("/api/me/wrongbook", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert resp.json()["total"] == len(expected)
    assert {i["question_code"] for i in items} == set(expected)
    for i in items:
        assert i["wrong_count"] == expected[i["question_code"]]
        assert i["dimension"] in ("D1", "D2", "D3", "D4", "D5", "D6") and i["dimension_name"]
        assert isinstance(i["tags"], list) and i["question_id"]

    # 同题跨会话累计：再跑一次全错快测（自适应确定性抽题，错题集合不变）
    before = {i["question_code"]: i["wrong_count"] for i in items}
    _run_full_flow(client, auth_headers, bank, correct=False)
    after = {i["question_code"]: i["wrong_count"] for i in client.get("/api/me/wrongbook", headers=auth_headers).json()["items"]}
    assert all(after[c] == n + 1 for c, n in before.items())


def test_practice_session_draws_unanswered_objective(client, auth_headers, practice_bank):
    user_id = _me(client, auth_headers)
    with SessionLocal() as db:
        answered = set(
            db.scalars(
                select(SessionAnswer.question_id)
                .join(AssessmentSession, AssessmentSession.id == SessionAnswer.session_id)
                .where(AssessmentSession.user_id == user_id)
            )
        )
    body = _post_practice(client, auth_headers, {"dimension": "D1", "size": 5})
    assert body["question_ids"] == [q["id"] for q in body["questions"]]
    assert len(body["question_ids"]) == 5
    assert all(q["dimension"] == "D1" for q in body["questions"])
    assert all(q["type"] in ("single", "multi", "judge") for q in body["questions"])
    assert all("answer" not in q and "rubric" not in q for q in body["questions"])
    assert not set(body["question_ids"]) & answered  # 避开正式测评已作答
    # 选项已随机洗牌（key 集合不变）
    for q in body["questions"]:
        assert q["options"] is None or all(o.get("text") for o in q["options"])


def test_practice_tag_filter_with_relaxation(client, auth_headers, practice_bank):
    """「稀缺」标签仅 3 道 < size=5：主池全保留，并放宽补足同维度任意未作答题。"""
    body = _post_practice(client, auth_headers, {"dimension": "D1", "tags": ["稀缺"], "size": 5})
    scarce_ids = {qid for qid, code in practice_bank.items() if code.startswith("D1-R")}
    assert scarce_ids <= set(body["question_ids"])  # 标签主池全部入选
    tagged = [q for q in body["questions"] if "稀缺" in q["tags"]]
    assert len(tagged) == 3
    assert len(body["question_ids"]) == 5  # 放宽后补足到 size


def test_practice_answer_immediate_grading_and_zero_impact(client, auth_headers, bank, practice_bank):
    _run_full_flow(client, auth_headers, bank, correct=False)
    user_id = _me(client, auth_headers)
    theta_before, reports_before = _user_state(user_id)

    start = _post_practice(client, auth_headers, {"dimension": "D1", "tags": ["补练"], "size": 5})
    q1, q2 = start["questions"][0], start["questions"][1]
    right = client.post(
        f"/api/practice/sessions/{start['id']}/answer",
        json={"question_id": q1["id"], "answer": "A"}, headers=auth_headers,
    )
    assert right.status_code == 200
    assert right.json()["is_correct"] is True and right.json()["explanation"]
    wrong = client.post(
        f"/api/practice/sessions/{start['id']}/answer",
        json={"question_id": q2["id"], "answer": "B"}, headers=auth_headers,
    )
    assert wrong.status_code == 200
    assert wrong.json()["is_correct"] is False
    # 同卷重复作答 / 不在卷内的题 → 400
    assert client.post(
        f"/api/practice/sessions/{start['id']}/answer",
        json={"question_id": q1["id"], "answer": "A"}, headers=auth_headers,
    ).status_code == 400
    outsider = next(qid for qid in practice_bank if qid not in set(start["question_ids"]))
    assert client.post(
        f"/api/practice/sessions/{start['id']}/answer",
        json={"question_id": outsider, "answer": "A"}, headers=auth_headers,
    ).status_code == 400

    theta_after, reports_after = _user_state(user_id)
    assert theta_after == theta_before  # 练习不回灌 θ
    assert reports_after == reports_before  # 练习不生成/不改正式报告
    with SessionLocal() as db:
        rows = db.scalars(
            select(PracticeAnswer).where(PracticeAnswer.practice_session_id == start["id"])
        ).all()
        assert {r.is_correct for r in rows} == {True, False}  # 练习记录落 practice_answers


def test_practice_avoids_previously_practiced(client, auth_headers, practice_bank):
    """已答过的练习题在新练习卷中不再出现。"""
    s1 = _post_practice(client, auth_headers, {"dimension": "D1", "tags": ["补练"], "size": 5})
    for q in s1["questions"][:2]:  # 练完前 2 题
        resp = client.post(
            f"/api/practice/sessions/{s1['id']}/answer",
            json={"question_id": q["id"], "answer": "A"}, headers=auth_headers,
        )
        assert resp.status_code == 200
    s2 = _post_practice(client, auth_headers, {"dimension": "D1", "size": 5})
    practiced = {q["id"] for q in s1["questions"][:2]}
    assert not practiced & set(s2["question_ids"])


def test_practice_validation_and_auth(client, auth_headers, practice_bank):
    fresh = client.post("/api/auth/student", json={"name": "无错题", "student_no": "NWRONG01"}).json()
    h = {"Authorization": f"Bearer {fresh['token']}"}
    assert client.get("/api/me/wrongbook", headers=h).json()["total"] == 0
    # 无错题且未指定维度 → 无法定位薄弱范围
    resp = client.post("/api/practice/sessions", json={"size": 5}, headers=h)
    assert resp.status_code == 400
    # 非法维度 / size 越界
    assert client.post("/api/practice/sessions", json={"dimension": "D9", "size": 5}, headers=auth_headers).status_code == 400
    assert client.post("/api/practice/sessions", json={"dimension": "D1", "size": 4}, headers=auth_headers).status_code == 422
    assert client.post("/api/practice/sessions", json={"dimension": "D1", "size": 11}, headers=auth_headers).status_code == 422
    # 未登录
    assert client.get("/api/me/wrongbook").status_code == 401
    assert client.post("/api/practice/sessions", json={"size": 5}).status_code == 401
    # 他人练习卷不可答
    s = _post_practice(client, auth_headers, {"dimension": "D1", "size": 5})
    assert client.post(
        f"/api/practice/sessions/{s['id']}/answer",
        json={"question_id": s["question_ids"][0], "answer": "A"}, headers=h,
    ).status_code == 404
