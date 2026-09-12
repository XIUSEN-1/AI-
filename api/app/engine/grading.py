"""客观题规则判分：单选精确匹配、判断布尔匹配、多选集合匹配。"""

from __future__ import annotations


def grade_objective(q_type: str, answer, correct) -> bool:
    if q_type == "single":
        return answer == correct
    if q_type == "judge":
        return bool(answer) == bool(correct)
    if q_type == "multi":
        if not isinstance(answer, list):
            return False
        return set(answer) == set(correct)
    raise ValueError(f"客观判分不支持题型: {q_type}")


def result_from_correct(is_correct: bool) -> float:
    return 1.0 if is_correct else 0.0
