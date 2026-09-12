from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Klass(Base):
    __tablename__ = "classes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    invite_code: Mapped[str] = mapped_column(String(16), unique=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32))
    student_no: Mapped[str] = mapped_column(String(32), unique=True)
    role: Mapped[str] = mapped_column(String(16))  # student | teacher | admin
    class_id: Mapped[int | None] = mapped_column(ForeignKey("classes.id"), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)  # 种子 id，如 D1-B03
    dimension: Mapped[str] = mapped_column(String(4))  # D1..D6
    tier: Mapped[str] = mapped_column(String(8))  # basic | advanced
    type: Mapped[str] = mapped_column(String(12))  # single | multi | judge | open | practical
    difficulty: Mapped[int] = mapped_column(Integer)  # 1..5
    stem: Mapped[str] = mapped_column(String(2000))
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)
    answer: Mapped[object | None] = mapped_column(JSON, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    est_seconds: Mapped[int] = mapped_column(Integer, default=60)
    explanation: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    rubric: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="published")
    version: Mapped[int] = mapped_column(Integer, default=1)


class AssessmentSession(Base):
    __tablename__ = "assessment_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    mode: Mapped[str] = mapped_column(String(8), default="full")  # full | quick
    status: Mapped[str] = mapped_column(String(16), default="in_progress")  # in_progress | judging | finished
    # 四阶段：客观 → 对话式 → 实操；ready 为实操已提交（T4）、finish 放行的前置态；quick 止于 objective
    stage: Mapped[str] = mapped_column(String(12), default="objective")  # objective | dialog | practical | ready
    theta_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    judging_step: Mapped[int] = mapped_column(Integer, default=0)  # 异步判题进度：已完成题数（前端轮询 x/y）
    judging_total: Mapped[int] = mapped_column(Integer, default=0)  # 异步判题总数（主观题数）
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SessionAnswer(Base):
    __tablename__ = "session_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("assessment_sessions.id"))
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    question_code: Mapped[str] = mapped_column(String(16))
    dimension: Mapped[str] = mapped_column(String(4))
    answer: Mapped[object] = mapped_column(JSON)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_spent: Mapped[int] = mapped_column(Integer, default=0)
    theta_after: Mapped[float] = mapped_column(Float)
    seq: Mapped[int] = mapped_column(Integer)


class SessionMessage(Base):
    __tablename__ = "session_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("assessment_sessions.id"))
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    channel: Mapped[str] = mapped_column(String(12))  # dialog | practical
    role: Mapped[str] = mapped_column(String(12))  # learner | examiner | assistant | submit(产物提交)
    content: Mapped[str] = mapped_column(Text)  # 学员发言/考官回复/实操产物文本
    seq: Mapped[int] = mapped_column(Integer)  # 会话内全局递增，保留完整时间线
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("assessment_sessions.id"), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    dimensions: Mapped[list] = mapped_column(JSON)  # 六维明细数组
    total_level: Mapped[int] = mapped_column(Integer)  # 1..5
    radar: Mapped[list] = mapped_column(JSON)  # [{dimension,label,value(0-100)}]
    strengths: Mapped[list] = mapped_column(JSON)  # ["D2","D3"]
    gaps: Mapped[list] = mapped_column(JSON)  # ["D1","D6"]
    advice: Mapped[list] = mapped_column(JSON)  # [str]
    advice_source: Mapped[str] = mapped_column(String(8), default="template")  # llm | template
    answers: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 逐题回显快照（M2a 前的报告为 NULL）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReviewQueue(Base):
    __tablename__ = "review_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_code: Mapped[str] = mapped_column(String(16))
    session_id: Mapped[int] = mapped_column(ForeignKey("assessment_sessions.id"))
    answer_id: Mapped[int] = mapped_column(ForeignKey("session_answers.id"))
    judge_raw: Mapped[dict] = mapped_column(JSON)  # 判题原始结果（含各次跑分）
    reason: Mapped[str] = mapped_column(String(200))  # 入队原因（如三跑分差过大）
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | resolved
    resolved_score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 人工终评分
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
