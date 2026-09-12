"""错题本与薄弱考点练习卷（spec M2e）：正式测评错题的只读派生 + 独立即时判分，
不回灌 θ、不进正式报告。"""

from __future__ import annotations

import random

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.api.session_routes import OBJECTIVE_TYPES, _question_out
from app.auth import current_user
from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES
from app.engine.grading import grade_objective
from app.models import AssessmentSession, PracticeAnswer, PracticeSession, Question, SessionAnswer

router = APIRouter(tags=["practice"])


def _wrongbook(db: OrmSession, user_id: int) -> list[dict]:
    """错题聚合：该用户正式测评错题（is_correct=False）按题号聚合次数，带维度/考点标签。"""
    rows = db.execute(
        select(SessionAnswer.question_code, Question, func.count().label("wrong_count"))
        .join(AssessmentSession, AssessmentSession.id == SessionAnswer.session_id)
        .join(Question, Question.id == SessionAnswer.question_id)
        .where(AssessmentSession.user_id == user_id, SessionAnswer.is_correct.is_(False))
        .group_by(SessionAnswer.question_code, Question.id)
    ).all()
    items = [
        {
            "question_id": q.id,
            "question_code": code,
            "dimension": q.dimension,
            "dimension_name": DIMENSION_NAMES[q.dimension],
            "tags": q.tags,
            "wrong_count": wrong_count,
        }
        for code, q, wrong_count in rows
    ]
    items.sort(key=lambda x: (x["dimension"], -x["wrong_count"], x["question_code"]))
    return items


def _answered_question_ids(db: OrmSession, user_id: int) -> set[int]:
    """用户已作答题目 id 集：正式测评 + 历次练习（练习抽题避开已作答）。"""
    formal = set(
        db.scalars(
            select(SessionAnswer.question_id)
            .join(AssessmentSession, AssessmentSession.id == SessionAnswer.session_id)
            .where(AssessmentSession.user_id == user_id)
        )
    )
    practiced = set(
        db.scalars(
            select(PracticeAnswer.question_id)
            .join(PracticeSession, PracticeSession.id == PracticeAnswer.practice_session_id)
            .where(PracticeSession.user_id == user_id)
        )
    )
    return formal | practiced


class PracticeStartIn(BaseModel):
    dimension: str | None = None
    tags: list[str] = Field(default_factory=list)
    size: int = Field(ge=5, le=10, default=5)


class PracticeAnswerIn(BaseModel):
    question_id: int
    answer: object


@router.get("/api/me/wrongbook")
def my_wrongbook(user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
    items = _wrongbook(db, user["id"])
    return {"items": items, "total": len(items)}


@router.post("/api/practice/sessions")
def start_practice(body: PracticeStartIn, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
    """生成练习卷：目标维度（指定或按错题量推导）内、命中目标标签（指定或取错题标签）的未作答客观题；
    不足 size 放宽到同维度任意未作答题。"""
    if body.dimension is not None and body.dimension not in DIMENSIONS:
        raise HTTPException(status_code=400, detail="dimension 非法")
    wrong_items = _wrongbook(db, user["id"])
    if body.dimension is not None:
        dims = [body.dimension]
    else:
        counts: dict[str, int] = {}
        for w in wrong_items:
            counts[w["dimension"]] = counts.get(w["dimension"], 0) + w["wrong_count"]
        dims = sorted(counts, key=lambda d: (-counts[d], d))  # 错题量降序取薄弱维度
        if not dims:
            raise HTTPException(status_code=400, detail="暂无错题记录，请先完成正式测评或指定练习维度")
    if body.tags:
        target_tags = set(body.tags)
    else:
        target_tags = {t for w in wrong_items if w["dimension"] in dims for t in w["tags"]}

    answered = _answered_question_ids(db, user["id"])
    fresh = [
        q
        for q in db.scalars(
            select(Question)
            .where(
                Question.dimension.in_(dims),
                Question.type.in_(OBJECTIVE_TYPES),
                Question.status == "published",
            )
            .order_by(Question.id)
        ).all()
        if q.id not in answered
    ]
    primary = [q for q in fresh if target_tags & set(q.tags)]
    picked = random.sample(primary, min(body.size, len(primary)))
    if len(picked) < body.size:  # 不足放宽：从同维度任意未作答题（含非标签题）补足
        chosen = {q.id for q in picked}
        rest = [q for q in fresh if q.id not in chosen]
        picked += random.sample(rest, min(body.size - len(picked), len(rest)))
    if not picked:
        raise HTTPException(status_code=400, detail="该范围内没有可练习的未作答题目")

    session = PracticeSession(user_id=user["id"], question_ids=[q.id for q in picked])
    db.add(session)
    db.commit()
    db.refresh(session)
    return {
        "id": session.id,
        "created_at": session.created_at.isoformat() + "Z",
        "dimension": body.dimension,
        "tags": sorted(target_tags),
        "size": len(picked),
        "question_ids": session.question_ids,
        "questions": [_question_out(q) for q in picked],
    }


@router.post("/api/practice/sessions/{session_id}/answer")
def submit_practice_answer(
    session_id: int, body: PracticeAnswerIn, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)
) -> dict:
    """练习作答：客观题规则即时判分，反馈对错+解析；只落 practice_answers，不更新 θ、不进正式报告。"""
    session = db.get(PracticeSession, session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="练习卷不存在")
    if body.question_id not in session.question_ids:
        raise HTTPException(status_code=400, detail="该题不在本练习卷中")
    if (
        db.scalar(
            select(PracticeAnswer.id).where(
                PracticeAnswer.practice_session_id == session.id,
                PracticeAnswer.question_id == body.question_id,
            )
        )
        is not None
    ):
        raise HTTPException(status_code=400, detail="该题已作答")
    question = db.get(Question, body.question_id)
    if question.type not in OBJECTIVE_TYPES:
        raise HTTPException(status_code=400, detail="该题型不支持练习作答")
    if question.type == "judge" and not isinstance(body.answer, bool):
        raise HTTPException(status_code=400, detail="判断题答案必须为布尔值")

    is_correct = grade_objective(question.type, body.answer, question.answer)
    db.add(
        PracticeAnswer(
            practice_session_id=session.id, question_id=question.id, answer=body.answer, is_correct=is_correct
        )
    )
    db.commit()
    answered = (
        db.scalar(
            select(func.count())
            .select_from(PracticeAnswer)
            .where(PracticeAnswer.practice_session_id == session.id)
        )
        or 0
    )
    return {
        "question_id": question.id,
        "is_correct": is_correct,
        "explanation": question.explanation,
        "answered": answered,
        "total": len(session.question_ids),
    }
