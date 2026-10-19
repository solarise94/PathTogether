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

/** 桌面首入侧栏默认收起：行级操作前先展开（真实用户路径）。 */
async function ensureSidebarVisible(page: Page) {
  const collapsed = await page.locator("#sidebar").evaluate(
    (el) => el.classList.contains("collapsed"));
  if (collapsed) await page.locator("#menu-btn").click();
  await expect(page.locator("#fb-location")).toBeVisible({ timeout: 15_000 });
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
                 jobId: e.jobId, slideId: e.slideId, error: e.error } : null;
  }, index);
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
});

test.afterEach(async ({ page }) => {
  // 失败/超时路径也清场：取消本页遗留的服务器任务，释放每账号排队名额
  await cancelIngestions(page);
});

/** 尽力取消本页遗留的服务器任务（释放每账号 COS 排队名额，避免污染同
 *  服务器的后续用例；复核脚本 cancelLocalJobs 同款）。终态任务 404/409。 */
async function cancelIngestions(page: Page) {
  try {
    await page.evaluate(async () => {
      const csrf = decodeURIComponent(
        (document.cookie.split("; ").find((x) => x.startsWith("csrf_token=")) || "")
          .split("=").slice(1).join("=") || "");
      const ids = new Set<string>();
      try {
        const o = (window as any).HP_UPLOAD && (window as any).HP_UPLOAD.inlineOrch();
        for (const e of (o && o.list() || [])) if (e.uploadJobId) ids.add(e.uploadJobId);
      } catch { /* 无面板 */ }
      try {
        for (const j of JSON.parse(localStorage.getItem("pt.cos.jobs") || "[]")) {
          if (j && j.job_id) ids.add(String(j.job_id));
        }
      } catch { /* 无记录 */ }
      for (const id of ids) {
        try {
          await fetch("/api/ingestions/" + encodeURIComponent(id) + "/cancel",
            { method: "POST", headers: { "X-CSRF-Token": csrf } });
        } catch { /* 尽力 */ }
      }
    });
  } catch { /* 页面已关闭 */ }
}

// --------------------------------------------------------------------------- //
// (a) 内容锁持有 → 取消 → 释放锁 → 第二个文件上传成功；被取消条目零任务
// --------------------------------------------------------------------------- //
test("a 等内容锁期间取消：槽位释放、下一文件上传成功、被取消条目零任务", async ({ page }) => {
  test.setTimeout(300_000);
  // 直传类别文件（OME-TIFF）：uploadFileCos 持跨标签内容锁——内容锁场景
  // 必须走直传（转换产物的锁是 engine.uploadLockName(jobId)，另一把）
  const ome = join(FIXTURE_DIR, "probe.ome.tif");
  if (!existsSync(ome)) {
    mkdirSync(FIXTURE_DIR, { recursive: true });
    const repo = join(__dirname, "..", "..");
    execFileSync("python3", ["-c", `
import struct
ome = b'<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"><Image ID="Image:0"><Pixels DimensionOrder="XYCZT" Type="uint8" SizeX="4" SizeY="4" SizeZ="1" SizeC="1" SizeT="1"></Pixels></Image></OME>'
entries = [
    (254, 4, 1, 0), (256, 4, 1, 4), (257, 4, 1, 4), (258, 3, 1, 8),
    (259, 3, 1, 1), (262, 3, 1, 1),
    (270, 2, len(ome) + 1, 0),
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
open(${JSON.stringify(ome)}, 'wb').write(header + ifd + ome + b'\\x00' + b'\\x00' * 16)
`], { cwd: repo, stdio: "inherit" });
  }

  const creates: string[] = [];
  page.on("request", (r) => {
    if (new URL(r.url()).pathname === "/api/ingestions" && r.method() === "POST") {
      creates.push(r.url());
    }
  });

  await openWorkbench(page);
  await expect.poll(() =>
    page.evaluate(() => !!(window as any).HP_UPLOAD?.inlineEnabled())).toBe(true);
  await page.evaluate(async () => {
    (window as any).reviewIdentity = await fetch("/api/auth/info").then((r) => r.json());
  });

  // 复核脚本同款：同一 File 对象（name/size/lastModified 三元组一致 → 同一把
  // 内容锁）先持锁、再经真实 input change 进入上传。锁键 = 账号+name+size+
  // lastModified（cos-uploader.js contentLockName），文件字节必须与真 KFB 相同
  //（锁在嗅探/上传前就位，2MB base64 走 evaluate 结构化克隆）。
  const b64 = readFileSync(ome).toString("base64");
  await page.evaluate(async ({ b64, name }: { b64: string; name: string }) => {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const f = new File([bytes], name,
      { type: "application/octet-stream", lastModified: 123 });
    const key = (window as any).HP_COS_UPLOAD.contentLockName(
      (window as any).reviewIdentity.user_id, f);
    (window as any).reviewHeld =
      await (window as any).HP_COS_UPLOAD.acquireContentLock(key);
    // 同一 File 走真实 input（change 事件路径与用户选择一致）
    const dt = new DataTransfer();
    dt.items.add(f);
    const input = document.querySelector("#file-input") as HTMLInputElement;
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }, { b64, name: "locked.ome.tif" });
  await ensureSidebarVisible(page);
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

  // 释放外部锁，选择第二个正常文件 → 照常上传到可打开（同一文件身份）
  await page.evaluate(() => (window as any).reviewHeld.release());
  await page.evaluate(async ({ b64, name }: { b64: string; name: string }) => {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const f = new File([bytes], name,
      { type: "application/octet-stream", lastModified: 123 });
    const dt = new DataTransfer();
    dt.items.add(f);
    const input = document.querySelector("#file-input") as HTMLInputElement;
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }, { b64, name: "after-cancel.ome.tif" });
  await expect.poll(async () => (await entryState(page, 1))?.state, {
    timeout: 120_000, intervals: [500],
  }).toBe("done");
  const second = await entryState(page, 1);
  expect(second!.slideId ?? null).toMatch(/^sld_/);
  // 被取消条目零服务器任务（全程恰好一个 POST /api/ingestions，属于第二个）
  expect(creates.length).toBe(1);
});

// --------------------------------------------------------------------------- //
// (b) 直传 PUT 403 耗尽重试 → 「继续上传」→ 放行后同任务续传 → viewable
//     （直传变体；转换产物变体见下一个用例）
// --------------------------------------------------------------------------- //
test("b1 直传 PUT 403 耗尽重试后行内可续传：同任务 id、复用已确认分块、最终可查看", async ({ page }) => {
  test.setTimeout(420_000);
  // 真 OME-TIFF 夹具（合成 OME-TIFF；HP_SLIDE_SNIFF 头级判定
  // cls=ome-tiff / direct_class=ome-tiff）。体积 ≥3×256KiB（e2e 假 COS 的
  // COS_PART_BYTES）→ 冻结计划多分块，「复用已确认分块」才可观察。
  mkdirSync(FIXTURE_DIR, { recursive: true });
  const ome = join(FIXTURE_DIR, "probe-direct.ome.tif");
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
open(${JSON.stringify(ome)}, 'wb').write(header + ifd + ome + b'\\x00' + b'\\x00' * (700 * 1024))
`], { cwd: repo, stdio: "inherit" });
  }

  const resumePuts: string[] = [];
  const createBodies: Array<Record<string, unknown>> = [];
  let denyPuts = true;
  let part1Sent = false;
  await page.route("**/*", (route) => {
    const r = route.request();
    const u = new URL(r.url());
    if (r.method() === "PUT" && u.port !== String(PORT)) {
      const part = u.searchParams.get("partNumber") || "?";
      if (denyPuts) {
        // 只放行分块 1 一次（它被服务端确认）；其余 PUT 一律 403——重试耗尽
        // 后 upload_failed，此时已确认分块 = {1}，续传复用才有观察面
        if (part === "1" && !part1Sent) { part1Sent = true; return route.continue(); }
        return route.fulfill({ status: 403, body: "test transfer denied",
          headers: { "Access-Control-Allow-Origin": "*",
                     "Access-Control-Expose-Headers": "ETag" } });
      }
      resumePuts.push(part);
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
  page.on("console", (m) => {
    if (process.env.B1_DEBUG) console.log("[b1 console]", m.type(), m.text().slice(0, 200));
  });
  page.on("pageerror", (e) => {
    if (process.env.B1_DEBUG) console.log("[b1 pageerror]", String(e).slice(0, 300));
  });
  await page.setInputFiles("#file-input", ome);

  // 重试耗尽：行停 upload_failed，错误是分块/传输失败（不是格式问题）
  await expect.poll(async () => (await entryState(page, 0))?.state, {
    timeout: 240_000, intervals: [1_000],
  }).toBe("upload_failed");
  const failed = await entryState(page, 0);
  expect(["part", "network", "generic"]).toContain(failed!.error?.kind);
  expect(failed!.uploadJobId).toBeTruthy();
  const taskId = failed!.uploadJobId as string;
  expect(part1Sent).toBe(true);

  // 行内有「继续上传」恢复入口（修复前只剩「申请支持该格式」）
  await ensureSidebarVisible(page);   // 行在侧栏：操作前展开（真实用户路径）
  const row = panelRows(page).last();
  const resume = row.locator(".upload-item-btn", { hasText: "继续上传" });
  await expect(resume).toHaveCount(1);
  await expect(row.locator(".upload-item-btn", { hasText: "申请支持该格式" }))
    .toHaveCount(0);

  // 放行传输 → 同一任务续传：恰好一个 ingestion；已确认分块不重发
  denyPuts = false;
  try {
    await resume.click();
    await expect.poll(async () => (await entryState(page, 0))?.state, {
      timeout: 240_000, intervals: [1_000],
    }).toBe("done");
    const done = await entryState(page, 0);
    expect(done!.uploadJobId).toBe(taskId);
    expect(createBodies.length).toBe(1);
    expect(done!.slideId ?? null).toMatch(/^sld_/);
    // 已确认分块不重发：续传阶段绝不再次 PUT 分块 1（同任务续传跳过已确认块）
    expect(resumePuts).not.toContain("1");
    expect(resumePuts.length).toBeGreaterThan(0);
    // 平台侧真任务：拼接分块可被读取器打开（viewable 判据不变）
    const stitch = await (await page.request.get(
      `/api/__test/stitch?ingestion=${encodeURIComponent(taskId)}`)).json() as {
        state: string; open_error: string | null; reader: string | null;
      };
    expect(stitch.state).toBe("completed");
    expect(stitch.open_error).toBeNull();
    expect(stitch.reader).toBeTruthy();
  } finally {
    // 任务已完成/或中途失败：都清掉，释放每账号排队名额
    await cancelIngestions(page);
  }
});

// --------------------------------------------------------------------------- //
// (b2) 转换产物变体：KFB 面板内转换 → 上传阶段注入 403 → 继续上传同任务
// --------------------------------------------------------------------------- //
test("b2 转换产物上传失败后同任务续传：继续上传入口 + 同 ingestion 完成", async ({ page }) => {
  test.setTimeout(480_000);   // 真转换 + 单分块重试耗尽（秒级）+ 续传
  const smallW = 580, smallH = 300;
  const kfb = ensureKfb(smallW, smallH);

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
    timeout: 480_000, intervals: [1_000],
  }).toBe("upload_failed");
  const failed = await entryState(page, 0);
  expect(failed!.jobId).toBeTruthy();          // 本机转换任务在
  expect(failed!.uploadJobId).toBeTruthy();    // 服务器任务 id 在（恢复依据）
  const taskId = failed!.uploadJobId as string;

  await ensureSidebarVisible(page);   // 行在侧栏：操作前展开（真实用户路径）
  const row = panelRows(page).last();
  const resume = row.locator(".upload-item-btn", { hasText: "继续上传" });
  await expect(resume).toHaveCount(1);

  denyPuts = false;
  try {
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
    expect(stitch.levels[0]).toEqual([smallW, smallH]);
  } finally {
    await cancelIngestions(page);
  }
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

  // 小屏（≤768px 抽屉全宽覆盖）——进度不得依赖侧栏；行必须渲染在可见的
  // 抽屉面板内（后面按几何断言行在抽屉边界内）
  await page.setViewportSize({ width: 480, height: 860 });
  await openWorkbench(page);

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
  // 几何断言：行可见且完全落在可见的抽屉面板内（不是藏在收起的侧栏里）
  const rowBox = await drawerRow.boundingBox();
  const drawerBox = await page.locator("#import-drawer").boundingBox();
  expect(rowBox).toBeTruthy();
  expect(drawerBox).toBeTruthy();
  expect(rowBox!.x).toBeGreaterThanOrEqual(drawerBox!.x);
  expect(rowBox!.y).toBeGreaterThanOrEqual(drawerBox!.y);
  expect(rowBox!.y + rowBox!.height).toBeLessThanOrEqual(
    drawerBox!.y + drawerBox!.height + 1);
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
  await cancelIngestions(page);
});
