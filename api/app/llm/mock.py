"""LLM mock 替身：测试与本地无 Key 环境使用，签名与 provider 保持一致。"""

from __future__ import annotations

import threading
from typing import Iterator, Literal

ModelRole = Literal["judge", "chat"]


class MockChat:
    """按序返回预设响应的非流式 mock，记录每次调用。
    判题双跑并发调用 → 记录与响应弹出以锁保持原子（否则 calls 顺序与响应分配会竞态）。"""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def __call__(
        self,
        messages: list[dict],
        *,
        model_role: ModelRole = "judge",
        temperature: float = 0.0,
        json_mode: bool = False,
        timeout: float = 60,
    ) -> str:
        with self._lock:
            self.calls.append(
                {
                    "messages": messages,
                    "model_role": model_role,
                    "temperature": temperature,
                    "json_mode": json_mode,
                }
            )
            if not self.responses:
                raise AssertionError("MockChat 预设响应已用尽")
            return self.responses.pop(0)


class MockStream:
    """按序产出预设分片的流式 mock，记录每次调用（记录操作加锁，防并发竞态）。"""

    def __init__(self, fragments: list[str]):
        self.fragments = list(fragments)
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def __call__(
        self,
        messages: list[dict],
        *,
        model_role: ModelRole = "chat",
        temperature: float = 0.7,
    ) -> Iterator[str]:
        with self._lock:
            self.calls.append(
                {"messages": messages, "model_role": model_role, "temperature": temperature}
            )
        yield from self.fragments
