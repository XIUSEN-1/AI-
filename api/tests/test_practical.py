"""实操任务端点（spec §6.3 真实 AI 协作窗）：任务卡、SSE 协作窗、产物提交与 ready 门禁。

单测零真实 LLM 调用：chat_stream 以 monkeypatch 注入 MockStream。实操 system 服务端注入，
不含题面与判分信息（不泄题）。
"""

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.practical_routes import PRACTICAL_SYSTEM
from app.db import SessionLocal
from app.llm.mock import MockStream
from app.llm.provider import ProviderUnavailableError
from app.models import AssessmentSession, SessionMessage
from test_stage_machine import _run_objective

ARTIFACT = "最终周计划：" + "本周目标是完成接口联调，分工与里程碑如下。" * 10  # 约 250 字


def _reach_practical(client: TestClient, headers: dict, bank: dict) -> dict:
    """答完客观、结束两道对话题，返回实操阶段的视图。"""
    view = _run_objective(client, headers, bank, mode="full")
    assert view["stage"] == "dialog"
    for _ in range(2):  # 结束两道对话题（无需开场即可跳过）
        resp = client.post(f"/api/sessions/{view['session_id']}/dialog/finish-question", headers=headers)
        assert resp.status_code == 200
        view = resp.json()
    assert view["stage"] == "practical"
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


# ---------- practical/task ----------


def test_practical_task_returns_brief(client, auth_headers, bank):
    view = _reach_practical(client, auth_headers, bank)
    resp = client.get(f"/api/sessions/{view['session_id']}/practical/task", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    q = body["question"]
    assert q["type"] == "practical" and q["code"] == "D5-T05"
    assert "周计划" in q["stem"]  # 题面 + 产物要求随 stem 下发
    assert "answer" not in q and "rubric" not in q  # 不泄漏判分信息
    assert body["artifact_min"] == 200 and body["artifact_max"] == 5000


def test_practical_task_rejected_outside_practical_stage(client, auth_headers, bank):
    view = _run_objective(client, auth_headers, bank, mode="full")  # 尚在 dialog 阶段
    resp = client.get(f"/api/sessions/{view['session_id']}/practical/task", headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "当前阶段不支持实操任务"


# ---------- practical/chat（SSE 协作窗）----------


def test_practical_chat_streams_and_persists(monkeypatch, client, auth_headers, bank):
    view = _reach_practical(client, auth_headers, bank)
    sid, q = view["session_id"], view["question"]
    stream = MockStream(["先把项目", "背景告诉我。"])
    monkeypatch.setattr("app.api.practical_routes.chat_stream", stream)
    with client.stream(
        "POST", f"/api/sessions/{sid}/practical/chat", json={"message": "帮我做周计划"}, headers=auth_headers
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(resp)
    assert events == [{"delta": "先把项目"}, {"delta": "背景告诉我。"}, {"done": True, "turns": 1}]
    system = stream.calls[0]["messages"][0]  # 服务端注入通用助手 system：不含题面与判分信息
    assert system == {"role": "system", "content": PRACTICAL_SYSTEM}
    assert "周计划" not in PRACTICAL_SYSTEM and "rubric" not in PRACTICAL_SYSTEM
    assert stream.calls[0]["model_role"] == "chat"
    msgs = [m for m in _messages(sid) if m.channel == "practical"]
    # seq 会话内全局递增：两道对话题的结束标记已占 seq 1/2
    assert [(m.role, m.seq) for m in msgs] == [("learner", 3), ("assistant", 4)]
    assert msgs[0].content == "帮我做周计划" and msgs[1].content == "先把项目背景告诉我。"
    assert all(m.question_id == q["id"] for m in msgs)


def test_practical_chat_carries_history(monkeypatch, client, auth_headers, bank):
    view = _reach_practical(client, auth_headers, bank)
    sid = view["session_id"]
    stream = MockStream(["好的。", "继续。"])
    monkeypatch.setattr("app.api.practical_routes.chat_stream", stream)
    for i in range(2):
        with client.stream(
            "POST", f"/api/sessions/{sid}/practical/chat", json={"message": f"第{i + 1}轮"}, headers=auth_headers
        ) as resp:
            _sse_events(resp)
    messages = stream.calls[1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]  # 全历史随行
    assert messages[-1]["content"] == "第2轮" and messages[1]["content"] == "第1轮"


def test_practical_chat_provider_error_emits_error_event(monkeypatch, client, auth_headers, bank):
    view = _reach_practical(client, auth_headers, bank)
    sid = view["session_id"]

    def broken(*args, **kwargs):
        raise ProviderUnavailableError("缺少 DEEPSEEK_API_KEY，无法调用 LLM")

    monkeypatch.setattr("app.api.practical_routes.chat_stream", broken)
    with client.stream(
        "POST", f"/api/sessions/{sid}/practical/chat", json={"message": "在吗"}, headers=auth_headers
    ) as resp:
        assert resp.status_code == 200
        events = _sse_events(resp)
    assert events[0] == {"error": "AI 服务暂不可用，请稍后重试或跳过本题"}
    assert events[1] == {"done": True, "turns": 1}
    assert [m.role for m in _messages(sid) if m.channel == "practical"] == ["learner"]


def test_practical_chat_caps_at_20_turns(monkeypatch, client, auth_headers, bank):
    view = _reach_practical(client, auth_headers, bank)
    sid = view["session_id"]
    stream = MockStream(["。"] * 20)
    monkeypatch.setattr("app.api.practical_routes.chat_stream", stream)
    for i in range(20):
        with client.stream(
            "POST", f"/api/sessions/{sid}/practical/chat", json={"message": f"第{i + 1}轮"}, headers=auth_headers
        ) as resp:
            assert resp.status_code == 200
    resp = client.post(f"/api/sessions/{sid}/practical/chat", json={"message": "第21轮"}, headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "实操协作对话已达 20 轮上限"


def test_practical_chat_validates_message_length(client, auth_headers, bank):
    view = _reach_practical(client, auth_headers, bank)
    url = f"/api/sessions/{view['session_id']}/practical/chat"
    assert client.post(url, json={"message": ""}, headers=auth_headers).status_code == 422
    assert client.post(url, json={"message": "长" * 2001}, headers=auth_headers).status_code == 422


# ---------- practical/submit ----------


def test_practical_submit_marks_ready_and_finishes(client, auth_headers, bank):
    view = _reach_practical(client, auth_headers, bank)
    sid, q = view["session_id"], view["question"]
    resp = client.post(f"/api/sessions/{sid}/practical/submit", json={"artifact": ARTIFACT}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"submitted": True, "stage": "ready"}
    submits = [m for m in _messages(sid) if m.role == "submit"]
    assert len(submits) == 1
    assert submits[0].channel == "practical" and submits[0].content == ARTIFACT
    assert submits[0].question_id == q["id"]
    with SessionLocal() as db:
        assert db.get(AssessmentSession, sid).stage == "ready"
    finish = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)  # ready 放行生成报告
    assert finish.status_code == 200 and finish.json()["report_id"]


def test_practical_submit_validates_artifact_length(client, auth_headers, bank):
    view = _reach_practical(client, auth_headers, bank)
    url = f"/api/sessions/{view['session_id']}/practical/submit"
    assert client.post(url, json={"artifact": "短" * 199}, headers=auth_headers).status_code == 422
    assert client.post(url, json={"artifact": "长" * 5001}, headers=auth_headers).status_code == 422


def test_practical_chat_and_submit_rejected_after_submit(client, auth_headers, bank):
    view = _reach_practical(client, auth_headers, bank)
    sid = view["session_id"]
    assert (
        client.post(f"/api/sessions/{sid}/practical/submit", json={"artifact": ARTIFACT}, headers=auth_headers).status_code
        == 200
    )
    chat = client.post(f"/api/sessions/{sid}/practical/chat", json={"message": "还能聊吗"}, headers=auth_headers)
    assert chat.status_code == 400 and chat.json()["detail"] == "当前阶段不支持实操任务"
    again = client.post(f"/api/sessions/{sid}/practical/submit", json={"artifact": ARTIFACT}, headers=auth_headers)
    assert again.status_code == 400 and again.json()["detail"] == "当前阶段不支持实操任务"


def test_practical_routes_reject_other_users_session(client, auth_headers, bank):
    view = _reach_practical(client, auth_headers, bank)
    resp = client.post("/api/auth/student", json={"name": "路人学员", "student_no": "PRC999"})
    other = {"Authorization": f"Bearer {resp.json()['token']}"}
    assert (
        client.get(f"/api/sessions/{view['session_id']}/practical/task", headers=other).status_code == 404
    )
    assert (
        client.post(
            f"/api/sessions/{view['session_id']}/practical/submit", json={"artifact": ARTIFACT}, headers=other
        ).status_code
        == 404
    )
