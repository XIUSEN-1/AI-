from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, update

from app.api.admin_routes import router as admin_router
from app.api.auth_routes import get_db, router as auth_router
from app.api.dialog_routes import router as dialog_router
from app.api.practical_routes import router as practical_router
from app.api.practice_routes import router as practice_router
from app.api.report_routes import router as report_router
from app.api.session_routes import router as session_router
from app.api.teacher_routes import router as teacher_router
from app.config import DEFAULT_JWT_SECRET, get_settings
from app.db import SessionLocal, init_db
from app.models import AssessmentSession, User

logger = logging.getLogger(__name__)


def warn_weak_jwt_secret() -> None:
    """生产加固：JWT 签名密钥仍为默认开发值时打 WARNING（部署时以 COMPASS_JWT_SECRET 覆盖）。"""
    if get_settings().jwt_secret == DEFAULT_JWT_SECRET:
        logger.warning(
            "COMPASS_JWT_SECRET 仍为默认开发密钥（%s），生产部署必须通过环境变量更换为随机长字符串！",
            DEFAULT_JWT_SECRET,
        )


def _reset_stale_judging() -> None:
    """服务重启时清扫陈旧 judging：判题后台线程不跨进程存活，崩溃遗留的卡死态复位为
    in_progress（step 归零），学员可重试 finish。"""
    with SessionLocal() as db:
        db.execute(
            update(AssessmentSession)
            .where(AssessmentSession.status == "judging")
            .values(status="in_progress", judging_step=0)
        )
        db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    _reset_stale_judging()
    warn_weak_jwt_secret()
    yield


app = FastAPI(title="AI 能力罗盘 API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(session_router)
app.include_router(dialog_router)
app.include_router(practical_router)
app.include_router(report_router)
app.include_router(practice_router)
app.include_router(teacher_router)
app.include_router(admin_router)


@app.post("/api/debug/login-dep")
def login_dep(payload: dict, db=Depends(get_db)) -> dict:
    """诊断对照：POST + pydantic dict + Depends(get_db) 三要素（main.py 直挂）。"""
    rows = db.execute(select(User.id)).all()
    return {"ok": True, "users": len(rows), "got": payload}


@app.post("/api/debug/postcheck")
def post_check(payload: dict) -> dict:
    return {"ok": True, "got": payload}


@app.get("/api/auth/version")
def auth_version() -> dict:
    return {"build": "v24-clean"}


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "app": "ai-compass"}


_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str):
    if full_path.startswith("api/") or not _DIST.exists():
        raise HTTPException(status_code=404)
    dist_root = _DIST.resolve()
    candidate = (_DIST / full_path).resolve()
    if candidate.is_relative_to(dist_root) and candidate.is_file():
        return FileResponse(candidate)
    index = _DIST / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404)
    return FileResponse(index)


if _DIST.exists():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="web")
