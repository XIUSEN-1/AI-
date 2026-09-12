"""教师端（Task 1）：建班得邀请码、班级看板只读聚合、CSV 导出（UTF-8 BOM 中文表头）、越权 403。

权限口径（Global Constraints）：teacher 只读本班学员聚合数据（不展示单个学员作答明细），
admin 全权可代查；聚合数字与学员各自报告逐项对账。零真实 LLM（quick 流程仅客观题规则判分）。
"""

import csv
import io
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.engine.adaptive import DIMENSIONS, DIMENSION_NAMES, LEVEL_NAMES
from conftest import finish_and_wait
from test_session_flow import _run_full_flow


def make_teacher(client: TestClient, suffix: str) -> dict[str, str]:
    """经 admin 建教师账号并登录，返回其鉴权头。"""
    admin = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()
    admin_headers = {"Authorization": f"Bearer {admin['token']}"}
    username = f"t{suffix}"
    resp = client.post(
        "/api/admin/teachers",
        json={"name": f"教师{suffix}", "username": username, "password": "pass123456"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    login = client.post("/api/auth/login", json={"username": username, "password": "pass123456"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['token']}"}


def register_student(client: TestClient, suffix: str, invite_code: str) -> dict[str, str]:
    resp = client.post(
        "/api/auth/student",
        json={"name": f"学员{suffix}", "student_no": f"TC-{suffix}", "invite_code": invite_code},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_create_class_returns_invite_code(client):
    teacher = make_teacher(client, uuid.uuid4().hex[:6])
    resp = client.post("/api/teacher/classes", json={"name": "一班"}, headers=teacher)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] and body["name"] == "一班"
    assert len(body["invite_code"]) >= 6
    # 邀请码真实可用：学员凭码入班
    student = register_student(client, uuid.uuid4().hex[:6], body["invite_code"])
    assert client.get("/api/auth/me", headers=student).status_code == 200


def test_list_classes_only_mine(client):
    teacher_a = make_teacher(client, uuid.uuid4().hex[:6])
    teacher_b = make_teacher(client, uuid.uuid4().hex[:6])
    id_a = client.post("/api/teacher/classes", json={"name": "A班"}, headers=teacher_a).json()["id"]
    id_b = client.post("/api/teacher/classes", json={"name": "B班"}, headers=teacher_b).json()["id"]
    rows = client.get("/api/teacher/classes", headers=teacher_a).json()
    ids = {r["id"] for r in rows}
    assert id_a in ids
    assert id_b not in ids


def test_analytics_aggregates_latest_reports(client, bank):
    """两名学员（全对/全错）各完成一次 quick 测评：看板各项聚合与学员报告逐项对账。"""
    teacher = make_teacher(client, uuid.uuid4().hex[:6])
    klass = client.post("/api/teacher/classes", json={"name": "聚合班"}, headers=teacher).json()
    good = register_student(client, "G" + uuid.uuid4().hex[:5], klass["invite_code"])
    poor = register_student(client, "P" + uuid.uuid4().hex[:5], klass["invite_code"])

    def finish_quick(headers: dict, correct: bool) -> dict:
        view = _run_full_flow(client, headers, bank, correct=correct)
        return finish_and_wait(client, headers, view["session_id"])

    report_good = client.get(f"/api/reports/{finish_quick(good, True)['report_id']}", headers=good).json()
    report_poor = client.get(f"/api/reports/{finish_quick(poor, False)['report_id']}", headers=poor).json()

    resp = client.get(f"/api/teacher/classes/{klass['id']}/analytics", headers=teacher)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["student_count"] == 2

    def pct(report, d):
        return next(x["percent"] for x in report["dimensions"] if x["dimension"] == d)

    expected_avg = {d: round((pct(report_good, d) + pct(report_poor, d)) / 2, 1) for d in DIMENSIONS}
    assert {r["dimension"]: r["avg_percent"] for r in data["radar"]} == expected_avg
    assert all(r["label"] == DIMENSION_NAMES[r["dimension"]] for r in data["radar"])

    rank = [r["dimension"] for r in data["dimension_rank"]]
    assert rank == sorted(DIMENSIONS, key=lambda d: (-expected_avg[d], d))

    weakest = rank[-3:][::-1]  # 最弱在前
    assert [w["dimension"] for w in data["weakness_top3"]] == weakest
    assert all(isinstance(w["tags"], list) for w in data["weakness_top3"])
    # 全错学员在每个维度都留有错题 → 最弱维度必有关联考点标签
    assert data["weakness_top3"][0]["tags"], "最弱维度应聚合出错题考点标签"

    rows = {s["name"]: s for s in data["students"]}
    assert len(rows) == 2
    for student_headers, report in ((good, report_good), (poor, report_poor)):
        me = client.get("/api/auth/me", headers=student_headers).json()
        row = rows[me["name"]]
        assert row["reports"] == 1
        assert row["last_level_name"] == LEVEL_NAMES[report["total_level"]]
        assert row["trend_slope"] == 0.0  # 单次报告无趋势

    overall = lambda r: round(sum(pct(r, d) for d in DIMENSIONS) / 6, 1)
    timeline = data["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["avg_percent"] == round((overall(report_good) + overall(report_poor)) / 2, 1)
    assert len(timeline[0]["date"]) == 10  # YYYY-MM-DD


def test_analytics_empty_class(client):
    teacher = make_teacher(client, uuid.uuid4().hex[:6])
    klass = client.post("/api/teacher/classes", json={"name": "空班"}, headers=teacher).json()
    data = client.get(f"/api/teacher/classes/{klass['id']}/analytics", headers=teacher).json()
    assert data["student_count"] == 0
    assert data["students"] == []
    assert data["timeline"] == []
    assert len(data["radar"]) == 6
    assert all(r["avg_percent"] == 0 for r in data["radar"])


def test_export_csv_bom_headers_and_rows(client, bank):
    teacher = make_teacher(client, uuid.uuid4().hex[:6])
    klass = client.post("/api/teacher/classes", json={"name": "导出班"}, headers=teacher).json()
    student = register_student(client, "C" + uuid.uuid4().hex[:5], klass["invite_code"])
    view = _run_full_flow(client, student, bank, correct=True)
    finish_and_wait(client, student, view["session_id"])

    resp = client.get(f"/api/teacher/classes/{klass['id']}/export.csv", headers=teacher)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.content[:3] == b"\xef\xbb\xbf", "CSV 必须带 UTF-8 BOM（Excel 中文兼容）"
    text = resp.content.decode("utf-8-sig")
    lines = [line for line in text.strip().splitlines() if line]
    header = lines[0].split(",")
    assert header[0] == "姓名"
    for label in DIMENSION_NAMES.values():
        assert label in header, f"缺少中文表头：{label}"
    me = client.get("/api/auth/me", headers=student).json()
    data_row = next(line for line in lines[1:] if me["name"] in line)
    cells = data_row.split(",")
    assert len([c for c in cells if c.strip()]) >= 7  # 姓名+学号+六维得分齐全


def test_export_csv_neutralizes_formula_injection(client, bank):
    """CSV 公式注入中和：以 = + - @ 等开头的自由文本单元格写出前前置单引号，防 Excel 求值。"""
    teacher = make_teacher(client, uuid.uuid4().hex[:6])
    klass = client.post("/api/teacher/classes", json={"name": "注入班"}, headers=teacher).json()
    evil_name = '=HYPERLINK(A1,"x")'
    evil_no = "+CMD" + uuid.uuid4().hex[:6]
    resp = client.post(
        "/api/auth/student",
        json={"name": evil_name, "student_no": evil_no, "invite_code": klass["invite_code"]},
    )
    assert resp.status_code == 200, resp.text
    student = {"Authorization": f"Bearer {resp.json()['token']}"}
    view = _run_full_flow(client, student, bank, correct=True)
    finish_and_wait(client, student, view["session_id"])

    content = client.get(f"/api/teacher/classes/{klass['id']}/export.csv", headers=teacher).content
    text = content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    row = next(r for r in rows[1:] if r and r[0].lstrip("'").startswith("=HYPERLINK"))
    assert row[0] == "'" + evil_name, "公式前缀姓名必须以单引号中和"
    assert row[1] == "'" + evil_no, "公式前缀学号必须以单引号中和"


def test_student_forbidden_on_teacher_routes(client):
    token = client.post("/api/auth/student", json={"name": "越权学员", "student_no": f"TF-{uuid.uuid4().hex[:6]}"}).json()
    headers = {"Authorization": f"Bearer {token['token']}"}
    assert client.post("/api/teacher/classes", json={"name": "x"}, headers=headers).status_code == 403
    assert client.get("/api/teacher/classes", headers=headers).status_code == 403
    assert client.get("/api/teacher/classes/1/analytics", headers=headers).status_code == 403
    assert client.get("/api/teacher/classes/1/export.csv", headers=headers).status_code == 403


def test_other_teacher_class_forbidden(client):
    teacher_a = make_teacher(client, uuid.uuid4().hex[:6])
    teacher_b = make_teacher(client, uuid.uuid4().hex[:6])
    klass = client.post("/api/teacher/classes", json={"name": "A的班"}, headers=teacher_a).json()
    assert client.get(f"/api/teacher/classes/{klass['id']}/analytics", headers=teacher_b).status_code == 403
    assert client.get(f"/api/teacher/classes/{klass['id']}/export.csv", headers=teacher_b).status_code == 403


def test_admin_full_access_to_class_analytics(client, bank):
    """admin 全权：可代查任意班级看板（Global Constraints）。"""
    teacher = make_teacher(client, uuid.uuid4().hex[:6])
    klass = client.post("/api/teacher/classes", json={"name": "代查班"}, headers=teacher).json()
    admin = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()
    headers = {"Authorization": f"Bearer {admin['token']}"}
    assert client.get(f"/api/teacher/classes/{klass['id']}/analytics", headers=headers).status_code == 200
