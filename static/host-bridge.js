/* =========================================================================
   HostBridge —— 平台 host 端（Stage 2 第一阶段：同源同窗口）

   与 plugins/histopilot/ui/bridge-client.js 对称：信封形态对齐 docs §7.5，
   第一阶段传输层 = 同窗口直接函数分发（_receiveFromPlugin / _onHostMessage 互调）。
   第二阶段切 sandbox iframe 时，把 _post 与入口换成 window.postMessage + message
   监听，信封字段、request/response 配对、10s 超时与主版本兼容校验逻辑保持不变。

   平台业务代码（app.js）通过 window.HostBridgeHost：
     - onRequest(method, fn)        注册 Plugin→Host request 的处理函数
     - onEvent(type, fn)            注册 Plugin→Host event 的处理函数
     - emit(type, payload)          向插件发 Host→Plugin event（如 slide.opened）
     - request(method, payload)     向插件发 Host→Plugin request（如 panel.toggle/branch.open/fork.open）
   host 端校验：protocolVersion major 兼容检查、未知 method/type 返回 unknown_method/忽略，不崩。
   ========================================================================= */
(function () {
  "use strict";
  if (window.HostBridgeHost && window.HostBridgeHost._host) return; // 防重复加载
  // 桥协议版本工具（Stage 5-1）：优先用共享模块 static/bridge-version.js（在
  // index.html 中先于本文件加载）；未加载时用下方内联兜底，保证 host 不崩。
  var BV = (typeof window.BridgeVersion !== "undefined") ? window.BridgeVersion : null;
  var PROTO = BV ? BV.PROTOCOL_VERSION : "1.0.0";
  var PLUGIN_ID = "histopilot";
  var reqSeq = 0, evtSeq = 0;
  var pending = {}; // requestId -> {resolve, reject, timer}
  var reqHandlers = {}; // Plugin→Host request: method -> fn(payload)->result|promise
  var evtHandlers = {}; // Plugin→Host event: type -> fn(payload)

  function pluginReady() {
    return !!(window.HistoPilot && typeof window.HistoPilot._onHostMessage === "function");
  }

  // 运行时主版本兼容校验（强制同 major）。优先走共享模块，缺失时内联兜底。
  function compat(v) {
    if (BV) return BV.compat(v, PROTO);
    try { return String(v || "").split(".")[0] === PROTO.split(".")[0]; }
    catch (e) { return false; }
  }

  // 发送：把信封交给插件入口。同窗口阶段直接调用 HistoPilot._onHostMessage。
  function _post(env) {
    if (pluginReady()) {
      try { window.HistoPilot._onHostMessage(env); } catch (e) {}
    }
    // 插件未加载（HISTOPILOT_UI_ENABLED=0）：静默丢弃，平台人工读片不受影响
  }

  // Host→Plugin request（需 response，10s 超时 → reject {code:"bridge_timeout"}）
  function request(method, payload) {
    return new Promise(function (resolve, reject) {
      var id = "hp_req_" + (++reqSeq);
      var env = {
        kind: "request", protocolVersion: PROTO, pluginInstallationId: PLUGIN_ID,
        requestId: id, method: method, payload: payload == null ? {} : payload,
      };
      var timer = setTimeout(function () {
        if (pending[id]) { delete pending[id]; reject({ code: "bridge_timeout", message: "plugin 未在 10s 内响应 " + method }); }
      }, 10000);
      pending[id] = { resolve: resolve, reject: reject, timer: timer };
      _post(env);
    });
  }

  // Host→Plugin event（单向，不等 ack，不参与 10s 超时）
  function emit(type, payload) {
    var id = "hp_evt_" + (++evtSeq);
    _post({
      kind: "event", protocolVersion: PROTO, pluginInstallationId: PLUGIN_ID,
      eventId: id, type: type, payload: payload == null ? {} : payload,
    });
  }

  // 处理插件发来的 request：跑注册的 handler，回 response（未知 method 回 unknown_method，不崩）
  function _handlePluginRequest(env) {
    var fn = reqHandlers[env.method];
    Promise.resolve().then(function () {
      if (!fn) throw { code: "unknown_method", message: "host 未实现 " + env.method };
      return fn(env.payload || {});
    }).then(function (result) {
      _post({ kind: "response", protocolVersion: PROTO, pluginInstallationId: PLUGIN_ID,
              requestId: env.requestId, ok: true, result: result == null ? null : result });
    }, function (err) {
      _post({ kind: "response", protocolVersion: PROTO, pluginInstallationId: PLUGIN_ID,
              requestId: env.requestId, ok: false,
              error: (err && err.code) ? err : { code: "host_error", message: String((err && err.message) || err) } });
    });
  }

  function _handlePluginEvent(env) {
    var fn = evtHandlers[env.type];
    try { if (fn) fn(env.payload || {}); } catch (e) {}
  }

  // 插件→host 入口（插件调用）。处理 response（配对 pending）/ request / event。
  function _receiveFromPlugin(env) {
    if (!env) return;
    if (env.protocolVersion && !compat(env.protocolVersion)) {
      // 主版本不兼容：request 回错，event/response 忽略，不崩
      if (env.kind === "request") {
        _post({ kind: "response", protocolVersion: PROTO, pluginInstallationId: PLUGIN_ID,
                requestId: env.requestId, ok: false, error: { code: "version_mismatch", message: "协议主版本不兼容" } });
      }
      return;
    }
    if (env.kind === "response") {
      var p = pending[env.requestId];
      if (!p) return;
      clearTimeout(p.timer);
      delete pending[env.requestId];
      if (env.ok) p.resolve(env.result);
      else p.reject(env.error || { code: "bridge_error" });
      return;
    }
    if (env.kind === "request") { _handlePluginRequest(env); return; }
    if (env.kind === "event") { _handlePluginEvent(env); return; }
  }

  window.HostBridgeHost = {
    _host: true,
    PROTO: PROTO,
    pluginReady: pluginReady,
    request: request,
    emit: emit,
    onRequest: function (method, fn) { reqHandlers[method] = fn; },
    onEvent: function (type, fn) { evtHandlers[type] = fn; },
    _receiveFromPlugin: _receiveFromPlugin,
  };
})();
