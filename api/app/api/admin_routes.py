"""管理后台（spec §8）：教师账号创建、题库 CRUD（validate_question 校验/版本递增/软删）、
人工复核队列与 resolve 人工终评。

resolve 裁定语义（计划内）：answer.score=final_score、报告逐题 rationale 追加"[人工复核]"
且 score 同步（报告以人工分为准）、开放/实操题 is_correct 保持 None（客观题若入队按
final_score≥3 同步对错）、review_queue.status='resolved'；**θ 不重放**（复核为个位数题目，
重放破坏性大收益小，报告逐题分以人工分为准）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.api.session_routes import OBJECTIVE_TYPES
from app.auth import hash_password, require_roles
from app.models import AssessmentSession, Question, Report, ReviewQueue, SessionAnswer, User
from app.seed import _MUTABLE, validate_question

router = APIRouter(prefix="/api/admin", tags=["admin"])

CORRECT_THRESHOLD = 3  # 客观题入复核队列（异常路径）时的人工终评对错折算阈值（≥良好记对）


# ---------- 教师账号 ----------

class TeacherIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=6, max_length=64)


@router.post("/teachers")
def create_teacher(body: TeacherIn, user: dict = Depends(require_roles("admin")), db: OrmSession = Depends(get_db)) -> dict:
    if db.scalar(select(User.id).where(User.student_no == body.username)) is not None:
        raise HTTPException(status_code=400, detail="用户名已存在")
    teacher = User(
        name=body.name,
        student_no=body.username,
        role="teacher",
        password_hash=hash_password(body.password),
    )
    db.add(teacher)
    db.commit()
    db.refresh(teacher)
    return {"id": teacher.id, "name": teacher.name, "username": teacher.student_no, "role": teacher.role}


# ---------- 题库 CRUD ----------

def _question_out(q: Question) -> dict:
    """管理端全量字段（含 answer/rubric，供编辑）。"""
    return {
        "id": q.id,
        "code": q.code,
        "dimension": q.dimension,
        "tier": q.tier,
        "type": q.type,
        "difficulty": q.difficulty,
        "stem": q.stem,
        "options": q.options,
        "answer": q.answer,
        "tags": q.tags,
        "est_seconds": q.est_seconds,
        "explanation": q.explanation,
        "rubric": q.rubric,
        "status": q.status,
        "version": q.version,
    }


@router.get("/questions")
def list_questions(
    dimension: str | None = None,
    tier: str | None = None,
    difficulty: int | None = None,
    status: str | None = None,
    page: int = Query(ge=1, default=1),
    page_size: int = Query(ge=1, le=100, default=20),
    user: dict = Depends(require_roles("admin")),
    db: OrmSession = Depends(get_db),
) -> dict:
    stmt = select(Question).order_by(Question.id)
    if dimension is not None:
        stmt = stmt.where(Question.dimension == dimension)
    if tier is not None:
        stmt = stmt.where(Question.tier == tier)
    if difficulty is not None:
        stmt = stmt.where(Question.difficulty == difficulty)
    if status is not None:
        stmt = stmt.where(Question.status == status)
    rows = db.scalars(stmt).all()
    start = (page - 1) * page_size
    return {
        "total": len(rows),
        "page": page,
        "page_size": page_size,
        "items": [_question_out(q) for q in rows[start : start + page_size]],
    }


@router.post("/questions")
def create_question(payload: dict, user: dict = Depends(require_roles("admin")), db: OrmSession = Depends(get_db)) -> dict:
    try:
        validate_question(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if db.scalar(select(Question.id).where(Question.code == payload["id"])) is not None:
        raise HTTPException(status_code=400, detail=f"{payload['id']}: 题目 code 已存在")
    q = Question(code=payload["id"], **{k: payload.get(k) for k in _MUTABLE})
    db.add(q)
    db.commit()
    db.refresh(q)
    return _question_out(q)


@router.put("/questions/{question_id}")
def update_question(question_id: int, payload: dict, user: dict = Depends(require_roles("admin")), db: OrmSession = Depends(get_db)) -> dict:
    q = db.get(Question, question_id)
    if q is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    merged = {k: getattr(q, k) for k in _MUTABLE}
    merged.update({k: payload[k] for k in _MUTABLE if k in payload})
    merged["id"] = q.code  # code 不可改，仅作 validate_question 的报错标识
    try:
        validate_question(merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    for k in _MUTABLE:
        setattr(q, k, merged[k])
    q.version += 1
    db.commit()
    db.refresh(q)
    return _question_out(q)


@router.delete("/questions/{question_id}")
def delete_question(question_id: int, user: dict = Depends(require_roles("admin")), db: OrmSession = Depends(get_db)) -> dict:
    """软删：status 置 retired（保留历史作答/报告的外键与统计完整性），不做物理删除。"""
    q = db.get(Question, question_id)
    if q is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    q.status = "retired"
    db.commit()
    return {"id": q.id, "status": q.status}


# ---------- 人工复核队列 ----------

def _review_item_out(db: OrmSession, item: ReviewQueue) -> dict:
    answer = db.get(SessionAnswer, item.answer_id)
    question = db.scalar(select(Question).where(Question.code == item.question_code))
    session = db.get(AssessmentSession, item.session_id)
    owner = db.get(User, session.user_id) if session is not None else None
    return {
        "id": item.id,
        "question_code": item.question_code,
        "question_stem": question.stem if question is not None else None,
        "dimension": answer.dimension if answer is not None else None,
        "student_no": owner.student_no if owner is not None else None,
        "session_id": item.session_id,
        "answer_id": item.answer_id,
        "answer": answer.answer if answer is not None else None,
        "judge_raw": item.judge_raw,
        "reason": item.reason,
        "status": item.status,
        "resolved_score": item.resolved_score,
        "created_at": item.created_at.isoformat() + "Z",
    }


@router.get("/review-queue")
def review_queue(
    # status 筛选：open/resolved/all（all=不过滤，供前端「全部」tab）；非法值 422
    status: str = Query("open", pattern="^(open|resolved|all)$"),
    user: dict = Depends(require_roles("teacher", "admin")),  # Global Constraints：仅 admin/teacher 可见队列
    db: OrmSession = Depends(get_db),
) -> dict:
    stmt = select(ReviewQueue).order_by(ReviewQueue.id)  # 先入先复核
    if status != "all":
        stmt = stmt.where(ReviewQueue.status == status)
    rows = db.scalars(stmt).all()
    return {"items": [_review_item_out(db, r) for r in rows], "total": len(rows)}


class ResolveIn(BaseModel):
    final_score: int = Field(ge=0, le=4)


@router.post("/review-queue/{item_id}/resolve")
def resolve_review(
    item_id: int,
    body: ResolveIn,
    user: dict = Depends(require_roles("teacher", "admin")),
    db: OrmSession = Depends(get_db),
) -> dict:
    item = db.get(ReviewQueue, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="复核条目不存在")
    if item.status != "open":
        raise HTTPException(status_code=400, detail="该条目已复核")
    answer = db.get(SessionAnswer, item.answer_id)
    question = db.scalar(select(Question).where(Question.code == item.question_code))
    if answer is not None:
        answer.score = float(body.final_score)
        if question is not None and question.type in OBJECTIVE_TYPES:
            answer.is_correct = body.final_score >= CORRECT_THRESHOLD  # 开放/实操题保持 None
    item.status = "resolved"
    item.resolved_score = body.final_score
    # 报告以人工分为准：逐题回显快照按 seq（会话内唯一）定位，同步 score 并追加人工复核标注；
    # θ/radar/dimensions 不重放（裁定：复核为个位数题目，重放破坏性大收益小）
    report = db.scalar(select(Report).where(Report.session_id == item.session_id))
    if report is not None and report.answers:
        entries = [dict(x) for x in report.answers]  # copy-on-write：JSON 列整体重赋值
        for entry in entries:
            if answer is not None and entry.get("seq") == answer.seq:
                entry["score"] = float(body.final_score)
                previous = entry.get("rationale") or ""
                entry["rationale"] = (f"{previous}；" if previous else "") + "[人工复核]"
        report.answers = entries
    db.commit()
    return {"id": item.id, "status": item.status, "resolved_score": item.resolved_score}
