import { afterEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";

import HomePage from "./HomePage";

/** 轻量组件冒烟：不引入 testing-library，react-dom/client + act 直渲 jsdom，原生事件驱动。 */
vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);

const ACTIVE_SESSION = {
  session_id: 9,
  stage: "dialog",
  progress: { D1: { n: 3 }, D2: { n: 2 } },
  started_at: "2026-09-12T08:00:00Z",
};

const WRONG_ITEMS = {
  items: [
    { dimension: "D1", dimension_name: "提问与信息检索", wrong_count: 2 },
    { dimension: "D1", dimension_name: "提问与信息检索", wrong_count: 1 },
  ],
  total: 2,
};

/** 按 URL 路由的 fetch 替身：me 决定角色场景，reports/wrongbook/active 为学员首页数据 */
function stubHomeFetch(me: { id: number; name: string; role: string }, active: unknown) {
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const respond = (body: unknown): Response => new Response(JSON.stringify(body), { status: 200 });
    if (url === "/api/auth/me") return respond(me);
    if (url === "/api/reports/mine") return respond([]);
    if (url === "/api/me/wrongbook") return respond(WRONG_ITEMS);
    if (url === "/api/sessions/active") return respond(active);
    throw new Error(`未预期的请求: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
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
      <MemoryRouter initialEntries={["/"]}>
        <HomePage />
      </MemoryRouter>,
    );
  });
  await flush();
  return { container, cleanup: () => { root.unmount(); container.remove(); } };
}

describe("HomePage 冒烟", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  });

  it("学员 + 进行中会话：显示继续测评卡片（阶段与已答进度）与错题口径", async () => {
    stubHomeFetch({ id: 1, name: "小明", role: "student" }, ACTIVE_SESSION);
    const { container, cleanup } = await renderPage();

    // 续答卡片：进行中会话 → 阶段中文 + 已答 5 题（3+2）
    expect(container.textContent).toContain("有一次未完成的测评");
    expect(container.textContent).toContain("对话式测评");
    expect(container.textContent).toContain("已答 5 题");
    expect(container.textContent).toContain("继续测评");

    // 错题口径：每条错题记录即一道去重题目，total = 去重题数
    expect(container.textContent).toContain("错题 2 题");
    // 短板维度"去练习"链接指向带维度参数的练习页
    const link = container.querySelector('a[href="/practice?dimension=D1"]');
    expect(link).not.toBeNull();
    expect(link!.textContent).toContain("去练习");
    cleanup();
  });

  it("学员无进行中会话：不显示续答卡片", async () => {
    stubHomeFetch({ id: 1, name: "小明", role: "student" }, null);
    const { container, cleanup } = await renderPage();
    expect(container.textContent).not.toContain("有一次未完成的测评");
    expect(container.textContent).toContain("开始一次测评");
    cleanup();
  });

  it("教师：只显示班级看板入口，不显示学员卡片", async () => {
    stubHomeFetch({ id: 2, name: "王老师", role: "teacher" }, null);
    const { container, cleanup } = await renderPage();
    expect(container.textContent).toContain("班级看板");
    expect(container.textContent).toContain("进入看板");
    expect(container.textContent).not.toContain("开始一次测评");
    expect(container.textContent).not.toContain("管理后台");
    cleanup();
  });

  it("管理员：显示题库后台/复核队列/创建教师入口", async () => {
    stubHomeFetch({ id: 3, name: "管理员", role: "admin" }, null);
    const { container, cleanup } = await renderPage();
    expect(container.textContent).toContain("管理后台");
    expect(container.textContent).toContain("题库后台");
    expect(container.textContent).toContain("复核队列");
    expect(container.textContent).toContain("创建教师");
    expect(container.textContent).not.toContain("开始一次测评");
    cleanup();
  });
});
