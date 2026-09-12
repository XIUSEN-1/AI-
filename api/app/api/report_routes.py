from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.auth import current_user
from app.engine.adaptive import LEVEL_NAMES
from app.models import Report

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
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    if report.user_id != user["id"] and user["role"] not in ("teacher", "admin"):
        raise HTTPException(status_code=403, detail="无权查看该报告")
    return {
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
