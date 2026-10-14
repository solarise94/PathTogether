/**
 * Viewer 文件夹体验 E2E（folder-experience 2026-10-13，真实浏览器验收）。
 *
 * 与 import-project-upgrade.spec.ts 同基座：playwright.config.ts 的 webServer
 * （tests/e2e/e2e_server.py：真实 Flask + 内嵌 PostgreSQL + 一次性凭据），
 * 登录真实 /login 后在真实 /app 工作区验证；不用 page.route mock 后端。
 *
 * 覆盖（docs/followup/folder-experience-implementation-20261013.md 用户路径）：
 *   1. 根目录翻到第 2 页 → 刷新 → 停在同一位置（位置记忆恢复）；
 *   2. 在文件夹内打开导入抽屉 → 目标默认当前文件夹（服务端 can_write）；
 *   3. 深链 ?slide=<另一文件夹内切片> → 侧栏定位该切片真实位置（深链 > 记忆）。
 * 截图写入 E2E_SHOT_DIR（本机运行按工作区约定落在 /mnt/ssd2/tests/<run>/）。
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
  process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
  "utf8") as string) as {
  baseUrl: string; ownerLogin: string; ownerPassword: string;
  userLogin: string; userPassword: string;
};
const SHOT_DIR = process.env.E2E_SHOT_DIR || join(tmpdir(), "gui-test-screenshots");
const RUN = "fx" + Date.now().toString(36);

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
});

async function login(page: Page, loginId: string, password: string) {
  await page.goto("/login");
  await page.fill('form[action="/login"] input[name="username"]', loginId);
  await page.fill('form[action="/login"] input[name="password"]', password);
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes("/login")),
    page.click('form[action="/login"] button[type="submit"]'),
  ]);
}

async function csrfToken(page: Page): Promise<string> {
  const cookies = await page.context().cookies();
  const tok = cookies.find((c) => c.name === "csrf_token")?.value;
  if (!tok) throw new Error("csrf_token cookie missing after login");
  return tok;
}

async function api(
  page: Page,
  method: string,
  path: string,
  opts: { json?: unknown; headers?: Record<string, string> } = {},
) {
  const headers: Record<string, string> = {
    "X-CSRF-Token": await csrfToken(page),
    ...(opts.headers || {}),
  };
  if (opts.json !== undefined) headers["Content-Type"] = "application/json";
  const resp = await page.request.fetch(CREDS.baseUrl + path, {
    method,
    headers,
    data: opts.json === undefined ? undefined : JSON.stringify(opts.json),
  });
  expect(resp.status(), `${method} ${path}`).toBeLessThan(300);
  return (await resp.json()) as never;
}

/** 展开默认收起的桌面侧栏（收起时 #fb-stack 不可见，先看折叠态再展开）。 */
async function expandSidebar(page: Page) {
  const collapsed = await page.locator("#sidebar").evaluate((el) =>
    el.classList.contains("collapsed"),
  );
  if (collapsed) await page.locator("#menu-btn").click();
  await expect(page.locator("#fb-location")).toBeVisible({ timeout: 15_000 });
  await expect(page.locator("#fb-stack")).toBeVisible();
}

test.describe("folder-experience：刷新恢复 / 导入默认目标 / 深链定位", () => {
  let folderA = "";
  let folderB = "";
  let slideInB = "";

  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage();
    await login(page, CREDS.userLogin, CREDS.userPassword);
    const tok = await csrfToken(page);
    void tok;
    folderA = (await api(page, "POST", "/api/project/create", {
      json: { name: RUN + "-肺组织" },
    } as never) as { pid: string }).pid;
    folderB = (await api(page, "POST", "/api/project/create", {
      json: { name: RUN + "-研究" },
    } as never) as { pid: string }).pid;
    const slides = (await api(page, "GET", "/api/slides") as Array<{
      slide_id: string; name: string | null; original_filename?: string | null;
    }>);
    const fbSlide = slides.find((s) =>
      (s.name || s.original_filename || "").startsWith("e2e-fb-01"));
    expect(fbSlide, "seeded e2e-fb-01.bmp present").toBeTruthy();
    slideInB = fbSlide!.slide_id;
    await api(page, "POST", "/api/project/" + folderB + "/slides", {
      json: { slide_ids: [slideInB] },
    });
    await page.close();
  });

  test("刷新恢复目录与页码；文件夹内导入抽屉默认当前文件夹；深链定位优先", async ({ page }) => {
    test.setTimeout(90_000);
    await login(page, CREDS.userLogin, CREDS.userPassword);
    await page.goto("/app");
    await expandSidebar(page);

    // —— 根目录：≥2 页（10 张未归类 + 2 个文件夹卡，每叠 ≤8）——
    // 页总数随侧栏展开时的可用高度而变（既有装箱行为），断言只锚页码。
    const pageInfo = page.locator("#fb-page-info");
    await expect(pageInfo).toContainText(/^1\/\d+/);
    const totalPages = Number((await pageInfo.textContent())!.match(/^1\/(\d+)/)?.[1] ?? "0");
    expect(totalPages).toBeGreaterThanOrEqual(2);

    // —— 翻到第 2 页后刷新 → 仍停在第 2 页（位置记忆）——
    await page.locator("#fb-next-btn").click();
    await expect(pageInfo).toContainText(/^2\//);
    await page.screenshot({ path: join(SHOT_DIR, "fx-01-root-page2.png") });
    await page.reload();
    await expandSidebar(page);
    await expect(pageInfo).toContainText(/^2\//);
    await expect(page.locator("#fb-location")).toContainText("我的切片");
    await page.screenshot({ path: join(SHOT_DIR, "fx-02-restored-page2.png") });

    // —— 进入文件夹 A，导入抽屉目标默认 A（最新授权列表 + can_write）——
    // 文件夹卡在首页（文件夹卡排前）；先翻回第 1 页
    await page.locator("#fb-prev-btn").click();
    // 刷新后侧栏可用高度可能不同，总页数随之变化：只锚当前页
    await expect(pageInfo).toContainText(/^1\//);
    await page.locator('.fb-folder[data-pid="' + folderA + '"]').click();
    await expect(page.locator("#fb-location")).toContainText("-肺组织");
    await page.locator("#fb-plus-btn").click();
    await page.locator("#import-slides-btn").click();
    const drawer = page.locator("#import-drawer");
    await expect(drawer).toBeVisible();
    await expect(page.locator("#import-target-select")).toHaveValue(folderA, {
      timeout: 10_000,
    });
    await expect(page.locator("#baidu-target-select")).toHaveValue(folderA);
    await page.screenshot({ path: join(SHOT_DIR, "fx-03-drawer-default-folderA.png") });
    await page.locator("#import-drawer-close").click();
    await expect(drawer).toBeHidden();

    // —— 深链打开 B 内的切片：侧栏定位到 B 及其真实位置（深链 > 记忆）——
    await page.goto("/app?slide=" + encodeURIComponent(slideInB));
    await expandSidebar(page);
    await expect(page.locator("#fb-location")).toContainText("-研究");
    const card = page.locator('.fb-hit[data-slide-id="' + slideInB + '"]');
    await expect(card).toBeVisible();
    await page.screenshot({ path: join(SHOT_DIR, "fx-04-deeplink-folderB.png") });
  });
});
