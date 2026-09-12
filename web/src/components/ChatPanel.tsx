import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { postSse } from "@/lib/sse";

/** 对话区消息：counterpart=考官/实操 AI 助手，learner=学员 */
export interface ChatMessage {
  role: "counterpart" | "learner";
  content: string;
}

interface ChatPanelProps {
  endpoint: string; // POST SSE 端点（dialog/turn 或 practical/chat）
  counterpartLabel: string;
  initialMessages: ChatMessage[];
  initialTurns: number;
  maxTurns: number;
  inputPlaceholder: string;
  streamingNote: string; // 流式接收期间发送按钮文案
  disabled?: boolean; // 外部禁用（如对话题已满轮等待切题）
  disabledHint?: string;
  onTurnDone?: (turns: number) => void;
}

const MAX_MESSAGE_LEN = 2000; // 后端 TurnIn/ChatIn 的 message 上限

export default function ChatPanel({
  endpoint,
  counterpartLabel,
  initialMessages,
  initialTurns,
  maxTurns,
  inputPlaceholder,
  streamingNote,
  disabled = false,
  disabledHint,
  onTurnDone,
}: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [turns, setTurns] = useState(initialTurns);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "nearest" });
  }, [messages, streaming]);

  function appendDelta(delta: string) {
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.role === "counterpart") {
        next[next.length - 1] = { ...last, content: last.content + delta };
      }
      return next;
    });
  }

  function dropEmptyCounterpart() {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      return last && last.role === "counterpart" && last.content === "" ? prev.slice(0, -1) : prev;
    });
  }

  async function send() {
    const text = input.trim();
    if (!text || streaming || disabled) return;
    setError("");
    setInput("");
    setMessages((prev) => [...prev, { role: "learner", content: text }, { role: "counterpart", content: "" }]);
    setStreaming(true);
    try {
      await postSse(endpoint, { message: text }, (ev) => {
        if (ev.error) {
          setError(ev.error); // SSE 错误事件：对话区内提示，不中断会话
          return;
        }
        if (ev.done) {
          const t = ev.turns ?? turns + 1;
          setTurns(t);
          setStreaming(false);
          onTurnDone?.(t);
          return;
        }
        if (ev.delta) appendDelta(ev.delta);
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "发送失败，请重试");
    } finally {
      setStreaming(false);
      dropEmptyCounterpart();
    }
  }

  const exhausted = disabled || turns >= maxTurns;
  const currentTurn = Math.min(turns + (streaming ? 1 : 0), maxTurns);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs text-slate-500">
        <span>{counterpartLabel}</span>
        <span>
          第 {currentTurn}/{maxTurns} 轮
        </span>
      </div>
      <div className="h-72 space-y-2 overflow-y-auto rounded-lg border bg-white p-3">
        {messages.map((m, i) => (
          <div key={i} className={`flex ${m.role === "learner" ? "justify-end" : "justify-start"}`}>
            <div
              className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm leading-relaxed ${
                m.role === "learner" ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-800"
              }`}
            >
              {m.content}
              {streaming && i === messages.length - 1 && m.role === "counterpart" && (
                <span className="animate-pulse">▍</span>
              )}
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      {error && <p className="text-xs text-red-600">{error}</p>}
      {exhausted ? (
        <p className="rounded-lg bg-slate-50 p-2 text-xs text-slate-500">
          {disabledHint ?? `已达 ${maxTurns} 轮上限`}
        </p>
      ) : (
        <div className="flex items-end gap-2">
          <Textarea
            rows={3}
            maxLength={MAX_MESSAGE_LEN}
            value={input}
            placeholder={inputPlaceholder}
            onChange={(e) => setInput(e.target.value)}
            disabled={streaming}
          />
          <Button onClick={send} disabled={!input.trim() || streaming}>
            {streaming ? streamingNote : "发送"}
          </Button>
        </div>
      )}
    </div>
  );
}
