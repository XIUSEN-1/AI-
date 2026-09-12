"""实操任务端点（spec §6.3 真实 AI 协作窗）：任务卡、SSE 协作窗与产物提交。

实操 system 由服务端注入：通用助手角色，不含题面与判分信息（不泄题、不代学员做判断）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.api.session_routes import (
    PRACTICAL_SKIPPED,
    _maybe_advance_stage,
    _next_seq,
    _owned_session,
    _question_out,
    _stage_plan,
)
from app.auth import current_user
from app.llm.provider import ProviderUnavailableError, chat_stream
from app.models import AssessmentSession, Question, SessionMessage

router = APIRouter(prefix="/api/sessions", tags=["practical"])

ARTIFACT_MIN = 200
ARTIFACT_MAX = 5000
CHAT_MAX_TURNS = 20
PROVIDER_ERROR = "AI 服务暂不可用，请稍后重试或跳过本题"

PRACTICAL_SYSTEM = (
    "你是一个通用 AI 助手，学员正在与你自由协作完成一项工作任务。"
    "请像日常助手一样提供帮助：讨论思路、给出建议、协助草拟内容都可以。"
    "要求：使用简体中文；不要提及任何测评、评分标准或能力维度；不主动评价学员表现；"
    "关键判断与最终方案由学员自己做出。"
)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class SubmitIn(BaseModel):
    artifact: str = Field(min_length=ARTIFACT_MIN, max_length=ARTIFACT_MAX)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _require_practical(db: OrmSession, session: AssessmentSession) -> Question:
    """推进阶段翻转后返回实操题，并完成阶段级校验。"""
    plan = _stage_plan(db, session)
    _maybe_advance_stage(db, session, plan)
    if session.stage != "practical":
        raise HTTPException(status_code=400, detail="当前阶段不支持实操任务")
    q = plan["practical"] if plan else None
    if q is None:
        raise HTTPException(status_code=400, detail="当前会话没有实操任务")
    return q


def _chat_turns_taken(db: OrmSession, session_id: int) -> int:
    """实操协作窗已进行的轮次：以学员发言条数计。"""
    return (
        db.scalar(
            select(func.count(SessionMessage.id)).where(
                SessionMessage.session_id == session_id,
                SessionMessage.channel == "practical",
                SessionMessage.role == "learner",
            )
        )
        or 0
    )


def _history(db: OrmSession, session_id: int) -> list[dict]:
    """协作窗全部留痕转 LLM 消息：学员→user，助手→assistant（不含产物提交行）。"""
    rows = db.scalars(
        select(SessionMessage)
        .where(
            SessionMessage.session_id == session_id,
            SessionMessage.channel == "practical",
            SessionMessage.role.in_(("learner", "assistant")),
        )
        .order_by(SessionMessage.seq)
    ).all()
    return [
        {"role": "assistant" if m.role == "assistant" else "user", "content": m.content} for m in rows
    ]


@router.get("/{session_id}/practical/task")
def practical_task(
    session_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)
) -> dict:
    session = _owned_session(db, session_id, user)
    q = _require_practical(db, session)
    return {
        "question": _question_out(q),  # 题面（含产物要求）随 stem 下发，不带 answer/rubric
        "artifact_min": ARTIFACT_MIN,
        "artifact_max": ARTIFACT_MAX,
    }


@router.post("/{session_id}/practical/chat")
def practical_chat(
    session_id: int,
    body: ChatIn,
    user: dict = Depends(current_user),
    db: OrmSession = Depends(get_db),
) -> StreamingResponse:
    session = _owned_session(db, session_id, user)
    q = _require_practical(db, session)
    taken = _chat_turns_taken(db, session.id)
    if taken >= CHAT_MAX_TURNS:  # 防滥用：单会话协作窗轮次上限
        raise HTTPException(status_code=400, detail="实操协作对话已达 20 轮上限")
    db.add(
        SessionMessage(
            session_id=session.id,
            question_id=q.id,
            channel="practical",
            role="learner",
            content=body.message,
            seq=_next_seq(db, session.id),
        )
    )
    db.commit()  # 学员消息先落库：即便 AI 不可用也保留完整时间线
    turn_no = taken + 1
    messages = [{"role": "system", "content": PRACTICAL_SYSTEM}] + _history(db, session.id)
    session_id_, question_id_ = session.id, q.id

    def gen():
        parts: list[str] = []
        try:
            for delta in chat_stream(messages, model_role="chat"):
                parts.append(delta)
                yield _sse({"delta": delta})
            db.add(
                SessionMessage(
                    session_id=session_id_,
                    question_id=question_id_,
                    channel="practical",
                    role="assistant",
                    content="".join(parts),
                    seq=_next_seq(db, session_id_),
                )
            )
            db.commit()
            yield _sse({"done": True, "turns": turn_no})
        except ProviderUnavailableError:  # 不中断会话：学员可稍后重试
            yield _sse({"error": PROVIDER_ERROR})
            yield _sse({"done": True, "turns": turn_no})

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/{session_id}/practical/submit")
def practical_submit(
    session_id: int,
    body: SubmitIn,
    user: dict = Depends(current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    session = _owned_session(db, session_id, user)
    q = _require_practical(db, session)
    db.add(
        SessionMessage(  # 产物以 submit 角色落库：与协作窗聊天消息区分，供 finish 判题取用
            session_id=session.id,
            question_id=q.id,
            channel="practical",
            role="submit",
            content=body.artifact,
            seq=_next_seq(db, session.id),
        )
    )
    session.stage = "ready"  # finish 放行的前置态
    db.add(session)
    db.commit()
    return {"submitted": True, "stage": "ready"}


@router.post("/{session_id}/practical/skip")
def practical_skip(
    session_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)
) -> dict:
    """学员主动跳过实操任务：落跳过标记（无产物行），直接置 ready 供 finish 放行。"""
    session = _owned_session(db, session_id, user)
    q = _require_practical(db, session)
    db.add(
        SessionMessage(  # examiner 角色落跳过标记：finish 见此标记且无 submit 行 → 跳过语义
            session_id=session.id,
            question_id=q.id,
            channel="practical",
            role="examiner",
            content=PRACTICAL_SKIPPED,
            seq=_next_seq(db, session.id),
        )
    )
    session.stage = "ready"
    db.add(session)
    db.commit()
    return {"skipped": True, "stage": "ready"}
