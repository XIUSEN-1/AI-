"""DeepSeek 兼容 API 的同步接入层（判题用非流式，对话用 SSE 流式）。"""

from __future__ import annotations

import json
from typing import Iterator, Literal

import httpx

from app.config import get_settings

ModelRole = Literal["judge", "chat"]


class ProviderUnavailableError(RuntimeError):
    """缺少 API Key 或上游返回异常，无法调用 LLM。"""


def _model_for(model_role: ModelRole) -> str:
    settings = get_settings()
    return settings.model_judge if model_role == "judge" else settings.model_chat


def chat_completion(
    messages: list[dict],
    *,
    model_role: ModelRole,
    temperature: float = 0.0,
    json_mode: bool = False,
    timeout: float = 60,
) -> str:
    settings = get_settings()
    if not settings.deepseek_api_key:
        raise ProviderUnavailableError("缺少 DEEPSEEK_API_KEY，无法调用 LLM")
    payload: dict = {
        "model": _model_for(model_role),
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        resp = httpx.post(
            f"{settings.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise ProviderUnavailableError(f"LLM 上游返回异常状态码 {resp.status_code}")
        return resp.json()["choices"][0]["message"]["content"]
    except httpx.HTTPError as exc:  # 含 Timeout/Connect 等全部网络异常
        raise ProviderUnavailableError(f"LLM 网络请求失败：{exc}") from exc


def chat_stream(
    messages: list[dict],
    *,
    model_role: ModelRole,
    temperature: float = 0.7,
) -> Iterator[str]:
    settings = get_settings()
    if not settings.deepseek_api_key:
        raise ProviderUnavailableError("缺少 DEEPSEEK_API_KEY，无法调用 LLM")
    try:
        with httpx.post(
            f"{settings.base_url}/chat/completions",
            json={
                "model": _model_for(model_role),
                "messages": messages,
                "temperature": temperature,
                "stream": True,
            },
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            timeout=60,
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                raise ProviderUnavailableError(f"LLM 上游返回异常状态码 {resp.status_code}")
            for line in resp.iter_lines():
                if not line.startswith("data:"):  # 注释行（: keep-alive 等）跳过
                    continue
                data = line.removeprefix("data:").strip()  # 容忍 data: 与 data: 两种间隔
                if data == "[DONE]":
                    break
                if not data:  # 空数据行（心跳）跳过
                    continue
                delta = json.loads(data)["choices"][0]["delta"].get("content")
                if delta:
                    yield delta
    except httpx.HTTPError as exc:  # 含建连失败与流中断/超时
        raise ProviderUnavailableError(f"LLM 网络请求失败：{exc}") from exc
