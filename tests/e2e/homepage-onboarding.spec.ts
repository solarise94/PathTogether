/**
 * 主页使用引导升级 E2E（docs/homepage-onboarding-upgrade-agent-plan-20260928.md §9）。
 *
 * 走 playwright.config.ts 的 webServer（tests/e2e/e2e_server.py：真实 Flask +
 * 内嵌 PostgreSQL + 一次性凭据；REQUIRE_ADMIN_AUTH=1，注册模式 fail-closed 降级
 * closed）。不 mock 后端：键盘/焦点/弹窗行为在真实渲染页面上验证。
 *
 * 覆盖（§9 验收表）：
 *   1. 首访：不打开菜单即可找到 Demo / 登录工作台 / 注册入口（closed 说明）；
 *      Hero 预览图加载（WebP 优先，固有尺寸 1440×900）。
 *   2. 账户头像 disclosure（WAI APG）：Enter/Space 开、Escape 关回触发器、
 *      点击外部关、aria-expanded 同步（键盘与读屏行）。
 *   3. 认证：Hero/头像面板/深链接同一 dialog；关闭后焦点回到可见触发器
 *      （面板内链接打开弹窗时焦点回账户按钮——entry.js 捕获阶段收面板）。
 *   4. 已登录：Hero「进入工作台」、无注册/登录引导、退出为 POST+CSRF 表单。
 *   5. H3 工作台空态：「上传你的第一张切片」复用导入抽屉；「查看示例」链接。
 *   6. 响应式：390×844 / 320 宽 / 720 宽 @2x（≈200% 缩放）无横溢出，
 *      入口在预览图之前可操作。
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
  process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
  "utf8",
)) as {
  baseUrl: string;
  ownerLogin: string; ownerPassword: string;
  userLogin: string; userPassword: string;
};

async function login(page: Page, loginId: string, password: string) {
  await page.goto("/login");
  await page.fill('form[action="/login"] input[name="username"]', loginId);
  await page.fill('form[action="/login"] input[name="password"]', password);
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes("/login")),
    page.click('form[action="/login"] button[type="submit"]'),
  ]);
}

test.use({ viewport: { width: 1440, height: 900 }, locale: "zh-CN" });

test("anon homepage: dual entries, register note, preview image, account disclosure keyboard", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await expect(page.locator("h1")).toHaveText("与 AI 一起，观察病理切片。");

  // 首访：Hero 双入口与说明（不打开任何菜单即可见）
  const heroLogin = page.locator(".hero-actions a.btn-primary");
  await expect(heroLogin).toHaveAttribute("href", "/login?next=/app");
  await expect(heroLogin).toHaveText("登录工作台");
  const demoBtn = page.locator(".hero-actions a.btn-secondary");
  await expect(demoBtn).toHaveAttribute("href", "/demo");
  await expect(page.locator(".hero-hint")).toContainText("Demo 无需登录");
  // 注册入口：本环境 fail-closed → closed 说明（不伪装开放注册）
  await expect(page.locator(".hero-register"))
    .toContainText("已有账号可登录；暂未开放注册");
  // 如何开始四步
  await expect(page.locator("#get-started .step")).toHaveCount(4);

  // 预览图：固有尺寸 + WebP 选中 + 标注
  const preview = page.locator("#workbench-preview-img");
  await expect(preview).toBeVisible();
  await expect(preview).toHaveAttribute("width", "1440");
  await expect(preview).toHaveAttribute("height", "900");
  const natural = await preview.evaluate(
    (img: HTMLImageElement) => ({ w: img.naturalWidth, h: img.naturalHeight, c: img.currentSrc }));
  expect(natural.w).toBe(1440);
  expect(natural.h).toBe(900);
  expect(natural.c).toContain("/static/entry-media/workbench-preview.webp");
  await expect(page.locator(".workbench-preview-tag")).toContainText("工作台预览 · 示例切片");

  // 账户头像 disclosure：初始收起 → Enter 展开 → Tab 进面板 → Escape 关回触发器
  const btn = page.locator("#account-btn");
  const panel = page.locator("#account-panel");
  await expect(btn).toHaveAttribute("aria-expanded", "false");
  await expect(panel).toBeHidden();
  await btn.focus();
  await page.keyboard.press("Enter");
  await expect(panel).toBeVisible();
  await expect(btn).toHaveAttribute("aria-expanded", "true");
  // 面板内容：登录工作台 + Demo；closed 说明；无表单
  await expect(panel.locator('a[href="/login?next=/app"]')).toHaveText("登录工作台");
  await expect(panel.locator('a[href="/demo"]')).toHaveText("直接体验 Demo");
  await expect(panel).toContainText("已有账号可登录；暂未开放注册");
  await page.keyboard.press("Tab"); // 焦点进面板首个可操作元素
  await expect(panel.locator("a, button").first()).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await expect(btn).toBeFocused();

  // 点击外部关闭
  await btn.click();
  await expect(panel).toBeVisible();
  await page.locator("h1").click();
  await expect(panel).toBeHidden();

  // Space 键开关（原生 button + aria 同步）
  await btn.focus();
  await page.keyboard.press("Space");
  await expect(panel).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  expect(errors).toEqual([]);
});

test("auth dialog opens from hero and account panel; focus returns to visible triggers", async ({ page }) => {
  await page.goto("/");
  const dialog = page.locator("#login-dialog");

  // Hero 主按钮 → 同一 dialog（登录视图），标题为「登录工作台」
  await page.locator(".hero-actions a.btn-primary").click();
  await expect(dialog).toBeVisible();
  await expect(page.locator("#login-view")).toBeVisible();
  await expect(page.locator("#login-dialog-title")).toHaveText("登录工作台");
  // 关闭（Esc）→ 焦点回 Hero 按钮（仍可见）
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.locator(".hero-actions a.btn-primary")).toBeFocused();

  // 账户面板内链接 → 面板先收起 + 弹窗打开；关闭后焦点回账户按钮（非隐藏链接）
  const btn = page.locator("#account-btn");
  await btn.click();
  await page.locator('#account-panel a[href^="/login"]').click();
  await expect(page.locator("#account-panel")).toBeHidden();
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(btn).toBeFocused();

  // 弹窗内切换到注册视图（closed：说明 + 已有账号？登录 可切回）
  await page.locator(".hero-actions a.btn-primary").click();
  await page.locator('#login-view a[data-auth-switch="register"]').click();
  await expect(page.locator("#register-view")).toBeVisible();
  await page.locator('#register-view a[data-auth-switch="login"]').click();
  await expect(page.locator("#login-view")).toBeVisible();
});

test("signed-in homepage: workbench entries, account panel summary, POST logout with CSRF", async ({ page }) => {
  await login(page, CREDS.userLogin, CREDS.userPassword);
  await page.goto("/");
  // Hero：进入工作台主按钮 + Demo 次按钮；无注册/登录引导
  const heroEnter = page.locator(".hero-actions a.btn-primary");
  await expect(heroEnter).toHaveAttribute("href", "/app");
  await expect(heroEnter).toHaveText("进入工作台");
  await expect(page.locator(".hero-register")).toHaveCount(0);
  await expect(page.locator(".hero-hint")).toContainText("上传切片，继续查看、标注与协作。");
  // 账户面板：身份摘要 + 工作台 + 退出（POST 表单带 CSRF）
  await page.locator("#account-btn").click();
  const panel = page.locator("#account-panel");
  await expect(panel.locator(".account-name")).not.toBeEmpty();
  await expect(panel.locator('a[href="/app"]')).toHaveText("进入工作台");
  const logout = panel.locator('form[action="/logout"]');
  await expect(logout).toHaveAttribute("method", "post");
  await expect(logout.locator('input[name="csrf_token"]')).toHaveValue(/.+/);
  // 已登录不渲染登录/注册弹窗
  await expect(page.locator("#login-dialog")).toHaveCount(0);
});

test("workbench empty state (H3): upload reuses the import drawer", async ({ page }) => {
  await login(page, CREDS.userLogin, CREDS.userPassword);
  await page.goto("/app");
  const empty = page.locator("#viewer-empty");
  await expect(empty).toBeVisible();
  await expect(empty.locator("#viewer-empty-upload")).toHaveText("上传切片");
  await expect(empty.locator('a[href="/demo"]')).toHaveText("查看示例");
  await expect(empty.locator("#viewer-empty-pick")).toBeVisible();
  // 复用既有导入抽屉（不另做上传实现）
  await empty.locator("#viewer-empty-upload").click();
  await expect(page.locator("#import-drawer")).toBeVisible();
});

test("responsive: entries reachable before preview, no horizontal overflow", async ({ page }) => {
  for (const size of [
    { width: 1280, height: 720 },
    { width: 820, height: 1180 },
    { width: 390, height: 844 },
    { width: 320, height: 800 },
  ]) {
    await page.setViewportSize(size);
    await page.goto("/");
    const heroLogin = page.locator(".hero-actions a.btn-primary");
    await expect(heroLogin).toBeVisible();
    await expect(heroLogin).toHaveText("登录工作台");
    await expect(page.locator("#account-btn")).toBeVisible();
    // 窄屏（手机堆叠）：入口在预览图之前；桌面为左右分栏不作此断言
    if (size.width <= 820) {
      const loginBox = await heroLogin.boundingBox();
      const previewBox = await page.locator("#workbench-preview-img").boundingBox();
      expect(loginBox!.y).toBeLessThan(previewBox!.y);
    }
    // 无横溢出（320/390/820 手机宽度）
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  }
  // 200% 缩放近似：720×450 视口 @2x deviceScaleFactor 仍可用
  const zoomed = await page.context().browser()!.newContext({
    viewport: { width: 720, height: 450 }, deviceScaleFactor: 2, locale: "zh-CN",
  });
  const zp = await zoomed.newPage();
  await zp.goto("/");
  await expect(zp.locator(".hero-actions a.btn-primary")).toBeVisible();
  const overflow2 = await zp.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow2).toBeLessThanOrEqual(0);
  await zoomed.close();
});
