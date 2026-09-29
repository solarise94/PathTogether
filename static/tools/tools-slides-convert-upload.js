// tools-slides-convert-upload.js — R1 一键转换并上传（drain 计划 §3.1）。
// 页面第三种入口（转换并上传 / 仅转换并保存 / 结果区手动上传）的一键编排：
// 点击后按序「本机转换 → 完整校验 → 最终大小/格式准入 → COS 上传 → 平台可查
// 看」，中间不再出现第二次上传确认。转换与校验仍在 C2 运行器（WASM + OPFS）
// 内完成；上传复用 C4 上传控制器（tools-slides-upload.js，共享 COS 引擎）。
//
// 硬约束（§3.1）：
//  - 点击之前零 /api/ 请求（能力预检在本模块内、点击后才发起）；
//  - 一次点击授权整条链：转换成功后自动进入上传，不边转换边上传、
//    不上传原始 KFB/KFBF、不调用后端转换（只发 ready 产物分块）；
//  - 转换前完成登录与能力预检，最终文件仍由上传控制器重新准入（大小/格式）；
//  - 上传意图（授权账号 + 目标）持久化在 OPFS 任务记录（record.intent，
//    runner.setJobIntent）：刷新/崩溃后重开只展示「继续转换并上传」/
//    「继续上传」，页面加载绝不自动传输；
//  - 转换失败/取消不建 ingestion；超限/不可查看停止自动上传、保留产物；
//  - 工作台交接（handoff）：同源 popup 经 postMessage 传 File（structured
//    clone，零额外整文件复制——唯一副本是运行器既有的 OPFS source.bin），
//    携带显式项目目标；发布后由本页关联项目（权限检查在服务端）并通知
//    打开者刷新列表。
'use strict';

import * as E from './slide-transform/engine.js';
import { fetchCapability } from './tools-slides-upload.js';

const HANDOFF_MSG = 'pt:convert-upload-handoff';
const HANDOFF_ACK = 'pt:convert-upload-ack';
const HANDOFF_DONE = 'pt:convert-upload-published';

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

/// 页面版 apiFetch（与 tools-slides-upload.js 同语义：非安全方法带 CSRF，
/// 401 不重定向——由调用方分类处理）。
export function pageApiFetch(url, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
    const tok = csrfToken();
    if (tok) opts.headers = { ...(opts.headers || {}), 'X-CSRF-Token': tok };
  }
  return fetch(url, { credentials: 'same-origin', ...opts });
}

/// 目标显示名（工作台交接横幅 / 意图回显用）。
function targetLabel(t, target) {
  if (target && target.project) return t('tools.handoff.target.project', { pid: target.project });
  if (target && target.newProject && target.newProject.name) {
    return t('tools.handoff.target.new', { name: target.newProject.name });
  }
  return t('tools.handoff.target.unfiled');
}

export function createConvertUploadController({
  runner, uploadCtl, t, onJobsRefresh, onFlowMessage, takeFile,
}) {
  const state = {
    running: false,        // 一键链进行中（转换或上传任一阶段）
    handoff: null,         // {file, target, source, port} —— 工作台交接的文件与目标
  };

  /// 流程状态文案（页级 #page-status，经页面注入的 onFlowMessage 渲染——
  /// 「去登录」链接也由页面构造；这里只维护可被语言切换重放的键值对）。
  const flowMsg = { key: null, vars: null };
  function setFlow(key, vars) {
    flowMsg.key = key;
    flowMsg.vars = vars || null;
    if (onFlowMessage) onFlowMessage(key, vars);
  }

  /// 转换前预检（点击后第一个网络动作）：登录（401 → 登录链接，不跳转）、
  /// 能力可用性。预检通过即把上传意图落盘——此后刷新/崩溃都能恢复出
  /// 「继续转换并上传」。返回规范化 capability | {authRequired} | {error}。
  async function precheckAndBind(jobId, target) {
    let cap;
    try {
      cap = await fetchCapability();
    } catch (e) {
      return { error: 'offline' };
    }
    if (cap.authRequired) return { authRequired: true };
    const cfg = window.HP_COS_UPLOAD
      ? window.HP_COS_UPLOAD.resolveConfig(cap.cos_upload) : null;
    if (!cfg) return { error: 'capability_off' };
    // 意图绑定授权账号（能力端点 account 字段）；换账号续传须重新确认
    await runner.setJobIntent(jobId, {
      state: 'pending',
      account: typeof cap.account === 'string' ? cap.account : '',
      target: target || null,
      channel: state.handoff ? 'workbench' : 'tool',
      createdAt: E.nowIso(),
    });
    return { cap, cfg };
  }

  /// 一键入口（页面「转换并上传」按钮）：预检 → 意图 → 转换（由页面注入的
  /// runConvert 驱动，成功后渲染结果面板）→ 自动上传（无第二次确认）。
  /// runConvert 返回与 onConvert 相同语义的 result（{ok}|{type:'cancelled'}|失败）。
  /// running 守卫从预检前就生效（预检期间重复点击不得并发开两条链）。
  async function start({ jobId, target, runConvert }) {
    if (state.running) return { stopped: 'busy' };
    state.running = true;
    try {
      const pre = await precheckAndBind(jobId, target);
      if (pre.authRequired) {
        setFlow('tools.cu.login.required');
        return { stopped: 'login' };
      }
      if (pre.error === 'offline') {
        setFlow('tools.cu.offline');
        return { stopped: 'offline' };
      }
      if (pre.error === 'capability_off') {
        setFlow('tools.cu.capability.off');
        return { stopped: 'capability' };
      }
      setFlow('tools.cu.chain.authorized');
      const result = await runConvert();
      if (!result || !result.ok) {
        // 转换失败/取消：撤销自动上传意图（不建 ingestion 由上传控制器保证——
        // 它只从 ready/exported 起步，失败任务到不了那里；这里把意图也收掉，
        // 任务列表不再显示「继续转换并上传」）。
        await runner.setJobIntent(jobId, {
          state: 'revoked', revokedReason: 'convert_failed', revokedAt: E.nowIso(),
        }).catch(() => { /* 记录写失败不掩盖转换失败本身 */ });
        if (onJobsRefresh) onJobsRefresh();
        return result || { stopped: 'convert' };
      }
      // 转换完成：自动进入上传（C4 控制器内完成最终大小/格式重新准入——
      // 超限/不可查看在那里停止并保留产物）。无额外确认。
      await uploadCtl.startOrContinue(jobId);
      return { ok: true };
    } finally {
      state.running = false;
    }
  }

  /// 发布收口（C4 控制器 onPublished 回调；已发布行的「重试加入项目」也走
  /// 这里）：先把工作台交接的显式项目目标关联上（权限检查在服务端
  /// /api/project/<pid>/slides），成功（或无目标）后才把意图标记 done 并
  /// 通知打开者。关联失败时意图保持 pending 并记下原因——切片已发布在
  /// 「未归类」，已发布行提供点击重试（页面加载不自动发请求）。关联是幂等的
  /// （重复加入同一切片无副作用；新建项目带交接时生成的幂等键，且建成后
  /// 先把 pid 写回目标，重试不再新建）。
  async function handlePublished(jobId, slideId) {
    const job = await runner.getJob(jobId).catch(() => null);
    const intent = job && job.intent;
    if (!intent || intent.state !== 'pending') return;
    const target = intent.target;
    let pid = null;
    if (target && slideId) {
      try {
        pid = await associateTarget(jobId, target, slideId);
      } catch (e) {
        await runner.setJobIntent(jobId, { assocError: E.errText(e) })
          .catch(() => { /* */ });
        setFlow('tools.cu.assoc.fail', { e: E.errText(e) });
        notifyOpener(jobId, slideId, null);
        return { assocFailed: true };
      }
      setFlow(pid ? 'tools.cu.assoc.ok' : null, pid ? { pid } : null);
    }
    await runner.setJobIntent(jobId, {
      state: 'done', doneAt: E.nowIso(), projectId: pid, assocError: null,
    }).catch(() => { /* */ });
    notifyOpener(jobId, slideId, pid);
    return { ok: true, projectId: pid };
  }

  /// 目标 → 项目 pid（「新项目」仅在上传成功后创建）并把切片加入该项目。
  /// 失败抛错（调用方保持意图 pending）。
  async function associateTarget(jobId, target, slideId) {
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
      await runner.setJobIntent(jobId, { target: { ...target, project: pid } });
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

  /// 通知打开者（工作台）发布结果——仅同源、仅结构化小消息（slide id）。
  /// 打开者已关闭时 postMessage 抛错/无处投递：目标关联已在服务端完成，
  /// 不影响结果。
  function notifyOpener(jobId, slideId, pid) {
    const port = state.handoff && state.handoff.port;
    if (!port) return;
    try {
      port.postMessage({
        type: HANDOFF_DONE, jobId, slideId: slideId || null, projectId: pid || null,
      }, window.location.origin);
    } catch { /* */ }
  }

  // ------------------------------------------------------ 工作台交接接收 --

  /// 接收工作台 postMessage 交接的 File（structured clone，零额外复制——
  /// 唯一副本是运行器 staging 的 OPFS source.bin）。同源校验 + 去重（重复
  /// 投递只 ack）。takeFile 由页面注入：走与手动选择完全相同的探测/磁盘
  /// 确认流程。
  function acceptHandoff(data, source) {
    const file = data && data.file;
    if (!file || typeof file.slice !== 'function') return;
    const ack = () => {
      try {
        source.postMessage({
          type: HANDOFF_ACK, name: file.name, size: file.size,
        }, window.location.origin);
      } catch { /* 打开者已关闭：文件照常保留在本页使用 */ }
    };
    if (state.handoff) { ack(); return; }   // 已接收过：幂等 ack
    state.handoff = {
      file, target: data.target || null, source: 'workbench', port: source,
    };
    ack();
    setFlow('tools.handoff.received', {
      name: file.name, target: targetLabel(t, state.handoff.target),
    });
    if (takeFile) takeFile(file);   // 走与手动选择完全相同的探测/磁盘确认流程
  }

  function installHandoffReceiver() {
    window.addEventListener('message', (ev) => {
      if (ev.origin !== window.location.origin) return;
      if (ev.data && ev.data.type === HANDOFF_MSG) acceptHandoff(ev.data, ev.source);
    });
  }

  function handoffTarget() {
    return state.handoff ? state.handoff.target : null;
  }

  function isRunning() { return state.running; }

  function rerenderForLang() {
    if (flowMsg.key) setFlow(flowMsg.key, flowMsg.vars);
  }

  return {
    start, handlePublished, installHandoffReceiver,
    handoffTarget, isRunning, rerenderForLang, setFlow,
    hasHandoff: () => !!state.handoff,
    handoffFile: () => (state.handoff ? state.handoff.file : null),
  };
}
