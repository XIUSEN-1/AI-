#!/bin/sh
set -e
cd /code/api
export COMPASS_DB="${COMPASS_DB:-/tmp/compass.db}"
python -m app.seed
cat > /tmp/orm_diag.py << 'PY'
import traceback
out = {}
try:
    from sqlalchemy import select
    from app.db import SessionLocal
    out["import"] = "ok"
    s = SessionLocal()
    out["session"] = "ok"
    from app.models import User
    rows = s.execute(select(User)).all()
    out["select"] = f"ok rows={len(rows)}"
except Exception:
    out["traceback"] = traceback.format_exc()
open("/tmp/orm_diag.txt", "w").write(repr(out))
PY
PYTHONPATH=/code/api python /tmp/orm_diag.py || true
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-9000}"
