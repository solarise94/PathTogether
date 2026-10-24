/**
 * 「保存到我的项目」（转换器页 → 工作台移交）E2E：真实 Chromium + 真实 WASM
 * 转换 + 假 COS 上传（与 upload-inline-convert.spec.ts 同一基建）。
 *
 * 场景（2026-10-22 设计）：
 *   (a) 匿名转换 → 保存到我的项目 → 登录深链接（?next= 带查询串）→ 回跳后
 *       对话框自动打开 → 选既有文件夹 → 工作台自动续传（仅该任务）→ 发布后
 *       切片进所选文件夹（API 断言）；
 *   (b) 新文件夹变体（createKey 幂等键，工作台侧建目录并关联）；
 *   (c) 取消对话框 → 不写 intent、不跳转（回 /app 无恢复行）；
 *   (d) ?save=<不存在的任务> → 显式说明（对话框内错误行）；
 *   (e) 工作台换账号 → 归属保护（account_mismatch），绝不静默上传。
 *
 * 运行（工作区约定；E2E_INLINE_CONVERT 基建由 playwright.config 按本 spec
 * 名显式装配）：
 *   TEST_RUN_DIR=/mnt/ssd2/tests/run-save-proj-XXXXXX E2E_PORT=8951 \
 *   npx playwright test tests/e2e/save-to-project.spec.ts \
 *     --output=$TEST_RUN_DIR/pw --reporter=line
 */
import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
  process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
  "utf8") as string) as {
  userLogin: string; userPassword: string;
  ownerLogin: string; ownerPassword: string;
  inlineConvert?: boolean;
};

const RUN_DIR = process.env.TEST_RUN_DIR || tmpdir();
const SHOTS = join(RUN_DIR, "shots");
const FIXTURE_DIR = process.env.E2E_INLINE_FIXTURE_DIR
  || join(RUN_DIR, "save-proj-fixtures");
const KFB = join(FIXTURE_DIR, "synth-580x300.kfb");

function ensureKfb(): string {
  if (existsSync(KFB)) return KFB;
  mkdirSync(FIXTURE_DIR, { recursive: true });
  const repo = join(__dirname, "..", "..");
  execFileSync("python3", ["-c", `
import sys
sys.path.insert(0, ${JSON.stringify(repo)})
from kfb.fixture import build_synthetic_kfb
build_synthetic_kfb(${JSON.stringify(KFB)}, width=580, height=300)
`], { cwd: repo, stdio: "inherit" });
  return KFB;
}

async function shot(page: Page, name: string): Promise<void> {
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`) });
}

async function login(page: Page, who: "user" | "owner"): Promise<void> {
  const u = who === "user"
    ? CREDS.userLogin : CREDS.ownerLogin;
  const p = who === "user" ? CREDS.userPassword : CREDS.ownerPassword;
  await page.goto("/login");
  await page.fill('form[action="/login"] input[name="username"]', u);
  await page.fill('form[action="/login"] input[name="password"]', p);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.includes("/login")),
    page.click('form[action="/login"] button[type="submit"]'),
  ]);
}

/** 真实登出（复核 r2 复盘）：clearCookies 只清浏览器侧 cookie，早到的
 * 认证响应可能带 Set-Cookie 把 session 复活，「匿名」前提失效——改为
 * POST /logout 让服务端 session.clear()，再清 cookie，并断言确实已匿名。 */
async function logout(page: Page): Promise<void> {
  await page.evaluate(async () => {
    const csrf = decodeURIComponent(
      (document.cookie.split("; ").find((x) => x.startsWith("csrf_token=")) || "")
        .split("=").slice(1).join("=") || "");
    await fetch("/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": csrf },
      credentials: "same-origin",
    });
  });
  await page.context().clearCookies();
  await expect.poll(async () => page.evaluate(async () => {
    const r = await fetch("/api/auth/info", { credentials: "same-origin" });
    return r.status;
  }), { timeout: 10_000, intervals: [300] }).toBe(401);
}

/** 桌面端侧栏首入默认收起；行断言前先展开（真实用户路径）。 */
async function expandSidebar(page: Page): Promise<void> {
  const collapsed = await page.locator("#sidebar").evaluate(
    (el) => el.classList.contains("collapsed"));
  if (collapsed) await page.locator("#menu-btn").click();
  await expect(page.locator("#fb-location")).toBeVisible({ timeout: 15_000 });
}

/** 布局断言（复核 r3 #4）：每个 .upload-item-btn 的包围盒都必须在所在
 * .upload-item 行内（±1px）——长文案按钮不得撑破侧栏/面板边缘。隐藏
 * （未布局）的按钮跳过。 */
async function expectButtonsInsideRows(page: Page): Promise<void> {
  const bad = await page.evaluate(() => {
    const out: string[] = [];
    document.querySelectorAll(".upload-item").forEach((row) => {
      const rr = row.getBoundingClientRect();
      row.querySelectorAll(".upload-item-btn").forEach((b) => {
        const br = (b as HTMLElement).getBoundingClientRect();
        if (br.width === 0 && br.height === 0) return;
        if (br.right > rr.right + 1 || br.left < rr.left - 1) {
          out.push(`"${(b.textContent || "").slice(0, 24)}" right=${br.right} rowRight=${rr.right}`);
        }
      });
    });
    return out;
  });
  expect(bad).toEqual([]);
}

/** 面板行（带按钮宿主的 inline 行）。 */
function panelRows(page: Page) {
  return page.locator(".upload-item").filter({ has: page.locator(".upload-item-actions") });
}

async function entryStates(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const o = (window as unknown as {
      HP_UPLOAD?: { inlineOrch?: () => { list: () => Array<{ state: string }> } | null };
    }).HP_UPLOAD?.inlineOrch?.();
    return o ? o.list().map((e) => e.state) : [];
  });
}

async function waitAllSettled(page: Page, timeoutMs = 240_000): Promise<void> {
  await expect.poll(async () => {
    const states = await entryStates(page);
    return states.length > 0 && states.every((s) =>
      ["done", "convert_failed", "upload_failed", "rejected", "cancelled",
       "account_mismatch"].includes(s));
  }, { timeout: timeoutMs, intervals: [500] }).toBe(true);
}

/** 转换一个 KFB（/tools/slides 公开页，登录与否皆可），返回就绪状态。 */
async function convertKfb(page: Page): Promise<void> {
  await page.goto("/tools/slides");
  await page.waitForFunction(() =>
    (window as unknown as { __stToolsReady?: boolean }).__stToolsReady === true);
  await page.setInputFiles("#file-input", ensureKfb());
  await expect(page.locator("#summary-section")).toBeVisible({ timeout: 90_000 });
  await page.click("#convert-btn");
  await expect(page.locator("#result-section")).toBeVisible({ timeout: 240_000 });
}

test.beforeEach(async ({ page }) => {
  ensureKfb();
  // 本 spec 断言 zh 文案（与 hp_lang 持久化同口径）
  await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
});

// --------------------------------------------------------------------------- //
// (a) 匿名 → 转换 → 保存到我的项目 → 登录回跳 → 既有文件夹 → 工作台上传 → 入夹
// --------------------------------------------------------------------------- //
test("(a) 匿名转换 → 登录回跳 → 既有文件夹 → 工作台自动上传并关联", async ({ page }) => {
  test.setTimeout(420_000);
  // 预置：以 user 身份建「父文件夹 + 子文件夹」层级，然后清 cookie 回到
  // 匿名（OPFS 保留）——对话框下拉按层级缩进呈现
  await login(page, "user");
  const stamp = Date.now();
  const folderParent = `e2e-父文件夹-${stamp}`;
  const folderName = `e2e-save-folder-${stamp}`;
  // 幂等键只用 ASCII（HTTP 头不允许非 Latin-1）
  await page.evaluate(async ({ names, key }) => {
    const csrf = decodeURIComponent(
      (document.cookie.split("; ").find((x) => x.startsWith("csrf_token=")) || "")
        .split("=").slice(1).join("=") || "");
    async function create(name, parent) {
      const r = await fetch("/api/project/create", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf,
                   "Idempotency-Key": key + "-" + (parent ? "c" : "p") },
        body: JSON.stringify({ name, note: "", slides: [],
          ...(parent ? { parent_project_id: parent } : {}) }),
      });
      if (!r.ok) throw new Error("create failed: " + name);
      return (await r.json()).pid;
    }
    const parentPid = await create(names[0], null);
    await create(names[1], parentPid);
  }, { names: [folderParent, folderName], key: "e2e-save-" + stamp });
  await logout(page);

  // 1) 匿名转换 → 结果卡出现新按钮（zh）
  await convertKfb(page);
  await expect(page.locator("#save-project-btn")).toBeEnabled();
  await expect(page.locator("#save-project-btn")).toHaveText("保存到我的项目");
  // 截图前滚到结果卡（按钮在首屏下方）
  await page.locator("#result-section").scrollIntoViewIfNeeded();
  await shot(page, "sp-zh-desktop-result");

  // 2) 点击 → 登录/注册/取消对话框（复核 r2 #1：无自动跳转计时器）；
  // 截图后点「登录」→ /login?next=（查询串原样）
  // 诊断捕获（复核 r2）：点击时刻的按钮状态、页面状态行与 /api/auth/info
  // 的响应——首次点击必须打开对话框，不做任何重试
  const clickState = await page.evaluate(() => ({
    btnDisabled: document.getElementById("save-project-btn").disabled,
    btnTitle: document.getElementById("save-project-btn").title,
    pageStatus: document.getElementById("page-status").textContent,
    saveStatus: document.getElementById("save-status").textContent,
  }));
  const authResponses: Array<{ status: number; ms: number }> = [];
  let authSentAt = 0;
  page.on("request", (r) => {
    if (r.url().includes("/api/auth/info")) authSentAt = Date.now();
  });
  page.on("response", (r) => {
    if (r.url().includes("/api/auth/info")) {
      authResponses.push({ status: r.status(), ms: Date.now() - authSentAt });
    }
  });
  const consoleErrors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push(String(e)));

  await page.click("#save-project-btn");
  const loginDialog = page.locator("#save-login-dialog");
  await expect(loginDialog).toBeVisible({ timeout: 10_000 });
  expect(clickState, "点击时刻状态").toMatchObject({ btnDisabled: false });
  expect(authResponses.length, "/api/auth/info 请求数").toBeGreaterThanOrEqual(1);
  await expect(loginDialog).toContainText("保存到我的项目需要登录");
  await shot(page, "sp-zh-desktop-handoff");
  await Promise.all([
    page.waitForURL(/\/login\?next=%2Ftools%2Fslides%3Fsave%3D/, { timeout: 15_000 }),
    page.click("#save-login-login-btn"),
  ]);

  // 3) 登录 → 回到 /tools/slides?save=<jobId> → 对话框自动打开
  await page.fill('form[action="/login"] input[name="username"]', CREDS.userLogin);
  await page.fill('form[action="/login"] input[name="password"]', CREDS.userPassword);
  await Promise.all([
    page.waitForURL(/\/tools\/slides\?save=/),
    page.click('form[action="/login"] button[type="submit"]'),
  ]);
  const dialog = page.locator("#save-project-dialog");
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  // 账号行 = 登录账号；文件行 = 产物（源名 · 大小）
  await expect(page.locator("#save-project-account")).toContainText(CREDS.userLogin);
  await expect(page.locator("#save-project-file")).toContainText("synth-580x300");
  // 目标下拉含预建文件夹（层级渲染走同一列表）
  const dest = page.locator("#save-project-dest");
  await expect(dest).toBeVisible();
  // 等文件夹列表渲染完成（含层级：父文件夹 + 缩进的子文件夹）
  await expect(dest.locator("option", { hasText: folderParent })).toHaveCount(1);
  await expect(dest.locator("option", { hasText: folderName })).toHaveCount(1);
  // 4) 选子文件夹（层级缩进：选项文本 = 全角空格缩进 + ↳ + 名称；按文本
  // 找到 option 再按 value 选择）。原生 select 的下拉展开无法截图——
  // 选中缩进的子文件夹后截，选择框自身即显示层级

  // 4) 选子文件夹（层级缩进：选项文本 = 全角空格缩进 + ↳ + 名称；按文本
  // 找到 option 再按 value 选择）
  const childOption = dest.locator("option", { hasText: folderName });
  await expect(childOption).toHaveCount(1);
  await dest.selectOption(await childOption.getAttribute("value"));
  await shot(page, "sp-zh-desktop-dialog");
  await page.click("#save-project-confirm-btn");
  await page.waitForURL(/\/app\?save-job=/, { timeout: 30_000 });
  // 移交标记被工作台摘除（自动续传一次之后）
  await expect.poll(async () => page.url().includes("save-job="), {
    timeout: 20_000, intervals: [500],
  }).toBe(false);

  // 5) 工作台上传（真实 WASM 转换已完成，这里只剩上传）：行 uploading/done
  await page.waitForLoadState("load");
  await expandSidebar(page);
  const row = panelRows(page).first();
  await expect(row).toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => {
    const states = await entryStates(page);
    return states.includes("uploading") || states.includes("done") ? "ok" : "wait";
  }, { timeout: 60_000, intervals: [300] }).toBe("ok");
  await shot(page, "sp-zh-desktop-row-uploading");
  await waitAllSettled(page);
  const states = await entryStates(page);
  expect(states).toContain("done");
  await shot(page, "sp-zh-desktop-row-done");
  // 布局断言：done 行的长文案「删除本地产物（…）」按钮不得撑破行宽
  await expectButtonsInsideRows(page);

  // 6) 切片进了所选文件夹（API 断言）
  const inFolder = await page.evaluate(async (name) => {
    const r = await fetch("/api/projects", { headers: { Accept: "application/json" } });
    const projects = await r.json();
    const folder = (projects || []).find((p: { name: string }) => p.name === name);
    if (!folder) return { found: false, count: 0 };
    // 负载同时带 slide_ids（id 通道）与 slides（名数组，旧后端）——同一
    // 切片会双计；优先 id 通道
    const count = (folder.slide_ids || []).length ||
      (folder.slides || []).length;
    return { found: true, count };
  }, folderName);
  expect(inFolder).toMatchObject({ found: true, count: 1 });
});

// --------------------------------------------------------------------------- //
// (b) 新文件夹变体
// --------------------------------------------------------------------------- //
test("(b) 新文件夹变体：工作台按 createKey 建目录并关联（en 桌面截图）", async ({ page }) => {
  test.setTimeout(420_000);
  // en 桌面：结果卡与对话框的英文截图（断言只依赖名称，不依赖语言）
  await page.addInitScript(() => localStorage.setItem("hp_lang", "en"));
  await login(page, "user");
  const newFolder = `e2e-save-new-${Date.now()}`;
  await convertKfb(page);
  await page.locator("#result-section").scrollIntoViewIfNeeded();
  await shot(page, "sp-en-desktop-result");
  await page.click("#save-project-btn");
  const dialog = page.locator("#save-project-dialog");
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  const dest = page.locator("#save-project-dest");
  await expect(dest).toBeVisible();
  await shot(page, "sp-en-desktop-dialog");
  await dest.selectOption("new");
  await expect(page.locator("#save-project-new-row")).toBeVisible();
  await page.fill("#save-project-new-name", newFolder);
  await page.click("#save-project-confirm-btn");
  await page.waitForURL(/\/app\?save-job=/, { timeout: 30_000 });
  await page.waitForLoadState("load");
  await waitAllSettled(page);
  expect((await entryStates(page))).toContain("done");
  const created = await page.evaluate(async (name) => {
    const r = await fetch("/api/projects", { headers: { Accept: "application/json" } });
    const projects = await r.json();
    const folder = (projects || []).find((p: { name: string }) => p.name === name);
    if (!folder) return { found: false, count: 0 };
    const count = (folder.slide_ids || []).length ||
      (folder.slides || []).length;
    return { found: true, count };
  }, newFolder);
  expect(created).toMatchObject({ found: true, count: 1 });
});

// --------------------------------------------------------------------------- //
// (c) 取消对话框：不写 intent、不跳转
// --------------------------------------------------------------------------- //
test("(c) 取消对话框：留在本页，工作台无恢复行（zh mobile 390px 截图）", async ({ page }) => {
  test.setTimeout(420_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, "user");
  await convertKfb(page);
  await page.locator("#result-section").scrollIntoViewIfNeeded();
  await shot(page, "sp-zh-mobile-result");
  await page.click("#save-project-btn");
  const dialog = page.locator("#save-project-dialog");
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  await shot(page, "sp-zh-mobile-dialog");
  await page.click("#save-project-cancel-btn");
  await expect(dialog).toBeHidden();
  // 没有跳转
  expect(page.url()).not.toContain("/app");
  expect(page.url()).not.toContain("save-job");
  // 工作台没有该任务的恢复行（没有 intent.channel=workbench-inline）
  await page.goto("/app");
  await page.waitForLoadState("load");
  await expect.poll(async () => (await entryStates(page)).length, {
    timeout: 20_000, intervals: [500],
  }).toBe(0);
});

// --------------------------------------------------------------------------- //
// (f) 注册绕行：注册不带 next —— 回工作台后由待保存标记提示「继续保存」
// --------------------------------------------------------------------------- //
test("(f) 注册绕行 → /app 待保存提示 → 继续保存 → 对话框 → 保存完成", async ({ page }) => {
  test.setTimeout(420_000);
  const folderName = `e2e-save-reg-${Date.now()}`;
  // 1) 匿名转换 → 保存到我的项目 → 对话框选「注册」（注册流不带 next）
  await convertKfb(page);
  await page.click("#save-project-btn");
  const loginDialog = page.locator("#save-login-dialog");
  await expect(loginDialog).toBeVisible({ timeout: 10_000 });
  await page.click("#save-login-register-btn");
  await page.waitForURL(/\/register/, { timeout: 15_000 });
  expect(page.url()).not.toContain("next=");   // 注册流不读 next（有意）
  // 2) 模拟「注册后未带 next 登录」直接落在 /app（待保存标记在 localStorage）
  await page.context().clearCookies();
  await login(page, "user");
  await page.goto("/app");
  await page.waitForLoadState("load");
  // 3) 待保存提示（非阻塞）：继续保存 / 忽略
  const notice = page.locator("#save-handoff-notice");
  await expect(notice).toBeVisible({ timeout: 30_000 });
  await expect(notice).toContainText("转换页有一个结果等待保存到我的项目");
  await shot(page, "sp-zh-desktop-pending-notice");
  // 4) 继续保存 → /tools/slides?save=<jobId> → 对话框自动打开
  await notice.locator("button", { hasText: "继续保存" }).click();
  await page.waitForURL(/\/tools\/slides\?save=/, { timeout: 30_000 });
  const dialog = page.locator("#save-project-dialog");
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  // 5) 建一个文件夹并保存 → 工作台完成 → 切片入夹
  const dest = page.locator("#save-project-dest");
  await expect(dest).toBeVisible({ timeout: 30_000 });
  await dest.selectOption("new");
  await page.fill("#save-project-new-name", folderName);
  await page.click("#save-project-confirm-btn");
  await page.waitForURL(/\/app\?save-job=/, { timeout: 30_000 });
  await page.waitForLoadState("load");
  await waitAllSettled(page);
  expect((await entryStates(page))).toContain("done");
  const created = await page.evaluate(async (name) => {
    const r = await fetch("/api/projects", { headers: { Accept: "application/json" } });
    const projects = await r.json();
    const folder = (projects || []).find((p: { name: string }) => p.name === name);
    if (!folder) return { found: false, count: 0 };
    const count = (folder.slide_ids || []).length || (folder.slides || []).length;
    return { found: true, count };
  }, folderName);
  expect(created).toMatchObject({ found: true, count: 1 });
});

// (g) 面板内上传关闭：移交标记显式失败（复核 r2 #2），绝不静默
// --------------------------------------------------------------------------- //
test("(g) 面板内上传关闭 → 移交标记显式失败提示", async ({ page }) => {
  test.setTimeout(300_000);
  // 匿名转换 → 登录（user）→ 对话框确认（intent 写入），但拦下去 /app 的导航
  await convertKfb(page);
  await page.click("#save-project-btn");
  const loginDialog = page.locator("#save-login-dialog");
  await expect(loginDialog).toBeVisible({ timeout: 10_000 });
  await Promise.all([
    page.waitForURL(/\/login\?next=/, { timeout: 15_000 }),
    page.click("#save-login-login-btn"),
  ]);
  await page.fill('form[action="/login"] input[name="username"]', CREDS.userLogin);
  await page.fill('form[action="/login"] input[name="password"]', CREDS.userPassword);
  await Promise.all([
    page.waitForURL(/\/tools\/slides\?save=/),
    page.click('form[action="/login"] button[type="submit"]'),
  ]);
  const dialog = page.locator("#save-project-dialog");
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  const dest = page.locator("#save-project-dest");
  await expect(dest).toBeVisible({ timeout: 30_000 });
  await page.route("**/app?*", (route) => route.fulfill({ status: 200, body: "held" }));
  await page.route("**/app", (route) => route.fulfill({ status: 200, body: "held" }));
  await page.click("#save-project-confirm-btn");
  await page.waitForURL(/save-job=/, { timeout: 30_000 });
  const jobId = new URL(page.url()).searchParams.get("save-job") || "";
  expect(jobId).not.toBe("");
  await page.unroute("**/app?*");
  await page.unroute("**/app");
  // 面板内上传关闭的 /app 载荷（e2e 服务器 __inline_convert=0 = 真实关态）
  await page.goto(`/app?save-job=${encodeURIComponent(jobId)}&__inline_convert=0`);
  await page.waitForLoadState("load");
  const notice = page.locator("#save-handoff-notice");
  await expect(notice).toBeVisible({ timeout: 30_000 });
  await expect(notice).toContainText("无法继续保存到项目：");
  await expect(notice).toContainText("面板内上传未开启");
  // 标记参数被摘除
  await expect.poll(async () => page.url().includes("save-job="), {
    timeout: 10_000, intervals: [300],
  }).toBe(false);
});

// (h) 登录/注册对话框截图：en 桌面 + zh 移动 390px（复核 r2 #1）
for (const variant of [
  { key: "sp-en-desktop-login-dialog", lang: "en", mobile: false },
  { key: "sp-zh-mobile-login-dialog", lang: "zh", mobile: true },
] as const) {
  test(`登录对话框截图（${variant.key}）`, async ({ page }) => {
    test.setTimeout(300_000);
    await page.addInitScript((lang) => localStorage.setItem("hp_lang", lang as string), variant.lang);
    if (variant.mobile) await page.setViewportSize({ width: 390, height: 844 });
    await convertKfb(page);
    await page.locator("#result-section").scrollIntoViewIfNeeded();
    await page.click("#save-project-btn");
    const loginDialog = page.locator("#save-login-dialog");
    await expect(loginDialog).toBeVisible({ timeout: 10_000 });
    await expect(loginDialog).toContainText(variant.lang === "zh"
      ? "保存到我的项目需要登录" : "Sign in to save to your projects");
    await shot(page, variant.key);
    // 取消 → 留在本页（不写标记）
    await page.click("#save-login-cancel-btn");
    await expect(loginDialog).toBeHidden();
    expect(page.url()).not.toContain("/register");
    expect(page.url()).not.toContain("/login");
  });
}

// (d) ?save= 指向不存在的任务 → 显式说明
// --------------------------------------------------------------------------- //
test("(d) ?save=<不存在的任务> → 对话框内显式说明，不跳转", async ({ page }) => {
  test.setTimeout(180_000);
  await login(page, "user");
  await page.goto("/tools/slides?save=no-such-job");
  // 参数被摘除（replaceState）
  await expect.poll(async () => page.url().includes("save="), {
    timeout: 10_000, intervals: [300],
  }).toBe(false);
  const dialog = page.locator("#save-project-dialog");
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("#save-project-error"))
    .toContainText("没有找到这个任务");
  // 复核 2026-10-23：错误行必须真的可见（曾藏在 hidden 的 fields 容器里）
  await expect(page.locator("#save-project-error")).toBeVisible();
  await expect(page.locator("#save-project-confirm-btn")).toBeDisabled();
  expect(page.url()).not.toContain("/app");
});

// --------------------------------------------------------------------------- //
// (e) 工作台换账号：归属保护（account_mismatch），绝不静默上传
// --------------------------------------------------------------------------- //
test("(e) 工作台换账号 → account_mismatch 保护，不建 ingestion", async ({ page }) => {
  test.setTimeout(420_000);
  // 以 user 登录完成「保存到我的项目」的确认（intent.account = user）
  await login(page, "user");
  const folderName = `e2e-save-mm-${Date.now()}`;
  await page.evaluate(async (name) => {
    const csrf = decodeURIComponent(
      (document.cookie.split("; ").find((x) => x.startsWith("csrf_token=")) || "")
        .split("=").slice(1).join("=") || "");
    await fetch("/api/project/create", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf,
                 "Idempotency-Key": "e2e-save-mm-" + String(Date.now()) },
      body: JSON.stringify({ name, note: "", slides: [] }),
    });
  }, folderName);
  // (e) 需要用户处于登录态：intent.account 冻结为 user，之后在工作台换
  // owner 账号触发归属保护——这里绝不能登出
  await convertKfb(page);
  await page.click("#save-project-btn");
  const dialog = page.locator("#save-project-dialog");
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  const dest = page.locator("#save-project-dest");
  await expect(dest).toBeVisible();
  await dest.selectOption({ label: folderName });
  // 拦下去 /app 的导航：先清 cookie 换成 owner，再放行
  await page.route("**/app?*", (route) => route.fulfill({ status: 200, body: "held" }));
  await page.route("**/app", (route) => route.fulfill({ status: 200, body: "held" }));
  await page.click("#save-project-confirm-btn");
  await page.waitForURL(/save-job=/, { timeout: 30_000 });
  await page.unroute("**/app?*");
  await page.unroute("**/app");
  await logout(page);
  await login(page, "owner");
  await page.goto("/app");
  await page.waitForLoadState("load");
  await expandSidebar(page);
  // 恢复行进入归属不符（不自动上传）；行注点名归属账号与出路
  const row = panelRows(page).first();
  await expect(row).toBeVisible({ timeout: 30_000 });
  await expect(row).toContainText("此记录由");
  await expect(row.locator(".upload-item-btn", { hasText: "改用当前账号继续" })).toBeVisible();
  // 状态稳定停在 account_mismatch（绝不静默上传）
  await page.waitForTimeout(5_000);
  expect(await entryStates(page)).toContain("account_mismatch");
  expect(await entryStates(page)).not.toContain("done");
  // 换回 user：其文件夹里没有切片（没有 ingestion 被创建/发布）
  await page.context().clearCookies();
  await login(page, "user");
  const count = await page.evaluate(async (name) => {
    const r = await fetch("/api/projects", { headers: { Accept: "application/json" } });
    const projects = await r.json();
    const folder = (projects || []).find((p: { name: string }) => p.name === name);
    if (!folder) return { found: false, count: -1 };
    return {
      found: true,
      count: (folder.slide_ids || []).length + (folder.slides || []).length,
    };
  }, folderName);
  expect(count).toMatchObject({ found: true, count: 0 });
});
