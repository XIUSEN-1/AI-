"""对话式测评考官端点（spec §6.2 / §5.3 三档追问）：SSE 流式、留痕、切题与阶段守卫。

单测零真实 LLM 调用：chat_completion/chat_stream 以 monkeypatch 注入 MockChat/MockStream。
"""

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.session_routes import DIALOG_CLOSING
from app.db import SessionLocal
from app.llm.mock import MockChat, MockStream
from app.llm.provider import ProviderUnavailableError
from app.models import AssessmentSession, SessionMessage
from test_stage_machine import _run_objective


def _start_dialog(client: TestClient, headers: dict, bank: dict) -> dict:
    """答完客观阶段进入 dialog，返回最后一个视图（question 为首道对话题 D3-T04）。"""
    view = _run_objective(client, headers, bank, mode="full")
    assert view["stage"] == "dialog"
    return view


def _sse_events(resp) -> list[dict]:
    events = []
    for line in resp.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line.removeprefix("data: ")))
    return events


def _messages(session_id: int) -> list[SessionMessage]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(SessionMessage)
                .where(SessionMessage.session_id == session_id)
                .order_by(SessionMessage.seq)
            )
        )


# ---------- dialog/start ----------


def test_dialog_start_returns_question_and_persists_opening(monkeypatch, client, auth_headers, bank):
    view = _start_dialog(client, auth_headers, bank)
    sid, q = view["session_id"], view["question"]
    monkeypatch.setattr("app.api.dialog_routes.chat_completion", MockChat(["同学你好，请听题。"]))
    resp = client.post(f"/api/sessions/{sid}/dialog/start", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["opening"] == "同学你好，请听题。"
    assert body["question"]["code"] == q["code"]
    assert body["question"]["dialog_turns_taken"] == 0
    assert "answer" not in body["question"] and "rubric" not in body["question"]
    msgs = _messages(sid)
    assert [(m.role, m.channel, m.seq) for m in msgs] == [("examiner", "dialog", 1)]
    assert msgs[0].content == "同学你好，请听题。"
    assert msgs[0].question_id == q["id"]


def test_dialog_start_falls_back_to_template_without_llm(monkeypatch, client, auth_headers, bank):
    view = _start_dialog(client, auth_headers, bank)
    sid, q = view["session_id"], view["question"]

    def broken(*args, **kwargs):
        raise ProviderUnavailableError("缺少 DEEPSEEK_API_KEY，无法调用 LLM")

    monkeypatch.setattr("app.api.dialog_routes.chat_completion", broken)
    resp = client.post(f"/api/sessions/{sid}/dialog/start", headers=auth_headers)
    assert resp.status_code == 200
    opening = resp.json()["opening"]
    assert opening.startswith("请结合你的实际经验，谈谈") and q["stem"] in opening
    msgs = _messages(sid)
    assert [m.role for m in msgs] == ["examiner"]  # 回退模板同样落库，且不消耗追问轮次（无 learner 消息）


def test_dialog_start_is_idempotent(monkeypatch, client, auth_headers, bank):
    view = _start_dialog(client, auth_headers, bank)
    sid = view["session_id"]
    # MockChat 仅 1 条预设：第二次 start 若再调 LLM 会 AssertionError → 500，测试随之失败
    monkeypatch.setattr("app.api.dialog_routes.chat_completion", MockChat(["开场白"]))
    first = client.post(f"/api/sessions/{sid}/dialog/start", headers=auth_headers).json()
    second = client.post(f"/api/sessions/{sid}/dialog/start", headers=auth_headers).json()
    assert first["opening"] == second["opening"] == "开场白"
    assert [m.role for m in _messages(sid)] == ["examiner"]


# ---------- dialog/turn（SSE）----------


def test_dialog_turn_streams_deltas_and_persists(monkeypatch, client, auth_headers, bank):
    view = _start_dialog(client, auth_headers, bank)
    sid, q = view["session_id"], view["question"]
    monkeypatch.setattr("app.api.dialog_routes.chat_completion", MockChat(["开场白"]))
    client.post(f"/api/sessions/{sid}/dialog/start", headers=auth_headers)
    stream = MockStream(["你好", "，考官"])
    monkeypatch.setattr("app.api.dialog_routes.chat_stream", stream)
    with client.stream(
        "POST", f"/api/sessions/{sid}/dialog/turn", json={"message": "我的回答"}, headers=auth_headers
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(resp)
    assert events == [{"delta": "你好"}, {"delta": "，考官"}, {"done": True, "turns": 1}]
    assert stream.calls[0]["model_role"] == "chat"
    msgs = _messages(sid)
    assert [(m.role, m.channel, m.seq) for m in msgs] == [
        ("examiner", "dialog", 1),
        ("learner", "dialog", 2),
        ("examiner", "dialog", 3),
    ]
    assert [m.content for m in msgs][1:] == ["我的回答", "你好，考官"]
    assert all(m.question_id == q["id"] for m in msgs)


def test_dialog_turn_system_escalates_by_turn(monkeypatch, client, auth_headers, bank):
    """三档追问按已轮次选 system：澄清 → 深挖 → 反例；历史逐轮累积。"""
    view = _start_dialog(client, auth_headers, bank)
    sid = view["session_id"]
    monkeypatch.setattr("app.api.dialog_routes.chat_completion", MockChat(["开场白", "第二题开场白"]))
    client.post(f"/api/sessions/{sid}/dialog/start", headers=auth_headers)
    stream = MockStream(["回一", "回二", "回三"])
    monkeypatch.setattr("app.api.dialog_routes.chat_stream", stream)
    keywords = ["澄清", "深挖", "反例"]
    for i in range(3):
        with client.stream(
            "POST", f"/api/sessions/{sid}/dialog/turn", json={"message": f"第{i + 1}轮发言"}, headers=auth_headers
        ) as resp:
            events = _sse_events(resp)
        assert events[-1] == {"done": True, "turns": i + 1}
        messages = stream.calls[i]["messages"]
        assert messages[0]["role"] == "system"
        assert keywords[i] in messages[0]["content"]
        assert f"第{i + 1}轮发言" in messages[-1]["content"]
        assert [m["role"] for m in messages].count("user") == i + 1  # 历史含全部学员发言
    # 满 3 轮本题自动结束：start 返回第二道对话题（θ 并列稳定序 → D4-T04）
    resp = client.post(f"/api/sessions/{sid}/dialog/start", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["question"]["code"] == "D4-T04"


def test_dialog_turn_provider_error_emits_error_event(monkeypatch, client, auth_headers, bank):
    view = _start_dialog(client, auth_headers, bank)
    sid = view["session_id"]
    monkeypatch.setattr("app.api.dialog_routes.chat_completion", MockChat(["开场白"]))
    client.post(f"/api/sessions/{sid}/dialog/start", headers=auth_headers)

    def broken(*args, **kwargs):
        raise ProviderUnavailableError("缺少 DEEPSEEK_API_KEY，无法调用 LLM")

    monkeypatch.setattr("app.api.dialog_routes.chat_stream", broken)
    with client.stream(
        "POST", f"/api/sessions/{sid}/dialog/turn", json={"message": "我的回答"}, headers=auth_headers
    ) as resp:
        assert resp.status_code == 200
        events = _sse_events(resp)
    assert events[0] == {"error": "AI 服务暂不可用，请稍后重试或跳过本题"}
    assert events[1] == {"done": True, "turns": 1}
    assert [m.role for m in _messages(sid)] == ["examiner", "learner"]  # 学员发言已留痕，考官无回复
    with SessionLocal() as db:  # 会话不中断：学员仍可跳过本题
        session = db.get(AssessmentSession, sid)
        assert session.status == "in_progress" and session.stage == "dialog"
    assert client.post(f"/api/sessions/{sid}/dialog/finish-question", headers=auth_headers).status_code == 200


def test_dialog_turn_requires_start_first(monkeypatch, client, auth_headers, bank):
    view = _start_dialog(client, auth_headers, bank)
    resp = client.post(
        f"/api/sessions/{view['session_id']}/dialog/turn", json={"message": "还没开始"}, headers=auth_headers
    )
    assert resp.status_code == 400
    assert "dialog/start" in resp.json()["detail"]


def test_dialog_turn_validates_message_length(client, auth_headers, bank):
    view = _start_dialog(client, auth_headers, bank)
    url = f"/api/sessions/{view['session_id']}/dialog/turn"
    assert client.post(url, json={"message": ""}, headers=auth_headers).status_code == 422
    assert client.post(url, json={"message": "长" * 2001}, headers=auth_headers).status_code == 422


# ---------- dialog/finish-question（切题/切阶段）----------


def test_dialog_finish_question_switches_next_then_practical(client, auth_headers, bank):
    view = _start_dialog(client, auth_headers, bank)
    sid = view["session_id"]
    resp = client.post(f"/api/sessions/{sid}/dialog/finish-question", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["stage"] == "dialog" and body["question"]["code"] == "D4-T04"  # 切到第二道对话题
    closing = [m for m in _messages(sid) if m.content == DIALOG_CLOSING]
    assert len(closing) == 1 and closing[0].role == "examiner"
    resp = client.post(f"/api/sessions/{sid}/dialog/finish-question", headers=auth_headers)
    body = resp.json()
    assert body["stage"] == "practical" and body["question"]["type"] == "practical"  # 两题结束切实操
    assert body["question"]["code"] == "D5-T05"
    with SessionLocal() as db:
        assert db.get(AssessmentSession, sid).stage == "practical"


# ---------- 守卫 ----------


def test_dialog_routes_rejected_outside_dialog_stage(client, auth_headers):
    view = client.post("/api/sessions", json={"mode": "full"}, headers=auth_headers).json()
    for path in ("dialog/start", "dialog/turn", "dialog/finish-question"):
        resp = client.post(
            f"/api/sessions/{view['session_id']}/{path}",
            json={"message": "hi"} if path == "dialog/turn" else None,
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "当前阶段不支持对话式测评"


def test_dialog_routes_reject_other_users_session(client, auth_headers, bank):
    view = _start_dialog(client, auth_headers, bank)
    resp = client.post("/api/auth/student", json={"name": "路人学员", "student_no": "DLG999"})
    other = {"Authorization": f"Bearer {resp.json()['token']}"}
    for path in ("dialog/start", "dialog/finish-question"):
        assert (
            client.post(f"/api/sessions/{view['session_id']}/{path}", headers=other).status_code == 404
        )


def test_objective_answer_rejected_after_stage_leaves_objective(client, auth_headers, bank):
    """客观题作答仅限 objective 阶段：进入对话阶段后旧题重交 → 400。"""
    view = client.post("/api/sessions", json={"mode": "full"}, headers=auth_headers).json()
    asked_id = None
    while view["question"] is not None and view.get("stage", "objective") == "objective":
        q = view["question"]
        asked_id = asked_id or q["id"]
        view = client.post(
            f"/api/sessions/{view['session_id']}/answer",
            json={"question_id": q["id"], "answer": bank[q["code"]]["answer"], "time_spent": 30},
            headers=auth_headers,
        ).json()
    assert view["stage"] == "dialog"
    resp = client.post(
        f"/api/sessions/{view['session_id']}/answer",
        json={"question_id": asked_id, "answer": True, "time_spent": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "当前阶段不支持客观题作答"
