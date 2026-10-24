/**
 * MRXS「先选 .mrxs，再选同名数据文件夹」E2E（真实 Chromium + 真实合成 MRXS
 * 夹具，两个页面统一验收）。
 *
 * 用户旅程（2026-10 交互统一）：
 *   1. 照常选择 `synthetic.mrxs`；
 *   2. 页面出现非错误的「这张切片还需要同名数据文件夹，请选择「synthetic」
 *      文件夹。」提示（转换器页：#companion-section 面板；工作台：上传行
 *      need_folder 状态），此时**没有任何内容被复制**（准备卡/摘要不出现、
 *      面板不建任务）；
 *   3. 补选同名数据文件夹后，导入**无需重选 .mrxs** 直接继续：
 *      - 转换器页（/tools/slides）：合并行走 prepareBundleSource →
 *        runner.prepareBundle → 摘要出现 → 转换跑到完成（结果面板）；
 *      - 工作台（/app）：行原地转整包 → enqueueBundle 同路 → 真实 WASM
 *        转换 → 假 COS 上传 → 收口 done（平台切片可查看）。
 *
 * 文件夹的两种提供方式都覆盖：
 *   - `delete window.showDirectoryPicker` → 隐藏 webkitdirectory input 回退
 *     （Playwright 的 filechooser/setInputFiles 支持目录）；
 *   - 页内 stub `showDirectoryPicker`（用**真实夹具字节**构造的
 *     FileSystemDirectoryHandle 鸭子型替身；转换器页）。
 * 另覆盖：错误文件夹（无 Slidedat.ini → 明确拒绝并保持可重选）、用户取消
 * （AbortError → 安静保持待选态）、不同名文件夹的显式确认（转换器页 =
 * 原生 <dialog>；工作台 = 行内确认按钮）。
 *
 * 运行（工作区约定：全部运行数据在 SSD2 的 TEST_RUN_DIR）：
 *   TEST_RUN_DIR=/mnt/ssd2/tests/run-mrxs-XXXXXX E2E_PORT=8943 \
 *   npx playwright test tests/e2e/mrxs-companion.spec.ts \
 *     --output=$TEST_RUN_DIR/pw-results --reporter=line
 * 截图（zh/en 桌面 + zh 移动 390px）落 $TEST_RUN_DIR/shots/。
 */
import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
  process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
  "utf8") as string) as {
  baseUrl: string;
  userLogin: string; userPassword: string;
  inlineConvert?: boolean;
};

/** 夹具：真实合成 MRXS 包（只读；运行副本一律落在 TEST_RUN_DIR）。 */
const FIXTURE_BASE = process.env.MRXS_FIXTURE_BASE
  || "/home/solarise/ZCodeProject/histopilot-suite/.scratch/review-ux-8cd6427/base";
const RUN_DIR = process.env.TEST_RUN_DIR || tmpdir();
const SHOTS = join(RUN_DIR, "shots");

const FX = join(RUN_DIR, "mrxs-fixture");
const MRXS_FILE = join(FX, "slide", "synthetic.mrxs");
const DATA_DIR = join(FX, "slide", "synthetic");          // 同名数据文件夹
const WRONG_DIR = join(FX, "wrong", "synthetic");         // 同名但没有 Slidedat.ini
const RENAMED_DIR = join(FX, "renamed", "renamed_data");  // 不同名但数据完整
const PARENT_DIR = join(FX, "parent");                    // 多切片父文件夹（复核 r2 #1）
const INCOMPLETE_DIR = join(FX, "incomplete", "synthetic"); // 缺成员的数据文件夹（复核 r2 #4）
const VMS_FILE = join(FX, "slide", "other.vms");          // VMS 散入口（行状态截图）
const KFB_FILE = join(FX, "slide", "synth.kfb");          // 转换中行（行状态截图）

function ensureFixtures(): void {
  if (existsSync(MRXS_FILE)) return;
  mkdirSync(join(FX, "slide"), { recursive: true });
  mkdirSync(join(FX, "wrong", "synthetic"), { recursive: true });
  mkdirSync(join(FX, "renamed", "renamed_data"), { recursive: true });
  mkdirSync(join(FX, "incomplete", "synthetic"), { recursive: true });
  cpSync(join(FIXTURE_BASE, "synthetic.mrxs"), MRXS_FILE);
  cpSync(join(FIXTURE_BASE, "synthetic"), DATA_DIR, { recursive: true });
  // 错误文件夹：同名但没有任何 Slidedat.ini（只在深层放无关文件）
  writeFileSync(join(WRONG_DIR, "Index.dat"), "not a slidedat\n");
  writeFileSync(join(WRONG_DIR, "readme.txt"), "junk\n");
  // 不同名文件夹：完整数据（含 Slidedat.ini），但文件夹名 ≠ 词干
  for (const name of ["Slidedat.ini", "Index.dat", "Data0000.dat", "Data0001.dat"]) {
    cpSync(join(FIXTURE_BASE, "synthetic", name), join(RENAMED_DIR, name));
  }
  // 多切片父文件夹（复核 r2 #1）：other/ 的 Slidedat.ini 与 synthetic/ 同场
  // ——判定必须挑 synthetic/ 子树，绝不能按遍历顺序拿第一个
  mkdirSync(join(PARENT_DIR, "other"), { recursive: true });
  writeFileSync(join(PARENT_DIR, "other", "Slidedat.ini"),
    "[DATAFILE]\nFILE_COUNT = 0\n");
  cpSync(join(FIXTURE_BASE, "synthetic"), join(PARENT_DIR, "synthetic"),
    { recursive: true });
  // 缺成员的数据文件夹（复核 r2 #4）：有 Slidedat.ini 与 Data0000.dat，
  // 缺 Index.dat 与 Data0001.dat → planner 类型化拒绝 → 面板/行进入重试态
  cpSync(join(FIXTURE_BASE, "synthetic", "Slidedat.ini"),
    join(INCOMPLETE_DIR, "Slidedat.ini"));
  cpSync(join(FIXTURE_BASE, "synthetic", "Data0000.dat"),
    join(INCOMPLETE_DIR, "Data0000.dat"));
  // VMS 散入口（行状态截图用）：分类进 bundle 分支的 VMS 静态说明
  writeFileSync(VMS_FILE, "not a real vms\n");
}

/** 合成 KFB（与 upload-inline-convert.spec.ts 同一构建器；行状态截图用）。 */
function ensureKfbFixture(): string {
  if (existsSync(KFB_FILE)) return KFB_FILE;
  ensureFixtures();
  const repo = join(__dirname, "..", "..");
  execFileSync("python3", ["-c", `
import sys
sys.path.insert(0, ${JSON.stringify(repo)})
from kfb.fixture import build_synthetic_kfb
build_synthetic_kfb(${JSON.stringify(KFB_FILE)}, width=580, height=300)
`], { cwd: repo, stdio: "inherit" });
  return KFB_FILE;
}

/** in-page stub 需要的真实夹具字节（base64；总计 <70KB）。 */
function fixturePayload(): Record<string, string> {
  const names = ["synthetic.mrxs", "Slidedat.ini", "Index.dat",
    "Data0000.dat", "Data0001.dat"];
  const out: Record<string, string> = {};
  for (const n of names) {
    const p = n === "synthetic.mrxs" ? MRXS_FILE : join(DATA_DIR, n);
    out[n] = readFileSync(p).toString("base64");
  }
  return out;
}

/** 布局断言（复核 r3 #4）：行内按钮不得撑破所在行（±1px）；隐藏行跳过。 */
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

async function shot(page: Page, name: string): Promise<void> {
  mkdirSync(SHOTS, { recursive: true });
  // 工作台首入默认收起侧栏，与打开动作存在竞态：截图前重扩展一次，
  // 保证行内容以完整宽度呈现
  // 工作台侧栏是 fixed 定位：fullPage 拼接会把行内容错位到页面底部，
  // 因此 /app 用视口截图；转换器页整页滚动布局用 fullPage。移动端（≤768px）
  // 侧栏是抽屉：截图前确保抽屉打开，行内容才是画面主体。
  const isApp = page.url().includes("/app");
  if (isApp && (page.viewportSize()?.width ?? 1280) <= 768) {
    try {
      const drawerOpen = await page.locator("#sidebar").evaluate((el) =>
        el.classList.contains("open"));
      if (!drawerOpen) {
        await page.locator("#menu-btn").click();
        await page.waitForTimeout(500);
      }
    } catch { /* 页面已离开：直接截图 */ }
  }
  await page.screenshot({
    path: join(SHOTS, `${name}.png`),
    fullPage: !isApp,
  });
}

async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.fill('form[action="/login"] input[name="username"]', CREDS.userLogin);
  await page.fill('form[action="/login"] input[name="password"]', CREDS.userPassword);
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes("/login")),
    page.click('form[action="/login"] button[type="submit"]'),
  ]);
}

async function expandSidebar(page: Page): Promise<void> {
  const collapsed = await page.locator("#sidebar").evaluate(
    (el) => el.classList.contains("collapsed"));
  if (collapsed) await page.locator("#menu-btn").click();
  await expect(page.locator("#fb-location")).toBeVisible({ timeout: 15_000 });
}

/** 面板行（带按钮宿主的 inline 行）。 */
function panelRows(page: Page) {
  return page.locator(".upload-item").filter({ has: page.locator(".upload-item-actions") });
}

async function entryOf(page: Page, index = 0) {
  return page.evaluate((i) => {
    const o = (window as unknown as {
      HP_UPLOAD?: { inlineOrch?: () => { list: () => Array<Record<string, unknown>> } | null };
    }).HP_UPLOAD?.inlineOrch?.();
    if (!o) return null;
    const list = o.list();
    const e = list[i] as Record<string, unknown> | undefined;
    return e ? {
      state: e.state,
      error: e.error,
      companion: e.companion,
      companionRetry: e.companionRetry,
      companionError: e.companionError,
      bundle: e.bundle ? { folderName: (e.bundle as Record<string, unknown>).folderName } : null,
      jobId: e.jobId,
      slideId: e.slideId,
      name: e.name,
    } : null;
  }, index);
}

/** 最后入队的 entry（行状态截图等多次入队的用例用）。 */
async function lastEntryOf(page: Page) {
  return page.evaluate(() => {
    const o = (window as unknown as {
      HP_UPLOAD?: { inlineOrch?: () => { list: () => Array<Record<string, unknown>> } | null };
    }).HP_UPLOAD?.inlineOrch?.();
    if (!o) return null;
    const list = o.list();
    const e = list[list.length - 1] as Record<string, unknown> | undefined;
    return e ? { state: e.state, error: e.error, companion: e.companion,
      companionRetry: e.companionRetry, jobId: e.jobId, slideId: e.slideId } : null;
  });
}

async function waitSettled(page: Page, states = ["done", "convert_failed",
  "upload_failed", "rejected", "cancelled"], timeoutMs = 240_000) {
  await expect.poll(async () => page.evaluate((want) => {
    const o = (window as unknown as {
      HP_UPLOAD?: { inlineOrch?: () => { list: () => Array<{ state: string }> } | null };
    }).HP_UPLOAD?.inlineOrch?.();
    if (!o) return null;
    const list = o.list().map((e) => e.state);
    return list.length > 0 && list.every((s) => (want as string[]).includes(s));
  }, states), { timeout: timeoutMs, intervals: [250] }).toBe(true);
}

/** 页内 stub：用真实夹具字节构造的目录句柄（values() 递归形状与真句柄一致）。 */
async function stubDirectoryPicker(page: Page, opts: {
  dirName?: string;
  reject?: { name: string };
} = {}): Promise<void> {
  await page.addInitScript(({ files, dirName, reject }) => {
    const w = window as unknown as {
      showDirectoryPicker?: unknown;
      __pickedOptions?: unknown;
      __pickerCalls?: number;
    };
    delete w.showDirectoryPicker;
    const table = files as Record<string, string>;
    const toU8 = (b64: string) => {
      const bin = atob(b64);
      const u = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
      return u;
    };
    w.__pickerCalls = 0;
    w.showDirectoryPicker = async (options?: unknown) => {
      w.__pickerCalls += 1;
      w.__pickedOptions = options;
      if (reject) {
        throw Object.assign(new Error("user cancelled"), { name: reject.name });
      }
      const mkFile = (name: string) => ({
        kind: "file",
        name,
        getFile: async () => new File([toU8(table[name])], name),
      });
      const names = Object.keys(table).filter((n) => n !== "synthetic.mrxs");
      return {
        kind: "directory",
        name: dirName,
        async *values() { for (const n of names) yield mkFile(n); },
      };
    };
  }, { files: fixturePayload(), dirName: opts.dirName || "synthetic",
       reject: opts.reject || null } as never);
}

// --------------------------------------------------------------------------- //
// 转换器页（/tools/slides；公开页，无需登录）
// --------------------------------------------------------------------------- //

test.describe("转换器页：MRXS 同名数据文件夹协助", () => {
  test.beforeEach(() => { ensureFixtures(); });

  test("选 .mrxs → 提示选同名文件夹 → 补选（webkitdirectory 回退）→ 不重选直接转换完成", async ({ page }) => {
    test.setTimeout(300_000);
    await page.addInitScript(() => {
      localStorage.setItem("hp_lang", "zh");
      delete (window as unknown as { showDirectoryPicker?: unknown }).showDirectoryPicker;
    });
    await page.goto("/tools/slides");
    await page.waitForFunction(() =>
      (window as unknown as { __stToolsReady?: boolean }).__stToolsReady === true);

    // 1) 只选 .mrxs：非错误的「需要文件夹」面板（词干点名；按钮聚焦）
    await page.setInputFiles("#file-input", MRXS_FILE);
    const panel = page.locator("#companion-section");
    await expect(panel).toBeVisible();
    await expect(panel.locator("#companion-message"))
      .toContainText("这张切片还需要同名数据文件夹");
    await expect(panel.locator("#companion-message")).toContainText("synthetic");
    const pickBtn = panel.locator("#companion-pick-btn");
    await expect(pickBtn).toContainText("选择「synthetic」文件夹");
    await expect(pickBtn).toBeFocused();
    // 没有复制任何内容：准备/摘要卡都没出现，页面错误区为空
    await expect(page.locator("#stage-section")).toBeHidden();
    await expect(page.locator("#summary-section")).toBeHidden();
    await expect(page.locator("#page-error")).toBeHidden();

    // 2a) 错误文件夹（同名但没有 Slidedat.ini）：明确拒绝并保持可重选
    await page.setInputFiles("#folder-input", WRONG_DIR);
    await expect(panel.locator("#companion-message"))
      .toContainText("不是「synthetic」的数据文件夹");
    await expect(page.locator("#summary-section")).toBeHidden();
    await shot(page, "tools-zh-desktop-wrong-folder");

    // 2b) 不同名但数据完整的文件夹：原生对话框显式确认
    await page.setInputFiles("#folder-input", RENAMED_DIR);
    const dialog = page.locator("#disk-dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("所选文件夹「renamed_data」与切片「synthetic」不同名");
    await shot(page, "tools-zh-desktop-mismatch");
    // 「重新选择」= 不使用，回到待选态
    await page.click("#disk-deny-btn");
    await expect(panel).toBeVisible();
    await expect(panel.locator("#companion-message"))
      .toContainText("这张切片还需要同名数据文件夹");

    // 2c) 补选「缺成员」的数据文件夹 → planner 类型化拒绝（缺的文件列在
    // 上方错误区）→ 面板以「重新选择」口径重出：正文不再重复入门提示，
    // 主按钮 = 重新选择「synthetic」文件夹（复核 r2 #4）
    await page.setInputFiles("#folder-input", INCOMPLETE_DIR);
    const pageError = page.locator("#page-error");
    await expect(pageError).toBeVisible();
    // 复核 r3 A：协助装配后的缺成员错误只说缺什么——不再出现与下方面板
    // 矛盾的「选上一级文件夹」入门指引
    await expect(pageError).toContainText("MRXS 数据文件夹不完整：缺少");
    await expect(pageError).toContainText("synthetic/Index.dat");
    await expect(pageError).not.toContainText("上一级文件夹");
    await expect(panel).toBeVisible();
    await expect(panel.locator("#companion-message"))
      .toContainText("所选文件夹缺少上方列出的文件");
    await expect(panel.locator("#companion-pick-btn"))
      .toContainText("重新选择「synthetic」文件夹");
    await shot(page, "tools-zh-desktop-planner-retry");

    // 3) 补选同名数据文件夹 → 不重选 .mrxs，直接进入准备与摘要
    await page.setInputFiles("#folder-input", DATA_DIR);
    await expect(page.locator("#summary-section")).toBeVisible({ timeout: 90_000 });
    await expect(page.locator("#summary-name")).toHaveText("synthetic.mrxs");
    await expect(page.locator("#companion-section")).toBeHidden();

    // 4) 转换跑到完成（真实 WASM）
    await page.click("#convert-btn");
    await expect(page.locator("#result-section")).toBeVisible({ timeout: 240_000 });
    await expect(page.locator("#save-btn")).toBeEnabled();
    await expect(page.locator("#result-format")).toContainText("OME");
  });

  test("showDirectoryPicker 句柄路径：stub 句柄（真实字节）→ id/mode 正确、startIn 未传、合并成功", async ({ page }) => {
    test.setTimeout(300_000);
    await stubDirectoryPicker(page, { dirName: "synthetic" });
    await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
    await page.goto("/tools/slides");
    await page.waitForFunction(() =>
      (window as unknown as { __stToolsReady?: boolean }).__stToolsReady === true);
    await page.setInputFiles("#file-input", MRXS_FILE);
    const panel = page.locator("#companion-section");
    await expect(panel).toBeVisible();
    await panel.locator("#companion-pick-btn").click();
    // stub 收到的 options：{ id, mode } —— input 选择的 .mrxs 没有句柄，
    // startIn 必须缺席
    const opts = await page.evaluate(() =>
      (window as unknown as { __pickedOptions?: Record<string, unknown> }).__pickedOptions);
    expect(opts).toEqual({ id: "mrxs-companion", mode: "read" });
    // 合并 → planner 接受 → 摘要出现 → 转换完成
    await expect(page.locator("#summary-section")).toBeVisible({ timeout: 90_000 });
    await expect(page.locator("#summary-name")).toHaveText("synthetic.mrxs");
    await page.click("#convert-btn");
    await expect(page.locator("#result-section")).toBeVisible({ timeout: 240_000 });
  });

  test("父文件夹句柄（其他切片在前 + 巨大兄弟子树）→ 只遍历同名子树，合并成功", async ({ page }) => {
    test.setTimeout(300_000);
    // stub：picked 目录名「Slides」≠ 词干；values() 先给 other/（内含 9000
    // 个文件——旧实现整树遍历会在 8192 上限处拒绝），再给 synthetic/（真实
    // 夹具字节）。复核 r2 #1 + #2 的端到端判据：只有 synthetic/ 被遍历，
    // 判定为 parent，合并与转换照常完成。
    const payload = fixturePayload();
    await page.addInitScript((dataFiles) => {
      const w = window as unknown as {
        showDirectoryPicker?: unknown;
        __pickedOptions?: unknown;
      };
      delete w.showDirectoryPicker;
      const table = dataFiles as Record<string, string>;
      const toU8 = (b64: string) => {
        const bin = atob(b64);
        const u = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
        return u;
      };
      const mkFile = (name: string, b64: string) => ({
        kind: "file", name,
        getFile: async () => new File([toU8(b64)], name),
      });
      const junkNames = Array.from({ length: 9000 }, (_v, i) => `pad${i}.dat`);
      const otherDir = {
        kind: "directory", name: "other",
        async *values() { for (const n of junkNames) yield mkFile(n, "QQ=="); },
      };
      const names = Object.keys(table).filter((n) => n !== "synthetic.mrxs");
      const syntheticDir = {
        kind: "directory", name: "synthetic",
        async *values() { for (const n of names) yield mkFile(n, table[n]); },
      };
      w.showDirectoryPicker = async (options?: unknown) => {
        w.__pickedOptions = options;
        return {
          kind: "directory", name: "Slides",
          async *values() { yield otherDir; yield syntheticDir; },
        };
      };
    }, payload);
    await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
    await page.goto("/tools/slides");
    await page.waitForFunction(() =>
      (window as unknown as { __stToolsReady?: boolean }).__stToolsReady === true);
    await page.setInputFiles("#file-input", MRXS_FILE);
    const panel = page.locator("#companion-section");
    await expect(panel).toBeVisible();
    await panel.locator("#companion-pick-btn").click();
    expect(await page.evaluate(() =>
      (window as unknown as { __pickedOptions?: Record<string, unknown> }).__pickedOptions))
      .toEqual({ id: "mrxs-companion", mode: "read" });
    // 摘要出现 = 只遍历了 synthetic/ 子树、判定 parent、planner 接受合并行
    await expect(page.locator("#summary-section")).toBeVisible({ timeout: 90_000 });
    await expect(page.locator("#summary-name")).toHaveText("synthetic.mrxs");
    await page.click("#convert-btn");
    await expect(page.locator("#result-section")).toBeVisible({ timeout: 240_000 });
  });

  test("父文件夹补选（webkitdirectory 回退；含其他切片的数据文件夹）→ 用 synthetic 子树", async ({ page }) => {
    test.setTimeout(300_000);
    await page.addInitScript(() => {
      localStorage.setItem("hp_lang", "zh");
      delete (window as unknown as { showDirectoryPicker?: unknown }).showDirectoryPicker;
    });
    await page.goto("/tools/slides");
    await page.waitForFunction(() =>
      (window as unknown as { __stToolsReady?: boolean }).__stToolsReady === true);
    await page.setInputFiles("#file-input", MRXS_FILE);
    const panel = page.locator("#companion-section");
    await expect(panel).toBeVisible();
    // PARENT_DIR 里 other/Slidedat.ini 与 synthetic/ 同场（webkitdirectory
    // 全量枚举，行序由浏览器决定）——判定必须挑 synthetic/ 子树
    await page.setInputFiles("#folder-input", PARENT_DIR);
    await expect(page.locator("#summary-section")).toBeVisible({ timeout: 90_000 });
    await expect(page.locator("#summary-name")).toHaveText("synthetic.mrxs");
    await expect(page.locator("#companion-section")).toBeHidden();
    await page.click("#convert-btn");
    await expect(page.locator("#result-section")).toBeVisible({ timeout: 240_000 });
  });

  test("用户取消目录选择器（AbortError）：安静保持「需要文件夹」状态", async ({ page }) => {
    test.setTimeout(120_000);
    await stubDirectoryPicker(page, { reject: { name: "AbortError" } });
    await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
    await page.goto("/tools/slides");
    await page.waitForFunction(() =>
      (window as unknown as { __stToolsReady?: boolean }).__stToolsReady === true);
    await page.setInputFiles("#file-input", MRXS_FILE);
    const panel = page.locator("#companion-section");
    await expect(panel).toBeVisible();
    await panel.locator("#companion-pick-btn").click();
    await expect(panel).toBeVisible();
    await expect(panel.locator("#companion-message"))
      .toContainText("这张切片还需要同名数据文件夹");
    await expect(page.locator("#page-error")).toBeHidden();
    await expect(page.locator("#summary-section")).toBeHidden();
    // stub 只被调用一次（没有重试风暴），面板仍可再次点击
    expect(await page.evaluate(() =>
      (window as unknown as { __pickerCalls?: number }).__pickerCalls)).toBe(1);
    await panel.locator("#companion-pick-btn").click();
    expect(await page.evaluate(() =>
      (window as unknown as { __pickerCalls?: number }).__pickerCalls)).toBe(2);
    await expect(panel).toBeVisible();
  });

  // 截图三连：zh 桌面 / en 桌面 / zh 移动 390px（提示、确认、拒绝三状态）
  for (const variant of [
    { key: "tools-zh-desktop", lang: "zh", mobile: false },
    { key: "tools-en-desktop", lang: "en", mobile: false },
    { key: "tools-zh-mobile", lang: "zh", mobile: true },
  ] as const) {
    test(`截图三态（${variant.key}）`, async ({ page }) => {
      test.setTimeout(300_000);
      await page.addInitScript((lang) => localStorage.setItem("hp_lang", lang as string), variant.lang);
      if (variant.mobile) {
        await page.setViewportSize({ width: 390, height: 844 });
      }
      await page.addInitScript(() => {
        delete (window as unknown as { showDirectoryPicker?: unknown }).showDirectoryPicker;
      });
      await page.goto("/tools/slides");
    await page.waitForFunction(() =>
      (window as unknown as { __stToolsReady?: boolean }).__stToolsReady === true);
      const panel = page.locator("#companion-section");

      await page.setInputFiles("#file-input", MRXS_FILE);
      await expect(panel).toBeVisible();
      await expect(panel.locator("#companion-message")).toContainText("synthetic");
      await shot(page, `${variant.key}-needs-folder`);

      await page.setInputFiles("#folder-input", WRONG_DIR);
      await expect(panel.locator("#companion-message"))
        .toContainText(variant.lang === "zh" ? "不是「synthetic」的数据文件夹"
          : "is not the data folder of");
      await shot(page, `${variant.key}-wrong-folder`);

      await page.setInputFiles("#folder-input", RENAMED_DIR);
      const dialog = page.locator("#disk-dialog");
      await expect(dialog).toBeVisible();
      await shot(page, `${variant.key}-mismatch`);
      await page.click("#disk-deny-btn");

      // 复核 r3 A：缺成员错误只说缺什么（en 桌面留一张重试态截图）
      await page.setInputFiles("#folder-input", INCOMPLETE_DIR);
      const pageError = page.locator("#page-error");
      await expect(pageError).toBeVisible();
      if (variant.lang === "en") {
        await expect(pageError).toContainText(
          "The MRXS data folder is incomplete: missing");
        await expect(pageError).not.toContainText("parent folder");
      } else {
        await expect(pageError).toContainText("MRXS 数据文件夹不完整");
      }
      await shot(page, `${variant.key}-planner-retry`);

      // en 版本抽查：协助面板此刻处于重试态（缺成员后的「重新选择」口径）
      if (variant.lang === "en") {
        await expect(panel.locator("#companion-message"))
          .toContainText("please choose the “synthetic” folder again");
      }
    });
  }
});

// --------------------------------------------------------------------------- //
// 工作台（/app；需登录 + 假 COS 基建 → 与 upload-inline-convert.spec 同门控）
// --------------------------------------------------------------------------- //

test.skip(!CREDS.inlineConvert,
  "未装配 inline-convert E2E 基建（需 E2E_INLINE_CONVERT=1 启动 e2e_server）");

test.describe("工作台：MRXS 同名数据文件夹协助（行内）", () => {
  test.beforeEach(() => { ensureFixtures(); });

  test("选 .mrxs → 行提示选同名文件夹 → 回退补选 → 不重选 .mrxs 转换并上传完成", async ({ page }) => {
    test.setTimeout(360_000);
    await page.addInitScript(() => {
      localStorage.setItem("hp_lang", "zh");
      delete (window as unknown as { showDirectoryPicker?: unknown }).showDirectoryPicker;
    });
    await login(page);
    await page.goto("/app");
    await page.waitForLoadState("load");
    await expandSidebar(page);

    // 1) 只选 .mrxs → 行进入 need_folder（非错误；点名词干的说明与按钮）
    await page.setInputFiles("#file-input", MRXS_FILE);
    await expect.poll(async () => (await entryOf(page))?.state, {
      timeout: 30_000, intervals: [250],
    }).toBe("need_folder");
    const entry0 = await entryOf(page);
    expect((entry0?.companion as { stem?: string } | null)?.stem).toBe("synthetic");
    expect(entry0?.error ?? null).toBeNull();
    expect(entry0?.jobId ?? null).toBeNull();   // 没建任何任务（零复制）
    expect(entry0?.bundle ?? null).toBeNull();
    const row = panelRows(page).last();
    await expect(row).toContainText("这张切片还需要同名数据文件夹");
    await expect(row).toContainText("选择「synthetic」文件夹");
    await shot(page, "app-zh-desktop-needs-folder");

    // 2) 错误文件夹（回退 webkitdirectory）：行内明确拒绝并保持可重选
    const [chooser1] = await Promise.all([
      page.waitForEvent("filechooser"),
      row.locator(".upload-item-btn", { hasText: "选择「synthetic」文件夹" }).click(),
    ]);
    await chooser1.setFiles(WRONG_DIR);
    await expect.poll(async () => {
      const e = await entryOf(page);
      return (e?.companionError as { folder?: string } | null)?.folder;
    }, { timeout: 15_000, intervals: [250] }).toBe("synthetic");
    await expect(row).toContainText("不是「synthetic」的数据文件夹");
    await shot(page, "app-zh-desktop-wrong-folder");

    // 3) 不同名但数据完整：行内确认（「使用此文件夹」）
    const [chooser2] = await Promise.all([
      page.waitForEvent("filechooser"),
      row.locator(".upload-item-btn", { hasText: "选择「synthetic」文件夹" }).click(),
    ]);
    await chooser2.setFiles(RENAMED_DIR);
    await expect.poll(async () => (await entryOf(page))?.state, {
      timeout: 15_000, intervals: [250],
    }).toBe("companion_confirm");
    await expect(row).toContainText("所选文件夹「renamed_data」与切片「synthetic」不同名");
    await shot(page, "app-zh-desktop-mismatch");

    // 4) 确认 → 原地转整包：真实 WASM 转换 → 假 COS 上传 → done
    await row.locator(".upload-item-btn", { hasText: "使用此文件夹" }).click();
    await waitSettled(page);
    const entry1 = await entryOf(page);
    expect(entry1?.state).toBe("done");
    expect(entry1?.slideId ?? null).toBeTruthy();
    expect((entry1?.bundle as { folderName?: string } | null)?.folderName).toBe("synthetic");
    // 只入队过这一行（从未要求重选 .mrxs）
    expect(await panelRows(page).count()).toBe(1);
  });

  test("用户取消目录选择器：行安静保持 need_folder；「重新选择文件」与取消入口在", async ({ page }) => {
    test.setTimeout(120_000);
    await stubDirectoryPicker(page, { reject: { name: "AbortError" } });
    await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
    await login(page);
    await page.goto("/app");
    await page.waitForLoadState("load");
    await expandSidebar(page);
    await page.setInputFiles("#file-input", MRXS_FILE);
    await expect.poll(async () => (await entryOf(page))?.state, {
      timeout: 30_000, intervals: [250],
    }).toBe("need_folder");
    const row = panelRows(page).last();
    await row.locator(".upload-item-btn", { hasText: "选择「synthetic」文件夹" }).click();
    await expect.poll(async () => (await entryOf(page))?.state, {
      timeout: 15_000, intervals: [250],
    }).toBe("need_folder");
    await expect(row).toContainText("这张切片还需要同名数据文件夹");
    // 重新选择文件 / 取消入口都可用
    await expect(row.locator(".upload-item-btn", { hasText: "重新选择文件" })).toBeVisible();
    await expect(row.locator(".upload-item-btn", { hasText: "取消" })).toBeVisible();
  });

  test("重命名冲突的行内确认可「重新选择」回到待选态", async ({ page }) => {
    test.setTimeout(180_000);
    await page.addInitScript(() => {
      localStorage.setItem("hp_lang", "zh");
      delete (window as unknown as { showDirectoryPicker?: unknown }).showDirectoryPicker;
    });
    await login(page);
    await page.goto("/app");
    await page.waitForLoadState("load");
    await expandSidebar(page);
    await page.setInputFiles("#file-input", MRXS_FILE);
    await expect.poll(async () => (await entryOf(page))?.state, {
      timeout: 30_000, intervals: [250],
    }).toBe("need_folder");
    const row = panelRows(page).last();
    const [chooser] = await Promise.all([
      page.waitForEvent("filechooser"),
      row.locator(".upload-item-btn", { hasText: "选择「synthetic」文件夹" }).click(),
    ]);
    await chooser.setFiles(RENAMED_DIR);
    await expect.poll(async () => (await entryOf(page))?.state, {
      timeout: 15_000, intervals: [250],
    }).toBe("companion_confirm");
    // 复核 r3 C：确认态的「重新选择」回的是**文件夹**（专用文案）
    const reselectBtn = row.locator(".upload-item-btn", { hasText: "重新选择文件夹" });
    await expect(reselectBtn).toBeVisible();
    await reselectBtn.click();
    await expect.poll(async () => (await entryOf(page))?.state, {
      timeout: 15_000, intervals: [250],
    }).toBe("need_folder");
    await expect(row).toContainText("这张切片还需要同名数据文件夹");

    // 复核 r2 #4：补选「缺成员」的数据文件夹 → planner 类型化拒绝 → 行内
    // 回到可重选状态（重试说明 + 带切片名的选择文件夹主按钮，is-primary）
    const [chooser3] = await Promise.all([
      page.waitForEvent("filechooser"),
      row.locator(".upload-item-btn", { hasText: "选择「synthetic」文件夹" }).click(),
    ]);
    await chooser3.setFiles(INCOMPLETE_DIR);
    await expect.poll(async () => (await entryOf(page))?.state, {
      timeout: 30_000, intervals: [250],
    }).toBe("convert_failed");
    const entryR = await entryOf(page);
    expect(entryR?.companionRetry).toBe(true);
    expect((entryR?.companion as { stem?: string } | null)?.stem).toBe("synthetic");
    // 复核 r3 B：重试说明点名缺的文件
    await expect(row).toContainText(
      "所选文件夹缺少 synthetic/Index.dat、synthetic/Data0001.dat");
    await expect(row).toContainText("请重新选择「synthetic」文件夹");
    const retryBtn = row.locator(".upload-item-btn",
      { hasText: "选择「synthetic」文件夹" });
    await expect(retryBtn).toBeVisible();
    await expect(retryBtn).toHaveClass(/is-primary/);
    await shot(page, "app-zh-desktop-planner-retry");
    // 重选补全的数据文件夹 → 无需重选 .mrxs，原地恢复整包转换并完成
    const [chooser4] = await Promise.all([
      page.waitForEvent("filechooser"),
      retryBtn.click(),
    ]);
    await chooser4.setFiles(DATA_DIR);
    await waitSettled(page);
    expect((await entryOf(page))?.state).toBe("done");
  });

  // ---- 复核 r2 #3：行内按钮样式回归截图（转换中 / 转换失败 / 需要文件夹 /
  // 磁盘确认）——.upload-item-btn 之前完全无样式，主动作新增 is-primary
  test("行状态截图（按钮样式）：转换中 / 转换失败 / 需要文件夹 / 磁盘确认", async ({ page }) => {
    test.setTimeout(360_000);
    const kfb = ensureKfbFixture();
    await page.addInitScript(() => {
      localStorage.setItem("hp_lang", "zh");
      delete (window as unknown as { showDirectoryPicker?: unknown }).showDirectoryPicker;
      // 磁盘门（uncertain）开关：置真后 estimate 不给 quota → need_disk_confirm
      const real = navigator.storage.estimate.bind(navigator.storage);
      navigator.storage.estimate = async () => {
        const w = window as unknown as { __diskFail?: boolean };
        return w.__diskFail ? {} : real();
      };
    });
    await login(page);
    await page.goto("/app");
    await page.waitForLoadState("load");
    await expandSidebar(page);

    // 1) 转换中的行（取消按钮）
    await page.setInputFiles("#file-input", kfb);
    await expect.poll(async () => (await lastEntryOf(page))?.state, {
      timeout: 30_000, intervals: [250],
    }).toBe("converting");
    await shot(page, "row-zh-desktop-converting");
    await expectButtonsInsideRows(page);
    await waitSettled(page, ["done", "convert_failed", "upload_failed"]);

    // 2) 转换失败的行（VMS 散入口：静态 need_bundle 说明 + 选择文件夹）
    await page.setInputFiles("#file-input", VMS_FILE);
    await expect.poll(async () => (await lastEntryOf(page))?.state, {
      timeout: 30_000, intervals: [250],
    }).toBe("convert_failed");
    await shot(page, "row-zh-desktop-convert-failed");

    // 3) 需要文件夹的行（主按钮 is-primary；权限提示在附注里）
    await page.setInputFiles("#file-input", MRXS_FILE);
    await expect.poll(async () => (await lastEntryOf(page))?.state, {
      timeout: 30_000, intervals: [250],
    }).toBe("need_folder");
    const row = panelRows(page).last();
    await expect(row).toContainText("浏览器会请求允许读取该文件夹");
    const pickBtn = row.locator(".upload-item-btn", { hasText: "选择「synthetic」文件夹" });
    await expect(pickBtn).toHaveClass(/is-primary/);
    await shot(page, "row-zh-desktop-need-folder");
    await expectButtonsInsideRows(page);

    // 4) 磁盘确认的行（继续（我已确认）/ 取消）
    await page.evaluate(() => {
      (window as unknown as { __diskFail?: boolean }).__diskFail = true;
    });
    const [chooser] = await Promise.all([
      page.waitForEvent("filechooser"),
      pickBtn.click(),
    ]);
    await chooser.setFiles(DATA_DIR);
    await expect.poll(async () => (await lastEntryOf(page))?.state, {
      timeout: 30_000, intervals: [250],
    }).toBe("need_disk_confirm");
    await expect(row).toContainText("浏览器只报告了");
    await shot(page, "row-zh-desktop-need-disk-confirm");
    await expectButtonsInsideRows(page);
  });

  // 截图三连（zh 桌面 / zh 移动）：提示、拒绝、确认
  for (const variant of [
    { key: "app-zh-desktop", lang: "zh", mobile: false },
    { key: "app-zh-mobile", lang: "zh", mobile: true },
    { key: "app-en-desktop", lang: "en", mobile: false },
  ] as const) {
    test(`截图三态（${variant.key}）`, async ({ page }) => {
      test.setTimeout(300_000);
      await page.addInitScript((lang) => localStorage.setItem("hp_lang", lang as string), variant.lang);
      if (variant.mobile) await page.setViewportSize({ width: 390, height: 844 });
      await page.addInitScript(() => {
        delete (window as unknown as { showDirectoryPicker?: unknown }).showDirectoryPicker;
      });
      await login(page);
      await page.goto("/app");
      await page.waitForLoadState("load");
      await expandSidebar(page);
      await page.setInputFiles("#file-input", MRXS_FILE);
      await expect.poll(async () => (await entryOf(page))?.state, {
        timeout: 30_000, intervals: [250],
      }).toBe("need_folder");
      const row = panelRows(page).last();
      const chooseLabel = variant.lang === "zh"
        ? "选择「synthetic」文件夹" : "Choose the “synthetic” folder";
      const chooseBtn = row.locator(".upload-item-btn", { hasText: chooseLabel });
      await expect(chooseBtn).toBeVisible();
      await expect(row).toContainText("synthetic");
      await shot(page, `${variant.key}-needs-folder`);

      // 文件夹的提供：桌面端走行按钮 → 隐藏 input 的 filechooser（真实手势
      // 路径）。移动端（390px）：行按钮在抽屉布局下的 hit-test 不可达
      //（元素在视口外——上传行移动端布局的既有情况，非本次改动引入），改用
      // 程序化 click 走同一按钮处理器（pendingCompanion → 隐藏 input）：
      // 选择器事件被拦截就直接 setFiles；否则 pendingCompanion 已就位，对
      // 隐藏 input setInputFiles（change → importPickFolder → 协助分支）。
      const provision = async (dir: string) => {
        if (variant.mobile) {
          const [c] = await Promise.all([
            page.waitForEvent("filechooser", { timeout: 5_000 }).catch(() => null),
            chooseBtn.evaluate((el) => (el as HTMLButtonElement).click()),
          ]);
          if (c) {
            await c.setFiles(dir);
            return;
          }
          await page.setInputFiles("#import-folder-input", dir);
          return;
        }
        const [c] = await Promise.all([
          page.waitForEvent("filechooser"),
          chooseBtn.click(),
        ]);
        await c.setFiles(dir);
      };
      await provision(WRONG_DIR);
      await expect.poll(async () => {
        const e = await entryOf(page);
        return (e?.companionError as { folder?: string } | null)?.folder;
      }, { timeout: 15_000, intervals: [250] }).toBe("synthetic");
      await expect(row).toContainText(variant.lang === "zh"
        ? "不是「synthetic」的数据文件夹" : "is not the data folder of");
      await shot(page, `${variant.key}-wrong-folder`);

      await provision(RENAMED_DIR);
      await expect.poll(async () => (await entryOf(page))?.state, {
        timeout: 15_000, intervals: [250],
      }).toBe("companion_confirm");
      await expect(row).toContainText(variant.lang === "zh"
        ? "不同名" : "does not match the slide name");
      await shot(page, `${variant.key}-mismatch`);
      // 布局断言（复核 r3 #4）：390px 下行内按钮同样不得撑破行宽
      //（行已布局且可见时才检查）
      if (await panelRows(page).first().isVisible().catch(() => false)) {
        await expectButtonsInsideRows(page);
      }
    });
  }
});
