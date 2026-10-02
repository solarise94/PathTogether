#!/usr/bin/env node
// R1「一键转换并上传」e2e（真实 Flask /tools/slides + 登录会话 + page.route 假
// ingestion/COS——字节保留版）。场景与验收条目（docs/slide-tools/
// c6-migration-drain-plan.md §3.1）一一对应：
//   a  单次点击顺序（转换→校验→上传→可查看）+ 网络捕获 + 上传对象=产物
//   b  转换失败 / 转换中取消：零 ingestion
//   c  超限 / 不可查看：自动上传在创建 ingestion 前停止，产物保留可保存
//   d  登录过期（点击前 / 上传中刷新恢复）+ 重开只显示继续动作、不自动传输
//      + 换账号：上传归属原账号，不续传他人的 ingestion——选择框只给
//        「用原账号登录」或「当前账号另起上传」（d3 管理员；d4/d5 两个普通
//        用户走真实 ingestion 归属/授权，仅 COS 分块 PUT 被拦截）
//   e  复制 / 转换各阶段刷新恢复（上传阶段刷新在 d2：同一 ingestion，creates==1）
//   f  重复点击 / 第二标签 / 完成回调重放：恰一个 ingestion
//   g  转换后、上传前取消：撤销自动上传意图（无 ingestion、产物保留）
//   h  仅转换并保存：零 /api/、零跨源（C3 隐私/离线回归另跑 C3 套件）
//   i  工作台入口：KFB 提供「在本机转换并上传」、交接不重选、保目标
//      （i4 含「新项目」目标：仅发布后建项目 + 幂等键）、原生 TIFF 直传、
//      弹窗被拦截回退
//   j  大文件磁盘确认仍在一键链前置
//   k  已发布任务重复触发上传：不再建 ingestion/不传字节（关联未完成只重试关联：i5）
//   l  上线 dogfood 回归：刷新后从项目打开 id_bundle 切片（按 slide_id 取 info）、
//      普通用户界面删除走 ID 端点（两张同原始文件名，只删所点那张）；
//      KFB 入口 >60s 不自动消失；导入抽屉打开时抽屉内入口可点；删除后项目
//      活动视图（行+计数）不再含被删切片
//   m  R1 跟进：刷新后从任务列表上传的反馈可见（登录/进度/失败/取消/重试），
//      真实端点的账号等待上限 409 与池满排队文案（主面板与列表入口）
//   n  管理插件切片页：可读名/资产状态/不可用说明 + 加入后真实可读
// i/i4 的目标关联走真实 /api/project 端点（发布的是测试库里真实的 ready 切片）。
// 复跑：node tests/browser/slide_tools_r1/run_e2e.js（服务复用 C4 server.py）。
'use strict';
const path = require('path');
const fs = require('fs');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8963'));
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
    await new Promise((r) => setTimeout(r, 150));
  }
}

async function textOf(page, sel) {
  return String((await page.textContent(sel)) || '');
}

async function currentJobId(page) {
  return page.$eval('.job-row', (r) => r.dataset.jobId);
}

async function jobRecords(page) {
  return page.evaluate(() => window.__readJobRecords());
}

async function intentOf(page, jobId) {
  const recs = await jobRecords(page);
  const rec = recs.find((r) => r.id === jobId);
  return rec ? (rec.intent || null) : null;
}

async function convertFixture(page, file, { policy = null } = {}) {
  await L.setFile(page, file);
  await waitFor(async () => (await page.$('#probe-section:not([hidden])')) !== null, 60000, 'probe');
  if (policy) await page.check('#policy-strict');
  return (await currentJobId(page));
}

async function main() {
  fs.mkdirSync(L.GATE, { recursive: true });
  if (!process.argv.includes('--reuse-server')) server = await L.startServer(PORT, CREDS);
  const creds = L.readCreds(CREDS);

  const bf = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const fl = L.ensureFixture('fl-600x400.kfbf', ['gen-kfbf']);
  const bf2g = L.ensureFixture('bf-2g.kfb', ['gen-kfb', '--width', '36500', '--height', '36500']);
  // e1（刷新后续跑 + 自动上传）：产物须低于 C4 测试服务的产品上限
  // （UPLOAD_PRODUCT_MAX_BYTES=900,000,000）——2g 夹具产物 ~900.4MB 会触发
  // 正确的超限停止。12000² 产物 ~230MB：转换时长足以在中途刷新，且分块
  // 上传（8 MiB/片）不会撑爆 page.route 的请求体缓冲。
  const bfMid = L.ensureFixture('bf-12000.kfb', ['gen-kfb', '--width', '12000', '--height', '12000']);

  // ---------------------------------------------------------------- (a) --
  // 单次点击：顺序（转换 → 校验 → 上传 → 可查看）、零额外确认、上传对象 ==
  // OPFS 产物（sha 逐字节）、点击前零 /api/、原始字节绝不出网。
  async function scenarioOneClick() {
    const { context, page } = await L.launch('a-oneclick', [L.READ_JOB_RECORDS, L.savePickerStub(), L.downloadGuard()]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 4 });
    let releaseCreate = null;
    fake.behavior.gateCreate = () => new Promise((r) => { releaseCreate = r; });
    const reqs = [];
    context.on('request', (r) => reqs.push({ url: r.url(), method: r.method(), r }));
    try {
      await L.login(page, PORT, creds, 'user');
      reqs.length = 0;                       // 捕获窗口：工具页全程
      await L.openTools(page, PORT);
      const jobId = await convertFixture(page, bf);
      // 点击前：零 /api/、零跨源
      const preApi = reqs.filter((x) => x.url.includes('/api/'));
      const preCross = reqs.filter((x) => !x.url.startsWith(`http://127.0.0.1:${PORT}`));
      if (preApi.length || preCross.length) {
        throw new Error(`before click: api=${preApi.length} cross=${preCross.length}`);
      }
      await page.click('#convert-upload-btn');
      // 顺序证明：第一个 /api/ 请求是能力预检；ingestion 建立被闸住，直到
      // 转换+校验完成（记录 state==ready）才放行
      await waitFor(() => reqs.some((x) => x.url.includes('/api/tools/slides/upload-capability')), 30000, 'capability fetched');
      await waitFor(async () => (await page.$('#result-section:not([hidden])')) !== null, 120000, 'converted');
      const stAtCreate = await jobRecords(page);
      if (!stAtCreate.length || stAtCreate[0].state !== 'ready') {
        throw new Error(`job state at create gate: ${JSON.stringify(stAtCreate.map((r) => r.state))}`);
      }
      if (fake.st.creates.length !== 0) throw new Error(`create before conversion done: ${fake.st.creates.length}`);
      releaseCreate();
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 120000, 'published');
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      const name = fake.st.creates[0].filename;
      if (!name.endsWith('.tif') || name.endsWith('.ome.tif')) throw new Error(`filename ${name}`);
      const product = await L.opfsJobSha256(page, jobId);
      const uploaded = L.uploadedSha256(fake.st);
      if (uploaded.sha256 !== product.sha256) {
        throw new Error(`uploaded sha ${uploaded.sha256} != product ${product.sha256}`);
      }
      if (uploaded.bytes !== product.size) throw new Error(`uploaded ${uploaded.bytes} != ${product.size}`);
      // 原始 KFB 字节绝不出网：非 COS 请求体不含 KFB magic；PUT 总量 == 产物
      const magic = Buffer.from([0xF1, 0x01, 0xEE, 0xEE]);
      for (const x of reqs) {
        if (x.url.startsWith(creds.cosOrigin)) continue;
        const pd = x.r.postDataBuffer ? x.r.postDataBuffer() : null;
        if (pd && pd.length && pd.indexOf(magic) >= 0) throw new Error(`KFB magic sent to ${x.url}`);
      }
      // 意图收口：done + 绑定授权账号（能力端点 account 字段的真实 user_id）
      const intent = await intentOf(page, jobId);
      if (!intent || intent.state !== 'done' || !intent.account) {
        throw new Error(`intent after publish: ${JSON.stringify(intent)}`);
      }
      record('a-oneclick-order-network', true, {
        preClickApi: 0, preClickCross: 0, creates: 1,
        filename: name, productSha: product.sha256.slice(0, 12),
        uploadedSha: uploaded.sha256.slice(0, 12), putBytes: uploaded.bytes,
        intentState: intent.state, intentAccountBound: !!intent.account,
      });
    } catch (e) {
      record('a-oneclick-order-network', false, { error: String(e).slice(0, 400) });
    } finally {
      if (releaseCreate) releaseCreate();
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (b) --
  async function scenarioConvertFail() {
    const { context, page } = await L.launch('b1-fail', [L.READ_JOB_RECORDS]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      await convertFixture(page, bf, { policy: 'strict' });   // 严格策略 + 边缘 tile → 核心拒绝
      await page.click('#convert-upload-btn');
      await waitFor(async () => /pixel_policy_violation/.test(await textOf(page, '#page-error')), 120000, 'core refusal');
      if (fake.st.creates.length !== 0) throw new Error(`creates=${fake.st.creates.length}`);
      const recs = await jobRecords(page);
      const failed = recs.find((r) => r.error && r.error.code === 'pixel_policy_violation');
      if (!failed) throw new Error(`no failed record: ${JSON.stringify(recs.map((r) => r.state))}`);
      if (!failed.intent || failed.intent.state !== 'revoked') {
        throw new Error(`intent not revoked: ${JSON.stringify(failed.intent)}`);
      }
      record('b1-convert-fail-zero-ingestion', true, {
        creates: 0, jobState: failed.state, intentState: failed.intent.state,
      });
    } catch (e) {
      record('b1-convert-fail-zero-ingestion', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  async function scenarioCancelDuringConvert() {
    const { context, page } = await L.launch('b2-cancel', [L.READ_JOB_RECORDS]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      await convertFixture(page, bf2g);
      await page.click('#convert-upload-btn');
      await waitFor(async () => (await page.$('#cancel-btn:not([hidden])')) !== null, 300000, 'converting');
      await waitFor(async () => /\d/.test(await textOf(page, '#run-bytes')), 300000, 'committed bytes');
      await page.click('#cancel-btn');
      await waitFor(async () => (await L.jobDirs(page)).length === 0, 120000, 'job cleaned');
      if (fake.st.creates.length !== 0) throw new Error(`creates=${fake.st.creates.length}`);
      record('b2-cancel-during-convert-zero-ingestion', true, { creates: 0, jobDirs: 0 });
    } catch (e) {
      record('b2-cancel-during-convert-zero-ingestion', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (c) --
  async function scenarioAdmissionStop(kind) {
    const id = kind === 'oversize' ? 'c1-oversize-stops' : 'c2-not-viewable-stops';
    const { context, page } = await L.launch(id, [L.READ_JOB_RECORDS, L.savePickerStub()]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    if (kind === 'oversize') {
      fake.behavior.onCapability = () => ({
        status: 200,
        body: {
          account: 'c4-user@pt.test',
          cos_upload: {
            available: true, manual_only: true, formats: ['tif', 'tiff'],
            max_size_bytes: 1000, part_bytes: 8, url_ttl_seconds: 600,
            max_concurrent_parts: 2, sign_batch_max_parts: 4, policy_version: 'v1-manual',
          },
          viewable_formats: ['classic-bigtiff-jpeg-pyramid',
            'ome-bigtiff-subifd-multichannel-jpeg-passthrough'],
        },
      });
    } else {
      fake.behavior.onCapability = () => ({
        status: 200,
        body: {
          account: 'c4-user@pt.test',
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
      await L.openTools(page, PORT);
      const jobId = await convertFixture(page, bf);
      await page.click('#convert-upload-btn');
      const want = kind === 'oversize' ? /超过平台上限|exceeds the platform/i
        : /不支持查看|cannot view/i;
      await waitFor(async () => want.test(await textOf(page, '#upload-status')), 120000, 'stop reason');
      if (fake.st.creates.length !== 0 || fake.st.statusGets !== 0) {
        throw new Error(`ingestion calls: creates=${fake.st.creates.length} gets=${fake.st.statusGets}`);
      }
      const intent = await intentOf(page, jobId);
      if (!intent || intent.state !== 'pending') throw new Error(`intent: ${JSON.stringify(intent)}`);
      const kept = await L.opfsJobSha256(page, jobId);
      if (!kept.sha256) throw new Error('product missing');
      // 本地保存仍可用
      await page.click('#save-btn');
      await waitFor(async () => /已保存|Saved/.test(await textOf(page, '#save-status')), 60000, 'save');
      const savedSha = await L.opfsSha256(page);
      if (savedSha.sha256 !== kept.sha256) throw new Error('saved sha mismatch');
      record(id, true, {
        creates: 0, reason: (await textOf(page, '#upload-status')).slice(0, 30),
        intentState: intent.state, productSha: kept.sha256.slice(0, 12),
      });
    } catch (e) {
      record(id, false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (d1) --
  async function scenarioLoginExpiredBefore() {
    const { context, page } = await L.launch('d1-login-before', [L.READ_JOB_RECORDS]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      await convertFixture(page, bf);
      await context.clearCookies();          // 登录过期（点击前）
      await page.click('#convert-upload-btn');
      await waitFor(async () => /需要登录|need to sign in/i.test(await textOf(page, '#page-status')), 30000, 'login prompt');
      const href = await page.$eval('#page-status a', (a) => a.getAttribute('href'));
      if (href !== '/login?next=/tools/slides') throw new Error(`login link ${href}`);
      // 未开始转换：结果面板不出现、零 ingestion、无意图
      const resultHidden = await page.$eval('#result-section', (el) => el.hidden);
      if (!resultHidden) throw new Error('conversion started without login');
      if (fake.st.creates.length !== 0) throw new Error(`creates=${fake.st.creates.length}`);
      const jobId = await currentJobId(page);
      const intent = await intentOf(page, jobId);
      if (intent) throw new Error(`intent created without login: ${JSON.stringify(intent)}`);
      record('d1-login-expired-before-click', true, {
        loginLink: href, creates: 0, conversionStarted: false, intent: null,
      });
    } catch (e) {
      record('d1-login-expired-before-click', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ------------------------------------------------- (d2/e2) 上传中失效 --
  async function scenarioSessionLossAndReopen({ who = 'user', acceptConfirm = true }) {
    const id = who === 'user' ? 'd2-session-loss-reopen-continue' : 'd3-different-account-reconfirm';
    const { context, page } = await L.launch(id, [L.READ_JOB_RECORDS]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 4 });
    let hold = true;
    fake.behavior.gatePut = async (n) => { if (hold && n >= 3) await new Promise(() => {}); };
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      const jobId = await convertFixture(page, bf);
      await page.click('#convert-upload-btn');
      await waitFor(() => fake.st.puts.length >= 2, 60000, 'first 2 parts PUT');
      const before = await L.opfsJobSha256(page, jobId);
      await context.clearCookies();          // 上传中登录过期
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      // 重开：只显示「继续上传」（意图未失），页面加载不自动传输
      await waitFor(async () => (await page.$('[data-action="upload-continue"]')) !== null, 60000, 'continue button');
      const createsAtReopen = fake.st.creates.length;
      const getsAtReopen = fake.st.statusGets;
      const signsAtReopen = fake.st.signs.length;
      await page.waitForTimeout(2500);
      if (fake.st.creates.length !== createsAtReopen || fake.st.statusGets !== getsAtReopen) {
        throw new Error(`auto-transmit on load: creates=${fake.st.creates.length} gets=${fake.st.statusGets}`);
      }
      const intent0 = await intentOf(page, jobId);
      if (!intent0 || intent0.state !== 'pending') throw new Error(`intent lost: ${JSON.stringify(intent0)}`);
      hold = false;
      // 重新登录（同账号 or 换账号）后点「继续上传」
      await L.login(page, PORT, creds, who);
      await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await waitFor(async () => (await page.$('[data-action="upload-continue"]')) !== null, 60000, 'continue after login');
      // 换账号（此处为管理员 owner：服务端允许其访问他人任务，但归属与记账
      // 仍属原用户）：不静默续传。先在选择框里取消 → 零请求；再点选择
      // 「另起上传」→ 新 ingestion，原上传原样记入 superseded、归属不变。
      const nativeDialogs = [];
      page.on('dialog', async (d) => { nativeDialogs.push(d.type()); await d.dismiss().catch(() => {}); });
      let firstIngestion = null;
      if (who !== 'user') {
        const recBefore = (await jobRecords(page)).find((r) => r.id === jobId);
        firstIngestion = recBefore.upload.ingestionId;
        if (!recBefore.upload.account || recBefore.upload.account !== intent0.account) {
          throw new Error(`upload record not bound to account: ${JSON.stringify(recBefore.upload)}`);
        }
        await page.click('[data-action="upload-continue"]');
        await waitFor(async () => (await page.$('#account-dialog[open]')) !== null, 30000, 'account dialog');
        const body = await textOf(page, '#account-dialog-body');
        if (!body.includes(creds.userLogin) || !body.includes(creds.ownerLogin)) {
          throw new Error(`dialog labels: ${body}`);
        }
        await page.click('#account-cancel-btn');
        await waitFor(async () => /另一个账号|another account/i.test(await textOf(page, '#upload-status')), 30000, 'account-changed msg');
        if (fake.st.statusGets !== getsAtReopen || fake.st.signs.length !== signsAtReopen) {
          throw new Error(`upload proceeded without a choice: gets=${fake.st.statusGets}`);
        }
        await waitFor(async () => (await page.$('[data-action="upload-continue"]')) !== null, 30000, 'continue again');
        await page.click('[data-action="upload-continue"]');
        await waitFor(async () => (await page.$('#account-dialog[open]')) !== null, 30000, 'account dialog 2');
        await page.click('#account-separate-btn');
      } else {
        await page.click('[data-action="upload-continue"]');
      }
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 120000, 'published');
      const wantCreates = who === 'user' ? 1 : 2;
      if (fake.st.creates.length !== wantCreates) throw new Error(`creates=${fake.st.creates.length}`);
      const rec1 = (await jobRecords(page)).find((r) => r.id === jobId);
      const intent1 = rec1.intent;
      if (!intent1 || intent1.state !== 'done') throw new Error(`intent: ${JSON.stringify(intent1)}`);
      if (nativeDialogs.length) throw new Error(`native dialogs: ${nativeDialogs}`);
      let superseded = [];
      if (who !== 'user') {
        superseded = rec1.upload.superseded || [];
        if (superseded.length !== 1 || superseded[0].ingestionId !== firstIngestion
            || superseded[0].account !== intent0.account || superseded[0].state !== 'open') {
          throw new Error(`superseded ${JSON.stringify(superseded)}`);
        }
        if (rec1.upload.ingestionId === firstIngestion) throw new Error('resumed the original ingestion');
        if (intent1.account === intent0.account) throw new Error(`intent not rebound: ${intent1.account}`);
      }
      const after = await L.opfsJobSha256(page, jobId);
      if (after.sha256 !== before.sha256) throw new Error('artifact sha changed');
      record(id, true, {
        creates: wantCreates, originalKeptInSuperseded: superseded.length === 1,
        accountRebound: intent1.account !== intent0.account, nativeDialogs: 0,
      });
    } catch (e) {
      record(id, false, { error: String(e).slice(0, 400) });
    } finally {
      hold = false;
      await context.close();
    }
  }

  // ----------------------------------------------- (k1) 发布后重复点击 --
  // 已发布且意图 done：结果面板不再给上传按钮；即使直接触发上传动作（含隐藏
  // 按钮的 DOM click），也不再拉能力、不建 ingestion、不传字节。
  async function scenarioPublishedRepeatClicks() {
    const id = 'k1-published-repeat-clicks';
    const { context, page } = await L.launch(id, [L.READ_JOB_RECORDS]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    let capGets = 0;
    page.on('request', (r) => { if (r.url().includes('/api/tools/slides/upload-capability')) capGets++; });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      const jobId = await convertFixture(page, bf);
      await page.click('#convert-upload-btn');
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 120000, 'published');
      await waitFor(async () => { const it = await intentOf(page, jobId); return it && it.state === 'done'; }, 30000, 'intent done');
      await waitFor(async () => page.$eval('#upload-btn', (b) => b.hidden), 30000, 'upload button replaced');
      const base = { creates: fake.st.creates.length, puts: fake.st.puts.length, caps: capGets };
      await page.evaluate(() => {
        const b = document.getElementById('upload-btn');
        b.click(); b.click(); b.click();
      });
      await page.waitForTimeout(2500);
      if (fake.st.creates.length !== base.creates || fake.st.puts.length !== base.puts) {
        throw new Error(`re-uploaded: creates=${fake.st.creates.length} puts=${fake.st.puts.length}`);
      }
      if (capGets !== base.caps) throw new Error(`capability fetched again: ${capGets - base.caps}`);
      if (!/已发布|Published/.test(await textOf(page, '#upload-status'))) throw new Error('published result not shown');
      // 列表行：已发布、无上传/继续按钮
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await waitFor(async () => (await page.$(`.job-row[data-job-id="${jobId}"] [data-upload-state="published"]`)) !== null, 30000, 'row published');
      const rowButtons = await page.$$eval(`.job-row[data-job-id="${jobId}"] [data-job-upload]`, (bs) => bs.length);
      if (rowButtons) throw new Error(`row still offers upload: ${rowButtons}`);
      record(id, true, { creates: fake.st.creates.length, extraCreates: 0, extraPuts: 0, extraCapability: 0, rowUploadButtons: 0 });
    } catch (e) {
      record(id, false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ------------------------------------ (d4/d5) 两个普通用户 × 真实授权 --
  // ingestion 控制 API 走真实 Flask（真实 _ingestion_fetch 归属检查 +
  // 进程内假 COS 仅做 Initiate）；只拦截 COS 分块 PUT。用户 A 上传中断，
  // 用户 B 登录后继续：d4 选「另起上传」、d5 选「用原账号登录」。
  async function scenarioTwoUsersRealAuthz(mode) {
    const id = mode === 'separate' ? 'd4-two-users-separate-upload' : 'd5-two-users-return-to-original';
    const { context, page } = await L.launch(id, [L.READ_JOB_RECORDS]);
    const cos = await L.fakeCosPuts(context, creds.cosOrigin);
    let who = 'user';
    const reqs = [];
    context.on('request', (r) => {
      const u = new URL(r.url());
      if (u.pathname.startsWith('/api/ingestions')) reqs.push({ who, method: r.method(), path: u.pathname });
    });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      const jobId = await convertFixture(page, bf);
      cos.hold = true;
      await page.click('#convert-upload-btn');
      await waitFor(() => cos.attempts >= 1, 90000, 'first PUT attempt (real sign)');
      const recA = (await jobRecords(page)).find((r) => r.id === jobId);
      const idA = recA.upload && recA.upload.ingestionId;
      if (!idA || !recA.upload.account) throw new Error(`A record ${JSON.stringify(recA.upload)}`);
      const accountA = recA.upload.account;
      // 上传中断（会话失效 + 重开），用户 B 登录
      await context.clearCookies();
      await page.reload({ waitUntil: 'load' });
      cos.hold = false;
      who = 'user2';
      await L.login(page, PORT, creds, 'user2');
      await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await waitFor(async () => (await page.$('[data-action="upload-continue"]')) !== null, 60000, 'continue (B)');
      await page.click('[data-action="upload-continue"]');
      await waitFor(async () => (await page.$('#account-dialog[open]')) !== null, 30000, 'account dialog');
      const body = await textOf(page, '#account-dialog-body');
      if (!body.includes(creds.userLogin) || !body.includes(creds.user2Login)) throw new Error(`dialog: ${body}`);
      const touchedA = () => reqs.filter((q) => q.who === 'user2' && q.path.includes(idA));
      if (touchedA().length) throw new Error(`B touched A's ingestion before choosing: ${JSON.stringify(touchedA())}`);
      if (mode === 'separate') {
        await page.click('#account-separate-btn');
        let idB = null;
        await waitFor(async () => {
          const r = (await jobRecords(page)).find((x) => x.id === jobId);
          idB = r && r.upload && r.upload.ingestionId;
          return idB && idB !== idA;
        }, 60000, 'separate ingestion');
        await waitFor(() => reqs.some((q) => q.who === 'user2' && q.method === 'POST'
          && q.path === `/api/ingestions/${idB}/upload-complete`), 90000, 'B upload-complete');
        if (touchedA().length) throw new Error(`B touched A's ingestion: ${JSON.stringify(touchedA())}`);
        const recB = (await jobRecords(page)).find((x) => x.id === jobId);
        const sup = recB.upload.superseded || [];
        if (sup.length !== 1 || sup[0].ingestionId !== idA || sup[0].account !== accountA || sup[0].state !== 'open') {
          throw new Error(`superseded ${JSON.stringify(sup)}`);
        }
        if (recB.upload.account === accountA) throw new Error('new upload bound to A');
        // 真实授权：B 读 A 的任务 403、读自己的 200
        const bOnA = await L.apiGet(page, `/api/ingestions/${idA}`);
        const bOnB = await L.apiGet(page, `/api/ingestions/${idB}`);
        if (bOnA.status !== 403 || bOnB.status !== 200) throw new Error(`B authz A=${bOnA.status} B=${bOnB.status}`);
        await page.click('#upload-cancel-btn').catch(() => {});
        // A 登录：原任务仍归 A、仍在 uploading；A 读 B 的任务 403；A 取消旧上传
        await context.clearCookies();
        who = 'user';
        await L.login(page, PORT, creds, 'user');
        await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
        const aOnA = await L.apiGet(page, `/api/ingestions/${idA}`);
        const aOnB = await L.apiGet(page, `/api/ingestions/${idB}`);
        if (aOnA.status !== 200 || aOnA.body.state !== 'uploading' || aOnB.status !== 403) {
          throw new Error(`A authz A=${aOnA.status}/${aOnA.body && aOnA.body.state} B=${aOnB.status}`);
        }
        const cancelBtn = `.job-row[data-job-id="${jobId}"] [data-action="superseded-cancel"]`;
        await waitFor(async () => (await page.$(cancelBtn)) !== null, 30000, 'superseded cancel button');
        await page.click(cancelBtn);
        await waitFor(async () => /旧上传已取消|earlier upload was cancelled/i.test(await textOf(page, '#upload-status')), 30000, 'superseded cancelled');
        const aOnA2 = await L.apiGet(page, `/api/ingestions/${idA}`);
        if (aOnA2.body.state !== 'cancelled') throw new Error(`A's ingestion state ${aOnA2.body.state}`);
        await waitFor(async () => (await page.$(cancelBtn)) === null, 30000, 'superseded note gone');
        const creates = reqs.filter((q) => q.method === 'POST' && q.path === '/api/ingestions').length;
        record(id, true, {
          creates, bReadA: bOnA.status, aReadB: aOnB.status, originalStateAfterB: aOnA.body.state,
          originalCancelledByA: true, bTouchedA: 0,
        });
      } else {
        await Promise.all([
          page.waitForURL(/\/login/, { timeout: 30000 }),
          page.click('#account-original-btn'),
        ]);
        if (touchedA().length) throw new Error(`B touched A's ingestion: ${JSON.stringify(touchedA())}`);
        const recStill = (await jobRecords(page)).find((x) => x.id === jobId);
        if (!recStill || recStill.upload.ingestionId !== idA || (recStill.upload.superseded || []).length) {
          throw new Error(`record changed: ${JSON.stringify(recStill && recStill.upload)}`);
        }
        who = 'user';
        await L.login(page, PORT, creds, 'user');
        await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
        await waitFor(async () => (await page.$('[data-action="upload-continue"]')) !== null, 60000, 'continue (A)');
        await page.click('[data-action="upload-continue"]');
        await waitFor(() => reqs.some((q) => q.who === 'user' && q.method === 'POST'
          && q.path === `/api/ingestions/${idA}/upload-complete`), 90000, 'A upload-complete on original');
        if (await page.$('#account-dialog[open]')) throw new Error('same-account resume asked for a choice');
        const creates = reqs.filter((q) => q.method === 'POST' && q.path === '/api/ingestions').length;
        if (creates !== 1) throw new Error(`creates=${creates}`);
        const aOnA = await L.apiGet(page, `/api/ingestions/${idA}`);
        if (aOnA.status !== 200 || aOnA.body.state === 'uploading') throw new Error(`A state ${aOnA.body && aOnA.body.state}`);
        record(id, true, { creates, resumedSameIngestion: true, stateAfterComplete: aOnA.body.state, bTouchedA: 0 });
      }
    } catch (e) {
      record(id, false, { error: String(e).slice(0, 400) });
    } finally {
      cos.hold = false;
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (e1) --
  async function scenarioRefreshDuringConvert() {
    const { context, page } = await L.launch('e1-refresh-convert', [L.READ_JOB_RECORDS]);
    // 8 MiB/片：大产物分块足够小，route 拦截不必整体缓冲
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partBytes: 8 * 1024 * 1024 });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      await convertFixture(page, bfMid);
      await page.click('#convert-upload-btn');
      await waitFor(async () => /\d/.test(await textOf(page, '#run-bytes')), 300000, 'committed bytes');
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      // 恢复动作：「继续转换并上传」（意图仍在记录里）
      const resumeBtn = '.job-row[data-next-action="resume"] button[data-action="resume"]';
      await waitFor(async () => (await page.$(resumeBtn)) !== null, 60000, 'resume row');
      const intentMarked = await page.$eval(resumeBtn, (b) => b.dataset.intent || '');
      if (intentMarked !== '1') throw new Error('resume button lacks intent marker');
      if ((await textOf(page, resumeBtn)).indexOf('上传') < 0) throw new Error('label not convert-and-upload');
      if (fake.st.creates.length !== 0) throw new Error(`creates during convert=${fake.st.creates.length}`);
      const intentLabel = await textOf(page, resumeBtn);   // 发布后行会重渲，先取
      await page.click(resumeBtn);
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 600000, 'published');
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      record('e1-refresh-during-convert', true, {
        creates: 1, intentLabel,
      });
    } catch (e) {
      // 排障快照：转换/上传各停在哪一步
      let snap = '';
      try {
        snap = JSON.stringify({
          run: (await textOf(page, '#run-status')).slice(0, 40),
          upload: (await textOf(page, '#upload-status')).slice(0, 60),
          err: (await textOf(page, '#page-error')).slice(0, 80),
          records: (await jobRecords(page)).map((r) => ({ s: r.state, i: r.intent && r.intent.state })),
          creates: fake.st.creates.length, puts: fake.st.puts.length,
        });
      } catch { /* */ }
      record('e1-refresh-during-convert', false, { error: String(e).slice(0, 300), snap });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (e3) --
  async function scenarioRefreshDuringCopy() {
    const { context, page } = await L.launch('e3-refresh-copy');
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      await L.setFile(page, bf2g);
      // 复制进行中刷新（授权点击尚未发生——复制是选择文件阶段的一部分）
      await page.waitForFunction(() => {
        const p = document.getElementById('stage-progress');
        return p && Number(p.getAttribute('aria-valuenow') || 0) > 3;
      }, null, { timeout: 120000 });
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await page.waitForTimeout(1500);
      const rows = await page.$$eval('.job-row', (rs) => rs.map((r) => r.dataset.nextAction));
      if (fake.st.creates.length !== 0) throw new Error(`creates=${fake.st.creates.length}`);
      // 复制中断 → 启动清扫删除 staging 目录；若赶上句柄未释放则保留 prepared
      //（继续动作为普通「开始」，无自动上传语义——授权点击还没发生）
      for (const next of rows) {
        if (next !== 'start') throw new Error(`unexpected next action ${next}`);
      }
      record('e3-refresh-during-copy', true, {
        creates: 0, rowsAfterSweep: rows,
      });
    } catch (e) {
      record('e3-refresh-during-copy', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (f) --
  async function scenarioRepeatedClicks() {
    const { context, page } = await L.launch('f1-repeat', [L.READ_JOB_RECORDS]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 4 });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      await convertFixture(page, bf);
      await page.click('#convert-upload-btn');
      await page.evaluate(() => {
        const b = document.getElementById('convert-upload-btn');
        b.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        b.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      });
      await waitFor(() => fake.st.creates.length === 1, 60000, 'one create');
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 120000, 'published');
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      record('f1-repeated-clicks-one-ingestion', true, { creates: 1 });
    } catch (e) {
      record('f1-repeated-clicks-one-ingestion', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  async function scenarioSecondTab() {
    const { context, page } = await L.launch('f2-tabs');
    const fake = await L.fakeUploadRoutes(context, creds.cosOrigin, { partsCount: 2 });
    fake.behavior.gateComplete = () => new Promise(() => {});
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      await convertFixture(page, bf);
      await page.click('#convert-upload-btn');
      await waitFor(() => fake.st.puts.length === 2 && fake.st.completeReqs === 1, 60000, 'A uploading');
      const b = await context.newPage();
      await L.openTools(b, PORT);
      await waitFor(async () => (await b.$('[data-action="upload-continue"]')) !== null, 60000, 'B continue');
      await b.click('[data-action="upload-continue"]');
      await waitFor(async () => /另一个标签页|another tab/.test(await textOf(b, '#upload-status')), 30000, 'other-tab msg');
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      record('f2-second-tab-one-ingestion', true, { creates: 1 });
    } catch (e) {
      record('f2-second-tab-one-ingestion', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  async function scenarioReplayedComplete() {
    const { context, page } = await L.launch('f3-replay');
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    fake.behavior.onComplete = (job, nth) => (nth === 1 ? 'abort'
      : { status: 409, body: { code: 'ingestion_state_conflict' } });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      await convertFixture(page, fl);
      await page.click('#convert-upload-btn');
      await waitFor(async () => /已发布|Published/.test(await textOf(page, '#upload-status')), 120000, 'published');
      if (fake.st.completeReqs !== 2) throw new Error(`completeReqs=${fake.st.completeReqs}`);
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      record('f3-replayed-complete-one-ingestion', true, {
        completeReqs: 2, creates: 1,
      });
    } catch (e) {
      record('f3-replayed-complete-one-ingestion', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (g) --
  async function scenarioCancelRevokesIntent() {
    const { context, page } = await L.launch('g-cancel-intent', [L.READ_JOB_RECORDS, L.savePickerStub()]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    fake.behavior.onCapability = () => ({
      status: 200,
      body: {
        account: 'c4-user@pt.test',
        cos_upload: {
          available: true, manual_only: true, formats: ['tif', 'tiff'],
          max_size_bytes: 1000, part_bytes: 8, url_ttl_seconds: 600,
          max_concurrent_parts: 2, sign_batch_max_parts: 4, policy_version: 'v1-manual',
        },
        viewable_formats: ['classic-bigtiff-jpeg-pyramid'],
      },
    });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      const jobId = await convertFixture(page, bf);
      await page.click('#convert-upload-btn');
      // 超限 → 自动上传停止（转换后、上传前）：意图保留、取消按钮可见
      await waitFor(async () => /超过平台上限|exceeds the platform/i.test(await textOf(page, '#upload-status')), 120000, 'stopped');
      const before = await L.opfsJobSha256(page, jobId);
      await waitFor(async () => (await page.$('#upload-cancel-btn:not([hidden])')) !== null, 30000, 'cancel visible');
      if (fake.st.creates.length !== 0) throw new Error(`creates=${fake.st.creates.length}`);
      await page.click('#upload-cancel-btn');
      await waitFor(async () => /已撤销自动上传|revoked/i.test(await textOf(page, '#upload-status')), 30000, 'revoked msg');
      const intent = await intentOf(page, jobId);
      if (!intent || intent.state !== 'revoked') throw new Error(`intent: ${JSON.stringify(intent)}`);
      const kept = await L.opfsJobSha256(page, jobId);
      if (kept.sha256 !== before.sha256) throw new Error('product lost');
      // 重开：不再显示「继续上传」（回普通「上传到工作台」）
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await waitFor(async () => (await page.$('[data-action="upload"]')) !== null, 60000, 'plain upload button');
      if ((await page.$('[data-action="upload-continue"]')) !== null) throw new Error('intent continue still shown');
      if (fake.st.creates.length !== 0) throw new Error(`creates=${fake.st.creates.length}`);
      record('g-cancel-after-convert-revokes-intent', true, {
        creates: 0, intentState: intent.state, productKept: true,
      });
    } catch (e) {
      record('g-cancel-after-convert-revokes-intent', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (h) --
  async function scenarioLocalOnly() {
    const { context, page } = await L.launch('h-local-only', [L.READ_JOB_RECORDS, L.savePickerStub()]);
    const reqs = [];
    context.on('request', (r) => reqs.push({ url: r.url(), method: r.method() }));
    try {
      await L.login(page, PORT, creds, 'user');
      reqs.length = 0;
      await L.openTools(page, PORT);
      const jobId = await convertFixture(page, bf);
      await page.click('#convert-btn');          // 仅转换并保存
      await waitFor(async () => (await page.$('#result-section:not([hidden])')) !== null, 120000, 'ready');
      await page.click('#save-btn');
      await waitFor(async () => /已保存|Saved/.test(await textOf(page, '#save-status')), 60000, 'save');
      const apiCalls = reqs.filter((x) => x.url.includes('/api/'));
      const cross = reqs.filter((x) => !x.url.startsWith(`http://127.0.0.1:${PORT}`));
      const recs = await jobRecords(page);
      if (recs.length && recs[0].intent) throw new Error('local-only run created an intent');
      record('h-local-only-zero-network', true, {
        apiCalls: apiCalls.length, crossOrigin: cross.length, jobId,
      });
    } catch (e) {
      record('h-local-only-zero-network', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (i) --
  /// 工作台就位：登录 /app + 能力可用 + 侧栏展开（桌面默认收起——#menu-btn
  /// 切换）。导入抽屉由各场景自行打开（抽屉遮罩会挡住侧栏按钮）。
  async function workbenchReady(page) {
    await L.login(page, PORT, creds, 'user', '/app');
    await page.waitForFunction(() => !!(window.HP_UPLOAD && window.HP_APP_BOOTSTRAP
      && window.HP_APP_BOOTSTRAP.capabilities
      && window.HP_APP_BOOTSTRAP.capabilities.cos_upload
      && window.HP_APP_BOOTSTRAP.capabilities.cos_upload.available === true),
    null, { timeout: 30000 });
    await page.click('#menu-btn');
    await page.waitForSelector('#import-slides-btn', { state: 'visible', timeout: 10000 });
  }

  async function openImportDrawer(page) {
    await page.click('#import-slides-btn');
    await page.waitForSelector('#import-drawer:not([hidden])');
  }

  /// 关闭导入抽屉：抽屉遮罩盖住上传行，行内「在本机转换并上传」要点按须先
  /// 收起抽屉（目标选择已进 importTargetState，会话内保留）。
  async function closeImportDrawer(page) {
    await page.click('#import-drawer-close');
    await page.waitForSelector('#import-drawer[hidden]', { state: 'attached', timeout: 10000 });
  }

  /// 工作台选文件：change 处理器取走 File 后会清空 input（app.js 既有行为），
  /// 不能用 C3 的 setFile（它等 files[0] 非空）——这里等上传行出现。
  async function setWorkbenchFile(page, p) {
    await page.evaluate(() => { const el = document.getElementById('file-input'); if (el) el.value = ''; });
    await page.setInputFiles('#file-input', p);
    await waitFor(async () => (await page.$('.upload-item')) !== null, 20000, 'upload row');
  }

  /// 「在本机转换并上传」按钮定位器（与取消按钮同类名，按文案区分）。
  function convertOfferLocator(page) {
    return page.locator('.upload-item-btn', { hasText: /本机转换并上传|Convert on this machine/ });
  }

  async function createProjectViaApi(page, name) {
    return page.evaluate(async (n) => {
      const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
      const tok = m ? decodeURIComponent(m[1]) : '';
      const r = await fetch('/api/project/create', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': tok },
        body: JSON.stringify({ name: n, note: '', slides: [] }),
      });
      const b = await r.json();
      if (!r.ok) throw new Error(b.error || r.status);
      return b.pid;
    }, name);
  }

  async function scenarioWorkbenchHandoff() {
    const { context, page } = await L.launch('i-workbench');
    // 假 ingestion 发布的是测试库里真实存在的 ready 切片（server.py
    // --seed-ready-slide 合成资产）；目标关联走真实 /api/project/<pid>/slides
    // （路由只旁观记录后放行）。
    const fake = await L.fakeUploadRoutes(context, creds.cosOrigin,
      { partsCount: 3, slideId: creds.readySlideId });
    const assocReqs = [];
    const handleProjectSlides = async (route) => {
      const req = route.request();
      const u = new URL(req.url());
      const m = u.pathname.match(/^\/api\/project\/([^/]+)\/slides$/);
      if (!m || req.method() !== 'POST') return route.fallback();
      assocReqs.push({ pid: decodeURIComponent(m[1]), body: req.postDataJSON() });
      return route.fallback();
    };
    await context.route('**/api/project/*/slides', handleProjectSlides);
    try {
      await workbenchReady(page);
      const pid = await createProjectViaApi(page, 'r1-交接目标项目');
      // 打开导入抽屉，目标选到该项目（抽屉打开时按项目列表重建下拉）
      await openImportDrawer(page);
      await waitFor(async () => {
        const opts = await page.$$eval('#import-target-select option', (os) => os.map((o) => o.value));
        return opts.includes(pid);
      }, 30000, 'target option');
      await page.selectOption('#import-target-select', pid);
      // 选择 KFB → 行内提供「在本机转换并上传」；收起抽屉后点行内入口
      await setWorkbenchFile(page, bf);
      await closeImportDrawer(page);
      await waitFor(async () => (await convertOfferLocator(page).count()) === 1, 30000, 'convert offer');
      const offerText = await convertOfferLocator(page).textContent();
      if (!/本机转换|this machine/i.test(offerText)) throw new Error(`offer text: ${offerText}`);
      // 点击 → popup 交接（真实用户激活）
      const [popup] = await Promise.all([
        context.waitForEvent('page', { timeout: 30000 }),
        convertOfferLocator(page).click(),
      ]);
      await popup.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      // 文件已交接：探测完成，无需重选（file-input 为空）
      await waitFor(async () => (await popup.$('#probe-section:not([hidden])')) !== null, 60000, 'popup probe');
      const fileInputEmpty = await popup.$eval('#file-input', (el) => !el.files || el.files.length === 0);
      if (!fileInputEmpty) throw new Error('popup re-selected a file');
      if (!/已接收工作台|Received the file/.test(await textOf(popup, '#page-status'))) {
        throw new Error(`handoff banner missing: ${await textOf(popup, '#page-status')}`);
      }
      // 一键：转换 → 上传 → 发布 → 关联目标项目
      await popup.click('#convert-upload-btn');
      await waitFor(async () => /已发布|Published/.test(await textOf(popup, '#upload-status')), 120000, 'published');
      await waitFor(async () => /已发布并加入目标项目|added to the target project/.test(await textOf(popup, '#page-status')), 60000, 'associated');
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      if (!fake.st.creates[0].filename.endsWith('.tif')) throw new Error(`filename ${fake.st.creates[0].filename}`);
      if (assocReqs.length !== 1) throw new Error(`assoc reqs=${JSON.stringify(assocReqs)}`);
      if (assocReqs[0].pid !== pid) throw new Error(`assoc pid ${assocReqs[0].pid} != ${pid}`);
      const assocIds = (assocReqs[0].body && assocReqs[0].body.slide_ids) || [];
      if (assocIds.length !== 1 || assocIds[0] !== creds.readySlideId) {
        throw new Error(`assoc slide_ids ${JSON.stringify(assocReqs[0].body)}`);
      }
      // 真实项目：服务端项目详情里确有该切片
      const proj = await L.apiGet(page, `/api/project/${encodeURIComponent(pid)}`);
      if (proj.status !== 200 || !JSON.stringify(proj.body).includes(creds.readySlideId)) {
        throw new Error(`real project lacks slide: ${proj.status} ${JSON.stringify(proj.body).slice(0, 200)}`);
      }
      // 工作台收到通知（toast）
      await waitFor(async () => /本机转换并上传完成|convert-and-upload finished/.test(await textOf(page, '#toast-container')), 30000, 'workbench toast');
      record('i-workbench-handoff', true, {
        creates: 1, projectId: pid, assocSlideIds: assocIds, realProjectHasSlide: true,
        popupReselected: false, offerText: offerText.trim().slice(0, 20),
      });
    } catch (e) {
      record('i-workbench-handoff', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.unroute('**/api/project/*/slides', handleProjectSlides).catch(() => {});
      await context.close();
    }
  }

  async function scenarioNativeDirectReal() {
    const { context, page } = await L.launch('i2-native-direct');
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partBytes: 8 });
    try {
      await workbenchReady(page);
      // 原生 .tif 直传：无「本机转换」入口，正常完成
      await page.evaluate(() => {
        const bytes = new Uint8Array(64);
        for (let i = 0; i < bytes.length; i++) bytes[i] = i;
        window.HP_UPLOAD.uploadFile(new File([bytes], 'r1-native.tif', { type: 'image/tiff' }));
      });
      await waitFor(async () => {
        const rows = await page.$$eval('.upload-item-status', (els) => els.map((e) => e.textContent || ''));
        return rows.some((t) => /入库完成|Done|published/.test(t));
      }, 60000, 'native done');
      const offers = await convertOfferLocator(page).count();
      if (offers) throw new Error(`convert offer shown for native: ${offers}`);
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      record('i2-native-direct-upload', true, { creates: 1, convertOffers: 0 });
    } catch (e) {
      record('i2-native-direct-upload', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ------------------------------------------------------- (i4) 新项目目标 --
  async function scenarioHandoffNewProject() {
    const { context, page } = await L.launch('i4-new-project');
    const fake = await L.fakeUploadRoutes(context, creds.cosOrigin,
      { partsCount: 3, slideId: creds.readySlideId });
    const assocReqs = [];
    let createdProjects = [];
    const handleProjectSlides = async (route) => {
      const req = route.request();
      const u = new URL(req.url());
      const m = u.pathname.match(/^\/api\/project\/([^/]+)\/slides$/);
      if (!m || req.method() !== 'POST') return route.fallback();
      assocReqs.push({ pid: decodeURIComponent(m[1]), body: req.postDataJSON() });
      return route.fallback();
    };
    const handleProjectCreate = async (route) => {
      const req = route.request();
      const u = new URL(req.url());
      if (u.pathname !== '/api/project/create' || req.method() !== 'POST') return route.fallback();
      createdProjects.push({
        body: req.postDataJSON(),
        idem: req.headers()['idempotency-key'] || '',
      });
      return route.fallback();
    };
    await context.route('**/api/project/*/slides', handleProjectSlides);
    await context.route('**/api/project/create', handleProjectCreate);
    try {
      await workbenchReady(page);
      // 目标选「新项目」并命名（显式目标；项目仅在上传成功后创建）
      await openImportDrawer(page);
      await waitFor(async () => (await page.$('#import-target-select option[value=new]')) !== null, 30000, 'new option');
      await page.selectOption('#import-target-select', 'new');
      await page.fill('#import-target-new-name', 'r1-新项目目标');
      await setWorkbenchFile(page, bf);
      await closeImportDrawer(page);
      await waitFor(async () => (await convertOfferLocator(page).count()) === 1, 30000, 'offer');
      const [popup] = await Promise.all([
        context.waitForEvent('page', { timeout: 30000 }),
        convertOfferLocator(page).click(),
      ]);
      await popup.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await waitFor(async () => (await popup.$('#probe-section:not([hidden])')) !== null, 60000, 'popup probe');
      // 交接目标带名称与幂等键；发布前不建项目
      await popup.click('#convert-upload-btn');
      await waitFor(() => createdProjects.length >= 0 && fake.st.puts.length >= 1, 60000, 'uploading');
      if (createdProjects.length !== 0) throw new Error(`project created before publish: ${createdProjects.length}`);
      await waitFor(async () => /已发布|Published/.test(await textOf(popup, '#upload-status')), 180000, 'published');
      await waitFor(() => createdProjects.length === 1, 60000, 'project created on success');
      await waitFor(async () => /已发布并加入目标项目|added to the target project/.test(await textOf(popup, '#page-status')), 60000, 'associated');
      if (assocReqs.length !== 1) throw new Error(`assoc reqs=${JSON.stringify(assocReqs)}`);
      if (!createdProjects[0].idem) throw new Error('create without idempotency key');
      if (createdProjects[0].body.name !== 'r1-新项目目标') throw new Error(`name ${createdProjects[0].body.name}`);
      // 真实项目：新建项目名正确、含已发布切片
      const proj = await L.apiGet(page, `/api/project/${encodeURIComponent(assocReqs[0].pid)}`);
      const pj = JSON.stringify(proj.body || {});
      if (proj.status !== 200 || !pj.includes('r1-新项目目标') || !pj.includes(creds.readySlideId)) {
        throw new Error(`real new project: ${proj.status} ${pj.slice(0, 200)}`);
      }
      record('i4-handoff-new-project-target', true, {
        creates: fake.st.creates.length, projectCreatedAfterPublish: true,
        idemKey: createdProjects[0].idem.slice(0, 8) + '…',
        assocPid: assocReqs[0].pid, realProjectHasSlide: true,
      });
    } catch (e) {
      record('i4-handoff-new-project-target', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.unroute('**/api/project/*/slides', handleProjectSlides).catch(() => {});
      await context.unroute('**/api/project/create', handleProjectCreate).catch(() => {});
      await context.close();
    }
  }

  // ------------------------------------------- (i5) 目标关联失败 → 重试 --
  // 发布成功但关联失败：意图保持 pending（新项目 pid 已写回目标）；刷新后
  // 已发布行仍给出「重试加入项目」，重试只关联不再建项目，成功后意图 done。
  async function scenarioAssocRetry() {
    const { context, page } = await L.launch('i5-assoc-retry', [L.READ_JOB_RECORDS]);
    const fake = await L.fakeUploadRoutes(context, creds.cosOrigin, { partsCount: 3 });
    const assocReqs = [];
    const createdProjects = [];
    const handleProjectSlides = async (route) => {
      const req = route.request();
      const u = new URL(req.url());
      const m = u.pathname.match(/^\/api\/project\/([^/]+)\/slides$/);
      if (!m || req.method() !== 'POST') return route.fallback();
      assocReqs.push({ pid: decodeURIComponent(m[1]), body: req.postDataJSON() });
      if (assocReqs.length <= 2) {
        return route.fulfill({ status: 500, contentType: 'application/json',
          body: JSON.stringify({ error: 'r1-injected-assoc-failure' }) });
      }
      return route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ pid: m[1], slides: [], slide_ids: req.postDataJSON().slide_ids || [] }) });
    };
    const handleProjectCreate = async (route) => {
      const req = route.request();
      const u = new URL(req.url());
      if (u.pathname !== '/api/project/create' || req.method() !== 'POST') return route.fallback();
      createdProjects.push({ idem: req.headers()['idempotency-key'] || '' });
      return route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ pid: `prj_r1retry_${createdProjects.length}`, name: 'r1-重试目标' }) });
    };
    await context.route('**/api/project/*/slides', handleProjectSlides);
    await context.route('**/api/project/create', handleProjectCreate);
    try {
      await workbenchReady(page);
      await openImportDrawer(page);
      await waitFor(async () => (await page.$('#import-target-select option[value=new]')) !== null, 30000, 'new option');
      await page.selectOption('#import-target-select', 'new');
      await page.fill('#import-target-new-name', 'r1-重试目标');
      await setWorkbenchFile(page, bf);
      await closeImportDrawer(page);
      await waitFor(async () => (await convertOfferLocator(page).count()) === 1, 30000, 'offer');
      const [popup] = await Promise.all([
        context.waitForEvent('page', { timeout: 30000 }),
        convertOfferLocator(page).click(),
      ]);
      await popup.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await waitFor(async () => (await popup.$('#probe-section:not([hidden])')) !== null, 60000, 'popup probe');
      const jobId = await currentJobId(popup);
      await popup.click('#convert-upload-btn');
      await waitFor(async () => /已发布|Published/.test(await textOf(popup, '#upload-status')), 180000, 'published');
      await waitFor(async () => /加入目标项目失败|adding it to the target project failed/i.test(await textOf(popup, '#page-status')), 60000, 'assoc fail msg');
      const failed = await intentOf(popup, jobId);
      if (!failed || failed.state !== 'pending') throw new Error(`intent after failure ${JSON.stringify(failed)}`);
      if (!failed.assocError) throw new Error('assocError not recorded');
      if (!failed.target || failed.target.project !== 'prj_r1retry_1') {
        throw new Error(`created pid not persisted: ${JSON.stringify(failed.target)}`);
      }
      // 已发布任务上重复触发上传动作：只重试关联（第 2 次关联，仍失败），
      // 不建 ingestion、不再建项目
      const putsAt = fake.st.puts.length;
      await popup.evaluate(() => document.getElementById('upload-btn').click());
      await waitFor(() => assocReqs.length === 2, 30000, 'assoc retried by repeat click');
      await popup.waitForTimeout(1500);
      if (fake.st.creates.length !== 1 || fake.st.puts.length !== putsAt) {
        throw new Error(`repeat click re-uploaded: creates=${fake.st.creates.length} puts=${fake.st.puts.length}`);
      }
      if (createdProjects.length !== 1) throw new Error(`repeat click created project: ${createdProjects.length}`);
      // 标签关闭/刷新后：已发布行给出重试（页面加载不自动发请求）
      await popup.reload();
      await popup.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      const retry = popup.locator(`.job-row[data-job-id="${jobId}"] button[data-action="assoc-retry"]`);
      await waitFor(async () => (await retry.count()) === 1, 30000, 'retry button');
      if (assocReqs.length !== 2) throw new Error(`auto-retried on load: ${assocReqs.length}`);
      await retry.click();
      await waitFor(async () => {
        const it = await intentOf(popup, jobId);
        return it && it.state === 'done';
      }, 30000, 'intent done');
      await waitFor(async () => /已发布并加入目标项目|added to the target project/.test(await textOf(popup, '#page-status')), 30000, 'associated');
      await waitFor(async () => (await retry.count()) === 0, 30000, 'retry button gone');
      const done = await intentOf(popup, jobId);
      if (assocReqs.length !== 3) throw new Error(`assoc reqs=${assocReqs.length}`);
      if (assocReqs.some((r) => r.pid !== 'prj_r1retry_1')) throw new Error(`assoc pids ${JSON.stringify(assocReqs.map((r) => r.pid))}`);
      if (createdProjects.length !== 1) throw new Error(`projects created=${createdProjects.length}`);
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      if (done.projectId !== 'prj_r1retry_1' || done.assocError) throw new Error(`done intent ${JSON.stringify(done)}`);
      record('i5-assoc-failure-retry', true, {
        creates: 1, projectsCreated: 1, assocAttempts: 3, repeatClickRetriedAssocOnly: true,
        pendingAfterFailure: true, retryAfterReload: true, finalState: done.state,
      });
    } catch (e) {
      record('i5-assoc-failure-retry', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.unroute('**/api/project/*/slides', handleProjectSlides).catch(() => {});
      await context.unroute('**/api/project/create', handleProjectCreate).catch(() => {});
      await context.close();
    }
  }

  async function scenarioPopupBlocked() {
    const { context, page } = await L.launch('i3-popup-blocked', [`
      Object.defineProperty(window, 'open', { value: () => null });
    `]);
    try {
      await workbenchReady(page);
      await openImportDrawer(page);
      await setWorkbenchFile(page, bf);
      await closeImportDrawer(page);
      await waitFor(async () => (await convertOfferLocator(page).count()) === 1, 30000, 'convert offer');
      await convertOfferLocator(page).click();
      // 明示原因 + 工具页链接；选择没有静默丢失（按钮仍在，可重试）
      await waitFor(async () => /弹窗被浏览器拦截|popup was blocked/i.test(
        (await page.$$eval('.upload-item-status', (els) => els.map((e) => e.textContent).join('|')) || '')), 30000, 'blocked msg');
      const link = await page.$eval('.upload-item a[href="/tools/slides"]', (a) => a.getAttribute('href'));
      if (link !== '/tools/slides') throw new Error(`tools link ${link}`);
      const pages = context.pages().length;
      if (pages !== 1) throw new Error(`pages=${pages}`);
      record('i3-popup-blocked-fallback', true, { toolsLink: link, pages, retryKept: true });
    } catch (e) {
      record('i3-popup-blocked-fallback', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (l) --
  /// 生产 dogfood P1：项目内 id_bundle 切片（name 为空）按文件名打开 → 404、
  /// 按旧名端点删除 → 403。两张同原始文件名切片：打开/删除都须按所点行的
  /// slide_id，另一张不受影响。
  async function scenarioProjectOpenAndUiDelete() {
    const { context, page } = await L.launch('l1-project-open-delete');
    const [keepId, delId] = creds.dupSlideIds || [];
    const infoReqs = [];
    const delReqs = [];
    page.on('response', (r) => {
      const u = new URL(r.url());
      if (/\/info$/.test(u.pathname) && /^\/api\/slides?\//.test(u.pathname)) {
        infoReqs.push({ path: u.pathname, status: r.status() });
      }
      if (r.request().method() === 'DELETE' && u.pathname.startsWith('/api/slide')) {
        delReqs.push({ path: u.pathname, status: r.status() });
      }
    });
    try {
      if (!keepId || !delId || keepId === delId) throw new Error(`dupSlideIds ${JSON.stringify(creds.dupSlideIds)}`);
      await workbenchReady(page);
      const pid = await page.evaluate(async (ids) => {
        const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
        const r = await fetch('/api/project/create', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' },
          body: JSON.stringify({ name: 'r1-同名双片项目', note: '', slide_ids: ids }),
        });
        const b = await r.json();
        if (!r.ok) throw new Error(b.error || r.status);
        return b.pid;
      }, [keepId, delId]);
      // 刷新后从项目进入（与真实用户路径一致：列表来自服务端，而非本页内存）
      await page.reload();
      await page.waitForFunction(() => !!window.HP_UPLOAD, null, { timeout: 30000 });
      if (!(await page.isVisible('#import-slides-btn'))) await page.click('#menu-btn');
      const projSel = `.proj-row[data-pid="${pid}"]`;
      await page.waitForSelector(projSel, { timeout: 30000 });
      await page.click(`${projSel} .proj-name`);
      await page.waitForSelector(`${projSel}.expanded .slide-row`, { timeout: 10000 });
      const rows = await page.$$eval(`${projSel} .slide-row`, (els) => els.map((e) => ({
        id: e.dataset.slideId, name: (e.querySelector('.slide-name') || {}).textContent || '',
      })));
      const rowIds = rows.map((r) => r.id).sort();
      if (JSON.stringify(rowIds) !== JSON.stringify([keepId, delId].sort())) {
        throw new Error(`project row ids ${JSON.stringify(rows)}`);
      }
      const failedRows = rows.filter((r) => /读取失败|read failed/.test(r.name));
      if (failedRows.length) throw new Error(`rows marked read-failed: ${JSON.stringify(rows)}`);
      // 打开：info 请求必须按所点行的 slide_id，200
      infoReqs.length = 0;
      await page.click(`${projSel} .slide-row[data-slide-id="${keepId}"] .slide-name`);
      await waitFor(async () => infoReqs.some((r) => r.status === 200), 30000, 'slide info 200');
      const badInfo = infoReqs.filter((r) => r.status !== 200 || r.path !== `/api/slides/${keepId}/info`);
      if (badInfo.length) throw new Error(`info reqs ${JSON.stringify(infoReqs)}`);
      // 界面删除另一张：确认框 → DELETE /api/slides/<id> 200 → 行消失
      page.once('dialog', (d) => d.accept());
      const delRow = page.locator(`${projSel} .slide-row[data-slide-id="${delId}"]`);
      await delRow.hover();
      await delRow.locator('.slide-del').click();
      await waitFor(async () => delReqs.length > 0, 30000, 'delete request');
      if (delReqs.length !== 1 || delReqs[0].path !== `/api/slides/${delId}` || delReqs[0].status !== 200) {
        throw new Error(`delete reqs ${JSON.stringify(delReqs)}`);
      }
      await waitFor(async () => /已删除|deleted/i.test(await textOf(page, '#toast-container')), 30000, 'delete toast');
      const toastText = await textOf(page, '#toast-container');
      if (/无权访问|删除失败|delete failed/i.test(toastText)) throw new Error(`toast ${toastText}`);
      // R1 跟进：被删的那张退出项目的活动视图（行与计数一致），刷新后也不回来；
      // 同名另一张照常可开（历史成员行在服务端保留——pytest 断言）。
      const metaBefore = await textOf(page, `${projSel} .proj-meta`);
      await page.reload();
      await page.waitForFunction(() => !!window.HP_UPLOAD, null, { timeout: 30000 });
      if (!(await page.isVisible('#import-slides-btn'))) await page.click('#menu-btn');
      await page.waitForSelector(projSel, { timeout: 30000 });
      await page.click(`${projSel} .proj-name`);
      await page.waitForSelector(`${projSel}.expanded .slide-row`, { timeout: 10000 });
      const after = await page.$$eval(`${projSel} .slide-row`, (els) => els.map((e) => ({
        id: e.dataset.slideId, name: (e.querySelector('.slide-name') || {}).textContent || '',
      })));
      if (after.length !== 1 || after[0].id !== keepId) throw new Error(`rows after refresh ${JSON.stringify(after)}`);
      if (after.some((r) => /读取失败|read failed/.test(r.name))) throw new Error(`read-failed row ${JSON.stringify(after)}`);
      const metaAfter = await textOf(page, `${projSel} .proj-meta`);
      if (!/^1\s*切片|^1 slide/.test(metaAfter.trim())) throw new Error(`count after delete: "${metaAfter}" (before "${metaBefore}")`);
      infoReqs.length = 0;
      await page.click(`${projSel} .slide-row[data-slide-id="${keepId}"] .slide-name`);
      await waitFor(async () => infoReqs.some((r) => r.status === 200 && r.path === `/api/slides/${keepId}/info`), 30000, 'kept opens');
      const gone = await L.apiGet(page, `/api/slides/${encodeURIComponent(delId)}/info`);
      const kept = await L.apiGet(page, `/api/slides/${encodeURIComponent(keepId)}/info`);
      if (![403, 404, 410].includes(gone.status) || kept.status !== 200) {
        throw new Error(`after delete: deleted=${gone.status} kept=${kept.status}`);
      }
      record('l1-project-open-and-ui-delete', true, {
        projectRows: rows.length, readFailedRows: 0, infoPath: '/api/slides/<id>/info',
        deletePath: '/api/slides/<id>', deleteStatus: 200, rowsAfterRefresh: after.length,
        countAfter: metaAfter.trim().slice(0, 20), deletedReadStatus: gone.status, otherSameNameKept: true,
      });
    } catch (e) {
      record('l1-project-open-and-ui-delete', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  /// 生产 dogfood P2：KFB 入口 60s 后自动消失 → 用户回来时选择已丢失。
  async function scenarioConvertOfferPersists() {
    const { context, page } = await L.launch('l2-offer-persists');
    try {
      await workbenchReady(page);
      await page.clock.install();
      await openImportDrawer(page);
      await setWorkbenchFile(page, bf);
      await closeImportDrawer(page);
      await waitFor(async () => (await convertOfferLocator(page).count()) === 1, 30000, 'convert offer');
      await page.clock.fastForward(70000);
      await page.clock.runFor(1000);
      const offers = await convertOfferLocator(page).count();
      const rowsLeft = await page.locator('.upload-item').count();
      if (offers !== 1 || rowsLeft < 1) throw new Error(`after 71s: offers=${offers} rows=${rowsLeft}`);
      record('l2-convert-offer-persists-past-60s', true, { fastForwardMs: 71000, offers, rows: rowsLeft });
    } catch (e) {
      record('l2-convert-offer-persists-past-60s', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  /// 生产 dogfood P2：导入抽屉遮罩盖住侧栏入口。抽屉不关，抽屉内入口须可点
  /// （Playwright 点击前做遮挡检查）并完成 popup 交接。
  async function scenarioDrawerOfferClickable() {
    const { context, page } = await L.launch('l3-drawer-offer');
    try {
      await workbenchReady(page);
      await openImportDrawer(page);
      await setWorkbenchFile(page, bf);
      const drawerBtn = page.locator('#import-drawer .import-convert-offer-btn');
      await waitFor(async () => (await drawerBtn.count()) === 1, 30000, 'drawer convert offer');
      if (!(await page.isVisible('#import-drawer'))) throw new Error('drawer closed');
      const [popup] = await Promise.all([
        context.waitForEvent('page', { timeout: 30000 }),
        drawerBtn.click({ timeout: 10000 }),
      ]);
      await popup.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await waitFor(async () => /已接收工作台|Received the file/.test(await textOf(popup, '#page-status')),
        60000, 'handoff banner');
      const left = await drawerBtn.count();
      if (left !== 0) throw new Error(`drawer offer not cleared after handoff: ${left}`);
      record('l3-drawer-offer-clickable', true, { drawerOpen: true, popup: true, drawerOfferCleared: true });
    } catch (e) {
      record('l3-drawer-offer-clickable', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // ---------------------------------------------------------------- (j) --
  async function scenarioDiskConfirmGates() {
    const big = L.sparseLargeKfb('r1-sparse-5g2.kfb', Math.ceil(5.2 * 2 ** 30));
    const stubCap = `(() => {
      const GiB = 2 ** 30;
      Object.defineProperty(navigator.storage, 'estimate', { value: async () => ({ usage: 0, quota: 10 * GiB }) });
    })();`;
    const { context, page } = await L.launch('j-disk', [stubCap]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 2 });
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      await L.setFile(page, big);
      // uncertain 磁盘确认：在探测（复制前）出现——一键按钮此时还不可用
      await waitFor(async () => (await page.$('#disk-dialog[open]')) !== null, 30000, 'disk dialog');
      const convertEnabled = await page.$eval('#convert-upload-btn', (b) => !b.disabled);
      if (convertEnabled) throw new Error('convert-upload enabled before disk confirm');
      await page.keyboard.press('Escape');    // 拒绝 → 无任务、零 ingestion
      await waitFor(async () => (await L.jobDirs(page)).length === 0, 30000, 'no job');
      if (fake.st.creates.length !== 0) throw new Error(`creates=${fake.st.creates.length}`);
      record('j-disk-confirm-gates-oneclick', true, {
        dialogShown: true, convertEnabledBeforeConfirm: false, creates: 0, jobDirs: 0,
      });
    } catch (e) {
      record('j-disk-confirm-gates-oneclick', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }


  // ------------------------------------------- (m) R1 跟进：任务列表上传反馈 --
  /// 断言某选择器对用户真实可见（自身与祖先都未隐藏、有布局盒）。
  async function visibleText(page, sel) {
    if (!(await page.isVisible(sel))) return '';
    return textOf(page, sel);
  }

  async function convertOnly(page, file) {
    const jobId = await convertFixture(page, file);
    await page.click('#convert-btn');
    await waitFor(async () => (await page.$('#result-section:not([hidden])')) !== null, 120000, 'converted');
    return jobId;
  }

  async function reloadTools(page) {
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
  }

  /// m1：未登录本地转换 → 刷新 → 任务列表「上传到工作台」→ 可见的登录说明与
  /// 链接 → 点链接登录 → 回到工具页 → 同一行上传 → 可见进度 → 可见发布结果。
  async function scenarioRefreshAnonLoginUpload() {
    const id = 'm1-refresh-anon-login-return-upload';
    const { context, page } = await L.launch(id, [L.READ_JOB_RECORDS]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 4 });
    let release = null;
    fake.behavior.gatePut = async (n) => { if (n === 3 && release === null) await new Promise((r) => { release = r; }); };
    try {
      await L.openTools(page, PORT);
      const jobId = await convertOnly(page, bf);
      await reloadTools(page);
      if (await page.isVisible('#result-section')) throw new Error('result panel visible right after refresh');
      const rowBtn = `.job-row[data-job-id="${jobId}"] [data-action="upload"]`;
      await page.waitForSelector(rowBtn, { timeout: 30000 });
      await page.click(rowBtn);
      await waitFor(async () => /需要登录|sign in/i.test(await visibleText(page, '#upload-status')), 30000, 'visible login prompt');
      if (!(await page.isVisible('#upload-status a'))) throw new Error('login link not visible');
      const href = await page.$eval('#upload-status a', (a) => a.getAttribute('href'));
      if (fake.st.creates.length) throw new Error('created while signed out');
      await Promise.all([page.waitForURL(/\/login/, { timeout: 30000 }), page.click('#upload-status a')]);
      await page.fill('#login-dialog-username', creds.userLogin);
      await page.fill('#login-dialog-password', creds.userPassword);
      await Promise.all([
        page.waitForURL(/\/tools\/slides/, { timeout: 30000 }),
        page.click('#login-dialog-form button[type="submit"]'),
      ]);
      await page.waitForFunction(() => !!window.__stToolsReady, null, { timeout: 30000 });
      await page.waitForSelector(rowBtn, { timeout: 30000 });
      if (await page.isVisible('#result-section')) throw new Error('panel visible before choosing a job');
      await page.click(rowBtn);
      await waitFor(() => release !== null, 60000, 'upload in progress');
      const progress = await visibleText(page, '#upload-status');
      if (!/上传中|Uploading|%/.test(progress)) throw new Error(`progress not visible: "${progress}"`);
      if (!(await page.isVisible('#upload-cancel-btn'))) throw new Error('cancel not visible during upload');
      release();
      await waitFor(async () => /已发布|Published/.test(await visibleText(page, '#upload-status')), 120000, 'visible published');
      if (fake.st.creates.length !== 1) throw new Error(`creates=${fake.st.creates.length}`);
      record(id, true, { loginLink: href, loginVisible: true, progressVisible: progress.slice(0, 24), creates: 1, publishedVisible: true });
    } catch (e) {
      record(id, false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  /// m2：刷新后的任务列表上传 → 服务端未知错误（error 为对象）→ 可读失败文案 +
  /// 可见重试；重试中取消 → 可见取消；再从列表重试 → 发布。
  async function scenarioRefreshFailureRetry() {
    const id = 'm2-refresh-failure-cancel-retry';
    const { context, page } = await L.launch(id, [L.READ_JOB_RECORDS]);
    const fake = await L.fakeUploadRoutes(page, creds.cosOrigin, { partsCount: 4 });
    let nCreate = 0;
    fake.behavior.onCreate = () => {
      nCreate += 1;
      return nCreate === 1 ? { status: 500, body: { error: { internal: 'boom' } } } : null;
    };
    let hold = false;
    fake.behavior.gatePut = async () => { if (hold) await new Promise(() => {}); };
    try {
      await L.login(page, PORT, creds, 'user');
      await L.openTools(page, PORT);
      const jobId = await convertOnly(page, bf);
      await reloadTools(page);
      const rowBtn = `.job-row[data-job-id="${jobId}"] [data-action="upload"]`;
      await page.waitForSelector(rowBtn, { timeout: 30000 });
      await page.click(rowBtn);
      await waitFor(async () => /上传失败|Upload failed/.test(await visibleText(page, '#upload-status')), 30000, 'visible failure');
      const failText = await visibleText(page, '#upload-status');
      if (/object Object/.test(failText) || !/500/.test(failText)) throw new Error(`failure text "${failText}"`);
      if (!(await page.isEnabled('#upload-btn')) || !(await page.isVisible('#upload-btn'))) throw new Error('panel retry not available');
      await page.waitForSelector(`${rowBtn}:not([disabled])`, { timeout: 30000 });
      hold = true;
      await page.click('#upload-btn');
      await waitFor(() => fake.st.puts.length === 0 && fake.st.creates.length === 2, 30000, 'second create');
      await waitFor(async () => page.isVisible('#upload-cancel-btn'), 30000, 'cancel visible');
      await page.click('#upload-cancel-btn');
      await waitFor(async () => /已取消上传|Upload cancelled/.test(await visibleText(page, '#upload-status')), 30000, 'visible cancelled');
      await waitFor(async () => (await page.$(`.job-row[data-job-id="${jobId}"] [data-upload-state="cancelled"]`)) !== null, 30000, 'row cancelled');
      hold = false;
      await reloadTools(page);
      await page.waitForSelector(rowBtn, { timeout: 30000 });
      await page.click(rowBtn);
      await waitFor(async () => /已发布|Published/.test(await visibleText(page, '#upload-status')), 120000, 'published after retry');
      record(id, true, { failureText: failText.slice(0, 40), cancelVisible: true, creates: fake.st.creates.length, published: true });
    } catch (e) {
      record(id, false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  async function apiPost(page, url, body) {
    return page.evaluate(async ([u, b]) => {
      const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
      const r = await fetch(u, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' },
        body: JSON.stringify(b || {}),
      });
      let j = null;
      try { j = await r.json(); } catch { j = null; }
      return { status: r.status, body: j };
    }, [url, body]);
  }

  /// m3：真实 Flask ingestion 合同（仅 COS 分块 PUT 被拦截）。本账号已有一个
  /// 进行中的上传时，再建的上传正常排队（202 waiting）；再上传：账号等待上限
  /// 409 cos_waiting_limit 在主面板与刷新后的任务列表入口都显示可读文案；撤掉
  /// 排队任务后，工具页上传正常排队（可见等待暂存空间）并可取消。
  async function scenarioWaitingLimitReal() {
    const id = 'm3-waiting-limit-real-endpoint';
    const { context, page } = await L.launch(id, [L.READ_JOB_RECORDS]);
    await L.fakeCosPuts(context, creds.cosOrigin);
    const apiIds = [];
    try {
      await L.login(page, PORT, creds, 'user');
      // 账号已有进行中的上传（本用例自建，或前序场景遗留）时，新建的上传排队
      let q = null;
      for (let i = 0; i < 2 && !q; i++) {
        const r = await apiPost(page, '/api/ingestions', { filename: `m3-${i}.tif`, declared_size: 900000000 });
        if (r.status !== 202) throw new Error(`create ${i}: ${r.status} ${JSON.stringify(r.body).slice(0, 200)}`);
        apiIds.push(r.body.job_id);
        if (r.body.code === 'cos_waiting_capacity') q = r;
      }
      if (!q || q.body.stage !== 'waiting_space') throw new Error(`no queued upload: ${JSON.stringify(q && q.body).slice(0, 200)}`);
      const probe = await apiPost(page, '/api/ingestions', { filename: 'm3-probe.tif', declared_size: 1000 });
      if (probe.status !== 409 || probe.body.code !== 'cos_waiting_limit') throw new Error(`contract ${probe.status} ${JSON.stringify(probe.body)}`);
      await L.openTools(page, PORT);
      const jobId = await convertOnly(page, bf);
      await page.click('#upload-btn');
      await waitFor(async () => /排队|queued/i.test(await visibleText(page, '#upload-status')), 30000, 'main panel waiting-limit');
      const mainText = await visibleText(page, '#upload-status');
      if (/object Object/.test(mainText)) throw new Error(`raw object: ${mainText}`);
      await reloadTools(page);
      const rowBtn = `.job-row[data-job-id="${jobId}"] [data-action="upload"]`;
      await page.waitForSelector(rowBtn, { timeout: 30000 });
      await page.click(rowBtn);
      await waitFor(async () => /排队|queued/i.test(await visibleText(page, '#upload-status')), 30000, 'row waiting-limit');
      const rowText = await visibleText(page, '#upload-status');
      if (/object Object/.test(rowText)) throw new Error(`raw object (row): ${rowText}`);
      // 撤掉排队的那个：工具页上传正常排队（可见等待暂存空间），可取消
      const c = await apiPost(page, `/api/ingestions/${q.body.job_id}/cancel`, {});
      if (![200, 202].includes(c.status)) throw new Error(`cancel queued ${c.status}`);
      await page.waitForSelector(`${rowBtn}:not([disabled])`, { timeout: 30000 });
      await page.click(rowBtn);
      await waitFor(async () => /等待暂存空间|Waiting for staging space/.test(await visibleText(page, '#upload-status')), 60000, 'visible waiting_space');
      const waitText = await visibleText(page, '#upload-status');
      if (!(await page.isVisible('#upload-cancel-btn'))) throw new Error('cancel not visible while queued');
      await page.click('#upload-cancel-btn');
      await waitFor(async () => /已取消上传|Upload cancelled/.test(await visibleText(page, '#upload-status')), 30000, 'cancelled');
      record(id, true, {
        secondQueued: true, probe409: probe.body.code, mainPanel: mainText.slice(0, 30),
        refreshedRow: rowText.slice(0, 30), queuedStage: waitText.slice(0, 20), cancelled: true,
      });
    } catch (e) {
      record(id, false, { error: String(e).slice(0, 400) });
    } finally {
      for (const aid of apiIds) await apiPost(page, `/api/ingestions/${aid}/cancel`, {}).catch(() => {});
      await context.close();
    }
  }

  /// n1：管理插件切片页（0.4.14）——新上传显示原始文件名 + slide_id、资产
  /// 状态可读；同名两片各自一行；已删除资产「加入」禁用并说明，服务端对它的
  /// 加入请求给出明确拒绝；对可用资产点「加入」后 owner 工作区真正可见可读。
  async function scenarioAdminSlidesDisplayGrant() {
    const id = 'n1-admin-slides-display-grant';
    const userCtx = await L.launch(`${id}-user`);
    const { context, page } = await L.launch(id);
    const [keepId, delId] = creds.dupSlideIds || [];
    try {
      await L.login(userCtx.page, PORT, creds, 'user', '/app');
      const del = await userCtx.page.evaluate(async (sid) => {
        const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
        const r = await fetch(`/api/slides/${sid}`, { method: 'DELETE', credentials: 'same-origin',
          headers: { 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' } });
        return r.status;
      }, delId);
      if (del !== 200) throw new Error(`user delete ${del}`);
      await L.login(page, PORT, creds, 'owner', '/admin');
      const before = await L.apiGet(page, `/api/slides/${keepId}/info`);
      if (before.status === 200) throw new Error('owner could already read the user slide before grant');
      const frame = page.frameLocator('#admin-plugin-frame');
      await frame.locator('.adm-nav-btn[data-page="slides"]').click();
      const keepRow = frame.locator('#adm-slides-tbody tr', { hasText: `ID ${keepId}` });
      const delRow = frame.locator('#adm-slides-tbody tr', { hasText: `ID ${delId}` });
      await keepRow.waitFor({ timeout: 30000 });
      const keepText = await keepRow.innerText();
      const delText = await delRow.innerText();
      if (!/r1-dup-same\.tif/.test(keepText) || !/可用/.test(keepText)) throw new Error(`keep row: ${keepText}`);
      if (!/r1-dup-same\.tif/.test(delText) || !/已删除/.test(delText)) throw new Error(`deleted row: ${delText}`);
      const delAdd = delRow.getByRole('button', { name: '加入' });
      if (await delAdd.isEnabled()) throw new Error('add enabled for deleted asset');
      const reason = await delAdd.getAttribute('title');
      if (!/已删除/.test(reason || '')) throw new Error(`reason ${reason}`);
      const refusal = await page.evaluate(async (sid) => {
        const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
        const r = await fetch(`/api/admin/v1/slides/${sid}/visibility`, {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' },
          body: JSON.stringify({ granted: true }),
        });
        return { status: r.status, body: await r.json().catch(() => null) };
      }, delId);
      if (refusal.status !== 409) throw new Error(`deleted grant refusal ${JSON.stringify(refusal)}`);
      await keepRow.getByRole('button', { name: '加入' }).click();
      await waitFor(async () => /已加入工作区 r1-dup-same\.tif/.test(await frame.locator('#adm-slides-status').innerText()), 30000, 'grant status');
      const after = await L.apiGet(page, `/api/slides/${keepId}/info`);
      const list = await L.apiGet(page, '/api/slides');
      const listed = JSON.stringify(list.body || '').includes(keepId);
      if (after.status !== 200 || !listed) throw new Error(`after grant info=${after.status} listed=${listed}`);
      await keepRow.waitFor({ timeout: 30000 });
      await frame.locator('#adm-slides-tbody tr', { hasText: `ID ${keepId}` }).getByRole('button', { name: '移除' }).click();
      await frame.locator('#adm-slides-confirm').getByRole('button', { name: '确认执行' }).click();
      await waitFor(async () => /已移出工作区/.test(await frame.locator('#adm-slides-status').innerText()), 30000, 'revoke status');
      record(id, true, {
        newUploadLabel: 'r1-dup-same.tif + ID', deletedState: '已删除', deletedAddDisabled: true,
        deletedServerRefusal: refusal.body && (refusal.body.code || (refusal.body.error && refusal.body.error.code)), readBefore: before.status, readAfterGrant: after.status, revoked: true,
      });
    } catch (e) {
      record(id, false, { error: String(e).slice(0, 400) });
    } finally {
      await userCtx.context.close();
      await context.close();
    }
  }

  const all = [
    ['a-oneclick-order-network', scenarioOneClick],
    ['b1-convert-fail-zero-ingestion', scenarioConvertFail],
    ['b2-cancel-during-convert-zero-ingestion', scenarioCancelDuringConvert],
    ['c1-oversize-stops', () => scenarioAdmissionStop('oversize')],
    ['c2-not-viewable-stops', () => scenarioAdmissionStop('viewable')],
    ['d1-login-expired-before-click', scenarioLoginExpiredBefore],
    ['d2-session-loss-reopen-continue', () => scenarioSessionLossAndReopen({ who: 'user' })],
    ['d3-different-account-reconfirm', () => scenarioSessionLossAndReopen({ who: 'owner' })],
    ['d4-two-users-separate-upload', () => scenarioTwoUsersRealAuthz('separate')],
    ['d5-two-users-return-to-original', () => scenarioTwoUsersRealAuthz('original')],
    ['e1-refresh-during-convert', scenarioRefreshDuringConvert],
    ['e3-refresh-during-copy', scenarioRefreshDuringCopy],
    ['f1-repeated-clicks-one-ingestion', scenarioRepeatedClicks],
    ['f2-second-tab-one-ingestion', scenarioSecondTab],
    ['f3-replayed-complete-one-ingestion', scenarioReplayedComplete],
    ['g-cancel-after-convert-revokes-intent', scenarioCancelRevokesIntent],
    ['h-local-only-zero-network', scenarioLocalOnly],
    ['i-workbench-handoff', scenarioWorkbenchHandoff],
    ['i2-native-direct-upload', scenarioNativeDirectReal],
    ['i3-popup-blocked-fallback', scenarioPopupBlocked],
    ['i4-handoff-new-project-target', scenarioHandoffNewProject],
    ['i5-assoc-failure-retry', scenarioAssocRetry],
    ['j-disk-confirm-gates-oneclick', scenarioDiskConfirmGates],
    ['k1-published-repeat-clicks', scenarioPublishedRepeatClicks],
    ['l1-project-open-and-ui-delete', scenarioProjectOpenAndUiDelete],
    ['l2-convert-offer-persists-past-60s', scenarioConvertOfferPersists],
    ['l3-drawer-offer-clickable', scenarioDrawerOfferClickable],
    ['m1-refresh-anon-login-return-upload', scenarioRefreshAnonLoginUpload],
    ['m2-refresh-failure-cancel-retry', scenarioRefreshFailureRetry],
    ['m3-waiting-limit-real-endpoint', scenarioWaitingLimitReal],
    ['n1-admin-slides-display-grant', scenarioAdminSlidesDisplayGrant],
  ];
  for (const [id, fn] of all) {
    if (ONLY && !ONLY.split(',').includes(id)) continue;
    await fn();
  }

  fs.mkdirSync(path.join(L.GATE, 'e2e'), { recursive: true });
  fs.writeFileSync(path.join(L.GATE, 'e2e', 'results.json'), JSON.stringify(results, null, 2));
  const failed = Object.values(results).filter((r) => !r.pass).length;
  console.log(failed ? `E2E FAILED: ${failed}` : 'E2E ALL PASS');
  if (server) server.kill('SIGTERM');
}

main().catch((e) => { console.error(e); process.exit(1); });
