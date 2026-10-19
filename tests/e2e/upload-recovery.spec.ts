/**
 * 上传流程修复回归（review 2026-10-20 三处 P2；场景移植自
 * /mnt/ssd2/tests/run-review-upload-flow-VawJaz 的 check-upload-recovery.mjs
 * 与 check-picker-progress.mjs——断言由「bug 存在」翻转为「正确行为」）。
 *
 * (a) 等同一文件内容锁期间取消：槽位释放、下一个文件照常上传、
 *     被取消条目零服务器任务（P2 问题 1）；
 * (b) 直传 PUT 注入 403 直到重试耗尽 → 行内有「继续上传」→ 放行后同任务
 *     续传（恰好一个 ingestion，不重传已确认分块）→ viewable（P2 问题 2，
 *     直传 + 转换产物两个变体）；
 * (c) 真实点击路径 导入切片 → 选择文件（FileChooser），侧栏折叠 + 小屏：
 *     进度与取消在抽屉里，进行中不显示「暂无进行中的任务」（P2 问题 3）。
 *
 * 基建与 upload-inline-convert.spec.ts 相同（真实 Flask/PG/假 COS/真 WASM）。
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync, existsSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
  process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
  "utf8") as string) as {
  userLogin: string; userPassword: string; inlineConvert?: boolean;
};

const FIXTURE_DIR = process.env.E2E_INLINE_FIXTURE_DIR
  || join(process.env.TEST_RUN_DIR || tmpdir(), "inline-convert-fixtures");

const KFB_W = 4000;
const KFB_H = 3000;

test.skip(!CREDS.inlineConvert,
  "未装配 inline-convert E2E 基建（需 E2E_INLINE_CONVERT=1 启动 e2e_server）");

function ensureKfb(width: number, height: number): string {
  mkdirSync(FIXTURE_DIR, { recursive: true });
  const out = join(FIXTURE_DIR, `synth-${width}x${height}.kfb`);
  if (existsSync(out)) return out;
  const repo = join(__dirname, "..", "..");
  execFileSync("python3", ["-c", `
import sys
sys.path.insert(0, ${JSON.stringify(repo)})
from kfb.fixture import build_synthetic_kfb
build_synthetic_kfb(${JSON.stringify(out)}, width=${width}, height=${height})
`], { cwd: repo, stdio: "inherit" });
  return out;
}

async function login(page: Page) {
  await page.goto("/login");
  await page.fill('form[action="/login"] input[name="username"]', CREDS.userLogin);
  await page.fill('form[action="/login"] input[name="password"]', CREDS.userPassword);
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes("/login")),
    page.click('form[action="/login"] button[type="submit"]'),
  ]);
}

async function openWorkbench(page: Page, query = "") {
  await login(page);
  await page.goto("/app" + query);
  await page.waitForLoadState("load");
}

/** 面板行（带按钮宿主；与既有 spec 同口径）。 */
function panelRows(page: Page): Locator {
  return page.locator(".upload-item").filter({ has: page.locator(".upload-item-actions") });
}

async function entryState(page: Page, index = 0) {
  return page.evaluate((i) => {
    const o = (window as any).HP_UPLOAD.inlineOrch();
    const e = o && o.list()[i];
    return e ? { state: e.state, uploadJobId: e.uploadJobId,
                 jobId: e.jobId, error: e.error } : null;
  }, index);
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
});

// --------------------------------------------------------------------------- //
// (a) 内容锁持有 → 取消 → 释放锁 → 第二个文件上传成功；被取消条目零任务
// --------------------------------------------------------------------------- //
test("a 等内容锁期间取消：槽位释放、下一文件上传成功、被取消条目零任务", async ({ page }) => {
  test.setTimeout(300_000);
  const kfb = ensureKfb(KFB_W, KFB_H);

  const creates: string[] = [];
  page.on("request", (r) => {
    if (new URL(r.url()).pathname === "/api/ingestions" && r.method() === "POST") {
      creates.push(r.url());
    }
  });

  await openWorkbench(page);
  await expect.poll(() =>
    page.evaluate(() => !!(window as any).HP_UPLOAD?.inlineEnabled())).toBe(true);
  // 冻结身份证据（内容锁名需要账号）
  await page.evaluate(async () => {
    (window as any).reviewIdentity = await fetch("/api/auth/info").then((r) => r.json());
  });

  // 先持同文件内容锁（模拟另一转换器/旧标签正在操作该文件）——真 Web Lock
  await page.evaluate(async (file: { name: string; bytes: number[] }) => {
    const f = new File([new Uint8Array(file.bytes)], file.name,
      { type: "application/octet-stream", lastModified: 123 });
    const key = (window as any).HP_COS_UPLOAD.contentLockName(
      (window as any).reviewIdentity.user_id, f);
    (window as any).reviewHeld =
      await (window as any).HP_COS_UPLOAD.acquireContentLock(key);
  }, { name: "locked.kfb", bytes: [0x4b, 0x46, 0x42, 0x00, ...new Array(16).fill(0)] });

  await page.setInputFiles("#file-input", kfb);
  const firstRow = panelRows(page).last();
  await expect(firstRow).toContainText("另一个标签页", { timeout: 30_000 });
  await expect(firstRow).toContainText("可取消");

  // 真取消按钮：等锁中的行必须能收口（引擎未建、无任务）
  await firstRow.locator(".upload-item-btn", { hasText: "取消" }).click();
  await expect.poll(async () => (await entryState(page, 0))?.state, {
    timeout: 15_000, intervals: [200],
  }).toBe("cancelled");
  // 槽位已释放：busy.uploading 必须为 false（修复前永久 true）
  await expect.poll(() =>
    page.evaluate(() => (window as any).HP_UPLOAD.inlineOrch().busy().uploading),
    { timeout: 10_000 }).toBe(false);

  // 释放外部锁，选择第二个正常文件 → 照常转换并上传到可打开
  await page.evaluate(() => (window as any).reviewHeld.release());
  await page.setInputFiles("#file-input", kfb);
  await expect.poll(async () => (await entryState(page, 1))?.state, {
    timeout: 240_000, intervals: [500],
  }).toBe("done");
  const second = await entryState(page, 1);
  expect(second!.slideId ?? await page.evaluate(async () => {
    const o = (window as any).HP_UPLOAD.inlineOrch();
    return o.list()[1].slideId;
  })).toMatch(/^sld_/);
  // 被取消条目零服务器任务（零 POST /api/ingestions 给它）
  expect(creates.length).toBe(1);
});

// --------------------------------------------------------------------------- //
// (b) 直传 PUT 403 耗尽重试 → 「继续上传」→ 放行后同任务续传 → viewable
//     （直传变体；转换产物变体见下一个用例）
// --------------------------------------------------------------------------- //
test("b1 直传 PUT 403 耗尽重试后行内可续传：同任务 id、复用已确认分块、最终可查看", async ({ page }) => {
  test.setTimeout(420_000);
  // 真 OME-TIFF 头夹具（合成最小 OME-TIFF；HP_SLIDE_SNIFF 头级判定
  // cls=ome-tiff / direct_class=ome-tiff——直传类别由产物字节声明）
  mkdirSync(FIXTURE_DIR, { recursive: true });
  const ome = join(FIXTURE_DIR, "probe.ome.tif");
  if (!existsSync(ome)) {
    const repo = join(__dirname, "..", "..");
    execFileSync("python3", ["-c", `
import struct
ome = b'<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"><Image ID="Image:0"><Pixels DimensionOrder="XYCZT" Type="uint8" SizeX="4" SizeY="4" SizeZ="1" SizeC="1" SizeT="1"></Pixels></Image></OME>'
entries = [
    (254, 4, 1, 0), (256, 4, 1, 4), (257, 4, 1, 4), (258, 3, 1, 8),
    (259, 3, 1, 1), (262, 3, 1, 1),
    (270, 2, len(ome) + 1, 0),   # ImageDescription = OME-XML（sniff 判 ome-tiff）
    (273, 4, 1, 0), (277, 3, 1, 1), (278, 4, 1, 4), (279, 4, 1, 16),
    (284, 3, 1, 1),
]
entries.sort()
header = b'II' + struct.pack('<H', 42) + struct.pack('<I', 8)
n = len(entries)
desc_off = 8 + 2 + n * 12 + 4
strip_off = desc_off + len(ome) + 1
ifd = struct.pack('<H', n)
for t, typ, cnt, val in entries:
    if t == 270: val = desc_off
    if t == 273: val = strip_off
    ifd += (struct.pack('<HHI', t, typ, cnt) + (struct.pack('<HH', val, 0) if typ == 3 else struct.pack('<I', val)))
ifd += struct.pack('<I', 0)
open(${JSON.stringify(ome)}, 'wb').write(header + ifd + ome + b'\x00' + b'\x00' * 16)
`], { cwd: repo, stdio: "inherit" });
  }

  const puts: Array<{ url: string; partNumber: string | null }> = [];
  const createBodies: Array<Record<string, unknown>> = [];
  let denyPuts = true;
  await page.route("**/*", (route) => {
    const r = route.request();
    const u = new URL(r.url());
    if (r.method() === "PUT" && u.port !== String(PORT)) {
      const part = u.searchParams.get("partNumber");
      puts.push({ url: u.pathname, partNumber: part });
      if (denyPuts) {
        return route.fulfill({ status: 403, body: "test transfer denied",
          headers: { "Access-Control-Allow-Origin": "*",
                     "Access-Control-Expose-Headers": "ETag" } });
      }
      return route.continue();
    }
    return route.continue();
  });
  page.on("request", (r) => {
    if (new URL(r.url()).pathname === "/api/ingestions" && r.method() === "POST") {
      try { createBodies.push(JSON.parse(r.postData() || "{}")); } catch { /* */ }
    }
  });

  await openWorkbench(page);
  await expect.poll(() =>
    page.evaluate(() => !!(window as any).HP_UPLOAD?.inlineEnabled())).toBe(true);
  await page.setInputFiles("#file-input", ome);

  // 重试耗尽：行停 upload_failed，错误是分块/传输失败（不是格式问题）
  await expect.poll(async () => (await entryState(page, 0))?.state, {
    timeout: 240_000, intervals: [1_000],
  }).toBe("upload_failed");
  const failed = await entryState(page, 0);
  expect(["part", "network", "generic"]).toContain(failed!.error?.kind);
  expect(failed!.uploadJobId).toBeTruthy();
  const taskId = failed!.uploadJobId as string;
  const putsBefore = puts.length;
  expect(putsBefore).toBeGreaterThan(0);

  // 行内有「继续上传」恢复入口（修复前只剩「申请支持该格式」）
  const row = panelRows(page).last();
  const resume = row.locator(".upload-item-btn", { hasText: "继续上传" });
  await expect(resume).toHaveCount(1);
  await expect(row.locator(".upload-item-btn", { hasText: "申请支持该格式" }))
    .toHaveCount(0);

  // 放行传输 → 同一任务续传：恰好一个 ingestion；已确认分块不重发
  denyPuts = false;
  await resume.click();
  await expect.poll(async () => (await entryState(page, 0))?.state, {
    timeout: 240_000, intervals: [1_000],
  }).toBe("done");
  const done = await entryState(page, 0);
  expect(done!.uploadJobId).toBe(taskId);
  expect(createBodies.length).toBe(1);
  expect(done!.slideId ?? null).toMatch(/^sld_/);
  // 平台侧真任务：拼接分块可被读取器打开（viewable 判据不变）
  const stitch = await (await page.request.get(
    `/api/__test/stitch?ingestion=${encodeURIComponent(taskId)}`)).json() as {
      state: string; open_error: string | null; reader: string | null;
    };
  expect(stitch.state).toBe("completed");
  expect(stitch.open_error).toBeNull();
  expect(stitch.reader).toBeTruthy();
});

// --------------------------------------------------------------------------- //
// (b2) 转换产物变体：KFB 面板内转换 → 上传阶段注入 403 → 继续上传同任务
// --------------------------------------------------------------------------- //
test("b2 转换产物上传失败后同任务续传：继续上传入口 + 同 ingestion 完成", async ({ page }) => {
  test.setTimeout(420_000);
  const kfb = ensureKfb(KFB_W, KFB_H);

  const creates: Array<Record<string, unknown>> = [];
  let denyPuts = true;
  await page.route("**/*", (route) => {
    const r = route.request();
    const u = new URL(r.url());
    if (r.method() === "PUT" && u.port !== String(PORT)) {
      if (denyPuts) {
        return route.fulfill({ status: 403, body: "test transfer denied",
          headers: { "Access-Control-Allow-Origin": "*",
                     "Access-Control-Expose-Headers": "ETag" } });
      }
      return route.continue();
    }
    return route.continue();
  });
  page.on("request", (r) => {
    if (new URL(r.url()).pathname === "/api/ingestions" && r.method() === "POST") {
      try { creates.push(JSON.parse(r.postData() || "{}")); } catch { /* */ }
    }
  });

  await openWorkbench(page);
  await page.setInputFiles("#file-input", kfb);

  // 转换完成后进入上传；PUT 全 403 → 重试耗尽 → upload_failed（kind=part）
  await expect.poll(async () => (await entryState(page, 0))?.state, {
    timeout: 360_000, intervals: [1_000],
  }).toBe("upload_failed");
  const failed = await entryState(page, 0);
  expect(failed!.jobId).toBeTruthy();          // 本机转换任务在
  expect(failed!.uploadJobId).toBeTruthy();    // 服务器任务 id 在（恢复依据）
  const taskId = failed!.uploadJobId as string;

  const row = panelRows(page).last();
  const resume = row.locator(".upload-item-btn", { hasText: "继续上传" });
  await expect(resume).toHaveCount(1);

  denyPuts = false;
  await resume.click();
  await expect.poll(async () => (await entryState(page, 0))?.state, {
    timeout: 240_000, intervals: [1_000],
  }).toBe("done");
  const done = await entryState(page, 0);
  expect(done!.uploadJobId).toBe(taskId);      // 同一任务，未新建
  expect(creates.length).toBe(1);
  const stitch = await (await page.request.get(
    `/api/__test/stitch?ingestion=${encodeURIComponent(taskId)}`)).json() as {
      state: string; open_error: string | null; reader: string | null;
      levels: number[][];
    };
  expect(stitch.state).toBe("completed");
  expect(stitch.open_error).toBeNull();
  expect(stitch.reader).toBeTruthy();
  expect(stitch.levels[0]).toEqual([KFB_W, KFB_H]);
});

// --------------------------------------------------------------------------- //
// (c) 真实点击路径：导入切片 → 选择文件（FileChooser），侧栏折叠 + 小屏
//     进度/取消在抽屉里；进行中不显示「暂无进行中的任务」
// --------------------------------------------------------------------------- //
test("c 抽屉选择文件后进度与取消在抽屉内（侧栏折叠 + 小屏），空态不撒谎", async ({ page }) => {
  test.setTimeout(240_000);
  // 与 b1 同一最小 OME-TIFF（直传类别；选择路径验证与传输无关）
  const tif = join(FIXTURE_DIR, "probe.ome.tif");

  let releasePut: (() => void) | null = null;
  const gate = new Promise<void>((r) => { releasePut = r; });
  let puts = 0;
  await page.route("**/*", async (route) => {
    const r = route.request();
    if (r.method() === "PUT" && new URL(r.url()).port !== String(PORT)) {
      puts += 1;
      await gate;
    }
    return route.continue();
  });

  // 小屏 + 侧栏默认收起（桌面首入默认收起；小屏抽屉全宽覆盖）
  await page.setViewportSize({ width: 480, height: 860 });
  await openWorkbench(page);
  const collapsed = await page.locator("#sidebar").evaluate(
    (el) => el.classList.contains("collapsed"));
  if (!collapsed) await page.locator("#menu-btn").click();
  await expect(page.locator("#sidebar")).toHaveClass(/collapsed/);

  // 真实点击路径：导入切片（＋菜单）→ 导入抽屉 → 选择文件 → FileChooser
  await page.locator("#viewer-empty-upload").click();
  await expect(page.locator("#import-drawer")).toBeVisible();
  const chooserPromise = page.waitForEvent("filechooser");
  await page.locator("#import-pick-files").click();
  const chooser = await chooserPromise;
  await chooser.setFiles(tif);

  // 真的上传发出（跨源 PUT 至少一次），进度行在**抽屉内**可见
  await expect.poll(() => puts, { timeout: 60_000 }).toBeGreaterThan(0);
  const drawerRow = page.locator("#import-drawer .upload-item").first();
  await expect(drawerRow).toBeVisible();
  await expect(drawerRow).toContainText(/正在上传|正在准备上传文件/);
  // 行上有取消入口（进行中可取消）
  await expect(drawerRow.locator(".upload-item-btn, button", { hasText: "取消" }).first())
    .toBeVisible();
  // 「后台任务」空态不得撒谎：本机队列有任务时不显示「暂无进行中的任务」
  await expect(page.locator("#import-task-list"))
    .not.toContainText("暂无进行中的任务", { timeout: 10_000 });

  // 行级取消 → 收口；关闭抽屉后行容器移回侧栏（同一份 DOM）
  await drawerRow.locator(".upload-item-btn", { hasText: "取消" }).first().click();
  await expect.poll(async () => (await entryState(page, 0))?.state, {
    timeout: 20_000, intervals: [250],
  }).toBe("cancelled");
  releasePut!();
  await page.locator("#import-drawer-close").click();
  await expect(page.locator("#import-drawer")).toBeHidden();
});
