import { afterEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";

import PracticePage from "./PracticePage";

/** 轻量组件冒烟：不引入 testing-library，react-dom/client + act 直渲 jsdom，原生事件驱动。 */
vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);

const WRONG_ITEM = {
  question_id: 101,
  question_code: "D1-B01",
  dimension: "D1",
  dimension_name: "提问与信息检索",
  tags: ["补练"],
  wrong_count: 2,
};

const SESSION = {
  id: 7,
  dimension: "D1",
  tags: ["补练"],
  size: 2,
  question_ids: [101, 102],
  questions: [
    {
      id: 101, code: "D1-P01", dimension: "D1", dimension_name: "提问与信息检索",
      type: "single" as const, difficulty: 2, stem: "单选题：以下哪个做法更好？",
      options: [{ key: "A", text: "选项甲" }, { key: "B", text: "选项乙" }], est_seconds: 60, tags: ["补练"],
    },
    {
      id: 102, code: "D1-P02", dimension: "D1", dimension_name: "提问与信息检索",
      type: "judge" as const, difficulty: 1, stem: "判断题：上下文越长效果越好。",
      options: null, est_seconds: 30, tags: ["补练"],
    },
  ],
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200 });
}

/** 按 method+URL 路由的 fetch 替身：错题本 → 开卷 → 逐题判分（101 对 / 102 错）。 */
function stubPracticeFetch() {
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url === "/api/me/wrongbook") {
      return json({ items: [WRONG_ITEM], total: 1 });
    }
    if (url === "/api/practice/sessions" && method === "POST") {
      return json(SESSION);
    }
    if (url === "/api/practice/sessions/7/answer" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { question_id: number };
      return json(
        body.question_id === 101
          ? { question_id: 101, is_correct: true, explanation: "解析甲", answered: 1, total: 2 }
          : { question_id: 102, is_correct: false, explanation: "解析乙", answered: 2, total: 2 },
      );
    }
    throw new Error(`未预期的请求: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function stubEmptyWrongbookFetch() {
  const fetchMock = vi.fn(async () => json({ items: [], total: 0 }));
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

async function clickEl(container: HTMLElement, selector: string) {
  const el = container.querySelector(selector);
  if (!el) throw new Error(`元素不存在: ${selector}`);
  await act(async () => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  });
}

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function renderPage(initialEntries: string[] = ["/practice"]) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root!: Root;
  await act(async () => {
    root = createRoot(container);
    root.render(
      <MemoryRouter initialEntries={initialEntries}>
        <PracticePage />
      </MemoryRouter>,
    );
  });
  await flush();
  return { container, cleanup: () => { root.unmount(); container.remove(); } };
}

describe("PracticePage 冒烟", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); // afterEach 清掉 stub 后恢复 act 环境
  });

  it("全流程：错题本选卷 → 逐题作答即时对错反馈 → 结束总结 → 再来一卷", async () => {
    stubPracticeFetch();
    const { container, cleanup } = await renderPage();

    // 设置页：维度选项来自错题本
    expect(container.textContent).toContain("错题练习");
    expect(container.textContent).toContain("提问与信息检索 · 错题 1 题");
    await clickButton(container, "开始练习");
    await flush();

    // 第 1 题（单选）：选 A → 提交 → 即时对错 + 解析
    expect(container.textContent).toContain("单选题：以下哪个做法更好？");
    expect(container.textContent).toContain("第 1/2 题");
    await clickEl(container, "#A");
    await clickButton(container, "提交答案");
    await flush();
    expect(container.textContent).toContain("回答正确");
    expect(container.textContent).toContain("解析甲");

    // 第 2 题（判断）：答错 → 反馈错误 → 查看总结
    await clickButton(container, "下一题");
    await flush();
    expect(container.textContent).toContain("判断题：上下文越长效果越好。");
    await clickEl(container, "#judge-true");
    await clickButton(container, "提交答案");
    await flush();
    expect(container.textContent).toContain("回答错误");
    await clickButton(container, "查看总结");
    await flush();
    expect(container.textContent).toContain("答对 1/2");

    // 再来一卷：回到设置页（保留错题本数据）
    await clickButton(container, "再来一卷");
    await flush();
    expect(container.textContent).toContain("开始练习");
    expect(container.textContent).toContain("提问与信息检索 · 错题 1 题");
    cleanup();
  });

  it("空态：暂无错题时引导去完成正式测评", async () => {
    stubEmptyWrongbookFetch();
    const { container, cleanup } = await renderPage();
    expect(container.textContent).toContain("暂无错题");
    expect(container.textContent).toContain("去完成一次测评");
    cleanup();
  });
});
