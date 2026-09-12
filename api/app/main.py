from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth_routes import router as auth_router
from app.api.report_routes import router as report_router
from app.api.session_routes import router as session_router

app = FastAPI(title="AI 能力罗盘 API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(session_router)
app.include_router(report_router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "app": "ai-compass"}


_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str):
    if full_path.startswith("api/") or not _DIST.exists():
        raise HTTPException(status_code=404)
    index = _DIST / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404)
    return FileResponse(index)


if _DIST.exists():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="web")
