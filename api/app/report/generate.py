from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES, LEVEL_NAMES, dimension_level
from app.models import AssessmentSession, Question, Report, SessionAnswer

logger = logging.getLogger(__name__)

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
    "若提供了「分级建议素材」，必须以其为基础个性化组装：直接引用素材中的真实资源名称与练习任务，"
    "并结合学员的具体分数与作答情况调整表述与侧重；不得编造素材之外的资源、链接或不存在的课程。"
    "你只输出一个 JSON 对象，不得包含任何其他文字或代码块标记，键固定为："
    "advice（1~3 条建议组成的字符串数组）。"
)

# 分级建议库：seeds/advice_matrix.json（6 维 × L1~L5 共 30 格），懒加载缓存；加载失败缓存空表（建议回退旧模板）
_ADVICE_MATRIX_PATH = Path(__file__).resolve().parents[3] / "seeds" / "advice_matrix.json"
_ADVICE_MATRIX: dict | None = None


def _valid_cell(cell: object) -> bool:
    """格子的最低可用校验：summary/promotion 非空，resources 2~4 项且字段齐全，exercises 2~3 项非空。"""
    if not isinstance(cell, dict):
        return False
    resources, exercises = cell.get("resources"), cell.get("exercises")
    if not isinstance(resources, list) or not 2 <= len(resources) <= 4:
        return False
    if not all(
        isinstance(r, dict)
        and all(isinstance(r.get(k), str) and r.get(k).strip() for k in ("title", "type", "note"))
        for r in resources
    ):
        return False
    if not isinstance(exercises, list) or not 2 <= len(exercises) <= 3:
        return False
    if not all(isinstance(e, str) and e.strip() for e in exercises):
        return False
    return (
        isinstance(cell.get("summary"), str)
        and bool(cell["summary"].strip())
        and isinstance(cell.get("promotion"), str)
        and bool(cell["promotion"].strip())
    )


def _load_advice_matrix() -> dict:
    """加载分级建议库。文件缺失/解析失败/格式非法均回退空表（不 crash），建议走旧模板。"""
    global _ADVICE_MATRIX
    if _ADVICE_MATRIX is None:
        matrix: dict = {}
        try:
            raw = json.loads(_ADVICE_MATRIX_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("advice_matrix.json 顶层必须是对象")
            for dim, levels in raw.items():
                if not isinstance(levels, dict):
                    continue
                matrix[dim] = {lvl: cell for lvl, cell in levels.items() if _valid_cell(cell)}
        except (OSError, ValueError):
            logger.warning("分级建议库加载失败，学习建议回退内置模板", exc_info=True)
            matrix = {}
        _ADVICE_MATRIX = matrix
    return _ADVICE_MATRIX


def _advice_cell(dim: str, level: int) -> dict | None:
    """命中维度×等级格子；库缺失、维度非法或等级越界返回 None。"""
    cell = _load_advice_matrix().get(dim, {}).get(f"L{level}")
    return cell if isinstance(cell, dict) else None


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


def _render_cell(x: dict, cell: dict) -> str:
    """格子直渲染为一条建议文案（summary+资源列表+练习任务+晋级标准）。"""
    resources = "；".join(f"{r['title']}（{r['type']}）——{r['note']}" for r in cell["resources"])
    exercises = "".join(f"{i}. {e} " for i, e in enumerate(cell["exercises"], 1))
    return (
        f"「{x['name']}」当前 {x['level_name']}：{cell['summary']}\n"
        f"推荐资源：{resources}\n"
        f"练习任务：{exercises.rstrip()}\n"
        f"晋级标准：{cell['promotion']}"
    )


def _fallback_advice(dimensions: list[dict], gaps: list[str]) -> tuple[list[str], str, list[dict]]:
    """无 LLM/LLM 失败时的建议：优先分级建议库格子直渲染（source="cell"，明细带结构化格子）；
    库缺失/格子未命中回退旧模板（source="template"）。"""
    by_dim = {x["dimension"]: x for x in dimensions}
    texts: list[str] = []
    detail: list[dict] = []
    for d in sorted(gaps):
        x = by_dim[d]
        cell = _advice_cell(d, x["level"])
        if cell is not None:
            texts.append(_render_cell(x, cell))
            detail.append(
                {"dimension": d, "level": x["level"], "text": texts[-1], "source_type": "cell", "cell": cell}
            )
        else:  # 旧模板兜底（建议库加载失败的路径）
            text = (
                f"「{x['name']}」当前 {x['level_name']}："
                + (ADVICE_LOW[d] if x["level"] <= 2 else ADVICE_HIGH[d])
            )
            texts.append(text)
            detail.append({"text": text, "source_type": "template"})
    source = "cell" if any(e["source_type"] == "cell" for e in detail) else "template"
    return texts, source, detail


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
    sections = []
    for d in sorted(gaps):
        cell = _advice_cell(d, by_dim[d]["level"])
        if cell is None:
            continue  # 库缺失/未命中：LLM 仅依据分数作答数据给建议
        resources = "；".join(f"{r['title']}（{r['type']}）——{r['note']}" for r in cell["resources"])
        exercises = "；".join(cell["exercises"])
        sections.append(
            f"「{by_dim[d]['name']}」（{d} · {by_dim[d]['level_name']}）\n"
            f"格子摘要：{cell['summary']}\n推荐资源：{resources}\n练习任务：{exercises}\n晋级标准：{cell['promotion']}"
        )
    user = f"【六维能力概览】{overview}\n【短板维度明细】{gap_detail}\n"
    if sections:
        user += "【短板维度分级建议素材】\n" + "\n\n".join(sections) + "\n"
    user += "请针对以上短板维度给出 1~3 条学习建议。"
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
) -> tuple[list[str], str, list[dict]]:
    """生成学习建议，返回 (建议文案, 来源, 逐条明细)。明细条目：{text, source_type, dimension?, level?, cell?}。
    chat_fn 可用且输出合法 → (LLM 建议, "llm")；
    chat_fn 为 None、调用抛错（如无 Key）或输出不可解析/为空 → 分级建议库格子直渲染（"cell"）；
    建议库缺失/格子未命中 → 旧模板（"template"）。"""
    fallback = _fallback_advice(dimensions, gaps)
    if chat_fn is None:
        return fallback
    messages = _advice_messages(dimensions, gaps)  # try 外构造：dimensions 形状 bug 显形为 KeyError，不被回退边界吞掉
    try:
        # 建议走 chat 角色（flash 非思考型，~5-10s）：判题主链路保持 judge（v4-pro 评分一致性）
        raw = chat_fn(messages, model_role="chat", temperature=0.0, json_mode=True)
        advice = _parse_advice(raw)
        return advice, "llm", [{"text": s, "source_type": "llm"} for s in advice]
    except Exception:  # 回退边界：任何 provider/解析失败都不得阻断报告生成
        return fallback


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
    advice, advice_source, advice_detail = generate_llm_advice(dimensions, gaps, chat_fn)
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
        advice_detail=advice_detail,
        answers=_answer_items(db, session, judged),
    )
