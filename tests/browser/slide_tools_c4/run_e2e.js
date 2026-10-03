#!/usr/bin/env node
// C4 工具页上传 e2e（真实 Flask /tools/slides + 登录会话 + page.route 假
// ingestion/COS）。场景 a–i 对应任务书；每场景断言 OPFS 产物 sha256 不变。
// 复跑：node tests/browser/slide_tools_c4/run_e2e.js（server 见 server.py）。
'use strict';
const path = require('path');
const fs = require('fs');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8953'));
const CREDS = L.arg('creds', path.join(L.GATE, 'creds.json'));
const ONLY = L.arg('only', null);

const results = {};
let server = null;

function record(id, pass, detail) {
  results[id] = { pass, ...detail };
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${JSON.stringify(detail).slice(0, 300)}`);
  if (!pass) process.exitCode = 1;
}

/// Node 侧轮询（Playwright waitForFunction 不 await async 谓词——任务书警示）。
async function waitFor(cond, timeout = 60000, label = '') {
  const t0 = Date.now();
  for (;;) {
    let v;
    try { v = await cond(); } catch (e) { v = false; }
    if (v) return v;
    if (Date.now() - t0 > timeout) throw new Error(`timeout: ${label}`);
    await new Promise((r) => setTimeout(r, 200));
  }
}

async function textOf(page, sel) {
  return String((await page.textContent(sel)) || '');
}

/// UI 文案断言的期望值取自页面 i18n 表当前键值（见 C3 run_e2e.js 同名助手）：
/// 断言不随浏览器语言漂移，且键缺失（t 回显键本身）时直接判错。
async function i18nLabel(page, key) {
  const s = await page.evaluate((k) => window.HP_I18N.t(k), key);
  if (!s || s === key || s.startsWith('tools.')) throw new Error(`i18n ${key} unresolved: "${s}"`);
  return s;
}

async function currentJobId(page) {
  return page.$eval('.job-row', (r) => r.dataset.jobId);
}

async function rowUploadState(page) {
  const el = await page.$('.job-upload-state');
  if (!el) return null;
  return el.getAttribute('data-upload-state');
}

/// 登录 + 打开工具页 + 转换一个夹具到 ready。返回 { page, context, jobId }。
async function convertFixture(context, page, fake, file) {
  await L.setFile(page, file);
  await waitFor(async () => (await page.$('#probe-section:not([hidden])')) !== null, 60000, 'probe');
  await page.click('#convert-btn');
  await waitFor(async () => (await page.$('#result-section:not([hidden])')) !== null, 120000, 'ready');
  const jobId = await currentJobId(page);
  return { jobId };
}

async function main() {
  fs.mkdirSync(L.GATE, { recursive: true });
  // 默认总是起自己的服务（它会重写 creds）；--reuse-server 才复用已在跑的实例
  if (!process.argv.includes('--reuse-server')) server = await L.startServer(PORT, CREDS);
  const creds = L.readCreds(CREDS);

  const bf = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const fl = L.ensureFixture('fl-600x400.kfbf', ['gen-kfbf']);

  // ---------------------------------------------------------------- (a) --
  async function scenarioHappy(modality, file) {
    const label = modality === 'fl' ? 'a-fl' : 'a-bf';
    const { context, page } = await L.launch(label, [L.savePickerStub(), L.downloadGuard()]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 4 });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, file);
      const before = await L.opfsJobSha256(page, jobId);
      await page.click('#upload-btn');
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 60000, 'published');
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      const name = fake.st.creates[0].filename;
      // both modalities now produce OME (brightfield: RGB OME profile)
      const wantExt = '.ome.tif';
      if (!name.endsWith(wantExt)) throw new Error(`filename ${name} should end ${wantExt}`);
      const putBytes = fake.st.puts.reduce((s, p) => s + p.bytes, 0);
      if (putBytes !== before.size) throw new Error(`PUT bytes ${putBytes} != ${before.size}`);
      const link = await page.$eval('#upload-status a', (a) => a.getAttribute('href'));
      if (link !== '/app') throw new Error(`workbench link ${link}`);
      await waitFor(async () => (await rowUploadState(page)) === 'published', 30000, 'row published');
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      record(label, true, {
        filename: name, putBytes, slideLink: link,
        sha: before.sha256.slice(0, 12), size: before.size,
      });
    } catch (e) {
      record(label, false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (b) --
  async function scenario401() {
    const { context, page } = await L.launch('b-401');
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      const before = await L.opfsJobSha256(page, jobId);
      fake.behavior.onSign = () => ({ status: 401, body: { code: 'auth_required', error: '登录已过期' } });
      await page.click('#upload-btn');
      await waitFor(async () => /登录已过期|session expired/i.test(await textOf(page, '#upload-status')), 60000, '401 message');
      const href = await page.$eval('#upload-status a', (a) => a.getAttribute('href'));
      if (href !== '/login?next=/tools/slides') throw new Error(`login link ${href}`);
      // 未自动跳转：仍在工具页
      if (!page.url().includes('/tools/slides')) throw new Error(`redirected to ${page.url()}`);
      // 产物保留（任务仍 ready），ingestion 已创建（1 个）
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      const kept = await L.opfsJobSha256(page, jobId);
      if (kept.sha256 !== before.sha256) throw new Error('artifact sha changed on 401');
      // “恢复登录”（撤掉 401 注入）→ 继续同一 ingestion → published
      fake.behavior.onSign = null;
      await page.click('[data-action="upload-continue"]');
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 60000, 'published after re-login');
      if (fake.st.creates.length !== 1) throw new Error(`second ingestion created: ${fake.st.creates.length}`);
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      record('b-401-continue', true, {
        loginLink: href, creates: fake.st.creates.length,
        sha: before.sha256.slice(0, 12),
      });
    } catch (e) {
      record('b-401-continue', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (c) --
  async function scenarioRefresh() {
    const { context, page } = await L.launch('c-refresh');
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 4 });
    const gates = new Set();
    fake.behavior.gatePut = async (n) => { if (gates.has(n)) await new Promise(() => {}); };
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      const before = await L.opfsJobSha256(page, jobId);
      gates.add(3); gates.add(4);   // 分块 3/4 挂起 → 上传中刷新
      await page.click('#upload-btn');
      await waitFor(() => fake.st.puts.length >= 2, 60000, 'first 2 parts PUT');
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      // 列表展示上传状态 + 继续上传
      await waitFor(async () => (await page.$('[data-action="upload-continue"]')) !== null, 60000, 'continue button');
      if ((await rowUploadState(page)) !== 'uploading') throw new Error('row upload state not shown');
      const putsBefore = fake.st.puts.length;
      const signsBefore = JSON.parse(JSON.stringify(fake.st.signs));
      if (signsBefore[0].length !== 4) throw new Error(`first sign batch ${JSON.stringify(signsBefore)}`);
      gates.delete(3); gates.delete(4);
      await page.click('[data-action="upload-continue"]');
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 60000, 'published after refresh');
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      // 只补传未确认分块：续传签名只含 [3,4]，PUT 只增加 3/4
      const signsAfter = fake.st.signs.slice(signsBefore.length);
      if (JSON.stringify(signsAfter) !== '[[3,4]]') {
        throw new Error(`resume signed ${JSON.stringify(fake.st.signs)}`);
      }
      const newPuts = fake.st.puts.slice(putsBefore).map((p) => p.partNumber).sort();
      if (JSON.stringify(newPuts) !== '[3,4]') throw new Error(`resume put ${JSON.stringify(newPuts)}`);
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      record('c-refresh-continue', true, {
        creates: fake.st.creates.length, resumeSigned: signsAfter,
        resumePuts: newPuts, sha: before.sha256.slice(0, 12),
      });
    } catch (e) {
      record('c-refresh-continue', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (d) --
  async function scenarioRepeatClicks() {
    const { context, page } = await L.launch('d-repeat');
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 4 });
    let release = null;
    fake.behavior.gateComplete = () => new Promise((r) => { release = r; });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      const before = await L.opfsJobSha256(page, jobId);
      // 三连击上传按钮：busy 守卫 → 恰一个 create（首击后按钮即禁用，
      // 追加击经 dispatchEvent 模拟快速重复点击，不经 actionability 等待）
      await page.click('#upload-btn');
      await page.evaluate(() => {
        const b = document.getElementById('upload-btn');
        b.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        b.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      });
      await waitFor(() => fake.st.creates.length === 1, 60000, 'one create');
      await waitFor(() => fake.st.puts.length === 4, 60000, 'all parts PUT');
      // complete 被闸住 → “继续上传”两连击：仍不新建（按钮忙时禁用，
      // dispatchEvent 模拟快速重复点击）
      await waitFor(async () => (await page.$('[data-action="upload-continue"]')) !== null, 60000, 'row rerendered');
      await page.evaluate(() => {
        const btns = document.querySelectorAll('[data-action="upload-continue"]');
        btns.forEach((b) => {
          b.dispatchEvent(new MouseEvent('click', { bubbles: true }));
          b.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        });
      });
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      if (release) release();
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 60000, 'published');
      if (fake.st.creates.length !== 1) throw new Error(`creates after continue: ${fake.st.creates.length}`);
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      record('d-repeated-clicks', true, { creates: 1, sha: before.sha256.slice(0, 12) });
    } catch (e) {
      record('d-repeated-clicks', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (e) --
  async function scenarioLostComplete() {
    const { context, page } = await L.launch('e-lost');
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    fake.behavior.onComplete = (job, nth) => (nth === 1 ? 'abort'
      : { status: 409, body: { code: 'ingestion_state_conflict' } });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      const before = await L.opfsJobSha256(page, jobId);
      await page.click('#upload-btn');
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 60000, 'published');
      if (fake.st.completeReqs !== 2) throw new Error(`completeReqs=${fake.st.completeReqs}`);
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      record('e-lost-complete', true, {
        completeReqs: fake.st.completeReqs, sha: before.sha256.slice(0, 12),
      });
    } catch (e) {
      record('e-lost-complete', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ------------------------------------------------------- (f/g) 禁用原因 --
  async function scenarioDisabled(kind) {
    const { context, page } = await L.launch(kind, [L.savePickerStub(), L.downloadGuard()]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    if (kind === 'f-oversize') {
      fake.behavior.onCapability = () => ({
        status: 200,
        body: {
          cos_upload: {
            available: true, manual_only: true, formats: ['tif', 'tiff'],
            max_size_bytes: 1000, part_bytes: 8, url_ttl_seconds: 600,
            max_concurrent_parts: 2, sign_batch_max_parts: 4, policy_version: 'v1-manual',
          },
          viewable_formats: ['classic-bigtiff-jpeg-pyramid',
            'ome-bigtiff-subifd-rgb-jpeg-pyramid',
            'ome-bigtiff-subifd-multichannel-jpeg-passthrough'],
        },
      });
    } else {
      fake.behavior.onCapability = () => ({
        status: 200,
        body: {
          cos_upload: {
            available: true, manual_only: true, formats: ['tif', 'tiff'],
            max_size_bytes: 900000000, part_bytes: 8, url_ttl_seconds: 600,
            max_concurrent_parts: 2, sign_batch_max_parts: 4, policy_version: 'v1-manual',
          },
          viewable_formats: ['some-future-format'],
        },
      });
    }
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      const before = await L.opfsJobSha256(page, jobId);
      await page.click('#upload-btn');
      const want = kind === 'f-oversize' ? /超过平台上限|exceeds the platform/i
        : /不支持查看|cannot view/i;
      await waitFor(async () => want.test(await textOf(page, '#upload-status')), 60000, 'reason');
      // 零 ingestion 调用
      if (fake.st.creates.length !== 0 || fake.st.statusGets !== 0) {
        throw new Error(`ingestion calls: creates=${fake.st.creates.length}`);
      }
      // 按钮对该结果保持禁用
      const disabled = await page.$eval('#upload-btn', (b) => b.disabled);
      if (!disabled) throw new Error('upload button not disabled');
      // 本地保存仍可用
      await page.click('#save-btn');
      await waitFor(async () => /已保存|Saved/.test(await textOf(page, '#save-status')), 60000, 'save');
      const saved = await L.C3.opfsSha256(page);
      if (saved.sha256 !== before.sha256) throw new Error('saved sha mismatch');
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      record(kind, true, {
        reason: (await textOf(page, '#upload-status')).slice(0, 40),
        creates: 0, savedSha: saved.sha256.slice(0, 12),
      });
    } catch (e) {
      record(kind, false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (h) --
  async function scenarioCancelTerminal() {
    const { context, page } = await L.launch('h-cancel');
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    let release = null;
    fake.behavior.gateComplete = () => new Promise((r) => { release = r; });
    page.on('dialog', (d) => d.accept());
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      const before = await L.opfsJobSha256(page, jobId);
      await page.click('#upload-btn');
      await waitFor(() => fake.st.puts.length === 2, 60000, 'parts PUT (complete gated)');
      // 上传进行中：删除被禁
      const discardBlocked = await page.$eval('[data-job-discard]', (b) => b.disabled);
      if (!discardBlocked) throw new Error('discard not blocked during upload');
      // 取消上传 → 产物保留、任务仍 ready、记录 cancelled、删除解禁
      await page.click('#upload-cancel-btn');
      await waitFor(async () => /已取消上传|Upload cancelled/.test(await textOf(page, '#upload-status')), 60000, 'cancelled msg');
      if (release) { release(); release = null; }   // 释放被闸住的 complete（引擎侧随取消悬置）
      if (fake.st.cancels !== 1) throw new Error(`cancels=${fake.st.cancels}`);
      await waitFor(async () => (await rowUploadState(page)) === 'cancelled', 30000, 'row cancelled');
      const kept = await L.opfsJobSha256(page, jobId);
      if (kept.sha256 !== before.sha256) throw new Error('artifact sha changed after cancel');
      const rowState = await page.$eval('.job-row', (r) => r.dataset.nextAction);
      if (rowState !== 'export') throw new Error(`job state ${rowState} (want export/ready)`);
      const discardOpen = await page.$eval('[data-job-discard]', (b) => !b.disabled);
      if (!discardOpen) throw new Error('discard still blocked after cancel');
      // 终态失败：取消后显式重试 → 新 ingestion；假后端直接回 terminal
      fake.behavior.onStatus = () => 'terminal';
      fake.behavior.onCreate = () => ({ status: 202, body: {} });
      const fakeFail = fake;
      void fakeFail;
      await page.click('[data-action="upload"]');
      await waitFor(async () => /终态失败|terminally failed/i.test(await textOf(page, '#upload-status')), 60000, 'terminal msg');
      if (fake.st.creates.length !== 2) throw new Error(`creates=${fake.st.creates.length} (want 2: retry after cancel)`);
      if ((await rowUploadState(page)) !== 'failed') throw new Error('row state not failed');
      const kept2 = await L.opfsJobSha256(page, jobId);
      if (kept2.sha256 !== before.sha256) throw new Error('artifact sha changed after terminal');
      // 删除允许（终态后）→ 目录清空
      await page.click('[data-job-discard]');
      await waitFor(async () => (await L.jobDirs(page)).length === 0, 60000, 'job discarded');
      record('h-cancel-terminal', true, {
        cancels: 1, creates: fake.st.creates.length,
        sha: before.sha256.slice(0, 12), discarded: true,
      });
    } catch (e) {
      record('h-cancel-terminal', false, { error: String(e).slice(0, 400) });
    } finally {
      if (release) release();
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (i) --
  async function scenarioNetwork() {
    const { context, page } = await L.launch('i-network', [L.savePickerStub(), L.downloadGuard()]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    const reqs = [];
    context.on('request', (r) => reqs.push({ url: r.url(), method: r.method(), r }));
    try {
      // 先登录（会话 cookie 就位），再开始捕获——捕获窗口覆盖工具页全程
      await L.login(page, PORT, creds, 'user');
      reqs.length = 0;
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      const before = await L.opfsJobSha256(page, jobId);
      // 点击前：零 /api/、零跨源
      const preApi = reqs.filter((x) => x.url.includes('/api/'));
      const preCross = reqs.filter((x) => !x.url.startsWith(`http://127.0.0.1:${PORT}`));
      if (preApi.length || preCross.length) {
        throw new Error(`before click: api=${JSON.stringify(preApi.map((x) => x.url))} cross=${JSON.stringify(preCross.map((x) => x.url))}`);
      }
      await page.click('#upload-btn');
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 60000, 'published');
      // 点击后：只允许能力端点 + /api/ingestions* + COS origin
      const bad = reqs.filter((x) => {
        const u = x.url;
        if (u.startsWith(`http://127.0.0.1:${PORT}`)) {
          return u.includes('/api/') && !u.includes('/api/tools/slides/upload-capability')
            && !u.includes('/api/ingestions');
        }
        return !u.startsWith(creds.cosOrigin);
      });
      if (bad.length) throw new Error(`unexpected requests: ${JSON.stringify(bad.map((x) => x.url))}`);
      const capCalls = reqs.filter((x) => x.url.includes('/api/tools/slides/upload-capability'));
      if (capCalls.length < 1) throw new Error('capability not fetched on click');
      // 文件字节只出现在 COS PUT 体：非 COS 请求体不含产物字节
      const prodPrefix = (await before.sha256).slice(0, 8);
      for (const x of reqs) {
        if (x.url.startsWith(creds.cosOrigin)) continue;
        const pd = x.r.postData();
        if (pd && pd.length > 4096) throw new Error(`large body (${pd.length}B) to ${x.url}`);
        if (pd && pd.includes(prodPrefix)) throw new Error(`sha fragment in body to ${x.url}`);
      }
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      record('i-network-capture', true, {
        preClickApi: 0, preClickCross: 0,
        postClickUrls: [...new Set(reqs.map((x) => x.method + ' ' + x.url.replace(/\?.*/, '').replace(/^https?:\/\/[^/]+/, '')))].slice(0, 10),
        putBytes: fake.st.puts.reduce((s, p) => s + p.bytes, 0),
      });
    } catch (e) {
      record('i-network-capture', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (j) --
  // 真实会话丢失：上传中清掉登录 cookie 并刷新 → 继续上传时真实能力端点回 401 →
  // 登录提示（不跳转）→ 真实重新登录 → 继续同一 ingestion → published。
  async function scenarioRealSessionLoss() {
    const { context, page } = await L.launch('j-session');
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    let hold = true;
    fake.behavior.gatePut = async (n) => { if (hold && n === 2) await new Promise(() => {}); };
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      const before = await L.opfsJobSha256(page, jobId);
      await page.click('#upload-btn');
      await waitFor(() => fake.st.puts.length >= 1, 60000, 'first part PUT');
      await context.clearCookies();
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await waitFor(async () => (await page.$('[data-action="upload-continue"]')) !== null, 60000, 'continue button');
      hold = false;
      await page.click('[data-action="upload-continue"]');
      await waitFor(async () => /登录|sign in/i.test(await textOf(page, '#upload-status')), 60000, 'login prompt');
      const href = await page.$eval('#upload-status a', (a) => a.getAttribute('href'));
      if (href !== '/login?next=/tools/slides') throw new Error(`login link ${href}`);
      if (!page.url().includes('/tools/slides')) throw new Error(`redirected to ${page.url()}`);
      const kept = await L.opfsJobSha256(page, jobId);
      if (kept.sha256 !== before.sha256) throw new Error('artifact sha changed after session loss');
      await L.login(page, PORT, creds, 'user');
      await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await waitFor(async () => (await page.$('[data-action="upload-continue"]')) !== null, 60000, 'continue after login');
      await page.click('[data-action="upload-continue"]');
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 60000, 'published');
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      record('j-real-session-loss', true, { loginLink: href, creates: 1, sha: before.sha256.slice(0, 12) });
    } catch (e) {
      record('j-real-session-loss', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (k) --
  // 跨标签：A 上传中 → B 继续上传被拒（另一标签）、B 删除被拒；关掉 A 后 B 的
  // 遗留记录经二次确认放弃（通知服务端取消）才删除。
  async function scenarioCrossTab() {
    const { context, page } = await L.launch('k-tabs');
    const fake = await L.fakeUploadRoutes(context, creds.cosOrigin, { partsCount: 2 });
    fake.behavior.gateComplete = () => new Promise(() => {});
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      await page.click('#upload-btn');
      await waitFor(() => fake.st.puts.length === 2 && fake.st.completeReqs === 1, 60000, 'A uploading');

      const b = await context.newPage();
      const dialogs = [];
      b.on('dialog', (d) => { dialogs.push(d.message()); d.accept(); });
      await L.C3.openTools(b, PORT);
      await waitFor(async () => (await b.$('[data-action="upload-continue"]')) !== null, 60000, 'B continue');
      await b.click('[data-action="upload-continue"]');
      await waitFor(async () => /另一个标签页|another tab/.test(await textOf(b, '#upload-status')), 30000, 'other-tab msg');
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      await b.click('[data-job-discard]');
      await waitFor(async () => /上传进行中|Upload in progress/.test(await textOf(b, '#page-error')), 30000, 'discard refused');
      if ((await L.jobDirs(b)).length !== 1) throw new Error('job removed while A uploading');
      if (dialogs.length !== 1) throw new Error(`abandon prompt shown while A holds the lock: ${dialogs.length}`);

      await page.close();
      await b.reload({ waitUntil: 'load' });
      await b.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await waitFor(async () => (await b.$('[data-job-discard]')) !== null, 30000, 'B row');
      await b.click('[data-job-discard]');
      await waitFor(async () => (await L.jobDirs(b)).length === 0, 60000, 'abandoned + discarded');
      if (dialogs.length !== 3) throw new Error(`dialogs=${dialogs.length} (want discard + discard + abandon)`);
      if (fake.st.cancels !== 1) throw new Error(`cancels=${fake.st.cancels}`);
      record('k-cross-tab-abandon', true, { jobId, creates: 1, cancels: 1, dialogs: dialogs.length });
    } catch (e) {
      record('k-cross-tab-abandon', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (l) --
  // 记录写入失败（真实工具页 OPFS 适配器：job.{a,b}.json 的 createWritable 被迫
  // 失败）→ 不发任何分块、取消刚建的任务；取消未确认时不再新建，直到确认取消。
  async function scenarioPersistFailure() {
    const { context, page } = await L.launch('l-persist');
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    const fakeStatus = () => ({ creates: fake.st.creates.length, cancels: fake.st.cancels,
      signs: fake.st.signs.length, puts: fake.st.puts.length, gets: fake.st.statusGets });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      const before = await L.opfsJobSha256(page, jobId);
      await page.evaluate(() => {
        const orig = FileSystemFileHandle.prototype.createWritable;
        FileSystemFileHandle.prototype.createWritable = function (...a) {
          if (window.__failRecords && /^job\.[ab]\.json$/.test(this.name)) {
            return Promise.reject(new DOMException('forced record write failure', 'QuotaExceededError'));
          }
          return orig.apply(this, a);
        };
        window.__failRecords = true;
      });
      const noTransfer = () => {
        const f = fakeStatus();
        if (f.signs || f.puts || f.gets) throw new Error(`transmission after failed record write: ${JSON.stringify(f)}`);
      };
      const click = async () => {
        await waitFor(async () => !(await page.$eval('#upload-btn', (b) => b.disabled)), 30000, 'upload enabled');
        await page.click('#upload-btn');
      };

      await click();
      await waitFor(() => fake.st.creates.length === 1 && fake.st.cancels === 1, 30000, 'attempt 1 cancelled');
      await waitFor(async () => /已取消|was cancelled/.test(await textOf(page, '#upload-status')), 30000, 'persist.failed msg');
      noTransfer();
      await click();
      await waitFor(() => fake.st.creates.length === 2 && fake.st.cancels === 2, 30000, 'attempt 2 cancelled');
      noTransfer();

      fake.behavior.onCancel = () => ({ status: 503, body: { code: 'unavailable' } });
      await click();
      await waitFor(async () => /还没能确认取消|not confirmed yet/.test(await textOf(page, '#upload-status')), 30000, 'orphan msg');
      if (fake.st.creates.length !== 3) throw new Error(`creates=${fake.st.creates.length}`);
      const orphanId = `inj_c4_3`;
      const stored = await page.evaluate(() => localStorage.getItem('pt.tools.upload.orphans'));
      if (!stored || !stored.includes(orphanId)) throw new Error(`orphan not remembered: ${stored}`);
      await page.evaluate(() => { document.getElementById('upload-status').textContent = ''; });
      await click();
      await waitFor(async () => /确认取消之前|no new upload is created/.test(await textOf(page, '#upload-status')), 30000, 'orphan.pending msg');
      if (fake.st.creates.length !== 3) throw new Error(`created while orphan unconfirmed: ${fake.st.creates.length}`);
      if (fake.st.cancels !== 4) throw new Error(`orphan cancel not retried: cancels=${fake.st.cancels}`);
      noTransfer();

      fake.behavior.onCancel = null;
      await page.evaluate(() => { window.__failRecords = false; });
      await click();
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 60000, 'published after recovery');
      if (fake.st.cancels !== 5) throw new Error(`orphan cancel before create: cancels=${fake.st.cancels}`);
      if (fake.st.creates.length !== 4) throw new Error(`creates=${fake.st.creates.length}`);
      const putBytes = fake.st.puts.reduce((n, x) => n + x.bytes, 0);
      if (putBytes !== before.size) throw new Error(`PUT bytes ${putBytes} != artifact ${before.size}`);
      const left = await page.evaluate(() => localStorage.getItem('pt.tools.upload.orphans'));
      if (left) throw new Error(`orphan key left: ${left}`);
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      record('l-record-write-failure', true, { ...fakeStatus(), putBytes, sha: before.sha256.slice(0, 12) });
    } catch (e) {
      record('l-record-write-failure', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (m) --
  // 轮询等待 / 签名退避期间取消：done 立即收口、上传锁释放（A 标签仍开着时，B 标签
  // 可直接删除任务），取消之后不再有控制请求。
  async function scenarioCancelDuringWait(kind) {
    const id = kind === 'waiting' ? 'm1-cancel-during-poll' : 'm2-cancel-during-backoff';
    const { context, page } = await L.launch(id);
    const fake = await L.fakeUploadRoutes(context, creds.cosOrigin, { partsCount: 2 });
    if (kind === 'waiting') fake.behavior.onStatus = () => 'waiting_space';
    else fake.behavior.onSign = () => ({ status: 429, body: { code: 'rate_limited' } });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      const before = await L.opfsJobSha256(page, jobId);
      await page.click('#upload-btn');
      await waitFor(() => (kind === 'waiting' ? fake.st.statusGets >= 1 : fake.st.signs.length >= 1), 30000, 'in wait');
      await page.waitForTimeout(300);
      const t0 = Date.now();
      await page.click('#upload-cancel-btn');
      await waitFor(async () => /已取消上传|Upload cancelled/.test(await textOf(page, '#upload-status')), 10000, 'cancelled msg');
      await waitFor(async () => (await rowUploadState(page)) === 'cancelled', 10000, 'row cancelled');
      const settleMs = Date.now() - t0;
      const gets = fake.st.statusGets;
      const signs = fake.st.signs.length;

      const b = await context.newPage();
      const dialogs = [];
      b.on('dialog', (d) => { dialogs.push(d.message()); d.accept(); });
      await L.C3.openTools(b, PORT);
      await waitFor(async () => (await b.$('[data-job-discard]')) !== null, 30000, 'B row');
      await b.click('[data-job-discard]');
      await waitFor(async () => (await L.jobDirs(b)).length === 0, 30000, 'B discarded while A open');
      if (dialogs.length !== 1) throw new Error(`dialogs=${dialogs.length} (want only the discard confirm)`);

      await page.waitForTimeout(kind === 'waiting' ? 6000 : 4000);
      if (fake.st.statusGets !== gets || fake.st.signs.length !== signs) {
        throw new Error(`requests after cancel: gets ${gets}->${fake.st.statusGets} signs ${signs}->${fake.st.signs.length}`);
      }
      if (fake.st.cancels !== 1) throw new Error(`cancels=${fake.st.cancels}`);
      if (fake.st.puts.length !== 0) throw new Error(`puts=${fake.st.puts.length}`);
      record(id, true, { settleMs, cancels: 1, requestsAfterCancel: 0, sha: before.sha256.slice(0, 12) });
    } catch (e) {
      record(id, false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (n) --
  // 明场输出格式选「经典金字塔 TIFF」：上传创建的是 <base>.tif（非 .ome.tif），
  // PUT 字节 == 产物大小，产物 sha256 == 原生 CLI --profile bf-classic。
  async function scenarioClassicUpload() {
    const { context, page } = await L.launch('n-classic', [L.savePickerStub(), L.downloadGuard()]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 4 });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      await L.setFile(page, bf);
      await waitFor(async () => (await page.$('#probe-section:not([hidden])')) !== null, 60000, 'probe');
      await waitFor(async () => (await page.$('#format-section:not([hidden])')) !== null, 30000, 'format choice');
      await page.check('#format-classic');
      await page.click('#convert-btn');
      await waitFor(async () => (await page.$('#result-section:not([hidden])')) !== null, 120000, 'ready');
      const jobId = await currentJobId(page);
      const before = await L.opfsJobSha256(page, jobId);
      const nativeDir = path.join(L.GATE, 'fixtures');
      fs.mkdirSync(nativeDir, { recursive: true });
      const native = L.nativeConvert(bf,
        path.join(nativeDir, 'bf-580x300-native-classic.tif'), ['--profile', 'bf-classic']);
      const nativeSha = await L.sha256File(native);
      if (before.sha256 !== nativeSha) {
        throw new Error(`classic artifact sha ${before.sha256} != native ${nativeSha}`);
      }
      // 格式行断言用 i18n 表值；「是哪个格式」另有稳定标识（上传创建的
      // 文件名后缀 + 产物 sha == 原生 --profile bf-classic）。
      const classicName = await i18nLabel(page, 'tools.result.format.bf-classic');
      const fmtRow = await textOf(page, '#result-format');
      if (!fmtRow.includes(classicName)) throw new Error(`format row "${fmtRow}"`);
      await page.click('#upload-btn');
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 60000, 'published');
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      const name = fake.st.creates[0].filename;
      if (!name.endsWith('.tif') || name.endsWith('.ome.tif')) {
        throw new Error(`filename ${name} should be <base>.tif (classic)`);
      }
      const putBytes = fake.st.puts.reduce((s, p) => s + p.bytes, 0);
      if (putBytes !== before.size) throw new Error(`PUT bytes ${putBytes} != ${before.size}`);
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      record('n-classic-upload', true, {
        filename: name, putBytes, sha: before.sha256.slice(0, 12),
        nativeSha: nativeSha.slice(0, 12), formatRow: fmtRow.trim().slice(0, 30),
      });
    } catch (e) {
      record('n-classic-upload', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (o) --
  // U1：字节级上传进度。page.route 的 fulfill 在请求体真正走网络前就完成，
  // 浏览器不会发送 body——XHR upload.onprogress 无从回调。因此本场景把假
  // COS 域名经 --host-resolver-rules 指到本地 HTTPS 假 COS（真实 socket），
  // 再用 CDP Network.emulateNetworkConditions 限制上行吞吐：网络层按字节
  // 缓慢发送 body，#upload-progress（aria-valuenow）在 PUT 完成前出现多次
  // 递增的中间值；上传 100% 后服务端接收/校验阶段持续显示到发布为止。
  // 单分片（partsCount=1，产物 ~289 KiB 全在同一个 PUT 里）：字节级更新
  // 只能来自 upload.onprogress，不能靠「分片完成」事件凑数（§2.4.1）。
  async function scenarioThrottledBytes() {
    const cosHost = new URL(creds.cosOrigin).host;
    const cos = await L.startLocalCos(cosHost);
    const { context, page } = await L.launch('o-bytes', [], [
      // 端口映射：URL 无端口（443），本地假 COS 在随机端口
      `--host-resolver-rules=MAP ${cosHost}:443 127.0.0.1:${cos.port}`,
      '--ignore-certificate-errors',   // 本地自签名证书（仅测试浏览器）
    ]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin,
      { partsCount: 1, skipCosRoute: true });
    // 默认假后端每个服务端阶段只展示一拍；钉住「接收 → 校验」各两拍，
    // 断言上传 100% 后页面持续显示接收/校验（不定态），直到可查看
    let serverPolls = 0;
    fake.behavior.onStatus = (job) => {
      if (fake.st.completeReqs === 0) return job.stage || 'uploading';
      serverPolls++;
      if (serverPolls <= 2) return 'downloading';
      if (serverPolls <= 4) return 'validating';
      job.stage = 'viewable';
      return 'viewable';
    };
    try {
      await L.login(page, PORT, creds, 'user');
      await L.C3.openTools(page, PORT);
      const { jobId } = await convertFixture(context, page, fake, bf);
      const before = await L.opfsJobSha256(page, jobId);
      const cdp = await context.newCDPSession(page);
      await cdp.send('Network.enable');
      await cdp.send('Network.emulateNetworkConditions', {
        offline: false, latency: 0,
        downloadThroughput: 1024 * 1024,   // 下载不限（状态轮询照常）
        uploadThroughput: 40 * 1024,       // 40 KB/s 上行 → ~7s 上传窗口
      });
      await page.click('#upload-btn');
      const pcts = [];
      const bytesTexts = [];
      const stageSeen = [];
      let indeterminateDuringServer = false;
      const deadline = Date.now() + 120000;
      for (;;) {
        const snap = await page.evaluate(() => {
          const bar = document.getElementById('upload-progress');
          const bytes = document.getElementById('upload-bytes');
          const status = document.getElementById('upload-status');
          return {
            hidden: bar.hidden,
            pct: bar.getAttribute('aria-valuenow'),
            indeterminate: bar.classList.contains('indeterminate'),
            bytes: bytes.hidden ? '' : (bytes.textContent || ''),
            stage: (status && status.textContent) || '',
          };
        });
        if (!snap.hidden) {
          if (snap.pct !== null) pcts.push(Number(snap.pct));
          bytesTexts.push(snap.bytes);
          if (snap.indeterminate && /接收|校验|处理/.test(snap.stage)) {
            indeterminateDuringServer = true;
          }
        }
        stageSeen.push(snap.stage);
        if (/已发布|Published/.test(snap.stage)) break;
        if (Date.now() > deadline) throw new Error('timeout waiting for published');
        await new Promise((r) => setTimeout(r, 250));
      }
      // ① PUT 完成前 ≥3 个互不相同的中间字节百分比（禁止只断言“有过进度”）
      const distinct = [...new Set(pcts)];
      const mid = distinct.filter((v) => v > 0 && v < 100);
      if (mid.length < 3) {
        throw new Error(`intermediate byte percentages insufficient: ${JSON.stringify(distinct)}`);
      }
      // ② 字节文本按字节加权（已传输 X / 总大小 Y）
      if (!bytesTexts.some((t) => /已传输|transferred/i.test(t))) {
        throw new Error(`no byte text seen: ${JSON.stringify([...new Set(bytesTexts)].slice(0, 4))}`);
      }
      // ③ 上传 100% 后、发布前：接收/校验阶段持续显示（不定态活动指示）
      const sawReceive = stageSeen.some((s) => /等待服务器接收|服务器接收中/.test(s));
      const sawValidate = stageSeen.some((s) => /正在校验|服务器处理中/.test(s));
      if (!sawReceive || !sawValidate) {
        throw new Error(`server stages missing before published: rx=${sawReceive} validate=${sawValidate}`);
      }
      if (!indeterminateDuringServer) throw new Error('no indeterminate bar during server stages');
      await waitFor(async () => (await rowUploadState(page)) === 'published', 30000, 'row published');
      // ④ 真实 socket 收到的 PUT 字节 == 产物大小（进度不改变传输合同）；
      //    单分片：恰好一个 PUT
      const putBytes = cos.st.puts.reduce((s, p) => s + p.bytes, 0);
      if (putBytes !== before.size) throw new Error(`local COS PUT bytes ${putBytes} != ${before.size}`);
      if (cos.st.puts.length !== 1) throw new Error(`puts=${JSON.stringify(cos.st.puts.map((p) => p.bytes))}`);
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      const sawSentAll = bytesTexts.some((t) => /数据已发送|awaiting confirmation/i.test(t));
      record('o-throttled-bytes', true, {
        midPct: mid, distinctPct: distinct.length, putBytes,
        parts: cos.st.puts.length, preflights: cos.st.options,
        sawSentAll, indeterminateDuringServer,
        stages: [...new Set(stageSeen.map((s) => s.slice(0, 10)))],
      });
    } catch (e) {
      record('o-throttled-bytes', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
      cos.server.close();
    }
  }

  const all = [
    ['a-bf', () => scenarioHappy('bf', bf)],
    ['a-fl', () => scenarioHappy('fl', fl)],
    ['b-401-continue', scenario401],
    ['c-refresh-continue', scenarioRefresh],
    ['d-repeated-clicks', scenarioRepeatClicks],
    ['e-lost-complete', scenarioLostComplete],
    ['f-oversize', () => scenarioDisabled('f-oversize')],
    ['g-format-unsupported', () => scenarioDisabled('g-format-unsupported')],
    ['h-cancel-terminal', scenarioCancelTerminal],
    ['i-network-capture', scenarioNetwork],
    ['j-real-session-loss', scenarioRealSessionLoss],
    ['k-cross-tab-abandon', scenarioCrossTab],
    ['l-record-write-failure', scenarioPersistFailure],
    ['m1-cancel-during-poll', () => scenarioCancelDuringWait('waiting')],
    ['m2-cancel-during-backoff', () => scenarioCancelDuringWait('backoff')],
    ['n-classic-upload', scenarioClassicUpload],
    ['o-throttled-bytes', scenarioThrottledBytes],
  ];
  for (const [id, fn] of all) {
    if (ONLY && id !== ONLY) continue;
    await fn();
  }

  fs.mkdirSync(path.join(L.GATE, 'e2e'), { recursive: true });
  fs.writeFileSync(path.join(L.GATE, 'e2e', 'results.json'), JSON.stringify(results, null, 2));
  const failed = Object.values(results).filter((r) => !r.pass).length;
  console.log(failed ? `E2E FAILED: ${failed}` : 'E2E ALL PASS');
  if (server) server.kill('SIGTERM');
}

main().catch((e) => { console.error(e); process.exit(1); });
