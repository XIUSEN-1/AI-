from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import update

from app.api.admin_routes import router as admin_router
from app.api.auth_routes import router as auth_router
from app.api.auth_routes import get_db
from app.api.dialog_routes import router as dialog_router
from app.api.practical_routes import router as practical_router
from app.api.practice_routes import router as practice_router
from app.api.report_routes import router as report_router
from app.api.session_routes import router as session_router
from app.api.teacher_routes import router as teacher_router
from app.config import DEFAULT_JWT_SECRET, get_settings
from app.db import SessionLocal, init_db
from app.models import AssessmentSession

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

@app.middleware("http")
async def traceback_middleware(request, call_next):
    """临时诊断（验证后移除）：全局异常可见化。"""
    try:
        return await call_next(request)
    except Exception:
        import traceback

        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(traceback.format_exc(), status_code=500)


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



from pydantic import BaseModel as _BM

class _EchoIn(_BM):
    x: int = 1

@app.get("/api/debug/dep")
def debug_dep(db=Depends(get_db)) -> dict:
    from sqlalchemy import select
    from app.models import User

    try:
        rows = db.execute(select(User.id)).all()
        return {"via_depends": True, "users": len(rows)}
    except Exception:
        import traceback

        return {"via_depends": False, "tb": traceback.format_exc()[-500:]}


@app.get("/api/debug/steps")
def debug_steps() -> dict:
    """逐步执行注册内部操作，定位 500 的确切环节。"""
    import traceback

    out = {}
    steps = []
    def _step(name, fn):
        try:
            steps.append({name: fn()})
        except Exception:
            steps.append({name: "EXC: " + traceback.format_exc()[-400:]})

    from app.api.auth_routes import StudentRegisterIn
    _step("pydantic", lambda: StudentRegisterIn(name="t", student_no="STEP01").model_dump())
    from app.auth import hash_password
    _step("hash", lambda: hash_password("x")[:10])
    from app.auth import make_token
    _step("jwt", lambda: make_token(999, "student")[:20])
    from app.api.auth_routes import get_db
    from sqlalchemy import select
    from app.models import User
    def _orm_insert():
        db = next(get_db())
        try:
            u = User(name="step", student_no="STEP" + str(len(steps)) + str(id(steps) % 1000), role="student")
            db.add(u)
            db.commit()
            return f"inserted id={u.id}"
        finally:
            db.close()
    _step("orm_insert", _orm_insert)
    out["steps"] = steps
    return out


@app.get("/api/debug/orm")
def debug_orm() -> dict:
    """走与注册完全相同的 get_db 依赖链。"""
    import traceback

    from app.api.auth_routes import get_db
    from sqlalchemy import select
    from app.models import User

    db = next(get_db())
    try:
        rows = db.execute(select(User.id)).all()
        return {"ok": True, "users": len(rows)}
    except Exception:
        return {"ok": False, "tb": traceback.format_exc()[-800:]}
    finally:
        db.close()


@app.post("/api/echo")
async def echo(body: _EchoIn):
    return {"received": body.x}


@app.get("/api/debug/echo2")
def echo2():
    return {"via": "main-direct"}


@app.post("/api/debug/stu")
def stu(body: StudentRegisterIn) -> dict:
    """与注册端点完全相同的 body 模型 + Depends(get_db)，但在 main.py。"""
    from sqlalchemy import select
    from app.models import User

    rows = db_dep().execute(select(User.id)).all()
    return {"ok": True, "name": body.name, "users": len(rows)}


def db_dep():
    return next(get_db())


@app.get("/api/health")
def debug_env() -> dict:
    """临时诊断（验证后移除）：环境快照。"""
    import os
    import sys

    return {
        "python": sys.version,
        "cwd": os.getcwd(),
        "compass_db": os.environ.get("COMPASS_DB", "<unset>"),
        "port": os.environ.get("PORT", "<unset>"),
        "jwt": os.environ.get("COMPASS_JWT_SECRET", "<unset>")[:8],
        "writable_tmp": os.access("/tmp", os.W_OK),
        "writable_cwd": os.access(os.getcwd(), os.W_OK),
    }


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
