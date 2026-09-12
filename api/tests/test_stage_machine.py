"""四阶段状态机与主观题抽题（spec §6.1）。

客观六维全部完成（full 模式）→ 自动翻转 dialog 并按 θ 升序第 3/4 位抽对话题；
D5 实操冲突顺延；quick 模式止于 objective（M1 行为）；finish 在 practical 提交前拒绝 full 会话。
"""

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.api.session_routes import _session_view, _stage_plan
from app.db import SessionLocal
from app.engine.adaptive import DIMENSIONS, DimensionState
from app.models import AssessmentSession, Question, SessionMessage


def _snapshot(thetas: dict[str, float]) -> dict:
    """六维客观已答满（n=6 触发题数停止）且 θ 各异的快照。"""
    return {d: DimensionState(theta=thetas[d], n=6).to_dict() for d in DIMENSIONS}


def _plan_session(thetas: dict[str, float]) -> AssessmentSession:
    """未持久化的会话（_stage_plan 只读快照；session_id 为空时已作答集合为空）。"""
    return AssessmentSession(user_id=1, mode="full", theta_snapshot=_snapshot(thetas))


def _run_objective(client: TestClient, headers: dict, bank: dict, mode: str) -> dict:
    """答完客观阶段（全对）：stage 翻转或无题可出即止，返回最后一个视图。"""
    view = client.post("/api/sessions", json={"mode": mode}, headers=headers).json()
    guard = 0
    while view["question"] is not None and view.get("stage", "objective") == "objective":
        guard += 1
        assert guard < 40, "客观答题循环未收敛"
        q = view["question"]
        resp = client.post(
            f"/api/sessions/{view['session_id']}/answer",
            json={"question_id": q["id"], "answer": bank[q["code"]]["answer"], "time_spent": 30},
            headers=headers,
        )
        assert resp.status_code == 200
        view = resp.json()
    return view


# ---------- _stage_plan 抽题规则 ----------


def test_stage_plan_none_when_objective_incomplete():
    snapshot = {d: DimensionState().to_dict() for d in DIMENSIONS}  # 各维 n=0
    with SessionLocal() as db:
        session = AssessmentSession(user_id=1, mode="full", theta_snapshot=snapshot)
        assert _stage_plan(db, session) is None


def test_stage_plan_picks_3rd_and_4th_dimensions_by_theta():
    """六维 θ 升序 D1<D2<…<D6 → 对话抽第 3/4 位（D3、D4）；D5 未冲突 → 首道 D5 实操。"""
    thetas = {"D1": 1.5, "D2": 2.0, "D3": 2.5, "D4": 3.0, "D5": 3.5, "D6": 4.0}
    with SessionLocal() as db:
        plan = _stage_plan(db, _plan_session(thetas))
        assert [q.code for q in plan["dialog"]] == ["D3-T04", "D4-T04"]
        assert plan["practical"].code == "D5-T05"


def test_stage_plan_d5_conflict_takes_next_d5_practical():
    """θ 升序第 3/4 位为 D5、D6 → D5 冲突，实操顺延取下一道 D5（D5-T06）。"""
    thetas = {"D1": 1.5, "D2": 2.0, "D5": 2.5, "D6": 3.0, "D3": 4.5, "D4": 5.0}
    with SessionLocal() as db:
        plan = _stage_plan(db, _plan_session(thetas))
        assert [q.code for q in plan["dialog"]] == ["D5-T04", "D6-T04"]
        assert plan["practical"].code == "D5-T06"


def test_stage_plan_d5_conflict_without_next_falls_back_to_d2():
    """D5 冲突且无下一道 D5 实操时回落 D2 实操（D2-T05）。"""
    thetas = {"D1": 1.5, "D2": 2.0, "D5": 2.5, "D6": 3.0, "D3": 4.5, "D4": 5.0}
    with SessionLocal() as db:
        try:
            db.execute(update(Question).where(Question.code == "D5-T06").values(status="draft"))
            db.commit()
            plan = _stage_plan(db, _plan_session(thetas))
            assert [q.code for q in plan["dialog"]] == ["D5-T04", "D6-T04"]
            assert plan["practical"].code == "D2-T05"
        finally:  # 还原共享测试库
            db.execute(update(Question).where(Question.code == "D5-T06").values(status="published"))
            db.commit()


# ---------- 端点级阶段流转 ----------


def test_new_session_starts_in_objective_stage(client, auth_headers):
    resp = client.post("/api/sessions", json={"mode": "full"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["stage"] == "objective"


def test_full_session_flips_to_dialog_after_objective(client, auth_headers, bank):
    view = _run_objective(client, auth_headers, bank, mode="full")
    assert view["stage"] == "dialog"
    assert all(d["done"] for d in view["progress"].values())
    q = view["question"]
    # 全对各维 θ 并列 → 稳定排序按 D1..D6，第 3 位为 D3（规则断言见 _stage_plan 单测）
    assert q["type"] == "open"
    assert q["code"] == "D3-T04"
    assert q["dialog_turns_taken"] == 0
    assert view["next_dimension"] == q["dimension"]
    with SessionLocal() as db:  # 翻转必须持久化（finish 门禁依赖）
        session = db.get(AssessmentSession, view["session_id"])
        assert session.stage == "dialog"


def test_full_finish_rejected_until_ready(client, auth_headers, bank):
    view = _run_objective(client, auth_headers, bank, mode="full")
    resp = client.post(f"/api/sessions/{view['session_id']}/finish", headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "请先完成对话式测评与实操任务"

    with SessionLocal() as db:  # 显式构造实操提交后的 ready 态（T3/T4/T5 打通真实链路）
        session = db.get(AssessmentSession, view["session_id"])
        session.stage = "ready"
        db.commit()
    resp = client.post(f"/api/sessions/{view['session_id']}/finish", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["report_id"]


def test_quick_mode_stays_objective_and_finishes(client, auth_headers, bank):
    view = _run_objective(client, auth_headers, bank, mode="quick")
    assert view["stage"] == "objective"  # quick 止于客观（M1 行为）
    assert view["question"] is None
    resp = client.post(f"/api/sessions/{view['session_id']}/finish", headers=auth_headers)
    assert resp.status_code == 200


def test_practical_stage_view_returns_practical_summary(client, auth_headers, bank):
    view = _run_objective(client, auth_headers, bank, mode="full")
    with SessionLocal() as db:
        session = db.get(AssessmentSession, view["session_id"])
        session.stage = "practical"
        db.commit()
        result = _session_view(db, session)
    assert result["stage"] == "practical"
    assert result["question"]["type"] == "practical"
    assert result["question"]["code"] == "D5-T05"  # 对话抽中 D3/D4，D5 未冲突
    assert "answer" not in result["question"] and "rubric" not in result["question"]


def test_dialog_view_counts_learner_turns(client, auth_headers, bank):
    view = _run_objective(client, auth_headers, bank, mode="full")
    q = view["question"]
    with SessionLocal() as db:
        session = db.get(AssessmentSession, view["session_id"])
        for seq, role in enumerate(["examiner", "learner", "learner"], start=1):
            db.add(
                SessionMessage(
                    session_id=session.id,
                    question_id=q["id"],
                    channel="dialog",
                    role=role,
                    content=f"消息 {seq}",
                    seq=seq,
                )
            )
        db.commit()
        result = _session_view(db, session)
    assert result["question"]["code"] == q["code"]
    assert result["question"]["dialog_turns_taken"] == 2  # 以学员发言计轮次


# ---------- 旧库迁移 ----------


def test_migrate_session_columns_adds_stage(tmp_path, monkeypatch):
    """M2b 前的旧库（无 stage 列）经 init_db 的幂等 ALTER 补列，存量会话默认 objective。
    全新测试库由 create_all 建表，此 ALTER 路径只有本测试覆盖。"""
    import sqlite3

    from sqlalchemy import create_engine, inspect, text

    from app import db as db_module

    db_file = tmp_path / "old.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE assessment_sessions (id INTEGER PRIMARY KEY, user_id INTEGER, mode VARCHAR(8),"
        " status VARCHAR(16), theta_snapshot JSON, started_at DATETIME, finished_at DATETIME)"
    )
    conn.execute("INSERT INTO assessment_sessions (id, user_id, mode, status) VALUES (1, 1, 'full', 'in_progress')")
    conn.commit()
    conn.close()

    test_engine = create_engine(f"sqlite:///{db_file}")
    monkeypatch.setattr(db_module, "engine", test_engine)
    try:
        db_module._migrate_session_columns()
        db_module._migrate_session_columns()  # 幂等：重复执行不报错
        cols = {c["name"] for c in inspect(test_engine).get_columns("assessment_sessions")}
        assert "stage" in cols
        with test_engine.connect() as c:
            assert c.execute(text("SELECT stage FROM assessment_sessions WHERE id=1")).scalar() == "objective"
    finally:
        test_engine.dispose()
