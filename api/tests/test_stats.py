"""校准统计纯函数：用构造数据验证 kappa/pearson/spearman 的数学正确性。"""

import math

import pytest

from app.judge.stats import kappa_quadratic, pearson, spearman


class TestPearson:
    def test_perfect_positive(self):
        assert pearson([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == pytest.approx(1.0)

    def test_perfect_negative(self):
        assert pearson([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)

    def test_known_value(self):
        # 手算：cov = 1/3，std_x = std_y = sqrt(2/3) → r = 0.5
        assert pearson([1, 2, 3], [1, 3, 2]) == pytest.approx(0.5)

    def test_zero_variance_returns_zero(self):
        # 一方恒定 → 相关系数无定义，约定返回 0.0（不抛异常，便于脚本聚合）
        assert pearson([2, 2, 2], [1, 2, 3]) == 0.0

    def test_mismatched_length_raises(self):
        with pytest.raises(ValueError):
            pearson([1, 2], [1, 2, 3])


class TestSpearman:
    def test_perfect_positive(self):
        assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_perfect_negative(self):
        assert spearman([1, 2, 3], [9, 5, 1]) == pytest.approx(-1.0)

    def test_ties_use_average_ranks(self):
        # 秩：x=[1.5,1.5,3]，y=[1,2,3] → cov=0.5，var_x=0.5，var_y=2/3 → r=sqrt(3)/2
        assert spearman([1, 1, 2], [1, 2, 3]) == pytest.approx(math.sqrt(3) / 2)

    def test_non_monotonic_known_value(self):
        # x 秩 [1,3,2]（即数据本身），y=[1,2,3] 秩 [1,2,3] → r = 0.5
        assert spearman([1, 3, 2], [10, 20, 30]) == pytest.approx(0.5)


class TestKappaQuadratic:
    def test_perfect_agreement(self):
        assert kappa_quadratic([0, 1, 2, 3, 4], [0, 1, 2, 3, 4]) == pytest.approx(1.0)

    def test_perfect_agreement_with_bias(self):
        # 完全一致但边际分布不均，仍是 1
        assert kappa_quadratic([0, 0, 1, 1, 4], [0, 0, 1, 1, 4]) == pytest.approx(1.0)

    def test_known_small_example(self):
        # 2×2 逐格手算：a=[0,0,1,1], b=[0,1,1,1]
        # O=[[1,1],[0,2]]，row=[2,2]，col=[1,3]，E=[[0.5,1.5],[0.5,1.5]]，w=[[0,1],[1,0]]
        # ΣwO = 1，ΣwE = 2 → kappa = 1 - 1/2 = 0.5
        assert kappa_quadratic([0, 0, 1, 1], [0, 1, 1, 1]) == pytest.approx(0.5)

    def test_chance_level_example(self):
        # 2×2 均匀混淆：O=[[1,1],[1,1]]，E 全 1 → ΣwO = ΣwE = 2 → kappa = 0（不优于随机）
        assert kappa_quadratic([0, 0, 1, 1], [0, 1, 0, 1]) == pytest.approx(0.0)

    def test_all_identical_constant_returns_one(self):
        # 两个评分者都全给同一档：完全一致（分母退化约定为 1.0）
        assert kappa_quadratic([2, 2, 2], [2, 2, 2]) == pytest.approx(1.0)

    def test_directional_disagreement_penalized_more(self):
        # 固定均匀边际（k=3，行列和均为 2，E 全 2/3，ΣwE = 2），Kappa 只随 ΣwO 变化：
        # 对换偏 1 档：O[0][1]=O[1][0]=O[2][2]=2，ΣwO = 2/4+2/4+0 = 1 → kappa = 0.5
        off1 = kappa_quadratic([0, 1, 2, 0, 1, 2], [1, 0, 2, 1, 0, 2])
        # 对换偏 2 档：O[0][2]=O[1][1]=O[2][0]=2，ΣwO = 2+0+2 = 4 → kappa = -1
        off2 = kappa_quadratic([0, 1, 2, 0, 1, 2], [2, 1, 0, 2, 1, 0])
        assert off1 == pytest.approx(0.5)
        assert off2 == pytest.approx(-1.0)
        assert off1 > off2

    def test_mismatched_length_raises(self):
        with pytest.raises(ValueError):
            kappa_quadratic([1, 2], [1])

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            kappa_quadratic([], [])
