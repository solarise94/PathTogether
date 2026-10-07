// tools-slides-direct-upload.js — 阶段 1（先转换后上传）：直传类别文件的
// 「上传到工作台」控制器（/tools/slides）。
//
// 场景（docs/slide-tools/upload-convert-first-phase1.md §5）：
//   - 用户在本页选择/拖入 OME-TIFF 或转换器 BigTIFF（或工作台交接来的此类
//     文件）——无需转换，数据源就是用户的 File；
//   - 复用共享 COS 引擎（window.HP_COS_UPLOAD）与能力判定语义
//     （fetchCapability/describeUploadError 来自 tools-slides-upload.js）：
//     登录（401 不跳转）/账号绑定（capability.account）/大小上限/格式受理
//     检查保持不变；
//   - 创建 ingestion 携带 direct_class 声明（嗅探结果），worker 在
//     open_slide 前头级核验；
//   - 「上传到工作台」按钮点击之前零 /api 请求（C3 隐私合同不变）；
//   - 工作台交接目标（project / newProject）在发布成功后关联（权限检查在
//     服务端）。
'use strict';

import { fetchCapability, describeUploadError } from './tools-slides-upload.js';
import { pageApiFetch } from './tools-slides-convert-upload.js';

const LOGIN_URL = '/login?next=/tools/slides';

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

function fmtBytes(n) {
  if (!Number.isFinite(n)) return '—';
  if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(2)} GiB`;
  if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(1)} MiB`;
  if (n >= 2 ** 10) return `${(n / 2 ** 10).toFixed(1)} KiB`;
  return `${n} B`;
}

/// 直传任务本地记录（localStorage；只存非秘密 job id 与文件提示，§5）。
/// key 按文件名+大小——同名同大小视为同一候选。review #1：候选只是线索，
/// 不是身份——记录绑定账号且携带分片 SHA-256（digests），共享引擎在续传前
/// 逐片核验「新选中文件」的字节后才允许跳过；无摘要的记录按 legacy 处理
/// （绝不凭名称/大小复用）。隐私模式写入失败静默（只失去续传）。
const RECORDS_KEY = 'pt.tools.direct.uploads';

function readRecords() {
  try {
    const arr = JSON.parse(localStorage.getItem(RECORDS_KEY) || '[]');
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function writeRecords(records) {
  try {
    localStorage.setItem(RECORDS_KEY, JSON.stringify(records));
  } catch { /* localStorage 不可用：仅失去刷新恢复 */ }
}

function resumableFor(file, account) {
  const acct = String(account || '');
  return readRecords().find(
    (r) => r && r.filename === file.name && r.size === file.size &&
            String(r.account || '') === acct) || null;
}

function readRecordById(jobId) {
  const r = readRecords().find((x) => x && x.job_id === jobId);
  if (!r) return Promise.resolve(null);
  return Promise.resolve({
    confirmed: r.confirmed || [],
    digests: r.digests && typeof r.digests === 'object' ? r.digests : null,
    account: typeof r.account === 'string' ? r.account : '',
    plan: Array.isArray(r.plan) ? r.plan : null,
  });
}

// --------------------------------------------------------------------- //
// published receipt（review 2026-10-07 #3/#4）：发布成功后持久化
// {account, filename, size, digests, plan, job_id, slide_id, target,
//  assoc: {state: 'ok'|'pending', error}}——同账号 + 同名 + 同大小 + 分片
// 内容凭证核验通过 = 同一文件已发布，绝不创建第二个 ingestion（展示
// 「打开切片」入口）；关联失败时 receipt 记 pending，只重试关联
// （同 slideId、同幂等键），成功后才向工作台发送全量成功。刷新后可恢复。
// --------------------------------------------------------------------- //
const RECEIPTS_KEY = 'pt.tools.direct.published';

function readReceipts() {
  try {
    const arr = JSON.parse(localStorage.getItem(RECEIPTS_KEY) || '[]');
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function writeReceipts(list) {
  try {
    localStorage.setItem(RECEIPTS_KEY, JSON.stringify(list));
  } catch { /* localStorage 不可用：仅失去去重/关联恢复 */ }
}

function receiptFor(file, account) {
  const acct = String(account || '');
  return readReceipts().find(
    (r) => r && r.filename === file.name && r.size === file.size &&
           String(r.account || '') === acct) || null;
}

function upsertReceipt(receipt) {
  writeReceipts(readReceipts()
    .filter((r) => !(r && r.filename === receipt.filename &&
                     r.size === receipt.size &&
                     String(r.account || '') === String(receipt.account || '')))
    .concat([receipt]));
}

function sha256Hex(buf) {
  const subtle = (typeof crypto !== 'undefined' && crypto && crypto.subtle)
    ? crypto.subtle : null;
  if (!subtle) return Promise.resolve(null);
  return subtle.digest('SHA-256', buf).then((d) => {
    const bytes = new Uint8Array(d);
    let out = '';
    for (let i = 0; i < bytes.length; i++) {
      out += (bytes[i] < 16 ? '0' : '') + bytes[i].toString(16);
    }
    return out;
  }, () => null);
}

/// receipt 内容凭证核验（有界内存：逐片读取、一片在内存）：plan 覆盖整个
/// 文件且每片摘要与新选中文件的对应分片一致 → 同一文件。
async function verifyReceipt(file, receipt) {
  const plan = Array.isArray(receipt.plan) ? receipt.plan : null;
  const digests = receipt.digests && typeof receipt.digests === 'object'
    ? receipt.digests : null;
  if (!plan || !plan.length || !digests) return { ok: false };
  let offset = 0;
  for (const p of plan) {
    const want = digests[String(p.part_number)];
    if (!(p.length > 0) || !want) return { ok: false };
    let buf = null;
    try {
      buf = await file.slice(offset, offset + p.length).arrayBuffer();
    } catch { buf = null; }
    if (!buf) return { ok: false };
    const hex = await sha256Hex(buf);
    if (!hex || hex !== want) return { ok: false };
    offset += p.length;
  }
  return { ok: offset === file.size };
}

export function createDirectUploadController({ t, onStatus, onPublished,
  onEngineEvent }) {
  const state = { busy: false, handle: null };

  function status(key, vars) {
    if (onStatus) onStatus(key ? t(key, vars || {}) : '');
  }

  /// 目标 → 项目（「新项目」仅在上传成功后创建）；失败抛错（发布不受影响，
  /// 切片留在未归类）。newProject 建成后把 pid 写回 target（重试不再建）。
  async function associateTarget(target, slideId) {
    if (!target || !slideId) return null;
    let pid = target.project || null;
    if (!pid && target.newProject && target.newProject.name) {
      const r = await pageApiFetch('/api/project/create', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': target.newProject.key || '',
        },
        body: JSON.stringify({
          name: target.newProject.name, note: '', slides: [],
        }),
      });
      const body = await r.json().then((b) => b, () => null);
      if (!r.ok || !body || !body.pid) {
        throw new Error((body && body.error) || `HTTP ${r.status}`);
      }
      pid = body.pid;
      target.project = pid;
    }
    if (!pid) return null;
    const r = await pageApiFetch(
      `/api/project/${encodeURIComponent(pid)}/slides`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slide_ids: [slideId] }),
      });
    if (!r.ok) {
      const body = await r.json().then((b) => b, () => null);
      throw new Error((body && body.error) || `HTTP ${r.status}`);
    }
    return pid;
  }

  /// receipt 记录关联结果（assoc ok/pending）；target 是 receipt 里的引用
  /// （associateTarget 已把 newProject 的 pid 写回）。
  function updateReceiptAssoc(receipt, state2, error) {
    receipt.assoc = { state: state2, error: error || null };
    upsertReceipt(receipt);
  }

  /// 关联重试（receipt assoc=pending 时页面「重试加入项目」入口）：
  /// 只做关联（同 slideId、同幂等键），成功后才回调 onPublished（全量成功
  /// 只发一次——review #4：发布成功 ≠ 关联成功，绝不提前报成功）。
  async function retryAssociation() {
    if (state.busy) return { ok: false, reason: 'busy' };
    const receipt = readReceipts().find(
      (r) => r && r.assoc && r.assoc.state === 'pending') || null;
    if (!receipt) return { ok: false, reason: 'none' };
    state.busy = true;
    try {
      await associateTarget(receipt.target || null, receipt.slide_id);
      updateReceiptAssoc(receipt, 'ok', null);
      status('tools.direct.published', { id: receipt.slide_id });
      if (onPublished && receipt.slide_id) onPublished(receipt.slide_id);
      return { ok: true, slideId: receipt.slide_id };
    } catch (e) {
      updateReceiptAssoc(receipt, 'pending', (e && e.message) || String(e));
      status('tools.direct.assoc.pending',
        { id: receipt.slide_id, e: (e && e.message) || String(e) });
      return { ok: false, reason: 'association', slideId: receipt.slide_id };
    } finally {
      state.busy = false;
    }
  }

  /// receipt 命中（同账号同名同大小）且内容凭证核验通过：不创建第二个
  /// ingestion——关联 pending 时给出重试入口，已关联/无目标时给出
  /// 「打开切片」入口。
  async function reuseReceipt(receipt) {
    const v = await verifyReceipt(receipt.sourceFile, receipt);
    if (!v.ok) {
      // 同名同大小但内容不同：过期凭证，清掉后按新文件走全新上传
      writeReceipts(readReceipts().filter((r) => r !== receipt));
      return null;
    }
    if (receipt.assoc && receipt.assoc.state === 'pending') {
      status('tools.direct.assoc.pending',
        { id: receipt.slide_id, e: receipt.assoc.error || '' });
      return { ok: true, slideId: receipt.slide_id, deduped: true,
               assocPending: true };
    }
    status('tools.direct.published.open', { id: receipt.slide_id });
    return { ok: true, slideId: receipt.slide_id, deduped: true };
  }

  /// 上传一个直传类别文件（唯一入口）。cls 是嗅探结果（classifyFile）：
  /// directClass 必须是 ome-tiff / converter-bigtiff。
  async function uploadFile(file, { cls, target } = {}) {
    if (!file || state.busy) return { ok: false, reason: 'busy' };
    state.busy = true;
    try {
      // ① 能力（点击后才拉取；401 → 登录链接，不跳转）
      let cap;
      try {
        cap = await fetchCapability();
      } catch {
        status('tools.upload.offline');
        return { ok: false, reason: 'offline' };
      }
      if (cap.authRequired) {
        status('tools.upload.login.required');
        return { ok: false, reason: 'login' };
      }
      const cfg = window.HP_COS_UPLOAD
        ? window.HP_COS_UPLOAD.resolveConfig(cap.cos_upload) : null;
      if (!cfg) {
        status('tools.upload.capability.off');
        return { ok: false, reason: 'capability' };
      }
      // ② 大小上限（与产物上传同一产品上限）
      if (!(typeof file.size === 'number') || file.size <= 0 ||
          file.size > cfg.max_size_bytes) {
        status('tools.upload.too.large', { max: fmtBytes(cfg.max_size_bytes) });
        return { ok: false, reason: 'too-large' };
      }
      // ③ 格式受理：扩展名在 capability 词表内 + 声明是直传类别
      const directClass = cls && cls.directClass;
      if (!directClass || (directClass !== 'ome-tiff' &&
                           directClass !== 'converter-bigtiff')) {
        status('tools.direct.format.unsupported');
        return { ok: false, reason: 'format' };
      }
      const ext = String(cls.ext || '').replace(/^\./, '').toLowerCase();
      if (ext && cfg.formats.indexOf(ext) < 0) {
        status('tools.direct.format.unsupported');
        return { ok: false, reason: 'format' };
      }

      // ③′ published receipt（review #3）：同账号同名同大小且内容凭证
      // 核验通过 → 已发布过，绝不创建第二个 ingestion
      const seen = receiptFor(file, typeof cap.account === 'string'
        ? cap.account : '');
      if (seen) {
        seen.sourceFile = file;
        const reused = await reuseReceipt(seen);
        if (reused) return reused;
      }

      // ④ 共享引擎上传（数据源 = 用户的 File，仅 slice 分块读取）
      const account = typeof cap.account === 'string' ? cap.account : '';
      const resume = resumableFor(file, account);
      let uploadedJobId = resume ? resume.job_id : null;
      // 发布成功后引擎会清掉上传记录（complete）——digests/plan 在 save 时
      // 就地留档（published receipt 的内容凭证）
      let lastSaved = null;
      const upload = window.HP_COS_UPLOAD.createUpload({
        source: file,
        apiFetch: pageApiFetch,
        config: cfg,
        createBody: { direct_class: directClass },
        resumeJobId: resume ? resume.job_id : null,
        skipConfirm: true,
        confirmResume: () => true,
        retryCompleteOnNetworkError: true,
        useResumeEndpoint: true,
        account,
        storage: {
          save(rec) {
            if (!rec || !rec.job_id) return undefined;
            lastSaved = {
              job_id: rec.job_id,
              digests: rec.digests && typeof rec.digests === 'object'
                ? rec.digests : {},
              plan: Array.isArray(rec.plan) ? rec.plan : null,
            };
            const records = readRecords()
              .filter((r) => r.job_id !== rec.job_id &&
                             !(r.filename === file.name && r.size === file.size &&
                               String(r.account || '') === account));
            records.push({
              job_id: rec.job_id, filename: file.name, size: file.size,
              account,
              confirmed: (rec.confirmed || []).slice(),
              digests: rec.digests && typeof rec.digests === 'object'
                ? rec.digests : {},
              plan: Array.isArray(rec.plan) ? rec.plan : null,
              slide_id: rec.slide_id || null,
            });
            writeRecords(records);
            return undefined;
          },
          complete(id, outcome) {
            writeRecords(readRecords().filter((r) => r.job_id !== id));
            return undefined;
          },
          remove(id) {
            writeRecords(readRecords().filter((r) => r.job_id !== id));
            return undefined;
          },
          findResumable: () => resumableFor(file, account),
          readConfirmed(id) {
            const rec = readRecords().find((r) => r.job_id === id);
            return (rec && rec.confirmed) || [];
          },
          readRecord: readRecordById,
        },
        onEvent: (ev) => {
          if (ev.type === 'created') {
            uploadedJobId = ev.jobId;
            status('tools.direct.working');
          }
          // review #6：progress/status/retry 事件交给页面（复用产物上传的
          // 字节进度条与取消交互）
          if (onEngineEvent) onEngineEvent(ev);
        },
      });
      state.handle = upload;
      const r = await upload.done;
      state.handle = null;
      if (!r || r.cancelled) {
        status(null);
        return { ok: false, reason: 'cancelled' };
      }
      const slideId = (r.body && r.body.slide_id) || null;
      // ⑤ published receipt（review #3/#4）：先持久化 published + 关联状态，
      // 再做关联——关联失败不报全成功（onPublished 只在关联成功/无目标时
      // 回调）；pending 可用「重试加入项目」恢复（同 slideId、同幂等键）。
      const receipt = {
        receipt: true,
        account,
        filename: file.name,
        size: file.size,
        digests: (lastSaved && lastSaved.digests) || {},
        plan: (lastSaved && lastSaved.plan) || null,
        job_id: uploadedJobId,
        slide_id: slideId,
        target: target ? JSON.parse(JSON.stringify(target)) : null,
        assoc: { state: target ? 'pending' : 'ok', error: null },
      };
      upsertReceipt(receipt);
      if (!target) {
        status('tools.direct.published', { id: slideId || '—' });
        if (onPublished && slideId) onPublished(slideId);
        return { ok: true, slideId };
      }
      try {
        await associateTarget(receipt.target, slideId);
      } catch (e) {
        updateReceiptAssoc(receipt, 'pending', (e && e.message) || String(e));
        status('tools.direct.assoc.pending',
          { id: slideId || '—', e: (e && e.message) || String(e) });
        // 切片已发布但未入项目：不是全量成功（不回调 onPublished）
        return { ok: false, reason: 'association', slideId };
      }
      updateReceiptAssoc(receipt, 'ok', null);
      status('tools.direct.published', { id: slideId || '—' });
      if (onPublished && slideId) onPublished(slideId);
      return { ok: true, slideId };
    } catch (err) {
      state.handle = null;
      const d = describeUploadError(err);
      status(d.key, d.vars);
      return { ok: false, reason: 'error' };
    } finally {
      state.busy = false;
    }
  }

  function cancel() {
    if (state.handle) state.handle.cancel();
  }

  return {
    uploadFile,
    retryAssociation,
    cancel,
    isBusy: () => state.busy,
  };
}
