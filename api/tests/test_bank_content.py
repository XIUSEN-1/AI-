from pathlib import Path

import pytest

from app.seed import load_seed_files, validate_question

SEEDS = Path(__file__).resolve().parent.parent.parent / "seeds" / "questions"


def test_seed_dir_has_six_files():
    assert len(list(SEEDS.glob("D*.json"))) == 6


@pytest.mark.parametrize("dim", ["D1", "D2", "D3", "D4", "D5", "D6"])
def test_bank_coverage_per_dimension(dim):
    items = [q for q in load_seed_files(SEEDS) if q["dimension"] == dim]
    basic = [q for q in items if q["tier"] == "basic"]
    advanced = [q for q in items if q["tier"] == "advanced"]
    objective_advanced = [
        q for q in advanced if q["type"] in ("single", "multi", "judge")
    ]
    assert len(basic) >= 10, f"{dim} 基础题不足 10"
    assert len(advanced) >= 5, f"{dim} 进阶题不足 5"
    assert any(q["type"] == "practical" for q in advanced), f"{dim} 缺实操题"
    assert len(objective_advanced) >= 4, f"{dim} 客观高难题不足 4"
    assert {q["difficulty"] for q in basic} >= {1, 2, 3}, f"{dim} 基础题难度未覆盖 1~3"
    assert all(q["difficulty"] >= 4 for q in advanced)


def test_bank_all_valid_and_unique():
    items = load_seed_files(SEEDS)
    codes = [q["id"] for q in items]
    assert len(codes) == len(set(codes)) == 114
    for q in items:
        validate_question(q)
