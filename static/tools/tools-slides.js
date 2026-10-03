// tools-slides.js — /tools/slides 本地切片工具页（C3；U2 页面简化）。
// 页面唯一职责：UI、文件授权、设置选择与结果展示；全部转换逻辑经
// C2 运行器（slide-transform/runner.js，页面唯一 API 面）。零第三方依赖。
//
// 隐私（计划 §6）：除本页自托管静态代码外不发任何网络请求；文件字节/
// 文件名/哈希/缩略图不出浏览器；不创建平台任务。离线可用（引擎加载后）。
//
// U2 结构：默认视图 = 标题 + 一行本地处理说明 + 大拖放区/选择文件；识别后
// 只出现一张配置摘要（文件/类型/输出/画质 + 折叠「更多选项」）与两个动作
// （转换并上传到工作台 / 仅转换）。file input 与 drop 都进入同一
// prepareSource 流程；一次只接受一个切片文件；扩展名只是提示，识别靠
// engine.js 的文件头魔数表（已知不支持文件在完整复制前被拒绝）。
// 画质（U3 编码档）与输出格式同样遵循「prepared 可改并落盘、开始即锁定、
// 重开以任务记录为准」的合同。
//
// 可访问性：progress 用 role=progressbar + aria-valuenow；状态文本走
// aria-live；对话框 <dialog showModal> 原生焦点圈 + 打开者焦点还原；
// 所有状态同时有文字，不只靠颜色。file input 视觉隐藏但保持键盘可达。
'use strict';

import { SlideToolsRunner } from './slide-transform/runner.js';
import * as E from './slide-transform/engine.js';
import { createUploadController } from './tools-slides-upload.js';
import { createConvertUploadController } from './tools-slides-convert-upload.js';

const CHANNEL_JSON_MAX_BYTES = 1 << 20; // 伴随文件读取上限 1 MiB（有界）

const $ = (id) => document.getElementById(id);
const els = {
  inputSection: $('input-section'),
  inputMessage: $('input-message'),
  dropZone: $('drop-zone'),
  pickFileBtn: $('pick-file-btn'),
  fileInput: $('file-input'),
  channelSection: $('channel-section'),
  channelInput: $('channel-input'),
  stageSection: $('stage-section'),
  stageStatus: $('stage-status'),
  stageProgress: $('stage-progress'),
  stageBar: $('stage-bar'),
  stageBytes: $('stage-bytes'),
  prepareCancelBtn: $('prepare-cancel-btn'),
  summarySection: $('summary-section'),
  summaryGrid: $('summary-grid'),
  outputSummaryText: $('output-summary-text'),
  qualityConflict: $('quality-conflict'),
  qualityFieldset: $('quality-fieldset'),
  qualityLocked: $('quality-locked'),
  moreOptions: $('more-options'),
  probeSection: $('probe-section'),
  probeGrid: $('probe-grid'),
  estimateSection: $('estimate-section'),
  estimateGrid: $('estimate-grid'),
  profileSection: $('profile-section'),
  profileSuggest: $('profile-suggest'),
  policySection: $('policy-section'),
  policyStrictWarn: $('policy-strict-warn'),
  formatSection: $('format-section'),
  formatFieldset: $('format-fieldset'),
  formatLocked: $('format-locked'),
  runSection: $('run-section'),
  convertBtn: $('convert-btn'),
  convertUploadBtn: $('convert-upload-btn'),
  cancelBtn: $('cancel-btn'),
  runStatus: $('run-status'),
  runProgress: $('run-progress'),
  runBar: $('run-bar'),
  runBytes: $('run-bytes'),
  resultSection: $('result-section'),
  resultGrid: $('result-grid'),
  saveBtn: $('save-btn'),
  persistBtn: $('persist-btn'),
  saveStatus: $('save-status'),
  uploadBtn: $('upload-btn'),
  uploadCancelBtn: $('upload-cancel-btn'),
  uploadStatus: $('upload-status'),
  jobsDetails: $('jobs-details'),
  jobsList: $('jobs-list'),
  pageError: $('page-error'),
  pageStatus: $('page-status'),
  diskDialog: $('disk-dialog'),
  diskTitle: $('disk-dialog-title'),
  diskBody: $('disk-dialog-body'),
  diskConfirm: $('disk-confirm-btn'),
  diskDeny: $('disk-deny-btn'),
};

const t = (key, vars) => window.HP_I18N.t(key, vars);

// ---------------------------------------------------------------- utils --

function fmtBytes(n) {
  if (!Number.isFinite(n)) return '—';
  if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(2)} GiB`;
  if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(1)} MiB`;
  if (n >= 2 ** 10) return `${(n / 2 ** 10).toFixed(1)} KiB`;
  return `${n} B`;
}

function errCode(e) {
  // stError 形状 {error:{code}} 与 worker done 的裸 {code,message} 都要认
  const c = E.errCode(e);
  return c || (e && typeof e.code === 'string' ? e.code : null);
}

function errRawText(e) {
  if (e && typeof e.code === 'string' && e.message) return `${e.code}: ${e.message}`;
  return E.errText(e);
}

function friendlyError(e) {
  const code = errCode(e);
  const raw = errRawText(e);
  const base = code ? t(`tools.err.${code}`) : null;
  // t() 回退返回 key 本身 —— 视为无翻译，退回原文
  const friendly = base && base !== `tools.err.${code}` ? base : null;
  if (code === 'disk_precheck_failed') return raw; // 磁盘缺口由对话框/数字呈现
  if (friendly) return `${friendly}\n(${raw})`;
  return raw;
}

function showError(e) {
  const text = friendlyError(e);
  els.pageError.hidden = false;
  els.pageError.textContent = text;
}

function clearError() {
  els.pageError.hidden = true;
  els.pageError.textContent = '';
}

/// 带 i18n 键的动态文案（语言切换时按保存的键重渲染，避免残留旧语言）
function dynText(el, store, key, vars) {
  if (key === null) {
    store.key = null;
    el.textContent = '';
    return;
  }
  store.key = key;
  store.vars = vars || null;
  el.textContent = t(key, store.vars);
}
const stageMsg = { key: null, vars: null };
const statusMsg = { key: null, vars: null };
const inputMsg = { key: null, vars: null };
function setStageMsg(key, vars) { dynText(els.stageStatus, stageMsg, key, vars); }
function setPageMsg(key, vars) { dynText(els.pageStatus, statusMsg, key, vars); }
function showInputMessage(key, vars) { dynText(els.inputMessage, inputMsg, key, vars); }
function clearInputMessage() { dynText(els.inputMessage, inputMsg, null); }

function setProgress(progressEl, barEl, bytesEl, frac, bytesText) {
  const pct = Math.max(0, Math.min(100, Math.round((frac || 0) * 100)));
  progressEl.setAttribute('aria-valuenow', String(pct));
  barEl.style.width = `${pct}%`;
  if (bytesEl) bytesEl.textContent = bytesText || '';
}

function dlRow(grid, term, definition, ddId) {
  const dt = document.createElement('dt');
  dt.textContent = term;
  const dd = document.createElement('dd');
  dd.textContent = definition;
  if (ddId) dd.id = ddId;
  grid.appendChild(dt);
  grid.appendChild(dd);
  return dd;
}

// ---------------------------------------------------------------- state --

const page = {
  runner: null,
  uploadCtl: null,         // C4 上传控制器（tools-slides-upload.js）
  convertUploadCtl: null,  // R1 一键转换并上传控制器（tools-slides-convert-upload.js）
  file: null,
  channelJson: null,       // string | null（≤1 MiB 读取结果）
  channelJsonName: null,
  prep: null,              // { jobId, probe, identity }
  running: false,
  readyInfo: null,        // { jobId, outputBytes, sha256, modality, sourceName }
  outputLockedProfile: null,  // 明场输出格式：任务一旦开始即锁定（lockOutputProfile）
  encodingLockedProfile: null, // 画质：任务一旦开始即锁定（lockEncodingProfile）
  saveSupported: typeof window.showSaveFilePicker === 'function',
  beforeunloadOn: false,
  lastJobs: [],
};

// ------------------------------------------------------------ i18n glue --

document.addEventListener('hp-lang-change', rerenderForLang);

function rerenderForLang() {
  document.title = t('tools.doc.title');
  updateProfileSuggestText();
  renderOutputFormatSection();
  renderQualitySection();
  renderSaveStatus();
  renderResultPanel();
  renderJobs(page.lastJobs || []);
  if (page.uploadCtl) page.uploadCtl.rerenderForLang();
  if (page.convertUploadCtl) page.convertUploadCtl.rerenderForLang();
  if (page.prep) {
    renderSummary();
    renderProbeSummary();
  }
  if (page.busyPhaseLabelKey) els.runStatus.textContent = t(page.busyPhaseLabelKey);
  if (stageMsg.key) els.stageStatus.textContent = t(stageMsg.key, stageMsg.vars);
  if (statusMsg.key) els.pageStatus.textContent = t(statusMsg.key, statusMsg.vars);
  if (inputMsg.key) els.inputMessage.textContent = t(inputMsg.key, inputMsg.vars);
  if (runBytesMsg.key && !els.runBytes.hidden) els.runBytes.textContent = t(runBytesMsg.key, runBytesMsg.vars);
  updateStrictWarning();
}

/// R1 一键链流程文案（#page-status；「去登录」链接是文案的一部分——DOM
/// 构造，不用 innerHTML）。语言切换由 convertUploadCtl.rerenderForLang 重放。
function renderFlowMsg(key, vars) {
  els.pageStatus.textContent = '';
  if (key === 'tools.cu.login.required') {
    els.pageStatus.appendChild(document.createTextNode(t(key)));
    els.pageStatus.appendChild(document.createTextNode(' '));
    const a = document.createElement('a');
    a.href = '/login?next=/tools/slides';
    a.textContent = t('tools.upload.login.link');
    els.pageStatus.appendChild(a);
    return;
  }
  if (key) els.pageStatus.textContent = t(key, vars || {});
}

// ------------------------------------------------------------- dialogs --

/// uncertain 磁盘确认：返回 true（确认继续）/ false（取消）。焦点进入对话框，
/// 关闭后还原到打开者（键盘可达；Esc = 取消）。U2：该确认是独立模态对话框，
/// 永不被折叠或隐藏。
function askDiskConfirm({ title, body, confirmLabel }) {
  return new Promise((resolve) => {
    const opener = document.activeElement;
    els.diskTitle.textContent = title;
    els.diskBody.textContent = body;
    els.diskConfirm.textContent = confirmLabel || t('tools.disk.confirm');
    els.diskDeny.textContent = t('tools.disk.cancel');
    const done = (answer) => {
      els.diskDialog.removeEventListener('close', onClose);
      els.diskDialog.close();
      els.diskDialog.removeEventListener('cancel', onCancel);
      if (opener && opener.focus) opener.focus();
      resolve(answer);
    };
    const onClose = () => {
      const a = els.diskDialog.returnValue === 'confirm';
      els.diskDialog.removeEventListener('cancel', onCancel);
      if (opener && opener.focus) opener.focus();
      resolve(a);
    };
    const onCancel = (ev) => { ev.preventDefault(); done(false); };
    els.diskDialog.addEventListener('close', onClose);
    els.diskDialog.addEventListener('cancel', onCancel);
    els.diskDialog.showModal();
    els.diskDeny.focus();
  });
}

async function probeWithDiskFlow(file, opts = {}) {
  const confirmOverride = opts.confirmUncertainDisk || false;
  try {
    return await page.runner.probe(file, {
      confirmUncertainDisk: confirmOverride, channelJson: page.channelJson,
      outputProfile: opts.outputProfile,
      encodingProfile: opts.encodingProfile,
    });
  } catch (e) {
    if (errCode(e) === 'disk_precheck_failed') {
      const info = (e && e.error) || {};
      if (info.uncertain) {
        const ok = await askDiskConfirm({
          title: t('tools.disk.title'),
          body: t('tools.disk.body', {
            need: fmtBytes(info.need && info.need.total),
            available: fmtBytes(info.available),
          }),
        });
        if (!ok) throw e;
        return await page.runner.probe(file, {
          confirmUncertainDisk: true, channelJson: page.channelJson,
          outputProfile: opts.outputProfile,
          encodingProfile: opts.encodingProfile,
        });
      }
    }
    throw e;
  }
}

// ------------------------------------------------------- prepareSource --
//
// file input 与 drop 的唯一汇合点（计划 §3.2.1）。一次只接受一个切片文件：
// 多文件 → 明确提示、什么都不开始；目录 / .mrxs / .dat → MRXS 需要完整包
// 且当前未支持；.svs → 尚未支持。扩展名只是提示：真正的识别在运行器的
// 文件头魔数检查（_prepare 在完整复制之前拒绝已知不支持的文件）。

async function prepareSource(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  if (page.running) {
    // 转换进行中不接受新输入：此时 resetFlowPanels 会撤掉进行中的进度 UI
    //（file input 已禁用，这里补上 drop 路径的同等防护）
    showInputMessage('tools.drop.busy');
    return;
  }
  if (files.length > 1) {
    showInputMessage('tools.drop.multiple', { n: String(files.length) });
    return;
  }
  const file = files[0];
  const hint = E.inputExtensionHint(file.name);
  if (hint === 'bundle') {
    showInputMessage('tools.drop.bundle');
    return;
  }
  if (hint === 'svs') {
    showInputMessage('tools.drop.svs');
    return;
  }
  await onFilePicked(file);
}

/// drop 事件取目录项只能在事件处理器内同步做（webkitGetAsEntry 的有效窗口）
function handleDropData(dt) {
  if (!dt) return;
  const items = dt.items ? Array.from(dt.items) : [];
  for (const it of items) {
    try {
      const entry = it.webkitGetAsEntry && it.webkitGetAsEntry();
      if (entry && entry.isDirectory) {
        showInputMessage('tools.drop.bundle');
        return;
      }
    } catch { /* 非 filesystem 条目：按普通文件处理 */ }
  }
  prepareSource(dt.files ? Array.from(dt.files) : []);
}

function resetFlowPanels() {
  clearError();
  clearInputMessage();
  page.prep = null;
  page.readyInfo = null;
  page.outputLockedProfile = null;
  page.encodingLockedProfile = null;
  for (const el of [els.probeSection, els.estimateSection, els.profileSection,
    els.policySection, els.formatSection, els.resultSection, els.summarySection,
    els.channelSection]) el.hidden = true;
  els.runSection.hidden = true;
  els.convertBtn.disabled = true;
  if (els.convertUploadBtn) els.convertUploadBtn.disabled = true;
  els.runStatus.textContent = '';
  els.runProgress.hidden = true;
  els.runBytes.hidden = true;
  els.stageSection.hidden = true;
  els.prepareCancelBtn.hidden = true;
  els.uploadStatus.textContent = '';
  els.uploadCancelBtn.hidden = true;
  els.qualityFieldset.hidden = true;
  els.qualityFieldset.disabled = false;
  els.qualityLocked.hidden = true;
  els.qualityConflict.hidden = true;
  els.moreOptions.open = false;
  const preserve = document.getElementById('quality-preserve');
  if (preserve) preserve.checked = true;
}

async function onFilePicked(explicitFile) {
  const list = els.fileInput.files;
  const file = explicitFile || (list && list[0]);
  if (!file) return;
  // 一次一个切片文件（U2 第一版）：多选只提示、不静默丢弃
  if (!explicitFile && list && list.length > 1) {
    showInputMessage('tools.drop.multiple', { n: String(list.length) });
    return;
  }
  clearInputMessage();
  // 离开上一个未完成任务：留在任务列表里可续跑/删除
  resetFlowPanels();
  page.file = file;
  await readChannelJsonInput();
  await runProbeFlow();
}

/// R1 工作台交接：postMessage 收到的 File 走与手动选择完全相同的流程
/// （复制与探测、磁盘确认、设置、结果）。不复制第二次——唯一副本仍是
/// 运行器 staging 进 OPFS 的 source.bin。用户无需重选文件。
async function takeHandoffFile(file) {
  if (!file) return;
  resetFlowPanels();
  els.fileInput.value = '';
  page.file = file;
  page.channelJson = null;
  page.channelJsonName = null;
  await runProbeFlow();
}

async function readChannelJsonInput() {
  page.channelJson = null;
  page.channelJsonName = null;
  const f = els.channelInput.files && els.channelInput.files[0];
  if (!f) return;
  if (f.size > CHANNEL_JSON_MAX_BYTES) {
    setPageMsg('tools.channel.too.large');
    return;
  }
  try {
    page.channelJson = await f.slice(0, CHANNEL_JSON_MAX_BYTES).text();
    page.channelJsonName = f.name;
  } catch {
    page.channelJson = null;
    setPageMsg('tools.channel.read.failed');
  }
}

// 已准备（未开始）的任务：伴随文件改选后同步到任务记录，刷新后从列表开始仍带上它
async function onChannelPicked() {
  await readChannelJsonInput();
  if (!page.prep || page.running) return;
  try {
    await page.runner.setPreparedChannelJson(page.prep.jobId, page.channelJson);
  } catch (e) {
    showError(e);
  }
}

async function runProbeFlow() {
  clearError();
  els.stageSection.hidden = false;
  setStageMsg('tools.stage.status');
  setProgress(els.stageProgress, els.stageBar, els.stageBytes, 0, `0 / ${fmtBytes(page.file.size)}`);
  setBeforeunload(true);
  els.prepareCancelBtn.hidden = false;
  els.prepareCancelBtn.disabled = false;
  try {
    const cjAtProbe = page.channelJson;
    // 输出格式与画质选择在准备时就传给运行器（prepared 记录反映用户选择）。
    // 模态在复制+探测后才能确认，这里用文件头嗅探（与核心同源的魔数表）
    // 预判：只有明场才传（荧光固定 fl-ome / 保留画质，运行器按模态默认）。
    const head = new Uint8Array(await page.file.slice(0, 8).arrayBuffer());
    const sniffedModality = E.magicModality(head);
    const prep = await probeWithDiskFlow(page.file, {
      outputProfile: sniffedModality === 'brightfield' ? selectedOutputProfile() : undefined,
      encodingProfile: sniffedModality === 'brightfield' ? selectedEncodingProfile() : undefined,
    });
    page.prep = prep;
    // 伴随文件在复制/探测期间改选过：补写进任务记录
    if (page.channelJson !== cjAtProbe) {
      await page.runner.setPreparedChannelJson(prep.jobId, page.channelJson);
    }
  } catch (e) {
    els.prepareCancelBtn.hidden = true;
    setBeforeunload(false);
    setStageMsg(null);
    const code = errCode(e);
    const info = (e && e.error) || {};
    if (code === 'cancelled') {
      // 取消准备（U2）：worker 已终止、任务目录已删除——明确反馈，不算错误
      els.stageSection.hidden = true;
      page.file = null;
      setPageMsg('tools.stage.cancelled');
      refreshJobs();
      return;
    }
    if (code === 'disk_precheck_failed') {
      els.pageError.hidden = false;
      els.pageError.textContent = (info.uncertain
        ? t('tools.disk.cancelled.note')
        : t('tools.disk.hard.title') + '\n' + t('tools.disk.hard.body', {
          need: fmtBytes(info.need && info.need.total),
          available: fmtBytes(info.available),
        })) + `\n(${E.errText(e)})`;
    } else {
      showError(e);
    }
    if (code === 'unsupported_input') els.stageSection.hidden = true;
    refreshJobs();
    return;
  }
  els.prepareCancelBtn.hidden = true;
  setBeforeunload(false);
  setStageMsg('tools.stage.done');
  setProgress(els.stageProgress, els.stageBar, els.stageBytes, 1,
    `${fmtBytes(page.file.size)} / ${fmtBytes(page.file.size)}`);
  // 识别完成：一张配置摘要 + 折叠的高级项（荧光只隐藏不适用的选择组）
  els.summarySection.hidden = false;
  els.probeSection.hidden = false;
  els.estimateSection.hidden = false;
  els.profileSection.hidden = false;
  els.policySection.hidden = false;
  renderOutputFormatSection();
  renderQualitySection();
  renderChannelSection();
  renderSummary();
  renderProbeSummary();
  els.runSection.hidden = false;
  els.convertBtn.disabled = false;
  if (els.convertUploadBtn) els.convertUploadBtn.disabled = false;
  applyProfileSuggestion();
  updateStrictWarning();
  refreshJobs();
}

function probeDoc() {
  return page.prep && page.prep.probe ? (page.prep.probe.document || {}) : {};
}

function probeEstimate() {
  const p = page.prep && page.prep.probe;
  if (!p) return {};
  return p.document && p.document.estimate ? p.document.estimate : (p.estimate || {});
}

// Summary card shows a reader-friendly family name; the core's format id stays
// in the technical summary under "更多选项".
function formatFamilyLabel(id) {
  const s = String(id || '');
  if (!s) return '—';
  if (s.startsWith('kfbf')) return 'KFBF';
  if (s.startsWith('kfb')) return 'KFB';
  if (s.startsWith('aperio-svs')) return 'SVS (Aperio)';
  if (s.startsWith('mirax')) return 'MRXS';
  return s;
}

function renderSummary() {
  if (!page.prep) return;
  const doc = probeDoc();
  const identity = page.prep.identity || {};
  const g = els.summaryGrid;
  g.textContent = '';
  dlRow(g, t('tools.summary.name'),
    String(identity.name || (page.file && page.file.name) || '—'), 'summary-name');
  dlRow(g, t('tools.summary.size'),
    fmtBytes(Number.isFinite(identity.size) ? identity.size : (page.file ? page.file.size : NaN)),
    'summary-size');
  dlRow(g, t('tools.probe.format'), formatFamilyLabel(doc.format), 'summary-format');
  dlRow(g, t('tools.probe.modality'), doc.modality === 'fluorescence'
    ? t('tools.probe.modality.fl') : t('tools.probe.modality.bf'), 'summary-modality');
  renderOutputSummary();
}

function renderProbeSummary() {
  if (!page.prep) return;
  const doc = probeDoc();
  const est = probeEstimate();
  const isFL = doc.modality === 'fluorescence';
  const grid = els.probeGrid;
  grid.textContent = '';
  dlRow(grid, t('tools.probe.format'), doc.format || '—', 'probe-format');
  dlRow(grid, t('tools.probe.modality'), isFL ? t('tools.probe.modality.fl') : t('tools.probe.modality.bf'));
  dlRow(grid, t('tools.probe.dims'), `${doc.width || '?'} × ${doc.height || '?'} px`);
  const levels = (doc.levels || []).length;
  dlRow(grid, t('tools.probe.levels'), String(levels));
  if (isFL) {
    const names = (doc.channels || []).map((c) => c.name || `#${c.index}`).join('、');
    dlRow(grid, t('tools.probe.channels'), `${(doc.channels || []).length}（${names}）`);
  } else {
    dlRow(grid, t('tools.probe.channels'), t('tools.probe.channels.bf'));
  }
  // MPP：本体缺失就显示未知，绝不从 objective 猜测（计划 §2）
  const mppX = Number(doc.mpp_x);
  const mppY = Number(doc.mpp_y);
  const mpp = Number(doc.mpp);
  let mppText = t('tools.probe.mpp.unknown');
  if (isFL) {
    if (mpp > 0) mppText = `${mpp.toFixed(4)} µm/px`;
  } else if (mppX > 0 && mppY > 0) {
    mppText = mppX === mppY
      ? `${mppX.toFixed(4)} µm/px`
      : `${mppX.toFixed(4)} × ${mppY.toFixed(4)} µm/px`;
  }
  dlRow(grid, t('tools.probe.mpp'), mppText, 'probe-mpp');
  const edge = Number(est.edge_tiles || 0);
  dlRow(grid, t('tools.probe.edge'), edge > 0
    ? t('tools.probe.edge.note', { n: edge })
    : t('tools.probe.edge.none'), 'probe-edge');
  const missing = Number(est.cells_missing || 0);
  if (missing > 0) {
    dlRow(grid, t('tools.probe.missing'), t('tools.probe.missing.note', { n: missing }));
  }
  dlRow(grid, t('tools.probe.identity.sha'), String(page.prep.identity.sha256 || '—'));

  renderEstimate();
}

// 空间预估：源副本 + 输出（上界，随所选画质取对应编码的上界）+ 索引/日志
function renderEstimate() {
  if (!page.prep) return;
  const est = probeEstimate();
  const need = E.diskNeedBytes(est, {
    sourceBytes: page.prep.identity.size,
    encoding: selectedEncodingProfile(),
  });
  const eg = els.estimateGrid;
  eg.textContent = '';
  dlRow(eg, t('tools.estimate.source'), fmtBytes(need.source), 'estimate-source');
  dlRow(eg, t('tools.estimate.output'), fmtBytes(need.output), 'estimate-output');
  dlRow(eg, t('tools.estimate.scratch'), fmtBytes(need.scratch + need.journal));
  dlRow(eg, t('tools.estimate.total'), fmtBytes(need.total), 'estimate-total');
}

// ------------------------------------------------------- profile/policy --

function applyProfileSuggestion() {
  const suggested = E.defaultProfileId(navigator.deviceMemory, navigator.hardwareConcurrency);
  const radio = document.getElementById(`profile-${suggested}`);
  if (radio) radio.checked = true;
  updateProfileSuggestText();
}

function updateProfileSuggestText() {
  const suggested = E.defaultProfileId(navigator.deviceMemory, navigator.hardwareConcurrency);
  els.profileSuggest.textContent = t('tools.profile.suggest', {
    name: t(`tools.profile.${suggested}`),
    dm: navigator.deviceMemory === undefined ? '—' : String(navigator.deviceMemory),
  });
}

function selectedProfileId() {
  const checked = document.querySelector('input[name="profile"]:checked');
  return checked ? checked.value : E.defaultProfileId(navigator.deviceMemory);
}

function selectedPolicy() {
  const checked = document.querySelector('input[name="policy"]:checked');
  return checked ? checked.value : 'allow-edge';
}

// ---------------------------------------------------- output format (bf) --

/// 当前 UI 的明场输出格式选择（模板默认勾选 bf-ome；无 DOM 时也回落默认）。
function selectedOutputProfile() {
  const checked = document.querySelector('input[name="outputFormat"]:checked');
  return checked ? checked.value : E.OUTPUT_PROFILES.BF_OME;
}

/// 传给运行器的 outputProfile：只有确定是明场才传当前选择；荧光（以及模态
/// 未知的记录）传 undefined → 运行器按模态默认 fl-ome / 记录值，绝不把明场
/// 选择塞给荧光任务（runner 会以 kind=output-profile 拒绝）。
function outputProfileForModality(modality) {
  return modality === 'brightfield' ? selectedOutputProfile() : undefined;
}

/// 任务一旦开始（写入输出），该任务的输出格式不可再改：字段组禁用，改以
/// 文案展示任务实际格式。换选新文件（新任务）时在 resetFlowPanels 解锁。
function lockOutputProfile(profile) {
  page.outputLockedProfile = profile || null;
  renderOutputFormatSection();
}

function renderOutputFormatSection() {
  // 荧光输入不提供选择：固定多通道 OME-TIFF（fl-ome），整节隐藏。
  const show = !!page.prep && probeDoc().modality !== 'fluorescence';
  els.formatSection.hidden = !show;
  if (!show) return;
  const locked = page.outputLockedProfile;
  els.formatFieldset.disabled = !!locked;
  els.formatLocked.hidden = !locked;
  if (locked) {
    els.formatLocked.textContent = t('tools.format.locked', {
      name: t(`tools.result.format.${locked}`),
    });
  }
  renderOutputSummary();
}

/// 配置摘要的「输出」行：按目标软件表达，同时保留真实格式名 + 扩展名。
function renderOutputSummary() {
  if (!page.prep) return;
  const modality = probeDoc().modality;
  const profile = page.outputLockedProfile
    || (modality === 'brightfield' ? selectedOutputProfile() : E.defaultOutputProfile(modality));
  const ext = E.isOmeProfile(profile) ? '.ome.tif' : '.tif';
  els.outputSummaryText.textContent = t('tools.summary.output.line', {
    name: t(`tools.jobs.format.${profile}`),
    ext,
  });
}

/// 通道信息（可选 channel.json）只在荧光/KFBF 文件识别后出现（U2：识别前
/// 不问用户要伴随文件；明场输入整节隐藏）。
function renderChannelSection() {
  const show = !!page.prep && probeDoc().modality === 'fluorescence';
  els.channelSection.hidden = !show;
}

function updateStrictWarning() {
  const est = probeEstimate();
  const willRefuse = Number(est.edge_tiles || 0) > 0;
  els.policyStrictWarn.hidden = !willRefuse;
  if (willRefuse) {
    els.policyStrictWarn.textContent = t('tools.policy.strict.warn', {
      n: Number(est.edge_tiles || 0),
    });
  }
}

// ------------------------------------------------------- quality (U3) --

const ENCODING_LABEL_KEY = {
  [E.ENCODING_PROFILES.PRESERVE]: 'tools.quality.preserve',
  [E.ENCODING_PROFILES.COMPACT]: 'tools.quality.compact',
};

function encodingLabel(encoding) {
  const key = ENCODING_LABEL_KEY[encoding];
  return key ? t(key) : String(encoding || '—');
}

/// 当前 UI 的画质选择（模板默认勾选保留画质；无 DOM 时回落 preserve）。
function selectedEncodingProfile() {
  const checked = document.querySelector('input[name="encoding"]:checked');
  return checked ? checked.value : E.defaultEncodingProfile();
}

/// 传给运行器的 encodingProfile：只有确定是明场才传当前选择；荧光不传
/// （运行器按记录/默认 preserve——荧光不提供有损模式，与 outputProfile
/// 同一模式，runner 会以 kind=encoding-profile 拒绝不适用的值）。
function encodingProfileForModality(modality) {
  return modality === 'brightfield' ? selectedEncodingProfile() : undefined;
}

/// 任务一旦开始，画质不可再改：字段组禁用，改以文案展示任务实际画质。
function lockEncodingProfile(encoding) {
  page.encodingLockedProfile = encoding || null;
  renderQualitySection();
}

function renderQualitySection() {
  // 荧光不提供画质选择（compact 仅明场；保留画质是唯一语义）→ 整组隐藏。
  const show = !!page.prep && probeDoc().modality !== 'fluorescence';
  els.qualityFieldset.hidden = !show;
  if (!show) return;
  const locked = page.encodingLockedProfile;
  els.qualityFieldset.disabled = !!locked;
  els.qualityLocked.hidden = !locked;
  if (locked) {
    els.qualityLocked.textContent = t('tools.quality.locked', {
      name: encodingLabel(locked),
    });
  }
  updateQualityPolicyGate();
}

/// 「像素严格无损」与「更小文件（有损）」互斥（U2 要求在 UI 也阻止，核心
/// 另有类型化拒绝兜底）：选了 compact → 禁用 strict；选了 strict → 禁用
/// compact。已经勾选的一方在切换时被自动改回（onEncodingChange /
/// onPolicyChange），并显示一次性说明。
function updateQualityPolicyGate() {
  const compactRadio = document.getElementById('quality-compact');
  const strictRadio = document.getElementById('policy-strict');
  if (!compactRadio || !strictRadio) return;
  const compactSel = selectedEncodingProfile() === E.ENCODING_PROFILES.COMPACT;
  const strictSel = selectedPolicy() === 'strict-lossless';
  compactRadio.disabled = strictSel || !!page.encodingLockedProfile;
  strictRadio.disabled = compactSel;
  const conflicted = compactSel && strictSel;
  els.qualityConflict.hidden = !conflicted;
  if (conflicted) els.qualityConflict.textContent = t('tools.quality.strict.conflict');
}

document.querySelectorAll('input[name="policy"]').forEach((r) => {
  r.addEventListener('change', () => {
    updateStrictWarning();
    // strict 与 compact 互斥：strict 被选中时把 compact 改回保留画质。
    // 派发 change 让 onEncodingChange 把改回的选择写回 prepared 记录
    //（radio 的程序化勾选本身不触发事件）。
    if (selectedPolicy() === 'strict-lossless'
        && selectedEncodingProfile() === E.ENCODING_PROFILES.COMPACT) {
      const preserve = document.getElementById('quality-preserve');
      if (preserve) {
        preserve.checked = true;
        preserve.dispatchEvent(new Event('change', { bubbles: true }));
        return;
      }
    }
    updateQualityPolicyGate();
    renderEstimate();
  });
});

/// 准备后改选明场输出格式：立即写回 prepared 记录（刷新后从任务列表「开始」
/// 仍带上该选择）。已开始的任务 radio 已禁用（锁定），不会到达这里；万一
/// 到达（运行器侧同样只允许 prepared），错误照常展示且记录不变。
async function onOutputFormatChange(ev) {
  if (!page.prep || page.outputLockedProfile || els.formatSection.hidden) return;
  try {
    await page.runner.setPreparedOutputProfile(page.prep.jobId, ev.target.value);
    refreshJobs();
  } catch (e) {
    showError(e);
  }
  renderOutputSummary();
}

document.querySelectorAll('input[name="outputFormat"]').forEach((r) => {
  r.addEventListener('change', onOutputFormatChange);
});

/// 准备后改选画质：立即写回 prepared 记录（与输出格式同一合同）；磁盘预估
/// 跟随所选编码取对应上界。strict+compact 在此也被阻止（选 compact 时把
/// strict 改回允许边缘重编码）。
async function onEncodingChange(ev) {
  if (!page.prep || page.encodingLockedProfile || els.qualityFieldset.hidden) return;
  if (ev.target.value === E.ENCODING_PROFILES.COMPACT
      && selectedPolicy() === 'strict-lossless') {
    const allow = document.getElementById('policy-allow-edge');
    if (allow) allow.checked = true;
  }
  updateStrictWarning();
  updateQualityPolicyGate();
  renderEstimate();
  try {
    await page.runner.setPreparedEncodingProfile(page.prep.jobId, ev.target.value);
    refreshJobs();
  } catch (e) {
    showError(e);
  }
}

document.querySelectorAll('input[name="encoding"]').forEach((r) => {
  r.addEventListener('change', onEncodingChange);
});

// -------------------------------------------------------------- convert --

const PHASE_LABEL_KEY = {
  selected: 'tools.phase.selected',
  probing: 'tools.phase.probing',
  planned: 'tools.phase.planned',
  running: 'tools.phase.running',
  paused: 'tools.phase.paused',
  finalizing: 'tools.phase.finalizing',
  validating: 'tools.phase.validating',
  ready: 'tools.phase.ready',
  exported: 'tools.phase.exported',
  failed: 'tools.phase.failed',
  cancelled: 'tools.phase.cancelled',
  cleanup_pending: 'tools.phase.cleanup_pending',
};

function phaseText(state) {
  const key = PHASE_LABEL_KEY[state];
  page.busyPhaseLabelKey = key;
  return key ? t(key) : state;
}

/// 转换执行驱动（「仅转换」与 R1「转换并上传」共用）：startJob → 进度/结果
/// UI。返回 worker 的 result（{ok}|{type:'cancelled'}|失败形态）。
async function driveConversion() {
  if (!page.prep || !page.file) return null;
  clearError();
  els.convertBtn.disabled = true;
  if (els.convertUploadBtn) els.convertUploadBtn.disabled = true;
  els.prepareCancelBtn.hidden = true;
  els.fileInput.disabled = true;
  els.channelInput.disabled = true;
  els.cancelBtn.hidden = false;
  els.runProgress.hidden = false;
  els.runBytes.hidden = false;
  setProgress(els.runProgress, els.runBar, els.runBytes, 0, '');
  els.runStatus.textContent = phaseText('planned');
  setBeforeunload(true);
  page.running = true;
  try {
    // 开始即用当前 UI 选择（明场；荧光不传 → fl-ome / preserve）；
    // 任务从此锁定该输出格式与画质。
    const outputProfile = outputProfileForModality(probeDoc().modality);
    const encodingProfile = encodingProfileForModality(probeDoc().modality);
    const { jobId, done } = await page.runner.startJob(page.file, {
      jobId: page.prep.jobId,
      profileId: selectedProfileId(),
      policy: selectedPolicy(),
      channelJson: page.channelJson,
      outputProfile,
      encodingProfile,
    });
    lockOutputProfile(outputProfile || E.defaultOutputProfile(probeDoc().modality));
    lockEncodingProfile(encodingProfile || E.defaultEncodingProfile());
    page.prep.jobId = jobId;
    const result = await done;
    page.running = false;
    setBeforeunload(false);
    els.cancelBtn.hidden = true;
    els.fileInput.disabled = false;
    els.channelInput.disabled = false;
    if (result && result.ok) {
      els.runStatus.textContent = phaseText('ready');
      setProgress(els.runProgress, els.runBar, els.runBytes, 1, '');
      setRunBytes('tools.run.done.bytes', { bytes: fmtBytes(result.result.output_bytes) });
      page.readyInfo = {
        jobId,
        outputBytes: result.result.output_bytes,
        sha256: result.validation && result.validation.sha256,
        modality: probeDoc().modality,
        outputProfile: result.result.output_profile || null,
        encoding: result.result.encoding || null,
        result: { format: result.result.format || null },
        sourceName: page.file.name,
        channels: result.result.channels || [],
      };
      renderResultPanel();
      renderSaveStatus();
    } else if (result && result.type === 'cancelled') {
      els.runStatus.textContent = phaseText('cancelled');
      setPageMsg('tools.run.cancelled.note');
      els.convertBtn.disabled = false;
      if (els.convertUploadBtn) els.convertUploadBtn.disabled = false;
    } else {
      const err = (result && result.error) || { code: 'io_recoverable', message: 'unknown' };
      els.runStatus.textContent = phaseText('failed');
      showError(err);
      els.convertBtn.disabled = false;
      if (els.convertUploadBtn) els.convertUploadBtn.disabled = false;
    }
    refreshJobs();
    return result;
  } catch (e) {
    page.running = false;
    setBeforeunload(false);
    els.cancelBtn.hidden = true;
    els.fileInput.disabled = false;
    els.channelInput.disabled = false;
    els.runStatus.textContent = phaseText('failed');
    showError(e);
    els.convertBtn.disabled = false;
    if (els.convertUploadBtn) els.convertUploadBtn.disabled = false;
    refreshJobs();
    return { ok: false, error: e };
  }
}

async function onConvert() {
  await driveConversion();
}

/// R1 一键「转换并上传」：授权整条链（预检 → 意图落盘 → 本机转换 → 校验 →
/// 重新准入 → 上传 → 可查看），转换成功后自动上传、无第二次确认。
async function onConvertUpload() {
  if (!page.prep || !page.file) return;
  await page.convertUploadCtl.start({
    jobId: page.prep.jobId,
    target: page.convertUploadCtl.handoffTarget(),
    runConvert: driveConversion,
  });
  refreshJobs();
}

async function onCancel() {
  // ≤250ms 反馈：cancelJob 同步翻转状态并终止 worker，随后清理任务目录
  els.runStatus.textContent = phaseText('cancelled');
  els.cancelBtn.disabled = true;
  try {
    const r = await page.runner.cancelJob();
    setPageMsg('tools.run.cancel.cleanup', { state: r.cleanup });
  } finally {
    els.cancelBtn.disabled = false;
    els.cancelBtn.hidden = true;
    els.convertBtn.disabled = false;
    els.runProgress.hidden = true;
    els.runBytes.hidden = true;
  }
  refreshJobs();
}

/// 取消准备（U2）：终止 worker、删除任务目录；runProbeFlow 的 probe promise
/// 以类型化 `cancelled` 拒绝（runner 小改动）并显示「已取消准备」。
async function onPrepareCancel() {
  els.prepareCancelBtn.disabled = true;
  try {
    await page.runner.cancelJob();
  } finally {
    els.prepareCancelBtn.disabled = false;
    els.prepareCancelBtn.hidden = true;
  }
}

// ---------------------------------------------------------------- ready --

function suggestedOutputName() {
  if (!page.readyInfo) return 'output.tif';
  return E.outputFileName(page.readyInfo.sourceName, page.readyInfo);
}

function renderResultPanel() {
  if (!page.readyInfo) { els.resultSection.hidden = true; return; }
  els.resultSection.hidden = false;
  const g = els.resultGrid;
  g.textContent = '';
  dlRow(g, t('tools.result.format'),
    t(`tools.result.format.${E.jobOutputProfile(page.readyInfo)}`), 'result-format');
  dlRow(g, t('tools.result.encoding'),
    encodingLabel(page.readyInfo.encoding || E.ENCODING_PROFILES.PRESERVE), 'result-encoding');
  dlRow(g, t('tools.result.size'), fmtBytes(page.readyInfo.outputBytes), 'result-size');
  dlRow(g, t('tools.result.sha256'), String(page.readyInfo.sha256 || '—'), 'result-sha');
  if (page.readyInfo.modality === 'fluorescence') {
    (page.readyInfo.channels || []).forEach((c, i) => {
      const dw = Array.isArray(c.display_window) ? c.display_window : null;
      dlRow(g, t('tools.result.channel', { name: c.name }),
        dw ? t('tools.result.channel.window', { lower: dw[0], upper: dw[1] })
          : t('tools.result.channel.window.none'),
        `result-channel-${i}`);
    });
  }
  if (page.uploadCtl) page.uploadCtl.setResultJob(page.readyInfo.jobId);
  updateUploadCancelVisibility();
}

/// 任务列表里的上传/继续/取消旧上传：反馈都写在结果面板（#upload-status）
/// 里——先把面板切到该任务并显示出来（刷新后面板本是隐藏的）。只用该任务
/// 自己的记录填面板，别的任务的上传状态不会带进来。
async function selectResultJob(jobId) {
  let job = (page.lastJobs || []).find((j) => j.id === jobId) || null;
  if (!job || !job.result) job = await page.runner.getJob(jobId).catch(() => null);
  if (!job || !job.result || !['ready', 'exported'].includes(job.state)) return;
  if (!page.readyInfo || page.readyInfo.jobId !== jobId) {
    page.readyInfo = {
      jobId,
      outputBytes: job.result.outputBytes,
      sha256: job.result.sha256,
      modality: job.modality,
      outputProfile: job.outputProfile || null,
      encoding: (job.result && job.result.encoding) || job.encodingProfile || null,
      result: { format: job.result.format || null },
      sourceName: job.source && job.source.name,
      channels: job.result.channels || [],
    };
    page.saveMsg = null;
  }
  renderResultPanel();
  renderSaveStatus();
  if (typeof els.resultSection.scrollIntoView === 'function') {
    els.resultSection.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

function renderSaveStatus() {
  const supported = typeof window.showSaveFilePicker === 'function';
  els.saveBtn.disabled = !supported || !page.readyInfo;
  els.saveBtn.setAttribute('aria-disabled', String(els.saveBtn.disabled));
  if (!supported) {
    els.saveStatus.textContent = t('tools.result.save.unsupported');
    return;
  }
  if (page.saveMsg) {
    els.saveStatus.textContent = t(page.saveMsg.key, page.saveMsg.vars || {});
  } else if (els.saveStatus.dataset.busy !== '1') {
    els.saveStatus.textContent = '';
  }
}

async function onSave() {
  if (typeof window.showSaveFilePicker !== 'function') return;
  clearError();
  els.saveBtn.disabled = true;
  els.saveStatus.dataset.busy = '1';
  page.saveMsg = null;
  els.saveStatus.textContent = t('tools.result.save.working');
  try {
    // 必须在用户手势内请求 picker（Chromium 手势约束）
    const handle = await window.showSaveFilePicker({
      suggestedName: suggestedOutputName(),
      types: E.saveFileTypes(page.readyInfo),
    });
    const r = await page.runner.exportJob(page.readyInfo.jobId, () => handle.createWritable());
    page.saveMsg = { key: 'tools.result.save.done', vars: { bytes: fmtBytes(r.exportedBytes) } };
    els.saveStatus.textContent = t('tools.result.save.done', { bytes: fmtBytes(r.exportedBytes) });
    setPageMsg(null);
  } catch (e) {
    if (e && e.name === 'AbortError') {
      els.saveStatus.textContent = t('tools.result.save.aborted');
    } else {
      els.saveStatus.textContent = t('tools.result.save.failed');
      showError(e);
    }
  } finally {
    delete els.saveStatus.dataset.busy;
    renderSaveStatus();
    refreshJobs();
  }
}

async function onPersist() {
  // 仅在用户手势内请求；如实报告结果（可能被拒绝）
  els.persistBtn.disabled = true;
  let granted = false;
  try {
    granted = await navigator.storage.persist();
  } catch {
    granted = false;
  }
  els.saveStatus.textContent = granted
    ? t('tools.result.persist.granted')
    : t('tools.result.persist.denied');
  els.persistBtn.disabled = false;
}

// ------------------------------------------------------------- job list --

const STATE_TONE = {
  ready: 'ok', exported: 'ok',
  failed: 'err', cancelled: 'err', cleanup_pending: 'warn',
  running: 'run', validating: 'run', finalizing: 'run', paused: 'warn',
  staging: 'warn', prepared: 'ok', planned: 'run', probing: 'run',
};

function stateLabel(state) {
  const key = `tools.jobs.state.${state}`;
  const s = t(key);
  return s === key ? state : s;
}

const JOB_ACTION_LABEL_KEY = {
  start: 'tools.jobs.action.start',
  resume: 'tools.jobs.action.resume',
  export: 'tools.jobs.action.export',
  wait: 'tools.jobs.action.wait',
  discard: 'tools.jobs.action.discard',
};

/// R1：挂着待执行上传意图的任务，开始/续跑按钮文案换成「继续转换并上传」
/// （动作不变——完成后自动进入上传）。
const JOB_ACTION_INTENT_LABEL_KEY = {
  start: 'tools.jobs.action.intent.start',
  resume: 'tools.jobs.action.intent.resume',
};

function renderJobs(jobs) {
  page.lastJobs = jobs;
  // 任务记录默认折叠；有任务（可续跑/可保存/上传中）时自动展开一次
  if (jobs.length && !els.jobsDetails.open) els.jobsDetails.open = true;
  els.jobsList.textContent = '';
  if (!jobs.length) {
    const p = document.createElement('p');
    p.className = 'jobs-empty';
    p.textContent = t('tools.jobs.empty');
    els.jobsList.appendChild(p);
    return;
  }
  for (const job of jobs) {
    const row = document.createElement('article');
    row.className = 'job-row';
    row.dataset.jobId = job.id;
    row.dataset.nextAction = job.nextAction;

    const head = document.createElement('div');
    head.className = 'job-row-head';
    const name = document.createElement('p');
    name.className = 'job-name';
    name.textContent = (job.source && job.source.name) || job.id;
    const st = document.createElement('span');
    st.className = 'job-state';
    // the record says planned/paused while this tab's worker is converting
    const shownState = job.active ? 'running' : job.state;
    st.dataset.tone = STATE_TONE[shownState] || '';
    st.textContent = stateLabel(shownState);
    head.appendChild(name);
    head.appendChild(st);
    row.appendChild(head);

    const meta = document.createElement('p');
    meta.className = 'job-meta';
    const size = job.source ? fmtBytes(job.source.size) : '—';
    // committed progress only means something for a run that can continue
    meta.textContent = (job.nextAction === 'resume' || job.nextAction === 'wait')
      ? t('tools.jobs.meta', { size, committed: fmtBytes(job.committedBytes || 0) })
      : t('tools.jobs.meta.source', { size });
    if (job.result && job.result.outputBytes) {
      meta.textContent += ` · ${t('tools.jobs.result.size', { bytes: fmtBytes(job.result.outputBytes) })}`;
    }
    if (job.settings && job.settings.profileId) {
      meta.textContent += ` · ${t('tools.jobs.settings', {
        profile: t(`tools.profile.${job.settings.profileId}`),
        policy: t(`tools.jobs.policy.${job.settings.policy}`),
      })}`;
    }
    // 输出格式（prepared 起记录里就有；含旧任务回退出的 classic/fl-ome）
    if (job.outputProfile) {
      meta.textContent += ` · ${t('tools.jobs.format', {
        name: t(`tools.jobs.format.${job.outputProfile}`),
      })}`;
    }
    // 画质：只在非默认（更小文件/有损）时展示，避免给旧任务刷屏
    if (job.encodingProfile === E.ENCODING_PROFILES.COMPACT) {
      meta.textContent += ` · ${t('tools.jobs.encoding', {
        name: t('tools.jobs.encoding.compact'),
      })}`;
    }
    if (job.hasChannelJson) meta.textContent += ` · ${t('tools.jobs.channel')}`;
    row.appendChild(meta);

    const actions = document.createElement('div');
    actions.className = 'job-actions';
    const primary = job.nextAction;
    const intentPending = !!(job.intent && job.intent.state === 'pending');
    if (primary !== 'discard') {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = primary === 'export' ? 'btn btn-primary' : 'btn btn-secondary';
      btn.dataset.action = primary;
      if (intentPending && JOB_ACTION_INTENT_LABEL_KEY[primary]) {
        btn.dataset.intent = '1';
      }
      btn.textContent = t(intentPending && JOB_ACTION_INTENT_LABEL_KEY[primary]
        ? JOB_ACTION_INTENT_LABEL_KEY[primary] : JOB_ACTION_LABEL_KEY[primary]);
      btn.disabled = primary === 'export' && typeof window.showSaveFilePicker !== 'function';
      btn.addEventListener('click', () => onJobAction(primary, job));
      actions.appendChild(btn);
    }
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn btn-danger';
    del.dataset.action = 'discard';
    del.textContent = t('tools.jobs.action.discard');
    del.setAttribute('data-job-discard', job.id);
    del.addEventListener('click', () => onJobAction('discard', job));
    actions.appendChild(del);
    row.appendChild(actions);
    // C4：行内上传状态/继续/取消旧上传；上传进行中禁用删除（避免丢唯一
    // 本地产物）。必须在 actions 挂到行之后再渲染（段内 insertBefore 以
    // actions 为锚点）。
    if (page.uploadCtl && (job.state === 'ready' || job.state === 'exported')) {
      page.uploadCtl.renderRowSegment(job, actions, del);
    }

    els.jobsList.appendChild(row);
  }
  updateUploadCancelVisibility();
}

/// R1：#upload-cancel-btn 在两种形态下可见——上传进行中（C4 既有语义），
/// 或当前结果面板指向的任务还挂着待执行的自动上传意图（转换后、上传前的
/// 停止态；点击 = 撤销自动上传，产物保留、无 ingestion）。
function updateUploadCancelVisibility() {
  if (!page.uploadCtl) return;
  let pending = false;
  if (page.readyInfo) {
    const job = (page.lastJobs || []).find((j) => j.id === page.readyInfo.jobId);
    pending = !!(job && job.intent && job.intent.state === 'pending'
      && (!job.upload || job.upload.state !== 'published'));
  }
  const busyHere = !!page.readyInfo && page.uploadCtl.busyJobId() === page.readyInfo.jobId;
  els.uploadCancelBtn.hidden = !(busyHere || pending);
}

async function onJobAction(action, job) {
  clearError();
  try {
    if (action === 'discard') {
      if (!window.confirm(t('tools.jobs.discard.confirm'))) return;
      try {
        await page.runner.discardJob(job.id);
      } catch (e) {
        const info = (e && e.error) || {};
        // 遗留上传记录（没有任何标签在传）：再确认一次放弃上传才删除
        if (errCode(e) !== 'upload_active' || info.lockHeld !== false) throw e;
        if (!window.confirm(t('tools.upload.abandon.confirm'))) return;
        await page.uploadCtl.abandon(job);
        await page.runner.discardJob(job.id, { abandonUpload: true });
      }
      refreshJobs();
      return;
    }
    if (action === 'export') {
      if (typeof window.showSaveFilePicker !== 'function') return;
      const handle = await window.showSaveFilePicker({
        suggestedName: E.outputFileName(job.source && job.source.name, job),
        types: E.saveFileTypes(job),
      });
      const r = await page.runner.exportJob(job.id, () => handle.createWritable());
      page.saveMsg = { key: 'tools.result.save.done', vars: { bytes: fmtBytes(r.exportedBytes) } };
      renderSaveStatus();
      refreshJobs();
      return;
    }
    if (action === 'start' || action === 'resume') {
      // prepared/paused 任务：源副本已在浏览器临时存储，无需重选文件。
      // resume 不传设置（含输出格式/画质）→ 沿用任务记录；start（prepared）
      // 只有当可见的输出格式/画质组属于该任务（本会话刚准备、未锁定）时才
      // 传当前界面选择，否则（刷新后 / 设置组属于别的任务）不传 → 运行器用
      // 任务记录里保存的值——记录值优先于隐藏的 radio 默认值。channel.json
      // 一律沿用准备时保存在任务记录里的那份。两种动作开始后格式与画质都
      // 锁定为任务实际值。
      els.runSection.hidden = false;
      els.cancelBtn.hidden = false;
      els.runProgress.hidden = false;
      els.runBytes.hidden = false;
      els.runStatus.textContent = phaseText(action === 'resume' ? 'paused' : 'planned');
      setBeforeunload(true);
      const thisPrep = page.prep && page.prep.jobId === job.id;
      const startOutputProfile = (action === 'start' && thisPrep && !els.formatSection.hidden)
        ? outputProfileForModality(job.modality) : undefined;
      const startEncodingProfile = (action === 'start' && thisPrep && !els.qualityFieldset.hidden)
        ? encodingProfileForModality(job.modality) : undefined;
      const started = action === 'resume'
        ? await page.runner.resumeJob(job.id)
        : await page.runner.startJob(null, {
          jobId: job.id,
          profileId: selectedProfileId(),
          policy: selectedPolicy(),
          outputProfile: startOutputProfile,
          encodingProfile: startEncodingProfile,
        });
      lockOutputProfile(startOutputProfile || job.outputProfile
        || E.defaultOutputProfile(job.modality));
      lockEncodingProfile(startEncodingProfile
        || (job.encodingProfile && E.encodingFitsModality(job.encodingProfile, job.modality)
          ? job.encodingProfile : null)
        || E.defaultEncodingProfile());
      const result = await started.done;
      setBeforeunload(false);
      els.cancelBtn.hidden = true;
      els.runProgress.hidden = true;
      els.runBytes.hidden = true;
      if (result && result.ok) {
        els.runStatus.textContent = phaseText('ready');
        page.readyInfo = {
          jobId: job.id,
          outputBytes: result.result.output_bytes,
          sha256: result.validation && result.validation.sha256,
          modality: job.modality,
          outputProfile: result.result.output_profile || job.outputProfile || null,
          encoding: result.result.encoding || job.encodingProfile || null,
          result: { format: result.result.format || null },
          sourceName: job.source && job.source.name,
          channels: result.result.channels || [],
        };
        page.saveMsg = null;
        renderResultPanel();
        renderSaveStatus();
        // R1「继续转换并上传」：任务挂着待执行的上传意图时，转换完成后
        // 自动进入上传（无第二次确认；大小/格式重新准入在上传控制器内）。
        if (job.intent && job.intent.state === 'pending') {
          await page.uploadCtl.startOrContinue(job.id);
        }
      } else if (result && result.type === 'cancelled') {
        els.runStatus.textContent = phaseText('cancelled');
      } else {
        els.runStatus.textContent = phaseText('failed');
        showError((result && result.error) || { code: 'io_recoverable', message: 'job failed' });
      }
      refreshJobs();
    }
  } catch (e) {
    showError(e);
    setBeforeunload(false);
    els.cancelBtn.hidden = true;
    els.runProgress.hidden = true;
    els.runBytes.hidden = true;
    refreshJobs();
  }
}

async function refreshJobs() {
  try {
    const jobs = await page.runner.listJobs();
    renderJobs(jobs);
  } catch (e) {
    console.warn('listJobs failed', e);
  }
}

// --------------------------------------------------------- beforeunload --

function setBeforeunload(on) {
  page.beforeunloadOn = on;
}

window.addEventListener('beforeunload', (ev) => {
  if (page.beforeunloadOn) {
    ev.preventDefault();
    ev.returnValue = '';
  }
});

// ---------------------------------------------------------------- init --

async function init() {
  document.title = t('tools.doc.title');
  renderSaveStatus();
  try {
    page.runner = await SlideToolsRunner.create();
  } catch (e) {
    setPageMsg('tools.init.failed');
    showError(e);
    return;
  }
  page.runner.on('progress', onProgress);
  page.runner.on('state', onRunnerState);
  page.uploadCtl = createUploadController({
    runner: page.runner,
    t,
    onJobsRefresh: refreshJobs,
    onPublished: (jobId, slideId) => (page.convertUploadCtl
      ? page.convertUploadCtl.handlePublished(jobId, slideId) : undefined),
    onSelectJob: selectResultJob,
  });
  page.convertUploadCtl = createConvertUploadController({
    runner: page.runner,
    uploadCtl: page.uploadCtl,
    t,
    onJobsRefresh: refreshJobs,
    onFlowMessage: renderFlowMsg,
    takeFile: takeHandoffFile,
  });
  page.convertUploadCtl.installHandoffReceiver();
  refreshJobs();

  // 拖放：全页阻止默认导航（拖到页面任意位置都不会打开/替换文档），
  // 拖放区内的 drop 进入与 file input 相同的 prepareSource 流程
  for (const type of ['dragover', 'drop']) {
    window.addEventListener(type, (ev) => { ev.preventDefault(); });
  }
  els.dropZone.addEventListener('dragover', () => els.dropZone.classList.add('dragover'));
  els.dropZone.addEventListener('dragleave', () => els.dropZone.classList.remove('dragover'));
  els.dropZone.addEventListener('drop', (ev) => {
    els.dropZone.classList.remove('dragover');
    handleDropData(ev.dataTransfer);
  });
  els.dropZone.addEventListener('click', () => { els.fileInput.click(); });
  els.pickFileBtn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    els.fileInput.click();
  });
  els.fileInput.addEventListener('change', () => { onFilePicked(); });
  els.channelInput.addEventListener('change', () => { onChannelPicked(); });
  els.convertBtn.addEventListener('click', () => { onConvert(); });
  if (els.convertUploadBtn) {
    els.convertUploadBtn.addEventListener('click', () => { onConvertUpload(); });
  }
  els.cancelBtn.addEventListener('click', () => { onCancel(); });
  els.prepareCancelBtn.addEventListener('click', () => { onPrepareCancel(); });
  els.saveBtn.addEventListener('click', () => { onSave(); });
  els.persistBtn.addEventListener('click', () => { onPersist(); });
  // C4：上传是唯一主动外连入口（点击后先查能力，再判定登录/限额/格式）
  els.uploadBtn.addEventListener('click', () => {
    if (page.readyInfo) page.uploadCtl.startOrContinue(page.readyInfo.jobId);
  });
  els.uploadCancelBtn.addEventListener('click', () => { page.uploadCtl.cancel(); });
  // 测试/诊断可观测钩子（不承载任何逻辑）
  window.__stToolsReady = true;
}

const runBytesMsg = { key: null, vars: null };
function setRunBytes(key, vars) {
  runBytesMsg.key = key;
  runBytesMsg.vars = vars;
  els.runBytes.textContent = t(key, vars);
}

function onProgress(p) {
  if (p.unit === 'stage') {
    const frac = p.total ? p.done / p.total : 0;
    els.stageSection.hidden = false;
    setProgress(els.stageProgress, els.stageBar, els.stageBytes, frac,
      `${fmtBytes(p.done)} / ${fmtBytes(p.total)}`);
    return;
  }
  if (p.unit === 'level') {
    const frac = p.total ? p.done / p.total : 0;
    setProgress(els.runProgress, els.runBar, els.runBytes, frac, '');
    setRunBytes('tools.run.progress.level', {
      done: p.done, total: p.total,
      bytes: fmtBytes(p.committed_bytes || 0),
    });
  } else if (p.unit === 'tile-row') {
    setRunBytes('tools.run.progress.row', {
      level: p.level, done: p.done, total: p.total,
      bytes: fmtBytes(p.committed_bytes || 0),
    });
  }
}

function onRunnerState(s) {
  if (!s || !s.to) return;
  if (['running', 'finalizing', 'validating', 'probing', 'planned'].includes(s.to)) {
    els.runStatus.textContent = phaseText(s.to);
  }
  if (s.to === 'running' || s.to === 'finalizing' || s.to === 'validating') {
    refreshJobs();
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
