import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await page
    .getByLabel("访问令牌", { exact: true })
    .fill("browser-test-token-123456789");
  await page.getByRole("button", { name: "进入控制台" }).click();
  await expect(
    page.getByRole("heading", { name: "服务运行总览" }),
  ).toBeVisible();
});

test("overview filters and schema-driven JSON/SSE console", async ({
  page,
}) => {
  await expect(page.locator(".instance-card")).toHaveCount(2);
  await expect(page.locator(".chart-empty")).toHaveCount(0);
  await page.screenshot({
    path: "test-results/overview-desktop.png",
    fullPage: true,
  });
  const card = page.locator(".instance-card").filter({ hasText: "Staging" });
  await card.getByTitle("展开接口").click();
  await expect(card.locator(".endpoint-row")).toHaveCount(3);
  await card
    .locator(".endpoint-row")
    .filter({ hasText: "/debug/bm25" })
    .getByRole("button", { name: "试调" })
    .click();
  const dialog = page.getByRole("dialog", { name: "接口试调" });
  await expect(dialog.getByLabel("topK", { exact: false })).toHaveValue("50");
  await dialog.getByLabel("query", { exact: false }).fill("北京天气");
  await dialog.getByRole("button", { name: "发送请求" }).click();
  await expect(dialog.locator(".response-section pre")).toContainText(
    "北京天气",
  );
  await expect(dialog.locator(".response-section pre")).toContainText(
    "console",
  );
  await page.screenshot({ path: "test-results/console.png", fullPage: true });
  await dialog.getByLabel("关闭").click();
  await card
    .locator(".endpoint-row")
    .filter({ hasText: "/stream" })
    .getByRole("button", { name: "试调" })
    .click();
  await page.getByRole("button", { name: "发送请求" }).click();
  await expect(page.locator(".response-section pre")).toContainText(
    '"done":true',
  );
  await page.getByLabel("关闭").click();
  await page.getByPlaceholder("搜索名称、地址或版本").fill("Staging");
  await expect(page.locator(".instance-card")).toHaveCount(1);
});

test("logs, database, failure trace and download", async ({ page }) => {
  const card = page.locator(".instance-card").filter({ hasText: "Staging" });
  await card.getByTitle("更多操作").click();
  await card.getByRole("button", { name: "查看日志", exact: true }).click();
  await expect(page.locator(".log-view")).toContainText(
    "Contract fixture is ready",
  );
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByRole("button", { name: "下一页" })).toHaveCount(0);
  await page.getByLabel("关闭").click();
  await card.getByTitle("更多操作").click();
  await card.getByRole("button", { name: "查看数据库", exact: true }).click();
  await page.getByLabel("数据表", { exact: true }).selectOption("samples");
  await expect(page.locator("table")).toContainText("天气如何");
  await page.getByLabel("关闭").click();
  await card.getByTitle("更多操作").click();
  await card.getByRole("button", { name: "失败请求" }).click();
  await page.locator(".request-list button").first().click();
  await page.getByRole("button", { name: "读取 Trace" }).click();
  await expect(page.locator(".detail-block pre")).toContainText('"event"');
  await page.getByLabel("关闭").click();
  await card.getByTitle("更多操作").click();
  await card.getByRole("button", { name: "回流数据库" }).click();
  await expect(page.getByRole("button", { name: "下载回流文件" })).toBeVisible({
    timeout: 15000,
  });
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载回流文件" }).click();
  expect((await downloaded).suggestedFilename()).toBe("snapshot.sqlite");
});

test("deployment form, disabled extensions and background task", async ({
  page,
}) => {
  await page
    .locator(".page-heading")
    .getByRole("button", { name: "新增实例", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "部署一个新实例" });
  await dialog.getByRole("button", { name: "Kubernetes 指定节点" }).click();
  await expect(
    dialog.getByRole("button", { name: "多节点 · 暂不支持" }),
  ).toBeDisabled();
  await dialog.getByRole("button", { name: "Docker 加载镜像" }).click();
  await dialog
    .getByLabel("实例名称", { exact: true })
    .fill("Browser deployment");
  await dialog
    .getByLabel("远程环境", { exact: true })
    .selectOption("fixture-env");
  await dialog.getByLabel("镜像 tar 包", { exact: true }).setInputFiles({
    name: "browser-image.tar",
    mimeType: "application/x-tar",
    buffer: Buffer.from("fixture image"),
  });
  await dialog.getByLabel("暴露端口", { exact: true }).fill("32999");
  await dialog
    .getByLabel("服务访问地址", { exact: true })
    .fill("http://localhost:32883");
  await dialog
    .getByLabel("Mock 管理令牌", { exact: true })
    .fill("contract-mock-token-123456789");
  await dialog.getByRole("button", { name: "开始部署" }).click();
  await expect(page.getByRole("dialog", { name: "任务详情" })).toBeVisible();
  await expect(page.locator(".task-log")).toContainText(
    "操作完成，实例已就绪",
    { timeout: 15000 },
  );
  await page.getByLabel("关闭").click();
  await expect(
    page.locator(".instance-card").filter({ hasText: "Browser deployment" }),
  ).toBeVisible();
});

test("mobile layout and environment form", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/overview-mobile.png",
    fullPage: true,
  });
  await page.locator("nav button").last().click();
  await page
    .getByRole("button", { name: "新增环境", exact: true })
    .first()
    .click();
  await expect(
    page.getByRole("button", { name: "Windows · 暂不支持" }),
  ).toBeDisabled();
  await page
    .getByLabel("环境名称", { exact: true })
    .fill("Browser environment");
  await page.getByLabel("SSH 主机", { exact: true }).fill("127.0.0.1");
  await page.getByLabel("SSH 用户", { exact: true }).fill("tester");
  await page.getByLabel("密码", { exact: true }).fill("not-a-real-secret");
  await expect(
    page.getByLabel("可信主机 SHA256 指纹（可选）", { exact: true }),
  ).not.toHaveAttribute("required");
  await page.getByRole("button", { name: "保存环境" }).click();
  await expect(
    page.getByRole("heading", { name: "Browser environment" }),
  ).toBeVisible();
});

test("attach existing service and remove monitoring without a deployment", async ({
  page,
}) => {
  await page
    .locator(".page-heading")
    .getByRole("button", { name: "新增实例", exact: true })
    .click();
  await page.getByRole("button", { name: "监测已有实例", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "监测已有实例" });
  await dialog.getByLabel("实例名称", { exact: true }).fill("External service");
  await dialog
    .getByLabel("服务访问地址", { exact: true })
    .fill("http://127.0.0.2:32883");
  await dialog
    .getByLabel("Mock 管理令牌", { exact: true })
    .fill("contract-mock-token-123456789");
  await dialog.getByRole("button", { name: "验证并接入" }).click();
  const card = page
    .locator(".instance-card")
    .filter({ hasText: "External service" });
  await expect(card).toBeVisible();
  await expect(card.getByTitle("重启实例")).toBeDisabled();
  await card.getByTitle("更多操作").click();
  await expect(
    card.getByRole("button", { name: "卸载实例", exact: true }),
  ).toBeDisabled();
  await card.getByRole("button", { name: "移除监测", exact: true }).click();
  await page.getByRole("button", { name: "确认移除监测" }).click();
  await expect(card).toHaveCount(0);
});

test("Docker advanced parameters and uninstall confirmation", async ({
  page,
}) => {
  await page
    .locator(".page-heading")
    .getByRole("button", { name: "新增实例", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "部署一个新实例" });
  await dialog.getByRole("button", { name: "Docker 加载镜像" }).click();
  await expect(dialog.getByLabel("远程部署根目录")).toHaveValue(
    "/opt/svc-panel",
  );
  await expect(dialog.getByLabel("失败最多重启次数")).toHaveValue("3");
  await dialog.getByLabel("启动方式", { exact: true }).selectOption("custom");
  await expect(
    dialog.getByLabel("ENTRYPOINT（可选，可执行文件路径）"),
  ).toBeVisible();
  await dialog
    .getByText("环境变量、挂载与更多 Docker 参数", { exact: true })
    .click();
  await expect(dialog.getByLabel("Docker 参数（JSON 数组）")).toBeVisible();
  await page.screenshot({
    path: "test-results/docker-options.png",
    fullPage: true,
  });
  await dialog.getByLabel("关闭").click();
  const card = page.locator(".instance-card").filter({ hasText: "Staging" });
  await card.getByTitle("更多操作").click();
  await card.getByRole("button", { name: "卸载实例", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "确认卸载", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("输入实例名称确认").fill("wrong");
  await expect(
    page.getByRole("button", { name: "确认卸载", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("输入实例名称确认").fill("Staging · 契约测试");
  await expect(
    page.getByRole("button", { name: "确认卸载", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "取消", exact: true }).click();
});

test("delete an unused environment and explain in-use dependencies", async ({
  page,
}) => {
  const headers = { Authorization: "Bearer browser-test-token-123456789" };
  const created = await page.request.post("/api/environments", {
    headers,
    data: {
      name: "Environment to delete",
      host: "127.0.0.1",
      username: "test",
      password: "disposable-ssh-password",
    },
  });
  expect(created.status()).toBe(201);
  await page.locator("nav").getByRole("button", { name: "远程环境" }).click();
  await page.getByTitle("刷新", { exact: true }).click();
  const card = page
    .locator(".environment-card")
    .filter({ hasText: "Environment to delete" });
  await card.getByRole("button", { name: "删除环境", exact: true }).click();
  await page.getByRole("button", { name: "确认删除环境", exact: true }).click();
  await expect(card).toHaveCount(0);
  const referenced = page
    .locator(".environment-card")
    .filter({ hasText: "本地契约测试环境" });
  await referenced
    .getByRole("button", { name: "删除环境", exact: true })
    .click();
  await page.getByRole("button", { name: "确认删除环境", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "删除远程环境" }).getByRole("alert"),
  ).toContainText("环境仍被实例或未完成任务使用");
  await expect(
    page.getByRole("dialog", { name: "删除远程环境" }).getByRole("alert"),
  ).toContainText("Staging");
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(referenced).toBeVisible();
});

test("change admin password, reject old credential and sign in again", async ({
  page,
}) => {
  const old = "browser-test-token-123456789",
    next = "browser-new-password-123456789";
  let changed = false;
  try {
    await page.getByRole("button", { name: "设置与帮助", exact: true }).click();
    await page
      .getByRole("button", { name: "修改登录密码", exact: true })
      .click();
    const dialog = page.getByRole("dialog", { name: "修改登录密码" });
    await dialog
      .getByLabel("当前密码 / 访问令牌", { exact: true })
      .fill("wrong-current-password");
    await dialog.getByLabel("新登录密码", { exact: true }).fill(next);
    await dialog.getByLabel("再次输入新密码", { exact: true }).fill(next);
    await dialog.getByRole("button", { name: "保存新密码" }).click();
    await expect(dialog.getByRole("alert")).toContainText(
      "当前登录密码或访问令牌不正确",
    );
    await dialog.getByLabel("当前密码 / 访问令牌", { exact: true }).fill(old);
    await dialog.getByRole("button", { name: "保存新密码" }).click();
    await expect(
      page.getByRole("heading", { name: "进入服务控制台" }),
    ).toBeVisible();
    changed = true;
    expect(
      await page.evaluate(() => sessionStorage.getItem("panel-token")),
    ).toBeNull();
    expect(
      (
        await page.request.get("/api/session", {
          headers: { Authorization: "Bearer " + old },
        })
      ).status(),
    ).toBe(401);
    await page.getByLabel("访问令牌", { exact: true }).fill(next);
    await page.getByRole("button", { name: "进入控制台" }).click();
    await expect(
      page.getByRole("heading", { name: "工作空间设置" }),
    ).toBeVisible();
    await page.screenshot({
      path: "test-results/account-settings.png",
      fullPage: true,
    });
  } finally {
    if (changed) {
      const restored = await page.request.post("/api/account/password", {
        headers: { Authorization: "Bearer " + next },
        data: {
          current_password: next,
          new_password: old,
          confirm_password: old,
        },
      });
      expect(restored.status()).toBe(200);
    }
  }
});
