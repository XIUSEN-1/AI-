"""会话查询与断线续答（Task 1，spec §10）：GET /sessions/active、GET /sessions/{id} 完整视图。

权限口径：本人返回 SessionView+消息历史；teacher 仅本班学员且只给元数据不含消息；admin 全权；
其他学员 404（不泄露存在性）。另覆盖 M2b2 遗留加固：finish 线程 spawn 失败回滚 in_progress 可重试。
"""

import uuid

from fastapi.testclient import TestClient

import app.api.session_routes as session_routes
from app.db import SessionLocal
from app.models import AssessmentSession, SessionMessage
from conftest import finish_and_wait
from test_session_flow import _run_full_flow
from test_teacher import make_teacher, register_student


def _me(client: TestClient, headers: dict) -> dict:
    return client.get("/api/auth/me", headers=headers).json()


# ---------- GET /api/sessions/active ----------

def test_active_null_when_no_session(client):
    token = client.post("/api/auth/student", json={"name": "无会话学员", "student_no": f"RS-{uuid.uuid4().hex[:6]}"}).json()
    headers = {"Authorization": f"Bearer {token['token']}"}
    resp = client.get("/api/sessions/active", headers=headers)
    assert resp.status_code == 200
    assert resp.json() is None


def test_active_returns_latest_in_progress(client, bank):
    headers = {"Authorization": f"Bearer {client.post('/api/auth/student', json={'name': '续答学员', 'student_no': f'RS-{uuid.uuid4().hex[:6]}'}).json()['token']}"}
    first = client.post("/api/sessions", json={"mode": "quick"}, headers=headers).json()
    # 第一会话答 1 题后即为 active
    q = first["question"]
    client.post(
        f"/api/sessions/{first['session_id']}/answer",
        json={"question_id": q["id"], "answer": bank[q["code"]]["answer"], "time_spent": 10},
        headers=headers,
    )
    active = client.get("/api/sessions/active", headers=headers).json()
    assert active["session_id"] == first["session_id"]
    assert active["stage"] == "objective"
    assert set(active["progress"]) == {"D1", "D2", "D3", "D4", "D5", "D6"}
    assert len(active["started_at"]) >= 19

    # 更晚创建的会话优先（最近 in_progress）
    with SessionLocal() as db:
        newer = AssessmentSession(user_id=_me(client, headers)["id"])
        db.add(newer)
        db.commit()
        newer_id = newer.id
    active = client.get("/api/sessions/active", headers=headers).json()
    assert active["session_id"] == newer_id
    with SessionLocal() as db:  # 清理造数会话
        db.get(AssessmentSession, newer_id).status = "finished"
        db.commit()
    assert client.get("/api/sessions/active", headers=headers).json()["session_id"] == first["session_id"]


def test_active_ignores_finished(client, bank):
    headers = {"Authorization": f"Bearer {client.post('/api/auth/student', json={'name': '完测学员', 'student_no': f'RS-{uuid.uuid4().hex[:6]}'}).json()['token']}"}
    view = _run_full_flow(client, headers, bank, correct=True)
    finish_and_wait(client, headers, view["session_id"])
    assert client.get("/api/sessions/active", headers=headers).json() is None


# ---------- GET /api/sessions/{id} ----------

def test_get_session_full_view_with_messages(client, auth_headers, bank):
    view = _run_full_flow(client, auth_headers, bank, correct=False)
    sid = view["session_id"]
    finish_and_wait(client, auth_headers, sid)
    with SessionLocal() as db:
        db.add(SessionMessage(session_id=sid, question_id=1, channel="dialog", role="learner", content="学员第一条发言", seq=2))
        db.add(SessionMessage(session_id=sid, question_id=1, channel="dialog", role="examiner", content="考官追问", seq=3))
        db.commit()
    resp = client.get(f"/api/sessions/{sid}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == sid and body["status"] == "finished"
    assert set(body["progress"]) == {"D1", "D2", "D3", "D4", "D5", "D6"}
    messages = body["messages"]
    assert [m["seq"] for m in messages] == sorted(m["seq"] for m in messages)
    assert {"channel", "role", "content", "seq"} <= set(messages[0])
    assert any(m["content"] == "考官追问" and m["role"] == "examiner" for m in messages)


def test_get_session_other_student_404(client, bank):
    owner = {"Authorization": f"Bearer {client.post('/api/auth/student', json={'name': '会话主人', 'student_no': f'RS-{uuid.uuid4().hex[:6]}'}).json()['token']}"}
    view = _run_full_flow(client, owner, bank, correct=True)
    intruder = {"Authorization": f"Bearer {client.post('/api/auth/student', json={'name': '窥探学员', 'student_no': f'RS-{uuid.uuid4().hex[:6]}'}).json()['token']}"}
    assert client.get(f"/api/sessions/{view['session_id']}", headers=intruder).status_code == 404


def _teacher_of_class_with_student(client, bank):
    """教师建班→学员入班→学员跑一次 quick 会话；返回 (teacher 头, session_id)。"""
    teacher = make_teacher(client, uuid.uuid4().hex[:6])
    klass = client.post("/api/teacher/classes", json={"name": "续答班"}, headers=teacher).json()
    student = register_student(client, "S" + uuid.uuid4().hex[:5], klass["invite_code"])
    view = _run_full_flow(client, student, bank, correct=True)
    finish_and_wait(client, student, view["session_id"])
    return teacher, student, view["session_id"]


def test_get_session_teacher_metadata_without_messages(client, bank):
    teacher, student, sid = _teacher_of_class_with_student(client, bank)
    with SessionLocal() as db:  # 该班学员会话本无消息，补一条以验证"不因含消息而泄漏"
        db.add(SessionMessage(session_id=sid, question_id=1, channel="dialog", role="learner", content="不应出现在教师视图", seq=9))
        db.commit()
    resp = client.get(f"/api/sessions/{sid}", headers=teacher)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == sid
    assert "messages" not in body  # 教师端仅元数据不含消息
    # 本人在同一会话上能看到消息
    assert any(m["content"] == "不应出现在教师视图" for m in client.get(f"/api/sessions/{sid}", headers=student).json()["messages"])


def test_get_session_teacher_other_class_403(client, bank):
    teacher, _, sid = _teacher_of_class_with_student(client, bank)
    other = make_teacher(client, uuid.uuid4().hex[:6])
    assert client.get(f"/api/sessions/{sid}", headers=other).status_code == 403


def test_get_session_admin_metadata_without_messages(client, bank):
    _, _, sid = _teacher_of_class_with_student(client, bank)
    admin = {"Authorization": f"Bearer {client.post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'}).json()['token']}"}
    resp = client.get(f"/api/sessions/{sid}", headers=admin)
    assert resp.status_code == 200
    assert "messages" not in resp.json()
    assert client.get("/api/sessions/999999", headers=admin).status_code == 404


# ---------- finish spawn 失败回滚（M2b2 遗留加固） ----------

def test_finish_spawn_failure_rolls_back_and_retry(client, auth_headers, monkeypatch):
    start = client.post("/api/sessions", json={"mode": "quick"}, headers=auth_headers).json()
    sid = start["session_id"]

    def boom(*args, **kwargs):
        raise RuntimeError("无法创建新线程")

    monkeypatch.setattr(session_routes.threading, "Thread", boom)
    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert resp.status_code == 503, resp.text
    status = client.get(f"/api/sessions/{sid}/status", headers=auth_headers).json()
    assert status["status"] == "in_progress", "spawn 失败必须回滚 judging 占位"
    # 回滚后可重试成功
    monkeypatch.undo()
    finish_and_wait(client, auth_headers, sid)
