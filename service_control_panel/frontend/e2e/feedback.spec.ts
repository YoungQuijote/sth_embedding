import { test, expect } from "@playwright/test";

const headers = { Authorization: "Bearer browser-test-token-123456789" };
const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);
async function signIn(page: any) {
  await page.goto("/");
  await page
    .getByLabel("访问令牌", { exact: true })
    .fill("browser-test-token-123456789");
  await page.getByRole("button", { name: "进入控制台" }).click();
  await expect(
    page.getByRole("heading", { name: "服务运行总览" }),
  ).toBeVisible();
}

test("feedback board create, image persistence, edit, filtering and delete", async ({
  page,
}) => {
  await signIn(page);
  const top = page.locator(".top-tools");
  await expect(
    top.getByRole("button", { name: "需求与反馈", exact: true }),
  ).toBeVisible();
  await top.getByRole("button", { name: "需求与反馈", exact: true }).click();
  const board = page.getByRole("dialog", { name: "需求与反馈", exact: true });
  await board.getByRole("button", { name: "新增需求 / 反馈" }).click();
  let detail = page.getByRole("dialog", {
    name: "新增需求与反馈",
    exact: true,
  });
  await expect(detail.getByLabel("处理状态", { exact: true })).toHaveValue(
    "pending",
  );
  await detail
    .getByLabel("需求简述（必填）", { exact: false })
    .fill("浏览器需求：增加耗时分布图");
  await detail
    .getByLabel("提出人（选填）", { exact: false })
    .fill("测试同学 🚀");
  await detail
    .getByLabel("详细描述（选填）", { exact: false })
    .fill("希望按接口查看耗时分布。\n保留文字中的 <script> 作为普通文本。");
  await detail.getByLabel("添加配图（选填）", { exact: true }).setInputFiles({
    name: "需求截图.png",
    mimeType: "image/png",
    buffer: png,
  });
  await expect(detail.getByAltText("需求截图.png")).toBeVisible();
  await detail.getByRole("button", { name: "保存需求与反馈" }).click();
  detail = page.getByRole("dialog", { name: "需求与反馈详情", exact: true });
  await expect(
    detail.getByRole("heading", {
      name: "浏览器需求：增加耗时分布图",
      exact: true,
    }),
  ).toBeVisible();
  await expect(detail.getByAltText("需求截图.png")).toBeVisible();
  const all = await (
    await page.request.get("/api/feedback", { headers })
  ).json();
  const record = all.items.find(
    (item: any) => item.summary === "浏览器需求：增加耗时分布图",
  );
  expect(record).toBeTruthy();
  const createdTime = record.created_at;
  await page.screenshot({
    path: "test-results/feedback-detail.png",
    fullPage: true,
  });
  await detail.getByLabel("关闭", { exact: true }).click();
  await expect(
    board.locator(".feedback-card").filter({ hasText: "浏览器需求" }),
  ).toBeVisible();
  await board.getByLabel("筛选状态", { exact: true }).selectOption("closed");
  await expect(
    board.locator(".feedback-card").filter({ hasText: "浏览器需求" }),
  ).toHaveCount(0);
  await board.getByLabel("筛选状态", { exact: true }).selectOption("");
  await board
    .locator(".feedback-card")
    .filter({ hasText: "浏览器需求" })
    .click();
  await page
    .getByRole("dialog", { name: "需求与反馈详情", exact: true })
    .getByRole("button", { name: "编辑", exact: true })
    .click();
  detail = page.getByRole("dialog", { name: "编辑需求与反馈", exact: true });
  await detail
    .getByLabel("处理状态", { exact: true })
    .selectOption("developing");
  await detail
    .getByLabel("详细描述（选填）", { exact: false })
    .fill("开发中，补充说明。");
  await detail.getByRole("button", { name: "保存需求与反馈" }).click();
  detail = page.getByRole("dialog", { name: "需求与反馈详情", exact: true });
  await expect(detail.locator(".feedback-status")).toHaveText("开发中");
  const changed = await (
    await page.request.get("/api/feedback/" + record.id, { headers })
  ).json();
  expect(changed.created_at).toBe(createdTime);
  expect(changed.status_updated_at).toBeGreaterThan(createdTime);
  await detail.getByLabel("关闭", { exact: true }).click();
  await board.getByLabel("关闭需求与反馈", { exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: "需求与反馈", exact: true }).click();
  await page
    .getByRole("dialog", { name: "需求与反馈", exact: true })
    .locator(".feedback-card")
    .filter({ hasText: "浏览器需求" })
    .click();
  detail = page.getByRole("dialog", { name: "需求与反馈详情", exact: true });
  await expect(detail.getByAltText("需求截图.png")).toBeVisible();
  await expect(detail.locator(".feedback-description")).toContainText(
    "开发中，补充说明。",
  );
  await detail.getByRole("button", { name: "编辑", exact: true }).click();
  detail = page.getByRole("dialog", { name: "编辑需求与反馈", exact: true });
  await detail.getByTitle("移除配图 需求截图.png").click();
  await detail.getByRole("button", { name: "保存需求与反馈" }).click();
  detail = page.getByRole("dialog", { name: "需求与反馈详情", exact: true });
  await expect(detail.getByAltText("需求截图.png")).toHaveCount(0);
  const textOnly = await (
    await page.request.get("/api/feedback/" + record.id, { headers })
  ).json();
  expect(textOnly.status_updated_at).toBe(changed.status_updated_at);
  await detail.getByRole("button", { name: "删除", exact: true }).click();
  await detail.getByRole("button", { name: "取消删除", exact: true }).click();
  await expect(detail).toBeVisible();
  await detail.getByRole("button", { name: "删除", exact: true }).click();
  await detail.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(detail).toHaveCount(0);
  await expect(
    page.locator(".feedback-card").filter({ hasText: "浏览器需求" }),
  ).toHaveCount(0);
  expect(
    (
      await page.request.get("/api/feedback/" + record.id, { headers })
    ).status(),
  ).toBe(404);
});

test("feedback sorting, mobile layout, Unicode limits and nested escape", async ({
  page,
}) => {
  await signIn(page);
  const a = await (
    await page.request.post("/api/feedback", {
      headers,
      data: { summary: "排序样例 A" },
    })
  ).json();
  const b = await (
    await page.request.post("/api/feedback", {
      headers,
      data: { summary: "排序样例 B" },
    })
  ).json();
  const updated = await page.request.put("/api/feedback/" + a.id, {
    headers,
    data: { summary: a.summary, revision: a.revision, status: "closed" },
  });
  expect(updated.status()).toBe(200);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "需求与反馈", exact: true }).click();
  const board = page.getByRole("dialog", { name: "需求与反馈", exact: true });
  for (const [sort, expected] of [
    ["created_asc", "排序样例 A"],
    ["created_desc", "排序样例 B"],
    ["status_asc", "排序样例 B"],
    ["status_desc", "排序样例 A"],
  ]) {
    await board.getByLabel("排序方式", { exact: true }).selectOption(sort);
    await expect(board.locator(".feedback-card").first()).toContainText(
      expected,
    );
  }
  await page.screenshot({
    path: "test-results/feedback-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await board.getByRole("button", { name: "新增需求 / 反馈" }).click();
  const detail = page.getByRole("dialog", {
    name: "新增需求与反馈",
    exact: true,
  });
  await detail
    .getByLabel("需求简述（必填）", { exact: false })
    .fill("x".repeat(257));
  await detail.getByRole("button", { name: "保存需求与反馈" }).click();
  await expect(detail.getByRole("alert")).toContainText("简述最多 256 字");
  await detail
    .getByLabel("需求简述（必填）", { exact: false })
    .fill("Unicode 长度验收");
  await detail
    .getByLabel("提出人（选填）", { exact: false })
    .fill("🚀".repeat(16));
  await detail.getByRole("button", { name: "保存需求与反馈" }).click();
  const saved = page.getByRole("dialog", {
    name: "需求与反馈详情",
    exact: true,
  });
  await expect(saved.locator(".feedback-meta")).toContainText("🚀".repeat(16));
  await page.keyboard.press("Escape");
  await expect(saved).toHaveCount(0);
  await expect(board).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(board).toHaveCount(0);
  const items = await (
    await page.request.get("/api/feedback", { headers })
  ).json();
  for (const item of items.items)
    await page.request.delete(
      `/api/feedback/${item.id}?revision=${item.revision}`,
      { headers },
    );
});
