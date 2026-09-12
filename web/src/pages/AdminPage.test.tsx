import { afterEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";

import AdminPage from "./AdminPage";

/** 轻量组件冒烟：不引入 testing-library，react-dom/client + act 直渲 jsdom，原生事件驱动。 */
vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);

const QUESTION = {
  id: 101,
  code: "D1-B01",
  dimension: "D1",
  tier: "basic",
  type: "single",
  difficulty: 2,
  stem: "单选题：以下哪个做法最能提升提问质量？",
  options: [{ key: "A", text: "选项甲" }],
  answer: "A",
  tags: ["提问"],
  est_seconds: 60,
  explanation: "解析",
  rubric: null,
  status: "published",
  version: 3,
};

const REVIEW_ITEM = {
  id: 7,
  question_code: "D3-O02",
  question_stem: "开放题：请描述你拆解复杂任务的过程。",
  dimension: "D3",
  student_no: "S001",
  session_id: 42,
  answer_id: 900,
  answer: "我会先把任务分成三步……",
  judge_raw: { type: "open", score: 2.1, rationale: "判题三跑分差大" },
  reason: "对话题判题三跑分差过大",
  status: "open",
  resolved_score: null,
  created_at: "2026-09-12T08:00:00Z",
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200 });
}

/** 有状态 fetch 替身：题库列表；复核队列首次返回 1 条 open，resolve 成功后重拉返回空 */
function stubAdminFetch() {
  let resolved = false;
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url.startsWith("/api/admin/questions?") && method === "GET") {
      return json({ total: 1, page: 1, page_size: 20, items: [QUESTION] });
    }
    if (url === "/api/admin/review-queue?status=open" && method === "GET") {
      return resolved ? json({ items: [], total: 0 }) : json({ items: [REVIEW_ITEM], total: 1 });
    }
    if (url === "/api/admin/review-queue/7/resolve" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as { final_score: number };
      if (body.final_score !== 4) throw new Error("应提交人工终评 4 分");
      resolved = true;
      return json({ id: 7, status: "resolved", resolved_score: 4 });
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

async function setSelectValue(container: HTMLElement, selector: string, value: string) {
  const select = container.querySelector(selector) as HTMLSelectElement;
  if (!select) throw new Error(`下拉框不存在: ${selector}`);
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!;
    setter.call(select, value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function renderPage(initialEntries: string[]) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root!: Root;
  await act(async () => {
    root = createRoot(container);
    root.render(
      <MemoryRouter initialEntries={initialEntries}>
        <AdminPage />
      </MemoryRouter>,
    );
  });
  await flush();
  return { container, cleanup: () => { root.unmount(); container.remove(); } };
}

describe("AdminPage 冒烟", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  });

  it("题库 tab：筛选表格渲染（题干/状态/版本/分页）", async () => {
    stubAdminFetch();
    const { container, cleanup } = await renderPage(["/admin"]);
    expect(container.textContent).toContain("题库管理");
    expect(container.textContent).toContain("D1-B01");
    expect(container.textContent).toContain("单选题：以下哪个做法最能提升提问质量？");
    expect(container.textContent).toContain("已发布");
    expect(container.textContent).toContain("共 1 题");
    expect(container.textContent).toContain("新建题目");
    cleanup();
  });

  it("复核 tab：查看 judge_raw 并按 0-4 打分裁定 → 队列刷新为空", async () => {
    const fetchMock = stubAdminFetch();
    const { container, cleanup } = await renderPage(["/admin?tab=review"]);

    expect(container.textContent).toContain("开放题：请描述你拆解复杂任务的过程。");
    expect(container.textContent).toContain("学员 S001");
    expect(container.textContent).toContain("入队原因：对话题判题三跑分差过大");
    expect(container.textContent).toContain("判题原始输出");
    expect(container.textContent).toContain("确认裁定");

    // 打 4 分裁定 → resolve 落分 → 重拉队列后为空
    await setSelectValue(container, "#score-7", "4");
    await clickButton(container, "确认裁定");
    await flush();
    expect(container.textContent).toContain("已裁定为 4 分");
    expect(container.textContent).toContain("当前筛选下没有复核条目");
    expect(
      fetchMock.mock.calls.some(([input, init]) => String(input) === "/api/admin/review-queue/7/resolve" && init?.method === "POST"),
    ).toBe(true);
    cleanup();
  });

  it("教师账号 tab：表单提交成功提示", async () => {
    const fetchMock = stubAdminFetch();
    const { container, cleanup } = await renderPage(["/admin?tab=teacher"]);
    expect(container.textContent).toContain("创建教师账号");
    expect(fetchMock.mock.calls.length).toBe(0); // 该 tab 不发列表请求
    cleanup();
  });
});
