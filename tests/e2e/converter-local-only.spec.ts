/**
 * 独立转换器本地化验收（2026-10-20 用户决定：/tools/slides 只做本地转换与
 * 本地保存；上传与交接整体退役）。真实 Chromium 网络断言：
 *
 *  1. 选文件 → 转换 →（可取消/续跑）全程只有静态代码/引擎加载——零
 *     /api/*（上传能力/登录信息/ingestion）、零跨源对象存储 PUT、零文件字节；
 *  2. 不登录可用（页面公开；保存走本机 picker，不发请求）；
 *  3. 历史 pending 上传意图/上传回执：打开页面、继续转换、保存都不发上传
 *     请求，不丢产物、不静默取消其他任务；
 *  4. 页面不再加载 COS 上传引擎（cos-uploader.js），无任何上传入口；
 *     已兼容文件提示「此文件无需转换…如需上传，请前往工作台」。
 */
import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
  process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
  "utf8") as string) as { inlineConvert?: boolean };

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

/** 最小 OME-TIFF（与 upload-recovery.spec.ts 同一构造）。 */
function ensureOmeTiff(): string {
  mkdirSync(FIXTURE_DIR, { recursive: true });
  const out = join(FIXTURE_DIR, "probe.ome.tif");
  if (existsSync(out)) return out;
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
open(${JSON.stringify(out)}, 'wb').write(header + ifd + ome + b'\\x00' + b'\\x00' * 16)
`], { cwd: repo, stdio: "inherit" });
  return out;
}

/** 请求分类：/api/* 与跨源对象存储 PUT 是「上传面」；静态资源不是。 */
function isApi(url: string): boolean {
  return new URL(url).pathname.startsWith("/api/");
}
function isCrossOriginPut(url: string, method: string): boolean {
  const u = new URL(url);
  return method === "PUT" && u.port !== String(PORT);
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
});

test.afterEach(async ({ page }) => {
  // 转换器不上传（零任务）；防御性清场：取消本页可能遗留的 ingestion
  try {
    await page.evaluate(async () => {
      const csrf = decodeURIComponent(
        (document.cookie.split("; ").find((x) => x.startsWith("csrf_token=")) || "")
          .split("=").slice(1).join("=") || "");
      for (const j of JSON.parse(localStorage.getItem("pt.cos.jobs") || "[]")) {
        if (j && j.job_id) {
          try {
            await fetch("/api/ingestions/" + encodeURIComponent(j.job_id) + "/cancel",
              { method: "POST", headers: { "X-CSRF-Token": csrf } });
          } catch { /* 尽力 */ }
        }
      }
    });
  } catch { /* 页面已关闭 */ }
});

test("转换器本地转换全程零 API/零对象存储；不登录可用；无上传入口", async ({ page }) => {
  test.setTimeout(240_000);
  const kfb = ensureKfb(KFB_W, KFB_H);
  const apiCalls: string[] = [];
  const cosPuts: string[] = [];
  const staticScripts: string[] = [];
  page.on("request", (r) => {
    const url = r.url();
    if (isApi(url)) apiCalls.push(url);
    if (isCrossOriginPut(url, r.method())) cosPuts.push(url);
    if (/\/static\/upload\/cos-uploader\.js/.test(url)) staticScripts.push(url);
  });

  // 不登录直接打开（页面公开；匿名状态本身就是「无需登录」的验收）
  await page.goto("/tools/slides");
  await expect(page.locator("#drop-zone")).toBeVisible();

  // 页面结构：本地处理说明 + 前往工作台；无任何上传入口/上传引擎
  // 2026-10-21 视觉打磨：隐私说明并入「完全本地处理」列后，摘要简化为
  // 「需要上传？请前往工作台。」（完整说明仍在展开内容里）。
  await expect(page.locator("#privacy-details summary"))
    .toContainText("需要上传？请前往工作台。");
  await expect(page.locator('a[href="/app"]:has([data-i18n="tools.nav.workbench"])')).toHaveCount(1);
  await expect(page.locator("#convert-upload-btn")).toHaveCount(0);
  await expect(page.locator("#upload-btn")).toHaveCount(0);
  await expect(page.locator("#direct-upload-btn")).toHaveCount(0);
  await expect(page.locator("#upload-block")).toHaveCount(0);
  await expect(page.locator("#account-dialog")).toHaveCount(0);

  // 选择文件 → 转换：FileChooser 真实用户路径
  const chooserPromise = page.waitForEvent("filechooser");
  await page.locator("#pick-file-btn").click();
  const chooser = await chooserPromise;
  await chooser.setFiles(kfb);

  // 准备（复制+识别）开始并完成，配置摘要出现
  await expect(page.locator("#stage-section")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("#summary-section")).toBeVisible({ timeout: 120_000 });

  // 开始转换 → 结果面板（真 WASM；全程断言零 API/零 PUT）
  await page.locator("#convert-btn").click();
  await expect(page.locator("#result-section")).toBeVisible({ timeout: 240_000 });
  await expect(page.locator("#result-grid")).toContainText("ome");

  // 全程：零 /api/*（上传能力/登录信息/ingestion 都属 /api/*）、零跨源 PUT
  expect(apiCalls, `API calls: ${apiCalls.join(", ")}`).toEqual([]);
  expect(cosPuts).toEqual([]);
  // COS 上传引擎不再被加载
  expect(staticScripts).toEqual([]);
  expect(await page.evaluate(() => (window as any).HP_COS_UPLOAD)).toBeUndefined();

  // 已兼容文件（OME-TIFF）：提示无需转换 + 前往工作台，仍零请求
  const chooser2 = page.waitForEvent("filechooser");
  await page.locator("#pick-file-btn").click();
  await (await chooser2).setFiles(ensureOmeTiff());
  await expect(page.locator("#input-message"))
    .toContainText("此文件无需转换，可直接使用；如需上传，请前往工作台", { timeout: 15_000 });
  await expect(page.locator("#input-message")).toContainText("前往工作台");
  expect(apiCalls).toEqual([]);
  expect(cosPuts).toEqual([]);
});

test("历史 pending 上传意图：继续转换不自动上传；删除经双重确认且零请求", async ({ page }) => {
  test.setTimeout(300_000);
  const kfb = ensureKfb(KFB_W, KFB_H);
  const apiCalls: string[] = [];
  const ingestionCalls: string[] = [];
  const cosPuts: string[] = [];
  page.on("request", (r) => {
    const url = r.url();
    if (isApi(url)) apiCalls.push(url);
    if (/\/api\/ingestions/.test(new URL(url).pathname)) ingestionCalls.push(url);
    if (isCrossOriginPut(url, r.method())) cosPuts.push(url);
  });

  await page.goto("/tools/slides");
  // 用页面同源 runner 模块准备一个 PREPARED 任务（probe 完成未开始；零网络；
  // 文件字节经 base64 注入——页面 fetch 不到文件系统路径），再注历史 pending
  // 上传意图 + 未收口上传记录（旧版形态；「开始」按旧版会在转换后自动上传）
  const b64 = readFileSync(kfb).toString("base64");
  const jobId = await page.evaluate(async (b64: string) => {
    try {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const f = new File([bytes], "legacy.kfb",
        { type: "application/octet-stream", lastModified: 123 });
      const mod = await import("/static/tools/slide-transform/runner.js");
      const runner = await mod.SlideToolsRunner.create();
      const prep = await runner.probe(f, {});
      // 历史 pending 上传意图（旧版「继续转换并上传」的执行依据）。遗留
      // 上传记录只在 ready/exported 任务上合法（运行器拒绝 prepared 挂
      // upload）——「开始」后按新语义停在结果面板、意图原样保留，即
      // 「不自动上传」的完整观察面。
      await runner.setJobIntent(prep.jobId, {
        state: "pending", account: "", channel: "tool",
        target: { pid: "legacy-project" }, createdAt: new Date().toISOString(),
      });
      return prep.jobId;
    } catch (e) {
      // 运行器的类型化错误是普通对象——转成带内容的 Error 才能进测试报告
      throw new Error("seed failed: " + JSON.stringify(e));
    }
  }, b64);
  expect(jobId).toBeTruthy();
  apiCalls.length = 0;
  ingestionCalls.length = 0;
  cosPuts.length = 0;

  // 刷新：任务列表恢复（不自动开始、零请求），意图不再出现「继续转换并上传」
  await page.reload();
  await page.waitForLoadState("load");
  await expect(page.locator("#jobs-details")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("#jobs-list")).toContainText("legacy.kfb", { timeout: 30_000 });
  await expect(page.locator("#jobs-list")).not.toContainText("继续转换并上传");
  await expect(page.locator('#jobs-list button[data-action="upload"],' +
    ' #jobs-list button[data-action="upload-continue"]')).toHaveCount(0);
  await page.waitForTimeout(3_000);
  expect(apiCalls, `刷新后 API: ${apiCalls.join(", ")}`).toEqual([]);
  expect(ingestionCalls).toEqual([]);
  expect(cosPuts).toEqual([]);

  // 继续（本地转换）：完成后停在结果面板——绝不自动上传；历史意图与遗留
  // 上传记录不被静默执行/改写（不丢产物、不取消其他任务）
  const start = page.locator('#jobs-list button[data-action="start"]').first();
  await expect(start).toBeVisible({ timeout: 30_000 });
  await start.click();
  await expect(page.locator("#result-section")).toBeVisible({ timeout: 240_000 });
  expect(ingestionCalls).toEqual([]);
  expect(cosPuts).toEqual([]);
  expect(apiCalls).toEqual([]);
  const state = await page.evaluate(async (id: string) => {
    const mod = await import("/static/tools/slide-transform/runner.js");
    const runner = await mod.SlideToolsRunner.create();
    const job = await runner.getJob(id);
    return { intent: job.intent && job.intent.state,
             upload: job.upload && job.upload.state,
             hasResult: !!(job.result && job.result.outputBytes) };
  }, jobId);
  expect(state.hasResult).toBe(true);      // 产物保留
  expect(state.intent).toBe("pending");    // 意图未被静默执行（也未标记 done）
  expect(state.upload).toBeFalsy();        // 零上传记录（全程未创建任务）

  // 布局断言（状态徽章 + 三个操作按钮）：桌面与 390px 下，徽章与按钮互不
  // 重叠、都不超出任务行（复核：390px 保存按钮压住「已完成」徽章）
  const jobRowIssues = (p: Page) => p.evaluate(() => {
    const out: string[] = [];
    document.querySelectorAll("#jobs-list .job-row").forEach((row) => {
      const rr = row.getBoundingClientRect();
      const cells = Array.from(
        row.querySelectorAll(".job-status-cell > *, .job-actions > *"));
      const boxes = cells.map((c) => ({
        text: (c.textContent || "").slice(0, 18),
        r: c.getBoundingClientRect(),
      })).filter((b) => !(b.r.width === 0 && b.r.height === 0));
      for (let i = 0; i < boxes.length; i++) {
        if (boxes[i].r.right > rr.right + 1 || boxes[i].r.left < rr.left - 1) {
          out.push(`超出行: "${boxes[i].text}" right=${boxes[i].r.right} rowRight=${rr.right}`);
        }
        for (let j = i + 1; j < boxes.length; j++) {
          const a = boxes[i].r, b = boxes[j].r;
          const separated = a.right <= b.left + 1 || b.right <= a.left + 1 ||
            a.bottom <= b.top + 1 || b.bottom <= a.top + 1;
          if (!separated) {
            out.push(`重叠: "${boxes[i].text}" 与 "${boxes[j].text}"`);
          }
        }
      }
    });
    return out;
  });
  expect(await jobRowIssues(page)).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await jobRowIssues(page)).toEqual([]);
  await page.setViewportSize({ width: 1280, height: 720 });
  expect(await jobRowIssues(page)).toEqual([]);

  // 删除：零网络（不再请求平台取消）
  page.on("dialog", (d) => d.accept());
  await page.locator(`[data-job-discard="${jobId}"]`).click();
  await expect(page.locator(`[data-job-discard="${jobId}"]`)).toHaveCount(0,
    { timeout: 30_000 });
  expect(ingestionCalls).toEqual([]);
  expect(cosPuts).toEqual([]);
  expect(apiCalls).toEqual([]);
});
