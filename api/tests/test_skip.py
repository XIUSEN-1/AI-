"""跳过功能（M2b-hotfix Task 1）：对话题与实操题支持跳过。

裁定口径：跳过=不判分、不回灌 θ、SessionAnswer score=None、报告 rationale 标注"学员跳过"，
不作负向评价（区别于"学员未作答"记 0 分）。对话跳过复用闭题/切题语义；实操跳过直接置 ready。
异步管线下的全跳回归（真实后台线程）见 tests/test_async_judging.py。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.session_routes import DIALOG_SKIPPED, PRACTICAL_SKIPPED
from app.db import SessionLocal
from app.llm.mock import MockChat
from app.models import AssessmentSession, Report, SessionAnswer, SessionMessage
from conftest import finish_and_wait
from test_finish_judging import ARTIFACT, RoutedChat, _ADVICE, _add_messages, _good
from test_stage_machine import _run_objective


@pytest.fixture(autouse=True)
def _inline_judging(monkeypatch):
    """本模块断言判题调用次数与顺序（MockChat 按序号喂响应）：内联单 worker 执行。"""
    monkeypatch.setattr("app.api.session_routes._ASYNC_JUDGING", False)


def _messages(sid: int) -> list[SessionMessage]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(SessionMessage).where(SessionMessage.session_id == sid).order_by(SessionMessage.seq)
            )
        )


def _snapshot(sid: int) -> dict:
    with SessionLocal() as db:
        return db.get(AssessmentSession, sid).theta_snapshot


def _answers(sid: int) -> dict[str, SessionAnswer]:
    with SessionLocal() as db:
        rows = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == sid)).all()
        return {r.question_code: r for r in rows}


def _report_body(client: TestClient, headers: dict, sid: int) -> dict:
    with SessionLocal() as db:
        report_id = db.scalar(select(Report).where(Report.session_id == sid)).id
    return client.get(f"/api/reports/{report_id}", headers=headers).json()


def _skip_all_dialog(client: TestClient, headers: dict, sid: int) -> dict:
    """连续跳过两道对话题，返回实操阶段视图。"""
    for _ in range(2):
        resp = client.post(f"/api/sessions/{sid}/dialog/skip", headers=headers)
        assert resp.status_code == 200
        view = resp.json()
    assert view["stage"] == "practical" and view["question"]["type"] == "practical"
    return view


# ---------- 对话跳过：切题与全跳直达实操 ----------


def test_dialog_skip_switches_next_then_practical(client, auth_headers, bank):
    view = _run_objective(client, auth_headers, bank, mode="full")  # 首道对话题 D3-T04
    sid = view["session_id"]

    resp = client.post(f"/api/sessions/{sid}/dialog/skip", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["stage"] == "dialog" and body["question"]["code"] == "D4-T04"  # 切到第二道
    marks = [m for m in _messages(sid) if m.content == DIALOG_SKIPPED]
    assert len(marks) == 1 and marks[0].role == "examiner" and marks[0].channel == "dialog"
    assert marks[0].question_id == view["question"]["id"]

    resp = client.post(f"/api/sessions/{sid}/dialog/skip", headers=auth_headers)
    body = resp.json()
    assert body["stage"] == "practical" and body["question"]["type"] == "practical"  # 全跳切实操
    with SessionLocal() as db:
        assert db.get(AssessmentSession, sid).stage == "practical"


# ---------- 实操跳过：置 ready ----------


def test_practical_skip_marks_ready(client, auth_headers, bank):
    view = _run_objective(client, auth_headers, bank, mode="full")
    sid = view["session_id"]
    view = _skip_all_dialog(client, auth_headers, sid)

    resp = client.post(f"/api/sessions/{sid}/practical/skip", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"skipped": True, "stage": "ready"}

    practical_msgs = [m for m in _messages(sid) if m.channel == "practical"]
    assert [(m.role, m.content) for m in practical_msgs] == [("examiner", PRACTICAL_SKIPPED)]
    assert all(m.question_id == view["question"]["id"] for m in practical_msgs)
    with SessionLocal() as db:
        assert db.get(AssessmentSession, sid).stage == "ready"


# ---------- 全跳 finish：六维纯客观 θ、score=None、报告标注"学员跳过" ----------


def test_all_skipped_finish_keeps_objective_theta(monkeypatch, client, auth_headers, bank):
    view = _run_objective(client, auth_headers, bank, mode="full")
    sid = view["session_id"]
    _skip_all_dialog(client, auth_headers, sid)
    resp = client.post(f"/api/sessions/{sid}/practical/skip", headers=auth_headers)
    assert resp.status_code == 200

    before = _snapshot(sid)
    chat = MockChat([_ADVICE])  # 跳过题零判题调用：finish 仅报告建议一次 LLM 调用
    monkeypatch.setattr("app.api.session_routes.chat_completion", chat)

    body = finish_and_wait(client, auth_headers, sid)
    assert len(chat.calls) == 1

    after = _snapshot(sid)
    for d in after:  # 六维纯客观 θ：D3/D4/D5 均不回灌
        assert after[d] == before[d]

    answers = _answers(sid)
    for code in ("D3-T04", "D4-T04", "D5-T05"):
        assert answers[code].score is None
        assert answers[code].is_correct is None

    report = _report_body(client, auth_headers, sid)
    subjective = [a for a in report["answers"] if a["type"] in ("open", "practical")]
    assert len(subjective) == 3
    assert all(a["score"] is None and a["rationale"] == "学员跳过" for a in subjective)


# ---------- 混合路径：跳过一题，其余正常作答判分 ----------


def test_mixed_skip_and_normal_answers(monkeypatch, client, auth_headers, bank):
    view = _run_objective(client, auth_headers, bank, mode="full")
    sid = view["session_id"]

    resp = client.post(f"/api/sessions/{sid}/dialog/skip", headers=auth_headers)  # 跳过 D3-T04
    q2 = resp.json()["question"]  # D4-T04 正常作答
    _add_messages(sid, q2["id"], "dialog", "learner", ["我的正常作答"])
    resp = client.post(f"/api/sessions/{sid}/dialog/finish-question", headers=auth_headers)
    assert resp.json()["stage"] == "practical"
    prac = resp.json()["question"]
    _add_messages(sid, prac["id"], "practical", "learner", ["帮我拆解任务"])
    resp = client.post(f"/api/sessions/{sid}/practical/submit", json={"artifact": ARTIFACT}, headers=auth_headers)
    assert resp.status_code == 200

    before = _snapshot(sid)
    # 判题调用（D3 跳过零调用）：D4 对话判分、D5 产物判分、过程量表、报告建议；
    # 实操双通道并发 → 按内容路由响应（RoutedChat），不再按序号喂
    chat = RoutedChat({"情境题（D4）": 3, "周计划": 4})
    monkeypatch.setattr("app.api.session_routes.chat_completion", chat)

    finish_and_wait(client, auth_headers, sid)
    assert len(chat.calls) == 6

    answers = _answers(sid)
    assert answers["D3-T04"].score is None
    assert answers["D4-T04"].score == 3
    assert answers["D5-T05"].score == 3.4  # 过程 3.0×0.6 + 产物 4×0.4

    after = _snapshot(sid)
    assert after["D3"] == before["D3"]  # 跳过维度不回灌
    assert after["D4"]["n"] == before["D4"]["n"] + 1
    assert after["D5"]["n"] == before["D5"]["n"] + 1

    report = _report_body(client, auth_headers, sid)
    d3_item = next(a for a in report["answers"] if a["dimension"] == "D3" and a["type"] == "open")
    d4_item = next(a for a in report["answers"] if a["dimension"] == "D4" and a["type"] == "open")
    assert d3_item["rationale"] == "学员跳过" and d3_item["score"] is None
    assert d4_item["rationale"] == "判题理由3" and d4_item["score"] == 3


# ---------- 跳过与未作答的区分 ----------


def test_skip_marked_but_answered_still_judged(monkeypatch, client, auth_headers, bank):
    """学员发言后再跳过（发言留痕非空）：判题照常进行，跳过标记不生效。"""
    view = _run_objective(client, auth_headers, bank, mode="full")
    sid = view["session_id"]
    _add_messages(sid, view["question"]["id"], "dialog", "learner", ["我已经说过了"])
    resp = client.post(f"/api/sessions/{sid}/dialog/skip", headers=auth_headers)
    assert resp.status_code == 200
    resp = client.post(f"/api/sessions/{sid}/dialog/skip", headers=auth_headers)  # 第二道直接跳
    assert resp.json()["stage"] == "practical"
    resp = client.post(f"/api/sessions/{sid}/practical/skip", headers=auth_headers)
    assert resp.status_code == 200

    chat = MockChat([_good(2), _good(2), _ADVICE])  # 仅 D3 正常判分 + 建议
    monkeypatch.setattr("app.api.session_routes.chat_completion", chat)
    finish_and_wait(client, auth_headers, sid)
    assert len(chat.calls) == 3

    answers = _answers(sid)
    assert answers["D3-T04"].score == 2  # 有发言 → 正常判分回灌
    assert answers["D4-T04"].score is None
    assert answers["D5-T05"].score is None
    with SessionLocal() as db:
        assert db.get(AssessmentSession, sid).status == "finished"
