#!/usr/bin/env node
// C3 工具页 e2e（真实 Flask app /tools/slides；Chromium headless，1.62.1）。
// 场景 a–m 对应任务书；证据 JSON 落 .gate-tmp/slide-tools-c3/e2e/。
// 真实系统保存选择器、persist() 真手势、Firefox/Safari/Edge 为外部门禁：
// showSaveFilePicker 在此以 OPFS 替身验证导出字节路径。
'use strict';
const path = require('path');
const fs = require('fs');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8943'));
const ONLY = L.arg('only', null); // e.g. --only a

const results = {};
let server = null;

function record(id, pass, detail) {
  results[id] = { pass, ...detail };
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${JSON.stringify(detail).slice(0, 300)}`);
  if (!pass) process.exitCode = 1;
}

async function waitVisible(page, sel, timeout = 60000) {
  await page.waitForSelector(sel, { state: 'visible', timeout });
}

async function waitText(page, sel, re, timeout = 60000) {
  await page.waitForFunction(([s, r]) => {
    const el = document.querySelector(s);
    return el && !el.hidden && r.test(el.textContent);
  }, [sel, re], { timeout });
}

/// 主流程：选文件 → probe 摘要 → 转换 → ready。返回 {saved?} 由调用方扩展。
async function runConvertFlow(page, file, { profile = null } = {}) {
  await L.setFile(page, file);
  await waitVisible(page, '#probe-section:not([hidden])');
  await waitText(page, '#estimate-total', /[0-9]/);
  if (profile) await page.check(`#profile-${profile}`);
  await page.click('#convert-btn');
  await waitVisible(page, '#result-section:not([hidden])');
}

/// Node 侧 HEAD：取真实 Flask 响应头（页面 CSP、wasm MIME）。
function httpHead(port, p) {
  const http = require('http');
  const get = (pathName) => new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: pathName, method: 'HEAD' }, (res) => {
      resolve({ status: res.statusCode, headers: res.headers });
      res.resume();
    });
    req.on('error', reject);
    req.end();
  });
  return (async () => {
    const r1 = await get('/tools/slides');
    const r2 = await get('/static/tools/slide-transform/slide_transform_bg.wasm');
    return {
      csp: r1.headers['content-security-policy'],
      wasmType: r2.headers['content-type'],
      wasmStatus: r2.status,
    };
  })();
}

async function shaOfResult(page) {
  const txt = await page.textContent('#result-sha');
  return String(txt || '').trim();
}

// ---------------------------------------------------------------- 场景 --

// (a) 明场小夹具 happy path + (j) 网络捕获 + wasm/worker CSP 加载证明。
async function scenarioA() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native.tif'));
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('a', [L.savePickerStub(), L.downloadGuard()]);
  const requests = [];
  const consoleErrors = [];
  context.on('request', (r) => requests.push({ url: r.url(), method: r.method() }));
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  try {
    await L.openTools(page, PORT);
    await L.shot(page, 'empty');

    // 页面级 CSP + wasm MIME（真实 Flask 响应头；Node 侧取，避免污染 (j) 捕获）
    const headers = await httpHead(PORT);

    // worker/wasm 在该 CSP 下真实加载（runner init 已实例化 wasm，转换再证）
    await runConvertFlow(page, kfb);
    await L.shot(page, 'probe-summary');

    const notSavedWarn = await page.textContent('#result-section .warn-panel');
    if (!/OPFS|还不是|NOT saved/i.test(notSavedWarn)) throw new Error('“尚未保存到磁盘”警示缺失');
    const saveSupported = await page.evaluate(() => typeof window.showSaveFilePicker === 'function');
    if (!saveSupported) throw new Error('picker stub missing');
    // finished-state presentation (C3 review): no stale controls or rows
    const ui = await page.evaluate(() => {
      const vis = (el) => !!el && getComputedStyle(el).display !== 'none' && el.offsetParent !== null;
      const rows = [...document.querySelectorAll('.job-row')];
      return {
        cancelVisible: vis(document.getElementById('cancel-btn')),
        stagePct: document.getElementById('stage-progress').getAttribute('aria-valuenow'),
        rows: rows.map((r) => ({ next: r.dataset.nextAction, state: r.querySelector('.job-state').textContent })),
      };
    });
    if (ui.cancelVisible) throw new Error('cancel button still visible after ready');
    if (ui.stagePct !== '100') throw new Error(`copy progress ${ui.stagePct}% after copy finished`);
    if (ui.rows.length !== 1 || ui.rows[0].next !== 'export') {
      throw new Error(`job list stale after ready: ${JSON.stringify(ui.rows)}`);
    }
    await L.shot(page, 'ready-not-saved');
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const saved = await L.opfsSha256(page);
    if (saved.sha256 !== nativeSha) throw new Error(`saved sha ${saved.sha256} != native ${nativeSha}`);
    const resultSha = await shaOfResult(page);
    if (resultSha !== nativeSha) throw new Error(`result sha ${resultSha} != native`);

    // persist()：手势内请求，如实回报（granted 或 denied 都接受，只要有明确文案）
    await page.click('#persist-btn');
    await waitText(page, '#save-status', /持久|persistent/i);
    const persistMsg = await page.textContent('#save-status');

    // (j) 网络捕获断言
    const origin = `http://127.0.0.1:${PORT}`;
    const bad = requests.filter((r) => !r.url.startsWith(origin));
    const nonGet = requests.filter((r) => r.method !== 'GET');
    const apiCalls = requests.filter((r) => r.url.includes('/api/'));
    const nameLeak = requests.filter((r) => r.url.includes(path.basename(kfb)));
    const shaLeak = requests.filter((r) => r.url.toLowerCase().includes(nativeSha.slice(0, 16)));
    const nonStatic = requests.filter((r) => {
      const u = new URL(r.url);
      return u.pathname !== '/tools/slides' && !u.pathname.startsWith('/static/');
    });
    const downloads = await page.evaluate(() => window.__downloads);
    const cspErrors = consoleErrors.filter((e) => /Refused|Content Security Policy|CSP/i.test(e));

    record("a-bf-happy-path", true, { savedSha256: saved.sha256, nativeSha256: nativeSha, savedBytes: saved.size, notSavedWarnShown: /OPFS|还不是|NOT saved/i.test(notSavedWarn), persistMsg: persistMsg.trim() });
    record('j-network-capture', bad.length === 0 && nonGet.length === 0 && apiCalls.length === 0
      && nameLeak.length === 0 && shaLeak.length === 0 && nonStatic.length === 0 && (!downloads || downloads.length === 0),
      { totalRequests: requests.length, nonSameOrigin: bad.length, nonGet: nonGet.map((r) => r.method),
        apiCalls: apiCalls.length, nameLeak: nameLeak.length, shaLeak: shaLeak.length,
        nonPageStatic: nonStatic.map((r) => new URL(r.url).pathname), downloads: downloads || [],
        requestPaths: [...new Set(requests.map((r) => new URL(r.url).pathname))] });
    record('csp-real-app', headers.wasmType === 'application/wasm' && cspErrors.length === 0,
      { csp: headers.csp, wasmType: headers.wasmType, wasmStatus: headers.wasmStatus, cspViolations: cspErrors });
  } catch (e) {
    record('a-bf-happy-path', false, { error: String(e).slice(0, 400) });
    record('j-network-capture', false, { error: 'aborted by (a) failure' });
    record('csp-real-app', false, { error: 'aborted by (a) failure' });
  } finally {
    await context.close();
  }
}

function channelFixture() {
  const cj = path.join(L.GATE, 'fixtures', 'channel.json');
  fs.mkdirSync(path.dirname(cj), { recursive: true });
  fs.writeFileSync(cj, JSON.stringify([
    { channelName: 'DAPI', channelIndex: 1, channelColor: '#0000E5', lower: 13, upper: 227, gamma: 1, show: true },
    { channelName: '520', channelIndex: 2, channelColor: '#00FF00', lower: 16, upper: 175, gamma: 1, show: true },
  ]));
  return cj;
}

/// channel.json 只进结果报告（显示窗口），不改 TIFF 像素——输出哈希比对发现不了
/// 它的丢失，必须直接断言结果面板里的显示窗口。
async function assertDisplayWindows(page) {
  const c0 = (await page.textContent('#result-channel-0')) || '';
  const c1 = (await page.textContent('#result-channel-1')) || '';
  if (!/13\s*–\s*227/.test(c0) || !/16\s*–\s*175/.test(c1)) {
    throw new Error(`display windows missing: ch0="${c0}" ch1="${c1}"`);
  }
  return { ch0: c0.trim(), ch1: c1.trim() };
}

// (b) KFBF 荧光夹具 + 可选 channel.json 伴随输入（≤1 MiB 有界读）。
// 伴随文件在选源文件之后才选：走「准备后改选 → 写回任务记录」路径。
async function scenarioB() {
  const kfbf = L.ensureFixture('fl-600x400.kfbf', ['gen-kfbf']);
  const cj = channelFixture();
  const native = L.nativeConvert(kfbf, path.join(L.GATE, 'fixtures', 'fl-native.tif'), ['--channel-json', cj]);
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('b', [L.savePickerStub()]);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfbf);
    await page.setInputFiles('#channel-input', cj);
    await waitVisible(page, '#probe-section:not([hidden])');
    const channelsText = await page.textContent('#probe-grid');
    if (!/DAPI/.test(channelsText)) throw new Error('probe summary missing channel names');
    await page.click('#convert-btn');
    await waitVisible(page, '#result-section:not([hidden])');
    const windows = await assertDisplayWindows(page);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const saved = await L.opfsSha256(page);
    if (saved.sha256 !== nativeSha) throw new Error(`saved sha ${saved.sha256} != native ${nativeSha}`);
    record('b-kfbf-happy-path', true, { savedSha256: saved.sha256, nativeSha256: nativeSha,
      savedBytes: saved.size, displayWindows: windows });
  } catch (e) {
    record('b-kfbf-happy-path', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (c) 转换中刷新 → 任务列表 resume → 完成；sha == 原生。含 copying/converting 截图。
async function scenarioC() {
  const kfb = L.ensureFixture('bf-2g.kfb', ['gen-kfb', '--width', '36500', '--height', '36500']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-2g-native.tif'));
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('c', [L.savePickerStub()]);
  const dialogs = [];
  page.on('dialog', (d) => { dialogs.push(d.type()); return d.accept(); });
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    // copying 截图：复制进行中（2 GiB，数秒窗口）
    await page.waitForFunction(() => {
      const p = document.getElementById('stage-progress');
      return p && Number(p.getAttribute('aria-valuenow') || 0) > 3;
    }, null, { timeout: 120000 });
    await L.shot(page, 'copying');
    await waitVisible(page, '#probe-section:not([hidden])');
    await page.click('#convert-btn');
    // converting 截图 + 等到有已提交字节后刷新（模拟用户中途关页）
    await page.waitForFunction(() => {
      const el = document.getElementById('run-bytes');
      return el && !el.hidden && /\d/.test(el.textContent);
    }, null, { timeout: 180000 });
    await L.shot(page, 'converting');
    await page.waitForTimeout(700);
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });

    // 刷新后任务列表：resume 入口
    const resumeBtn = '.job-row[data-next-action="resume"] button[data-action="resume"]';
    await waitVisible(page, resumeBtn);
    await L.shot(page, 'job-list-resume');
    await page.click(resumeBtn);
    await waitVisible(page, '#result-section:not([hidden])', 300000);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/, 120000);
    const saved = await L.opfsSha256(page);
    if (saved.sha256 !== nativeSha) throw new Error(`resume sha ${saved.sha256} != native ${nativeSha}`);
    record('c-refresh-resume', true, { savedSha256: saved.sha256, nativeSha256: nativeSha,
      beforeunloadSeen: dialogs.includes('beforeunload') });
  } catch (e) {
    record('c-refresh-resume', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (d) uncertain 磁盘确认（stub estimate 上限 10 GiB）+ 硬不足拒绝。
async function scenarioD() {
  const big = L.sparseLargeKfb('bf-sparse-5g2.kfb', Math.ceil(5.2 * 2 ** 30));
  const mid = L.ensureFixture('bf-2g.kfb', ['gen-kfb', '--width', '36500', '--height', '36500']);

  // d1: stub 报告上限（available = 10 GiB 恰为 cap）→ uncertain 对话框
  const stubCap = `(() => {
    const GiB = 2 ** 30;
    Object.defineProperty(navigator.storage, 'estimate', { value: async () => ({ usage: 0, quota: 10 * GiB }) });
  })();`;
  {
    const { context, page } = await L.launch('d1', [stubCap]);
    page.on('dialog', (d) => d.accept()); // beforeunload（复制中 reload）
    try {
      await L.openTools(page, PORT);
      await page.focus('#file-input'); // 对话框焦点还原断言的锚点
      await L.setFile(page, big);
      await waitVisible(page, '#disk-dialog[open]', 30000);
      const bodyText = await page.textContent('#disk-dialog-body');
      if (!/GiB/.test(bodyText)) throw new Error('dialog missing numbers');
      // 焦点在对话框内
      const focusIn = await page.evaluate(() => !!document.querySelector('#disk-dialog').contains(document.activeElement));
      await L.shot(page, 'uncertain-dialog');
      // Esc = 取消 → 无任务目录
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !document.getElementById('disk-dialog').open, null, { timeout: 10000 });
      const focusRestored = await page.evaluate(() => document.activeElement === document.getElementById('file-input'));
      const dirs = await L.jobDirs(page);
      if (dirs.length !== 0) throw new Error(`cancel left job dirs: ${dirs}`);
      // 确认路径：重选 → 对话框 → 仍要继续 → 开始复制
      await L.setFile(page, big);
      await waitVisible(page, '#disk-dialog[open]', 30000);
      await page.click('#disk-confirm-btn');
      await page.waitForFunction(() => {
        const p = document.getElementById('stage-progress');
        return p && Number(p.getAttribute('aria-valuenow') || 0) > 0;
      }, null, { timeout: 120000 });
      const dirsDuringCopy = await L.jobDirs(page);
      const proceeded = dirsDuringCopy.length === 1;
      // 复制中断页（beforeunload 接受）→ 下次进入启动清扫删除 staging 目录；
      // 垂死 worker 的句柄可能短暂锁住目录 → 最多再进一次让 pending-cleanup 重试
      let dirsAfterSweep = [];
      for (let round = 0; round < 3; round++) {
        await page.reload({ waitUntil: 'load' });
        await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
        await page.waitForTimeout(round === 0 ? 1500 : 4000);
        dirsAfterSweep = await L.jobDirs(page);
        if (dirsAfterSweep.length === 0) break;
      }
      record('d1-uncertain-disk', proceeded && dirsAfterSweep.length === 0,
        { dialogNumbers: /GiB/.test(bodyText), focusInDialog: focusIn, focusRestored,
          cancelLeftNoDir: dirs.length === 0, confirmProceeded: proceeded,
          dirsAfterReloadSweep: dirsAfterSweep });
    } catch (e) {
      record('d1-uncertain-disk', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // d2: 真 estimate（fresh profile，本机空闲 > 10 GiB → 报告封顶）→ 同样 uncertain
  {
    const { context, page } = await L.launch('d2', []);
    try {
      await L.openTools(page, PORT);
      await L.setFile(page, big);
      await waitVisible(page, '#disk-dialog[open]', 30000);
      record('d2-uncertain-real-quota', true, { note: 'fresh profile, real estimate hits reporting cap' });
      await page.keyboard.press('Escape');
    } catch (e) {
      record('d2-uncertain-real-quota', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // d3: 硬不足（available 1 GiB < 需求，非 uncertain）→ 明确停止 + 数字，无对话框
  const stubHard = `(() => {
    const GiB = 2 ** 30;
    Object.defineProperty(navigator.storage, 'estimate', { value: async () => ({ usage: 0, quota: 1 * GiB }) });
  })();`;
  {
    const { context, page } = await L.launch('d3', [stubHard]);
    try {
      await L.openTools(page, PORT);
      await L.setFile(page, mid);
      await waitText(page, '#page-error', /空间不足|Not enough/, 30000);
      const errText = await page.textContent('#page-error');
      const dialogOpen = await page.evaluate(() => document.getElementById('disk-dialog').open);
      const dirs = await L.jobDirs(page);
      record('d3-hard-shortage', !dialogOpen && dirs.length === 0 && /GiB/.test(errText),
        { errText: errText.trim().slice(0, 200), dialogOpen, jobDirs: dirs.length });
    } catch (e) {
      record('d3-hard-shortage', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }
}

// (e) 取消反馈 ≤250ms + 任务目录删除。
async function scenarioE() {
  const kfb = L.ensureFixture('bf-2g.kfb', ['gen-kfb', '--width', '36500', '--height', '36500']);
  const { context, page } = await L.launch('e', []);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#probe-section:not([hidden])', 300000);
    await page.click('#convert-btn');
    await waitVisible(page, '#cancel-btn:not([hidden])', 300000);
    // 点击 → 同步反馈 + 下一渲染帧均 ≤ 250ms
    const latency = await page.evaluate(() => new Promise((resolve) => {
      const status = document.getElementById('run-status');
      const t0 = performance.now();
      document.getElementById('cancel-btn').click();
      const syncMs = performance.now() - t0;
      requestAnimationFrame(() => resolve({ syncMs, paintedMs: performance.now() - t0, status: status.textContent }));
    }));
    // 目录清理（终止 worker 后删除，可能重试）
    const deadline = Date.now() + 60000;
    let dirs = [];
    while (Date.now() < deadline) {
      dirs = await L.jobDirs(page);
      if (dirs.length === 0) break;
      await page.waitForTimeout(500);
    }
    record('e-cancel-250ms', latency.syncMs <= 250 && latency.paintedMs <= 250 && dirs.length === 0,
      { syncMs: latency.syncMs, paintedMs: latency.paintedMs, statusAfter: latency.status, jobDirsAfter: dirs });
  } catch (e) {
    record('e-cancel-250ms', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (f) 列表删除 + prepared 任务跨刷新「开始」。
async function scenarioF() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native.tif'));
  const nativeSha = await L.sha256File(native);
  const { context, page } = await L.launch('f', [L.savePickerStub()]);
  page.on('dialog', (d) => d.accept());
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '.job-row[data-next-action="start"]', 60000);
    // 刷新 → prepared 任务在列表 → 开始（无需重选文件）
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    await waitVisible(page, '.job-row[data-next-action="start"] button[data-action="start"]');
    await page.click('.job-row[data-next-action="start"] button[data-action="start"]');
    await waitVisible(page, '#result-section:not([hidden])', 120000);
    const startWorked = (await shaOfResult(page)) === nativeSha;
    // 删除任务（含确认）
    await page.click('.job-row [data-job-discard]');
    await page.waitForFunction(() => document.querySelectorAll('.job-row').length === 0, null, { timeout: 30000 });
    const dirs = await L.jobDirs(page);
    record('f-discard-and-list-start', startWorked && dirs.length === 0,
      { startFromListShaOk: startWorked, jobDirsAfterDiscard: dirs });
  } catch (e) {
    record('f-discard-and-list-start', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (g) 严格无损被核心拒绝（pixel_policy_violation）且 UI 明示。
async function scenarioG() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const { context, page } = await L.launch('g', []);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#probe-section:not([hidden])');
    const warnVisible = await page.evaluate(() => !document.getElementById('policy-strict-warn').hidden);
    await page.check('#policy-strict');
    await page.click('#convert-btn');
    await waitText(page, '#page-error', /pixel_policy_violation/, 120000);
    const errText = await page.textContent('#page-error');
    const jobState = await page.textContent('.job-row .job-state');
    record('g-strict-lossless-refusal', warnVisible && /pixel_policy_violation/.test(errText),
      { preWarningShown: warnVisible, errText: errText.trim().slice(0, 200), jobStateAfter: jobState });
  } catch (e) {
    record('g-strict-lossless-refusal', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (h) 不支持的文件在复制前被拒（无任务目录）。
async function scenarioH() {
  const bad = path.join(L.GATE, 'fixtures', 'not-a-slide.txt');
  fs.mkdirSync(path.dirname(bad), { recursive: true });
  fs.writeFileSync(bad, 'this is definitely not a KFB or KFBF file\n'.repeat(64));
  const { context, page } = await L.launch('h', []);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, bad);
    await waitText(page, '#page-error', /unsupported_input|不支持|not a supported/i, 30000);
    const dirs = await L.jobDirs(page);
    record('h-unsupported-input', dirs.length === 0, { jobDirs: dirs });
  } catch (e) {
    record('h-unsupported-input', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (i) showSaveFilePicker 不可用 → 保存禁用 + 说明，绝不整文件下载。
async function scenarioI() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const noPicker = `Object.defineProperty(window, 'showSaveFilePicker', {
    configurable: true, get() { return undefined; } });`;
  const { context, page } = await L.launch('i', [noPicker, L.downloadGuard()]);
  try {
    await L.openTools(page, PORT);
    await runConvertFlow(page, kfb);
    const saveDisabled = await page.evaluate(() => document.getElementById('save-btn').disabled);
    const msg = await page.textContent('#save-status');
    const downloads = await page.evaluate(() => window.__downloads || []);
    const anchorDownloads = await page.evaluate(() =>
      document.querySelectorAll('a[download], a[href^="blob:"]').length);
    record('i-save-unavailable', saveDisabled && downloads.length === 0 && anchorDownloads === 0,
      { saveDisabled, message: msg.trim().slice(0, 160), downloads, anchorDownloads });
  } catch (e) {
    record('i-save-unavailable', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (k) 离线：页面与 worker 已加载后 setOffline(true)，完整转换 + 保存成功。
async function scenarioK() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native.tif'));
  const nativeSha = await L.sha256File(native);
  const { context, page } = await L.launch('k', [L.savePickerStub()]);
  const offlineReqs = [];
  context.on('request', (r) => offlineReqs.push(r.url()));
  context.on('requestfailed', (r) => offlineReqs.push(`FAILED ${r.url()}`));
  try {
    await L.openTools(page, PORT); // 页面 + worker + wasm 已加载
    await context.setOffline(true);
    offlineReqs.length = 0; // 只统计离线后的请求
    await runConvertFlow(page, kfb);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const saved = await L.opfsSha256(page);
    const anyNetwork = offlineReqs.length > 0;
    record('k-offline-full-flow', saved.sha256 === nativeSha && !anyNetwork,
      { savedSha256: saved.sha256, nativeSha256: nativeSha, networkAttemptsWhileOffline: offlineReqs });
  } catch (e) {
    record('k-offline-full-flow', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.setOffline(false).catch(() => {});
    await context.close();
  }
}

// (l) 中英切换覆盖（无未翻译键、无残留中文）。
async function scenarioL() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const { context, page } = await L.launch('l', [L.savePickerStub()]);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#probe-section:not([hidden])');
    await page.click('#convert-btn');
    await waitVisible(page, '#result-section:not([hidden])');
    await page.click('.lang-toggle');
    await page.waitForFunction(() => document.documentElement.lang === 'en');
    await L.shot(page, 'en-locale');
    const audit = await page.evaluate(() => {
      const clone = document.body.cloneNode(true);
      clone.querySelectorAll('.lang-toggle').forEach((el) => el.remove());
      const text = clone.innerText || '';
      const rawKeys = (text.match(/\btools\.[a-z0-9.]+/g) || []);
      const cjk = (text.match(/[\u4e00-\u9fff]/g) || []).join('');
      const saveLabel = (document.getElementById('save-btn') || {}).textContent || '';
      const saveSupported = typeof window.showSaveFilePicker === 'function';
      return { rawKeys, cjkSample: cjk.slice(0, 40), saveDisabled: document.getElementById('save-btn').disabled, saveLabel, saveSupported };
    });
    // 切回 zh 也应正常（保存按钮文案回中文）
    await page.click('.lang-toggle');
    await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
    const backZh = await page.evaluate(() => ({
      h1: document.querySelector('h1').textContent,
      save: document.getElementById('save-btn').textContent,
    }));
    record('l-zh-en-toggle', audit.rawKeys.length === 0 && audit.cjkSample === ''
      && /保存到磁盘/.test(backZh.save) && /本地切片工具/.test(backZh.h1),
      { ...audit, backZhH1: backZh.h1, backZhSave: backZh.save });
  } catch (e) {
    record('l-zh-en-toggle', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (m) 纯键盘主流程（Tab/Enter/方向键 + 文件选择器经 filechooser 事件）。
async function scenarioM() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const { context, page } = await L.launch('m', [L.savePickerStub()]);
  page.on('dialog', (d) => d.accept());
  try {
    await L.openTools(page, PORT);

    // Tab 到文件输入并 Enter 打开选择器（filechooser 由驱动设置文件）
    const focusFileInput = async () => {
      for (let i = 0; i < 40; i++) {
        const tag = await page.evaluate(() => `${document.activeElement && document.activeElement.id}|${document.activeElement && document.activeElement.tagName}`);
        if (tag.startsWith('file-input|')) return true;
        await page.keyboard.press('Tab');
      }
      return false;
    };
    if (!(await focusFileInput())) throw new Error('file input not reachable by Tab');
    const [chooser] = await Promise.all([
      page.waitForEvent('filechooser'),
      page.keyboard.press('Enter'),
    ]);
    await chooser.setFiles(kfb);
    await waitVisible(page, '#probe-section:not([hidden])');

    // 资源档位 radio：Tab 进入组，方向键可选（值必须真的改变）
    for (let i = 0; i < 20; i++) {
      const inGroup = await page.evaluate(() => {
        const a = document.activeElement;
        return a && a.name === 'profile';
      });
      if (inGroup) break;
      await page.keyboard.press('Tab');
    }
    const profileBefore = await page.evaluate(() =>
      (document.querySelector('input[name="profile"]:checked') || {}).value);
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('ArrowDown');
    const profileChanged = await page.evaluate(() => {
      const c = document.querySelector('input[name="profile"]:checked');
      return c && c.value;
    });

    // Tab 到开始转换并 Enter
    const focusAndPress = async (id) => {
      for (let i = 0; i < 60; i++) {
        const hit = await page.evaluate((b) => document.activeElement === document.getElementById(b), id);
        if (hit) { await page.keyboard.press('Enter'); return true; }
        await page.keyboard.press('Tab');
      }
      return false;
    };
    if (!(await focusAndPress('convert-btn'))) throw new Error('convert button not keyboard reachable');
    await waitVisible(page, '#result-section:not([hidden])', 120000);
    // Tab 到保存并 Enter（picker stub 在 OPFS 落盘）
    if (!(await focusAndPress('save-btn'))) throw new Error('save button not keyboard reachable');
    await waitText(page, '#save-status', /已保存|Saved/, 60000);
    const saved = await L.opfsSha256(page);

    // 对话框键盘：硬不足的错误不是对话框——用删除确认（native confirm）覆盖；
    // <dialog> 焦点圈已由 d1 的 focusIn/focusRestored 断言覆盖。
    record('m-keyboard-only', !!profileChanged && profileChanged !== profileBefore && !!saved.sha256,
      { profileBefore, profileChangedTo: profileChanged, savedSha256: saved.sha256 });
  } catch (e) {
    record('m-keyboard-only', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

/// 测试侧只读 OPFS 任务记录（经 engine.js 的双槽读取），不另起 runner 实例。
const READ_JOB_RECORDS = `window.__readJobRecords = async () => {
  const E = await import('/static/tools/slide-transform/engine.js');
  const root = await navigator.storage.getDirectory();
  let jobs;
  try { jobs = await root.getDirectoryHandle('slide-jobs'); } catch { return []; }
  const out = [];
  for await (const [name, h] of jobs.entries()) {
    if (h.kind !== 'directory' || name.startsWith('.')) continue;
    const rec = await E.readSlotRecord(h, 'job').catch(() => null);
    if (rec) out.push(rec);
  }
  return out;
};`;

// (n) C4-1：选择 → 准备 → 刷新 → 从任务列表开始，channel.json 不丢。
//   n1：伴随文件先于源文件选择（随 probe 写入准备记录）
//   n2：源文件准备完成后才选伴随文件（setPreparedChannelJson 写回）
async function scenarioN() {
  const kfbf = L.ensureFixture('fl-600x400.kfbf', ['gen-kfbf']);
  const cj = channelFixture();
  for (const variant of ['n1', 'n2']) {
    const id = `${variant}-channel-json-refresh-list-start`;
    const { context, page } = await L.launch(variant, [L.savePickerStub(), READ_JOB_RECORDS]);
    page.on('dialog', (d) => d.accept());
    try {
      await L.openTools(page, PORT);
      if (variant === 'n1') await page.setInputFiles('#channel-input', cj);
      await L.setFile(page, kfbf);
      await waitVisible(page, '#probe-section:not([hidden])');
      if (variant === 'n2') await page.setInputFiles('#channel-input', cj);
      // 等准备记录里落下 channel.json 后再刷新（异步谓词不能交给 waitForFunction：
      // 它把返回的 Promise 当作真值立即放行）
      const deadline = Date.now() + 20000;
      for (;;) {
        const recs = await page.evaluate(() => window.__readJobRecords());
        if (recs.length === 1 && recs[0].state === 'prepared' && recs[0].channelJson) break;
        if (Date.now() > deadline) throw new Error(`prepared record lacks channel.json: ${JSON.stringify(recs.map((r) => ({ state: r.state, gen: r.gen })))}`);
        await page.waitForTimeout(200);
      }
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
      const startBtn = '.job-row[data-next-action="start"] button[data-action="start"]';
      await waitVisible(page, startBtn);
      const meta = await page.textContent('.job-row[data-next-action="start"]');
      if (!/channel\.json/.test(meta)) throw new Error(`job row lacks channel.json marker: ${meta}`);
      await page.click(startBtn);
      await waitVisible(page, '#result-section:not([hidden])', 120000);
      const windows = await assertDisplayWindows(page);
      const rec = (await page.evaluate(() => window.__readJobRecords()))[0];
      const report = (rec.result && rec.result.channels) || [];
      const byName = Object.fromEntries(report.map((c) => [c.name, c.display_window]));
      if (JSON.stringify(byName.DAPI) !== '[13,227]' || JSON.stringify(byName['520']) !== '[16,175]') {
        throw new Error(`job report display windows wrong: ${JSON.stringify(byName)}`);
      }
      record(id, true, { displayWindows: windows, report: byName });
    } catch (e) {
      record(id, false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }
}

// ------------------------------------------------------------------ main --

const SCENARIOS = [
  ['a', scenarioA], ['b', scenarioB], ['c', scenarioC], ['d', scenarioD],
  ['e', scenarioE], ['f', scenarioF], ['g', scenarioG], ['h', scenarioH],
  ['i', scenarioI], ['k', scenarioK], ['l', scenarioL], ['m', scenarioM],
  ['n', scenarioN],
];

async function main() {
  fs.mkdirSync(L.GATE, { recursive: true });
  server = await L.startServer(PORT);
  console.log(`c3 e2e server on :${PORT}`);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  try {
    for (const [id, fn] of SCENARIOS) {
      if (ONLY && id !== ONLY) continue;
      const t0 = Date.now();
      await fn();
      console.log(`[${id}] ${((Date.now() - t0) / 1000).toFixed(1)}s`);
    }
  } finally {
    try { server.kill('SIGTERM'); } catch { /* */ }
  }
  L.writeJson('e2e/results.json', {
    finishedAt: new Date().toISOString(),
    pass: Object.values(results).every((r) => r.pass),
    results,
  });
  const failed = Object.entries(results).filter(([, r]) => !r.pass).map(([k]) => k);
  console.log(failed.length ? `E2E FAILED: ${failed.join(', ')}` : 'E2E ALL PASS');
}

main().catch((e) => { console.error(e); process.exit(1); });
