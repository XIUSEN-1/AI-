"""管理端（Task 1）：教师账号创建、题库 CRUD（校验/版本/软删）、人工复核队列与 resolve 落分。

resolve 裁定语义（计划内）：answer.score=final_score、报告逐题 rationale 追加"[人工复核]"、
开放题 is_correct 保持 None（客观题同步对错）、队列 resolved，**θ 不重放**（报告以人工分为准）。
零真实 LLM：复核条目直接经 enqueue_review 造数，报告经 build_report(chat_fn=None) 模板兜底。
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.judge.pipeline import enqueue_review
from app.models import AssessmentSession, Question, Report, ReviewQueue, SessionAnswer, User
from app.report.generate import build_report


@pytest.fixture()
def admin_headers(client) -> dict[str, str]:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _student_headers(client) -> dict[str, str]:
    resp = client.post("/api/auth/student", json={"name": "越权学员", "student_no": f"AD-{uuid.uuid4().hex[:6]}"})
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture()
def created_question_ids():
    """CRUD 测试自建题目的清理：测试库共享（test_seed 断言库内恰 27 题），用毕物理删除。"""
    ids: list[int] = []
    yield ids
    if ids:
        with SessionLocal() as db:
            db.query(Question).filter(Question.id.in_(ids)).delete(synchronize_session=False)
            db.commit()


def _valid_open_question() -> dict:
    suffix = uuid.uuid4().hex[:6]
    return {
        "id": f"D2-A{suffix}",
        "dimension": "D2",
        "tier": "basic",
        "type": "open",
        "difficulty": 3,
        "stem": f"管理端自建开放题{suffix}",
        "tags": ["考点"],
        "est_seconds": 120,
        "rubric": {"points": ["要点一", "要点二"], "anchors": {"0": "未触及", "1": "初步", "2": "基本", "3": "良好", "4": "优秀"}},
    }


# ---------- 教师账号 ----------

def test_create_teacher_and_login(client, admin_headers):
    username = f"t{uuid.uuid4().hex[:6]}"
    resp = client.post(
        "/api/admin/teachers",
        json={"name": "王老师", "username": username, "password": "pass123456"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "teacher"
    login = client.post("/api/auth/login", json={"username": username, "password": "pass123456"})
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "teacher"


def test_create_teacher_duplicate_username(client, admin_headers):
    username = f"t{uuid.uuid4().hex[:6]}"
    body = {"name": "李老师", "username": username, "password": "pass123456"}
    assert client.post("/api/admin/teachers", json=body, headers=admin_headers).status_code == 200
    assert client.post("/api/admin/teachers", json=body, headers=admin_headers).status_code == 400


def test_create_teacher_requires_admin(client, admin_headers):
    for headers in (_student_headers(client), None):
        resp = client.post(
            "/api/admin/teachers",
            json={"name": "x", "username": f"t{uuid.uuid4().hex[:6]}", "password": "pass123456"},
            headers=headers,
        )
        assert resp.status_code in (401, 403)


# ---------- 题库 CRUD ----------

def test_question_list_pagination_and_filters(client, admin_headers):
    resp = client.get("/api/admin/questions", params={"page": 1, "page_size": 5}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["page"] == 1 and body["page_size"] == 5
    assert body["total"] >= 27  # fixture 题库 27 题（另含本文件自建题）
    assert len(body["items"]) == 5
    assert all("answer" in item and "version" in item for item in body["items"])

    d1 = client.get("/api/admin/questions", params={"dimension": "D1", "page_size": 100}, headers=admin_headers).json()
    assert d1["total"] > 0 and all(q["dimension"] == "D1" for q in d1["items"])
    adv = client.get("/api/admin/questions", params={"tier": "advanced", "difficulty": 4, "page_size": 100}, headers=admin_headers).json()
    assert adv["total"] > 0 and all(q["tier"] == "advanced" and q["difficulty"] == 4 for q in adv["items"])


def test_question_create_validates_and_persists(client, admin_headers, created_question_ids):
    payload = _valid_open_question()
    resp = client.post("/api/admin/questions", json=payload, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    created_question_ids.append(body["id"])
    assert body["code"] == payload["id"] and body["version"] == 1 and body["status"] == "published"

    bad = _valid_open_question() | {"difficulty": 9}
    resp = client.post("/api/admin/questions", json=bad, headers=admin_headers)
    assert resp.status_code == 400
    dup = _valid_open_question()
    dup_created = client.post("/api/admin/questions", json=dup, headers=admin_headers)
    assert dup_created.status_code == 200
    created_question_ids.append(dup_created.json()["id"])
    assert client.post("/api/admin/questions", json=dup, headers=admin_headers).status_code == 400


def test_question_update_increments_version_and_validates(client, admin_headers, created_question_ids):
    created = client.post("/api/admin/questions", json=_valid_open_question(), headers=admin_headers).json()
    created_question_ids.append(created["id"])
    qid = created["id"]
    resp = client.put(f"/api/admin/questions/{qid}", json={"stem": "更新后的题干", "difficulty": 3}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == created["version"] + 1
    assert resp.json()["stem"] == "更新后的题干"
    # 非法更新被 validate_question 拦截且不落库（version 不变）：基础题 difficulty=3 改进阶 tier 违规
    resp = client.put(f"/api/admin/questions/{qid}", json={"tier": "advanced"}, headers=admin_headers)
    assert resp.status_code == 400
    with SessionLocal() as db:
        assert db.get(Question, qid).version == created["version"] + 1
    assert client.put("/api/admin/questions/999999", json={"stem": "x"}, headers=admin_headers).status_code == 404


def test_question_delete_soft_retires(client, admin_headers, created_question_ids):
    created = client.post("/api/admin/questions", json=_valid_open_question(), headers=admin_headers).json()
    created_question_ids.append(created["id"])
    qid = created["id"]
    resp = client.delete(f"/api/admin/questions/{qid}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "retired"
    with SessionLocal() as db:  # 软删：行仍在，仅 status 置 retired
        row = db.get(Question, qid)
        assert row is not None and row.status == "retired"
    retired = client.get("/api/admin/questions", params={"status": "retired", "page_size": 100}, headers=admin_headers).json()
    assert any(q["id"] == qid for q in retired["items"])
    published = client.get("/api/admin/questions", params={"status": "published", "page_size": 100}, headers=admin_headers).json()
    assert all(q["id"] != qid for q in published["items"])


def test_question_crud_requires_admin(client):
    headers = _student_headers(client)
    assert client.get("/api/admin/questions", headers=headers).status_code == 403
    assert client.post("/api/admin/questions", json={}, headers=headers).status_code == 403
    assert client.put("/api/admin/questions/1", json={}, headers=headers).status_code == 403
    assert client.delete("/api/admin/questions/1", headers=headers).status_code == 403


# ---------- 人工复核队列 ----------

@pytest.fixture()
def review_item():
    """一条满足外键的复核条目（学员/会话/开放题/作答+报告+入队），用毕清理。"""
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        user = User(name="复核学员", student_no=f"RV-{suffix}", role="student")
        db.add(user)
        db.flush()
        session = AssessmentSession(user_id=user.id)
        db.add(session)
        db.flush()
        question = Question(
            code=f"D2-X{suffix[:4]}{suffix[4:6]}", dimension="D2", tier="basic", type="open",
            difficulty=3, stem=f"测试开放题{suffix}", tags=["考点"], rubric={"points": ["要点"], "anchors": {}},
        )
        db.add(question)
        db.flush()
        answer = SessionAnswer(
            session_id=session.id, question_id=question.id, question_code=question.code,
            dimension="D2", answer="学员的开放题作答", score=1.0, theta_after=3.0, seq=1,
        )
        db.add(answer)
        db.flush()
        report = build_report(db, session)  # chat_fn=None：模板建议，零 LLM
        db.add(report)
        db.flush()
        item = enqueue_review(
            db, question.code, session.id, answer.id,
            {"score": 1, "runs": [1, 4, 3], "rationale": "原始判题理由"},
            "对话题判题三跑分差过大",
        )
        db.commit()
        ids = {
            "user_id": user.id, "session_id": session.id, "question_id": question.id,
            "answer_id": answer.id, "report_id": report.id, "item_id": item.id,
            "question_code": question.code, "student_no": user.student_no, "stem": question.stem,
        }
    yield ids
    with SessionLocal() as db:
        db.query(ReviewQueue).filter(ReviewQueue.answer_id == ids["answer_id"]).delete()
        db.query(Report).filter(Report.id == ids["report_id"]).delete()
        db.query(SessionAnswer).filter(SessionAnswer.id == ids["answer_id"]).delete()
        db.query(Question).filter(Question.id == ids["question_id"]).delete()
        db.query(AssessmentSession).filter(AssessmentSession.id == ids["session_id"]).delete()
        db.query(User).filter(User.id == ids["user_id"]).delete()
        db.commit()


def test_review_queue_listing_fields(client, admin_headers, review_item):
    resp = client.get("/api/admin/review-queue", params={"status": "open"}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    items = [x for x in resp.json()["items"] if x["id"] == review_item["item_id"]]
    assert len(items) == 1
    item = items[0]
    assert item["question_stem"] == review_item["stem"]  # 题目 stem
    assert item["dimension"] == "D2"  # 维度
    assert item["student_no"] == review_item["student_no"]  # 学员代号（不暴露真实姓名）
    assert item["judge_raw"]["runs"] == [1, 4, 3]  # 判题原始结果
    assert item["reason"] == "对话题判题三跑分差过大"
    assert item["answer"] == "学员的开放题作答"  # 供复核者看原始作答
    assert item["status"] == "open" and item["resolved_score"] is None


def test_review_queue_status_filter(client, admin_headers, review_item):
    with SessionLocal() as db:
        item = db.get(ReviewQueue, review_item["item_id"])
        item.status = "resolved"
        item.resolved_score = 3
        db.commit()
    open_items = client.get("/api/admin/review-queue", params={"status": "open"}, headers=admin_headers).json()["items"]
    assert all(x["id"] != review_item["item_id"] for x in open_items)
    resolved = client.get("/api/admin/review-queue", params={"status": "resolved"}, headers=admin_headers).json()["items"]
    assert any(x["id"] == review_item["item_id"] and x["resolved_score"] == 3 for x in resolved)


def test_resolve_updates_answer_report_and_queue(client, admin_headers, review_item):
    """resolve 落分：answer.score、报告逐题 rationale 追加[人工复核]且 score 同步、
    开放题 is_correct 保持 None、队列 resolved；θ 不重放（theta_after 不变）。"""
    resp = client.post(
        f"/api/admin/review-queue/{review_item['item_id']}/resolve",
        json={"final_score": 3},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"
    with SessionLocal() as db:
        answer = db.get(SessionAnswer, review_item["answer_id"])
        assert answer.score == 3
        assert answer.is_correct is None  # 开放题保持 None
        item = db.get(ReviewQueue, review_item["item_id"])
        assert item.status == "resolved" and item.resolved_score == 3
        report = db.get(Report, review_item["report_id"])
        entry = next(x for x in report.answers if x["seq"] == answer.seq)
        assert entry["score"] == 3  # 报告以人工分为准
        assert "[人工复核]" in entry["rationale"]
        assert entry["theta_after"] == 3.0  # θ 不重放
    # 已处理条目不可重复 resolve
    assert (
        client.post(
            f"/api/admin/review-queue/{review_item['item_id']}/resolve",
            json={"final_score": 2},
            headers=admin_headers,
        ).status_code
        == 400
    )


def test_resolve_guardrails(client, admin_headers, review_item):
    assert (
        client.post("/api/admin/review-queue/999999/resolve", json={"final_score": 3}, headers=admin_headers).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/admin/review-queue/{review_item['item_id']}/resolve",
            json={"final_score": 5},  # 越界 0..4
            headers=admin_headers,
        ).status_code
        == 422
    )


def test_review_queue_roles(client, admin_headers, review_item):
    """队列 teacher/admin 均可见可复核（Global Constraints）；学员 403。"""
    student = _student_headers(client)
    assert client.get("/api/admin/review-queue", headers=student).status_code == 403
    assert (
        client.post(f"/api/admin/review-queue/{review_item['item_id']}/resolve", json={"final_score": 3}, headers=student).status_code
        == 403
    )
    # 教师账号（admin 创建）可查队列并复核
    username = f"t{uuid.uuid4().hex[:6]}"
    client.post("/api/admin/teachers", json={"name": "复核教师", "username": username, "password": "pass123456"}, headers=admin_headers)
    teacher = {"Authorization": f"Bearer {client.post('/api/auth/login', json={'username': username, 'password': 'pass123456'}).json()['token']}"}
    listing = client.get("/api/admin/review-queue", params={"status": "open"}, headers=teacher)
    assert listing.status_code == 200
    resolve = client.post(
        f"/api/admin/review-queue/{review_item['item_id']}/resolve", json={"final_score": 2}, headers=teacher
    )
    assert resolve.status_code == 200
