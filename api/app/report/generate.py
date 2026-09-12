from __future__ import annotations

import json
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES, LEVEL_NAMES, dimension_level
from app.models import AssessmentSession, Question, Report, SessionAnswer

ADVICE_LOW = {
    "D1": "系统学习 AI 基础概念：推荐吴恩达《AI for Everyone》入门，重点掌握大模型的能力边界与幻觉成因。",
    "D2": "练习结构化提示词：从「角色+任务+约束+示例」四要素模板开始，每天用 AI 完成 1 个真实小任务并复盘指令质量。",
    "D3": "上手 2~3 款主流 AI 工具（对话助手、办公插件、数据分析），对比同一任务在不同工具中的表现，建立选型直觉。",
    "D4": "养成「先验证后采用」习惯：对 AI 给出的事实性内容交叉查证，每周挑 3 段 AI 输出做错误与幻觉标注练习。",
    "D5": "从拆解小任务开始练习人机协作：把一个复杂任务拆成 3~5 个子步骤，明确每步是人做还是 AI 做，并记录过程。",
    "D6": "学习 AI 伦理基础：了解个人信息保护要点与生成内容版权常识，使用 AI 产出时主动标注来源。",
}

ADVICE_HIGH = {
    "D1": "深入技术原理：了解微调、RAG、Agent 的技术脉络，关注主流评测基准（MMLU 等）的含义与局限。",
    "D2": "进阶提示工程：练习少样本示例设计、思维链诱导与上下文编排，对同一任务做多版本指令 A/B 对比。",
    "D3": "构建个人工具矩阵：按场景沉淀 AI 工具清单与工作流（数据分析、文档、代码），固化成可复用 SOP。",
    "D4": "系统性甄别训练：建立幻觉核查清单（来源、时效、自洽性），练习为 AI 输出撰写「改进指令」做迭代优化。",
    "D5": "挑战复杂项目协同：选一个跨环节真实项目全流程用 AI 协作完成，记录人机分工与迭代策略并复盘。",
    "D6": "成为负责任使用的示范者：在团队内推动 AI 使用规范（隐私脱敏、内容标注、合规审查），帮助他人识别风险。",
}

ADVICE_SYSTEM_PROMPT = (
    "你是 AI 能力测评的学习顾问。请根据学员六维能力数据，针对短板维度输出 1~3 条个性化中文学习建议，"
    "每条须包含：短板归因（结合分数与作答情况说明为什么弱）与可执行行动（具体的练习步骤、方法或资源）。"
    "你只输出一个 JSON 对象，不得包含任何其他文字或代码块标记，键固定为："
    "advice（1~3 条建议组成的字符串数组）。"
)


def _answer_items(
    db: OrmSession, session: AssessmentSession, judged: dict[str, dict] | None = None
) -> list[dict]:
    """逐题回显快照：session_answers 连 questions 取题型/题干/解析，按（维度, 作答序号）排序。
    judged 为 finish 判题结果（按 question_code 索引）：开放题/实操题补充 rationale，
    实操题另带过程/产物双通道分项。"""
    rows = db.execute(
        select(SessionAnswer, Question).join(Question, Question.id == SessionAnswer.question_id).where(
            SessionAnswer.session_id == session.id
        )
    ).all()
    items = []
    for a, q in rows:
        item = {
            "seq": a.seq,
            "dimension": a.dimension,
            "type": q.type,
            "stem_head": q.stem[:60],
            "is_correct": a.is_correct,
            "score": a.score,
            "explanation": q.explanation,
            "theta_after": round(a.theta_after, 3),
        }
        info = (judged or {}).get(a.question_code)
        if q.type in ("open", "practical"):
            item["rationale"] = info["rationale"] if info else "暂无判题理由"
            if q.type == "practical" and info:
                item["process_score"] = info.get("process_score")
                item["artifact_score"] = info.get("artifact_score")
        items.append(item)
    items.sort(key=lambda x: (x["dimension"], x["seq"]))
    return items


def _template_advice(dimensions: list[dict], gaps: list[str]) -> list[str]:
    by_dim = {x["dimension"]: x for x in dimensions}
    return [
        f"「{by_dim[d]['name']}」当前 {by_dim[d]['level_name']}："
        + (ADVICE_LOW[d] if by_dim[d]["level"] <= 2 else ADVICE_HIGH[d])
        for d in sorted(gaps)
    ]


def _advice_messages(dimensions: list[dict], gaps: list[str]) -> list[dict]:
    overview = "；".join(
        f"{x['name']}（{x['dimension']}）能力值 {x['theta']:.2f}、{x['level_name']}、答对 {x['correct']}/{x['answered']}"
        for x in dimensions
    )
    by_dim = {x["dimension"]: x for x in dimensions}
    gap_detail = "；".join(
        f"{by_dim[d]['name']}（{by_dim[d]['dimension']}）：能力值 {by_dim[d]['theta']:.2f}"
        f"（{by_dim[d]['level_name']}），答对 {by_dim[d]['correct']}/{by_dim[d]['answered']}"
        for d in sorted(gaps)
    )
    user = (
        f"【六维能力概览】{overview}\n"
        f"【短板维度明细】{gap_detail}\n"
        "请针对以上短板维度给出 1~3 条学习建议。"
    )
    return [
        {"role": "system", "content": ADVICE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _parse_advice(raw: str) -> list[str]:
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("advice"), list):
        raise ValueError("advice 缺失或不是列表")
    advice = [s.strip() for s in data["advice"] if isinstance(s, str) and s.strip()]
    if not advice:
        raise ValueError("advice 为空")
    return advice[:3]


def generate_llm_advice(
    dimensions: list[dict],
    gaps: list[str],
    chat_fn: Callable[..., str] | None = None,
) -> tuple[list[str], str]:
    """生成学习建议：chat_fn 可用且输出合法 → (LLM 建议, "llm")；
    chat_fn 为 None、调用抛错（如无 Key）或输出不可解析/为空 → (模板建议, "template")。
    """
    fallback = _template_advice(dimensions, gaps)
    if chat_fn is None:
        return fallback, "template"
    messages = _advice_messages(dimensions, gaps)  # try 外构造：dimensions 形状 bug 显形为 KeyError，不被回退边界吞掉
    try:
        raw = chat_fn(messages, model_role="judge", temperature=0.0, json_mode=True)
        return _parse_advice(raw), "llm"
    except Exception:  # 回退边界：任何 provider/解析失败都不得阻断报告生成
        return fallback, "template"


def build_report(
    db: OrmSession,
    session: AssessmentSession,
    chat_fn: Callable[..., str] | None = None,
    judged: dict[str, dict] | None = None,
) -> Report:
    answers = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == session.id)).all()
    dimensions = []
    for d in DIMENSIONS:
        rows = [a for a in answers if a.dimension == d]
        theta = float(session.theta_snapshot.get(d, {}).get("theta", 3.0))
        level = dimension_level(theta)
        dimensions.append(
            {
                "dimension": d,
                "name": DIMENSION_NAMES[d],
                "theta": round(theta, 2),
                "level": level,
                "level_name": LEVEL_NAMES[level],
                "answered": len(rows),
                "correct": sum(1 for a in rows if a.is_correct),
                "percent": round((theta - 1) / 4 * 100),
            }
        )
    total_theta = sum(x["theta"] for x in dimensions) / len(dimensions)
    total_level = dimension_level(total_theta)
    ranked = sorted(dimensions, key=lambda x: x["theta"], reverse=True)
    gaps = [x["dimension"] for x in ranked[-2:]]
    advice, advice_source = generate_llm_advice(dimensions, gaps, chat_fn)
    return Report(
        session_id=session.id,
        user_id=session.user_id,
        dimensions=dimensions,
        total_level=total_level,
        radar=[{"dimension": x["dimension"], "label": x["name"], "value": x["percent"]} for x in dimensions],
        strengths=[x["dimension"] for x in ranked[:2]],
        gaps=gaps,
        advice=advice,
        advice_source=advice_source,
        answers=_answer_items(db, session, judged),
    )
