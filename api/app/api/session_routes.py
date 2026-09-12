from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.auth import current_user
from app.engine import adaptive
from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES, DimensionState
from app.engine.grading import grade_objective, result_from_correct
from app.models import AssessmentSession, Question, Report, SessionAnswer, utcnow
from app.report.generate import build_report

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

OBJECTIVE_TYPES = ("single", "multi", "judge")


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


def _done_dimensions(states: dict[str, DimensionState]) -> set[str]:
    return {d for d in DIMENSIONS if states[d].n > 0 and adaptive.should_stop(states[d])}


def _session_view(db: OrmSession, session: AssessmentSession) -> dict:
    states = _states(session.theta_snapshot)
    answers = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == session.id)).all()
    asked = {a.question_code for a in answers}
    done = _done_dimensions(states)
    question = dimension = None
    for d in DIMENSIONS:
        if d in done:
            continue
        question = _pick_question(db, d, states[d], asked)
        if question is not None:
            dimension = d
            break
    reason = ""
    if question is not None:
        s = states[dimension]
        reason = (
            f"你在「{DIMENSION_NAMES[dimension]}」当前估计 {s.theta:.1f} 分"
            f"（{adaptive.LEVEL_NAMES[adaptive.dimension_level(s.theta)]}），"
            f"本题难度 {question.difficulty}，用于校准你的水平边界"
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
        "question": _question_out(question) if question else None,
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
    question = db.get(Question, body.question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    answers = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == session.id)).all()
    asked = {a.question_code for a in answers}
    if question.code in asked:
        raise HTTPException(status_code=400, detail="该题已作答")
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
    report = build_report(db, session)
    session.status = "finished"
    session.finished_at = utcnow()
    db.add(report)
    db.commit()
    db.refresh(report)
    return {"report_id": report.id}
