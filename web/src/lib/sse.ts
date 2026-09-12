import { ApiError, getToken } from "./api";

/** 后端 SSE 数据事件形状（dialog/turn 与 practical/chat 共用） */
export interface SseEvent {
  delta?: string;
  done?: boolean;
  turns?: number;
  error?: string;
}

/** 解析 buffer 中全部完整帧（\n\n 分隔，data: 前缀，JSON 解析，坏帧跳过），返回未完成的尾部。 */
function emitFrames(buffer: string, onEvent: (event: SseEvent) => void): string {
  let idx: number;
  while ((idx = buffer.indexOf("\n\n")) >= 0) {
    const frame = buffer.slice(0, idx);
    buffer = buffer.slice(idx + 2);
    const data = frame
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data) continue;
    try {
      onEvent(JSON.parse(data) as SseEvent);
    } catch {
      // 坏帧跳过：不中断后续事件
    }
  }
  return buffer;
}

/** POST + fetch 流式读取 SSE（EventSource 不支持 POST）。读取完毕 resolve，异常 reject。 */
export async function postSse(
  path: string,
  body: unknown,
  onEvent: (event: SseEvent) => void,
): Promise<void> {
  const headers = new Headers({ "Content-Type": "application/json" });
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const resp = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  if (!resp.ok || !resp.body) {
    let detail = `请求失败（${resp.status}）`;
    try {
      const data = (await resp.json()) as { detail?: string };
      if (data?.detail) detail = String(data.detail);
    } catch {
      // 非 JSON 响应，用默认文案
    }
    throw new ApiError(resp.status, detail);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer = emitFrames(buffer + decoder.decode(value, { stream: true }), onEvent);
  }
  emitFrames(buffer + decoder.decode() + "\n\n", onEvent); // 收尾：无 \n\n 结尾的尾帧也解析
}
