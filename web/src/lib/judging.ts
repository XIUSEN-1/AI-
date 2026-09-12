import { api } from "./api";

export interface SessionStatus {
  status: string;
  stage: string;
  judging_step: number;
  judging_total: number;
  report_id?: number | null;
}

export interface JudgingHandlers {
  onProgress: (step: number, total: number) => void;
  onDone: (reportId: number) => void;
  onError: (message: string) => void;
}

/** 判题进度轮询：每 intervalMs 查一次 status。
 * finished → onDone 并停止；in_progress（后台判题异常回滚）或请求失败 → onError 并停止。
 * 返回停止函数（组件卸载时清理定时器）。 */
export function pollJudging(sessionId: number, handlers: JudgingHandlers, intervalMs = 2000): () => void {
  const stop = () => clearInterval(timer);
  const timer = setInterval(async () => {
    try {
      const st = await api<SessionStatus>(`/api/sessions/${sessionId}/status`);
      if (st.status === "finished") {
        stop();
        if (st.report_id != null) handlers.onDone(st.report_id);
        else handlers.onError("报告生成异常，请重试");
        return;
      }
      if (st.status === "in_progress") {
        stop();
        handlers.onError("判题中断，请重试");
        return;
      }
      handlers.onProgress(st.judging_step, st.judging_total);
    } catch (err) {
      stop();
      handlers.onError(err instanceof Error ? err.message : "进度查询失败，请重试");
    }
  }, intervalMs);
  return stop;
}
