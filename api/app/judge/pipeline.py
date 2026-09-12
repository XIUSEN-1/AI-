"""LLM 判题管线：提示词构造 → JSON 校验/重试 → 关键词降级 → 双跑一致性。"""

from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.engine.adaptive import DimensionState, update
from app.models import ReviewQueue

ChatFn = Callable[..., str]

MAX_RETRIES = 2  # 单跑内解析失败的重试上限（初次 + 2 次重试）

SYSTEM_PROMPT = (
    "你是严谨的 AI 能力测评判题官。请依据评分要点与等级锚点对学员作答给出 0~4 的整数评分"
    "（0=未触及，1=初步，2=基本，3=良好，4=优秀）。"
    "你只输出一个 JSON 对象，不得包含任何其他文字或代码块标记，键固定为："
    'score（0~4 整数）、hits（命中的评分要点）、strengths（作答优点）、'
    "gaps（作答不足）、rationale（评分理由，简体中文一句话）。"
)


class JudgeResult(BaseModel):
    score: int = Field(ge=0, le=4)
    hits: list[str]
    strengths: list[str]
    gaps: list[str]
    rationale: str
    degraded: bool = False
    needs_review: bool = False
    runs: list[int] = Field(default_factory=list)  # 各次跑分


def _round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def _clip(value: float) -> int:
    return min(4, max(0, int(value)))


def _build_messages(
    question: dict, submission: str, learner_prompts: list[str] | None
) -> list[dict]:
    rubric = question.get("rubric") or {}
    points: list = rubric.get("points") or []
    anchors: dict = rubric.get("anchors") or {}
    parts = [f"【题目】{question.get('stem', '')}"]
    parts.append("【评分要点】" + ("；".join(points) if points else "无"))
    if anchors:
        ordered = sorted(anchors.items(), key=lambda kv: int(kv[0]))
        parts.append("【等级锚点】" + "；".join(f"{k}分：{v}" for k, v in ordered))
    parts.append(f"【学员作答】{submission}")
    if learner_prompts:
        parts.append("【学员此前与 AI 的对话提示】\n" + "\n".join(learner_prompts))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _parse_judge_output(raw: str) -> dict:
    """解析并校验判题输出，不合法抛 ValueError（score 越界由调用方裁剪）。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("输出不是 JSON 对象")
    score = data.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("score 缺失或不是数字")
    for key in ("hits", "strengths", "gaps"):
        if not isinstance(data.get(key), list) or not all(
            isinstance(x, str) for x in data[key]
        ):
            raise ValueError(f"{key} 缺失或不是字符串列表")
    if not isinstance(data.get("rationale"), str):
        raise ValueError("rationale 缺失或不是字符串")
    return data


def _run_once(chat_fn: ChatFn, messages: list[dict]) -> dict | None:
    """单跑：解析失败时附上次输出与错误重试（≤MAX_RETRIES 次），仍失败返回 None。"""
    current = list(messages)
    for _ in range(MAX_RETRIES + 1):
        raw = chat_fn(current, model_role="judge", temperature=0.0, json_mode=True)
        try:
            return _parse_judge_output(raw)
        except ValueError as exc:
            current = [
                *current,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"你上次的输出不是合法的判题结果（错误：{exc}）。"
                        "请重新只输出符合要求的 JSON 对象。"
                    ),
                },
            ]
    return None


def _keyword_fallback(question: dict, submission: str, reason: str) -> JudgeResult:
    """降级评分：按 rubric 要点关键词覆盖度折算 0~4 分。"""
    points: list = (question.get("rubric") or {}).get("points") or []
    hits = [p for p in points if p in submission]
    score = _clip(_round_half_up(len(hits) / len(points) * 4)) if points else 0
    return JudgeResult(
        score=score,
        hits=hits,
        strengths=[],
        gaps=[p for p in points if p not in hits],
        rationale=(
            f"LLM 判题输出无法解析（{reason}），已降级为关键词覆盖度评分："
            f"命中 {len(hits)}/{len(points)} 个要点。"
        ),
        degraded=True,
    )


def judge_answer(
    question: dict,
    submission: str,
    chat_fn: ChatFn,
    *,
    learner_prompts: list[str] | None = None,
) -> JudgeResult:
    """判题主链路：双跑并发（同 prompt、temperature=0，chat_fn 线程安全——
    provider 每调用自建 httpx.Client）→ |Δ|≤1 取均值四舍五入；
    |Δ|>1 双跑完成后串行追跑第三跑取中位，三跑极差仍 >1 则 needs_review；
    任一跑解析重试耗尽则降级关键词覆盖度（跑内重试仍串行 ≤MAX_RETRIES）。
    """
    messages = _build_messages(question, submission, learner_prompts)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run_once, chat_fn, messages) for _ in range(2)]
        run1, run2 = [f.result() for f in futures]
    if run1 is None:
        return _keyword_fallback(question, submission, "第 1 跑两次重试均失败")
    if run2 is None:
        return _keyword_fallback(question, submission, "第 2 跑两次重试均失败")

    scores = [_clip(run1["score"]), _clip(run2["score"])]
    if abs(scores[0] - scores[1]) <= 1:
        final = _round_half_up(sum(scores) / 2)
        best = _pick_run([run1, run2], scores, final)
        return _result(best, final, scores)

    run3 = _run_once(chat_fn, messages)
    if run3 is None:
        return _keyword_fallback(question, submission, "第 3 跑两次重试均失败")
    scores.append(_clip(run3["score"]))
    final = sorted(scores)[1]  # 三跑取中位
    best = _pick_run([run1, run2, run3], scores, final)
    result = _result(best, final, scores)
    result.needs_review = max(scores) - min(scores) > 1
    return result


def _pick_run(runs: list[dict], scores: list[int], final: int) -> dict:
    """定性字段取自跑分最接近最终分的跑（同距取更早的跑）。"""
    index = min(range(len(runs)), key=lambda i: (abs(scores[i] - final), i))
    return runs[index]


def _result(best: dict, final: int, scores: list[int]) -> JudgeResult:
    return JudgeResult(
        score=final,
        hits=best["hits"],
        strengths=best["strengths"],
        gaps=best["gaps"],
        rationale=best["rationale"],
        runs=scores,
    )


def update_open_result(theta_state: DimensionState, difficulty: float, score: int) -> DimensionState:
    """开放题得分（0~4）折算为掌握度（score/4）后更新能力估计。"""
    return update(theta_state, difficulty, score / 4)


def enqueue_review(
    db: Session,
    question_code: str,
    session_id: int,
    answer_id: int,
    judge_raw: dict,
    reason: str,
) -> ReviewQueue:
    """将需人工复核的判题结果入队；同一作答已有未处理条目时幂等返回原条目。"""
    existing = (
        db.query(ReviewQueue)
        .filter(ReviewQueue.answer_id == answer_id, ReviewQueue.status == "open")
        .one_or_none()
    )
    if existing is not None:
        return existing
    row = ReviewQueue(
        question_code=question_code,
        session_id=session_id,
        answer_id=answer_id,
        judge_raw=judge_raw,
        reason=reason,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
