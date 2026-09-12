"""判题管线测试：全 mock chat_fn，覆盖 校验/重试/降级/双跑一致性。"""

import json

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
    assert (r1.score, r1.runs) == (3, [2, 3])
    r2 = judge_answer(make_q(), "作答", MockChat([good(3), good(4)]))
    assert (r2.score, r2.runs) == (4, [3, 4])


def test_divergent_runs_third_median():
    # 1,4 分差>1 触发第三跑 3 → 取中位 3；三跑极差 3>1 → needs_review
    chat = MockChat([good(1), good(4), good(3)])
    r = judge_answer(make_q(), "作答", chat)
    assert r.score == 3
    assert r.runs == [1, 4, 3]
    assert r.needs_review
    assert not r.degraded
    assert len(chat.calls) == 3


def test_needs_review_true_when_wide_divergence():
    chat = MockChat([good(0), good(4), good(4)])
    r = judge_answer(make_q(), "作答", chat)
    assert r.score == 4  # 中位
    assert r.runs == [0, 4, 4]
    assert r.needs_review


def test_retry_on_bad_json_then_success():
    chat = MockChat(["这不是JSON", good(3), good(3)])
    r = judge_answer(make_q(), "作答", chat)
    assert r.score == 3
    assert r.runs == [3, 3]
    assert not r.degraded
    # 重试时附上次原始输出与错误说明
    retry_messages = chat.calls[1]["messages"]
    assert any("这不是JSON" in m["content"] for m in retry_messages)
    assert any("错误" in m["content"] for m in retry_messages)


def test_degrade_after_retries_exhausted():
    chat = MockChat(["坏输出"] * 3)  # 第 1 跑：初次 + 2 次重试均失败
    submission = "我的作答涵盖要点A与要点B，第三方面没有展开。"
    r = judge_answer(make_q(), submission, chat)
    assert r.degraded
    assert not r.needs_review
    assert r.runs == []
    assert r.hits == ["要点A", "要点B"]
    assert r.score == 3  # 2/3*4≈2.67 → 四舍五入
    assert "降级" in r.rationale
    assert len(chat.calls) == 3  # 降级后不再进行第 2 跑


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
