import pytest

from app.engine.grading import grade_objective, result_from_correct


def test_single_exact_match():
    assert grade_objective("single", "B", "B")
    assert not grade_objective("single", "A", "B")


def test_multi_set_equality_ignores_order():
    assert grade_objective("multi", ["C", "A"], ["A", "C"])
    assert not grade_objective("multi", ["A"], ["A", "C"])


def test_multi_rejects_non_list():
    assert not grade_objective("multi", "A", ["A"])


def test_judge_boolean():
    assert grade_objective("judge", True, True)
    assert not grade_objective("judge", True, False)


def test_unsupported_type_raises():
    with pytest.raises(ValueError):
        grade_objective("open", "x", "x")


def test_result_from_correct():
    assert result_from_correct(True) == 1.0
    assert result_from_correct(False) == 0.0
