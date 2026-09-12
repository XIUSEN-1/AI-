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
    assert len(codes) == len(set(codes)) == 207
    for q in items:
        validate_question(q)


def _id_num(qid: str) -> int:
    """D1-B11 -> 11；D1-H05 -> 5；D1-A06 -> 6。"""
    return int(qid.split("-")[1][1:])


def test_bank_batch1_quota_d1_d3():
    """批次 1 前半：D1~D3 各 +31（客观 25 + 主观 6），id 续号与难度金字塔精确。"""
    items = load_seed_files(SEEDS)
    assert len(items) == 207
    for dim in ["D1", "D2", "D3"]:
        qs = [q for q in items if q["dimension"] == dim]
        assert len(qs) == 50, f"{dim} 应为 50 题"
        basic = [q for q in qs if q["tier"] == "basic"]
        adv_obj = [q for q in qs if q["tier"] == "advanced" and q["type"] in ("single", "multi", "judge")]
        open_q = [q for q in qs if q["type"] == "open"]
        practical = [q for q in qs if q["type"] == "practical"]
        assert len(basic) == 27, f"{dim} 基础客观应为 27"
        assert len(adv_obj) == 12, f"{dim} 高难客观应为 12"
        assert len(open_q) == 8, f"{dim} open 应为 8"
        assert len(practical) == 3, f"{dim} practical 应为 3"
        # 新增客观题（B11.. / H05..）难度金字塔：d1×5 d2×6 d3×6 d4×5 d5×3
        new_obj = [
            q for q in adv_obj if _id_num(q["id"]) >= 5
        ] + [q for q in basic if _id_num(q["id"]) >= 11]
        assert len(new_obj) == 25, f"{dim} 新增客观应为 25"
        by_diff = {}
        for q in new_obj:
            by_diff[q["difficulty"]] = by_diff.get(q["difficulty"], 0) + 1
        assert by_diff == {1: 5, 2: 6, 3: 6, 4: 5, 5: 3}, f"{dim} 难度金字塔不符：{by_diff}"
        # 新增主观题：A06..A09 open + A10..A11 practical，advanced 且难度 4..5
        new_subj = [q for q in qs if q["type"] in ("open", "practical") and _id_num(q["id"]) >= 6]
        assert [q["id"] for q in new_subj] == [f"{dim}-A{n:02d}" for n in range(6, 12)], f"{dim} 主观续号不符"
        assert all(q["tier"] == "advanced" and 4 <= q["difficulty"] <= 5 for q in new_subj)
