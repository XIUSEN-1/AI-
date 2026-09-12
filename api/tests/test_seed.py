import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Question
from app.seed import import_questions, load_seed_files, validate_question

FIXTURES = Path(__file__).parent / "fixtures" / "test_bank.json"


@pytest.fixture(scope="module")
def items() -> list[dict]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))["questions"]


def test_fixture_bank_has_12_questions(items):
    assert len(items) == 12
    assert {q["dimension"] for q in items} == {"D1", "D2", "D3", "D4", "D5", "D6"}


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "X1", "dimension": "D9", "tier": "basic", "type": "single", "difficulty": 3, "stem": "s", "options": [{"key": "A", "text": "a"}, {"key": "B", "text": "b"}], "answer": "A", "est_seconds": 60},
        {"id": "X2", "dimension": "D1", "tier": "basic", "type": "single", "difficulty": 5, "stem": "s", "options": [{"key": "A", "text": "a"}, {"key": "B", "text": "b"}], "answer": "A", "est_seconds": 60},
        {"id": "X3", "dimension": "D1", "tier": "basic", "type": "single", "difficulty": 2, "stem": "s", "options": [{"key": "A", "text": "a"}, {"key": "B", "text": "b"}], "answer": "Z", "est_seconds": 60},
        {"id": "X4", "dimension": "D1", "tier": "advanced", "type": "open", "difficulty": 4, "stem": "s", "est_seconds": 60},
    ],
)
def test_validate_rejects_bad_question(bad):
    with pytest.raises(ValueError):
        validate_question(bad)


def test_import_is_idempotent(items):
    init_db()
    with SessionLocal() as db:
        first = import_questions(items, db)
        second = import_questions(items, db)
    # conftest 的 session 级 seeded_db 已先行导入，故本次 first 可能是全新导入(12)或重复导入(0)
    assert first["updated"] == 0
    assert first["created"] in (0, 12)
    assert second == {"created": 0, "updated": 0}
    with SessionLocal() as db:
        codes = db.scalars(select(Question.code)).all()
        assert len(codes) == 12
