#!/bin/sh
set -e
cd /code/api
export COMPASS_DB="${COMPASS_DB:-/tmp/compass.db}"
python -m app.seed
cat > /tmp/orm_diag.py << 'PY'
import json
import traceback

out = {}
try:
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    for path, payload in [
        ("/api/health", None),
        ("/api/auth/me", None),
        ("/api/auth/login", {"username": "admin", "password": "admin123"}),
        ("/api/auth/student", {"name": "diag", "student_no": "DIAG01"}),
    ]:
        try:
            r = c.post(path, json=payload) if payload else c.get(path)
            out[path] = {"status": r.status_code, "body": r.text[:300]}
        except Exception:
            out[path] = {"exc": traceback.format_exc()[-600:]}
except Exception:
    out["setup"] = traceback.format_exc()[-600:]
open("/tmp/orm_diag.txt", "w").write(json.dumps(out, ensure_ascii=False, indent=1))
PY
PY
PYTHONPATH=/code/api python /tmp/orm_diag.py || true
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-9000}"
