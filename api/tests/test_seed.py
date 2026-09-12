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


def test_fixture_bank_has_18_questions(items):
    assert len(items) == 18
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


def test_validate_rejects_bool_difficulty():
    bad = {
        "id": "X5", "dimension": "D1", "tier": "basic", "type": "single", "difficulty": True,
        "stem": "s", "options": [{"key": "A", "text": "a"}, {"key": "B", "text": "b"}],
        "answer": "A", "est_seconds": 60,
    }
    with pytest.raises(ValueError):
        validate_question(bad)


def test_validate_rejects_non_dict_option():
    bad = {
        "id": "X6", "dimension": "D1", "tier": "basic", "type": "single", "difficulty": 2,
        "stem": "s", "options": [{"key": "A", "text": "a"}, "B"],
        "answer": "A", "est_seconds": 60,
    }
    with pytest.raises(ValueError):
        validate_question(bad)


def test_import_rejects_duplicate_code_in_batch(items):
    duplicated = [*items, dict(items[0])]
    init_db()
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="批内存在重复 id"):
            import_questions(duplicated, db)


def test_load_seed_files_error_includes_filename(tmp_path):
    (tmp_path / "bad_bank.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="bad_bank.json"):
        load_seed_files(tmp_path)


def test_import_is_idempotent(items):
    init_db()
    with SessionLocal() as db:
        first = import_questions(items, db)
        second = import_questions(items, db)
    # conftest 的 session 级 seeded_db 已先行导入，故本次 first 可能是全新导入(18)或重复导入(0)
    assert first["updated"] == 0
    assert first["created"] in (0, 18)
    assert second == {"created": 0, "updated": 0}
    with SessionLocal() as db:
        codes = db.scalars(select(Question.code)).all()
        assert len(codes) == 18
