"""LLM 接入层测试：全部 monkeypatch httpx 或注入 Mock，绝不触真实 API。"""

import json as jsonlib

import pytest

from app.config import Settings
from app.llm.mock import MockChat, MockStream
from app.llm.provider import (
    ProviderUnavailableError,
    chat_completion,
    chat_stream,
)


# ---------- 测试工具 ----------


def use_settings(monkeypatch, **overrides) -> Settings:
    """给 provider 注入固定 Settings，隔离环境与 .env 差异。"""
    values = {"deepseek_api_key": "test-key"}
    values.update(overrides)
    settings = Settings(**values)
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    return settings


class FakeResponse:
    """非流式假响应。"""

    def __init__(self, payload=None, status_code=200):
        self._payload = payload or {"choices": [{"message": {"content": "ok"}}]}
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeStreamResponse:
    """SSE 流式假响应：可作上下文管理器并按行迭代。"""

    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        return iter(self._lines)


def sse_line(delta: dict) -> str:
    return "data: " + jsonlib.dumps({"choices": [{"delta": delta}]})


# ---------- chat_completion ----------


def test_chat_completion_sends_expected_payload(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr("app.llm.provider.httpx.post", fake_post)
    use_settings(monkeypatch)
    messages = [{"role": "user", "content": "hi"}]
    out = chat_completion(messages, model_role="judge")
    assert out == "ok"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["json"]["model"] == "deepseek-v4-pro"
    assert captured["json"]["temperature"] == 0.0
    assert captured["json"]["messages"] == messages
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["timeout"] == 60


def test_chat_completion_model_role_chat_uses_chat_model(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return FakeResponse()

    monkeypatch.setattr("app.llm.provider.httpx.post", fake_post)
    use_settings(monkeypatch)
    chat_completion([], model_role="chat", temperature=0.7)
    assert captured["json"]["model"] == "deepseek-flash"
    assert captured["json"]["temperature"] == 0.7


def test_json_mode_sets_response_format(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return FakeResponse()

    monkeypatch.setattr("app.llm.provider.httpx.post", fake_post)
    use_settings(monkeypatch)
    chat_completion([], model_role="judge", json_mode=True)
    assert captured["json"]["response_format"] == {"type": "json_object"}

    chat_completion([], model_role="judge")  # 默认关闭
    assert "response_format" not in captured["json"]


def test_no_key_raises(monkeypatch):
    use_settings(monkeypatch, deepseek_api_key="")
    with pytest.raises(ProviderUnavailableError):
        chat_completion([{"role": "user", "content": "hi"}], model_role="judge")


def test_non_200_raises(monkeypatch):
    def fake_post(url, **kw):
        return FakeResponse(status_code=500)

    monkeypatch.setattr("app.llm.provider.httpx.post", fake_post)
    use_settings(monkeypatch)
    with pytest.raises(ProviderUnavailableError, match="500"):
        chat_completion([], model_role="judge")


# ---------- chat_stream ----------


def test_chat_stream_yields_deltas(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, stream=False):
        captured.update(url=url, json=json, headers=headers)
        lines = [
            sse_line({"content": "你"}),
            ": keep-alive 注释行应被忽略",
            sse_line({}),  # 空 delta 应被跳过
            sse_line({"content": "好"}),
            "data: [DONE]",
            sse_line({"content": "不应出现"}),
        ]
        return FakeStreamResponse(lines)

    monkeypatch.setattr("app.llm.provider.httpx.post", fake_post)
    use_settings(monkeypatch)
    deltas = list(chat_stream([{"role": "user", "content": "hi"}], model_role="chat"))
    assert deltas == ["你", "好"]
    assert captured["json"]["stream"] is True
    assert captured["json"]["temperature"] == 0.7
    assert captured["headers"]["Authorization"] == "Bearer test-key"


def test_chat_stream_no_key_raises(monkeypatch):
    use_settings(monkeypatch, deepseek_api_key="")
    with pytest.raises(ProviderUnavailableError):
        list(chat_stream([], model_role="chat"))


# ---------- Mock ----------


def test_mock_chat_records_calls():
    chat = MockChat(["第一段", "第二段"])
    out1 = chat([{"role": "user", "content": "a"}], model_role="judge", temperature=0.0, json_mode=True)
    out2 = chat([{"role": "user", "content": "b"}], model_role="chat", temperature=0.7)
    assert (out1, out2) == ("第一段", "第二段")
    assert len(chat.calls) == 2
    assert chat.calls[0]["messages"] == [{"role": "user", "content": "a"}]
    assert chat.calls[0]["model_role"] == "judge"
    assert chat.calls[0]["json_mode"] is True
    assert chat.calls[1]["temperature"] == 0.7
    with pytest.raises(AssertionError):
        chat([], model_role="judge")  # 响应用尽


def test_mock_stream_yields_fragments():
    stream = MockStream(["片", "段"])
    out = list(stream([{"role": "user", "content": "hi"}], model_role="chat"))
    assert out == ["片", "段"]
    assert len(stream.calls) == 1
    assert stream.calls[0]["model_role"] == "chat"


# ---------- config ----------


@pytest.fixture()
def fresh_settings(monkeypatch, tmp_path):
    """重置单例缓存并把 .env 指向不存在的临时路径，隔离真实环境。"""
    monkeypatch.setattr("app.config._settings", None)
    monkeypatch.setattr("app.config._ENV_PATH", tmp_path / "absent.env")
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL_JUDGE", "DEEPSEEK_MODEL_CHAT"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_settings_defaults(fresh_settings):
    from app.config import get_settings

    s = get_settings()
    assert s.deepseek_api_key == ""
    assert s.base_url == "https://api.deepseek.com"
    assert s.model_judge == "deepseek-v4-pro"
    assert s.model_chat == "deepseek-flash"


def test_settings_singleton_cached(fresh_settings):
    from app.config import get_settings

    assert get_settings() is get_settings()


def test_env_file_loaded_without_overriding_existing_env(fresh_settings, monkeypatch):
    env_file = fresh_settings / "real.env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=file-key\n"
        "# 注释行\n"
        "DEEPSEEK_BASE_URL=https://file.example\n"
        "DEEPSEEK_MODEL_JUDGE=file-judge\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config._ENV_PATH", env_file)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://env.example")  # 已有 env 不被覆盖
    from app.config import get_settings

    s = get_settings()
    assert s.deepseek_api_key == "file-key"  # 仅存在于 .env → 采用
    assert s.base_url == "https://env.example"  # env 优先于 .env
    assert s.model_judge == "file-judge"
    assert s.model_chat == "deepseek-flash"  # 两处都没有 → 默认
