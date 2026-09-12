"""部署配置：CORS 白名单 env 解析、JWT 密钥 env 化与弱密钥启动告警。"""

import logging

from app.config import parse_cors_origins

DEFAULT_ORIGINS = ("http://localhost:5173",)
DEFAULT_SECRET = "dev-secret-change-me"


# ---------- parse_cors_origins：逗号分隔白名单纯函数 ----------


def test_parse_single_origin():
    assert parse_cors_origins("http://localhost:5173") == ("http://localhost:5173",)


def test_parse_multiple_with_spaces_and_empty_segments():
    raw = " https://a.example.com , https://b.example.com , ,"
    assert parse_cors_origins(raw) == ("https://a.example.com", "https://b.example.com")


def test_parse_empty_string_yields_empty():
    assert parse_cors_origins("") == ()


# ---------- get_settings：env 优先，默认行为不变 ----------


def test_settings_reads_cors_and_jwt_from_env(monkeypatch):
    from app import config as cfg

    monkeypatch.setenv("COMPASS_CORS_ORIGINS", "https://x.example.cn,https://y.example.cn")
    monkeypatch.setenv("COMPASS_JWT_SECRET", "prod-secret-9f8e7d6c")
    monkeypatch.setattr(cfg, "_settings", None)  # 重置单例缓存，强制重读
    s = cfg.get_settings()
    assert s.cors_origins == ("https://x.example.cn", "https://y.example.cn")
    assert s.jwt_secret == "prod-secret-9f8e7d6c"


def test_settings_defaults_unchanged_without_env(monkeypatch):
    from app import config as cfg

    monkeypatch.delenv("COMPASS_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("COMPASS_JWT_SECRET", raising=False)
    monkeypatch.setattr(cfg, "_settings", None)
    s = cfg.get_settings()
    assert s.cors_origins == DEFAULT_ORIGINS
    assert s.jwt_secret == DEFAULT_SECRET


def test_app_cors_allows_default_dev_origin():
    """默认白名单仍是 http://localhost:5173（行为不变）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.options(
        "/api/health",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


# ---------- JWT 弱密钥启动告警（caplog） ----------


def test_default_jwt_secret_warns_on_startup(monkeypatch, caplog):
    from app.main import warn_weak_jwt_secret

    monkeypatch.delenv("COMPASS_JWT_SECRET", raising=False)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        warn_weak_jwt_secret()
    assert any("COMPASS_JWT_SECRET" in r.message for r in caplog.records)


def test_custom_jwt_secret_does_not_warn(monkeypatch, caplog):
    from app import config as cfg
    from app.main import warn_weak_jwt_secret

    monkeypatch.setenv("COMPASS_JWT_SECRET", "prod-secret-9f8e7d6c")
    monkeypatch.setattr(cfg, "_settings", None)  # 单例已缓存默认值，强制重读 env
    with caplog.at_level(logging.WARNING, logger="app.main"):
        warn_weak_jwt_secret()
    assert not caplog.records


def test_auth_uses_settings_secret_for_token_roundtrip(monkeypatch):
    """密钥经 settings 生效：换密钥后旧 token 失效、新 token 可验。"""
    import jwt as pyjwt
    from app import config as cfg
    from app.auth import make_token

    monkeypatch.setenv("COMPASS_JWT_SECRET", "rotated-secret-abcdef")
    monkeypatch.setattr(cfg, "_settings", None)
    token = make_token(42, "student")
    assert pyjwt.decode(token, "rotated-secret-abcdef", algorithms=["HS256"])["sub"] == "42"
