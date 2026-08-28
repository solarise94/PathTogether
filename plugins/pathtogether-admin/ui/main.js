/* =========================================================================
   pathtogether-admin 桥接客户端 + 概览壳（PR3 骨架）

   docs/admin-billing-plugin-implementation-plan.md §8.3/§8.4：
     - 本页运行在 /admin 宿主页的 opaque iframe（sandbox="allow-scripts"）内；
     - 宿主在每次 iframe load 后 postMessage 一条 {kind:"init"} 携带一次性
       256-bit nonce 与协议版本；本端保存 nonce（仅内存），此后所有请求回带
       nonce + 本次会话内递增的 requestId；
     - 响应按 requestId 配对；超时（15s）与拒绝都有稳定错误码；
     - opaque origin：不能读写 localStorage/cookie，全部状态在内存。
   业务页面（overview/users/billing 等）由 PR3b 填充；本文件只实现握手与
   admin.auth.get 的最小渲染。
   ========================================================================= */
(function () {
  "use strict";

  var BRIDGE = "admin";
  var PROTOCOL_FALLBACK = "1.0.0";
  var REQUEST_TIMEOUT_MS = 15000;

  // ---- 内存态（opaque origin：无 localStorage/cookie，一切仅存本页生命周期）----
  var state = {
    nonce: null,            // 宿主 init 下发的一次性 nonce（仅内存）
    protocolVersion: null,  // 宿主声明的桥协议版本
    granted: [],            // 宿主确认的 manifest adminPermissions（展示用）
    seq: 0,                 // 请求序号（本页生命周期内单调递增）
    pending: {},            // requestId -> {resolve, reject, timer, method}
    dead: false,            // 宿主作废（reload/登出/切换）后不再发任何请求
  };

  var els = {
    handshake: document.getElementById("adm-handshake-status"),
    handshakeCard: document.getElementById("adm-handshake-card"),
    overviewCard: document.getElementById("adm-overview-card"),
    actorInfo: document.getElementById("adm-actor-info"),
    errorCard: document.getElementById("adm-error-card"),
    errorText: document.getElementById("adm-error-text"),
  };

  function setHandshake(text) {
    if (els.handshake) els.handshake.textContent = text;
  }

  function showError(code, message) {
    if (!els.errorCard || !els.errorText) return;
    els.errorCard.hidden = false;
    els.errorText.textContent = (code || "error") + (message ? (": " + message) : "");
  }

  function kvRow(dl, key, value) {
    var dt = document.createElement("dt");
    dt.textContent = key;
    var dd = document.createElement("dd");
    dd.textContent = value == null ? "—" : String(value);
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  // ---- 请求：nonce + 递增 requestId；targetOrigin "*" —— iframe 是 opaque
  //      origin，读不到宿主 origin；安全边界在宿主侧（精确 WindowProxy +
  //      高熵 nonce + 服务端 owner/CSRF 复核，docs §8.3）。
  function request(method, payload) {
    return new Promise(function (resolve, reject) {
      if (state.dead) {
        reject({ code: "bridge_invalidated", message: "宿主已作废本次桥接会话" });
        return;
      }
      if (!state.nonce) {
        reject({ code: "not_ready", message: "尚未收到宿主初始化消息" });
        return;
      }
      var requestId = "adm_req_" + (++state.seq);
      var env = {
        kind: "request",
        bridge: BRIDGE,
        protocolVersion: state.protocolVersion || PROTOCOL_FALLBACK,
        nonce: state.nonce,
        requestId: requestId,
        method: method,
        payload: payload || {},
      };
      var timer = setTimeout(function () {
        if (!state.pending[requestId]) return;
        delete state.pending[requestId];
        reject({ code: "bridge_timeout", message: "宿主未在 " + REQUEST_TIMEOUT_MS + "ms 内响应 " + method });
      }, REQUEST_TIMEOUT_MS);
      state.pending[requestId] = { resolve: resolve, reject: reject, timer: timer, method: method };
      window.parent.postMessage(env, "*");
    });
  }

  function failAllPending(err) {
    Object.keys(state.pending).forEach(function (requestId) {
      var p = state.pending[requestId];
      clearTimeout(p.timer);
      p.reject(err);
      delete state.pending[requestId];
    });
  }

  function onMessage(event) {
    var env = event.data;
    if (!env || typeof env !== "object" || env.bridge !== BRIDGE) return;

    if (env.kind === "init") {
      // 握手：宿主每次 load 重新下发 nonce —— 直接覆盖旧值并丢弃旧在途请求
      state.nonce = typeof env.nonce === "string" ? env.nonce : null;
      state.protocolVersion = env.protocolVersion || PROTOCOL_FALLBACK;
      state.granted = Array.isArray(env.adminPermissions) ? env.adminPermissions.slice() : [];
      state.dead = false;
      setHandshake("握手成功（protocolVersion=" + state.protocolVersion +
        "，已授权管理能力 " + state.granted.length + " 项）");
      loadIdentity();
      return;
    }

    if (env.kind === "response") {
      var p = state.pending[env.requestId];
      if (!p) return; // 未知/已超时/已作废的响应：静默丢弃
      clearTimeout(p.timer);
      delete state.pending[env.requestId];
      if (env.ok) {
        p.resolve(env.result == null ? null : env.result);
      } else {
        p.reject(env.error || { code: "bridge_error", message: "宿主返回未知错误" });
      }
      return;
    }

    if (env.kind === "event" && env.type === "bridge_invalidated") {
      // 宿主作废（reload/登出/插件切换）：立即停止一切请求
      state.dead = true;
      state.nonce = null;
      failAllPending({ code: "bridge_invalidated", message: env.message || "宿主已作废桥接会话" });
      setHandshake("桥接会话已作废（" + (env.reason || "unknown") + "），等待重新握手…");
    }
  }

  function loadIdentity() {
    request("admin.auth.get", {}).then(function (identity) {
      if (!els.overviewCard || !els.actorInfo) return;
      els.overviewCard.hidden = false;
      els.actorInfo.textContent = "";
      kvRow(els.actorInfo, "当前 actor 角色", identity && identity.role);
      kvRow(els.actorInfo, "登录账号（掩码）", identity && identity.loginIdMasked);
      kvRow(els.actorInfo, "预览态", identity && identity.previewActive ? "是（管理端只读）" : "否");
    }).catch(function (err) {
      showError(err && err.code ? err.code : "bridge_error",
        err && err.message ? err.message : "admin.auth.get 失败");
    });
  }

  window.addEventListener("message", onMessage);

  // 导出（仅调试/测试用；不含 nonce 读取器）
  window.PathTogetherAdminClient = {
    request: request,
    handshakeState: function () {
      return {
        ready: !!state.nonce && !state.dead,
        protocolVersion: state.protocolVersion,
        grantedCount: state.granted.length,
      };
    },
  };
})();
