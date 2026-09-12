from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.auth import current_user
from app.engine.adaptive import LEVEL_NAMES
from app.models import Klass, Report, User

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/mine")
def my_reports(user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Report).where(Report.user_id == user["id"]).order_by(Report.id.desc())).all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat() + "Z",
            "total_level": r.total_level,
            "total_level_name": LEVEL_NAMES[r.total_level],
        }
        for r in rows
    ]


@router.get("/{report_id}")
def get_report(report_id: int, user: dict = Depends(current_user), db: OrmSession = Depends(get_db)) -> dict:
    """报告详情：本人/admin 完整；teacher 仅本班学员且只给维度聚合层级
    （剥离 answers 逐题明细，Global Constraints"不展示单个作答明细"）；其余 403。"""
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    is_owner = report.user_id == user["id"]
    teacher_view = False
    if not is_owner:
        if user["role"] == "admin":
            pass  # admin 全权：完整报告
        elif user["role"] == "teacher":
            owner = db.get(User, report.user_id)
            klass = db.get(Klass, owner.class_id) if owner is not None and owner.class_id else None
            if klass is None or klass.teacher_id != user["id"]:
                raise HTTPException(status_code=403, detail="无权查看该报告")
            teacher_view = True
        else:
            raise HTTPException(status_code=403, detail="无权查看该报告")
    out = {
        "id": report.id,
        "session_id": report.session_id,
        "created_at": report.created_at.isoformat() + "Z",
        "dimensions": report.dimensions,
        "total_level": report.total_level,
        "total_level_name": LEVEL_NAMES[report.total_level],
        "radar": report.radar,
        "strengths": report.strengths,
        "gaps": report.gaps,
        "advice": report.advice,
        "advice_source": report.advice_source,
        "advice_detail": report.advice_detail or [],
        "answers": report.answers or [],
    }
    if teacher_view:
        out.pop("answers")  # 教师版仅维度聚合层级
    return out
