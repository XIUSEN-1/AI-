"""自适应测评引擎：纯函数 + 不可变状态（copy-on-write），与 FastAPI 解耦。"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

DIMENSIONS = ("D1", "D2", "D3", "D4", "D5", "D6")

DIMENSION_NAMES = {
    "D1": "AI 基础认知",
    "D2": "提示词工程",
    "D3": "AI 工具使用",
    "D4": "AI 结果评估与优化",
    "D5": "人机协同解决问题",
    "D6": "AI 伦理与合规",
}

LEVEL_NAMES = {1: "初识 L1", 2: "会用 L2", 3: "熟练 L3", 4: "精通 L4", 5: "专家 L5"}

THETA_MIN, THETA_MAX = 1.0, 5.0
INITIAL_THETA = 3.0
STOP_STREAK = 2
STOP_MAX_N = 6
CONVERGENCE = 0.15


@dataclass(frozen=True)
class DimensionState:
    theta: float = INITIAL_THETA
    n: int = 0
    streak: int = 0  # 连对为正、连错为负
    recent_deltas: tuple[float, ...] = ()  # 最近 3 次更新量
    max_answered: int = 0  # 该维度已作答题目的最高难度

    def to_dict(self) -> dict:
        data = asdict(self)
        data["recent_deltas"] = list(self.recent_deltas)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DimensionState":
        return cls(
            theta=float(data.get("theta", INITIAL_THETA)),
            n=int(data.get("n", 0)),
            streak=int(data.get("streak", 0)),
            recent_deltas=tuple(data.get("recent_deltas", [])),
            max_answered=int(data.get("max_answered", 0)),
        )


def expected(theta: float, difficulty: float) -> float:
    """期望掌握度：θ 与题目难度同尺度（1~5）。"""
    return 1.0 / (1.0 + math.exp(-(theta - difficulty)))


def k_factor(n: int) -> float:
    """更新步长随该维度答题数衰减。"""
    return 0.8 / (1.0 + 0.25 * n)


def _next_streak(streak: int, result: float) -> int:
    if result >= 0.5:
        return streak + 1 if streak > 0 else 1
    return streak - 1 if streak < 0 else -1


def update(state: DimensionState, difficulty: float, result: float, slow: bool = False) -> DimensionState:
    """作答后返回新的能力估计。result∈[0,1]（客观题 0/1，开放题 score/4）。

    slow=True 表示答对但用时超过预估 2 倍，更新量×0.7。
    """
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"result 必须在 [0,1]，收到 {result}")
    delta = k_factor(state.n) * (result - expected(state.theta, difficulty))
    if slow and result >= 0.5:
        delta *= 0.7
    theta = min(THETA_MAX, max(THETA_MIN, state.theta + delta))
    recent = tuple([*state.recent_deltas, delta][-3:])
    return DimensionState(
        theta=theta,
        n=state.n + 1,
        streak=_next_streak(state.streak, result),
        recent_deltas=recent,
        max_answered=max(state.max_answered, int(difficulty)),
    )


def should_stop(state: DimensionState, ceiling: int | None = None) -> bool:
    """ceiling 为该维度客观题池的最高难度：连对停止须已触达上限（防止强学员被提前掐断）；
    ceiling=None（缺省）保持 M1 行为。连错、题数上限、收敛停止不受 ceiling 影响。
    """
    if state.streak >= STOP_STREAK:
        return ceiling is None or state.max_answered >= ceiling
    if state.streak <= -STOP_STREAK:
        return True
    if state.n >= STOP_MAX_N:
        return True
    converged = len(state.recent_deltas) >= 3 and all(abs(d) < CONVERGENCE for d in state.recent_deltas)
    return converged and state.n >= 4


def next_difficulty(state: DimensionState) -> int:
    """下一题难度：θ 四舍五入（half-up，规避 Python 银行家舍入）。"""
    return min(5, max(1, math.floor(state.theta + 0.5)))


def dimension_level(theta: float) -> int:
    if theta < 1.8:
        return 1
    if theta < 2.6:
        return 2
    if theta < 3.4:
        return 3
    if theta < 4.2:
        return 4
    return 5


def select_next_question(questions: list[dict], asked_codes: set[str], target_difficulty: int) -> dict | None:
    """同维度未作答题中选目标难度题；无精确匹配取最近难度，同距取更低难度。"""
    candidates = [q for q in questions if q["code"] not in asked_codes]
    if not candidates:
        return None
    return min(candidates, key=lambda q: (abs(q["difficulty"] - target_difficulty), q["difficulty"]))
