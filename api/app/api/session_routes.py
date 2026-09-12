from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.auth import current_user
from app.db import SessionLocal
from app.engine import adaptive
from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES, DimensionState
from app.engine.grading import grade_objective, result_from_correct
from app.judge.pipeline import enqueue_review, judge_answer, update_open_result
from app.llm.provider import ProviderUnavailableError, chat_completion
from app.models import AssessmentSession, Question, Report, SessionAnswer, SessionMessage, utcnow
from app.report.generate import build_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

OBJECTIVE_TYPES = ("single", "multi", "judge")

# 对话题结束标记（dialog/finish-question 落库的考官结束语，同时作为切题依据）
DIALOG_CLOSING = "本题作答结束。"
# 学员主动跳过标记：与 DIALOG_CLOSING 同样使 _dialog_closed 判定闭题（复用切题/阶段翻转），
# finish 判题时据此区分"学员跳过"（score=None，不回灌 θ）与"学员未作答"（记 0 分）
DIALOG_SKIPPED = "学员跳过本题。"
PRACTICAL_SKIPPED = "学员跳过实操任务。"


class StartIn(BaseModel):
    mode: str = "full"


class AnswerIn(BaseModel):
    question_id: int
    answer: object
    time_spent: int = Field(ge=0, default=0)


def _states(snapshot: dict) -> dict[str, DimensionState]:
    return {d: DimensionState.from_dict(snapshot.get(d, {})) for d in DIMENSIONS}


def _snapshot(states: dict[str, DimensionState]) -> dict:
    return {d: s.to_dict() for d, s in states.items()}


def _question_out(q: Question) -> dict:
    return {
        "id": q.id,
        "code": q.code,
        "dimension": q.dimension,
        "dimension_name": DIMENSION_NAMES[q.dimension],
        "type": q.type,
        "difficulty": q.difficulty,
        "stem": q.stem,
        "options": q.options,
        "est_seconds": q.est_seconds,
        "tags": q.tags,
    }


def _pick_question(db: OrmSession, dimension: str, state: DimensionState, asked_codes: set[str]) -> Question | None:
    pool = db.scalars(
        select(Question).where(
            Question.dimension == dimension,
            Question.type.in_(OBJECTIVE_TYPES),
            Question.status == "published",
        )
    ).all()
    dicts = [{"code": q.code, "difficulty": q.difficulty} for q in pool]
    chosen = adaptive.select_next_question(dicts, asked_codes, adaptive.next_difficulty(state))
    if chosen is None:
        return None
    code = chosen["code"]
    return next(q for q in pool if q.code == code)


def _difficulty_ceilings(db: OrmSession) -> dict[str, int | None]:
    """每维度已发布客观题的最大难度：作为连对停止的难度触顶条件。"""
    rows = db.execute(
        select(Question.dimension, func.max(Question.difficulty)).where(
            Question.type.in_(OBJECTIVE_TYPES),
            Question.status == "published",
        ).group_by(Question.dimension)
    ).all()
    return {dimension: ceiling for dimension, ceiling in rows}


def _done_dimensions(states: dict[str, DimensionState], ceilings: dict[str, int | None]) -> set[str]:
    return {
        d for d in DIMENSIONS
        if states[d].n > 0 and adaptive.should_stop(states[d], ceilings.get(d))
    }


def _subjective_pool(
    db: OrmSession, dimension: str, qtype: str, asked_codes: set[str]
) -> list[Question]:
    """维度内未作答的已发布主观题，难度降序、同难度按 id 升序（确定性抽题）。"""
    return db.scalars(
        select(Question).where(
            Question.dimension == dimension,
            Question.type == qtype,
            Question.status == "published",
            Question.code.not_in(asked_codes),
        ).order_by(Question.difficulty.desc(), Question.id)
    ).all()


def _stage_plan(db: OrmSession, session: AssessmentSession) -> dict | None:
    """主观阶段抽题（spec §6.1）。客观六维未全部完成 → None。

    对话：六维按 θ 升序取第 3、4 位维度（θ 并列时按 D1..D6 稳定序）各 1 道未作答 open 题；
    实操：固定 D5 practical；D5 为对话抽中维度时顺延下一道 D5 practical，仍无则 D2 practical。
    """
    states = _states(session.theta_snapshot)
    ceilings = _difficulty_ceilings(db)
    if _done_dimensions(states, ceilings) != set(DIMENSIONS):
        return None
    asked = set(
        db.scalars(
            select(SessionAnswer.question_code).where(SessionAnswer.session_id == session.id)
        )
    )
    ordered = sorted(DIMENSIONS, key=lambda d: states[d].theta)
    dialog_dims = ordered[2:4]
    dialog = []
    for d in dialog_dims:
        pool = _subjective_pool(db, d, "open", asked)
        if pool:
            dialog.append(pool[0])
    d5_pool = _subjective_pool(db, "D5", "practical", asked)
    practical_candidates = d5_pool[1:] if "D5" in dialog_dims else d5_pool  # 冲突顺延下一道
    if not practical_candidates:
        practical_candidates = _subjective_pool(db, "D2", "practical", asked)[:1]  # 仍无则 D2
    return {"dialog": dialog, "practical": practical_candidates[0] if practical_candidates else None}


def _dialog_turns_taken(db: OrmSession, session_id: int, question_id: int) -> int:
    """当前对话题已进行的轮次：以学员发言条数计（每轮 = 学员发言 + 考官回复）。"""
    return len(
        db.scalars(
            select(SessionMessage.id).where(
                SessionMessage.session_id == session_id,
                SessionMessage.question_id == question_id,
                SessionMessage.channel == "dialog",
                SessionMessage.role == "learner",
            )
        ).all()
    )


def _dialog_closed(db: OrmSession, session_id: int, question: Question) -> bool:
    """对话题是否已结束：学员发言满 3 轮，或考官已落结束/跳过标记（学员提前完成/跳过）。"""
    if _dialog_turns_taken(db, session_id, question.id) >= 3:
        return True
    return (
        db.scalar(
            select(SessionMessage.id).where(
                SessionMessage.session_id == session_id,
                SessionMessage.question_id == question.id,
                SessionMessage.channel == "dialog",
                SessionMessage.role == "examiner",
                SessionMessage.content.in_((DIALOG_CLOSING, DIALOG_SKIPPED)),
            )
        )
        is not None
    )


def _next_seq(db: OrmSession, session_id: int) -> int:
    """SessionMessage.seq：会话内全局递增（跨 channel 保留完整时间线）。"""
    return (
        db.scalar(
            select(func.max(SessionMessage.seq)).where(SessionMessage.session_id == session_id)
        )
        or 0
    ) + 1


def _owned_session(db: OrmSession, session_id: int, user: dict) -> AssessmentSession:
    """对话/实操路由共用的会话装载：归属校验 + 进行中校验。"""
    session = db.get(AssessmentSession, session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.status == "judging":  # finish 判题占位期：尚未结束，但不可再推进流程
        raise HTTPException(status_code=409, detail="报告生成中，请稍候")
    if session.status != "in_progress":
        raise HTTPException(status_code=400, detail="会话已结束")
    return session


def _maybe_advance_stage(db: OrmSession, session: AssessmentSession, plan: dict | None) -> None:
    """阶段自动翻转（full 模式）：客观全完 → dialog/practical；对话题全部结束 → practical。"""
    if session.mode != "full":
        return
    if session.stage == "objective" and plan is not None and (plan["dialog"] or plan["practical"] is not None):
        session.stage = "dialog" if plan["dialog"] else "practical"  # quick 模式止于 objective
        db.add(session)
        db.commit()
    elif (
        session.stage == "dialog"
        and plan is not None
        and all(_dialog_closed(db, session.id, q) for q in plan["dialog"])
        and plan["practical"] is not None
    ):
        session.stage = "practical"
        db.add(session)
        db.commit()


def _session_view(db: OrmSession, session: AssessmentSession) -> dict:
    states = _states(session.theta_snapshot)
    answers = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == session.id)).all()
    asked = {a.question_code for a in answers}
    ceilings = _difficulty_ceilings(db)
    done = _done_dimensions(states, ceilings)

    plan = _stage_plan(db, session)
    _maybe_advance_stage(db, session, plan)  # 客观全完→主观；对话题全部结束→实操

    question = dimension = None
    reason = ""
    open_dialog = [q for q in (plan["dialog"] if plan else []) if not _dialog_closed(db, session.id, q)]
    if session.stage == "dialog" and open_dialog:
        q = open_dialog[0]
        dimension = q.dimension
        question = _question_out(q) | {"dialog_turns_taken": _dialog_turns_taken(db, session.id, q.id)}
        reason = (
            f"客观测评已完成。进入对话式测评：考官将围绕「{DIMENSION_NAMES[dimension]}」情境题"
            "结合你的回答逐步追问"
        )
    elif session.stage == "practical" and plan is not None and plan["practical"] is not None:
        q = plan["practical"]
        dimension = q.dimension
        question = _question_out(q)
        reason = f"对话式测评完成后，请在实操任务中与 AI 真实协作完成「{DIMENSION_NAMES[dimension]}」任务并提交产物"
    else:
        for d in DIMENSIONS:
            if d in done:
                continue
            picked = _pick_question(db, d, states[d], asked)
            if picked is not None:
                dimension = d
                question = _question_out(picked)
                break
        if question is not None:
            s = states[dimension]
            reason = (
                f"你在「{DIMENSION_NAMES[dimension]}」当前估计 {s.theta:.1f} 分"
                f"（{adaptive.LEVEL_NAMES[adaptive.dimension_level(s.theta)]}），"
                f"本题难度 {question['difficulty']}，用于校准你的水平边界"
            )
    progress = {
        d: {
            "name": DIMENSION_NAMES[d],
            "theta": round(states[d].theta, 3),
            "level": adaptive.dimension_level(states[d].theta),
            "level_name": adaptive.LEVEL_NAMES[adaptive.dimension_level(states[d].theta)],
            "n": states[d].n,
            "done": d in done,
        }
        for d in DIMENSIONS
    }
    return {
        "session_id": session.id,
        "status": session.status,
        "stage": session.stage,
        "question": question,
        "next_dimension": dimension,
        "reason": reason,
        "progress": progress,
    }


@router.post("")
def start_session(body: StartIn, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
    if body.mode not in ("full", "quick"):
        raise HTTPException(status_code=400, detail="mode 仅支持 full/quick")
    session = AssessmentSession(user_id=user["id"], mode=body.mode, theta_snapshot=_snapshot(_states({})))
    db.add(session)
    db.commit()
    db.refresh(session)
    return _session_view(db, session)


@router.post("/{session_id}/answer")
def submit_answer(
    session_id: int,
    body: AnswerIn,
    user: dict = Depends(current_user),
    db: OrmSession = Depends(get_db),
) -> dict:
    session = db.get(AssessmentSession, session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.status != "in_progress":
        raise HTTPException(status_code=400, detail="会话已结束")
    if session.stage != "objective":
        raise HTTPException(status_code=400, detail="当前阶段不支持客观题作答")
    question = db.get(Question, body.question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    answers = db.scalars(select(SessionAnswer).where(SessionAnswer.session_id == session.id)).all()
    asked = {a.question_code for a in answers}
    if question.code in asked:
        raise HTTPException(status_code=400, detail="该题已作答")
    if question.type not in OBJECTIVE_TYPES:
        raise HTTPException(status_code=400, detail="该题型不支持线上客观作答")
    if question.type == "judge" and not isinstance(body.answer, bool):
        raise HTTPException(status_code=400, detail="判断题答案必须为布尔值")

    is_correct = grade_objective(question.type, body.answer, question.answer)
    slow = is_correct and body.time_spent > 2 * question.est_seconds
    new_state = adaptive.update(
        _states(session.theta_snapshot)[question.dimension],
        question.difficulty,
        result_from_correct(is_correct),
        slow=slow,
    )
    states = _states(session.theta_snapshot)
    states[question.dimension] = new_state
    session.theta_snapshot = _snapshot(states)
    db.add(
        SessionAnswer(
            session_id=session.id,
            question_id=question.id,
            question_code=question.code,
            dimension=question.dimension,
            answer=body.answer,
            is_correct=is_correct,
            score=result_from_correct(is_correct),
            time_spent=body.time_spent,
            theta_after=new_state.theta,
            seq=len(answers) + 1,
        )
    )
    db.commit()
    db.refresh(session)
    return _session_view(db, session) | {
        "just": {
            "is_correct": is_correct,
            "explanation": question.explanation,
            "dimension": question.dimension,
            "theta": round(new_state.theta, 3),
        }
    }


# ---------- finish 统一判题（spec §6.4）----------

# 实操过程分固定 5 点量表：各 0-4 取均值，一次 LLM 调用输出 5 项 JSON
PROCESS_SYSTEM_PROMPT = (
    "你是严谨的 AI 能力测评判题官。学员在实操任务中与 AI 助手多轮协作，"
    "请依据学员的全部发言评估其协作过程的五个方面，各给 0~4 的整数评分"
    "（0=未体现，1=初步，2=基本，3=良好，4=优秀）：\n"
    "clarity 指令清晰：给 AI 的指令具体、明确、可直接执行；\n"
    "decomposition 任务拆解：把任务拆成合理的子步骤逐段推进；\n"
    "context 上下文给料：提供必要的背景材料、约束与示例；\n"
    "iteration 迭代甄别：对 AI 的输出追问、质疑、要求修正或核验；\n"
    "integration 结果整合：把多轮 AI 输出整合为连贯的最终成果。\n"
    "你只输出一个 JSON 对象，不得包含任何其他文字或代码块标记，"
    "键固定为：clarity、decomposition、context、iteration、integration（各为 0~4 整数）。"
)
PROCESS_KEYS = ("clarity", "decomposition", "context", "iteration", "integration")

UNANSWERED = "学员未作答"
SKIPPED = "学员跳过"


def _messages_contents(
    db: OrmSession, session_id: int, question_id: int, channel: str, role: str
) -> list[str]:
    """该题指定渠道/角色的留痕文本，按 seq 保序（判题取材：learner=过程材料，submit=产物）。"""
    rows = db.scalars(
        select(SessionMessage)
        .where(
            SessionMessage.session_id == session_id,
            SessionMessage.question_id == question_id,
            SessionMessage.channel == channel,
            SessionMessage.role == role,
        )
        .order_by(SessionMessage.seq)
    ).all()
    return [m.content for m in rows]


def _skip_marked(db: OrmSession, session_id: int, question_id: int, channel: str) -> bool:
    """该题是否落有学员跳过标记（对话=DIALOG_SKIPPED，实操=PRACTICAL_SKIPPED）。"""
    return (
        db.scalar(
            select(SessionMessage.id).where(
                SessionMessage.session_id == session_id,
                SessionMessage.question_id == question_id,
                SessionMessage.channel == channel,
                SessionMessage.role == "examiner",
                SessionMessage.content.in_((DIALOG_SKIPPED, PRACTICAL_SKIPPED)),
            )
        )
        is not None
    )


def _judge_question_dict(q: Question) -> dict:
    """judge_answer 所需的题目字段（判题 prompt 与关键词降级都要 rubric）。"""
    return {"code": q.code, "stem": q.stem, "rubric": q.rubric}


def _judge_process_score(chat_fn, prompts: list[str]) -> float | None:
    """实操过程分（0~4）：固定 5 项量表单次 LLM 调用，各项裁剪到 0~4 后取均值。
    provider 不可用（由 finish 的 provider 包装承接为空串）或输出不可解析 → None，
    由调用方按产物分折算。"""
    messages = [
        {"role": "system", "content": PROCESS_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "【学员在协作窗的全部发言（按时间顺序）】\n"
            + "\n".join(f"{i}. {p}" for i, p in enumerate(prompts, 1)),
        },
    ]
    raw = chat_fn(messages, model_role="judge", temperature=0.0, json_mode=True)
    try:
        data = json.loads(raw)
        scores = []
        for key in PROCESS_KEYS:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} 缺失或不是数字")
            scores.append(min(4, max(0, int(value))))
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return None
    return round(sum(scores) / len(scores), 2)


def _review_reason(kind: str, result) -> str | None:
    """按判题结果组装入队原因；无需复核返回 None。"""
    reasons = []
    if result.needs_review:
        reasons.append(f"{kind}判题三跑分差过大")
    if result.degraded:
        reasons.append(f"{kind}判题降级（关键词覆盖度）")
    return "；".join(reasons) or None


def _judging_tasks(db: OrmSession, session: AssessmentSession) -> list[dict]:
    """判题任务快照（纯数据）：判题材料在此一次读齐，后台线程不再依赖请求级 DB 会话。"""
    plan = _stage_plan(db, session)
    if plan is None:
        return []
    asked = set(
        db.scalars(
            select(SessionAnswer.question_code).where(SessionAnswer.session_id == session.id)
        )
    )
    tasks = []
    for q in (*plan["dialog"], plan["practical"]):
        if q is None or q.code in asked:
            continue
        channel = "dialog" if q.type == "open" else "practical"
        prompts = _messages_contents(db, session.id, q.id, channel, "learner")
        artifact_contents = (
            _messages_contents(db, session.id, q.id, "practical", "submit") if q.type == "practical" else []
        )
        artifact = artifact_contents[-1] if artifact_contents else None
        submission = artifact if artifact is not None else "\n".join(prompts)
        tasks.append(
            {
                "question": _judge_question_dict(q)
                | {"id": q.id, "dimension": q.dimension, "difficulty": q.difficulty, "type": q.type},
                "prompts": prompts,
                "submission": submission,
                "skipped": _skip_marked(db, session.id, q.id, channel)
                and (not prompts if q.type == "open" else artifact is None),
            }
        )
    return tasks


def _provider_chat(messages: list[dict], **kwargs) -> str:
    # 无 Key/上游异常时返回空串：judge_answer 解析失败走内置关键词降级，报告建议走模板回退
    try:
        return chat_completion(messages, **kwargs)  # model_role 等由调用方传入
    except ProviderUnavailableError:
        return ""


def _judge_task(task: dict) -> dict:
    """单题判题（线程池调用）：只做 LLM 调用与结果组装，不碰 DB。
    跳过/未作答短路（零 LLM 调用）；对话题整卷判分；实操双通道（产物 rubric + 过程量表）。"""
    q = task["question"]
    if task["skipped"]:
        return {"code": q["code"], "skipped": True}
    if not task["submission"]:
        return {"code": q["code"], "unanswered": True}
    if q["type"] == "open":
        result = judge_answer(q, task["submission"], _provider_chat, learner_prompts=task["prompts"])
        return {
            "code": q["code"],
            "score": float(result.score),
            "judge_raw": result.model_dump(),
            "reason": _review_reason("对话题", result),
            "echo": {"rationale": result.rationale},
        }
    artifact_result = judge_answer(q, task["submission"], _provider_chat)
    process = _judge_process_score(_provider_chat, task["prompts"]) if task["prompts"] else None
    process_degraded = False
    notes = []
    if process is None:  # 无过程材料或量表判分失败 → 过程分按产物分折算
        process = float(artifact_result.score)
        if task["prompts"]:
            process_degraded = True
            notes.append("过程判分不可用，过程分按产物分折算（降级）")
        else:
            notes.append("协作窗无学员发言，过程分按产物分折算")
    score = round(process * 0.6 + artifact_result.score * 0.4, 2)
    rationale = "；".join([*notes, artifact_result.rationale])
    judge_raw = {
        "type": "practical",
        "artifact": artifact_result.model_dump(),
        "process": {"score": process, "degraded": process_degraded},
        "score": score,
    }
    reason = _review_reason("实操产物", artifact_result)
    if process_degraded:
        reason = "；".join(filter(None, [reason, "实操过程判分降级（按产物分折算）"]))
    return {
        "code": q["code"],
        "score": score,
        "judge_raw": judge_raw,
        "reason": reason,
        "echo": {
            "rationale": rationale,
            "process_score": process,
            "artifact_score": artifact_result.score,
        },
    }


def _bump_judging_step(session_id: int) -> None:
    """进度计数：独立短连接递增 judging_step（前端轮询 status 拿 x/y 进度）。"""
    with SessionLocal() as db:
        session = db.get(AssessmentSession, session_id)
        if session is not None and session.status == "judging":
            session.judging_step += 1
            db.commit()


def _write_judged(session_id: int, tasks: list[dict], results: dict[str, dict]) -> None:
    """判题结果统一落库（判完后顺序执行，新开 DB 会话，避免 SQLite 跨线程写锁）：
    θ 回灌、SessionAnswer、复核队列、报告与 status=finished。"""
    with SessionLocal() as db:
        session = db.get(AssessmentSession, session_id)
        states = _states(session.theta_snapshot)
        judged: dict[str, dict] = {}
        for task in tasks:
            q = task["question"]
            res = results[q["code"]]
            seq = (
                db.scalar(
                    select(func.max(SessionAnswer.seq)).where(SessionAnswer.session_id == session_id)
                )
                or 0
            ) + 1
            if res.get("skipped") or res.get("unanswered"):
                # 跳过：score=None 注明"学员跳过"；未作答：score 记 0。均不判分不回灌 θ
                skipped = res.get("skipped")
                db.add(
                    SessionAnswer(
                        session_id=session_id,
                        question_id=q["id"],
                        question_code=q["code"],
                        dimension=q["dimension"],
                        answer="",
                        is_correct=None,
                        score=None if skipped else 0,
                        theta_after=states[q["dimension"]].theta,
                        seq=seq,
                    )
                )
                db.flush()  # 即时落库：后续题的 max(seq) 查询才能看到，避免重号
                judged[q["code"]] = {"rationale": SKIPPED if skipped else UNANSWERED}
                continue
            states[q["dimension"]] = update_open_result(states[q["dimension"]], q["difficulty"], res["score"])
            answer = SessionAnswer(
                session_id=session_id,
                question_id=q["id"],
                question_code=q["code"],
                dimension=q["dimension"],
                answer=task["submission"],
                is_correct=None,
                score=res["score"],
                theta_after=states[q["dimension"]].theta,
                seq=seq,
            )
            db.add(answer)
            db.flush()
            if res["reason"] is not None:
                enqueue_review(db, q["code"], session_id, answer.id, res["judge_raw"], res["reason"])
            judged[q["code"]] = res["echo"]
        session.theta_snapshot = _snapshot(states)
        report = build_report(db, session, chat_fn=_provider_chat, judged=judged)
        session.status = "finished"
        session.finished_at = utcnow()
        db.add(report)
        db.commit()


def _judge_all_parallel(session_id: int, tasks: list[dict], workers: int = 4) -> None:
    """后台判题主流程：并行 LLM 调用（结果内存收集）→ 判完统一写库。
    任一题非降级链异常 → 线程级回滚：status 回 in_progress、step 归零（可重试）。"""
    results: dict[str, dict] = {}
    fatal: BaseException | None = None
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_judge_task, t): t["question"]["code"] for t in tasks}
            for future in as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except BaseException as exc:  # noqa: BLE001 —— 首个致命异常留待循环外统一回滚
                    if fatal is None:
                        fatal = exc
                    continue
                _bump_judging_step(session_id)
        if fatal is not None:
            raise fatal
        _write_judged(session_id, tasks, results)
    except BaseException:
        logger.exception("后台判题异常，会话 %s 回滚为 in_progress", session_id)
        with SessionLocal() as db:
            session = db.get(AssessmentSession, session_id)
            if session is not None and session.status == "judging":
                session.status = "in_progress"
                session.judging_step = 0
                db.commit()


# 判题执行模式：生产为后台线程并行判题；测试注入 False 时请求内单 worker 顺序执行（调用顺序确定）
_ASYNC_JUDGING = True


@router.post("/{session_id}/finish", response_model=None)  # 返回 dict（幂等）或 202 JSONResponse，跳过响应模型推导
def finish_session(session_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict | JSONResponse:
    session = db.get(AssessmentSession, session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    existing = db.scalar(select(Report).where(Report.session_id == session.id))
    if existing is not None:
        return {"report_id": existing.id}  # 幂等早返回在占位置换之前：已完成会话不再走判题
    if session.status == "judging":
        # 判题占位中：上一个 finish 仍在跑（客户端超时重试/并发的典型撞车窗口）
        raise HTTPException(status_code=409, detail="报告生成中，请勿重复提交")
    if session.mode != "quick" and session.stage != "ready":
        # full 模式须完成对话式测评与实操（stage 到达实操提交后的 ready 态）方可生成报告
        raise HTTPException(status_code=400, detail="请先完成对话式测评与实操任务")

    # 材料快照先取齐（后台线程不依赖请求级会话）：占位后 judging 拦截一切流程推进，消息不可变
    tasks = _judging_tasks(db, session) if session.mode == "full" else []
    session.status = "judging"  # 防重入占位 + 进度基数，判题（含多次 LLM 调用）转后台并行
    session.judging_step = 0
    session.judging_total = len(tasks)
    db.commit()
    if _ASYNC_JUDGING:
        threading.Thread(
            target=_judge_all_parallel, args=(session.id, tasks), daemon=True, name=f"judge-{session.id}"
        ).start()
    else:
        _judge_all_parallel(session.id, tasks, workers=1)
    return JSONResponse(status_code=202, content={"session_id": session.id, "judging_total": len(tasks)})


@router.get("/{session_id}/status")
def session_status(
    session_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)
) -> dict:
    """轻量进度端点：finish 202 后前端轮询（判题 x/y → finished 自动出报告）。"""
    session = db.get(AssessmentSession, session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    out = {
        "status": session.status,
        "stage": session.stage,
        "judging_step": session.judging_step,
        "judging_total": session.judging_total,
    }
    if session.status == "finished":
        report = db.scalar(select(Report).where(Report.session_id == session.id))
        out["report_id"] = report.id if report is not None else None
    return out
