"""教师端（spec §8）：班级管理（建班得邀请码）与只读聚合看板、CSV 导出。

权限口径（Global Constraints）：teacher 只读本班学员的聚合数据（不展示单个学员作答明细），
admin 全权可代查；越权 403/404。看板"最新报告"口径：学员在该班级范围内的最新一次测评报告。
"""

from __future__ import annotations

import csv
import io
import secrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.api.auth_routes import get_db
from app.auth import require_roles
from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES, LEVEL_NAMES
from app.models import AssessmentSession, Klass, Question, Report, SessionAnswer, User

router = APIRouter(prefix="/api/teacher", tags=["teacher"])


class ClassIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)


def _gen_invite_code(db: OrmSession) -> str:
    """生成唯一邀请码（C + 6 位大写十六进制），冲突重试。"""
    while True:
        code = "C" + secrets.token_hex(3).upper()
        if db.scalar(select(Klass.id).where(Klass.invite_code == code)) is None:
            return code


def _owned_class(db: OrmSession, class_id: int, user: dict) -> Klass:
    """班级装载：不存在 404；非本班教师 403（admin 全权放行）。"""
    klass = db.get(Klass, class_id)
    if klass is None:
        raise HTTPException(status_code=404, detail="班级不存在")
    if user["role"] != "admin" and klass.teacher_id != user["id"]:
        raise HTTPException(status_code=403, detail="无权访问该班级")
    return klass


def _class_students(db: OrmSession, class_id: int) -> list[User]:
    return db.scalars(
        select(User).where(User.class_id == class_id, User.role == "student").order_by(User.id)
    ).all()


def _class_reports(
    db: OrmSession, students: list[User]
) -> tuple[dict[int, list[Report]], dict[int, Report]]:
    """班内学员的全部报告（按 id 升序）与每人最新一份；无报告学员不出现在字典中。"""
    reports: dict[int, list[Report]] = {}
    latest: dict[int, Report] = {}
    if students:
        rows = db.scalars(
            select(Report).where(Report.user_id.in_([s.id for s in students])).order_by(Report.id)
        ).all()
        for r in rows:
            reports.setdefault(r.user_id, []).append(r)
            latest[r.user_id] = r  # id 升序，末位即最新
    return reports, latest


def _dim_percent(report: Report, dimension: str) -> float:
    return next(x["percent"] for x in report.dimensions if x["dimension"] == dimension)


def _overall_percent(report: Report) -> float:
    return sum(x["percent"] for x in report.dimensions) / len(report.dimensions)


def _trend_slope(values: list[float]) -> float:
    """成长趋势：历次报告综合得分（六维均值）对报告序号的最小二乘斜率（分/次），
    单次或无报告为 0；正=上行、负=下行。"""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    denom = sum((i - mean_x) ** 2 for i in range(n))
    return round(sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values)) / denom, 2)


def _weakness_tags(db: OrmSession, students: list[User], dimension: str) -> list[str]:
    """维度薄弱考点：班内学员该维度错题（is_correct=False）的知识点标签频次 TOP3。"""
    counts: dict[str, int] = {}
    if students:
        rows = db.execute(
            select(Question.tags)
            .join(SessionAnswer, SessionAnswer.question_id == Question.id)
            .join(AssessmentSession, AssessmentSession.id == SessionAnswer.session_id)
            .where(
                AssessmentSession.user_id.in_([s.id for s in students]),
                SessionAnswer.dimension == dimension,
                SessionAnswer.is_correct.is_(False),
            )
        ).all()
        for (tags,) in rows:
            for tag in tags or []:
                counts[tag] = counts.get(tag, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [tag for tag, _ in ordered[:3]]


@router.post("/classes")
def create_class(body: ClassIn, user: dict = Depends(require_roles("teacher", "admin")), db: OrmSession = Depends(get_db)) -> dict:
    klass = Klass(name=body.name, invite_code=_gen_invite_code(db), teacher_id=user["id"])
    db.add(klass)
    db.commit()
    db.refresh(klass)
    return {"id": klass.id, "name": klass.name, "invite_code": klass.invite_code}


@router.get("/classes")
def list_classes(user: dict = Depends(require_roles("teacher", "admin")), db: OrmSession = Depends(get_db)) -> list[dict]:
    if user["role"] == "admin":
        klasses = db.scalars(select(Klass).order_by(Klass.id)).all()
    else:
        klasses = db.scalars(select(Klass).where(Klass.teacher_id == user["id"]).order_by(Klass.id)).all()
    return [
        {"id": k.id, "name": k.name, "invite_code": k.invite_code, "student_count": len(_class_students(db, k.id))}
        for k in klasses
    ]


@router.get("/classes/{class_id}/analytics")
def class_analytics(class_id: int, user: dict = Depends(require_roles("teacher", "admin")), db: OrmSession = Depends(get_db)) -> dict:
    """班级只读聚合：六维均值雷达、维度排名、薄弱 TOP3、学员表（最新等级/次数/趋势）、成长时间线。"""
    klass = _owned_class(db, class_id, user)
    students = _class_students(db, klass.id)
    reports, latest = _class_reports(db, students)

    def avg_percent(dimension: str) -> float:
        values = [_dim_percent(latest[s.id], dimension) for s in students if s.id in latest]
        return round(sum(values) / len(values), 1) if values else 0.0

    ranked = sorted(DIMENSIONS, key=lambda d: (-avg_percent(d), d))
    radar = [{"dimension": d, "label": DIMENSION_NAMES[d], "avg_percent": avg_percent(d)} for d in DIMENSIONS]
    dimension_rank = [
        {"dimension": d, "label": DIMENSION_NAMES[d], "avg_percent": avg_percent(d)} for d in ranked
    ]
    weakness_top3 = [
        {"dimension": d, "tags": _weakness_tags(db, students, d)}
        for d in ranked[-3:][::-1]  # 最弱在前
    ]
    student_rows = [
        {
            "id": s.id,
            "name": s.name,
            "student_no": s.student_no,
            "last_level_name": LEVEL_NAMES[latest[s.id].total_level] if s.id in latest else None,
            "reports": len(reports.get(s.id, [])),
            "trend_slope": _trend_slope([_overall_percent(r) for r in reports.get(s.id, [])]),
        }
        for s in students
    ]
    by_date: dict[str, list[float]] = {}
    for r in sorted((x for rows_ in reports.values() for x in rows_), key=lambda x: x.id):
        by_date.setdefault(r.created_at.date().isoformat(), []).append(_overall_percent(r))
    timeline = [
        {"date": day, "avg_percent": round(sum(values) / len(values), 1)} for day, values in sorted(by_date.items())
    ]
    return {
        "student_count": len(students),
        "radar": radar,
        "dimension_rank": dimension_rank,
        "weakness_top3": weakness_top3,
        "students": student_rows,
        "timeline": timeline,
    }


@router.get("/classes/{class_id}/export.csv")
def export_class_csv(class_id: int, user: dict = Depends(require_roles("teacher", "admin")), db: OrmSession = Depends(get_db)) -> Response:
    """学员×六维最新得分 CSV：中文表头，UTF-8 BOM（Excel 直接打开不乱码）。"""
    klass = _owned_class(db, class_id, user)
    students = _class_students(db, klass.id)
    _, latest = _class_reports(db, students)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["姓名", "学号", *[DIMENSION_NAMES[d] for d in DIMENSIONS]])
    for s in students:
        report = latest.get(s.id)
        row = [s.name, s.student_no]
        for d in DIMENSIONS:
            row.append(_dim_percent(report, d) if report is not None else "")
        writer.writerow(row)
    content = "\ufeff" + buf.getvalue()
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=class_{klass.id}.csv"},
    )
