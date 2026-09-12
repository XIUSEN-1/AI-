import { afterEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";

import TeacherPage from "./TeacherPage";

/** 轻量组件冒烟：不引入 testing-library，react-dom/client + act 直渲 jsdom，原生事件驱动。 */
vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);

// recharts ResponsiveContainer 依赖 ResizeObserver，jsdom 缺失需补桩
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

const CLASSES = [
  { id: 1, name: "一班", invite_code: "CA1B2C3", student_count: 2 },
  { id: 2, name: "二班", invite_code: "CD4E5F6", student_count: 0 },
];

const ANALYTICS = {
  student_count: 2,
  radar: [
    { dimension: "D1", label: "提问与信息检索", avg_percent: 80 },
    { dimension: "D2", label: "清晰表达", avg_percent: 60 },
    { dimension: "D3", label: "任务拆解", avg_percent: 70 },
    { dimension: "D4", label: "上下文构建", avg_percent: 50 },
    { dimension: "D5", label: "迭代与甄别", avg_percent: 40 },
    { dimension: "D6", label: "结果整合", avg_percent: 65 },
  ],
  dimension_rank: [
    { dimension: "D1", label: "提问与信息检索", avg_percent: 80 },
    { dimension: "D3", label: "任务拆解", avg_percent: 70 },
    { dimension: "D6", label: "结果整合", avg_percent: 65 },
    { dimension: "D2", label: "清晰表达", avg_percent: 60 },
    { dimension: "D4", label: "上下文构建", avg_percent: 50 },
    { dimension: "D5", label: "迭代与甄别", avg_percent: 40 },
  ],
  weakness_top3: [
    { dimension: "D5", tags: ["追问不足", "缺乏核验"] },
    { dimension: "D4", tags: [] },
    { dimension: "D2", tags: ["表达含糊"] },
  ],
  students: [
    { id: 11, name: "小明", student_no: "S001", last_level_name: "良好", reports: 2, trend_slope: 1.5 },
    { id: 12, name: "小红", student_no: "S002", last_level_name: null, reports: 0, trend_slope: 0 },
  ],
  timeline: [
    { date: "2026-09-10", avg_percent: 55.5 },
    { date: "2026-09-11", avg_percent: 66.0 },
  ],
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200 });
}

/** 按 URL 路由的 fetch 替身：班级列表 / 看板聚合 / 建班 */
function stubTeacherFetch() {
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url === "/api/teacher/classes" && method === "GET") return json(CLASSES);
    if (/^\/api\/teacher\/classes\/\d+\/analytics$/.test(url) && method === "GET") return json(ANALYTICS);
    if (url === "/api/teacher/classes" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { name: string };
      return json({ id: 3, name: body.name, invite_code: "CFFFFFF" }); // 与后端一致：响应不含 student_count
    }
    throw new Error(`未预期的请求: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function clickButton(container: HTMLElement, text: string) {
  const btn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes(text));
  if (!btn) throw new Error(`按钮不存在: ${text}；当前内容: ${container.textContent}`);
  await act(async () => {
    btn.click();
  });
}

/** 通过原型 value setter 更新受控输入（同步 React 内部 value tracker）再派发原生事件 */
async function setInputValue(container: HTMLElement, selector: string, value: string) {
  const input = container.querySelector(selector) as HTMLInputElement;
  if (!input) throw new Error(`输入框不存在: ${selector}`);
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function renderPage() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root!: Root;
  await act(async () => {
    root = createRoot(container);
    root.render(
      <MemoryRouter initialEntries={["/teacher"]}>
        <TeacherPage />
      </MemoryRouter>,
    );
  });
  await flush();
  return { container, cleanup: () => { root.unmount(); container.remove(); } };
}

describe("TeacherPage 冒烟", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  });

  it("渲染班级下拉与看板聚合：雷达/排名/短板/学员表/曲线/导出", async () => {
    const fetchMock = stubTeacherFetch();
    const { container, cleanup } = await renderPage();

    // 班级下拉默认选第一个，显示邀请码
    expect(container.textContent).toContain("一班（2 人）");
    expect(container.textContent).toContain("邀请码 CA1B2C3");

    // 看板卡片：维度排名（最强）、短板 TOP3 标签、学员表（最近等级/趋势）、时间线
    expect(container.textContent).toContain("六维均值雷达");
    expect(container.textContent).toContain("维度排名");
    expect(container.textContent).toContain("共性短板 TOP3");
    expect(container.textContent).toContain("追问不足");
    expect(container.textContent).toContain("学员概览（2 人）");
    expect(container.textContent).toContain("小明");
    expect(container.textContent).toContain("良好");
    expect(container.textContent).toContain("↑ +1.5");
    expect(container.textContent).toContain("→ 持平");
    expect(container.textContent).toContain("班级成长曲线");
    expect(container.textContent).toContain("导出 CSV");
    expect(
      fetchMock.mock.calls.some(([input]) => String(input) === "/api/teacher/classes/1/analytics"),
    ).toBe(true);
    cleanup();
  });

  it("建班：填写名称创建后进入新班级", async () => {
    const fetchMock = stubTeacherFetch();
    const { container, cleanup } = await renderPage();

    await clickButton(container, "创建班级");
    await setInputValue(container, "#class-name", "三班");
    await clickButton(container, "确认创建");
    await flush();

    const called = fetchMock.mock.calls.some(
      ([input, init]) => String(input) === "/api/teacher/classes" && init?.method === "POST",
    );
    expect(called).toBe(true);
    expect(container.textContent).toContain("三班（0 人）");
    expect(container.textContent).toContain("邀请码 CFFFFFF");
    await flush(); // 等新班级的 analytics 拉取完成后干净卸载
    cleanup();
  });
});
