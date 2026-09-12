"""对话式测评考官端点（spec §6.2 / §5.3）：开场白、SSE 追问、留痕与切题。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.api.session_routes import (
    DIALOG_CLOSING,
    DIALOG_SKIPPED,
    _dialog_closed,
    _dialog_turns_taken,
    _maybe_advance_stage,
    _next_seq,
    _owned_session,
    _question_out,
    _session_view,
    _stage_plan,
)
from app.auth import current_user
from app.engine.adaptive import DIMENSION_NAMES
from app.llm.provider import ProviderUnavailableError, chat_completion, chat_stream
from app.models import AssessmentSession, Question, SessionAnswer, SessionMessage

router = APIRouter(prefix="/api/sessions", tags=["dialog"])

MAX_TURNS = 3
PROVIDER_ERROR = "AI 服务暂不可用，请稍后重试或跳过本题"

# 三档追问策略（spec §5.3）：按已轮次递进，第 3 轮为最后一轮
_TIER_STRATEGY = {
    1: "澄清——校验学员对情境与核心概念的理解，请学员澄清模糊表述、结合自身实际经验把观点说具体。",
    2: "深挖——针对学员此前的回答追问背后的原理与工程取舍（为什么这样做、有什么替代方案、各自代价）。",
    3: "反例挑战——构造边界条件或反例来质疑学员的观点，要求学员识别其中问题并自我纠错。",
}


class TurnIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _require_dialog(db: OrmSession, session: AssessmentSession) -> Question:
    """推进阶段翻转后返回第一道未结束的对话题，并完成阶段/题目级校验。"""
    plan = _stage_plan(db, session)
    _maybe_advance_stage(db, session, plan)
    if session.stage != "dialog":
        raise HTTPException(status_code=400, detail="当前阶段不支持对话式测评")
    q = next((q for q in plan["dialog"] if not _dialog_closed(db, session.id, q)), None) if plan else None
    if q is None:
        raise HTTPException(status_code=400, detail="当前没有进行中的对话题")
    if (
        db.scalar(
            select(SessionAnswer.id).where(
                SessionAnswer.session_id == session.id, SessionAnswer.question_id == q.id
            )
        )
        is not None
    ):
        raise HTTPException(status_code=400, detail="该题已判分")
    return q


def _examiner_system(q: Question, turn: int) -> str:
    return (
        f"你是对话式测评的考官，正在考察维度「{DIMENSION_NAMES[q.dimension]}」。\n"
        f"情境题：{q.stem}\n"
        f"本轮是第 {turn} 轮追问，追问策略：{_TIER_STRATEGY[turn]}\n"
        "要求：只用简体中文；每次只提出一个追问；不透露评分标准与答案要点；不替学员作答；回复不超过 200 字。"
    )


def _opening_messages(q: Question) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                f"你是对话式测评的考官，正在考察维度「{DIMENSION_NAMES[q.dimension]}」。\n"
                f"情境题：{q.stem}\n"
                "请用一两句自然的中文开场：向学员完整转述这道情境题并邀请其作答。"
                "不透露评分标准；不复述本指令。"
            ),
        },
        {"role": "user", "content": "请生成考官开场白。"},
    ]


def _history(db: OrmSession, session_id: int, question_id: int) -> list[dict]:
    """该题全部留痕转 LLM 消息：考官→assistant，学员→user。"""
    rows = db.scalars(
        select(SessionMessage)
        .where(
            SessionMessage.session_id == session_id,
            SessionMessage.question_id == question_id,
            SessionMessage.channel == "dialog",
            SessionMessage.role.in_(("examiner", "learner")),
        )
        .order_by(SessionMessage.seq)
    ).all()
    return [
        {"role": "assistant" if m.role == "examiner" else "user", "content": m.content} for m in rows
    ]


@router.post("/{session_id}/dialog/start")
def dialog_start(
    session_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)
) -> dict:
    session = _owned_session(db, session_id, user)
    q = _require_dialog(db, session)
    existing = db.scalar(
        select(SessionMessage)
        .where(
            SessionMessage.session_id == session.id,
            SessionMessage.question_id == q.id,
            SessionMessage.channel == "dialog",
            SessionMessage.role == "examiner",
        )
        .order_by(SessionMessage.seq)
    )
    if existing is not None:  # 幂等：已开场直接回放，不再调 LLM
        opening = existing.content
    else:
        try:
            opening = chat_completion(_opening_messages(q), model_role="chat", temperature=0.7)
        except ProviderUnavailableError:
            opening = f"请结合你的实际经验，谈谈：{q.stem}"
        db.add(
            SessionMessage(
                session_id=session.id,
                question_id=q.id,
                channel="dialog",
                role="examiner",
                content=opening,
                seq=_next_seq(db, session.id),
            )
        )
        db.commit()
    return {
        "question": _question_out(q)
        | {"dialog_turns_taken": _dialog_turns_taken(db, session.id, q.id)},
        "opening": opening,
    }


@router.post("/{session_id}/dialog/turn")
def dialog_turn(
    session_id: int,
    body: TurnIn,
    user: dict = Depends(current_user),
    db: OrmSession = Depends(get_db),
) -> StreamingResponse:
    session = _owned_session(db, session_id, user)
    q = _require_dialog(db, session)
    if (
        db.scalar(
            select(SessionMessage.id).where(
                SessionMessage.session_id == session.id,
                SessionMessage.question_id == q.id,
                SessionMessage.channel == "dialog",
                SessionMessage.role == "examiner",
            )
        )
        is None
    ):
        raise HTTPException(status_code=400, detail="请先开始本题（POST dialog/start）")
    turn_no = _dialog_turns_taken(db, session.id, q.id) + 1
    db.add(
        SessionMessage(
            session_id=session.id,
            question_id=q.id,
            channel="dialog",
            role="learner",
            content=body.message,
            seq=_next_seq(db, session.id),
        )
    )
    db.commit()  # 学员发言先落库：即便 AI 不可用也保留完整时间线
    messages = [{"role": "system", "content": _examiner_system(q, turn_no)}]
    messages += _history(db, session.id, q.id)
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
                    channel="dialog",
                    role="examiner",
                    content="".join(parts),
                    seq=_next_seq(db, session_id_),
                )
            )
            db.commit()
            yield _sse({"done": True, "turns": turn_no})
        except ProviderUnavailableError:  # 不中断会话：学员可重试或跳过本题
            yield _sse({"error": PROVIDER_ERROR})
            yield _sse({"done": True, "turns": turn_no})

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/{session_id}/dialog/finish-question")
def dialog_finish_question(
    session_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)
) -> dict:
    session = _owned_session(db, session_id, user)
    q = _require_dialog(db, session)
    db.add(
        SessionMessage(
            session_id=session.id,
            question_id=q.id,
            channel="dialog",
            role="examiner",
            content=DIALOG_CLOSING,
            seq=_next_seq(db, session.id),
        )
    )
    db.commit()
    db.refresh(session)
    return _session_view(db, session)


@router.post("/{session_id}/dialog/skip")
def dialog_skip(
    session_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)
) -> dict:
    """学员主动跳过当前对话题：落跳过标记（判定闭题），复用既有切题/阶段翻转逻辑。"""
    session = _owned_session(db, session_id, user)
    q = _require_dialog(db, session)
    db.add(
        SessionMessage(
            session_id=session.id,
            question_id=q.id,
            channel="dialog",
            role="examiner",
            content=DIALOG_SKIPPED,
            seq=_next_seq(db, session.id),
        )
    )
    db.commit()
    db.refresh(session)
    return _session_view(db, session)
