import pytest
from fastapi.testclient import TestClient

from app.main import _DIST, app

client = TestClient(app)


@pytest.mark.skipif(not (_DIST / "index.html").exists(), reason="web/dist 未构建")
def test_spa_deep_link_returns_index():
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_unknown_api_still_404():
    resp = client.get("/api/nonexistent")
    assert resp.status_code == 404
