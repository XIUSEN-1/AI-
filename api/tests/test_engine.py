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


def test_update_tracks_max_answered():
    assert DimensionState().max_answered == 0
    s2 = update(DimensionState(), difficulty=3.0, result=1.0)
    assert s2.max_answered == 3
    s3 = update(s2, difficulty=4.0, result=1.0)
    assert s3.max_answered == 4


def test_update_max_answered_keeps_highest():
    """回退到低难度作答时记录不下降：max_answered 是历史最高。"""
    s = update(DimensionState(), 4.0, 1.0)
    s2 = update(s, 2.0, 0.0)
    assert s2.max_answered == 4


def test_serialization_roundtrip_keeps_max_answered():
    s = update(DimensionState(), 4.0, 1.0)
    restored = DimensionState.from_dict(s.to_dict())
    assert restored == s
    assert restored.max_answered == 4


def test_from_dict_defaults_max_answered_zero():
    """M1 旧快照无 max_answered 字段，反序列化必须缺省 0。"""
    legacy = DimensionState.from_dict({"theta": 3.4, "n": 2, "streak": 2, "recent_deltas": [0.4, 0.26]})
    assert legacy.max_answered == 0


def test_stop_on_two_streak():
    assert should_stop(DimensionState(n=2, streak=2))
    assert should_stop(DimensionState(n=2, streak=-2))


def test_streak_stop_waits_for_ceiling():
    """连对但尚未触达题池最高难度时不停止，触达后才停止。"""
    below = DimensionState(n=2, streak=2, max_answered=3)
    assert not should_stop(below, ceiling=4)
    reached = DimensionState(n=2, streak=2, max_answered=4)
    assert should_stop(reached, ceiling=4)


def test_ceiling_none_keeps_m1_behavior():
    """ceiling=None（缺省）时连对停止规则与 M1 完全一致。"""
    assert should_stop(DimensionState(n=2, streak=2, max_answered=1), ceiling=None)
    assert should_stop(DimensionState(n=2, streak=2, max_answered=1))


def test_wrong_streak_stops_despite_ceiling():
    """连错停止不受触顶条件影响。"""
    assert should_stop(DimensionState(n=2, streak=-2, max_answered=1), ceiling=5)


def test_max_n_stop_unaffected_by_ceiling():
    assert should_stop(DimensionState(n=6, streak=1, max_answered=2), ceiling=5)


def test_convergence_stop_unaffected_by_ceiling():
    tiny = (0.01, 0.02, 0.01)
    assert should_stop(DimensionState(n=4, streak=0, recent_deltas=tiny, max_answered=2), ceiling=5)


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
