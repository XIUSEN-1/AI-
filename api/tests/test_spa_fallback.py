import pytest
from fastapi.testclient import TestClient

from app.main import _DIST, app

client = TestClient(app)

_STATIC_ASSET = next((_DIST / "assets").glob("*.js"), None) or (
    _DIST / "favicon.svg"
)


@pytest.mark.skipif(not (_DIST / "index.html").exists(), reason="web/dist 未构建")
def test_spa_deep_link_returns_index():
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_unknown_api_still_404():
    resp = client.get("/api/nonexistent")
    assert resp.status_code == 404


@pytest.mark.skipif(not _STATIC_ASSET.exists(), reason="web/dist 未构建")
def test_static_asset_served_directly():
    rel = _STATIC_ASSET.relative_to(_DIST).as_posix()
    resp = client.get(f"/{rel}")
    assert resp.status_code == 200
    assert "text/html" not in resp.headers["content-type"]


@pytest.mark.skipif(not (_DIST / "index.html").exists(), reason="web/dist 未构建")
def test_path_traversal_blocked():
    resp = client.get("/%2e%2e/%2e%2e/api/app/main.py")
    leaked = resp.status_code == 200 and "from fastapi" in resp.text
    assert not leaked
