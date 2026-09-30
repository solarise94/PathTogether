'use strict';
// R1「一键转换并上传」驱动共享库（Playwright；复用 C3/C4 的启动模式与假后端
// 形态）。被测对象：真实 Flask app（tests/browser/slide_tools_c4/server.py
// 原样复用——AUTH_ENABLED=True + 假 COS 配置 + 一次性 owner/user 凭据）。
//
// 与 C4 lib 的差别：假 COS 分块 PUT **保留字节**（Buffer），供场景把上传对象
// 重组后与 OPFS 产物逐字节比对（验收 1：上传的必须是转换后的产物，不是源
// KFB/KFBF）。route 注册在 context 上（工作台 popup 交接场景：popup 与
// 打开者共用同一套假后端）。
const path = require('path');
const crypto = require('crypto');
const C4 = require('../slide_tools_c4/lib.js');
const C3 = C4.C3;

const HERE = __dirname;
const REPO = path.resolve(HERE, '../../..');
const GATE = path.join(REPO, '.gate-tmp/slide-tools-r1');

/// 有状态假后端（C4 fakeUploadRoutes 的字节保留版）。target 接受 page 或
/// browserContext（popup 场景传 context）。
async function fakeUploadRoutes(target, cosOrigin, opts = {}) {
  const partBytes = opts.partBytes || 0;
  const partsCount = opts.partsCount || 0;
  const st = {
    creates: [],            // [ {filename, declared_size} ]
    signs: [],              // [ [part numbers] ]
    puts: [],               // [ {partNumber, bytes, buf} ]
    completeReqs: 0,
    cancels: 0,
    resumes: 0,
    statusGets: 0,
    jobs: new Map(),        // ingestionId -> job record
  };
  const behavior = {
    onCreate: null,
    onStatus: null,
    onComplete: null,
    onSign: null,
    onCancel: null,
    onResume: null,
    onCapability: null,
    gatePut: null,
    gateComplete: null,
    gateCreate: null,       // async () => void | null —— POST /api/ingestions 前的闸
  };

  function planFor(size) {
    const parts = [];
    if (partsCount > 0) {
      const len = Math.ceil(size / partsCount);
      let off = 0;
      let n = 1;
      while (off < size) {
        parts.push({ part_number: n, length: Math.min(len, size - off) });
        off += Math.min(len, size - off);
        n += 1;
      }
      return parts;
    }
    const bytes = partBytes || 8;
    let off = 0;
    let n = 1;
    while (off < size) {
      const len = Math.min(bytes, size - off);
      parts.push({ part_number: n, length: len });
      off += len;
      n += 1;
    }
    return parts;
  }

  const SERVER_STAGES = ['awaiting_server', 'downloading', 'validating', 'processing', 'readiness'];

  function statusBody(job) {
    let stage = behavior.onStatus ? (behavior.onStatus(job) || job.stage) : job.stage;
    if (!behavior.onStatus && SERVER_STAGES.includes(stage)) {
      const cur = stage;
      job.stage = 'viewable';
      stage = cur;
    }
    const body = { job_id: job.id, state: stage, stage, declared_size: job.size };
    if (stage === 'uploading') body.parts = job.parts;
    if (stage === 'viewable') {
      body.slide = job.filename;
      body.slide_id = job.slideId || 'sld_r1fake00000000000000001';
      body.canonical_name = job.filename;
    }
    if (stage === 'downloading') body.downloaded_bytes = Math.floor(job.size / 2);
    return body;
  }

  async function handleIngestions(route) {
    const req = route.request();
    const url = new URL(req.url());
    const m = url.pathname.match(/^\/api\/ingestions(\/([^/]+)(\/.*)?)?$/);
    if (!m) return route.fallback();
    const method = req.method();
    const id = m[2] || null;
    const sub = m[3] || '';
    if (!id && method === 'POST') {
      if (behavior.gateCreate) await behavior.gateCreate();
      const body = req.postDataJSON();
      st.creates.push(body);
      const over = behavior.onCreate && behavior.onCreate(body, null);
      if (over && over.status !== 202) {
        return route.fulfill({ status: over.status, contentType: 'application/json', body: JSON.stringify(over.body || {}) });
      }
      const jid = `inj_r1_${st.creates.length}`;
      const job = {
        id: jid, filename: body.filename, size: body.declared_size,
        stage: 'uploading', parts: planFor(body.declared_size),
        confirmed: new Set(), slideId: opts.slideId || null,
      };
      st.jobs.set(jid, job);
      const respBody = { job_id: jid, state: 'uploading', stage: 'uploading', declared_size: job.size };
      const custom = over && over.body ? Object.assign({}, respBody, over.body) : respBody;
      return route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify(custom) });
    }
    const job = id && st.jobs.get(id);
    if (!job) {
      return route.fulfill({ status: 404, contentType: 'application/json',
        body: JSON.stringify({ error: 'not found', code: 'not_found' }) });
    }
    if (method === 'GET' && !sub) {
      st.statusGets++;
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(statusBody(job)) });
    }
    if (method === 'POST' && sub === '/parts/sign') {
      const body = req.postDataJSON();
      st.signs.push(body.part_numbers);
      const over = behavior.onSign && behavior.onSign(body, job);
      if (over === 'abort') return route.abort('connectionfailed');
      if (over && over.status !== 200) {
        return route.fulfill({ status: over.status, contentType: 'application/json', body: JSON.stringify(over.body || {}) });
      }
      const urls = body.part_numbers.map((n) => ({
        url: `${cosOrigin}/incoming/${job.id}?uploadId=up-${job.id}&partNumber=${n}`,
        part_number: n,
      }));
      return route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ job_id: job.id, upload_id: `up-${job.id}`, transport: 'presign_parts', urls }) });
    }
    if (method === 'POST' && sub === '/upload-complete') {
      st.completeReqs++;
      const nth = st.completeReqs;
      if (behavior.gateComplete) await behavior.gateComplete();
      if (job.stage === 'uploading') job.stage = 'awaiting_server';
      const over = behavior.onComplete && behavior.onComplete(job, nth);
      if (over === 'abort') return route.abort('connectionfailed');
      if (over && over.status !== 202) {
        return route.fulfill({ status: over.status, contentType: 'application/json', body: JSON.stringify(over.body || {}) });
      }
      return route.fulfill({ status: 202, contentType: 'application/json',
        body: JSON.stringify({ job_id: job.id, state: job.stage, stage: job.stage }) });
    }
    if (method === 'POST' && sub === '/cancel') {
      st.cancels++;
      const over = behavior.onCancel && behavior.onCancel(job);
      if (over && over.status !== 202) {
        return route.fulfill({ status: over.status, contentType: 'application/json', body: JSON.stringify(over.body || {}) });
      }
      job.stage = 'terminal';
      job.failCode = 'cancelled';
      return route.fulfill({ status: 202, contentType: 'application/json',
        body: JSON.stringify({ job_id: job.id, state: 'terminal', stage: 'terminal' }) });
    }
    if (method === 'POST' && sub === '/resume') {
      st.resumes++;
      const over = behavior.onResume && behavior.onResume(job);
      if (over && over.status !== 202) {
        return route.fulfill({ status: over.status, contentType: 'application/json', body: JSON.stringify(over.body || {}) });
      }
      if (job.stage === 'completing' || job.stage === 'awaiting_server') job.stage = 'uploading';
      return route.fulfill({ status: 202, contentType: 'application/json',
        body: JSON.stringify({ job_id: job.id, state: job.stage, stage: job.stage }) });
    }
    return route.fallback();
  }

  async function handleCos(route) {
    const req = route.request();
    if (req.method() !== 'PUT') return route.fallback();
    const u = new URL(req.url());
    const n = Number(u.searchParams.get('partNumber'));
    const buf = req.postDataBuffer();
    if (behavior.gatePut) await behavior.gatePut(n);
    st.puts.push({ partNumber: n, bytes: buf ? buf.length : 0, buf: Buffer.from(buf || '') });
    const jid = (u.pathname.match(/incoming\/(inj_r1_\d+)/) || [])[1];
    const job = jid ? st.jobs.get(jid) : null;
    if (job) job.confirmed.add(n);
    return route.fulfill({ status: 200, headers: { ETag: `"etag-${n}"` }, body: '' });
  }

  async function handleCapability(route) {
    const over = behavior.onCapability && behavior.onCapability();
    if (over === 'abort') return route.abort('connectionfailed');
    if (over && over.status) {
      return route.fulfill({ status: over.status, contentType: 'application/json', body: JSON.stringify(over.body || {}) });
    }
    return route.fallback();   // 默认：真实 Flask 响应（含 account 字段）
  }

  await target.route('**/api/ingestions**', handleIngestions);
  await target.route(`${cosOrigin}/**`, handleCos);
  await target.route('**/api/tools/slides/upload-capability', handleCapability);
  return { st, behavior };
}

/// 只拦截 COS 分块 PUT（ingestion 控制 API 走真实 Flask：真实归属/授权）。
/// hold=true 时 PUT 挂起不回（模拟上传中断）；其余回 200 + ETag。
async function fakeCosPuts(target, cosOrigin) {
  const st = { puts: [], attempts: 0, hold: false };
  await target.route(`${cosOrigin}/**`, async (route) => {
    const req = route.request();
    if (req.method() !== 'PUT') return route.fallback();
    st.attempts += 1;
    if (st.hold) return new Promise(() => {});
    const u = new URL(req.url());
    const buf = req.postDataBuffer();
    st.puts.push({ partNumber: Number(u.searchParams.get('partNumber')),
      bytes: buf ? buf.length : 0, key: u.pathname });
    return route.fulfill({ status: 200, headers: { ETag: `"etag-${st.puts.length}"` }, body: '' });
  });
  return st;
}

/// R1 测试服务：C4 server.py + 第二个普通用户、进程内假 COS preparing
/// worker（真实 ingestion 可走到 uploading）、一个真实 ready 切片。
async function startServer(port, credsPath) {
  const { spawn } = require('child_process');
  const server = spawn(
    process.env.C4_PY || '.venv/bin/python3',
    [path.join(REPO, 'tests/browser/slide_tools_c4/server.py'), '--port', String(port),
      '--creds', credsPath, '--fake-cos-worker', '--seed-ready-slide'],
    { cwd: REPO, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  let buf = '';
  await new Promise((res, rej) => {
    const t = setTimeout(() => rej(new Error(`server start timeout; log: ${buf}`)), 120000);
    const on = (d) => { buf += d; if (buf.includes('Running on')) { clearTimeout(t); res(); } };
    server.stdout.on('data', on);
    server.stderr.on('data', on);
    server.on('exit', (code) => rej(new Error(`server exited ${code}: ${buf}`)));
  });
  server.stderr.on('data', (d) => {
    const line = String(d);
    if (/fake-cos-worker error|Traceback/.test(line) && !/shutting down|No such file or directory/.test(line)) process.stderr.write(`[server] ${line}`);
  });
  return server;
}

/// 登录（who: 'owner' | 'user' | 'user2'）。
async function login(page, port, creds, who = 'user', next = '/tools/slides') {
  if (who !== 'user2') return C4.login(page, port, creds, who, next);
  await page.goto(`http://127.0.0.1:${port}/login?next=${encodeURIComponent(next)}`,
    { waitUntil: 'load' });
  await page.fill('#login-dialog-username', creds.user2Login);
  await page.fill('#login-dialog-password', creds.user2Password);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'load', timeout: 30000 }),
    page.click('#login-dialog-form button[type="submit"]'),
  ]);
  return page.url();
}

/// 以页面当前会话调真实 API（GET 读 JSON；返回 {status, body}）。
async function apiGet(page, url) {
  return page.evaluate(async (u) => {
    const r = await fetch(u, { credentials: 'same-origin' });
    let body = null;
    try { body = await r.json(); } catch { body = null; }
    return { status: r.status, body };
  }, url);
}

/// 重组上传对象（按分块编号排序拼接）→ sha256（验收 1：上传的是产物）。
function uploadedSha256(st) {
  const sorted = st.puts.slice().sort((a, b) => a.partNumber - b.partNumber);
  const h = crypto.createHash('sha256');
  for (const p of sorted) h.update(p.buf);
  return { sha256: h.digest('hex'), bytes: sorted.reduce((s, p) => s + p.bytes, 0) };
}

/// 测试侧只读 OPFS 任务记录（engine.js 双槽读取，与 C3 相同形态）。
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

module.exports = {
  fakeUploadRoutes, fakeCosPuts, uploadedSha256, READ_JOB_RECORDS, apiGet,
  GATE, REPO,
  startServer, readCreds: C4.readCreds, login,
  arg: C3.arg, launch: C3.launch, openTools: C3.openTools,
  savePickerStub: C3.savePickerStub, downloadGuard: C3.downloadGuard,
  setFile: C3.setFile, ensureFixture: C3.ensureFixture, sparseLargeKfb: C3.sparseLargeKfb,
  sha256File: C3.sha256File, clearJobs: C3.clearJobs, jobDirs: C3.jobDirs,
  opfsJobSha256: C4.opfsJobSha256, opfsSha256: C3.opfsSha256,
};
