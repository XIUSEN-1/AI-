"""finish 异步并行判题与进度轮询（M2b-hotfix Task 2）。

契约：finish 校验通过 → 置 judging（judging_total=主观题数）并立即 202 {session_id, judging_total}；
后台线程 ThreadPoolExecutor(4) 并行判题（只做 LLM 调用与内存收集，判完新开连接统一写库）；
GET /api/sessions/{id}/status → {status, stage, judging_step, judging_total, report_id?}。
并行下调用顺序不确定：mock 一律按 prompt 内容路由响应，不按序号喂。
"""

import threading
import time

from sqlalchemy import select

from app.db import SessionLocal
from app.llm.mock import MockChat
from app.models import AssessmentSession, Report, SessionAnswer
from conftest import finish_and_wait
from test_finish_judging import _ADVICE, _good, _process_json, _ready
from test_stage_machine import _run_objective

DELAY = 0.5  # 每次 LLM 调用的人为延迟


class RoutingChat:
    """按 prompt 内容路由响应的慢速 mock：判题官 prompt 含题干（D3/D4 情境题、周计划实操）、
    过程量表 system 含「指令清晰」、建议 system 含「学习顾问」。每次调用 sleep delay。"""

    def __init__(self, delay: float = DELAY, scores: dict[str, int] | None = None):
        self.delay = delay
        self.scores = scores or {}
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, messages, **kwargs):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        system = messages[0]["content"]
        if "指令清晰" in system:
            return _process_json()
        if "学习顾问" in system:
            return _ADVICE
        text = "\n".join(m["content"] for m in messages)
        for marker, score in self.scores.items():
            if marker in text:
                return _good(score)
        return _good(3)


def _status(client, headers, sid: int) -> dict:
    resp = client.get(f"/api/sessions/{sid}/status", headers=headers)
    assert resp.status_code == 200
    return resp.json()


def _poll(client, headers, sid: int, predicate, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _status(client, headers, sid)
        if predicate(last):
            return last
        time.sleep(0.02)
    raise AssertionError(f"status 轮询超时，最后状态: {last}")


# ---------- 202 立即返回与并行墙钟 ----------


def test_finish_returns_202_immediately_and_parallel_beats_serial(monkeypatch, client, auth_headers, bank):
    sid, _ = _ready(
        client,
        auth_headers,
        bank,
        dialog_answers={"D3-T04": ["方案一"], "D4-T04": ["回答一"]},
        practical_prompts=["帮我拆解"],
    )
    scores = {"情境题（D3）": 3, "情境题（D4）": 2, "周计划": 4}
    chat = RoutingChat(scores=scores)
    monkeypatch.setattr("app.api.session_routes.chat_completion", chat)

    start = time.perf_counter()
    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    finish_latency = time.perf_counter() - start
    assert resp.status_code == 202
    assert resp.json() == {"session_id": sid, "judging_total": 3}
    assert finish_latency < 1.0  # 立即受理：判题全部转后台

    st = _poll(client, auth_headers, sid, lambda s: s["status"] == "finished")
    total = time.perf_counter() - start
    assert st["judging_step"] == 3 and st["report_id"]
    # 并行墙钟：串行 8 次调用 × 0.5s = 4s+；并行关键路径 = 实操 3 次调用 1.5s + 建议 0.5s ≈ 2s
    assert total < 3.2, f"并行判题墙钟 {total:.2f}s 未显著低于串行 4s"
    assert chat.calls == 8

    with SessionLocal() as db:  # 并行不丢不串：各题判分正确落库
        answers = {
            a.question_code: a
            for a in db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == sid))
        }
    assert answers["D3-T04"].score == 3
    assert answers["D4-T04"].score == 2
    assert answers["D5-T05"].score == 3.4  # 过程 3.0×0.6 + 产物 4×0.4


# ---------- 进度轮询：step 递增至 finished ----------


def test_status_polling_reports_step_progress(monkeypatch, client, auth_headers, bank):
    sid, _ = _ready(
        client,
        auth_headers,
        bank,
        dialog_answers={"D3-T04": ["方案一"], "D4-T04": ["回答一"]},
        practical_prompts=["帮我拆解"],
    )
    release = threading.Event()
    scores = {"情境题（D3）": 3, "情境题（D4）": 2, "周计划": 4}

    class GatedChat(RoutingChat):
        def __call__(self, messages, **kwargs):
            system = messages[0]["content"]
            text = "\n".join(m["content"] for m in messages)
            if "指令清晰" not in system and "学习顾问" not in system and "周计划" in text:
                release.wait(timeout=15)  # 挡住实操判题：轮询才能稳定观察到中间进度
            return super().__call__(messages, **kwargs)

    monkeypatch.setattr("app.api.session_routes.chat_completion", GatedChat(scores=scores))

    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert resp.status_code == 202

    st = _poll(client, auth_headers, sid, lambda s: s["status"] == "judging" and s["judging_step"] == 2)
    assert st["judging_total"] == 3  # 两道对话题已判完，实操判题中

    release.set()
    st = _poll(client, auth_headers, sid, lambda s: s["status"] == "finished")
    assert st["judging_step"] == 3 and st["report_id"]


# ---------- judging 中重复 finish 409 ----------


def test_duplicate_finish_during_judging_returns_409(monkeypatch, client, auth_headers, bank):
    sid, _ = _ready(client, auth_headers, bank, practical_prompts=["帮我拆解"])
    release = threading.Event()

    class BlockingChat(RoutingChat):
        def __call__(self, messages, **kwargs):
            release.wait(timeout=15)
            return super().__call__(messages, **kwargs)

    monkeypatch.setattr("app.api.session_routes.chat_completion", BlockingChat())

    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert resp.status_code == 202
    again = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert again.status_code == 409
    assert again.json()["detail"] == "报告生成中，请勿重复提交"
    assert _status(client, auth_headers, sid)["status"] == "judging"

    release.set()
    st = _poll(client, auth_headers, sid, lambda s: s["status"] == "finished")
    done = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)  # 完成后幂等
    assert done.status_code == 200 and done.json()["report_id"] == st["report_id"]


# ---------- 后台线程致命异常：回滚可重试 ----------


def test_async_fatal_error_rolls_back_for_retry(monkeypatch, client, auth_headers, bank):
    sid, _ = _ready(client, auth_headers, bank)

    def boom(messages, **kwargs):
        raise RuntimeError("上游 LLM 超时")  # 非 ProviderUnavailableError：不走降级链

    monkeypatch.setattr("app.api.session_routes.chat_completion", boom)
    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert resp.status_code == 202  # 受理即返回，异常发生在后台
    st = _poll(client, auth_headers, sid, lambda s: s["status"] == "in_progress")
    assert st["judging_step"] == 0  # step 归零：可重试
    with SessionLocal() as db:
        assert db.scalar(select(Report).where(Report.session_id == sid)) is None

    chat = MockChat([_good(2), _good(2), _ADVICE])  # 重试：产物双跑 + 建议
    monkeypatch.setattr("app.api.session_routes.chat_completion", chat)
    finish_and_wait(client, auth_headers, sid)
    with SessionLocal() as db:
        assert db.get(AssessmentSession, sid).status == "finished"


# ---------- status 端点契约 ----------


def test_status_endpoint_shape_and_ownership(client, auth_headers, bank):
    view = client.post("/api/sessions", json={"mode": "quick"}, headers=auth_headers).json()
    sid = view["session_id"]
    body = _status(client, auth_headers, sid)
    assert body["status"] == "in_progress" and body["stage"] == "objective"
    assert body["judging_step"] == 0 and body["judging_total"] == 0
    assert "report_id" not in body  # 未完成不带 report_id

    other = client.post("/api/auth/student", json={"name": "路人学员", "student_no": "ASY999"}).json()
    resp = client.get(f"/api/sessions/{sid}/status", headers={"Authorization": f"Bearer {other['token']}"})
    assert resp.status_code == 404


# ---------- 服务重启：陈旧 judging 清扫 ----------


def test_lifespan_resets_stale_judging(client, auth_headers):
    """判题线程不跨进程存活：服务重启（lifespan 启动）把崩溃遗留的卡死 judging 复位为
    in_progress 可重试；正常 in_progress 会话不受影响。"""
    from fastapi.testclient import TestClient

    from app.main import app

    stale = client.post("/api/sessions", json={"mode": "quick"}, headers=auth_headers).json()
    fresh = client.post("/api/sessions", json={"mode": "quick"}, headers=auth_headers).json()
    with SessionLocal() as db:  # 模拟进程崩溃遗留的卡死态
        session = db.get(AssessmentSession, stale["session_id"])
        session.status = "judging"
        session.judging_step = 2
        session.judging_total = 3
        db.commit()

    with TestClient(app) as running:  # 上下文管理器触发 lifespan startup
        assert running.get("/api/health").status_code == 200

    with SessionLocal() as db:
        swept = db.get(AssessmentSession, stale["session_id"])
        assert swept.status == "in_progress" and swept.judging_step == 0
        untouched = db.get(AssessmentSession, fresh["session_id"])
        assert untouched.status == "in_progress" and untouched.judging_step == 0


# ---------- 跳过路径走异步管线回归 ----------


def test_skipped_questions_flow_through_async_judging(monkeypatch, client, auth_headers, bank):
    """全跳会话经真实异步管线：跳过题计入进度（x/y）但零判题调用，报告如实标注。"""
    view = _run_objective(client, auth_headers, bank, mode="full")
    sid = view["session_id"]
    for _ in range(2):
        assert client.post(f"/api/sessions/{sid}/dialog/skip", headers=auth_headers).status_code == 200
    assert client.post(f"/api/sessions/{sid}/practical/skip", headers=auth_headers).status_code == 200

    chat = MockChat([_ADVICE])
    monkeypatch.setattr("app.api.session_routes.chat_completion", chat)
    resp = client.post(f"/api/sessions/{sid}/finish", headers=auth_headers)
    assert resp.status_code == 202 and resp.json() == {"session_id": sid, "judging_total": 3}

    st = _poll(client, auth_headers, sid, lambda s: s["status"] == "finished")
    assert st["judging_step"] == 3 and st["report_id"]
    assert len(chat.calls) == 1  # 跳过题零判题调用，仅报告建议
    report = client.get(f"/api/reports/{st['report_id']}", headers=auth_headers).json()
    skipped = [a for a in report["answers"] if a["type"] in ("open", "practical")]
    assert len(skipped) == 3 and all(a["rationale"] == "学员跳过" for a in skipped)
