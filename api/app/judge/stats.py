"""校准统计纯函数：Pearson/Spearman 相关系数与二次加权 Kappa（无第三方依赖）。"""

from __future__ import annotations

import math


def _check_pairs(x: list[float], y: list[float]) -> None:
    if len(x) != len(y):
        raise ValueError(f"两组数据长度不一致：{len(x)} vs {len(y)}")
    if not x:
        raise ValueError("数据为空")


def pearson(x: list[float], y: list[float]) -> float:
    """皮尔逊相关系数；任一方零方差（无定义）时约定返回 0.0。"""
    _check_pairs(x, y)
    n = len(x)
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    var_y = sum((yi - mean_y) ** 2 for yi in y)
    denom = math.sqrt(var_x) * math.sqrt(var_y)
    if denom == 0:
        return 0.0
    return cov / denom


def _average_ranks(values: list[float]) -> list[float]:
    """平均秩：相同值取秩的算术平均（如 [1, 1, 3] → [1.5, 1.5, 3.0]）。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1  # 1 起始秩，同值取平均
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float:
    """斯皮尔曼等级相关：对两组数据取平均秩后求皮尔逊相关。"""
    _check_pairs(x, y)
    return pearson(_average_ranks(x), _average_ranks(y))


def kappa_quadratic(a: list[int], b: list[int]) -> float:
    """二次加权 Kappa（Cohen's kappa with quadratic weights，评分者 a × 评分者 b）。

    k = max(a)∪max(b) + 1 为档位数；完全一致且分母退化（双方同评一档）时约定返回 1.0。
    """
    _check_pairs(a, b)
    k = max(max(a), max(b)) + 1

    def index(v: int) -> int:
        if not 0 <= v < k:
            raise ValueError(f"评分 {v} 超出 0~{k - 1} 范围")
        return v

    observed = [[0] * k for _ in range(k)]
    for ai, bi in zip(a, b):
        observed[index(ai)][index(bi)] += 1

    n = len(a)
    row_marg = [sum(observed[i]) for i in range(k)]
    col_marg = [sum(observed[i][j] for i in range(k)) for j in range(k)]

    def weighted_sum(matrix: list[list[float]]) -> float:
        return sum(
            ((i - j) ** 2) / ((k - 1) ** 2 if k > 1 else 1) * matrix[i][j]
            for i in range(k)
            for j in range(k)
        )

    expected = [[row_marg[i] * col_marg[j] / n for j in range(k)] for i in range(k)]
    denom = weighted_sum(expected)
    if denom == 0:  # 双方所有样本都给了同一档：视为完全一致
        return 1.0
    return 1.0 - weighted_sum(observed) / denom
