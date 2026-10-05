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
/// key 按文件名+大小——同名同大小视为同一候选（真正的字节核验在 worker
/// ListParts）。隐私模式写入失败静默（只失去续传）。
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

function resumableFor(file) {
  return readRecords().find(
    (r) => r && r.filename === file.name && r.size === file.size) || null;
}

export function createDirectUploadController({ t, onStatus, onPublished }) {
  const state = { busy: false, handle: null };

  function status(key, vars) {
    if (onStatus) onStatus(key ? t(key, vars || {}) : '');
  }

  /// 目标 → 项目（「新项目」仅在上传成功后创建）；失败抛错（发布不受影响，
  /// 切片留在未归类）。
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

      // ④ 共享引擎上传（数据源 = 用户的 File，仅 slice 分块读取）
      const resume = resumableFor(file);
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
        storage: {
          save(rec) {
            if (!rec || !rec.job_id) return undefined;
            const records = readRecords()
              .filter((r) => r.job_id !== rec.job_id &&
                             !(r.filename === file.name && r.size === file.size));
            records.push({
              job_id: rec.job_id, filename: file.name, size: file.size,
              confirmed: (rec.confirmed || []).slice(),
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
          findResumable: () => resumableFor(file),
          readConfirmed(id) {
            const rec = readRecords().find((r) => r.job_id === id);
            return (rec && rec.confirmed) || [];
          },
        },
        onEvent: (ev) => {
          if (ev.type === 'created') {
            status('tools.direct.working');
          }
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
      status('tools.direct.published', { id: slideId || '—' });
      // ⑤ 工作台交接目标关联（权限检查在服务端；失败不回滚发布）
      let assocError = null;
      try {
        await associateTarget(target || null, slideId);
      } catch (e) {
        assocError = e;
        status('tools.direct.failed', { e: (e && e.message) || String(e) });
      }
      if (onPublished && slideId) onPublished(slideId);
      return { ok: true, slideId, assocError };
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
    cancel,
    isBusy: () => state.busy,
  };
}
