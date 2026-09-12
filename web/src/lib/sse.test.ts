import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, clearToken, setToken } from "./api";
import { postSse } from "./sse";

function sseResponse(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(stream, { status, headers: { "Content-Type": "text/event-stream" } });
}

afterEach(() => {
  clearToken();
  vi.unstubAllGlobals();
});

describe("postSse", () => {
  it("以 POST 发送 JSON body 并携带 Bearer token", async () => {
    setToken("t123");
    const fetchMock = vi.fn(async (_path: string, _init?: RequestInit) =>
      sseResponse(['data: {"done": true, "turns": 1}\n\n']),
    );
    vi.stubGlobal("fetch", fetchMock);
    await postSse("/api/sessions/1/dialog/turn", { message: "你好" }, () => {});
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/sessions/1/dialog/turn");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer t123");
    expect(init.body).toBe(JSON.stringify({ message: "你好" }));
  });

  it("按 \\n\\n 分帧、跨 chunk 聚合，依序回调 delta/done 事件", async () => {
    const fetchMock = vi.fn(async () =>
      sseResponse([
        'data: {"delta": "你"}\n\ndata: {"del',
        'ta": "好"}\n\ndata: {"delta": "！"}\n\n',
        'data: {"done": true, "turns": 3}\n\n',
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const events: unknown[] = [];
    await postSse("/p", {}, (e) => events.push(e));
    expect(events).toEqual([
      { delta: "你" },
      { delta: "好" },
      { delta: "！" },
      { done: true, turns: 3 },
    ]);
  });

  it("error 事件原样回调，由调用方决定呈现", async () => {
    const fetchMock = vi.fn(async () =>
      sseResponse(['data: {"error": "AI 服务暂不可用"}\n\n', 'data: {"done": true, "turns": 1}\n\n']),
    );
    vi.stubGlobal("fetch", fetchMock);
    const events: unknown[] = [];
    await postSse("/p", {}, (e) => events.push(e));
    expect(events[0]).toEqual({ error: "AI 服务暂不可用" });
    expect(events[1]).toEqual({ done: true, turns: 1 });
  });

  it("坏帧与注释行跳过，不中断后续事件", async () => {
    const fetchMock = vi.fn(async () =>
      sseResponse([
        ": ping\n\n", // SSE 注释行：忽略
        "data: 这不是JSON\n\n", // 坏帧：跳过
        "\n\n", // 空帧：跳过
        'event: message\ndata: {"delta": "好"}\n\n', // 多行帧只取 data 行
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const events: unknown[] = [];
    await postSse("/p", {}, (e) => events.push(e));
    expect(events).toEqual([{ delta: "好" }]);
  });

  it("流结束时未以 \\n\\n 收尾的尾帧也会被解析", async () => {
    const fetchMock = vi.fn(async () => sseResponse(['data: {"delta": "尾"}\n\ndata: {"done": true}']));
    vi.stubGlobal("fetch", fetchMock);
    const events: unknown[] = [];
    await postSse("/p", {}, (e) => events.push(e));
    expect(events).toEqual([{ delta: "尾" }, { done: true }]);
  });

  it("非 2xx 抛出携带后端 detail 的 ApiError", async () => {
    const fetchMock = vi.fn(
      async () => new Response(JSON.stringify({ detail: "请先开始本题" }), { status: 400 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const err = await postSse("/p", {}, () => {}).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toBe("请先开始本题");
  });
});
