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
    assert len(codes) == len(set(codes)) == 300 + 50 * len(BATCH2_DIMS) + 67 * len(BATCH3_DIMS)
    for q in items:
        validate_question(q)


def _id_num(qid: str) -> int:
    """D1-B11 -> 11；D1-H05 -> 5；D1-A06 -> 6。"""
    return int(qid.split("-")[1][1:])


BATCH2_DIMS = ["D1", "D2", "D3", "D4", "D5", "D6"]  # 批次 2 按任务逐维扩展：Task1 D1+D2 → Task2 +D3+D4 → Task3 +D5+D6
BATCH3_DIMS = ["D1", "D2"]  # 批次 3 按任务逐维扩展：Task1 D1+D2 → Task2 +D3+D4 → Task3 +D5+D6


def test_bank_batch1_quota():
    """批次 1：六维各 +31（客观 25 + 主观 6），id 续号与难度金字塔精确。"""
    items = load_seed_files(SEEDS)
    for dim in ["D1", "D2", "D3", "D4", "D5", "D6"]:
        qs = [q for q in items if q["dimension"] == dim]
        basic1 = [q for q in qs if q["tier"] == "basic" and _id_num(q["id"]) <= 27]
        adv_obj1 = [
            q for q in qs
            if q["tier"] == "advanced" and q["type"] in ("single", "multi", "judge") and _id_num(q["id"]) <= 12
        ]
        subj1 = [q for q in qs if q["type"] in ("open", "practical") and _id_num(q["id"]) <= 11]
        assert len(basic1) == 27, f"{dim} 批次1基础客观应为 27"
        assert len(adv_obj1) == 12, f"{dim} 批次1高难客观应为 12"
        assert sum(q["type"] == "open" for q in subj1) == 8, f"{dim} 批次1 open 应为 8"
        assert sum(q["type"] == "practical" for q in subj1) == 3, f"{dim} 批次1 practical 应为 3"
        # 批次 1 新增客观题（B11..B27 / H05..H12）难度金字塔：d1×5 d2×6 d3×6 d4×5 d5×3
        new_obj = [
            q for q in adv_obj1 if _id_num(q["id"]) >= 5
        ] + [q for q in basic1 if _id_num(q["id"]) >= 11]
        assert len(new_obj) == 25, f"{dim} 批次1新增客观应为 25"
        by_diff = {}
        for q in new_obj:
            by_diff[q["difficulty"]] = by_diff.get(q["difficulty"], 0) + 1
        assert by_diff == {1: 5, 2: 6, 3: 6, 4: 5, 5: 3}, f"{dim} 批次1难度金字塔不符：{by_diff}"
        # 批次 1 新增主观题：A06..A09 open + A10..A11 practical，advanced 且难度 4..5
        new_subj = [q for q in subj1 if _id_num(q["id"]) >= 6]
        assert [q["id"] for q in new_subj] == [f"{dim}-A{n:02d}" for n in range(6, 12)], f"{dim} 主观续号不符"
        assert all(q["tier"] == "advanced" and 4 <= q["difficulty"] <= 5 for q in new_subj)


def test_bank_batch2_quota():
    """批次 2：已写入维度每维 +50（客观 40：d1×8/d2×10/d3×10/d4×8/d5×4 + 主观 10：7 open + 3 practical），
    id 续号 B56..B83 / H25..H36 / A22..A31（区间收界，防批次 3 溢入），难度金字塔精确。"""
    items = load_seed_files(SEEDS)
    assert len(items) == 300 + 50 * len(BATCH2_DIMS) + 67 * len(BATCH3_DIMS)
    for dim in BATCH2_DIMS:
        qs = [q for q in items if q["dimension"] == dim]
        if dim not in BATCH3_DIMS:
            assert len(qs) == 100, f"{dim} 应为 100 题"
        basic = [q for q in qs if q["tier"] == "basic"]
        adv_obj = [q for q in qs if q["tier"] == "advanced" and q["type"] in ("single", "multi", "judge")]
        new_basic = [q for q in basic if 56 <= _id_num(q["id"]) <= 83]
        new_adv_obj = [q for q in adv_obj if 25 <= _id_num(q["id"]) <= 36]
        assert [q["id"] for q in new_basic] == [f"{dim}-B{n:02d}" for n in range(56, 84)], f"{dim} 批次2基础续号不符"
        assert [q["id"] for q in new_adv_obj] == [f"{dim}-H{n:02d}" for n in range(25, 37)], f"{dim} 批次2高难续号不符"
        assert len(new_basic) == 28 and len(new_adv_obj) == 12
        # 难度金字塔：d1×8 d2×10 d3×10 d4×8 d5×4
        by_diff = {}
        for q in new_basic + new_adv_obj:
            by_diff[q["difficulty"]] = by_diff.get(q["difficulty"], 0) + 1
        assert by_diff == {1: 8, 2: 10, 3: 10, 4: 8, 5: 4}, f"{dim} 批次2难度金字塔不符：{by_diff}"
        # 主观题：A22..A28 open（7）+ A29..A31 practical（3），advanced 且难度 4..5
        new_subj = [q for q in qs if q["type"] in ("open", "practical") and 22 <= _id_num(q["id"]) <= 31]
        assert [q["id"] for q in new_subj] == [f"{dim}-A{n:02d}" for n in range(22, 32)], f"{dim} 批次2主观续号不符"
        assert sum(q["type"] == "open" for q in new_subj) == 7, f"{dim} open 应为 7"
        assert sum(q["type"] == "practical" for q in new_subj) == 3, f"{dim} practical 应为 3"
        assert all(q["tier"] == "advanced" and 4 <= q["difficulty"] <= 5 for q in new_subj)


def test_bank_batch3_quota():
    """批次 3：已写入维度每维 +67（客观 53：d1×10/d2×13/d3×13/d4×11/d5×6 + 主观 14：10 open + 4 practical），
    id 续号 B84..B119 / H37..H53 / A32..A45，难度金字塔精确。"""
    items = load_seed_files(SEEDS)
    assert len(items) == 300 + 50 * len(BATCH2_DIMS) + 67 * len(BATCH3_DIMS)
    for dim in BATCH3_DIMS:
        qs = [q for q in items if q["dimension"] == dim]
        assert len(qs) == 100 + 67, f"{dim} 批次3后应为 167 题"
        basic = [q for q in qs if q["tier"] == "basic"]
        adv_obj = [q for q in qs if q["tier"] == "advanced" and q["type"] in ("single", "multi", "judge")]
        new_basic = [q for q in basic if _id_num(q["id"]) >= 84]
        new_adv_obj = [q for q in adv_obj if _id_num(q["id"]) >= 37]
        assert [q["id"] for q in new_basic] == [f"{dim}-B{n}" for n in range(84, 120)], f"{dim} 批次3基础续号不符"
        assert [q["id"] for q in new_adv_obj] == [f"{dim}-H{n}" for n in range(37, 54)], f"{dim} 批次3高难续号不符"
        assert len(new_basic) == 36 and len(new_adv_obj) == 17
        # 难度金字塔：d1×10 d2×13 d3×13 d4×11 d5×6
        by_diff = {}
        for q in new_basic + new_adv_obj:
            by_diff[q["difficulty"]] = by_diff.get(q["difficulty"], 0) + 1
        assert by_diff == {1: 10, 2: 13, 3: 13, 4: 11, 5: 6}, f"{dim} 批次3难度金字塔不符：{by_diff}"
        # 主观题：A32..A41 open（10）+ A42..A45 practical（4），advanced 且难度 4..5
        new_subj = [q for q in qs if q["type"] in ("open", "practical") and _id_num(q["id"]) >= 32]
        assert [q["id"] for q in new_subj] == [f"{dim}-A{n}" for n in range(32, 46)], f"{dim} 批次3主观续号不符"
        assert sum(q["type"] == "open" for q in new_subj) == 10, f"{dim} open 应为 10"
        assert sum(q["type"] == "practical" for q in new_subj) == 4, f"{dim} practical 应为 4"
        assert all(q["tier"] == "advanced" and 4 <= q["difficulty"] <= 5 for q in new_subj)
