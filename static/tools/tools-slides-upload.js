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

export function createUploadController({
  runner, t, onJobsRefresh, onPublished,
}) {
  const state = {
    busyJobId: null,     // 本标签唯一进行中的上传
    handle: null,        // 引擎句柄（取消用）
    disabled: {},        // jobId -> true（超限/格式不支持：按钮保持禁用）
    statusKey: null, statusVars: null,
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

  function setMsg(key, vars) {
    state.statusKey = key;
    state.statusVars = vars || null;
    const el = document.getElementById('upload-status');
    if (!el) return;
    el.textContent = '';
    if (key === null) return;
    el.textContent = t(key, vars || {});
  }

  /// 登录链接 / 工作台链接是消息的一部分（DOM 构造，不用 innerHTML）。
  function setMsgWithLink(key, linkHref, linkKey, vars) {
    const el = document.getElementById('upload-status');
    if (!el) return;
    el.textContent = '';
    el.appendChild(document.createTextNode(t(key, vars || {})));
    el.appendChild(document.createTextNode(' '));
    const a = document.createElement('a');
    a.href = linkHref;
    a.textContent = t(linkKey);
    el.appendChild(a);
    state.statusKey = key;
    state.statusVars = vars || null;
  }

  function setBusy(jobId, busy) {
    state.busyJobId = busy ? jobId : null;
    const btn = document.getElementById('upload-btn');
    const cancelBtn = document.getElementById('upload-cancel-btn');
    if (btn && page.currentJobId === jobId) {
      btn.disabled = busy || !!state.disabled[jobId];
    }
    if (cancelBtn) cancelBtn.hidden = !busy;
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
  function opfsStorage(jobId, filename, size) {
    return {
      save(rec) {
        if (!rec || !rec.job_id) return undefined;
        return queueWrite(() => runner.setJobUpload(jobId, {
          ingestionId: rec.job_id, filename, size, state: 'uploading',
          confirmedParts: rec.confirmed || [],
          slideId: rec.slide_id || null, error: null,
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
        setMsg('tools.upload.persist.failed');
      } else {
        rememberOrphan(jobId, err.ingestionId);
        setMsg('tools.upload.persist.orphan');
      }
      return;
    }
    if (isAuthError(err)) {
      setMsgWithLink('tools.upload.login.expired', LOGIN_URL, 'tools.upload.login.link');
      return;
    }
    if (err && err.network) {
      setMsg('tools.upload.resume.hint');
      return;
    }
    if (err && err.terminal) {
      const code = (err.data && err.data.fail_code) || '';
      setMsg('tools.upload.failed.terminal', { code: code || '—' });
      return;
    }
    if (err && typeof err.part === 'number') {
      setMsg('tools.upload.failed.part', { n: err.part });
      return;
    }
    const raw = E.errText(err);
    setMsg('tools.upload.failed', { e: raw });
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

  /// 上传/继续上传（唯一入口）。本标签 busy 守卫 + 跨标签 Web Lock：同一任务
  /// 任何时刻只有一处在上传，重复点击/多标签都不会并发创建 ingestion。
  async function startOrContinue(jobId) {
    if (state.busyJobId) return null;
    if (state.disabled[jobId]) return null;
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
      setMsg('tools.upload.other.tab');
      return { ok: false, reason: 'other-tab' };
    }
    return outcome || { ok: false };
  }

  async function runLocked(jobId) {
    setBusy(jobId, true);
    if (onJobsRefresh) onJobsRefresh();   // 行立即反映“上传中”（删除禁用）
    try {
      const job = await runner.getJob(jobId);
      if (!job || !['ready', 'exported'].includes(job.state)) return;

      // ① 能力（点击后才拉取——之前零 /api/ 请求）
      let cap;
      try {
        cap = await fetchCapability();
      } catch (e) {
        setMsg('tools.upload.offline');
        return;
      }
      if (cap.authRequired) {
        setMsgWithLink('tools.upload.login.required', LOGIN_URL, 'tools.upload.login.link');
        return;
      }
      // ①b R1 意图绑定账号核对：任务挂着待执行的上传意图，但当前登录已是
      //     另一账号——绝不静默换账号上传；须显式确认后才重绑并继续（drain
      //     计划 §3.1「重新登录为另一账号……须重新确认，不能自动替换」）。
      const intent = job.intent;
      if (intent && intent.state === 'pending' && typeof cap.account === 'string'
          && intent.account && cap.account !== intent.account) {
        const ok = window.confirm(t('tools.upload.account.changed.confirm'));
        if (!ok) {
          setMsg('tools.upload.account.changed');
          return;
        }
        await runner.setJobIntent(jobId, {
          account: cap.account, reconfirmedAt: E.nowIso(),
        });
      }
      const cfg = window.HP_COS_UPLOAD
        ? window.HP_COS_UPLOAD.resolveConfig(cap.cos_upload) : null;
      if (!cfg) {
        setMsg('tools.upload.capability.off');
        return;
      }
      // ② 限额按最终文件（不是源文件）
      const outBytes = (job.result && job.result.outputBytes) || 0;
      if (outBytes > cfg.max_size_bytes) {
        state.disabled[jobId] = true;
        setMsg('tools.upload.too.large', { max: fmtBytes(cfg.max_size_bytes) });
        return;
      }
      // ③ 平台能否查看该输出（按核心 result.format 键控，非扩展名）
      const fmt = (job.result && job.result.format) || '';
      if (!fmt || !(cap.viewable_formats || []).includes(fmt)) {
        state.disabled[jobId] = true;
        setMsg('tools.upload.format.unsupported');
        return;
      }

      // ④ 复用既有 ingestion：仅在无记录或非终态时续传；终态（含取消/失败）
      //    后的新建只能来自这里的显式点击
      const prev = job.upload;
      let resumeJobId = null;
      if (prev && prev.ingestionId &&
          !TERMINAL_UPLOAD_STATES.includes(prev.state)) {
        resumeJobId = prev.ingestionId;
        setMsg(resumeJobId ? 'tools.upload.resuming' : 'tools.upload.working');
      } else {
        if (!(await settleOrphan(jobId))) {
          setMsg('tools.upload.orphan.pending');
          return;
        }
        setMsg('tools.upload.working');
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
        storage: opfsStorage(jobId, name, file.size),
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
            if (txt) setStageTxt(txt);
          } else if (ev.type === 'progress' && typeof ev.frac === 'number') {
            setStageTxt(`${t('upload.cos.stage.uploading')} ${Math.round(ev.frac * 100)}%`);
          } else if (ev.type === 'created') {
            setStageTxt(t('upload.cos.stage.uploading'));
            // 记录已排队写入：列表行切到“上传中/继续上传”形态
            queueRefresh();
          }
        },
      });
      state.handle = upload;
      const r = await upload.done;
      state.handle = null;
      if (r && r.cancelled) {
        setMsg('tools.upload.cancelled');
        return;
      }
      // published：先冲刷记录写入（storage.complete 已排队），再读回填的
      // slide id；R1 意图收口（done 标记 + 工作台目标关联 + 打开者通知）在
      // onPublished 回调里做——那里按意图当前状态幂等处理
      await Promise.race([state.writes, new Promise((r) => setTimeout(r, 3000))]);
      const after = await runner.getJob(jobId);
      const sid = after && after.upload && after.upload.slideId;
      const el = document.getElementById('upload-status');
      if (el) {
        el.textContent = '';
        el.appendChild(document.createTextNode(
          t('tools.upload.published', { id: sid || '—' })));
        el.appendChild(document.createTextNode(' '));
        const a = document.createElement('a');
        a.href = '/app';
        a.textContent = t('tools.upload.open.workbench');
        el.appendChild(a);
      }
      state.statusKey = 'tools.upload.published';
      state.statusVars = { id: sid || '—' };
      // R1：发布收口回调（页面用它做工作台目标的关联与打开者通知）
      if (onPublished) {
        try { await onPublished(jobId, sid || null); } catch { /* 关联失败已明示 */ }
      }
      return { ok: true, slideId: sid || null };
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

  function setStageTxt(txt) {
    const el = document.getElementById('upload-status');
    if (el) el.textContent = txt;
    state.statusKey = null;
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
    if (state.handle) {
      state.handle.cancel();
      setMsg('tools.upload.cancelled');
      return;
    }
    const jobId = page.currentJobId;
    if (!jobId) return;
    runner.getJob(jobId).then((job) => {
      const pendingIntent = job && job.intent && job.intent.state === 'pending';
      const uploadSettled = !job || !job.upload
        || TERMINAL_UPLOAD_STATES.includes(job.upload.state);
      if (!pendingIntent || !uploadSettled) return;
      return runner.setJobIntent(jobId, {
        state: 'revoked', revokedReason: 'user_cancel', revokedAt: E.nowIso(),
      }).then(() => {
        setMsg('tools.upload.intent.revoked');
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
    if (!up || TERMINAL_UPLOAD_STATES.includes(up.state)) {
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
      btn.disabled = !!state.disabled[job.id] || isActive;
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
    btn.disabled = isActive;
    btn.addEventListener('click', () => { startOrContinue(job.id); });
    actionsEl.appendChild(btn);
  }

  /// 放弃遗留的未收口上传（其标签已关闭）：尽力通知服务端取消（未登录/离线
  /// 时失败即忽略，服务端等待超时会兜底），本地随后可删除。
  async function abandon(job) {
    const id = job.upload && job.upload.ingestionId;
    if (!id) return;
    try {
      await pageApiFetch(`/api/ingestions/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
    } catch { /* best effort */ }
  }

  function rerenderForLang() {
    if (state.statusKey) setMsg(state.statusKey, state.statusVars);
  }

  function setResultJob(jobId) {
    page.currentJobId = jobId;
    const btn = document.getElementById('upload-btn');
    if (btn) btn.disabled = state.busyJobId !== null;
  }

  return {
    startOrContinue, cancel, abandon, renderRowSegment, rerenderForLang, setResultJob,
    isBusy: () => state.busyJobId !== null,
    isDisabled: (jobId) => !!state.disabled[jobId],
  };
}
