import { afterEach, describe, expect, it, vi } from "vitest";

import { pollJudging } from "./judging";

function statusBody(overrides: Record<string, unknown> = {}) {
  return { status: "judging", stage: "ready", judging_step: 1, judging_total: 3, ...overrides };
}

/** 按 URL 顺序返回预设响应；带 side effect 可在调用间动态换响应。 */
function stubStatusQueue(bodies: Record<string, unknown>[]) {
  const queue = [...bodies];
  const fetchMock = vi.fn(async (_path: string) => {
    const body = queue.length > 0 ? queue.shift() : statusBody();
    return new Response(JSON.stringify(body), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("pollJudging", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("每 2s 汇报进度，finished 后回调 report_id 并停止轮询", async () => {
    vi.useFakeTimers();
    const fetchMock = stubStatusQueue([
      statusBody({ judging_step: 1 }),
      statusBody({ judging_step: 2 }),
      statusBody({ status: "finished", judging_step: 3, report_id: 9 }),
      statusBody({ status: "finished", report_id: 9 }), // 停止后不应再被消费
    ]);
    const onProgress = vi.fn();
    const onDone = vi.fn();
    const onError = vi.fn();
    const stop = pollJudging(1, { onProgress, onDone, onError });
    expect(fetchMock).not.toHaveBeenCalled(); // 首拍等 interval，不立即请求

    await vi.advanceTimersByTimeAsync(2000);
    expect(onProgress).toHaveBeenLastCalledWith(1, 3);
    await vi.advanceTimersByTimeAsync(2000);
    expect(onProgress).toHaveBeenLastCalledWith(2, 3);
    await vi.advanceTimersByTimeAsync(2000);
    expect(onDone).toHaveBeenCalledWith(9);
    expect(onError).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(6000); // 停止后不再发请求
    expect(fetchMock).toHaveBeenCalledTimes(3);
    stop();
  });

  it("轮询中途 in_progress（后台判题回滚）→ onError 并停止", async () => {
    vi.useFakeTimers();
    stubStatusQueue([statusBody({ judging_step: 1 }), statusBody({ status: "in_progress" })]);
    const onProgress = vi.fn();
    const onDone = vi.fn();
    const onError = vi.fn();
    pollJudging(2, { onProgress, onDone, onError });
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(2000);
    expect(onError).toHaveBeenCalledWith("判题中断，请重试");
    expect(onDone).not.toHaveBeenCalled();
  });

  it("请求失败 → onError 上屏错误并停止", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ detail: "会话不存在" }), { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    const onProgress = vi.fn();
    const onDone = vi.fn();
    const onError = vi.fn();
    pollJudging(3, { onProgress, onDone, onError });
    await vi.advanceTimersByTimeAsync(2000);
    expect(onError).toHaveBeenCalledWith("会话不存在");
    await vi.advanceTimersByTimeAsync(4000);
    expect(fetchMock).toHaveBeenCalledTimes(1); // 失败即停
  });

  it("停止函数清理定时器", async () => {
    vi.useFakeTimers();
    const fetchMock = stubStatusQueue([]);
    const stop = pollJudging(4, { onProgress: vi.fn(), onDone: vi.fn(), onError: vi.fn() });
    stop();
    await vi.advanceTimersByTimeAsync(6000);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
