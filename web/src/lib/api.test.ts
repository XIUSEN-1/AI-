import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, clearToken, getToken, setToken } from "./api";

describe("api client", () => {
  afterEach(() => {
    clearToken();
    vi.unstubAllGlobals();
  });

  it("token 存取", () => {
    expect(getToken()).toBeNull();
    setToken("t1");
    expect(getToken()).toBe("t1");
    clearToken();
    expect(getToken()).toBeNull();
  });

  it("携带 Bearer token", async () => {
    setToken("t123");
    const fetchMock = vi.fn(async (_path: string, _init?: RequestInit) => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await api("/api/health");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer t123");
  });

  it("非 2xx 抛出携带后端 detail 的 ApiError", async () => {
    const fetchMock = vi.fn(
      async () => new Response(JSON.stringify({ detail: "邀请码无效" }), { status: 400 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const err = await api("/api/auth/student", { method: "POST", body: "{}" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toBe("邀请码无效");
  });
});
