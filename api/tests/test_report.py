import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES
from app.llm.mock import MockChat
from app.llm.provider import ProviderUnavailableError
from app.models import AssessmentSession
from app.report import generate
from app.report.generate import build_report
from conftest import finish_and_wait
from test_teacher import make_teacher, register_student
from tests.test_session_flow import _run_full_flow


def _finish_a_session(client: TestClient, headers: dict, bank: dict) -> int:
    view = _run_full_flow(client, headers, bank, correct=True)
    return finish_and_wait(client, headers, view["session_id"])["report_id"]


def test_finish_returns_report_with_radar(client, auth_headers, bank):
    report_id = _finish_a_session(client, auth_headers, bank)
    resp = client.get(f"/api/reports/{report_id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["dimensions"]) == 6
    assert len(body["radar"]) == 6
    assert all(0 <= r["value"] <= 100 for r in body["radar"])
    assert 1 <= body["total_level"] <= 5
    assert body["advice"]
    assert body["created_at"].endswith("Z")
    assert all(d["answered"] >= 2 for d in body["dimensions"])


def test_finish_is_idempotent(client, auth_headers, bank):
    view = _run_full_flow(client, auth_headers, bank, correct=True)
    first = finish_and_wait(client, auth_headers, view["session_id"])
    second = client.post(f"/api/sessions/{view['session_id']}/finish", headers=auth_headers)
    assert second.status_code == 200  # 已完成会话幂等早返回（不再走异步判题）
    assert first["report_id"] == second.json()["report_id"]


def test_mine_lists_reports(client, auth_headers, bank):
    report_id = _finish_a_session(client, auth_headers, bank)
    resp = client.get("/api/reports/mine", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    assert "total_level_name" in resp.json()[0]
    assert resp.json()[0]["created_at"].endswith("Z")
    # 成长趋势字段：avg_percent = 六维 percent 均值（round），与报告详情口径一致
    detail = client.get(f"/api/reports/{report_id}", headers=auth_headers).json()
    expected = round(sum(d["percent"] for d in detail["dimensions"]) / len(detail["dimensions"]))
    assert resp.json()[0]["avg_percent"] == expected


def test_report_forbidden_for_others(client, auth_headers, bank):
    report_id = _finish_a_session(client, auth_headers, bank)
    other = client.post("/api/auth/student", json={"name": "他人", "student_no": "OTHER01"}).json()
    resp = client.get(f"/api/reports/{report_id}", headers={"Authorization": f"Bearer {other['token']}"})
    assert resp.status_code == 403


# ---------- 教师读报告限权（M2c fix round 1）：仅本班、且剥离逐题明细 ----------

def _class_scenario(client: TestClient, bank: dict):
    """教师建班→学员入班→完成一次测评；返回 (teacher 头, student 头, report_id)。"""
    teacher = make_teacher(client, uuid.uuid4().hex[:6])
    klass = client.post("/api/teacher/classes", json={"name": "报告班"}, headers=teacher).json()
    student = register_student(client, "R" + uuid.uuid4().hex[:5], klass["invite_code"])
    report_id = _finish_a_session(client, student, bank)
    return teacher, student, report_id


def test_teacher_reads_own_class_report_without_answers(client, bank):
    """本班 teacher 可读学员报告，但仅维度聚合层级：剥离 answers 逐题明细；本人仍完整。"""
    teacher, student, report_id = _class_scenario(client, bank)
    resp = client.get(f"/api/reports/{report_id}", headers=teacher)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "answers" not in body, "教师版报告不得含单个作答明细"
    assert len(body["dimensions"]) == 6 and body["advice"]  # 聚合层级完整保留
    assert "answers" in client.get(f"/api/reports/{report_id}", headers=student).json()


def test_report_forbidden_for_other_class_teacher(client, bank):
    teacher, _, report_id = _class_scenario(client, bank)
    other = make_teacher(client, uuid.uuid4().hex[:6])
    assert client.get(f"/api/reports/{report_id}", headers=other).status_code == 403


def test_report_forbidden_for_teacher_when_student_has_no_class(client, bank):
    """学员无班级（自由测评）时 Klass 关联缺失，teacher 不可读。"""
    free = {"Authorization": f"Bearer {client.post('/api/auth/student', json={'name': '自由学员', 'student_no': f'RF-{uuid.uuid4().hex[:6]}'}).json()['token']}"}
    report_id = _finish_a_session(client, free, bank)
    teacher, _, _ = _class_scenario(client, bank)
    assert client.get(f"/api/reports/{report_id}", headers=teacher).status_code == 403


def test_report_full_for_admin(client, bank):
    _, _, report_id = _class_scenario(client, bank)
    admin = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()
    resp = client.get(f"/api/reports/{report_id}", headers={"Authorization": f"Bearer {admin['token']}"})
    assert resp.status_code == 200
    assert "answers" in resp.json()  # admin 全权：完整报告


def _dims() -> list[dict]:
    """构造 build_report 内产的六维明细（D1/D6 弱，其余强）。"""
    weak = {"D1", "D6"}
    return [
        {
            "dimension": d,
            "name": DIMENSION_NAMES[d],
            "theta": 2.0 if d in weak else 4.0,
            "level": 1 if d in weak else 4,
            "level_name": "入门" if d in weak else "熟练",
            "answered": 3,
            "correct": 1 if d in weak else 3,
            "percent": 25 if d in weak else 75,
        }
        for d in DIMENSIONS
    ]


def test_llm_advice_success_marks_llm_source():
    chat = MockChat([json.dumps({"advice": ["建议一：D1 归因+行动", "建议二：D6 归因+行动"]}, ensure_ascii=False)])
    advice, source, detail = generate.generate_llm_advice(_dims(), ["D1", "D6"], chat)
    assert source == "llm"
    assert advice == ["建议一：D1 归因+行动", "建议二：D6 归因+行动"]
    assert detail == [
        {"text": "建议一：D1 归因+行动", "source_type": "llm"},
        {"text": "建议二：D6 归因+行动", "source_type": "llm"},
    ]
    # prompt 必须带六维分数与短板维度明细，且走 chat 角色（flash 非思考型提速）、低温 JSON 模式
    assert len(chat.calls) == 1
    assert chat.calls[0]["model_role"] == "chat"
    assert chat.calls[0]["temperature"] == 0.0
    assert chat.calls[0]["json_mode"] is True
    text = "\n".join(m["content"] for m in chat.calls[0]["messages"])
    for d in DIMENSIONS:
        assert DIMENSION_NAMES[d] in text
    assert "2.0" in text  # 短板维度分数进入 prompt


def test_llm_advice_caps_at_three_items():
    chat = MockChat([json.dumps({"advice": ["一", "二", "三", "四", "五"]}, ensure_ascii=False)])
    advice, source, detail = generate.generate_llm_advice(_dims(), ["D1"], chat)
    assert source == "llm"
    assert advice == ["一", "二", "三"]
    assert len(detail) == 3


def _assert_cell_fallback(dims, gaps, advice, source, detail):
    """无 LLM/失败回退断言：格子直渲染 + source=cell + 明细对齐。"""
    assert source == "cell"
    assert [e["text"] for e in detail] == advice
    assert [e["dimension"] for e in detail] == sorted(gaps)
    for e, d in zip(detail, sorted(gaps)):
        assert e["source_type"] == "cell"
        assert e["cell"] == generate._advice_cell(d, 1)  # _dims 弱维 level=1
        assert e["cell"]["summary"][:15] in e["text"]


def test_llm_advice_falls_back_on_provider_error():
    def boom(messages, **kwargs):
        raise ProviderUnavailableError("缺少 DEEPSEEK_API_KEY，无法调用 LLM")

    dims, gaps = _dims(), ["D1", "D6"]
    advice, source, detail = generate.generate_llm_advice(dims, gaps, boom)
    _assert_cell_fallback(dims, gaps, advice, source, detail)


def test_llm_advice_falls_back_on_bad_payload():
    bad_outputs = ["这不是JSON", json.dumps({"advice": []}), json.dumps({"advice": "一条"}), json.dumps([1, 2]), ""]
    for raw in bad_outputs:
        advice, source, detail = generate.generate_llm_advice(_dims(), ["D1", "D6"], MockChat([raw]))
        _assert_cell_fallback(_dims(), ["D1", "D6"], advice, source, detail)


def test_llm_advice_none_chat_fn_renders_cells():
    advice, source, detail = generate.generate_llm_advice(_dims(), ["D1", "D6"], None)
    _assert_cell_fallback(_dims(), ["D1", "D6"], advice, source, detail)


def test_llm_advice_prompt_shape_bug_surfaces():
    """dimensions 缺 prompt 所需键（theta/correct/answered）时必须以 KeyError 显形，
    不得被回退边界吞成模板——模板可用的键与 prompt 所需的键不完全重叠。"""
    bad = [{"dimension": "D1", "name": "概念认知", "level": 2, "level_name": "入门"}]
    chat = MockChat([])  # 预设为空：任何意外调用都会在此炸响
    with pytest.raises(KeyError):
        generate.generate_llm_advice(bad, ["D1"], chat)
    assert chat.calls == []  # 形状 bug 在构造 prompt 时即暴露，未发起任何调用


def test_finish_report_answers_shape_and_order(client, auth_headers, bank):
    report_id = _finish_a_session(client, auth_headers, bank)
    body = client.get(f"/api/reports/{report_id}", headers=auth_headers).json()
    # 测试环境无 DEEPSEEK_API_KEY → provider 抛错被吞，报告回退分级建议库直渲染
    assert body["advice_source"] == "cell"
    assert len(body["advice_detail"]) == len(body["advice"])
    assert all(e["source_type"] == "cell" for e in body["advice_detail"])
    assert all(e["cell"]["resources"] for e in body["advice_detail"])
    answers = body["answers"]
    assert len(answers) == sum(d["answered"] for d in body["dimensions"])
    keys = {"seq", "dimension", "type", "stem_head", "is_correct", "score", "explanation", "theta_after"}
    assert all(keys <= set(a) for a in answers)
    assert all(a["stem_head"] and len(a["stem_head"]) <= 60 for a in answers)
    assert all(a["is_correct"] in (True, False) for a in answers)  # 客观题回显对错
    assert all(a["explanation"] for a in answers)  # fixture 客观题全部带解析
    assert all(a["type"] in ("single", "multi", "judge") for a in answers)
    order = [(a["dimension"], a["seq"]) for a in answers]
    assert order == sorted(order)  # 先按维度再按作答序号


def test_build_report_with_mock_llm_marks_llm(client, auth_headers, bank):
    view = _run_full_flow(client, auth_headers, bank, correct=True)
    with SessionLocal() as db:
        session = db.get(AssessmentSession, view["session_id"])
        chat = MockChat([json.dumps({"advice": ["LLM 给出的归因与行动"]}, ensure_ascii=False)])
        report = build_report(db, session, chat_fn=chat)
        assert report.advice_source == "llm"
        assert report.advice == ["LLM 给出的归因与行动"]
        assert report.advice_detail == [{"text": "LLM 给出的归因与行动", "source_type": "llm"}]
        # answers 快照与维度作答数一致，且维度有序
        assert len(report.answers) == sum(d["answered"] for d in report.dimensions)
        dims_in_order = [a["dimension"] for a in report.answers]
        assert dims_in_order == sorted(dims_in_order)
        assert all(a["is_correct"] is True for a in report.answers)


def test_finish_endpoint_wires_provider_chat_fn(client, auth_headers, bank, monkeypatch):
    """finish 端点的 provider 包装必须真正把 LLM 结果带回报告——
    防 kwargs 透传错误（如重复传 model_role）被回退边界吞掉、静默永远走模板。"""
    from app.api import session_routes

    def fake_chat_completion(messages, *, model_role, temperature=0.0, json_mode=False, timeout=60):
        assert model_role == "chat"  # 报告建议走 flash（chat 角色），判题主链路仍为 judge
        assert json_mode is True
        return json.dumps({"advice": ["端点级 LLM 建议"]}, ensure_ascii=False)

    monkeypatch.setattr(session_routes, "chat_completion", fake_chat_completion)
    view = _run_full_flow(client, auth_headers, bank, correct=True)
    report_id = finish_and_wait(client, auth_headers, view["session_id"])["report_id"]
    body = client.get(f"/api/reports/{report_id}", headers=auth_headers).json()
    assert body["advice_source"] == "llm"
    assert body["advice"] == ["端点级 LLM 建议"]
