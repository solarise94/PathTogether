'use strict';
// C4 工具页上传驱动共享库（Playwright；复用 C3 的 lib.js 与 Flask 启动模式）。
// 被测对象：真实 Flask app（server.py：AUTH_ENABLED=True + 假 COS 配置 +
// 一次性 owner/user 凭据）。ingestion 控制 API 与 COS 分块 PUT 由 page.route
// 的**有状态假后端**承担（路由晚于 CSP 检查执行——CSP 写错 PUT 会被浏览器
// 直接拦下，测试随之失败）。
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const C3 = require('../slide_tools_c3/lib.js');

const HERE = __dirname;
const REPO = path.resolve(HERE, '../../..');
const GATE = path.join(REPO, '.gate-tmp/slide-tools-c4');

async function startServer(port, credsPath) {
  const server = spawn(
    process.env.C4_PY || '.venv/bin/python3',
    [path.join(HERE, 'server.py'), '--port', String(port), '--creds', credsPath],
    { cwd: REPO, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  let buf = '';
  const started = new Promise((res, rej) => {
    const t = setTimeout(() => rej(new Error(`server start timeout; log: ${buf}`)), 120000);
    server.stdout.on('data', (d) => { buf += d; if (buf.includes('Running on')) { clearTimeout(t); res(); } });
    server.stderr.on('data', (d) => { buf += d; if (buf.includes('Running on')) { clearTimeout(t); res(); } });
    server.on('exit', (code) => rej(new Error(`server exited ${code}: ${buf}`)));
  });
  await started;
  return server;
}

function readCreds(p) {
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

/// 真实登录（/login 表单：用户名/密码/CSRF 隐藏域；session 落在 context）。
/// next 指回工具页，登录后停在 /tools/slides。
async function login(page, port, creds, who = 'user', next = '/tools/slides') {
  await page.goto(`http://127.0.0.1:${port}/login?next=${encodeURIComponent(next)}`,
    { waitUntil: 'load' });
  await page.fill('#login-dialog-username', who === 'owner' ? creds.ownerLogin : creds.userLogin);
  await page.fill('#login-dialog-password', who === 'owner' ? creds.ownerPassword : creds.userPassword);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'load', timeout: 30000 }),
    page.click('#login-dialog-form button[type="submit"]'),
  ]);
  return page.url();
}

// ------------------------------------------------------ 假 ingestion/COS --

/// 安装有状态假后端。返回 { st, behavior, uninstall }：
///  - st：可观察状态（创建数/签名批/PUT 字节/complete 次数/任务表）；
///  - behavior：可变钩子（测试按场景改写，null = 走默认实现）。
///  - opts.skipCosRoute（U1）：不拦截 COS origin——分块 PUT 放行到真实网络
///    （配合 startLocalCos 的本地 HTTPS 假 COS + --host-resolver-rules +
///    CDP 上行限速，驱动真实 XHR upload.onprogress 字节进度）。
async function fakeUploadRoutes(page, cosOrigin, opts = {}) {
  const partBytes = opts.partBytes || 0;
  const partsCount = opts.partsCount || 0;   // 按块数等分（优先于 partBytes）
  const skipCosRoute = !!opts.skipCosRoute;
  const st = {
    creates: [],            // [ {filename, declared_size} ]
    signs: [],              // [ [part numbers] ]
    puts: [],               // [ {partNumber, bytes} ]
    completeReqs: 0,
    cancels: 0,
    resumes: 0,
    statusGets: 0,
    jobs: new Map(),        // ingestionId -> job record
  };
  const behavior = {
    // (body, job) => {status, body} | null —— /api/ingestions 创建响应
    onCreate: null,
    // (job) => stage 字符串 | null（null = 默认推进）—— GET 状态响应
    onStatus: null,
    // (job, nth) => {status, body} | 'abort' | null —— upload-complete
    onComplete: null,
    // (body, job) => {status, body} | 'abort' | null —— parts/sign
    onSign: null,
    // (job) => {status, body} | null —— cancel
    onCancel: null,
    // (job) => {status, body} | null —— resume
    onResume: null,
    // () => {status, body} | 'abort' | null —— 能力端点（默认放行真服务端）
    onCapability: null,
    // async () => void | null —— 分块 PUT 前的闸（挂起指定分块供场景控制）
    gatePut: null,
    // async () => void | null —— upload-complete 前的闸
    gateComplete: null,
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
    // 默认推进：服务端阶段每被轮询一次后就绪（测试可用 onStatus 钉住阶段）
    if (!behavior.onStatus && SERVER_STAGES.includes(stage)) {
      const cur = stage;
      job.stage = 'viewable';
      stage = cur;
    }
    const body = { job_id: job.id, state: stage, stage, declared_size: job.size };
    if (stage === 'uploading') body.parts = job.parts;
    if (stage === 'viewable') {
      body.slide = job.filename;
      body.slide_id = job.slideId || 'sld_c4fake0000000000000001';
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
      const body = req.postDataJSON();
      st.creates.push(body);
      const over = behavior.onCreate && behavior.onCreate(body, null);
      if (over && over.status !== 202) {
        return route.fulfill({ status: over.status, contentType: 'application/json', body: JSON.stringify(over.body || {}) });
      }
      const jid = `inj_c4_${st.creates.length}`;
      const job = {
        id: jid, filename: body.filename, size: body.declared_size,
        stage: 'uploading', parts: planFor(body.declared_size),
        confirmed: new Set(), slideId: null,
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
      // 服务端语义：complete 幂等且**先于响应**应用——响应丢失（abort）或
      // 重放（409 ingestion_state_conflict）时状态都已离开 uploading。
      if (job.stage === 'uploading') job.stage = 'awaiting_server';
      const over = behavior.onComplete && behavior.onComplete(job, nth);
      if (over === 'abort') return route.abort('connectionfailed');
      if (over && over.status !== 202) {
        // 409 冲突 = 已完成过（服务端状态权威）
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
    const data = req.postDataBuffer();
    if (behavior.gatePut) await behavior.gatePut(n);
    st.puts.push({ partNumber: n, bytes: data ? data.length : 0, url: u.origin });
    const jid = (u.pathname.match(/incoming\/(inj_c4_\d+)/) || [])[1];
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
    return route.fallback();   // 默认：真实 Flask 响应
  }

  await page.route('**/api/ingestions**', handleIngestions);
  if (!skipCosRoute) await page.route(`${cosOrigin}/**`, handleCos);
  await page.route('**/api/tools/slides/upload-capability', handleCapability);
  return {
    st, behavior,
    async uninstall() {
      await page.unroute('**/api/ingestions**', handleIngestions);
      if (!skipCosRoute) await page.unroute(`${cosOrigin}/**`, handleCos);
      await page.unroute('**/api/tools/slides/upload-capability', handleCapability);
    },
  };
}

// ------------------------------------------------- 本地 HTTPS 假 COS（U1） --

/// 起一个真实 TLS 的本地假 COS（127.0.0.1 随机端口）。page.route 的响应在
/// 请求被网络栈接管前就完成，浏览器不会真正发送请求体——XHR upload progress
/// 事件随之缺失。要测字节级上传进度，PUT 必须走到真实 socket：调用方用
/// Chromium `--host-resolver-rules=MAP <cosHost> 127.0.0.1` +
/// `--ignore-certificate-errors`（自签名证书）把假 COS 域名指到本服务，
/// 再用 CDP `Network.emulateNetworkConditions` 限制上行吞吐，网络层就会按
/// 字节缓慢发送 body，XHR upload.onprogress 逐次回调。
/// 证书经 openssl 生成到 .gate-tmp（测试专用，不进仓库）。
async function startLocalCos(cosHost) {
  const https = require('https');
  const { execFileSync } = require('child_process');
  fs.mkdirSync(GATE, { recursive: true });
  const keyPath = path.join(GATE, 'local-cos-key.pem');
  const certPath = path.join(GATE, 'local-cos-cert.pem');
  if (!fs.existsSync(keyPath) || !fs.existsSync(certPath)) {
    execFileSync('openssl', [
      'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
      '-keyout', keyPath, '-out', certPath,
      '-subj', `/CN=${cosHost}`,
      '-addext', `subjectAltName=DNS:${cosHost}`,
    ], { stdio: 'ignore' });
  }
  const st = { puts: [], options: 0, bytes: 0 };
  const server = https.createServer(
    { key: fs.readFileSync(keyPath), cert: fs.readFileSync(certPath) },
    (req, res) => {
      const u = new URL(req.url, `https://${cosHost}/`);
      const cors = {
        'Access-Control-Allow-Origin': req.headers.origin || '*',
        'Access-Control-Allow-Methods': 'PUT, OPTIONS',
        'Access-Control-Expose-Headers': 'ETag',
        Vary: 'Origin',
      };
      if (req.method === 'OPTIONS') {
        st.options++;
        // 带类型的 Blob（File.slice 继承 MIME）会让 XHR 附带 Content-Type，
        // 预检即请求放行这些头——原样回显请求的头清单（含 content-type）
        res.writeHead(204, {
          ...cors,
          'Access-Control-Allow-Headers':
            req.headers['access-control-request-headers'] || '',
          'Access-Control-Max-Age': '600',
        });
        return res.end();
      }
      if (req.method === 'PUT') {
        let n = 0;
        req.on('data', (c) => { n += c.length; });
        req.on('end', () => {
          const partNumber = Number(u.searchParams.get('partNumber'));
          st.puts.push({ partNumber, bytes: n });
          st.bytes += n;
          res.writeHead(200, { ...cors, ETag: `"etag-${st.puts.length}"` });
          res.end('');
        });
        return;
      }
      res.writeHead(404, cors);
      res.end('');
    });
  await new Promise((res) => server.listen(0, '127.0.0.1', res));
  return { server, port: server.address().port, st };
}

// ------------------------------------------------------------- OPFS 哈希 --

/// 任务产物（slide-jobs/<jobId>/output.tif）的流式 sha256：1 MiB 分块把
/// 产物复制到 OPFS 根（不整文件物化），再复用 C3 的纯 JS 实现计算。
async function opfsJobSha256(page, jobId) {
  await page.evaluate(async (jid) => {
    const root = await navigator.storage.getDirectory();
    const dir = await (await root.getDirectoryHandle('slide-jobs')).getDirectoryHandle(jid);
    const src = await (await dir.getFileHandle('output.tif')).getFile();
    const dst = await root.getFileHandle('__c4-output.bin', { create: true });
    const w = await dst.createWritable();
    const CHUNK = 1 << 20;
    for (let off = 0; off < src.size; off += CHUNK) {
      await w.write(await src.slice(off, Math.min(src.size, off + CHUNK)).arrayBuffer());
    }
    await w.close();
  }, jobId);
  return C3.opfsSha256(page, '__c4-output.bin');
}

module.exports = {
  startServer, readCreds, login, fakeUploadRoutes, opfsJobSha256, startLocalCos,
  arg: C3.arg, launch: C3.launch, openTools: C3.openTools,
  savePickerStub: C3.savePickerStub, downloadGuard: C3.downloadGuard,
  setFile: C3.setFile, ensureFixture: C3.ensureFixture, nativeConvert: C3.nativeConvert,
  sha256File: C3.sha256File, clearJobs: C3.clearJobs, jobDirs: C3.jobDirs,
  GATE, REPO,
  C3,
};
