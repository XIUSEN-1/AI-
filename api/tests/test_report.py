from fastapi.testclient import TestClient

from tests.test_session_flow import _run_full_flow


def _finish_a_session(client: TestClient, headers: dict, bank: dict) -> int:
    view = _run_full_flow(client, headers, bank, correct=True)
    resp = client.post(f"/api/sessions/{view['session_id']}/finish", headers=headers)
    assert resp.status_code == 200
    return resp.json()["report_id"]


def test_finish_returns_report_with_radar(client, auth_headers, bank):
    report_id = _finish_a_session(client, auth_headers, bank)
    resp = client.get(f"/api/reports/{report_id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["dimensions"]) == 6
    assert len(body["radar"]) == 6
    assert all(0 <= r["value"] <= 100 for r in body["radar"])
    assert 1 <= body["total_level"] <= 5
    assert body["advice"]
    assert body["created_at"].endswith("Z")
    assert all(d["answered"] >= 2 for d in body["dimensions"])


def test_finish_is_idempotent(client, auth_headers, bank):
    view = _run_full_flow(client, auth_headers, bank, correct=True)
    first = client.post(f"/api/sessions/{view['session_id']}/finish", headers=auth_headers).json()
    second = client.post(f"/api/sessions/{view['session_id']}/finish", headers=auth_headers).json()
    assert first["report_id"] == second["report_id"]


def test_mine_lists_reports(client, auth_headers, bank):
    _finish_a_session(client, auth_headers, bank)
    resp = client.get("/api/reports/mine", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    assert "total_level_name" in resp.json()[0]
    assert resp.json()[0]["created_at"].endswith("Z")


def test_report_forbidden_for_others(client, auth_headers, bank):
    report_id = _finish_a_session(client, auth_headers, bank)
    other = client.post("/api/auth/student", json={"name": "他人", "student_no": "OTHER01"}).json()
    resp = client.get(f"/api/reports/{report_id}", headers={"Authorization": f"Bearer {other['token']}"})
    assert resp.status_code == 403
