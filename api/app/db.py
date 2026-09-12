from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def _default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "compass.db"


DB_PATH = os.environ.get("COMPASS_DB", str(_default_db_path()))

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from app import models  # noqa: F401  确保模型完成注册

    Base.metadata.create_all(engine)
    _migrate_report_columns()


def _migrate_report_columns() -> None:
    """create_all 不做列级迁移：为 M2a 前的旧库补齐 reports 新增列（幂等，新库天然跳过）。"""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("reports"):
        return
    cols = {c["name"] for c in insp.get_columns("reports")}
    with engine.begin() as conn:
        if "answers" not in cols:
            conn.execute(text("ALTER TABLE reports ADD COLUMN answers JSON"))
        if "advice_source" not in cols:
            conn.execute(text("ALTER TABLE reports ADD COLUMN advice_source VARCHAR(8) NOT NULL DEFAULT 'template'"))
