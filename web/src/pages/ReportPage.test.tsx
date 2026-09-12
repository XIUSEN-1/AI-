import { afterEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import ReportPage from "./ReportPage";

/** 轻量组件冒烟：不引入 testing-library，react-dom/client + act 直渲 jsdom，原生事件驱动。 */
vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
// recharts ResponsiveContainer 依赖 ResizeObserver，jsdom 缺失需补桩（同 TeacherPage.test）
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

const REPORT_ID = 5;

const REPORT = {
  id: REPORT_ID,
  created_at: "2026-09-12T08:00:00Z",
  dimensions: Array.from({ length: 6 }, (_, i) => ({
    dimension: `D${i + 1}`,
    name: `维度${i + 1}`,
    theta: 3.0,
    level: 3,
    level_name: "胜任",
    answered: 4,
    correct: 3,
    percent: 75,
  })),
  total_level: 3,
  total_level_name: "胜任",
  radar: [],
  strengths: [],
  gaps: [],
  advice: ["建议一"],
  advice_source: "cell",
  advice_detail: [],
  answers: [],
};

/** 按 URL 路由的 fetch 替身：报告详情 + 历次报告列表 */
function stubReportFetch(mine: unknown[]) {
  const fetchMock = vi.fn(async (input: string | URL | Request) => {
    const url = String(input);
    const respond = (body: unknown): Response => new Response(JSON.stringify(body), { status: 200 });
    if (url === `/api/reports/${REPORT_ID}`) return respond(REPORT);
    if (url === "/api/reports/mine") return respond(mine);
    throw new Error(`未预期的请求: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function renderReport(mine: unknown[]) {
  stubReportFetch(mine);
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root!: Root;
  await act(async () => {
    root = createRoot(container);
    root.render(
      <MemoryRouter initialEntries={[`/report/${REPORT_ID}`]}>
        <Routes>
          <Route path="/report/:id" element={<ReportPage />} />
        </Routes>
      </MemoryRouter>,
    );
  });
  await flush();
  await flush(); // 报告详情与 mine 两个异步请求各跑一轮
  return { container, cleanup: () => { root.unmount(); container.remove(); } };
}

describe("ReportPage 冒烟", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  });

  it("历史 ≥2 条：渲染能力成长趋势折线卡与导出 PNG 按钮", async () => {
    const mine = [
      { id: 4, created_at: "2026-09-01T08:00:00Z", total_level: 2, total_level_name: "进阶", avg_percent: 60 },
      { id: REPORT_ID, created_at: "2026-09-12T08:00:00Z", total_level: 3, total_level_name: "胜任", avg_percent: 75 },
    ];
    const { container, cleanup } = await renderReport(mine);
    expect(container.textContent).toContain("能力成长趋势");
    expect(container.textContent).toContain("导出 PNG");
    expect(container.textContent).toContain("六维能力雷达");
    cleanup();
  });

  it("历史不足 2 条（仅当前报告自身）：不渲染趋势卡", async () => {
    const mine = [
      { id: REPORT_ID, created_at: "2026-09-12T08:00:00Z", total_level: 3, total_level_name: "胜任", avg_percent: 75 },
    ];
    const { container, cleanup } = await renderReport(mine);
    expect(container.textContent).not.toContain("能力成长趋势");
    expect(container.textContent).toContain("六维能力雷达");
    cleanup();
  });
});
