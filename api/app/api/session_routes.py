from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.auth import current_user
from app.engine import adaptive
from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES, DimensionState
from app.engine.grading import grade_objective, result_from_correct
from app.judge.pipeline import enqueue_review, judge_answer, update_open_result
from app.llm.provider import ProviderUnavailableError, chat_completion
from app.models import AssessmentSession, Question, Report, SessionAnswer, SessionMessage, utcnow
from app.report.generate import build_report

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


def _judge_subjective(db: OrmSession, session: AssessmentSession, chat_fn) -> dict[str, dict]:
    """full 会话 ready 态统一判题：两道对话题整卷判分（learner 消息拼接为 submission）、
    实操双通道（过程量表均值×0.6 + 产物 rubric 分×0.4），逐题 update_open_result 回灌 θ
    （对话题→所属维度、实操→D5），degraded/needs_review 结果入复核队列。
    返回 question_code → 报告回显补充信息（rationale、实操双通道分项）。
    学员零有效发言的题不判分、不回灌 θ：有跳过标记记 score=None 并注明"学员跳过"，
    否则 score 记 0 并注明未作答。"""
    plan = _stage_plan(db, session)
    if plan is None:
        return {}
    asked = set(
        db.scalars(
            select(SessionAnswer.question_code).where(SessionAnswer.session_id == session.id)
        )
    )
    states = _states(session.theta_snapshot)
    judged: dict[str, dict] = {}
    pending = [q for q in (*plan["dialog"], plan["practical"]) if q is not None and q.code not in asked]
    for q in pending:
        channel = "dialog" if q.type == "open" else "practical"
        prompts = _messages_contents(db, session.id, q.id, channel, "learner")
        artifact_contents = (
            _messages_contents(db, session.id, q.id, "practical", "submit") if q.type == "practical" else []
        )
        artifact = artifact_contents[-1] if artifact_contents else None
        submission = artifact if artifact is not None else "\n".join(prompts)
        seq = (
            db.scalar(
                select(func.max(SessionAnswer.seq)).where(SessionAnswer.session_id == session.id)
            )
            or 0
        ) + 1
        if _skip_marked(db, session.id, q.id, channel) and (
            not prompts if q.type == "open" else artifact is None
        ):
            # 学员主动跳过（无有效发言/产物）：不判分、不回灌 θ、不作负向评价
            db.add(
                SessionAnswer(
                    session_id=session.id,
                    question_id=q.id,
                    question_code=q.code,
                    dimension=q.dimension,
                    answer="",
                    is_correct=None,
                    score=None,
                    theta_after=states[q.dimension].theta,
                    seq=seq,
                )
            )
            db.flush()  # 跳过行同样即时落库：后续题的 max(seq) 查询才能看到，避免重号
            judged[q.code] = {"rationale": SKIPPED}
            continue
        if not submission:
            db.add(
                SessionAnswer(
                    session_id=session.id,
                    question_id=q.id,
                    question_code=q.code,
                    dimension=q.dimension,
                    answer="",
                    is_correct=None,
                    score=0,
                    theta_after=states[q.dimension].theta,
                    seq=seq,
                )
            )
            db.flush()  # 未作答行同样即时落库：后续题的 max(seq) 查询才能看到，避免重号
            judged[q.code] = {"rationale": UNANSWERED}
            continue

        if q.type == "open":
            result = judge_answer(_judge_question_dict(q), submission, chat_fn, learner_prompts=prompts)
            score = float(result.score)
            rationale = result.rationale
            judge_raw = result.model_dump()
            reason = _review_reason("对话题", result)
            echo = {"rationale": rationale}
        else:
            artifact_result = judge_answer(_judge_question_dict(q), submission, chat_fn)
            process = _judge_process_score(chat_fn, prompts) if prompts else None
            process_degraded = False
            notes = []
            if process is None:  # 无过程材料或量表判分失败 → 过程分按产物分折算
                process = float(artifact_result.score)
                if prompts:
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
            echo = {
                "rationale": rationale,
                "process_score": process,
                "artifact_score": artifact_result.score,
            }

        states[q.dimension] = update_open_result(states[q.dimension], q.difficulty, score)
        answer = SessionAnswer(
            session_id=session.id,
            question_id=q.id,
            question_code=q.code,
            dimension=q.dimension,
            answer=submission,
            is_correct=None,
            score=score,
            theta_after=states[q.dimension].theta,
            seq=seq,
        )
        db.add(answer)
        db.flush()
        if reason is not None:
            enqueue_review(db, q.code, session.id, answer.id, judge_raw, reason)
        judged[q.code] = echo
    db.flush()  # 未作答行不经 enqueue_review 的 commit，须显式落库供报告回显查询
    session.theta_snapshot = _snapshot(states)
    return judged


@router.post("/{session_id}/finish")
def finish_session(session_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
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

    # 防重入占位：判题含多次串行 LLM 调用（数十秒），先落库 status 让并发/重试 finish 立即 409
    session.status = "judging"
    db.commit()

    def _chat(messages: list[dict], **kwargs) -> str:
        # 无 Key/上游异常时返回空串：judge_answer 解析失败走内置关键词降级，报告建议走模板回退
        try:
            return chat_completion(messages, **kwargs)  # model_role 等由调用方传入
        except ProviderUnavailableError:
            return ""

    try:
        # full 模式：先统一判题（对话/实操双通道 + θ 回灌 + 复核队列），全部判完才写报告
        judged = _judge_subjective(db, session, _chat) if session.mode == "full" else {}
        report = build_report(db, session, chat_fn=_chat, judged=judged)
        session.status = "finished"
        session.finished_at = utcnow()
        db.add(report)
        db.commit()
    except Exception:
        # 判题/报告异常：丢弃未提交写入（enqueue_review 逐题已提交的结果保留，重试按 asked 跳过），
        # 回滚占位为 in_progress 保证 finish 可重试，re-raise 维持既有 500 行为
        db.rollback()
        session.status = "in_progress"
        db.commit()
        raise
    db.refresh(report)
    return {"report_id": report.id}
