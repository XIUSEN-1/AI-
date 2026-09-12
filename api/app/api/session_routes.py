from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.auth import current_user
from app.engine import adaptive
from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES, DimensionState
from app.engine.grading import grade_objective, result_from_correct
from app.llm.provider import chat_completion
from app.models import AssessmentSession, Question, Report, SessionAnswer, SessionMessage, utcnow
from app.report.generate import build_report

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

OBJECTIVE_TYPES = ("single", "multi", "judge")

# 对话题结束标记（dialog/finish-question 落库的考官结束语，同时作为切题依据）
DIALOG_CLOSING = "本题作答结束。"


class StartIn(BaseModel):
    mode: str = "full"


class AnswerIn(BaseModel):
    question_id: int
    answer: object
    time_spent: int = Field(ge=0, default=0)


def _states(snapshot: dict) -> dict[str, DimensionState]:
    return {d: DimensionState.from_dict(snapshot.get(d, {})) for d in DIMENSIONS}


def _snapshot(states: dict[str, DimensionState]) -> dict:
    return {d: s.to_dict() for d, s in states.items()}


def _question_out(q: Question) -> dict:
    return {
        "id": q.id,
        "code": q.code,
        "dimension": q.dimension,
        "dimension_name": DIMENSION_NAMES[q.dimension],
        "type": q.type,
        "difficulty": q.difficulty,
        "stem": q.stem,
        "options": q.options,
        "est_seconds": q.est_seconds,
        "tags": q.tags,
    }


def _pick_question(db: OrmSession, dimension: str, state: DimensionState, asked_codes: set[str]) -> Question | None:
    pool = db.scalars(
        select(Question).where(
            Question.dimension == dimension,
            Question.type.in_(OBJECTIVE_TYPES),
            Question.status == "published",
        )
    ).all()
    dicts = [{"code": q.code, "difficulty": q.difficulty} for q in pool]
    chosen = adaptive.select_next_question(dicts, asked_codes, adaptive.next_difficulty(state))
    if chosen is None:
        return None
    code = chosen["code"]
    return next(q for q in pool if q.code == code)


def _difficulty_ceilings(db: OrmSession) -> dict[str, int | None]:
    """每维度已发布客观题的最大难度：作为连对停止的难度触顶条件。"""
    rows = db.execute(
        select(Question.dimension, func.max(Question.difficulty)).where(
            Question.type.in_(OBJECTIVE_TYPES),
            Question.status == "published",
        ).group_by(Question.dimension)
    ).all()
    return {dimension: ceiling for dimension, ceiling in rows}


def _done_dimensions(states: dict[str, DimensionState], ceilings: dict[str, int | None]) -> set[str]:
    return {
        d for d in DIMENSIONS
        if states[d].n > 0 and adaptive.should_stop(states[d], ceilings.get(d))
    }


def _subjective_pool(
    db: OrmSession, dimension: str, qtype: str, asked_codes: set[str]
) -> list[Question]:
    """维度内未作答的已发布主观题，难度降序、同难度按 id 升序（确定性抽题）。"""
    return db.scalars(
        select(Question).where(
            Question.dimension == dimension,
            Question.type == qtype,
            Question.status == "published",
            Question.code.not_in(asked_codes),
        ).order_by(Question.difficulty.desc(), Question.id)
    ).all()


def _stage_plan(db: OrmSession, session: AssessmentSession) -> dict | None:
    """主观阶段抽题（spec §6.1）。客观六维未全部完成 → None。

    对话：六维按 θ 升序取第 3、4 位维度（θ 并列时按 D1..D6 稳定序）各 1 道未作答 open 题；
    实操：固定 D5 practical；D5 为对话抽中维度时顺延下一道 D5 practical，仍无则 D2 practical。
    """
    states = _states(session.theta_snapshot)
    ceilings = _difficulty_ceilings(db)
    if _done_dimensions(states, ceilings) != set(DIMENSIONS):
        return None
    asked = set(
        db.scalars(
            select(SessionAnswer.question_code).where(SessionAnswer.session_id == session.id)
        )
    )
    ordered = sorted(DIMENSIONS, key=lambda d: states[d].theta)
    dialog_dims = ordered[2:4]
    dialog = []
    for d in dialog_dims:
        pool = _subjective_pool(db, d, "open", asked)
        if pool:
            dialog.append(pool[0])
    d5_pool = _subjective_pool(db, "D5", "practical", asked)
    practical_candidates = d5_pool[1:] if "D5" in dialog_dims else d5_pool  # 冲突顺延下一道
    if not practical_candidates:
        practical_candidates = _subjective_pool(db, "D2", "practical", asked)[:1]  # 仍无则 D2
    return {"dialog": dialog, "practical": practical_candidates[0] if practical_candidates else None}


def _dialog_turns_taken(db: OrmSession, session_id: int, question_id: int) -> int:
    """当前对话题已进行的轮次：以学员发言条数计（每轮 = 学员发言 + 考官回复）。"""
    return len(
        db.scalars(
            select(SessionMessage.id).where(
                SessionMessage.session_id == session_id,
                SessionMessage.question_id == question_id,
                SessionMessage.channel == "dialog",
                SessionMessage.role == "learner",
            )
        ).all()
    )


def _dialog_closed(db: OrmSession, session_id: int, question: Question) -> bool:
    """对话题是否已结束：学员发言满 3 轮，或考官已落结束标记（学员提前完成/跳过）。"""
    if _dialog_turns_taken(db, session_id, question.id) >= 3:
        return True
    return (
        db.scalar(
            select(SessionMessage.id).where(
                SessionMessage.session_id == session_id,
                SessionMessage.question_id == question.id,
                SessionMessage.channel == "dialog",
                SessionMessage.role == "examiner",
                SessionMessage.content == DIALOG_CLOSING,
            )
        )
        is not None
    )


def _next_seq(db: OrmSession, session_id: int) -> int:
    """SessionMessage.seq：会话内全局递增（跨 channel 保留完整时间线）。"""
    return (
        db.scalar(
            select(func.max(SessionMessage.seq)).where(SessionMessage.session_id == session_id)
        )
        or 0
    ) + 1


def _owned_session(db: OrmSession, session_id: int, user: dict) -> AssessmentSession:
    """对话/实操路由共用的会话装载：归属校验 + 进行中校验。"""
    session = db.get(AssessmentSession, session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.status != "in_progress":
        raise HTTPException(status_code=400, detail="会话已结束")
    return session


def _maybe_advance_stage(db: OrmSession, session: AssessmentSession, plan: dict | None) -> None:
    """阶段自动翻转（full 模式）：客观全完 → dialog/practical；对话题全部结束 → practical。"""
    if session.mode != "full":
        return
    if session.stage == "objective" and plan is not None and (plan["dialog"] or plan["practical"] is not None):
        session.stage = "dialog" if plan["dialog"] else "practical"  # quick 模式止于 objective
        db.add(session)
        db.commit()
    elif (
        session.stage == "dialog"
        and plan is not None
        and all(_dialog_closed(db, session.id, q) for q in plan["dialog"])
        and plan["practical"] is not None
    ):
        session.stage = "practical"
        db.add(session)
        db.commit()


def _session_view(db: OrmSession, session: AssessmentSession) -> dict:
    states = _states(session.theta_snapshot)
    answers = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == session.id)).all()
    asked = {a.question_code for a in answers}
    ceilings = _difficulty_ceilings(db)
    done = _done_dimensions(states, ceilings)

    plan = _stage_plan(db, session)
    _maybe_advance_stage(db, session, plan)  # 客观全完→主观；对话题全部结束→实操

    question = dimension = None
    reason = ""
    open_dialog = [q for q in (plan["dialog"] if plan else []) if not _dialog_closed(db, session.id, q)]
    if session.stage == "dialog" and open_dialog:
        q = open_dialog[0]
        dimension = q.dimension
        question = _question_out(q) | {"dialog_turns_taken": _dialog_turns_taken(db, session.id, q.id)}
        reason = (
            f"客观测评已完成。进入对话式测评：考官将围绕「{DIMENSION_NAMES[dimension]}」情境题"
            "结合你的回答逐步追问"
        )
    elif session.stage == "practical" and plan is not None and plan["practical"] is not None:
        q = plan["practical"]
        dimension = q.dimension
        question = _question_out(q)
        reason = f"对话式测评完成后，请在实操任务中与 AI 真实协作完成「{DIMENSION_NAMES[dimension]}」任务并提交产物"
    else:
        for d in DIMENSIONS:
            if d in done:
                continue
            picked = _pick_question(db, d, states[d], asked)
            if picked is not None:
                dimension = d
                question = _question_out(picked)
                break
        if question is not None:
            s = states[dimension]
            reason = (
                f"你在「{DIMENSION_NAMES[dimension]}」当前估计 {s.theta:.1f} 分"
                f"（{adaptive.LEVEL_NAMES[adaptive.dimension_level(s.theta)]}），"
                f"本题难度 {question['difficulty']}，用于校准你的水平边界"
            )
    progress = {
        d: {
            "name": DIMENSION_NAMES[d],
            "theta": round(states[d].theta, 3),
            "level": adaptive.dimension_level(states[d].theta),
            "level_name": adaptive.LEVEL_NAMES[adaptive.dimension_level(states[d].theta)],
            "n": states[d].n,
            "done": d in done,
        }
        for d in DIMENSIONS
    }
    return {
        "session_id": session.id,
        "status": session.status,
        "stage": session.stage,
        "question": question,
        "next_dimension": dimension,
        "reason": reason,
        "progress": progress,
    }


@router.post("")
def start_session(body: StartIn, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
    if body.mode not in ("full", "quick"):
        raise HTTPException(status_code=400, detail="mode 仅支持 full/quick")
    session = AssessmentSession(user_id=user["id"], mode=body.mode, theta_snapshot=_snapshot(_states({})))
    db.add(session)
    db.commit()
    db.refresh(session)
    return _session_view(db, session)


@router.post("/{session_id}/answer")
def submit_answer(
    session_id: int,
    body: AnswerIn,
    user: dict = Depends(current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    session = db.get(AssessmentSession, session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.status != "in_progress":
        raise HTTPException(status_code=400, detail="会话已结束")
    if session.stage != "objective":
        raise HTTPException(status_code=400, detail="当前阶段不支持客观题作答")
    question = db.get(Question, body.question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    answers = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == session.id)).all()
    asked = {a.question_code for a in answers}
    if question.code in asked:
        raise HTTPException(status_code=400, detail="该题已作答")
    if question.type not in OBJECTIVE_TYPES:
        raise HTTPException(status_code=400, detail="该题型不支持线上客观作答")
    if question.type == "judge" and not isinstance(body.answer, bool):
        raise HTTPException(status_code=400, detail="判断题答案必须为布尔值")

    is_correct = grade_objective(question.type, body.answer, question.answer)
    slow = is_correct and body.time_spent > 2 * question.est_seconds
    new_state = adaptive.update(
        _states(session.theta_snapshot)[question.dimension],
        question.difficulty,
        result_from_correct(is_correct),
        slow=slow,
    )
    states = _states(session.theta_snapshot)
    states[question.dimension] = new_state
    session.theta_snapshot = _snapshot(states)
    db.add(
        SessionAnswer(
            session_id=session.id,
            question_id=question.id,
            question_code=question.code,
            dimension=question.dimension,
            answer=body.answer,
            is_correct=is_correct,
            score=result_from_correct(is_correct),
            time_spent=body.time_spent,
            theta_after=new_state.theta,
            seq=len(answers) + 1,
        )
    )
    db.commit()
    db.refresh(session)
    return _session_view(db, session) | {
        "just": {
            "is_correct": is_correct,
            "explanation": question.explanation,
            "dimension": question.dimension,
            "theta": round(new_state.theta, 3),
        }
    }


@router.post("/{session_id}/finish")
def finish_session(session_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
    session = db.get(AssessmentSession, session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    existing = db.scalar(select(Report).where(Report.session_id == session.id))
    if existing is not None:
        return {"report_id": existing.id}
    if session.mode != "quick" and session.stage != "ready":
        # full 模式须完成对话式测评与实操（stage 到达实操提交后的 ready 态）方可生成报告
        raise HTTPException(status_code=400, detail="请先完成对话式测评与实操任务")

    def _chat(messages: list[dict], **kwargs) -> str:
        return chat_completion(messages, **kwargs)  # model_role 等由调用方（generate_llm_advice）传入

    # 无 Key/上游异常时 provider 抛 ProviderUnavailableError，由 generate_llm_advice 捕获并回退模板
    report = build_report(db, session, chat_fn=_chat)
    session.status = "finished"
    session.finished_at = utcnow()
    db.add(report)
    db.commit()
    db.refresh(report)
    return {"report_id": report.id}
