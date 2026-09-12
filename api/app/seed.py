"""题库种子导入：python -m app.seed [--dir seeds/questions]"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.db import SessionLocal, init_db
from app.models import Klass, Question, User

DIMENSIONS = ("D1", "D2", "D3", "D4", "D5", "D6")
OBJECTIVE_TYPES = ("single", "multi", "judge")
ALL_TYPES = (*OBJECTIVE_TYPES, "open", "practical")
_MUTABLE = ("dimension", "tier", "type", "difficulty", "stem", "options", "answer", "tags", "est_seconds", "explanation", "rubric")


def load_seed_files(dir_: Path) -> list[dict]:
    items: list[dict] = []
    for path in sorted(dir_.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if "questions" not in data:
                raise ValueError(f"{path}: 种子文件缺少 questions 键")
            data = data["questions"]
        items.extend(data)
    return items


def validate_question(q: dict) -> None:
    code = q.get("id", "<无 id>")
    if q.get("dimension") not in DIMENSIONS:
        raise ValueError(f"{code}: dimension 非法")
    if q.get("tier") not in ("basic", "advanced"):
        raise ValueError(f"{code}: tier 非法")
    if q.get("type") not in ALL_TYPES:
        raise ValueError(f"{code}: type 非法")
    difficulty = q.get("difficulty")
    if not isinstance(difficulty, int) or isinstance(difficulty, bool) or not 1 <= difficulty <= 5:
        raise ValueError(f"{code}: difficulty 必须为 1..5")
    if q["tier"] == "basic" and difficulty > 3:
        raise ValueError(f"{code}: 基础题难度不得超过 3")
    if q["tier"] == "advanced" and difficulty < 4:
        raise ValueError(f"{code}: 进阶题难度不得低于 4")
    if not q.get("stem"):
        raise ValueError(f"{code}: stem 不能为空")
    est = q.get("est_seconds")
    if not isinstance(est, int) or not 15 <= est <= 600:
        raise ValueError(f"{code}: est_seconds 必须为 15..600")

    if q["type"] in ("single", "multi"):
        options = q.get("options")
        if not isinstance(options, list) or len(options) < 2:
            raise ValueError(f"{code}: 客观选择题 options 至少 2 项")
        if not all(isinstance(o, dict) for o in options):
            raise ValueError(f"{code}: options 的 key/text 不合规")
        keys = {o.get("key") for o in options}
        if len(keys) != len(options) or not all(o.get("text") for o in options):
            raise ValueError(f"{code}: options 的 key/text 不合规")
        answer = q.get("answer")
        if q["type"] == "single":
            if answer not in keys:
                raise ValueError(f"{code}: 单选 answer 必须是选项 key")
        else:
            if not isinstance(answer, list) or not answer or not set(answer) <= keys:
                raise ValueError(f"{code}: 多选 answer 必须是选项 key 的非空子集")
    elif q["type"] == "judge":
        if not isinstance(q.get("answer"), bool):
            raise ValueError(f"{code}: 判断题 answer 必须为布尔值")
    else:  # open | practical
        if q.get("answer") is not None:
            raise ValueError(f"{code}: 开放题不应预设 answer")
        rubric = q.get("rubric")
        if not isinstance(rubric, dict):
            raise ValueError(f"{code}: 开放题必须提供 rubric")
        if not isinstance(rubric.get("points"), list) or not rubric["points"]:
            raise ValueError(f"{code}: rubric.points 不能为空")
        anchors = rubric.get("anchors")
        if not isinstance(anchors, dict) or set(anchors) != {"0", "1", "2", "3", "4"}:
            raise ValueError(f"{code}: rubric.anchors 必须含 0-4 五档锚定")


def import_questions(items: list[dict], session: OrmSession) -> dict[str, int]:
    created = updated = 0
    seen: set[str] = set()
    for raw in items:
        validate_question(raw)
        code = raw["id"]
        if code in seen:
            raise ValueError(f"{code}: 批内存在重复 id")
        seen.add(code)
        existing = session.scalar(select(Question).where(Question.code == code))
        if existing is None:
            session.add(Question(code=raw["id"], **{k: raw.get(k) for k in _MUTABLE}))
            created += 1
            continue
        changed = any(getattr(existing, k) != raw.get(k) for k in _MUTABLE)
        if changed:
            for k in _MUTABLE:
                setattr(existing, k, raw.get(k))
            existing.version += 1
            updated += 1
    session.commit()
    return {"created": created, "updated": updated}


def ensure_base_accounts(session: OrmSession) -> None:
    """幂等创建默认班级与 admin 账号（Task 5 实现密码部分）。"""
    if session.scalar(select(Klass).where(Klass.invite_code == "PUBLIC")) is None:
        session.add(Klass(name="自由测评班", invite_code="PUBLIC"))
    if session.scalar(select(User).where(User.student_no == "admin")) is None:
        from app.auth import hash_password

        session.add(User(name="管理员", student_no="admin", role="admin", password_hash=hash_password("admin123")))
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="导入题库种子")
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent.parent / "seeds" / "questions"))
    args = parser.parse_args()
    init_db()
    with SessionLocal() as db:
        ensure_base_accounts(db)
        result = import_questions(load_seed_files(Path(args.dir)), db)
    print(result)


if __name__ == "__main__":
    main()
