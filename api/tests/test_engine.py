import pytest

from app.engine.adaptive import (
    DIMENSION_NAMES,
    DimensionState,
    dimension_level,
    expected,
    k_factor,
    next_difficulty,
    select_next_question,
    should_stop,
    update,
)


def test_six_dimensions_defined():
    assert set(DIMENSION_NAMES) == {"D1", "D2", "D3", "D4", "D5", "D6"}


def test_expected_midpoint():
    assert expected(3.0, 3.0) == pytest.approx(0.5)


def test_expected_monotonic_in_theta():
    assert expected(4.0, 3.0) > expected(3.0, 3.0) > expected(2.0, 3.0)


def test_k_factor_decays_with_n():
    assert k_factor(0) == pytest.approx(0.8)
    assert k_factor(4) < k_factor(0)


def test_update_correct_raises_theta():
    s2 = update(DimensionState(), difficulty=3.0, result=1.0)
    assert s2.theta == pytest.approx(3.4)
    assert s2.n == 1
    assert s2.streak == 1


def test_update_wrong_lowers_theta():
    s2 = update(DimensionState(), difficulty=3.0, result=0.0)
    assert s2.theta == pytest.approx(2.6)
    assert s2.streak == -1


def test_update_partial_result_moves_little():
    s2 = update(DimensionState(), difficulty=3.0, result=0.5)
    assert s2.theta == pytest.approx(3.0)


def test_update_slow_correct_discounted():
    fast = update(DimensionState(), 3.0, 1.0, slow=False)
    slow = update(DimensionState(), 3.0, 1.0, slow=True)
    assert slow.theta < fast.theta


def test_update_rejects_out_of_range_result():
    with pytest.raises(ValueError):
        update(DimensionState(), 3.0, 1.5)


def test_update_clamps_theta_to_range():
    low = update(DimensionState(theta=1.05), 1.0, 0.0)
    high = update(DimensionState(theta=4.95), 5.0, 1.0)
    assert 1.0 <= low.theta <= 5.0
    assert 1.0 <= high.theta <= 5.0


def test_update_is_immutable():
    s = DimensionState()
    update(s, 3.0, 1.0)
    assert s.n == 0 and s.theta == 3.0


def test_state_serialization_roundtrip():
    s = update(DimensionState(), 3.0, 1.0)
    assert DimensionState.from_dict(s.to_dict()) == s


def test_stop_on_two_streak():
    assert should_stop(DimensionState(n=2, streak=2))
    assert should_stop(DimensionState(n=2, streak=-2))


def test_stop_on_max_n():
    assert should_stop(DimensionState(n=6, streak=1))


def test_convergence_stop_requires_n4():
    tiny = (0.01, 0.02, 0.01)
    assert not should_stop(DimensionState(n=3, streak=0, recent_deltas=tiny))
    assert should_stop(DimensionState(n=4, streak=0, recent_deltas=tiny))


def test_no_stop_early():
    assert not should_stop(DimensionState(n=1, streak=1))


def test_next_difficulty_rounds_half_up():
    assert next_difficulty(DimensionState(theta=2.5)) == 3
    assert next_difficulty(DimensionState(theta=3.4)) == 3
    assert next_difficulty(DimensionState(theta=4.6)) == 5


def test_next_difficulty_clamped():
    assert next_difficulty(DimensionState(theta=1.0)) == 1
    assert next_difficulty(DimensionState(theta=5.0)) == 5


@pytest.mark.parametrize(
    "theta,level",
    [(1.0, 1), (1.79, 1), (1.8, 2), (2.5, 2), (2.6, 3), (3.3, 3), (3.4, 4), (4.1, 4), (4.2, 5), (5.0, 5)],
)
def test_dimension_level(theta, level):
    assert dimension_level(theta) == level


def test_select_prefers_exact_difficulty():
    qs = [
        {"code": "D1-a", "difficulty": 1},
        {"code": "D1-b", "difficulty": 3},
        {"code": "D1-c", "difficulty": 5},
    ]
    assert select_next_question(qs, set(), 3)["code"] == "D1-b"


def test_select_skips_asked():
    qs = [{"code": "D1-b", "difficulty": 3}]
    assert select_next_question(qs, {"D1-b"}, 3) is None


def test_select_nearest_then_lower():
    qs = [{"code": "D1-hi", "difficulty": 5}, {"code": "D1-lo", "difficulty": 2}]
    assert select_next_question(qs, set(), 3)["code"] == "D1-lo"
