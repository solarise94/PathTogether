/* =========================================================================
   PluginSDK —— 通用插件 HostBridge 客户端（Stage 5-2，docs §7.5）

   UMD 风格工厂：
     - 浏览器 <script>            → 挂 window.PluginSDK（createPluginBridge）；
     - node（vitest 单测）        → module.exports = { createPluginBridge }。

   用法：
     var bridge = PluginSDK.createPluginBridge({ pluginId: "sample-annotator" });
     await bridge.negotiate();                 // 发 bridge.negotiate；不兼容 reject {code:"version_incompatible"}
     var meta = await bridge.request("slide.getCurrent");
     bridge.emit("notification.show", { msg: "hi" });
     bridge.onEvent("x", fn); bridge.onRequest("y", fn);

   信封 / 10s 超时 / requestId 配对 / 版本字段逻辑对齐
   plugins/histopilot/ui/bridge-client.js（Stage 2 参考实现），但**不 import 也不依赖
   window.HistoPilot 任何字段**——本模块是通用 SDK，histopilot 是内置特权插件，两者解耦。

   传输层 = 同窗口直接函数分发：发请求调 window.HostBridgeHost._receiveFromPlugin；
   同时把本实例的 _onHostMessage 注册进 host 端注册表
   （window.HostBridgeHost.registerPlugin(pluginId, fn)），host 才能 post 响应回来。
   host-bridge.js 维护 pluginInstallationId → 接收函数 的注册表，未注册时回落到
   window.HistoPilot._onHostMessage（histopilot 默认兼容，行为不变）。

   protocolVersion 取 window.BridgeVersion.PROTOCOL_VERSION（缺失兜底 "1.0.0"）。
   ========================================================================= */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module && typeof module.exports === "object") {
    module.exports = factory();
  } else {
    root.PluginSDK = root.PluginSDK || factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // 桥协议版本工具（Stage 5-1 共享模块）。host-bridge.js / bridge-client.js 复用其
  // compat；未加载时用内联兜底，保证插件不崩。
  var BV = (typeof window !== "undefined" && window.BridgeVersion) ? window.BridgeVersion : null;
  var DEFAULT_PROTO = (BV && BV.PROTOCOL_VERSION) || "1.0.0";

  // 运行时主版本兼容校验（强制同 major）。优先共享模块，缺失内联兜底。
  function compat(v, proto) {
    if (BV) return BV.compat(v, proto);
    try {
      var maj = String(v || "").split(".")[0];
      return maj === (proto || DEFAULT_PROTO).split(".")[0];
    } catch (e) { return false; }
  }

  function createPluginBridge(opts) {
    opts = opts || {};
    var pluginId = opts.pluginId || "unknown-plugin";
    var timeoutMs = (opts.timeoutMs == null) ? 10000 : opts.timeoutMs; // 响应超时（可注入，测试用短值）
    var proto = opts.protocolVersion || DEFAULT_PROTO;

    var reqSeq = 0, evtSeq = 0;
    var pending = {};    // requestId -> {resolve, reject, timer}
    var reqHandlers = {}; // Host→Plugin request: method -> fn(payload)->result|promise
    var evtHandlers = {}; // Host→Plugin event: type -> fn(payload)

    // 发送：把信封交给 host 入口。同窗口阶段走 host.postFromPlugin，由 host 盖章身份。
    function _post(env) {
      var host = (typeof window !== "undefined") ? window.HostBridgeHost : null;
      if (host && typeof host.postFromPlugin === "function") {
        try { host.postFromPlugin(pluginId, env); } catch (e) {}
      } else if (host && typeof host._receiveFromPlugin === "function") {
        try { host._receiveFromPlugin(env); } catch (e) {}
      }
      // host 未就绪（平台关闭插件 flag）：静默丢弃，不阻塞插件内部逻辑
    }

    // Plugin→Host request（需 response，timeoutMs 超时 → reject {code:"bridge_timeout"}）
    function request(method, payload) {
      return new Promise(function (resolve, reject) {
        var id = "req_" + pluginId + "_" + (++reqSeq);
        var env = {
          kind: "request", protocolVersion: proto, pluginInstallationId: pluginId,
          requestId: id, method: method, payload: payload == null ? {} : payload,
        };
        var timer = setTimeout(function () {
          if (pending[id]) {
            delete pending[id];
            reject({ code: "bridge_timeout", message: "host 未在 " + timeoutMs + "ms 内响应 " + method });
          }
        }, timeoutMs);
        pending[id] = { resolve: resolve, reject: reject, timer: timer };
        _post(env);
      });
    }

    // Plugin→Host event（单向，不等 ack，不参与超时；对齐 §7.5 notification.show 语义）
    function emit(type, payload) {
      var id = "evt_" + pluginId + "_" + (++evtSeq);
      _post({
        kind: "event", protocolVersion: proto, pluginInstallationId: pluginId,
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
          kind: "response", protocolVersion: proto, pluginInstallationId: pluginId,
          requestId: env.requestId, ok: true, result: result == null ? null : result,
        });
      }, function (err) {
        _post({
          kind: "response", protocolVersion: proto, pluginInstallationId: pluginId,
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
      if (env.protocolVersion && !compat(env.protocolVersion, proto)) return; // 主版本不兼容：忽略，不崩
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

    // 握手期桥协议版本协商：发 bridge.negotiate {protocolVersion}，host 兼容 resolve；
    // 不兼容（host 回 ok:false, error={code:"version_incompatible",...}）→ reject 同 code。
    function negotiate() {
      return new Promise(function (resolve, reject) {
        request("bridge.negotiate", { protocolVersion: proto }).then(function (res) {
          resolve(res); // {ok:true, protocolVersion}
        }, function (err) {
          reject(err || { code: "bridge_error" });
        });
      });
    }

    function onRequest(method, fn) { reqHandlers[method] = fn; }
    function onEvent(type, fn) { evtHandlers[type] = fn; }

    // 把本实例的接收函数注册进 host 传输层，host 才能按 pluginInstallationId 路由响应/
    // 事件回来。host 端 HostBridgeHost.registerPlugin(pluginId, fn) 维护注册表。
    function _registerReceiver() {
      var host = (typeof window !== "undefined") ? window.HostBridgeHost : null;
      if (host && typeof host.registerPlugin === "function") {
        try { host.registerPlugin(pluginId, _onHostMessage); } catch (e) {}
      }
    }
    _registerReceiver();

    return {
      pluginId: pluginId,
      negotiate: negotiate,
      request: request,
      emit: emit,
      onRequest: onRequest,
      onEvent: onEvent,
      _onHostMessage: _onHostMessage,
    };
  }

  return { createPluginBridge: createPluginBridge };
});
