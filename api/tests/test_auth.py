from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import app
from app.seed import ensure_base_accounts


def setup_module(module):
    init_db()
    with SessionLocal() as db:
        ensure_base_accounts(db)


def test_student_register_returns_token():
    client = TestClient(app)
    resp = client.post("/api/auth/student", json={"name": "张三", "student_no": "S1001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["role"] == "student"
    assert body["token"]


def test_student_register_idempotent_login():
    client = TestClient(app)
    first = client.post("/api/auth/student", json={"name": "李四", "student_no": "S1002"}).json()
    second = client.post("/api/auth/student", json={"name": "李四", "student_no": "S1002"}).json()
    assert first["user"]["id"] == second["user"]["id"]


def test_invalid_invite_code_rejected():
    client = TestClient(app)
    resp = client.post("/api/auth/student", json={"name": "王五", "student_no": "S1003", "invite_code": "NOPE"})
    assert resp.status_code == 400


def test_student_register_cannot_takeover_non_student():
    """已知非学员学号（如 admin）不得经注册接口换取其角色令牌。"""
    client = TestClient(app)
    resp = client.post("/api/auth/student", json={"name": "x", "student_no": "admin"})
    assert resp.status_code == 400


def test_admin_password_login():
    client = TestClient(app)
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "admin"


def test_wrong_password_rejected():
    client = TestClient(app)
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    assert resp.status_code == 401


def test_me_requires_token():
    client = TestClient(app)
    assert client.get("/api/auth/me").status_code == 401
    token = client.post("/api/auth/student", json={"name": "赵六", "student_no": "S1004"}).json()["token"]
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "赵六"
