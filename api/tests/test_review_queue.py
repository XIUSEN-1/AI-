"""人工复核队列测试：建表、入队、幂等、status 流转。"""

import uuid

import pytest
from sqlalchemy import inspect

from app.db import SessionLocal, engine
from app.judge.pipeline import enqueue_review
from app.models import AssessmentSession, Question, ReviewQueue, SessionAnswer, User


@pytest.fixture()
def sample():
    """造一条满足外键的真实作答（用户/会话/题目/作答），返回关联 id；用毕清理，不污染共享库。"""
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        user = User(name="复核学员", student_no=f"RV-{suffix}", role="student")
        db.add(user)
        db.flush()
        session = AssessmentSession(user_id=user.id)
        db.add(session)
        db.flush()
        question = Question(
            code=f"D2-X{suffix[:4]}",
            dimension="D2",
            tier="basic",
            type="open",
            difficulty=3,
            stem="测试开放题",
            tags=[],
        )
        db.add(question)
        db.flush()
        answer = SessionAnswer(
            session_id=session.id,
            question_id=question.id,
            question_code=question.code,
            dimension="D2",
            answer={"text": "作答"},
            theta_after=3.0,
            seq=1,
        )
        db.add(answer)
        db.commit()
        ids = {
            "user_id": user.id,
            "session_id": session.id,
            "question_id": question.id,
            "answer_id": answer.id,
            "question_code": question.code,
        }
    yield ids
    with SessionLocal() as db:
        db.query(ReviewQueue).filter(ReviewQueue.answer_id == ids["answer_id"]).delete()
        db.query(SessionAnswer).filter(SessionAnswer.id == ids["answer_id"]).delete()
        db.query(Question).filter(Question.id == ids["question_id"]).delete()
        db.query(AssessmentSession).filter(AssessmentSession.id == ids["session_id"]).delete()
        db.query(User).filter(User.id == ids["user_id"]).delete()
        db.commit()


def test_review_queue_table_created():
    assert inspect(engine).has_table("review_queue")


def test_enqueue_persists_fields(sample):
    with SessionLocal() as db:
        row = enqueue_review(
            db,
            sample["question_code"],
            sample["session_id"],
            sample["answer_id"],
            judge_raw={"score": 3, "runs": [1, 4, 3], "rationale": "理由"},
            reason="三跑分差过大",
        )
        assert row.id is not None
        assert row.status == "open"
        assert row.resolved_score is None
        assert row.created_at is not None
    with SessionLocal() as db:  # 新会话重查，确认已持久化
        saved = db.get(ReviewQueue, row.id)
        assert saved.question_code == sample["question_code"]
        assert saved.session_id == sample["session_id"]
        assert saved.answer_id == sample["answer_id"]
        assert saved.judge_raw == {"score": 3, "runs": [1, 4, 3], "rationale": "理由"}
        assert saved.reason == "三跑分差过大"
        assert saved.status == "open"


def test_enqueue_idempotent_while_open(sample):
    with SessionLocal() as db:
        first = enqueue_review(db, sample["question_code"], sample["session_id"], sample["answer_id"], {"score": 2}, "分差过大")
        second = enqueue_review(db, sample["question_code"], sample["session_id"], sample["answer_id"], {"score": 2}, "分差过大")
        assert second.id == first.id  # 未处理条目不重复入队
        assert db.query(ReviewQueue).filter(ReviewQueue.answer_id == sample["answer_id"]).count() == 1


def test_status_flow_open_to_resolved(sample):
    with SessionLocal() as db:
        row = enqueue_review(db, sample["question_code"], sample["session_id"], sample["answer_id"], {"score": 2}, "分差过大")
        row.status = "resolved"
        row.resolved_score = 3
        db.commit()
    with SessionLocal() as db:
        saved = db.get(ReviewQueue, row.id)
        assert saved.status == "resolved"
        assert saved.resolved_score == 3
        # 已处理条目不再拦截：同一作答再次需复核时生成新条目
        again = enqueue_review(db, sample["question_code"], sample["session_id"], sample["answer_id"], {"score": 4}, "再次分差过大")
        assert again.id != saved.id
        assert again.status == "open"
