"""判题管线测试：全 mock chat_fn，覆盖 校验/重试/降级/双跑一致性。

双跑在 judge_answer 内并行执行：两次调用并发弹 MockChat 预设响应，哪个跑拿到
哪条响应不确定 → 断言一律顺序无关（sorted(runs) / 全调用搜索），第三跑在双跑
完成后才发起（顺序确定）。
"""

import json
import threading
import time

from app.engine.adaptive import DimensionState, update
from app.llm.mock import MockChat
from app.judge.pipeline import JudgeResult, judge_answer, update_open_result


def make_q() -> dict:
    return {
        "id": "D2-A01",
        "stem": "题面",
        "rubric": {
            "points": ["要点A", "要点B", "要点C"],
            "anchors": {str(i): f"档{i}" for i in range(5)},
        },
    }


def good(score: int, **overrides) -> str:
    payload = {
        "score": score,
        "hits": ["要点A"],
        "strengths": ["s"],
        "gaps": ["g"],
        "rationale": "理由",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_consistent_double_run_returns_score():
    chat = MockChat([good(3)] * 2)
    r = judge_answer(make_q(), "作答", chat)
    assert isinstance(r, JudgeResult)
    assert r.score == 3
    assert not r.needs_review
    assert not r.degraded
    assert r.runs == [3, 3]
    assert r.hits == ["要点A"]
    assert r.rationale == "理由"


def test_near_scores_take_rounded_mean():
    # |Δ|=1 取均值四舍五入（half-up，规避银行家舍入：2.5→3、3.5→4）
    r1 = judge_answer(make_q(), "作答", MockChat([good(2), good(3)]))
    assert (r1.score, sorted(r1.runs)) == (3, [2, 3])
    r2 = judge_answer(make_q(), "作答", MockChat([good(3), good(4)]))
    assert (r2.score, sorted(r2.runs)) == (4, [3, 4])


def test_divergent_runs_third_median():
    # 1,4 分差>1 触发第三跑 3 → 取中位 3；三跑极差 3>1 → needs_review
    chat = MockChat([good(1), good(4), good(3)])
    r = judge_answer(make_q(), "作答", chat)
    assert r.score == 3
    assert sorted(r.runs) == [1, 3, 4]
    assert r.needs_review
    assert not r.degraded
    assert len(chat.calls) == 3


def test_needs_review_true_when_wide_divergence():
    chat = MockChat([good(0), good(4), good(4)])
    r = judge_answer(make_q(), "作答", chat)
    assert r.score == 4  # 中位
    assert sorted(r.runs) == [0, 4, 4]
    assert r.needs_review


def test_retry_on_bad_json_then_success():
    chat = MockChat(["这不是JSON", good(3), good(3)])
    r = judge_answer(make_q(), "作答", chat)
    assert r.score == 3
    assert r.runs == [3, 3]
    assert not r.degraded
    # 重试时附上次原始输出与错误说明（双跑并行 → 全调用搜索而非按下标）
    retries = [c for c in chat.calls if any("错误" in m["content"] for m in c["messages"])]
    assert len(retries) == 1
    assert any("这不是JSON" in m["content"] for m in retries[0]["messages"])


def test_degrade_after_retries_exhausted():
    # 双跑并行：两跑各做 初次+2 重试 = 6 次调用全部失败才降级
    chat = MockChat(["坏输出"] * 6)
    submission = "我的作答涵盖要点A与要点B，第三方面没有展开。"
    r = judge_answer(make_q(), submission, chat)
    assert r.degraded
    assert not r.needs_review
    assert r.runs == []
    assert r.hits == ["要点A", "要点B"]
    assert r.score == 3  # 2/3*4≈2.67 → 四舍五入
    assert "降级" in r.rationale
    assert len(chat.calls) == 6


def test_clip_out_of_range_score():
    r_high = judge_answer(make_q(), "作答", MockChat([good(7)] * 2))
    assert r_high.score == 4
    assert r_high.runs == [4, 4]
    r_low = judge_answer(make_q(), "作答", MockChat([good(-2)] * 2))
    assert r_low.score == 0


def test_learner_prompts_included_in_messages():
    chat = MockChat([good(3)] * 2)
    judge_answer(make_q(), "作答", chat, learner_prompts=["先分析再作答", "请用中文回答"])
    first = chat.calls[0]["messages"]
    text = "\n".join(m["content"] for m in first)
    assert "题面" in text  # 题干
    assert "要点A" in text  # rubric 要点
    assert "档3" in text  # 锚点
    assert "作答" in text  # 学员作答
    assert "先分析再作答" in text
    assert "请用中文回答" in text
    # 判题调用参数固定为低温 + JSON 模式
    assert chat.calls[0]["model_role"] == "judge"
    assert chat.calls[0]["temperature"] == 0.0
    assert chat.calls[0]["json_mode"] is True


def test_open_result_feeds_theta():
    state = update_open_result(DimensionState(), 4.0, 3)
    expected = update(DimensionState(), 4.0, 0.75)
    assert state.theta == expected.theta
    assert state.n == expected.n
    assert state.streak == expected.streak


# ---------- 双跑并行：墙钟减半 ----------


class SlowChat:
    """每次调用 sleep 0.3s 的 mock（计数加锁防双跑并发竞态）。"""

    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, messages, **kwargs) -> str:
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return good(3)


def test_parallel_double_run_beats_serial_wall_clock():
    # 双跑并行：墙钟 ≈ 单次 0.3s（串行需 0.6s+）；结果与串行一致
    chat = SlowChat()
    start = time.perf_counter()
    r = judge_answer(make_q(), "作答", chat)
    elapsed = time.perf_counter() - start
    assert r.score == 3 and r.runs == [3, 3] and chat.calls == 2
    assert elapsed < 0.5, f"双跑并行墙钟 {elapsed:.2f}s 未低于串行 0.6s"


def test_third_run_still_serial_after_divergence():
    # 回归：分差>1 时第三跑在双跑完成后串行追跑，取中位逻辑不变
    class DivergentSlowChat(SlowChat):
        def __call__(self, messages, **kwargs) -> str:
            with self._lock:
                n = self.calls
                self.calls += 1
            time.sleep(self.delay)
            return good(3 if n == 2 else (1 if n % 2 == 0 else 4))  # 前两跑 1/4，第三跑 3

    chat = DivergentSlowChat(delay=0.2)
    start = time.perf_counter()
    r = judge_answer(make_q(), "作答", chat)
    elapsed = time.perf_counter() - start
    assert r.score == 3 and sorted(r.runs) == [1, 3, 4] and r.needs_review
    # 双跑并行 0.2s + 第三跑串行 0.2s ≈ 0.4s（全串行需 0.6s+）
    assert elapsed < 0.55, f"双跑并行+第三串行墙钟 {elapsed:.2f}s 超预期"
    assert chat.calls == 3
