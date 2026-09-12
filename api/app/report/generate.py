from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES, LEVEL_NAMES, dimension_level
from app.models import AssessmentSession, Report, SessionAnswer

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


def build_report(db: OrmSession, session: AssessmentSession) -> Report:
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
    by_dim = {x["dimension"]: x for x in dimensions}
    advice = [
        f"「{by_dim[d]['name']}」当前 {by_dim[d]['level_name']}："
        + (ADVICE_LOW[d] if by_dim[d]["level"] <= 2 else ADVICE_HIGH[d])
        for d in sorted(gaps)
    ]
    return Report(
        session_id=session.id,
        user_id=session.user_id,
        dimensions=dimensions,
        total_level=total_level,
        radar=[{"dimension": x["dimension"], "label": x["name"], "value": x["percent"]} for x in dimensions],
        strengths=[x["dimension"] for x in ranked[:2]],
        gaps=gaps,
        advice=advice,
    )
