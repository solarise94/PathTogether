#!/usr/bin/env node
// bf-ome 发布包查看器浏览器回归（Playwright Chromium + 真实 Flask /app）。
//
// 被测对象：server.py --seed-bfome 播种的 ready 切片（id_bundle，发布入口
// 字节 = 给定 bf-ome 产物；读取走真实 slide_io.open_slide，不 patch 读取器）。
// 验收口径：
//   1. 登录 → 建真实项目（含该切片）→ 工作台侧栏项目展开 → 点切片行打开；
//   2. OpenSeadragon 实际拉到 /api/slides/<id>/tiles/<level>/<col>_<row>.jpeg
//      200 + image/jpeg，覆盖 ≥3 个 DeepZoom 层级**且含最高分辨率层**
//      （home 低倍 → 鼠标滚轮中间层级 → 1:1 按钮（viewport.zoomTo 原生））；
//   3. info：image_mode=="native_rgb"、channels 空、mpp 来自 metadata、
//      UI 无通道面板；倍率徽章按 mpp 出倍率（非百分比）；
//   4. 渲染画布非空白且呈组织样（粉/紫）均值；低倍与全分辨率各取证一次；
//   5. 瓦片色彩对照：另取同一批瓦片 URL 的字节，与 classic 参照按相同
//      DeepZoom 几何（openslide DeepZoomGenerator 512/1/limit_bounds，仅用于
//      参照坐标）由同目录 Python 助手（bfome_tilecmp.py）逐张比对（JPEG 再编码小容差、
//      不允许通道互换）；
//   6. 重开：整页 reload + 全新浏览器上下文，项目→切片→瓦片再次 200 且
//      info 不变。
//
//   node tests/browser/slide_tools_c4/run_bfome_viewer.js \
//     --input <bf-ome.ome.tif> --reference <classic.tif> \
//     [--port 8967] [--out-dir <dir>] [--keep|--reuse-server]
//
// 缺省 --input/--reference 时用 release CLI 现造合成样张（gen-kfb +
// bf-ome/bf-classic），保证仓库默认自洽。截图/结果 JSON 落 --out-dir
// （默认 .gate-tmp/bfome-viewer，绝不入库）。
'use strict';
const fs = require('fs');
const path = require('path');
const { spawn, execFileSync } = require('child_process');
const L = require('./lib.js');
const C3 = L.C3;

const PORT = Number(L.arg('port', '8967'));
const OUTDIR = L.arg('out-dir', path.join(L.REPO, '.gate-tmp/bfome-viewer'));
const INPUT = L.arg('input', '');
const REFERENCE = L.arg('reference', '');
const CREDS = L.arg('creds', path.join(OUTDIR, 'creds.json'));
const KEEP = process.argv.includes('--keep');
const REUSE = process.argv.includes('--reuse-server');
const PY = process.env.C4_PY || '.venv/bin/python3';
const TILECMP = path.join(__dirname, 'bfome_tilecmp.py');
const TISSUEPLAN = path.join(__dirname, 'bfome_tissue.py');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/// 阈值按样张性质分档：真实 H&E 样张（显式 --input/--reference）走严格口径
/// （粉/紫主导、通道互换负向、MAD≤8）；合成缺省样张是逐像素彩色噪声
/// （通道均值对称、4:2:0 子采样残差天然 ~24，见 tests/test_bf_ome_platform_chain.py
/// 的容差论证），只保留通道均值正确性与放宽的 MAD 上限。
const SYNTH = !(INPUT && REFERENCE);
const TH = SYNTH
  ? { chanMean: 2.0, mad: 30, lumaMad: 12, perm: false, pink: false }
  : { chanMean: 2.5, mad: 8, lumaMad: 8, perm: true, pink: true };

async function waitFor(fn, timeout, label) {
  const t0 = Date.now();
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() - t0 > timeout) throw new Error(`timeout waiting: ${label}`);
    await sleep(250);
  }
}

function fail(msg) { throw new Error(msg); }

// ------------------------------------------------------------- fixtures --
function prepareDefaultFixtures() {
  const cli = path.join(L.REPO, 'slide-transform-core/target/release/slide-transform');
  if (!fs.existsSync(cli)) {
    fail('未提供 --input 且合成 CLI 不存在（slide-transform-core/target/release/slide-transform）');
  }
  const dir = path.join(OUTDIR, 'synthetic');
  fs.mkdirSync(dir, { recursive: true });
  const kfb = path.join(dir, 'src.kfb');
  const ome = path.join(dir, 'out.ome.tif');
  const cls = path.join(dir, 'ref-classic.tif');
  const run = (args, out) => {
    try {
      execFileSync(cli, args, { cwd: L.REPO, stdio: ['ignore', 'ignore', 'pipe'] });
    } catch (e) {
      fail(`slide-transform ${args[0]} 失败: ${String(e.stderr).slice(-300)}`);
    }
    if (!fs.existsSync(out)) fail(`slide-transform 未产出 ${path.basename(out)}`);
  };
  run(['gen-kfb', kfb, '--width', '3000', '--height', '2000'], kfb);
  run(['convert', kfb, ome, '--profile', 'bf-ome', '--overwrite'], ome);
  run(['convert', kfb, cls, '--profile', 'bf-classic', '--overwrite'], cls);
  return { input: ome, reference: cls };
}

// -------------------------------------------------------------- server --
function startServer(credsPath, inputPath) {
  const server = spawn(
    PY,
    [path.join(__dirname, 'server.py'), '--port', String(PORT),
      '--creds', credsPath, '--seed-bfome', inputPath],
    { cwd: L.REPO, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  let buf = '';
  server.stdout.on('data', (d) => { buf += d; });
  server.stderr.on('data', (d) => { buf += d; });
  server.on('exit', (code) => {
    if (!KEEP && code !== 0 && code !== null) {
      process.stderr.write(`[bfome-server] exited ${code}; tail:\n${buf.slice(-1200)}\n`);
    }
  });
  return {
    server,
    async waitReady() {
      const t0 = Date.now();
      for (;;) {
        if (buf.includes('Running on')) return;
        if (Date.now() - t0 > 180000) {
          fail(`bf-ome 测试服务启动超时；日志尾:\n${buf.slice(-1500)}`);
        }
        await sleep(250);
      }
    },
    kill() { server.kill('SIGTERM'); },
  };
}

// ------------------------------------------------- page-side probes --
const TILE_RE = /\/api\/slides\/([^/]+)\/tiles\/(\d+)\/(\d+)_(\d+)\.jpeg/;

function tileRecorder(page) {
  const st = {
    entries: [],          // {level, col, row, status, contentType, at, url}
    lastAt: 0,
  };
  page.on('response', async (r) => {
    const u = r.url();
    const m = u.match(TILE_RE);
    if (!m) return;
    let ct = '';
    try { ct = r.headers()['content-type'] || ''; } catch (e) { /* 已销毁 */ }
    st.entries.push({
      level: Number(m[2]), col: Number(m[3]), row: Number(m[4]),
      status: r.status(), contentType: ct, at: Date.now(), url: u,
    });
    st.lastAt = Date.now();
  });
  st.byLevel = () => {
    const out = {};
    for (const e of st.entries) {
      if (e.status !== 200 || !/^image\/jpeg/.test(e.contentType)) continue;
      const k = `L${e.level}`;
      out[k] = (out[k] || 0) + 1;
    }
    return out;
  };
  st.okDistinctLevels = () => {
    const s = new Set();
    for (const e of st.entries) {
      if (e.status === 200 && /^image\/jpeg/.test(e.contentType)) s.add(e.level);
    }
    return [...s].sort((a, b) => a - b);
  };
  st.badEntries = () => st.entries.filter(
    (e) => e.status !== 200 || !/^image\/jpeg/.test(e.contentType));
  st.settled = async (quietMs, minTiles) => {
    await sleep(150);
    return st.entries.filter((e) => e.status === 200
        && /^image\/jpeg/.test(e.contentType)).length >= (minTiles || 1)
      && Date.now() - st.lastAt > quietMs;
  };
  return st;
}

async function waitForTileSettle(st, quietMs, timeout, label) {
  await waitFor(() => st.settled(quietMs), timeout, `tile settle: ${label}`);
}

/// 画布取证：全幅均值/方差、组织覆盖、以及「最浓组织窗」统计与锚点。
/// 组织判据 = 不透明（切片区内）且「有彩色」（通道极差 ≥10）：H&E 组织粉/紫，
/// 切片白底与切片边缘的暗背景混色都近灰，不会误判。窗口按积分图取各档尺寸
/// （240/120/80）中密度达标的最大档（低倍下组织稀疏时自动收到小窗聚焦），
/// 窗中心即缩放锚点（画布坐标）。
async function canvasProbe(page) {
  return page.evaluate(() => {
    const cv = document.querySelector('#viewer canvas');
    if (!cv || !cv.width || !cv.height) return null;
    const ctx = cv.getContext('2d');
    let data;
    try { data = ctx.getImageData(0, 0, cv.width, cv.height).data; } catch (e) {
      return { error: String(e) };
    }
    const w = cv.width, h = cv.height, n = w * h;
    const sum = [0, 0, 0], sumsq = [0, 0, 0];
    const mask = new Uint8Array(n);
    let tissue = 0;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        const r = data[i], g = data[i + 1], b = data[i + 2];
        sum[0] += r; sum[1] += g; sum[2] += b;
        sumsq[0] += r * r; sumsq[1] += g * g; sumsq[2] += b * b;
        if (data[i + 3] >= 250
            && Math.max(Math.abs(r - g), Math.abs(g - b), Math.abs(r - b)) >= 10) {
          mask[y * w + x] = 1;
          tissue++;
        }
      }
    }
    const mean = sum.map((v) => v / n);
    const std = sumsq.map((v, c) => Math.sqrt(Math.max(0, v / n - mean[c] * mean[c])));
    const rect = cv.getBoundingClientRect();
    // 积分图（行/列两遍累加）
    const ii = new Float64Array((w + 1) * (h + 1));
    for (let y = 0; y < h; y++) {
      let rowSum = 0;
      for (let x = 0; x < w; x++) {
        rowSum += mask[y * w + x];
        ii[(y + 1) * (w + 1) + (x + 1)] = ii[y * (w + 1) + (x + 1)] + rowSum;
      }
    }
    const rectSum = (x0, y0, S) => {
      const W1 = w + 1;
      return ii[(y0 + S) * W1 + (x0 + S)] - ii[y0 * W1 + (x0 + S)]
        - ii[(y0 + S) * W1 + x0] + ii[y0 * W1 + x0];
    };
    const sizes = [240, 120, 80].filter((S) => S <= Math.min(w, h));
    const cands = [];
    for (const S of sizes) {
      const stride = Math.max(8, Math.floor(S / 5));
      let best = null;
      for (let y0 = 0; y0 + S <= h; y0 += stride) {
        for (let x0 = 0; x0 + S <= w; x0 += stride) {
          const d = rectSum(x0, y0, S) / (S * S);
          if (!best || d > best.d) best = { d, x0, y0, S };
        }
      }
      // 覆盖右/下边缘（步进未对齐时）
      if (best) {
        for (const [x0, y0] of [[w - S, h - S], [w - S, best.y0], [best.x0, h - S]]) {
          if (x0 >= 0 && y0 >= 0) {
            const d = rectSum(x0, y0, S) / (S * S);
            if (d > best.d) best = { d, x0, y0, S };
          }
        }
      }
      cands.push(best);
    }
    let win = null;
    const okCand = cands.filter((c) => c && c.d >= 0.15).pop();
    const chosen = okCand || cands.filter(Boolean).pop() || null;
    if (chosen) {
      const { x0, y0, S } = chosen;
      const csum = [0, 0, 0], csq = [0, 0, 0];
      let cn = 0;
      for (let y = y0; y < y0 + S; y++) {
        for (let x = x0; x < x0 + S; x++) {
          const i = (y * w + x) * 4;
          const r = data[i], g = data[i + 1], b = data[i + 2];
          csum[0] += r; csum[1] += g; csum[2] += b;
          csq[0] += r * r; csq[1] += g * g; csq[2] += b * b;
          cn++;
        }
      }
      const cmean = csum.map((v) => v / cn);
      const cstd = csq.map((v, c) => Math.sqrt(Math.max(0, v / cn - cmean[c] * cmean[c])));
      win = {
        mean: cmean, std: cstd, origin: [x0, y0], size: [S, S],
        tissueFrac: chosen.d,
        anchor: [x0 + S / 2, y0 + S / 2],
      };
    }
    return {
      canvas: { w, h },
      mean, std, tissueFrac: tissue / n,
      tissueWindow: win,
      pageRect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
    };
  });
}

function tissueLookOk(stats, where, errs) {
  if (!stats.tissueWindow) {
    errs.push(`${where}: 画布未检出组织（无彩色组织质心）`);
    return { luma: null, meanRGB: null, stdMax: null, tissueFrac: null };
  }
  const m = stats.tissueWindow.mean;
  const luma = 0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2];
  const maxRB = Math.max(m[0], m[2]);
  if (!(luma < 244)) errs.push(`${where}: 组织窗近空白（luma=${luma.toFixed(1)}）`);
  if (TH.pink && !(maxRB > m[1] + 4)) {
    errs.push(`${where}: 组织窗非粉/紫主导（RGB=${m.map((v) => v.toFixed(1))}）`);
  }
  const stdMax = Math.max(...stats.tissueWindow.std);
  if (!(stdMax > 6)) errs.push(`${where}: 组织窗近乎均匀（std=${stats.tissueWindow.std.map((v) => v.toFixed(1))}）`);
  if (!(stats.tissueWindow.tissueFrac >= 0.10)) {
    errs.push(`${where}: 组织窗彩色像素占比过低（${(stats.tissueWindow.tissueFrac * 100).toFixed(1)}%）`);
  }
  return {
    luma: +luma.toFixed(2), meanRGB: m.map((v) => +v.toFixed(2)),
    stdMax: +stdMax.toFixed(2), tissueFrac: +stats.tissueWindow.tissueFrac.toFixed(3),
  };
}

async function fetchInfo(page, slideId) {
  return page.evaluate(async (id) => {
    const r = await fetch(`/api/slides/${encodeURIComponent(id)}/info`,
      { credentials: 'same-origin' });
    const body = await r.json().catch(() => null);
    return { status: r.status, body };
  }, slideId);
}

function infoEssence(info) {
  const b = info.body || {};
  return {
    image_mode: b.image_mode,
    mpp_x: b.mpp_x, mpp_y: b.mpp_y, mpp_source: b.mpp_source,
    width: b.width, height: b.height,
    channels: (b.channels || []).length,
    max_level: b.deepzoom ? b.deepzoom.max_level : undefined,
    display_image_mode: b.display ? b.display.image_mode : undefined,
  };
}

function checkInfo(ess, errs, prefix) {
  if (ess.image_mode !== 'native_rgb') errs.push(`${prefix}: image_mode=${ess.image_mode}`);
  if (!(ess.channels === 0)) errs.push(`${prefix}: channels=${ess.channels} 非空`);
  if (!(Number(ess.mpp_x) > 0 && Number(ess.mpp_y) > 0)) errs.push(`${prefix}: mpp 缺失 (${ess.mpp_x},${ess.mpp_y})`);
  if (ess.mpp_source !== 'metadata') errs.push(`${prefix}: mpp_source=${ess.mpp_source}`);
  if (!(Number(ess.width) > 0 && Number(ess.height) > 0)) errs.push(`${prefix}: 尺寸缺失`);
  if (!(Number(ess.max_level) >= 1)) errs.push(`${prefix}: deepzoom.max_level=${ess.max_level}`);
}

async function createProjectViaApi(page, name, slideIds) {
  return page.evaluate(async ({ n, ids }) => {
    const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    const r = await fetch('/api/project/create', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' },
      body: JSON.stringify({ name: n, note: '', slide_ids: ids }),
    });
    const b = await r.json();
    if (!r.ok) throw new Error(b.error || r.status);
    return b.pid;
  }, { n: name, ids: slideIds });
}

/// 展开侧栏：只按 #sidebar.collapsed 的**实际当前态**决定是否点 #menu-btn，
/// 竞态安全（isVisible 早读会被 boot 后补的 collapsed class 打败）。
async function ensureSidebarExpanded(page) {
  // attached：collapsed 态是 visibility:hidden，「可见」等待永不满足
  await page.waitForSelector('#sidebar', { state: 'attached', timeout: 15000 });
  for (let i = 0; i < 12; i++) {
    const collapsed = await page.$eval('#sidebar',
      (el) => el.classList.contains('collapsed')).catch(() => null);
    if (collapsed === false) {
      await sleep(250);
      const again = await page.$eval('#sidebar',
        (el) => el.classList.contains('collapsed')).catch(() => null);
      if (again === false) return;
    } else if (collapsed === true) {
      await page.click('#menu-btn').catch(() => {});
      await sleep(400);
    } else {
      await sleep(200);
    }
  }
  fail('侧栏未能展开（#sidebar.collapsed 未解除）');
}

async function openProjectSlide(page, pid, slideId) {
  await page.waitForFunction(() => !!window.HP_UPLOAD, null, { timeout: 30000 });
  await ensureSidebarExpanded(page);
  const sel = `.proj-row[data-pid="${pid}"]`;
  await page.waitForSelector(sel, { timeout: 30000 });
  await page.click(`${sel} .proj-name`);
  await page.waitForSelector(`${sel}.expanded .slide-row`, { timeout: 10000 });
  const rows = await page.$$eval(`${sel} .slide-row`, (els) => els.map((e) => ({
    id: e.dataset.slideId, name: (e.querySelector('.slide-name') || {}).textContent || '',
  })));
  const row = rows.find((r) => r.id === slideId);
  if (!row) fail(`项目行未包含目标切片: ${JSON.stringify(rows.map((r) => ({ id: r.id })))}`);
  if (/读取失败|read failed/i.test(row.name)) fail(`切片行标记读取失败: "${row.name}"`);
  await page.click(`${sel} .slide-row[data-slide-id="${slideId}"] .slide-name`);
}

async function fetchTileB64(page, url) {
  return page.evaluate(async (u) => {
    const r = await fetch(u, { credentials: 'same-origin' });
    if (!r.ok) return null;
    const bytes = new Uint8Array(await r.arrayBuffer());
    let s = '';
    const CH = 0x8000;
    for (let i = 0; i < bytes.length; i += CH) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    }
    return btoa(s);
  }, url);
}

function pickCompareTiles(st, maxLevel) {
  const okTiles = st.entries.filter(
    (e) => e.status === 200 && /^image\/jpeg/.test(e.contentType));
  const byLevel = new Map();
  for (const e of okTiles) {
    if (!byLevel.has(e.level)) byLevel.set(e.level, []);
    byLevel.get(e.level).push(e);
  }
  const levels = [...byLevel.keys()].sort((a, b) => a - b);
  if (!levels.length) return [];
  const low = levels[0];
  const mid = levels.filter((l) => l > low && l < maxLevel).pop() || null;
  const picks = [];
  const take = (lvl, count) => {
    const arr = (byLevel.get(lvl) || []).slice(0, count);
    for (const e of arr) picks.push({ level: e.level, col: e.col, row: e.row, url: e.url });
  };
  take(low, 2);
  if (mid != null) take(mid, 2);
  take(maxLevel, 3);
  return picks;
}

function runTileCompare(manifestPath, outPath, referencePath) {
  return new Promise((resolve) => {
    const p = spawn(PY, [TILECMP, '--classic', referencePath,
      '--manifest', manifestPath, '--out', outPath],
    { cwd: L.REPO, stdio: ['ignore', 'pipe', 'pipe'] });
    let err = '';
    p.stderr.on('data', (d) => { err += d; });
    p.on('close', (code) => resolve({ code, err: err.slice(-800) }));
  });
}

function runTissuePlan(outPath, referencePath) {
  return new Promise((resolve, reject) => {
    const p = spawn(PY, [TISSUEPLAN, '--classic', referencePath,
      '--out', outPath],
    { cwd: L.REPO, stdio: ['ignore', 'pipe', 'pipe'] });
    let err = '';
    p.stderr.on('data', (d) => { err += d; });
    p.on('close', (code) => {
      if (code === 0) return resolve(JSON.parse(fs.readFileSync(outPath, 'utf8')));
      reject(new Error(`tissue plan 失败: ${err.slice(-400)}`));
    });
  });
}

// ---------------------------------------------------------------- main --
async function main() {
  fs.mkdirSync(OUTDIR, { recursive: true });
  fs.mkdirSync(path.join(OUTDIR, 'tiles'), { recursive: true });
  const fx = INPUT && REFERENCE
    ? { input: INPUT, reference: REFERENCE }
    : prepareDefaultFixtures();
  if (!fs.existsSync(fx.input)) fail(`--input 不存在`);
  if (!fs.existsSync(fx.reference)) fail(`--reference 不存在`);

  let srv = null;
  if (!REUSE) {
    srv = startServer(CREDS, fx.input);
    await srv.waitReady();
  }
  const creds = L.readCreds(CREDS);
  const slideId = creds.bfomeSlideId;
  if (!slideId) fail('creds 缺 bfomeSlideId（server.py --seed-bfome 未生效）');

  // 组织瓦片取样计划：坐标几何只来自 classic 参照（openslide DeepZoom）
  const plan = await runTissuePlan(path.join(OUTDIR, 'tissue-plan.json'), fx.reference);

  const result = {
    when: new Date().toISOString(),
    syntheticDefault: !(INPUT && REFERENCE),
    inputBytes: fs.statSync(fx.input).size,
    phases: {}, errs: [],
  };
  let exitCode = 0;

  const { context, page } = await C3.launch('bfome-viewer');
  try {
    // ---- 1) 登录 + 项目 + 打开 ----
    await L.login(page, PORT, creds, 'user', '/app');
    if (!page.url().includes('/app')) fail(`login landed at ${page.url()}`);
    const pid = await createProjectViaApi(page, 'bf-ome 查看器回归项目', [slideId]);
    result.phases.project = { created: true };
    await page.reload({ waitUntil: 'load' });

    const st = tileRecorder(page);
    await openProjectSlide(page, pid, slideId);
    await waitForTileSettle(st, 1200, 90000, 'open(home)');
    const homeLevels = st.okDistinctLevels();
    result.phases.openHome = { levels: homeLevels, counts: st.byLevel() };

    // ---- 2) info / UI 断言 ----
    const info = await fetchInfo(page, slideId);
    if (info.status !== 200) fail(`info status ${info.status}`);
    const ess = infoEssence(info);
    checkInfo(ess, result.errs, 'info');
    result.info = ess;
    const maxLevel = Number(ess.max_level);

    const ui = await page.evaluate(() => ({
      channelBtnHidden: document.getElementById('channel-btn').hidden,
      channelPanelHidden: document.getElementById('channel-panel').hidden,
      rgbBadgeHidden: document.getElementById('rgb-badge').hidden,
      roiNoScaleHintHidden: document.getElementById('roi-no-scale-hint').hidden,
      mppSetterDisplay: document.getElementById('mpp-setter').style.display,
      zoomBadge: (document.getElementById('zoom-badge').textContent || '').trim(),
    }));
    if (!ui.channelBtnHidden) result.errs.push('UI: 通道入口按钮出现');
    if (!ui.channelPanelHidden) result.errs.push('UI: 通道面板出现');
    if (!ui.roiNoScaleHintHidden) result.errs.push('UI: 无标尺提示出现（mpp 应可用）');
    if (ui.mppSetterDisplay === 'flex') result.errs.push('UI: mpp 手动设置区显示（应隐藏）');
    result.ui = ui;

    // ---- 3) 低倍画布取证 + 截图 ----
    await sleep(600);
    const homeStats = await canvasProbe(page);
    if (!homeStats || homeStats.error) fail(`低倍画布读取失败: ${JSON.stringify(homeStats)}`);
    result.phases.openHome.canvas = {
      meanRGB: homeStats.mean.map((v) => +v.toFixed(2)),
      std: homeStats.std.map((v) => +v.toFixed(2)),
      tissueFrac: +homeStats.tissueFrac.toFixed(4),
    };
    result.phases.openHome.tissueLook
      = tissueLookOk(homeStats, '低倍', result.errs);
    await page.screenshot({ path: path.join(OUTDIR, '01-lowzoom.png') });

    // ---- 4) 滚轮放大（组织锚点上，中间层级；点下内容缩放不漂移）→ 1:1 ----
    const anchor = homeStats.tissueWindow && homeStats.tissueWindow.anchor;
    if (!anchor) fail('低倍画布未找到组织锚点（疑似空白）');
    const rect = homeStats.pageRect;
    const px = rect.x + (anchor[0] / homeStats.canvas.w) * rect.w;
    const py = rect.y + (anchor[1] / homeStats.canvas.h) * rect.h;
    await page.mouse.move(px, py);
    for (let round = 0; round < 2; round++) {
      for (let i = 0; i < 7; i++) {
        await page.mouse.wheel(0, -120);
        await sleep(260);
      }
      await waitForTileSettle(st, 1000, 60000, `wheel zoom round ${round}`);
      await sleep(300);
    }
    const midLevels = st.okDistinctLevels();
    result.phases.wheelZoom = { levels: midLevels, counts: st.byLevel() };
    const badgeAfterWheel = await page.$eval('#zoom-badge', (el) => el.textContent.trim());
    result.phases.wheelZoom.zoomBadge = badgeAfterWheel;

    await page.click('#zoom-native');
    await waitFor(
      () => st.okDistinctLevels().includes(maxLevel), 90000, 'max-level tiles');
    await waitForTileSettle(st, 1200, 90000, 'native zoom');
    const allLevels = st.okDistinctLevels();
    result.phases.nativeZoom = { levels: allLevels, counts: st.byLevel() };
    if (!(allLevels.length >= 3)) result.errs.push(`层级覆盖不足: ${allLevels}`);
    if (!allLevels.includes(maxLevel)) result.errs.push(`未覆盖最高分辨率层 ${maxLevel}`);
    const badgeNative = await page.$eval('#zoom-badge', (el) => el.textContent.trim());
    result.phases.nativeZoom.zoomBadge = badgeNative;
    // 倍率须由 mpp 推导（含「数字放大」后缀均为倍率；百分比 = mpp 缺失）
    if (!/×/.test(badgeNative)) result.errs.push(`倍率徽标异常: "${badgeNative}"`);
    if (/%/.test(badgeNative)) result.errs.push(`倍率徽标显示百分比（mpp 未生效）: "${badgeNative}"`);

    // ---- 5) 全分辨率画布取证 + 截图 ----
    await sleep(600);
    const nativeStats = await canvasProbe(page);
    if (!nativeStats || nativeStats.error) fail(`全分辨率画布读取失败: ${JSON.stringify(nativeStats)}`);
    result.phases.nativeZoom.canvas = {
      meanRGB: nativeStats.mean.map((v) => +v.toFixed(2)),
      std: nativeStats.std.map((v) => +v.toFixed(2)),
      tissueFrac: +nativeStats.tissueFrac.toFixed(4),
    };
    result.phases.nativeZoom.tissueLook
      = tissueLookOk(nativeStats, '全分辨率', result.errs);
    await page.screenshot({ path: path.join(OUTDIR, '02-fullres.png') });

    const bad = st.badEntries();
    if (bad.length) {
      result.errs.push(`非 200/非 JPEG 瓦片 ${bad.length} 个（首个 level=${bad[0].level} status=${bad[0].status} ct=${bad[0].contentType}）`);
    }
    result.tileTotals = {
      responses: st.entries.length,
      ok200: st.entries.length - bad.length,
      distinctLevels: allLevels,
      perLevel: st.byLevel(),
    };

    // ---- 6) 瓦片色彩对照（classic 参照，Python 助手） ----
    // 取样两路：OSD 实际拉过的瓦片 + 组织计划瓦片（classic 几何定位的组织点，
    // 保证比对瓦片有色彩判别力）。URL 形态与查看器请求完全一致。
    const picks = pickCompareTiles(st, maxLevel);
    for (const lv of plan.levels) {
      for (const [col, row] of lv.tiles) {
        picks.push({
          level: lv.level, col, row,
          url: `/api/slides/${encodeURIComponent(slideId)}/tiles/${lv.level}/${col}_${row}.jpeg`,
        });
      }
    }
    if (!picks.length || !picks.some((p) => p.level === maxLevel)) {
      result.errs.push('对照取样失败：未取到含最高层的瓦片');
    } else {
      const manifest = [];
      const seen = new Set();
      for (const p of picks) {
        const key = `${p.level}/${p.col}_${p.row}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const b64 = await fetchTileB64(page, p.url);
        if (!b64) { result.errs.push(`瓦片重取失败 L${p.level}/${p.col}_${p.row}`); continue; }
        const file = `l${p.level}_${p.col}_${p.row}.jpg`;
        fs.writeFileSync(path.join(OUTDIR, 'tiles', file), Buffer.from(b64, 'base64'));
        manifest.push({ file: path.join(OUTDIR, 'tiles', file), level: p.level, col: p.col, row: p.row });
      }
      const manifestPath = path.join(OUTDIR, 'tile-manifest.json');
      fs.writeFileSync(manifestPath, JSON.stringify({
        tiles: manifest,
        tolerances: TH,
      }));
      const cmpOut = path.join(OUTDIR, 'tile-compare.json');
      const cmp = await runTileCompare(manifestPath, cmpOut, fx.reference);
      if (!fs.existsSync(cmpOut)) {
        result.errs.push(`参照比对助手未产出（exit ${cmp.code}）: ${cmp.err}`);
      } else {
        const cmpJson = JSON.parse(fs.readFileSync(cmpOut, 'utf8'));
        result.tileCompare = cmpJson;
        if (!cmpJson.ok) result.errs.push(`瓦片与 classic 参照不符: ${JSON.stringify(cmpJson.failures).slice(0, 400)}`);
      }
    }

    // ---- 7) 重开 A：整页 reload ----
    st.entries.length = 0; st.lastAt = Date.now();
    await page.reload({ waitUntil: 'load' });
    await openProjectSlide(page, pid, slideId);
    await waitForTileSettle(st, 1200, 90000, 'reopen(reload)');
    const info2 = await fetchInfo(page, slideId);
    const ess2 = infoEssence(info2);
    checkInfo(ess2, result.errs, 'reopen');
    if (JSON.stringify(ess2) !== JSON.stringify(ess)) {
      result.errs.push(`重开后 info 不一致: ${JSON.stringify(ess2)}`);
    }
    if (!st.okDistinctLevels().length) result.errs.push('重开后无 200 瓦片');
    result.phases.reopenReload = {
      levels: st.okDistinctLevels(), counts: st.byLevel(),
      bad: st.badEntries().length,
    };
    await page.screenshot({ path: path.join(OUTDIR, '03-reopen.png') });
  } catch (e) {
    result.errs.push(String(e).slice(0, 600));
    try { await page.screenshot({ path: path.join(OUTDIR, '99-failure.png') }); } catch (e2) { /* 忽略 */ }
  } finally {
    await context.close().catch(() => {});
  }

  // ---- 8) 重开 B：全新浏览器上下文 + 重新登录 ----
  try {
    const second = await C3.launch('bfome-reopen');
    const st2 = tileRecorder(second.page);
    try {
      await L.login(second.page, PORT, creds, 'user', '/app');
      const pid = await createProjectViaApi(second.page, 'bf-ome 重开校验', [slideId])
        .catch(async () => null);
      // 项目已存在时用列表里既有项目；否则用新建的
      let target = pid;
      if (!target) {
        target = await second.page.evaluate(async (id) => {
          const r = await fetch('/api/projects', { credentials: 'same-origin' });
          const b = await r.json();
          const list = Array.isArray(b) ? b : (b.projects || []);
          const hit = list.find((p) => (p.slide_refs || []).some(
            (s) => s.slide_id === id));
          return hit ? hit.pid : null;
        }, slideId);
      }
      if (!target) fail('重开上下文未找到项目');
      // 侧栏项目列表在页面 boot 时拉取（建项目动作之后才入库）→ reload 再开
      await second.page.reload({ waitUntil: 'load' });
      await openProjectSlide(second.page, target, slideId);
      await waitForTileSettle(st2, 1200, 90000, 'reopen(new context)');
      const info3 = await fetchInfo(second.page, slideId);
      const ess3 = infoEssence(info3);
      if (JSON.stringify(ess3) !== JSON.stringify(result.info)) {
        result.errs.push(`新上下文 info 不一致: ${JSON.stringify(ess3)}`);
      }
      result.phases.reopenNewContext = {
        levels: st2.okDistinctLevels(), counts: st2.byLevel(),
        bad: st2.badEntries().length,
      };
      if (!st2.okDistinctLevels().length) result.errs.push('新上下文无 200 瓦片');
    } finally {
      await second.context.close().catch(() => {});
    }
  } catch (e) {
    result.errs.push(`新上下文重开失败: ${String(e).slice(0, 400)}`);
  }

  if (srv && !KEEP && !REUSE) srv.kill();

  result.pass = result.errs.length === 0;
  const outPath = path.join(OUTDIR, 'result.json');
  fs.writeFileSync(outPath, JSON.stringify(result, null, 2));
  console.log(`levels: openHome=${JSON.stringify(result.phases.openHome && result.phases.openHome.levels)}`
    + ` wheel=${JSON.stringify(result.phases.wheelZoom && result.phases.wheelZoom.levels)}`
    + ` native=${JSON.stringify(result.phases.nativeZoom && result.phases.nativeZoom.levels)}`);
  if (result.tileCompare) {
    console.log(`tileCompare: ok=${result.tileCompare.ok}`
      + ` worstChanMeanDiff=${result.tileCompare.worstChanMeanDiff}`
      + ` worstMad=${result.tileCompare.worstMad}`);
  }
  console.log(`errors: ${result.errs.length}`);
  for (const e of result.errs) console.log(`  - ${e}`);
  console.log(`${result.pass ? 'PASS' : 'FAIL'} -> ${outPath}`);
  if (!result.pass) exitCode = 1;
  process.exit(exitCode);
}

main().catch((e) => { console.error(e); process.exit(1); });
