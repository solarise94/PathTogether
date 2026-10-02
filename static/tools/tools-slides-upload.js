// tools-slides-upload.js — C4 上传接入（/tools/slides 结果面板 + 任务列表）。
// 引擎是共享 classic script window.HP_COS_UPLOAD（static/upload/cos-uploader.js，
// 与工作台同一份）；本模块只做：点击后的能力判定（登录/可用/限额/格式）、
// OPFS 产物 → 引擎 source 视图（仅 slice 分块读取）、上传记录持久化到
// OPFS 任务记录（runner.setJobUpload）与 UI。
//
// 硬约束（计划 §1/§5/§9 C4）：
//  - 用户点击上传之前零 /api/ 请求（能力在点击时才拉取）；
//  - 不支持查看的输出格式禁用上传入口并说明原因，仍允许本地保存；
//  - 限额按最终文件（result.output_bytes vs max_size_bytes）；
//  - 登录过期（401）绝不自动跳转：保留产物、给出登录链接，登录后继续
//    同一 ingestion（CSRF 每次调用都从 cookie 重读）；
//  - 取消/失败保留本地产物，任务保持 ready/exported；上传进行中删除被拒。
'use strict';

import * as E from './slide-transform/engine.js';

const CAPABILITY_URL = '/api/tools/slides/upload-capability';
const LOGIN_URL = '/login?next=/tools/slides';

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

/// 页面版 apiFetch：非安全方法附带 X-CSRF-Token（每次调用重读 cookie）；
/// 401 **不重定向**（与工作台 app.js 相反——上传中断不能丢本地产物），
/// 由调用方按 auth_required 分类处理。
function pageApiFetch(url, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
    const tok = csrfToken();
    if (tok) opts.headers = { ...(opts.headers || {}), 'X-CSRF-Token': tok };
  }
  return fetch(url, { credentials: 'same-origin', ...opts });
}

/// R1 一键转换并上传控制器（tools-slides-convert-upload.js）与本控制器
/// 共用同一能力判定语义（登录/离线/负载解析），导出复用。
export async function fetchCapability() {
  let resp;
  try {
    resp = await pageApiFetch(CAPABILITY_URL);
  } catch (e) {
    const err = new Error('network');
    err.offline = true;
    throw err;
  }
  if (resp.status === 401) return { authRequired: true };
  if (!resp.ok) throw new Error(`capability ${resp.status}`);
  try {
    return await resp.json();
  } catch (e) {
    throw new Error('capability body');
  }
}

function isAuthError(err) {
  if (!err) return false;
  if (err.status === 401) return true;
  const code = err && err.data && (err.data.code || err.data.error);
  return code === 'auth_required';
}

const TERMINAL_UPLOAD_STATES = ['published', 'failed', 'cancelled'];

/// 服务端结构化错误（{status, data:{error, code, …}}）→ 文案键。只认稳定码
/// 与 HTTP 状态，绝不把原始对象/响应体拼进文案（[object Object]、内部细节）。
const STABLE_CODE_RE = /^[a-z][a-z0-9_]{0,63}$/;

export function describeUploadError(err) {
  const data = err && typeof err.data === 'object' && err.data ? err.data : null;
  const rawCode = data && typeof data.code === 'string' ? data.code : '';
  const code = STABLE_CODE_RE.test(rawCode) ? rawCode : '';
  const status = err && Number.isInteger(err.status) ? err.status : 0;
  if (code === 'cos_waiting_limit') return { key: 'tools.upload.err.waiting_limit' };
  if (code === 'upload_too_large' || status === 413) {
    const max = data && Number.isFinite(data.max_size_bytes) ? data.max_size_bytes : null;
    return { key: 'tools.upload.too.large', max, disable: true };
  }
  if (code === 'cos_pool_below_product_limit') return { key: 'tools.upload.err.pool_config' };
  if (code === 'cos_format_unsupported') return { key: 'tools.upload.format.unsupported', disable: true };
  if (code === 'ingestion_state_conflict') return { key: 'tools.upload.err.state' };
  if (code === 'cos_sign_rate_limited' || status === 429) return { key: 'tools.upload.err.rate' };
  if (code === 'cos_capacity_reconcile_required') return { key: 'tools.upload.err.reconcile' };
  if (code === 'cos_unavailable') return { key: 'tools.upload.capability.off' };
  if (status === 403) return { key: 'tools.upload.err.forbidden' };
  if (status === 503 || status === 502 || status === 504) return { key: 'tools.upload.err.unavailable' };
  if (status) return { key: 'tools.upload.err.http', vars: { status, code: code || '—' } };
  if (err instanceof Error && err.message) return { key: 'tools.upload.failed', vars: { e: err.message } };
  return { key: 'tools.upload.err.unknown' };
}

export function createUploadController({
  runner, t, onJobsRefresh, onPublished, onSelectJob,
}) {
  const state = {
    busyJobId: null,     // 本标签唯一进行中的上传
    handle: null,        // 引擎句柄（取消用）
    disabled: {},        // jobId -> true（超限/格式不支持：按钮保持禁用）
    // jobId -> 该任务最近一条上传反馈（{key,vars,link} | {text} | {published}）。
    // 只有结果面板当前指向的任务会画进 #upload-status：别的任务的进度/失败
    // 不会出现在这个面板里，切回该任务时重放。
    msgs: {},
    writes: Promise.resolve(),   // record.upload 写入队列（刷新列表前冲刷）
    orphans: {},                 // jobId -> 未确认取消的 ingestion id
  };

  /// 记录写入排进串行队列（收口后先冲刷再刷新列表）。返回的 Promise 如实
  /// 反映这次写入的成败——引擎据此判断 ingestion id 是否已落盘；队列本身
  /// 不因某次失败而中断。
  function queueWrite(fn) {
    const run = state.writes.then(() => fn());
    state.writes = run.catch(() => {});
    return run;
  }

  /// 把某任务的反馈画进 #upload-status（仅当结果面板正指向它）。链接/按钮
  /// 用 DOM 构造，不用 innerHTML。
  function paint(jobId) {
    if (!jobId || jobId !== page.currentJobId) return;
    const el = document.getElementById('upload-status');
    if (!el) return;
    el.textContent = '';
    const m = state.msgs[jobId];
    if (!m) return;
    if (m.published) {
      paintPublished(el, jobId, m.published);
      return;
    }
    if (typeof m.text === 'string') {
      el.textContent = m.text;
      return;
    }
    if (!m.link) {
      el.textContent = t(m.key, m.vars || {});
      return;
    }
    el.appendChild(document.createTextNode(t(m.key, m.vars || {})));
    el.appendChild(document.createTextNode(' '));
    const a = document.createElement('a');
    a.href = m.link.href;
    a.textContent = t(m.link.key);
    el.appendChild(a);
  }

  function setMsg(jobId, key, vars) {
    if (!jobId) return;
    if (key === null) delete state.msgs[jobId];
    else state.msgs[jobId] = { key, vars: vars || null };
    paint(jobId);
  }

  /// 登录链接 / 工作台链接是消息的一部分。
  function setMsgWithLink(jobId, key, linkHref, linkKey, vars) {
    if (!jobId) return;
    state.msgs[jobId] = { key, vars: vars || null, link: { href: linkHref, key: linkKey } };
    paint(jobId);
  }

  function setBusy(jobId, busy) {
    state.busyJobId = busy ? jobId : null;
    const btn = document.getElementById('upload-btn');
    const cancelBtn = document.getElementById('upload-cancel-btn');
    if (btn && page.currentJobId === jobId) {
      btn.disabled = busy || !!state.disabled[jobId];
    }
    // 取消按钮只跟随结果面板当前指向的任务（不替别的任务的上传给出取消入口）
    if (cancelBtn && page.currentJobId === jobId) cancelBtn.hidden = !busy;
  }

  // 当前结果面板指向的任务（tools-slides.js 维护）
  const page = { currentJobId: null, modality: null, baseName: null };

  function uploadFileName(job) {
    const base = (job.source && job.source.name || 'slide')
      .replace(/\.(kfb|kfbf)$/i, '');
    return (job.modality === 'fluorescence') ? `${base}.ome.tif` : `${base}.tif`;
  }

  /// OPFS 任务记录持久化适配器：把引擎的 save/complete/remove 映射为
  /// record.upload（经 runner.setJobUpload，双槽记录串行写）。
  function opfsStorage(jobId, filename, size, acct) {
    return {
      save(rec) {
        if (!rec || !rec.job_id) return undefined;
        return queueWrite(() => runner.setJobUpload(jobId, {
          ingestionId: rec.job_id, filename, size, state: 'uploading',
          confirmedParts: rec.confirmed || [],
          slideId: rec.slide_id || null, error: null,
          account: acct.account, accountLabel: acct.label,
        }));
      },
      complete(id, outcome) {
        if (outcome && outcome.succeeded) {
          return queueWrite(() => runner.setJobUpload(jobId, {
            ingestionId: id, filename, size, state: 'published',
            slideId: (outcome && outcome.slide_id) || null, error: null,
          }));
        }
        return queueWrite(() => runner.setJobUpload(jobId, {
          ingestionId: id, filename, size, state: 'failed',
          error: (outcome && outcome.fail_code) || 'terminal',
        }));
      },
      remove(id) {
        return queueWrite(() => runner.setJobUpload(jobId, {
          ingestionId: id, filename, size, state: 'cancelled',
        }));
      },
      // 续传起点：任务记录里上次已确认的分块（服务端 ListParts 才是权威）
      readConfirmed(id) {
        return runner.getJob(jobId).then((job) => (
          job && job.upload && job.upload.ingestionId === id
            ? (job.upload.confirmedParts || []) : []));
      },
    };
  }

  function stageText(body) {
    if (!body) return '';
    const key = `upload.cos.stage.${body.stage}`;
    const s = t(key);
    if (s === key) return String(body.stage || '');
    if (body.stage === 'waiting_space' && typeof body.queue_position === 'number') {
      return `${s} · ${t('upload.cos.queue', { n: body.queue_position + 1 })}`;
    }
    return s;
  }

  function handleFailure(err, jobId) {
    if (err && err.persist) {
      if (err.reconciled) {
        setMsg(jobId, 'tools.upload.persist.failed');
      } else {
        rememberOrphan(jobId, err.ingestionId);
        setMsg(jobId, 'tools.upload.persist.orphan');
      }
      return;
    }
    if (isAuthError(err)) {
      setMsgWithLink(jobId, 'tools.upload.login.expired', LOGIN_URL, 'tools.upload.login.link');
      return;
    }
    if (err && err.network) {
      setMsg(jobId, 'tools.upload.resume.hint');
      return;
    }
    if (err && err.terminal) {
      const code = (err.data && err.data.fail_code) || '';
      setMsg(jobId, 'tools.upload.failed.terminal', {
        code: (typeof code === 'string' && STABLE_CODE_RE.test(code)) ? code : '—',
      });
      return;
    }
    if (err && typeof err.part === 'number') {
      setMsg(jobId, 'tools.upload.failed.part', { n: err.part });
      return;
    }
    const d = describeUploadError(err);
    if (d.disable) {
      state.disabled[jobId] = true;
      const btn = document.getElementById('upload-btn');
      if (btn && page.currentJobId === jobId) btn.disabled = true;
    }
    if (d.key === 'tools.upload.too.large') {
      setMsg(jobId, d.key, { max: d.max === null ? '—' : fmtBytes(d.max) });
      return;
    }
    setMsg(jobId, d.key, d.vars);
  }

  // 创建后没能记下 id、且取消未获确认的服务端任务：同一任务再建之前必须先确认
  // 取消它。本地记录写不进去时，内存 + localStorage 是仅剩的去处（尽力）。
  const ORPHANS_KEY = 'pt.tools.upload.orphans';

  function readOrphans() {
    let stored = {};
    try { stored = JSON.parse(localStorage.getItem(ORPHANS_KEY) || '{}') || {}; } catch { stored = {}; }
    return { ...stored, ...state.orphans };
  }

  function writeOrphans(map) {
    state.orphans = map;
    try {
      if (Object.keys(map).length) localStorage.setItem(ORPHANS_KEY, JSON.stringify(map));
      else localStorage.removeItem(ORPHANS_KEY);
    } catch { /* memory copy still guards this tab */ }
  }

  function rememberOrphan(jobId, ingestionId) {
    writeOrphans({ ...readOrphans(), [jobId]: ingestionId });
  }

  async function settleOrphan(jobId) {
    const map = readOrphans();
    const id = map[jobId];
    if (!id) return true;
    let ok = false;
    try {
      const r = await pageApiFetch(`/api/ingestions/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
      ok = r.ok || r.status === 404 || r.status === 409;
    } catch { ok = false; }
    if (ok) {
      delete map[jobId];
      writeOrphans(map);
    }
    return ok;
  }

  /// 已发布任务的结果面板：上传按钮换成发布结果（slide id + 工作台链接）；
  /// 目标项目关联未完成时附「重试加入项目」（只重试关联）。
  function showPublished(jobId, slideId, intent, { assocRetry = true } = {}) {
    state.msgs[jobId] = { published: { slideId, intent, assocRetry } };
    const btn = document.getElementById('upload-btn');
    if (btn && page.currentJobId === jobId) btn.hidden = true;
    paint(jobId);
  }

  function paintPublished(el, jobId, { slideId, intent, assocRetry }) {
    el.appendChild(document.createTextNode(
      t('tools.upload.published', { id: slideId || '—' })));
    el.appendChild(document.createTextNode(' '));
    const a = document.createElement('a');
    a.href = '/app';
    a.textContent = t('tools.upload.open.workbench');
    el.appendChild(a);
    if (assocRetry && intent && intent.state === 'pending' && intent.target && onPublished) {
      el.appendChild(document.createTextNode(' '));
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-secondary';
      b.id = 'upload-assoc-retry-btn';
      b.dataset.action = 'assoc-retry';
      b.textContent = t('tools.upload.assoc.retry');
      b.addEventListener('click', () => { startOrContinue(jobId); });
      el.appendChild(b);
    }
  }

  /// 无归属字段的遗留上传记录：按服务端归属探测（他人的任务 403）。
  async function probeIngestionAccess(ingestionId) {
    try {
      const r = await pageApiFetch(`/api/ingestions/${encodeURIComponent(ingestionId)}`);
      if (r.status === 403) return 'forbidden';
      if (r.status === 401) return 'auth';
    } catch { /* 网络问题交给续传路径处理 */ }
    return 'ok';
  }

  /// 账号不一致时的显式选择：'original'（退出后用原账号登录）/
  /// 'separate'（为当前账号另起上传）/ 'cancel'。
  function chooseAccountAction({ ownerLabel, currentLabel, inflight }) {
    const dlg = document.getElementById('account-dialog');
    if (!dlg || typeof dlg.showModal !== 'function') return Promise.resolve('cancel');
    const vars = {
      owner: ownerLabel || t('tools.account.unknown'),
      current: currentLabel || t('tools.account.unknown'),
    };
    const title = document.getElementById('account-dialog-title');
    const body = document.getElementById('account-dialog-body');
    if (title) title.textContent = t('tools.account.title');
    if (body) {
      body.textContent = t(inflight ? 'tools.account.body.upload'
        : 'tools.account.body.intent', vars);
    }
    return new Promise((resolve) => {
      const onClose = () => {
        dlg.removeEventListener('close', onClose);
        resolve(dlg.returnValue || 'cancel');
      };
      dlg.returnValue = '';
      dlg.addEventListener('close', onClose);
      dlg.showModal();
    });
  }

  /// 「用原账号登录」：先退出当前会话（登录页对已登录会话直接跳走），
  /// 本地任务与上传记录都保留，登录后在任务列表继续。
  async function signOutForOriginal() {
    try {
      await pageApiFetch('/logout', { method: 'POST', redirect: 'manual' });
    } catch { /* 登录页仍可手动退出 */ }
    window.location.assign(LOGIN_URL);
  }

  /// 取消被另起上传取代的旧 ingestion——只有它的原账号有权取消（服务端
  /// 归属检查）；成功（或服务端已无/已终态）后从待清理列表里标记完成。
  async function cancelSuperseded(jobId, ingestionId) {
    await selectJob(jobId);
    let cap;
    try {
      cap = await fetchCapability();
    } catch {
      setMsg(jobId, 'tools.upload.offline');
      return;
    }
    if (cap.authRequired) {
      setMsgWithLink(jobId, 'tools.upload.login.required', LOGIN_URL, 'tools.upload.login.link');
      return;
    }
    const job = await runner.getJob(jobId).catch(() => null);
    const list = (job && job.upload && job.upload.superseded) || [];
    const entry = list.find((s) => s.ingestionId === ingestionId);
    if (!entry) return;
    const owner = entry.accountLabel || t('tools.account.unknown');
    if (entry.account && cap.account !== entry.account) {
      setMsg(jobId, 'tools.upload.superseded.wrong', { owner });
      return;
    }
    let ok = false;
    let status = 0;
    try {
      const r = await pageApiFetch(
        `/api/ingestions/${encodeURIComponent(ingestionId)}/cancel`, { method: 'POST' });
      status = r.status;
      ok = r.ok || r.status === 404 || r.status === 409;
    } catch { ok = false; }
    if (!ok) {
      setMsg(jobId, status === 403 ? 'tools.upload.superseded.wrong'
        : 'tools.upload.superseded.failed', { owner });
      return;
    }
    await queueWrite(() => runner.setJobUpload(jobId, {
      superseded: list.map((s) => (s.ingestionId === ingestionId
        ? { ...s, state: 'cancelled', cancelledAt: E.nowIso() } : s)),
    })).catch(() => { /* 下次再点 */ });
    setMsg(jobId, 'tools.upload.superseded.cancelled');
    if (onJobsRefresh) onJobsRefresh();
  }

  /// 上传/继续上传（唯一入口）。本标签 busy 守卫 + 跨标签 Web Lock：同一任务
  /// 任何时刻只有一处在上传，重复点击/多标签都不会并发创建 ingestion。
  async function startOrContinue(jobId) {
    // 反馈写在结果面板里：从任务列表（含刷新后）发起时先把面板切到该任务
    await selectJob(jobId);
    if (state.busyJobId) {
      if (state.busyJobId !== jobId) setMsg(jobId, 'tools.upload.busy.other');
      return null;
    }
    if (state.disabled[jobId]) {
      paint(jobId);
      return null;
    }
    state.busyJobId = jobId;
    let outcome = null;
    const ran = await navigator.locks.request(E.uploadLockName(jobId), { ifAvailable: true },
      async (lock) => {
        if (!lock) return false;
        outcome = await runLocked(jobId);
        return true;
      });
    if (!ran) {
      state.busyJobId = null;
      setMsg(jobId, 'tools.upload.other.tab');
      return { ok: false, reason: 'other-tab' };
    }
    return outcome || { ok: false };
  }

  /// 结果面板切到该任务（页面回调负责显示面板）；已指向时不动。
  async function selectJob(jobId) {
    if (page.currentJobId === jobId || !onSelectJob) return;
    try { await onSelectJob(jobId); } catch { /* 面板切不过去时反馈仍按任务保存 */ }
  }

  async function runLocked(jobId) {
    setBusy(jobId, true);
    if (onJobsRefresh) onJobsRefresh();   // 行立即反映“上传中”（删除禁用）
    try {
      const job = await runner.getJob(jobId);
      if (!job || !['ready', 'exported'].includes(job.state)) return;

      // ⓪ 已发布的任务绝不再建 ingestion、再传文件（在锁内读记录：另一标签
      //    刚发布的也算）。目标项目关联仍未完成时只重试关联。
      if (job.upload && job.upload.state === 'published') {
        const sid = job.upload.slideId || null;
        const it = job.intent;
        const assocPending = !!(it && it.state === 'pending' && it.target && onPublished);
        showPublished(jobId, sid, it, { assocRetry: !assocPending });
        if (assocPending) {
          try { await onPublished(jobId, sid); } catch { /* 原因已由回调显示 */ }
          const after = await runner.getJob(jobId).catch(() => null);
          showPublished(jobId, sid, after && after.intent);
        }
        return { ok: true, slideId: sid, alreadyPublished: true };
      }

      // ① 能力（点击后才拉取——之前零 /api/ 请求）
      let cap;
      try {
        cap = await fetchCapability();
      } catch (e) {
        setMsg(jobId, 'tools.upload.offline');
        return;
      }
      if (cap.authRequired) {
        setMsgWithLink(jobId, 'tools.upload.login.required', LOGIN_URL, 'tools.upload.login.link');
        return;
      }
      // ①b 账号归属。进行中的上传属于创建它的账号——服务端 ingestion 的
      //     归属与容量记账都在那个账号上，换账号续传要么被拒（403），要么
      //     （管理员）替别人续传。待执行的自动上传意图同样绑定授权账号。
      //     当前登录不同：不续传、不改旧上传的归属，只能退出后用原账号登录，
      //     或显式为当前账号另起一个上传（旧上传原样记入 superseded，
      //     由原账号取消或到期由服务端清理）。
      const acct = {
        account: typeof cap.account === 'string' ? cap.account : '',
        label: typeof cap.account_label === 'string' ? cap.account_label : '',
      };
      let prev = job.upload;
      const intent = job.intent;
      const inflight = !!(prev && prev.ingestionId
        && !TERMINAL_UPLOAD_STATES.includes(prev.state));
      let owner = null;
      let ownerLabel = '';
      if (inflight && prev.account) {
        owner = prev.account;
        ownerLabel = prev.accountLabel || '';
      } else if (!inflight && intent && intent.state === 'pending' && intent.account) {
        owner = intent.account;
        ownerLabel = intent.accountLabel || '';
      }
      let mismatch = owner !== null && owner !== acct.account;
      if (!mismatch && inflight && !prev.account) {
        // 归属字段之前的记录：以服务端归属为准（他人的任务 403）
        const probe = await probeIngestionAccess(prev.ingestionId);
        if (probe === 'auth') {
          setMsgWithLink(jobId, 'tools.upload.login.required', LOGIN_URL, 'tools.upload.login.link');
          return;
        }
        mismatch = probe === 'forbidden';
      }
      if (mismatch) {
        const choice = await chooseAccountAction({
          ownerLabel, currentLabel: acct.label, inflight,
        });
        if (choice === 'original') {
          await signOutForOriginal();
          return { ok: false, reason: 'account-original' };
        }
        if (choice !== 'separate') {
          setMsg(jobId, 'tools.upload.account.changed');
          return { ok: false, reason: 'account-mismatch' };
        }
        if (inflight) {
          const superseded = (prev.superseded || []).concat([{
            ingestionId: prev.ingestionId,
            account: prev.account || owner || '',
            accountLabel: prev.accountLabel || ownerLabel || '',
            state: 'open',
            supersededAt: E.nowIso(),
          }]);
          await queueWrite(() => runner.setJobUpload(jobId, {
            ingestionId: null, state: null, confirmedParts: [], slideId: null,
            error: null, account: acct.account, accountLabel: acct.label,
            superseded,
          }));
          prev = null;
        }
        if (intent && intent.state === 'pending') {
          await runner.setJobIntent(jobId, {
            account: acct.account, accountLabel: acct.label,
            reconfirmedAt: E.nowIso(),
          });
        }
      }
      const cfg = window.HP_COS_UPLOAD
        ? window.HP_COS_UPLOAD.resolveConfig(cap.cos_upload) : null;
      if (!cfg) {
        setMsg(jobId, 'tools.upload.capability.off');
        return;
      }
      // ② 限额按最终文件（不是源文件）
      const outBytes = (job.result && job.result.outputBytes) || 0;
      if (outBytes > cfg.max_size_bytes) {
        state.disabled[jobId] = true;
        setMsg(jobId, 'tools.upload.too.large', { max: fmtBytes(cfg.max_size_bytes) });
        return;
      }
      // ③ 平台能否查看该输出（按核心 result.format 键控，非扩展名）
      const fmt = (job.result && job.result.format) || '';
      if (!fmt || !(cap.viewable_formats || []).includes(fmt)) {
        state.disabled[jobId] = true;
        setMsg(jobId, 'tools.upload.format.unsupported');
        return;
      }

      // ④ 复用既有 ingestion：仅在无记录或非终态时续传；终态（含取消/失败）
      //    后的新建只能来自这里的显式点击
      let resumeJobId = null;
      if (prev && prev.ingestionId &&
          !TERMINAL_UPLOAD_STATES.includes(prev.state)) {
        resumeJobId = prev.ingestionId;
        setMsg(jobId, resumeJobId ? 'tools.upload.resuming' : 'tools.upload.working');
      } else {
        if (!(await settleOrphan(jobId))) {
          setMsg(jobId, 'tools.upload.orphan.pending');
          return;
        }
        setMsg(jobId, 'tools.upload.working');
      }

      // ⑤ 产物只经 slice() 分块读取（引擎内绝不整体物化）
      const file = await runner.artifactView(jobId);
      const name = uploadFileName(job);
      const source = {
        name,
        size: file.size,
        slice: (s, e) => file.slice(s, e),
      };
      const upload = window.HP_COS_UPLOAD.createUpload({
        source,
        apiFetch: pageApiFetch,
        config: cfg,
        storage: opfsStorage(jobId, name, file.size, acct),
        resumeJobId,
        skipConfirm: true,
        confirmResume: () => true,
        // 工具页语义（计划 §9 C4）：完成响应丢失重发（409 冲突=已完成过）；
        // 续传时服务端已离开 uploading 则先 /resume。
        retryCompleteOnNetworkError: true,
        useResumeEndpoint: true,
        onEvent: (ev) => {
          if (ev.type === 'status') {
            const txt = stageText(ev.body);
            if (txt) setStageTxt(jobId, txt);
          } else if (ev.type === 'progress' && typeof ev.frac === 'number') {
            setStageTxt(jobId, `${t('upload.cos.stage.uploading')} ${Math.round(ev.frac * 100)}%`);
          } else if (ev.type === 'created') {
            setStageTxt(jobId, t('upload.cos.stage.uploading'));
            // 记录已排队写入：列表行切到“上传中/继续上传”形态
            queueRefresh();
          }
        },
      });
      state.handle = upload;
      const r = await upload.done;
      state.handle = null;
      if (r && r.cancelled) {
        setMsg(jobId, 'tools.upload.cancelled');
        return;
      }
      // published：先冲刷记录写入（storage.complete 已排队），再读回填的
      // slide id；R1 意图收口（done 标记 + 工作台目标关联 + 打开者通知）在
      // onPublished 回调里做——那里按意图当前状态幂等处理
      await Promise.race([state.writes, new Promise((r) => setTimeout(r, 3000))]);
      const after = await runner.getJob(jobId);
      const sid = (after && after.upload && after.upload.slideId) || null;
      showPublished(jobId, sid, after && after.intent, { assocRetry: false });
      // R1：发布收口回调（页面用它做工作台目标的关联与打开者通知）
      if (onPublished) {
        try { await onPublished(jobId, sid); } catch { /* 关联失败已明示 */ }
        const settled = await runner.getJob(jobId).catch(() => null);
        showPublished(jobId, sid, settled && settled.intent);
      }
      return { ok: true, slideId: sid };
    } catch (err) {
      handleFailure(err, jobId);
    } finally {
      setBusy(jobId, false);
      state.handle = null;
      // 收口后先冲刷记录写入再刷新列表（行上传状态不落后于最终态）
      await Promise.race([state.writes, new Promise((r) => setTimeout(r, 3000))]);
      if (onJobsRefresh) onJobsRefresh();
    }
  }

  function setStageTxt(jobId, txt) {
    state.msgs[jobId] = { text: txt };
    paint(jobId);
  }

  /// 记录写入落盘后刷新列表（行上传状态/继续按钮跟随）。
  function queueRefresh() {
    queueWrite(() => Promise.resolve()).then(() => {
      if (onJobsRefresh) onJobsRefresh();
    });
  }

  function fmtBytes(n) {
    if (!Number.isFinite(n)) return '—';
    if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(2)} GiB`;
    if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(1)} MiB`;
    if (n >= 2 ** 10) return `${(n / 2 ** 10).toFixed(1)} KiB`;
    return `${n} B`;
  }

  /// 引擎 cancel 会立即结束 done（进行中的等待一并结束），runLocked 随之
  /// 走完 finally：清 busy、冲刷记录写入、释放上传锁、刷新列表。
  /// R1 扩展：上传尚未开始（转换后、上传前的停止态）时，cancel 撤销待执行
  /// 的自动上传意图（无 ingestion、本地产物保留）——「取消停止当前阶段并
  /// 撤销后续自动上传意图」（drain 计划 §3.1）。
  function cancel() {
    const jobId = page.currentJobId;
    if (!jobId) return;
    if (state.handle && state.busyJobId === jobId) {
      state.handle.cancel();
      setMsg(jobId, 'tools.upload.cancelled');
      return;
    }
    runner.getJob(jobId).then((job) => {
      const pendingIntent = job && job.intent && job.intent.state === 'pending';
      const up = job && job.upload;
      // 已发布的任务没有「后续自动上传」可撤销（关联重试不在此列）
      if (up && up.state === 'published') return;
      const uploadSettled = !up || !up.ingestionId
        || TERMINAL_UPLOAD_STATES.includes(up.state);
      if (!pendingIntent || !uploadSettled) return;
      return runner.setJobIntent(jobId, {
        state: 'revoked', revokedReason: 'user_cancel', revokedAt: E.nowIso(),
      }).then(() => {
        setMsg(jobId, 'tools.upload.intent.revoked');
        if (onJobsRefresh) onJobsRefresh();
      });
    }).catch(() => { /* 记录写不进去时按钮仍可用（下次再试） */ });
  }

  /// 任务列表行：上传状态 + 继续/上传按钮 + 删除禁用（上传进行中）。
  function renderRowSegment(job, actionsEl, discardBtn) {
    const up = job.upload;
    const isActive = state.busyJobId === job.id;
    if (discardBtn) {
      // 本标签正在上传才禁用；遗留的未收口记录（关掉的标签）可在确认后放弃
      discardBtn.disabled = isActive;
      discardBtn.title = isActive ? t('tools.upload.discard.blocked') : '';
    }
    renderSuperseded(job, actionsEl);
    if (up && up.state === 'published') {
      const p = document.createElement('p');
      p.className = 'job-upload-state ok';
      p.dataset.uploadState = 'published';
      const a = document.createElement('a');
      a.href = '/app';
      a.textContent = t('tools.upload.row.published', {
        id: up.slideId || '—',
      });
      p.appendChild(document.createTextNode(t('tools.upload.row.published.prefix') + ' '));
      p.appendChild(a);
      actionsEl.parentNode.insertBefore(p, actionsEl);
      // 已发布但目标项目关联未完成（关联失败/发布后标签关闭）：点击重试
      const it = job.intent;
      if (it && it.state === 'pending' && it.target && onPublished) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-secondary';
        btn.dataset.action = 'assoc-retry';
        btn.textContent = t('tools.upload.assoc.retry');
        btn.disabled = isActive;
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          try {
            await onPublished(job.id, up.slideId || null);
          } catch { /* 失败原因已由回调显示 */ }
          if (onJobsRefresh) onJobsRefresh();
        });
        actionsEl.appendChild(btn);
      }
      return;
    }
    if (!up || !up.ingestionId || TERMINAL_UPLOAD_STATES.includes(up.state)) {
      if (up && (up.state === 'failed' || up.state === 'cancelled')) {
        const p = document.createElement('p');
        p.className = 'job-upload-state err';
        p.dataset.uploadState = up.state;
        p.textContent = t(up.state === 'failed'
          ? 'tools.upload.row.failed' : 'tools.upload.row.cancelled',
        { err: up.error || '' });
        actionsEl.parentNode.insertBefore(p, actionsEl);
      }
      // R1：待执行的自动上传意图（转换后停止/刷新恢复）→「继续上传」，
      // 与无意图的手动「上传到工作台」区分（点击仍是同一续传入口）。
      const intentPending = !!(job.intent && job.intent.state === 'pending');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-secondary';
      btn.dataset.action = intentPending ? 'upload-continue' : 'upload';
      btn.dataset.jobUpload = job.id;
      btn.textContent = t(intentPending
        ? 'tools.upload.continue' : 'tools.upload.btn');
      btn.disabled = !!state.disabled[job.id] || state.busyJobId !== null;
      if (state.busyJobId !== null && !isActive) btn.title = t('tools.upload.busy.other');
      btn.addEventListener('click', () => { startOrContinue(job.id); });
      actionsEl.appendChild(btn);
      return;
    }
    // 非终态（含刷新后的中断记录）：展示状态 + 继续上传
    const p = document.createElement('p');
    p.className = 'job-upload-state run';
    p.dataset.uploadState = up.state || 'uploading';
    p.textContent = t(isActive ? 'tools.upload.row.active' : 'tools.upload.row.pending');
    actionsEl.parentNode.insertBefore(p, actionsEl);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-secondary';
    btn.dataset.action = 'upload-continue';
    btn.dataset.jobUpload = job.id;
    btn.textContent = t('tools.upload.continue');
    btn.disabled = state.busyJobId !== null;
    if (state.busyJobId !== null && !isActive) btn.title = t('tools.upload.busy.other');
    btn.addEventListener('click', () => { startOrContinue(job.id); });
    actionsEl.appendChild(btn);
    if (isActive) {
      // 进行中：进度与取消在结果面板里——给出切过去的入口
      const show = document.createElement('button');
      show.type = 'button';
      show.className = 'btn btn-secondary';
      show.dataset.action = 'upload-show';
      show.textContent = t('tools.upload.show.progress');
      show.addEventListener('click', () => { selectJob(job.id); });
      actionsEl.appendChild(show);
    }
  }

  /// 被另起上传取代、仍未确认清理的旧 ingestion：提示归属账号 + 取消入口
  /// （只有原账号能取消；否则服务端到期清理）。
  function renderSuperseded(job, actionsEl) {
    const open = ((job.upload && job.upload.superseded) || [])
      .filter((s) => s.state === 'open');
    for (const s of open) {
      const p = document.createElement('p');
      p.className = 'job-upload-state warn';
      p.dataset.superseded = s.ingestionId;
      p.textContent = t('tools.upload.superseded', {
        owner: s.accountLabel || t('tools.account.unknown'),
      });
      p.appendChild(document.createTextNode(' '));
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-secondary';
      b.dataset.action = 'superseded-cancel';
      b.dataset.ingestion = s.ingestionId;
      b.textContent = t('tools.upload.superseded.cancel');
      b.addEventListener('click', async () => {
        b.disabled = true;
        await cancelSuperseded(job.id, s.ingestionId);
        b.disabled = false;
      });
      p.appendChild(b);
      actionsEl.parentNode.insertBefore(p, actionsEl);
    }
  }

  /// 放弃遗留的未收口上传（其标签已关闭）：尽力通知服务端取消（未登录/离线
  /// 时失败即忽略，服务端等待超时会兜底），本地随后可删除。被取代的旧上传
  /// 一并尽力取消（非其账号时服务端拒绝，到期清理兜底）。
  async function abandon(job) {
    const up = job.upload || {};
    const ids = [];
    if (up.ingestionId && !TERMINAL_UPLOAD_STATES.includes(up.state)) ids.push(up.ingestionId);
    for (const s of (up.superseded || [])) {
      if (s.state === 'open' && s.ingestionId) ids.push(s.ingestionId);
    }
    for (const id of ids) {
      try {
        await pageApiFetch(`/api/ingestions/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
      } catch { /* best effort */ }
    }
  }

  function rerenderForLang() {
    paint(page.currentJobId);
  }

  /// 结果面板切到某任务：已发布的任务不给上传按钮，只显示发布结果。
  function setResultJob(jobId) {
    const changed = page.currentJobId !== jobId;
    page.currentJobId = jobId;
    const btn = document.getElementById('upload-btn');
    if (btn) {
      btn.disabled = state.busyJobId !== null || !!state.disabled[jobId];
      if (changed) btn.hidden = false;
    }
    if (changed) paint(jobId);
    return runner.getJob(jobId).then((job) => {
      if (page.currentJobId !== jobId || !job) return;
      const up = job.upload;
      if (up && up.state === 'published') {
        showPublished(jobId, up.slideId || null, job.intent);
      } else if (btn) {
        btn.hidden = false;
      }
    }).catch(() => { /* 记录读不到：保持按钮，控制器内仍有发布守卫 */ });
  }

  return {
    startOrContinue, cancel, abandon, renderRowSegment, rerenderForLang, setResultJob,
    isBusy: () => state.busyJobId !== null,
    busyJobId: () => state.busyJobId,
    isDisabled: (jobId) => !!state.disabled[jobId],
  };
}
