"""finish 统一判题（Task 5）：对话题整卷判分、实操双通道（过程 5 项量表 + 产物 rubric）、
θ 回灌、degraded/needs_review 入复核队列、报告 answers 回显开放题 rationale/score。

单测零真实 LLM 调用：session_routes.chat_completion 以 monkeypatch 注入 MockChat/抛错替身。
裁定口径：学员零有效发言（无 learner 消息，实操无产物）的题不判分不回灌 θ、score 记 0、
rationale 注明"学员未作答"。
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.engine.adaptive import DimensionState, update
from app.llm.mock import MockChat
from app.llm.provider import ProviderUnavailableError
from app.models import AssessmentSession, ReviewQueue, SessionAnswer, SessionMessage
from test_stage_machine import _run_objective

ARTIFACT = "最终周计划：" + "本周目标是完成接口联调，分工与里程碑如下。" * 10  # ≥200 字


def _good(score: int) -> str:
    return json.dumps(
        {
            "score": score,
            "hits": ["命中要点"],
            "strengths": ["优点"],
            "gaps": ["不足"],
            "rationale": f"判题理由{score}",
        },
        ensure_ascii=False,
    )


def _process_json(**overrides) -> str:
    payload = {"clarity": 3, "decomposition": 3, "context": 3, "iteration": 3, "integration": 3}
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


_ADVICE = json.dumps({"advice": ["LLM 学习建议"]}, ensure_ascii=False)


def _add_messages(sid: int, qid: int, channel: str, role: str, contents: list[str]) -> None:
    with SessionLocal() as db:
        base = (
            db.scalar(select(func.max(SessionMessage.seq)).where(SessionMessage.session_id == sid))
            or 0
        )
        for i, content in enumerate(contents, start=1):
            db.add(
                SessionMessage(
                    session_id=sid, question_id=qid, channel=channel, role=role,
                    content=content, seq=base + i,
                )
            )
        db.commit()


def _seed_dialog_and_reach_practical(client: TestClient, headers: dict, bank: dict, dialog_answers: dict):
    """答完客观进入 dialog；dialog_answers 按 code 播种 learner 消息，然后跳过全部对话题到 practical。
    返回 (session_id, practical 视图)。"""
    view = _run_objective(client, headers, bank, mode="full")
    sid = view["session_id"]
    q1 = view["question"]  # 全对 → θ 并列稳定序，对话题为 D3-T04、D4-T04
    if dialog_answers.get(q1["code"]):
        _add_messages(sid, q1["id"], "dialog", "learner", dialog_answers[q1["code"]])
    view = client.post(f"/api/sessions/{sid}/dialog/finish-question", headers=headers).json()
    q2 = view["question"]
    if dialog_answers.get(q2["code"]):
        _add_messages(sid, q2["id"], "dialog", "learner", dialog_answers[q2["code"]])
    view = client.post(f"/api/sessions/{sid}/dialog/finish-question", headers=headers).json()
    assert view["stage"] == "practical" and view["question"]["type"] == "practical"
    return sid, view


def _ready(
    client: TestClient,
    headers: dict,
    bank: dict,
    *,
    dialog_answers: dict | None = None,
    practical_prompts: list[str] | None = None,
    artifact: str | None = ARTIFACT,
) -> tuple[int, dict]:
    """构造 ready 态会话：客观答完 + 对话题按需播种 + 实操协作窗按需播种 + 交产物。"""
    sid, view = _seed_dialog_and_reach_practical(client, headers, bank, dialog_answers or {})
    if practical_prompts:
        _add_messages(sid, view["question"]["id"], "practical", "learner", practical_prompts)
    if artifact is not None:
        resp = client.post(f"/api/sessions/{sid}/practical/submit", json={"artifact": artifact}, headers=headers)
        assert resp.status_code == 200
    return sid, view


def _snapshot(sid: int) -> dict:
    with SessionLocal() as db:
        return db.get(AssessmentSession, sid).theta_snapshot


def _answers(sid: int) -> dict[str, SessionAnswer]:
    with SessionLocal() as db:
        rows = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == sid)).all()
        return {r.question_code: r for r in rows}


def _reviews(sid: int) -> list[ReviewQueue]:
    with SessionLocal() as db:
        return list(
            db.scalars(select(ReviewQueue).where(ReviewQueue.session_id == sid)).all()
        )


def _report_body(client: TestClient, headers: dict, sid: int) -> dict:
    report_id = None
    with SessionLocal() as db:
        from app.models import Report

        row = db.scalar(select(Report).where(Report.session_id == sid))
        report_id = row.id
    return client.get(f"/api/reports/{report_id}", headers=headers).json()


def _open_items(body: dict, dimension: str) -> list[dict]:
    return [a for a in body["answers"] if a["dimension"] == dimension and a["type"] == "open"]


def _practical_item(body: dict) -> dict:
    return next(a for a in body["answers"] if a["type"] == "practical")


# ---------- happy path：判分 → 回灌 → 报告回显 ----------


def test_finish_judges_all_channels_and_feeds_theta(monkeypatch, client, auth_headers, bank):
    sid, _ = _ready(
        client,
        auth_headers,
        bank,
        dialog_answers={"D3-T04": ["我的方案一", "我的方案二"], "D4-T04": ["我的回答"]},
        practical_prompts=["帮我拆解任务", "这个方案再改改"],
    )
    before = _snapshot(sid)
    # mock 响应顺序 = finish 判题顺序：D3 双跑、D4 双跑、D5 产物双跑、D5 过程量表、报告建议
    chat = MockChat([_good(3), _good(3), _good(2), _good(2), _good(4), _good(4), _process_json(), _ADVICE])
    monkeypatch.setattr("app.api.session_routes.chat_completion", chat)

    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert resp.status_code == 200

    # ---- 判题调用形状：submission=learner 消息拼接，learner_prompts 随行，低温 JSON 模式
    assert len(chat.calls) == 8
    first = chat.calls[0]
    assert first["model_role"] == "judge" and first["temperature"] == 0.0 and first["json_mode"] is True
    text = "\n".join(m["content"] for m in first["messages"])
    assert "我的方案一" in text and "我的方案二" in text
    assert "给出检索增强/知识库等具体方案" in text  # D3-T04 rubric 要点进入判题 prompt
    # ---- 过程量表调用：固定 5 项 + 全部学员发言
    process_call = chat.calls[6]
    assert "指令清晰" in process_call["messages"][0]["content"]
    assert "任务拆解" in process_call["messages"][0]["content"]
    user_text = process_call["messages"][1]["content"]
    assert "帮我拆解任务" in user_text and "这个方案再改改" in user_text
    assert chat.calls[7]["model_role"] == "judge"  # 报告建议同链路

    # ---- SessionAnswer 落库：主观题 is_correct=None、submission 存档、seq 续接
    answers = _answers(sid)
    d3 = answers["D3-T04"]
    assert d3.is_correct is None and d3.score == 3 and d3.answer == "我的方案一\n我的方案二"
    d5 = answers["D5-T05"]
    assert d5.is_correct is None and d5.answer == ARTIFACT

    # ---- θ 回灌：对话题→所属维度、实操→D5，theta_after 与快照一致
    after = _snapshot(sid)
    exp_d3 = update(DimensionState.from_dict(before["D3"]), 4, 3 / 4)
    assert after["D3"]["n"] == before["D3"]["n"] + 1
    assert after["D3"]["theta"] == pytest.approx(exp_d3.theta)
    assert d3.theta_after == pytest.approx(exp_d3.theta)
    exp_d5 = update(DimensionState.from_dict(before["D5"]), 4, 3.4 / 4)  # 3×0.6+4×0.4=3.4
    assert after["D5"]["n"] == before["D5"]["n"] + 1
    assert after["D5"]["theta"] == pytest.approx(exp_d5.theta)
    assert _reviews(sid) == []  # 无降级/分差 → 不入复核队列

    # ---- 报告 answers 回显：开放题 score/rationale，实操双通道分项
    body = _report_body(client, auth_headers, sid)
    assert body["advice_source"] == "llm"  # provider 包装真实把 mock 结果带回报告
    d3_item = _open_items(body, "D3")[0]
    assert d3_item["score"] == 3 and d3_item["rationale"] == "判题理由3"
    assert d3_item["is_correct"] is None
    d4_item = _open_items(body, "D4")[0]
    assert d4_item["score"] == 2 and d4_item["rationale"] == "判题理由2"
    prac = _practical_item(body)
    assert prac["score"] == 3.4  # 过程 3.0×0.6 + 产物 4×0.4
    assert prac["process_score"] == 3.0 and prac["artifact_score"] == 4
    assert "判题理由4" in prac["rationale"]
    with SessionLocal() as db:
        assert db.get(AssessmentSession, sid).status == "finished"


# ---------- 未作答：不判分不回灌 θ ----------


def test_unanswered_questions_score_zero_without_theta_feed(monkeypatch, client, auth_headers, bank):
    sid, _ = _ready(client, auth_headers, bank, practical_prompts=None)  # 对话全跳过、实操无协作发言
    before = _snapshot(sid)
    chat = MockChat([_good(2), _good(2), _ADVICE])  # 仅产物判分 + 建议：无对话题调用、无过程调用
    monkeypatch.setattr("app.api.session_routes.chat_completion", chat)

    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert resp.status_code == 200
    assert len(chat.calls) == 3

    after = _snapshot(sid)
    for d in ("D3", "D4"):  # 未作答维度：θ 与题数不变
        assert after[d] == before[d]
    assert after["D5"]["n"] == before["D5"]["n"] + 1  # 实操已交产物 → 正常回灌

    answers = _answers(sid)
    assert answers["D3-T04"].score == 0 and answers["D4-T04"].score == 0
    body = _report_body(client, auth_headers, sid)
    for dim in ("D3", "D4"):
        item = _open_items(body, dim)[0]
        assert item["score"] == 0 and item["rationale"] == "学员未作答"
    prac = _practical_item(body)
    assert prac["score"] == 2.0  # 无协作发言：过程按产物折算 → 2×0.6+2×0.4
    assert prac["process_score"] == 2.0 and prac["artifact_score"] == 2
    assert "无学员发言" in prac["rationale"]
    assert _reviews(sid) == []  # 折算非降级，不入队


def test_practical_without_artifact_is_unanswered(monkeypatch, client, auth_headers, bank):
    """强制 ready（未交产物）：实操同样按未作答处理，不发起任何判题调用。"""
    sid, view = _seed_dialog_and_reach_practical(client, auth_headers, bank, {})
    with SessionLocal() as db:
        session = db.get(AssessmentSession, sid)
        session.stage = "ready"
        db.commit()
    chat = MockChat([_ADVICE])
    monkeypatch.setattr("app.api.session_routes.chat_completion", chat)

    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert resp.status_code == 200
    assert len(chat.calls) == 1  # 只有报告建议
    answers = _answers(sid)
    assert answers["D5-T05"].score == 0
    body = _report_body(client, auth_headers, sid)
    assert _practical_item(body)["rationale"] == "学员未作答"


# ---------- 降级与复核队列 ----------


def test_provider_unavailable_degrades_and_enqueues_review(monkeypatch, client, auth_headers, bank):
    d3_point = "给出检索增强/知识库等具体方案"  # D3-T04 rubric 要点原文：命中 1/3 → 降级分 1
    sid, _ = _ready(
        client,
        auth_headers,
        bank,
        dialog_answers={"D3-T04": [d3_point + "，并做人工核验"], "D4-T04": ["随便聊聊"]},
        practical_prompts=["帮我做计划"],
    )
    before = _snapshot(sid)

    class BrokenChat:
        def __init__(self):
            self.calls = 0

        def __call__(self, messages, **kwargs):
            self.calls += 1
            raise ProviderUnavailableError("缺少 DEEPSEEK_API_KEY，无法调用 LLM")

    broken = BrokenChat()
    monkeypatch.setattr("app.api.session_routes.chat_completion", broken)

    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert resp.status_code == 200
    # D3 3 次 + D4 3 次 + 产物 3 次 + 过程 1 次 + 建议 1 次 = 11（全部由 provider 包装承接）
    assert broken.calls == 11

    answers = _answers(sid)
    body = _report_body(client, auth_headers, sid)
    assert answers["D3-T04"].score == 1 and "降级" in _open_items(body, "D3")[0]["rationale"]
    assert answers["D4-T04"].score == 0
    after = _snapshot(sid)
    assert after["D3"]["n"] == before["D3"]["n"] + 1  # 降级分仍回灌 θ

    reviews = {(r.question_code): r for r in _reviews(sid)}
    assert set(reviews) == {"D3-T04", "D4-T04", "D5-T05"}  # 三题全降级入队
    assert reviews["D5-T05"].reason and "降级" in reviews["D5-T05"].reason

    assert body["advice_source"] == "template"  # provider 异常 → 建议走模板
    prac = _practical_item(body)
    assert prac["score"] == 0.0 and "降级" in prac["rationale"]  # 过程失败按产物分（降级 0）折算


def test_divergent_dialog_runs_enqueue_review_with_median(monkeypatch, client, auth_headers, bank):
    sid, _ = _ready(client, auth_headers, bank, dialog_answers={"D3-T04": ["我的回答"]})
    chat = MockChat([_good(1), _good(4), _good(3), _good(2), _good(2), _ADVICE])
    monkeypatch.setattr("app.api.session_routes.chat_completion", chat)

    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert resp.status_code == 200

    answers = _answers(sid)
    assert answers["D3-T04"].score == 3  # 三跑 1/4/3 取中位
    reviews = {r.question_code: r for r in _reviews(sid)}
    assert set(reviews) == {"D3-T04"}  # 只有分差过大的题入队
    assert "分差过大" in reviews["D3-T04"].reason
    assert reviews["D3-T04"].judge_raw["runs"] == [1, 4, 3]


def test_process_failure_falls_back_to_artifact_score(monkeypatch, client, auth_headers, bank):
    sid, _ = _ready(client, auth_headers, bank, practical_prompts=["帮我拆解", "再改一版"])
    before = _snapshot(sid)
    chat = MockChat([_good(4), _good(4), "这不是JSON", _ADVICE])
    monkeypatch.setattr("app.api.session_routes.chat_completion", chat)

    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert resp.status_code == 200

    body = _report_body(client, auth_headers, sid)
    prac = _practical_item(body)
    assert prac["process_score"] == 4.0  # 量表输出不可解析 → 过程分按产物分折算
    assert prac["artifact_score"] == 4
    assert prac["score"] == 4.0
    assert "折算" in prac["rationale"] and "降级" in prac["rationale"]
    reviews = {r.question_code: r for r in _reviews(sid)}
    assert set(reviews) == {"D5-T05"}
    assert "过程" in reviews["D5-T05"].reason
    after = _snapshot(sid)
    answers = _answers(sid)
    assert after["D5"]["n"] == before["D5"]["n"] + 1
    assert answers["D5-T05"].theta_after == pytest.approx(
        update(DimensionState.from_dict(before["D5"]), 4, 1.0).theta
    )
