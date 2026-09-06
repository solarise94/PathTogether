/**
 * viewer 浏览器轨迹基准（image-transport-upgrade §7.3；可复现工具）。
 *
 * 真实 Flask + OpenSlide/tifffile + 嵌入 PG 的本地集成环境；**不用 route
 * mock**（§7.3 红线）；CDP Network 记录每个响应的 status / bytes(dataLength) /
 * fromDiskCache / fromMemoryCache。轨迹：首次打开 → 连续平移×3 → 缩放三档 →
 * 返回起始视野 → [精细切换（仅 candidate RGB）] → 正常重载。完成判定：
 * networkidle（可见 tile 请求完成），不用固定 sleep 充当清晰完成信号。
 *
 * 用法：node run_trajectory.mjs --base http://127.0.0.1:8931 --label candidate \
 *   --dpr 1 --repeats 3 --slide "1-NC肠(1) HE.svs" [--detail] [--out result.json]
 */
import { chromium } from "playwright";
import fs from "node:fs";

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  return i > 0 ? process.argv[i + 1] : dflt;
}
const has = (name) => process.argv.includes("--" + name);

const BASE = arg("base", "http://127.0.0.1:8931");
console.error("[traj] argv:", process.argv.join(" "));
console.error("[traj] has(--detail):", process.argv.includes("--detail"));
const LABEL = arg("label", "run");
const DPR = Number(arg("dpr", "1"));
const REPEATS = Number(arg("repeats", "3"));
const SLIDE = arg("slide");
const VIEWPORT = { width: Number(arg("w", "1440")), height: Number(arg("h", "900")) };

async function collectNetwork(context) {
  const pages = context.pages();
  const cdp = await pages.length
    ? pages[0].context().newCDPSession(pages[0])
    : null;
  return cdp;
}

let __reqCount = 0;
async function settle(page, quietMs = 900, timeoutMs = 12000) {
  // 完成判定：请求计数在 quietMs 内无增长（可见 tile 请求完成），
  // 不用固定 sleep 充当清晰完成信号；超时按已到状态继续（记录）
  const start = Date.now();
  let last = __reqCount, lastChange = Date.now();
  page.on("request", () => { __reqCount += 1; });
  while (Date.now() - start < timeoutMs) {
    await page.waitForTimeout(150);
    if (__reqCount !== last) { last = __reqCount; lastChange = Date.now(); }
    else if (Date.now() - lastChange >= quietMs) return;
  }
}

async function oneRun(browser) {
  console.error("[traj] in oneRun, has(--detail) =", process.argv.includes("--detail"));
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: DPR,
  });
  const page = await context.newPage();
  if (process.argv.includes("--start-detail")) {
    console.error("[traj] start-detail active");
    // §7.2 口径：偏好即精细的整轨迹（冷缓存首开即 detail），非切换双取
    await context.addInitScript(() => {
      try { window.localStorage.setItem("pt.viewerQuality.rgb", "native-detail-v1"); } catch (e) {}
    });
  }
  // CDP：Network 收集（body 字节 dataLength；缓存来源）
  const cdp = await context.newCDPSession(page);
  await cdp.send("Network.enable");
  const entries = [];
  cdp.on("Network.responseReceived", (ev) => {
    entries.push({ url: ev.response.url, status: ev.response.status,
                   mime: ev.response.mimeType, phase: currentPhase,
                   encodedDataLength: null, fromCache: null,
                   requestId: ev.requestId });
  });
  cdp.on("Network.loadingFinished", (ev) => {
    const e = entries.find((x) => x.requestId === ev.requestId);
    if (e) e.encodedDataLength = ev.encodedDataLength;
  });

  let currentPhase = "boot";

  const escaped = SLIDE.replace(/[\\^$.*+?()[\]{}|\\"/]/g, "\\$&");
  const slideSel = `.slide-row[data-name="${escaped}"]`;
  async function openSlide() {
    currentPhase = "first-open";
    await page.goto(BASE + "/", { waitUntil: "networkidle" });
    // 侧栏行在 1440px 布局里被折叠（w≈16px）无法命中点击；走 DOM click
    // 派发，仍触发 app.js 的 openSlide 处理器（非 route mock、非 sleep）
    await page.evaluate((sel) => {
      document.querySelector(sel).click();
    }, slideSel);
    await settle(page);
  }
  async function drag(dx, dy) {
    const box = await page.locator("#viewer").boundingBox();
    const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
    await page.mouse.move(cx, cy);
    await page.mouse.down();
    await page.mouse.move(cx + dx, cy + dy, { steps: 8 });
    await page.mouse.up();
    await settle(page);
  }
  await openSlide();
  // 平移 ×3
  currentPhase = "pan";
  await drag(160, 90); await drag(-220, 60); await drag(80, -160);
  // 缩放三档（ctrl+wheel 缩放；OSD 滚轮即缩放）
  currentPhase = "zoom";
  const box = await page.locator("#viewer").boundingBox();
  const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
  for (const dy of [-240, -240, 240]) {
    await page.mouse.move(cx, cy);
    await page.mouse.wheel(0, dy);
    await settle(page);
  }
  // 返回起始视野：复位按钮
  currentPhase = "reset";
  await page.locator("#reset-btn").click();
  await settle(page);
  // 精细切换（candidate only）
  if (process.argv.includes("--detail")) {
    currentPhase = "detail-toggle";
    const btn = page.locator(
      '#quality-control .viewer-quality-btn[data-i18n="quality.detail"]');
    console.error("[traj] detail btn count:", await btn.count());
    if (await btn.count()) {
      await btn.evaluate((el) => el.click());   // 与行点击同口径：DOM 派发
      await settle(page);
    }
  }
  // 荧光通道切换（仅 MC + --channel-switch）：勾选下一通道 → applySelection
  if (process.argv.includes("--channel-switch")) {
    currentPhase = "channel-switch";
    await page.evaluate(() => {
      const boxes = document
        .querySelectorAll('#channel-panel input[type="checkbox"]');
      for (const cb of boxes) {
        if (!cb.checked) { cb.click(); break; }
      }
    });
    await settle(page, 1200);
  }
  // 正常重载
  currentPhase = "reload";
  await page.reload({ waitUntil: "networkidle" });
  await page.evaluate((sel) => {
    document.querySelector(sel).click();
  }, slideSel);
  await settle(page);

  await context.close();
  // 汇总：剔除静态资源与 API（只统计瓦片/缩略图/info/dzi 相关图像传输）
  const relevant = entries.filter((e) =>
    /_files\/\d+\/\d+_\d+\.jpeg|\/thumbnail|\.dzi|\/info(\?|$)|render-context/.test(e.url));
  const totalBytes = relevant.reduce((s, e) => s + (e.encodedDataLength || 0), 0);
  const tileBytes = relevant.filter((e) => /_files\//.test(e.url))
    .reduce((s, e) => s + (e.encodedDataLength || 0), 0);
  const counts = {};
  for (const e of relevant) counts[e.phase] = (counts[e.phase] || 0) + 1;
  const statuses = {};
  for (const e of relevant) statuses[e.status] = (statuses[e.status] || 0) + 1;
  return { label: LABEL, dpr: DPR, totalBytes, tileBytes,
           requests: relevant.length, counts, statuses,
           urls: relevant.map((e) => ({ u: e.url.slice(0, 160), b: e.encodedDataLength, s: e.status, p: e.phase })) };
}

const browser = await chromium.launch();
const runs = [];
for (let i = 0; i < REPEATS; i++) {
  runs.push(await oneRun(browser));
  console.error(`[traj] ${LABEL} dpr=${DPR} run ${i + 1}/${REPEATS} done`);
}
await browser.close();
const med = (xs) => xs.slice().sort((a, b) => a - b)[Math.floor(xs.length / 2)];
const summary = {
  label: LABEL, dpr: DPR, repeats: REPEATS, viewport: VIEWPORT,
  totalBytes_median: med(runs.map((r) => r.totalBytes)),
  tileBytes_median: med(runs.map((r) => r.tileBytes)),
  requests_median: med(runs.map((r) => r.requests)),
  runs,
};
const out = arg("out");
if (out) fs.writeFileSync(out, JSON.stringify(summary, null, 1));
console.log(JSON.stringify({ label: LABEL, dpr: DPR,
  totalBytes_median: summary.totalBytes_median,
  tileBytes_median: summary.tileBytes_median,
  requests_median: summary.requests_median }));
