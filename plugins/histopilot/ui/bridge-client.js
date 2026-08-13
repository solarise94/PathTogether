/* =========================================================================
   HistoPilot UI bundle —— HostBridge 客户端（Stage 2 第一阶段：同源同窗口）

   形态对齐 docs/pathtogather-histopilot-platform-plugin-upgrade.md §7.5：
     request:  {kind:"request",  protocolVersion, pluginInstallationId, requestId, method, payload}
     response: {kind:"response", protocolVersion, pluginInstallationId, requestId, ok, result?, error?}
     event:    {kind:"event",    protocolVersion, pluginInstallationId, eventId, type, payload}

   第一阶段传输层 = 同窗口直接函数分发（_post 直接调用 window.HostBridgeHost._receiveFromPlugin）。
   第二阶段切 sandbox iframe 时，只需把 _post / 入口换成 window.postMessage + message 监听，
   信封字段、request/response 配对、10s 超时与主版本兼容校验逻辑全部保持不变。

   本文件是 bundle 的第一个脚本：建立 window.HistoPilot 命名空间与共享状态对象 HP.s，
   并提供与平台解耦的纯工具（i18n / 转义 / 数字格式化）+ toast / setOverlay 桥接封装。
   后续脚本（api/sse/renderer/config-panel/sessions/main）都向同一 HP 挂载能力。
   plugin 代码不读取平台全局 state/viewer/DOM selector（Stage 2 验收项）。
   ========================================================================= */
(function () {
  "use strict";
  if (window.HistoPilot && window.HistoPilot._bridge) return; // 防重复加载
  var PROTO = "1.0.0";
  var PLUGIN_ID = "histopilot";
  var HP = (window.HistoPilot = window.HistoPilot || {});
  // 共享状态对象：各脚本以 `var S = HP.s;` 捕获同一引用；main.js 在加载时填充字段。
  HP.s = HP.s || {};

  // ---------- 主版本兼容校验（major 必须相等） ----------
  function compat(v) {
    try {
      var maj = String(v || "").split(".")[0];
      return maj === PROTO.split(".")[0];
    } catch (e) { return false; }
  }

  // ---------- i18n：复用平台共享库 window.HP_I18N（i18n.js，非平台私有状态） ----------
  function t(key, vars) {
    return window.HP_I18N ? window.HP_I18N.t(key, vars) : key;
  }
  // 本轮新增的 i18n 文案（暂未落入 i18n.js 字典的兜底表）。优先取 i18n.js 的值。
  var _EXTRA_I18N = {
    "ai.fork.sending": { zh: "发送中…", en: "Sending…" },
    "anno.fork.quick": { zh: "快速问答", en: "Quick Q&A" },
    "anno.fork.quick.tip": { zh: "就此标注快速提问（轻量批注对话）", en: "Ask about this annotation (lightweight fork chat)" },
    "anno.branch.deep": { zh: "从此处深读", en: "Deep dive" },
    "anno.branch.deep.tip": { zh: "在 AI 面板从此标注开分支会话（全量工具深读）", en: "Open a branch session from here (full tools, deep read)" },
    "anno.private.badge": { zh: "私有", en: "Private" },
  };
  function tt(key) {
    try {
      var s = window.HP_I18N && window.HP_I18N.t(key);
      if (s && s !== key) return s;
    } catch (e) {}
    var lang = (window.HP_I18N && window.HP_I18N.getLang()) || "zh";
    var e = _EXTRA_I18N[key];
    return (e && (e[lang] || e.zh)) || key;
  }

  // ---------- 纯工具（与 app.js 解耦，插件自备副本） ----------
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  // AI 轨迹里的倍率：数字格式化或已带单位的字符串
  function fmtAiMag(mag) {
    if (mag === null || mag === undefined || mag === "") return "";
    if (typeof mag === "string") return mag;
    var m = Number(mag);
    if (!isFinite(m)) return String(mag);
    return (m >= 10 ? Math.round(m) : m.toFixed(1)) + "x";
  }
  function fmtNum(v) { return v == null ? "?" : Math.round(v); }
  function truncateStr(s, n) {
    s = String(s == null ? "" : s);
    if (s.length <= n) return s;
    return s.slice(0, n) + "…";
  }
  // iMessage 顶部时间分隔文案：按当前界面语言走 Intl 本地化
  function fmtMsgTs(d) {
    var lang = (window.HP_I18N && window.HP_I18N.getLang()) || "zh";
    var locale = lang === "zh" ? "zh-CN" : "en-US";
    var now = new Date();
    var hm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
    var sameDay = d.toDateString() === now.toDateString();
    try {
      var wd = new Intl.DateTimeFormat(locale, { weekday: "short" }).format(d);
      if (sameDay) return wd + " " + hm;
      if (d.getFullYear() === now.getFullYear()) {
        var datePart = lang === "zh"
          ? (d.getMonth() + 1) + "月" + d.getDate() + "日"
          : new Intl.DateTimeFormat(locale, { month: "short", day: "numeric" }).format(d);
        return datePart + " " + wd + " " + hm;
      }
      var fullPart = lang === "zh"
        ? d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日"
        : new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", year: "numeric" }).format(d);
      return fullPart + " " + wd + " " + hm;
    } catch (e) { return d.toLocaleString(); }
  }

  // ---------- HostBridge 客户端 ----------
  var reqSeq = 0;
  var evtSeq = 0;
  var pending = {}; // requestId -> {resolve, reject, timer}
  var reqHandlers = {}; // Host→Plugin request: method -> fn(payload)->result|promise
  var evtHandlers = {}; // Host→Plugin event: type -> fn(payload)

  // 发送：把信封交给 host 入口。同窗口阶段直接调用 host._receiveFromPlugin。
  function _post(env) {
    var host = window.HostBridgeHost;
    if (host && typeof host._receiveFromPlugin === "function") {
      try { host._receiveFromPlugin(env); } catch (e) {}
    }
    // host 未就绪（平台关闭插件 flag）：静默丢弃，不阻塞插件内部逻辑
  }

  // Plugin→Host request（需 response，10s 超时 → reject {code:"bridge_timeout"}）
  function request(method, payload) {
    return new Promise(function (resolve, reject) {
      var id = "hp_req_" + (++reqSeq);
      var env = {
        kind: "request", protocolVersion: PROTO, pluginInstallationId: PLUGIN_ID,
        requestId: id, method: method, payload: payload == null ? {} : payload,
      };
      var timer = setTimeout(function () {
        if (pending[id]) {
          delete pending[id];
          reject({ code: "bridge_timeout", message: "host 未在 10s 内响应 " + method });
        }
      }, 10000);
      pending[id] = { resolve: resolve, reject: reject, timer: timer };
      _post(env);
    });
  }

  // Plugin→Host event（单向，不等 ack，不参与 10s 超时；对齐 §7.5 notification.show 语义）
  function emit(type, payload) {
    var id = "hp_evt_" + (++evtSeq);
    _post({
      kind: "event", protocolVersion: PROTO, pluginInstallationId: PLUGIN_ID,
      eventId: id, type: type, payload: payload == null ? {} : payload,
    });
  }

  // 处理 host 发来的 request：跑注册的 handler，回 response（未知 method 回 unknown_method，不崩）
  function _handleHostRequest(env) {
    var fn = reqHandlers[env.method];
    Promise.resolve().then(function () {
      if (!fn) throw { code: "unknown_method", message: "plugin 未实现 " + env.method };
      return fn(env.payload || {});
    }).then(function (result) {
      _post({
        kind: "response", protocolVersion: PROTO, pluginInstallationId: PLUGIN_ID,
        requestId: env.requestId, ok: true, result: result == null ? null : result,
      });
    }, function (err) {
      _post({
        kind: "response", protocolVersion: PROTO, pluginInstallationId: PLUGIN_ID,
        requestId: env.requestId, ok: false,
        error: (err && err.code) ? err : { code: "plugin_error", message: String((err && err.message) || err) },
      });
    });
  }

  function _handleHostEvent(env) {
    var fn = evtHandlers[env.type];
    try { if (fn) fn(env.payload || {}); } catch (e) {}
  }

  // host→plugin 入口（host 调用）。处理 response（配对 pending）/ request / event。
  function _onHostMessage(env) {
    if (!env) return;
    if (env.protocolVersion && !compat(env.protocolVersion)) return; // 主版本不兼容：忽略，不崩
    if (env.kind === "response") {
      var p = pending[env.requestId];
      if (!p) return;
      clearTimeout(p.timer);
      delete pending[env.requestId];
      if (env.ok) p.resolve(env.result);
      else p.reject(env.error || { code: "bridge_error" });
      return;
    }
    if (env.kind === "request") { _handleHostRequest(env); return; }
    if (env.kind === "event") { _handleHostEvent(env); return; }
  }

  // ---------- 平台能力的桥接封装（替代插件直接写平台 DOM/canvas/全局） ----------
  // toast：单向 event 请求平台显示 toast（平台侧 toast()）。
  function toast(msg, type) {
    emit("notification.show", { msg: String(msg == null ? "" : msg), type: type || "info" });
  }
  // 叠加层：请求平台把 boxes 画到 anno-canvas（替代插件直接写 aiOverlay/redrawAnnoCanvas）。
  // boxes=[] 清除。每个 box: {x,y,w,h,magnification?,label?,color?}（magnification 供平台画标签）。
  function setOverlay(boxes) {
    emit("viewer.highlight", { boxes: Array.isArray(boxes) ? boxes : [] });
  }

  // ---------- 对外暴露 ----------
  HP.t = t;
  HP.tt = tt;
  HP.esc = esc;
  HP.fmtAiMag = fmtAiMag;
  HP.fmtNum = fmtNum;
  HP.truncateStr = truncateStr;
  HP.fmtMsgTs = fmtMsgTs;
  HP.toast = toast;
  HP.setOverlay = setOverlay;
  HP.bridge = { request: request, emit: emit };
  HP.onRequest = function (method, fn) { reqHandlers[method] = fn; };
  HP.onEvent = function (type, fn) { evtHandlers[type] = fn; };
  HP._onHostMessage = _onHostMessage;
  HP.PROTO = PROTO;
  HP._bridge = true;
})();
