/* =========================================================================
   AdminBridge —— admin.workspace 宿主侧（PR3，docs §8.3/§8.4）

   信任模型（与 viewer HostBridge 完全不同的一套）：
     - admin 插件运行在 opaque iframe（sandbox="allow-scripts"，无
       allow-same-origin）内：读不到平台 Cookie / CSRF token / 父页 DOM，也做
       不了同源 fetch —— 数据唯一通道是本桥；
     - iframe 的 message event.origin 恒为 "null"，**不能**用 origin 鉴权；
     - 每次 iframe load 由宿主生成 256-bit 一次性 nonce（crypto.getRandomValues），
       保存**确切**的 iframe.contentWindow 引用并随 init 消息下发。init 使用
       targetOrigin "*" 是安全的：opaque origin 读不出宿主 origin，而真正的
       安全边界 = 精确 WindowProxy 匹配 + 高熵一次性 nonce + 服务端对每个
       Admin API 的 owner/CSRF 复核（本桥的放行绝不替代服务端授权）；
     - 只接受同时满足：event.source === contentWindow、nonce 属于当前 load、
       协议版本同 major、requestId 本次 load 内唯一（防重放）、method 在固定
       表、参数过 schema、所需 adminPermission 已在 manifest 申请、当前 actor
       仍是 owner（每条消息回查 /api/auth/info）；
     - iframe reload / 登出（401）/ 插件切换 / 页面卸载 → 立即作废 nonce 与
       全部在途请求（reject + 通知 iframe）；
     - 宿主用 fetch + CSRF cookie（与 app.js 同机制）请求 Admin API；**绝不**
       向 iframe 暴露 CSRF token / session 内容 / 通用 fetch 能力。

   本批只实现 admin.auth.get（最小身份 JSON）；其余方法在 METHOD_PERMISSIONS
   表中占位并稳定返回 not_implemented —— PR3b 只需在 METHOD_BACKENDS 加条目。
   ========================================================================= */
(function () {
  "use strict";
  if (window.AdminBridgeHost) return; // 防重复加载

  var BRIDGE = "admin";
  var PROTOCOL_VERSION = "1.0.0";
  var NONCE_BYTES = 32;        // 256-bit
  var REQUEST_TIMEOUT_MS = 20000;

  // §8.4 method → 所需 adminPermission（代码级常量，与
  // plugins/sdk/manifest.py 的 MANIFEST_ADMIN_PERMISSIONS、文档 §8.4 表同源；
  // 未知 method 一律拒绝）。
  var METHOD_PERMISSIONS = {
    "admin.auth.get": "admin:overview:read",
    "admin.overview.get": "admin:overview:read",
    "admin.users.list": "admin:users:read",
    "admin.users.create": "admin:users:write",
    "admin.users.setEnabled": "admin:users:write",
    "admin.users.setAiAccess": "admin:users:write",
    "admin.users.resetPassword": "admin:users:write",
    "admin.invites.list": "admin:invites:read",
    "admin.invites.create": "admin:invites:write",
    "admin.invites.revoke": "admin:invites:write",
    "admin.turnBudgets.get": "admin:turn-budgets:read",
    "admin.turnBudgets.update": "admin:turn-budgets:write",
    "admin.turnBudgets.newPeriod": "admin:turn-budgets:write",
    "admin.billing.account.get": "admin:billing:read",
    "admin.billing.account.updateCaps": "admin:billing:write",
    "admin.billing.adjust": "admin:billing:write",
    "admin.billing.usage.list": "admin:billing:read",
    "admin.billing.ledger.list": "admin:billing:read",
    "admin.billing.providerBalance.get": "admin:billing:read",
    "admin.acquisition.summary": "admin:acquisition:read",
    "admin.acquisition.list": "admin:acquisition:read",
    "admin.audit.list": "admin:audit:read",
  };

  // 参数 schema（最小子集：对象形态 + 未声明属性拒绝；PR3b 按方法细化）。
  // 未列出的方法仅要求 payload 是对象（真正的前端参数校验在接入实现时补齐）。
  var METHOD_PARAM_SCHEMAS = {
    "admin.auth.get": { properties: {}, additionalProperties: false },
  };

  // ---- 工具 ----
  function sameMajor(remote, local) {
    try {
      return String(remote || "").split(".")[0] === String(local).split(".")[0];
    } catch (e) { return false; }
  }

  // 常数时间字符串比较（nonce / requestId 用；高熵值下主要是防侥幸短路）。
  function timingSafeEqual(a, b) {
    if (typeof a !== "string" || typeof b !== "string") return false;
    if (a.length !== b.length) return false;
    var diff = 0;
    for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
    return diff === 0;
  }

  function validateParams(method, payload) {
    if (payload == null) payload = {};
    if (typeof payload !== "object" || Array.isArray(payload)) return false;
    var schema = METHOD_PARAM_SCHEMAS[method];
    if (!schema) return true; // 未声明 schema（PR3b 待实现）：仅要求对象形态
    if (schema.additionalProperties === false) {
      var props = schema.properties || {};
      for (var k in payload) {
        if (Object.prototype.hasOwnProperty.call(payload, k) && !(k in props)) return false;
      }
    }
    return true;
  }

  // login id 掩码（admin.auth.get 返回最小身份，不回传完整账号）。
  function maskLoginId(id) {
    if (typeof id !== "string" || !id) return null;
    var at = id.indexOf("@");
    if (at > 0) {
      var local = id.slice(0, at);
      var head = local.slice(0, 1);
      return head + "***@" + id.slice(at + 1);
    }
    if (id.length <= 2) return id[0] + "*";
    return id.slice(0, 1) + "***" + id.slice(-1);
  }

  // ---- 宿主侧 HTTP（带 CSRF 双提交，与 app.js 同机制；不外传 token）----
  function csrfTokenFromCookie(doc) {
    try {
      var m = doc.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
      return m ? decodeURIComponent(m[1]) : "";
    } catch (e) { return ""; }
  }

  function makeFetchJson(win, doc) {
    return function (url, opts) {
      opts = opts || {};
      var method = (opts.method || "GET").toUpperCase();
      if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
        var tok = csrfTokenFromCookie(doc);
        if (tok) {
          var headers = Object.assign({}, opts.headers || {});
          if (!headers["X-CSRF-Token"]) headers["X-CSRF-Token"] = tok;
          opts.headers = headers;
        }
      }
      return win.fetch(url, opts).then(function (resp) {
        return resp.json().then(
          function (body) { return { status: resp.status, ok: resp.ok, body: body }; },
          function () { return { status: resp.status, ok: resp.ok, body: null }; });
      });
    };
  }

  // 每条消息回查当前 actor 是否仍是 owner（服务端仍会对每个 Admin API 独立
  // 复核 owner + CSRF —— 本检查是纵深防御，不是授权替代）。
  function makeOwnerGuard(fetchJson) {
    return function () {
      return fetchJson("/api/auth/info").then(function (res) {
        if (!res.ok || !res.body) return false;
        var actor = res.body.actor || {};
        return actor.role === "owner";
      }, function () { return false; });
    };
  }

  // ------------------------------------------------------------------
  // method → 后端调用映射表（**唯一一处**；PR3b 只在这里加条目）。
  // 每项 fn(ctx, payload)，ctx = { fetchJson, ensureOwner }；
  // 返回值即 iframe 收到的 result；throw / reject → error 信封。
  // ------------------------------------------------------------------
  var METHOD_BACKENDS = {
    "admin.auth.get": function (ctx) {
      return ctx.fetchJson("/api/auth/info").then(function (res) {
        if (!res.ok) {
          throw { code: res.status === 401 ? "auth_required" : "backend_error",
                  message: "GET /api/auth/info -> " + res.status };
        }
        var info = res.body || {};
        var actor = info.actor || {};
        return {
          role: actor.role || null,
          loginIdMasked: maskLoginId(actor.username || info.username || ""),
          previewActive: !!info.preview,
        };
      });
    },
    // PR3b：admin.overview.get / admin.users.list / ... 在此追加实现。
  };

  // ---- 桥实例 ----
  function create(opts) {
    opts = opts || {};
    var iframe = opts.iframe;
    var win = opts.window || window;
    var doc = opts.document || win.document;
    var cryptoObj = opts.crypto ||
        (win.crypto && typeof win.crypto.getRandomValues === "function" ? win.crypto : null);
    var fetchJson = opts.fetchJson || makeFetchJson(win, doc);
    var ensureOwner = opts.ensureOwner || makeOwnerGuard(fetchJson);
    var grantedPermissions = (opts.permissions || []).filter(function (p) {
      return typeof p === "string";
    });
    var protocolVersion = opts.protocolVersion || PROTOCOL_VERSION;
    var timeoutMs = opts.timeoutMs || REQUEST_TIMEOUT_MS;

    // 登出探测：任一后端调用收到 401 → 会话失效，立即作废（包装在创建处，
    // 使 backend ctx 拿到的 fetchJson 也带观测）
    var onUnauthorized = function () { invalidate("logout"); };
    var observedFetchJson = function (url, opts2) {
      return fetchJson(url, opts2).then(function (res) {
        if (res && res.status === 401) onUnauthorized();
        return res;
      });
    };

    // 当前 load 会话：nonce + 确切 contentWindow + 已见 requestId + 在途请求
    var load = null;
    var stats = { denied: 0, handled: 0 };

    function postToPlugin(targetWindow, env) {
      if (!targetWindow || typeof targetWindow.postMessage !== "function") return;
      try {
        // opaque iframe 的 targetOrigin 只能是 "*"：安全边界见文件头注释。
        targetWindow.postMessage(env, "*");
      } catch (e) { /* 目标 window 已销毁（reload 中途）→ 丢弃 */ }
    }

    function randomNonce() {
      if (!cryptoObj || typeof cryptoObj.getRandomValues !== "function") {
        throw new Error("crypto.getRandomValues unavailable");
      }
      var buf = new Uint8Array(NONCE_BYTES);
      cryptoObj.getRandomValues(buf);
      var hex = "";
      for (var i = 0; i < buf.length; i++) hex += ("0" + buf[i].toString(16)).slice(-2);
      return hex;
    }

    function respond(targetWindow, requestId, ok, data) {
      var env = { kind: "response", bridge: BRIDGE, protocolVersion: protocolVersion,
                  requestId: requestId, ok: ok };
      if (ok) env.result = data == null ? null : data;
      else env.error = data || { code: "bridge_error" };
      postToPlugin(targetWindow, env);
    }

    function failPending(session, err) {
      if (!session || !session.pending) return;
      Object.keys(session.pending).forEach(function (rid) {
        var entry = session.pending[rid];
        if (entry && entry.timer) clearTimeout(entry.timer);
        delete session.pending[rid];
        if (entry && entry.reject) entry.reject(err);
        if (session.contentWindow) respond(session.contentWindow, rid, false, err);
      });
    }

    // 作废当前 load（iframe reload / 登出 / 插件切换 / pagehide）：
    // nonce 失效 + 在途请求全部 reject + 通知 iframe。
    function invalidate(reason) {
      if (load) {
        var old = load;
        load = null;
        old.dead = true;
        failPending(old, { code: "bridge_invalidated",
                           message: "桥接会话已作废（" + reason + "）" });
        postToPlugin(old.contentWindow, {
          kind: "event", bridge: BRIDGE, protocolVersion: protocolVersion,
          type: "bridge_invalidated", reason: reason,
        });
      }
    }

    function handleIframeLoad() {
      if (!iframe) return;
      invalidate("iframe_reload"); // 旧 load 的 nonce / 在途请求立即作废
      var contentWindow = iframe.contentWindow;
      if (!contentWindow) return;
      var nonce;
      try {
        nonce = randomNonce();
      } catch (e) {
        (win.console && win.console.error ? win.console.error : function () {})(e);
        return; // 无高熵熵源 → 不建立桥（fail-closed）
      }
      load = { nonce: nonce, contentWindow: contentWindow,
               seen: {}, pending: {}, dead: false };
      // init 携带一次性 nonce + 协议版本 + 宿主确认的管理能力申请。
      // nonce 不进 URL / DOM dataset / storage，只经此消息一次性下发（§8.3）。
      postToPlugin(contentWindow, {
        kind: "init", bridge: BRIDGE, protocolVersion: protocolVersion,
        nonce: nonce, adminPermissions: grantedPermissions.slice(),
      });
    }

    function dispatch(session, env) {
      var rid = env.requestId;
      var entry = { timer: null, reject: null };
      entry.timer = setTimeout(function () {
        if (load !== session || !session.pending[rid]) return;
        delete session.pending[rid];
        respond(session.contentWindow, rid, false,
                { code: "bridge_timeout", message: "宿主处理 " + env.method + " 超时" });
      }, timeoutMs);
      session.pending[rid] = entry;

      var finish = function (ok, data) {
        if (load !== session || !session.pending[rid]) return; // 已作废/已应答
        clearTimeout(entry.timer);
        delete session.pending[rid];
        respond(session.contentWindow, rid, ok, data);
      };
      entry.reject = function (err) { finish(false, err || { code: "bridge_error" }); };

      // ⑧ 当前 actor 仍是 owner（每条消息回查；fail-closed）
      ensureOwner().then(function (isOwner) {
        if (load !== session) return;
        if (!isOwner) {
          finish(false, { code: "forbidden", message: "当前 actor 不是 owner" });
          return;
        }
        var backend = METHOD_BACKENDS[env.method];
        if (!backend) {
          // 本批未实现：稳定错误码，PR3b 填实现
          finish(false, { code: "not_implemented", message: env.method + " 尚未实现" });
          return;
        }
        Promise.resolve().then(function () {
          return backend({ fetchJson: observedFetchJson, ensureOwner: ensureOwner },
                         env.payload || {});
        }).then(function (result) { finish(true, result == null ? null : result); },
                function (err) {
                  finish(false, (err && err.code) ? err
                          : { code: "bridge_error", message: String((err && err.message) || err) });
                });
      }, function () {
        finish(false, { code: "bridge_error", message: "actor 校验失败" });
      });
    }

    function handleWindowMessage(event) {
      if (!load || load.dead) return;
      // ① 精确 WindowProxy 匹配（origin 恒 "null"，不能用于鉴权）
      if (!event.source || event.source !== load.contentWindow) { stats.denied++; return; }
      var env = event.data;
      if (!env || typeof env !== "object" || env.bridge !== BRIDGE) return;
      if (env.kind !== "request") return;
      // ② nonce 匹配当前 load
      if (!timingSafeEqual(env.nonce, load.nonce)) { stats.denied++; return; }
      // ③ 协议版本同 major
      if (!sameMajor(env.protocolVersion, protocolVersion)) { stats.denied++; return; }
      // ④ requestId 本次 load 内唯一（重放拒绝）
      var rid = env.requestId;
      if (typeof rid !== "string" || !rid) { stats.denied++; return; }
      if (Object.prototype.hasOwnProperty.call(load.seen, rid)) {
        stats.denied++;
        respond(load.contentWindow, rid, false,
                { code: "request_id_replayed", message: "requestId 已在本次会话中使用" });
        return;
      }
      load.seen[rid] = true;
      // ⑤ method 在固定表
      var method = env.method;
      if (typeof method !== "string" ||
          !Object.prototype.hasOwnProperty.call(METHOD_PERMISSIONS, method)) {
        stats.denied++;
        respond(load.contentWindow, rid, false,
                { code: "unknown_method", message: "未知或未登记的桥方法" });
        return;
      }
      // ⑥ 参数 schema
      if (!validateParams(method, env.payload)) {
        stats.denied++;
        respond(load.contentWindow, rid, false,
                { code: "invalid_params", message: "参数校验失败：" + method });
        return;
      }
      // ⑦ method→permission 映射所要求的 adminPermission 已在 manifest 申请
      var required = METHOD_PERMISSIONS[method];
      if (grantedPermissions.indexOf(required) === -1) {
        stats.denied++;
        respond(load.contentWindow, rid, false,
                { code: "permission_denied",
                  message: "manifest 未申请 " + required + "，禁止调用 " + method });
        return;
      }
      stats.handled++;
      dispatch(load, env);
    }

    var handle = {
      _handleIframeLoad: handleIframeLoad,
      _handleWindowMessage: handleWindowMessage,
      invalidate: invalidate,
      isReady: function () { return !!(load && !load.dead); },
      stats: function () { return { denied: stats.denied, handled: stats.handled }; },
      reloadPlugin: function () {
        invalidate("plugin_reload");
        if (!iframe) return;
        var src = iframe.getAttribute && iframe.getAttribute("src");
        if (src && iframe.setAttribute) iframe.setAttribute("src", src); // 触发 load → 新 nonce
      },
    };
    handle._fetchJson = observedFetchJson;
    return handle;
  }

  // ---- 页面装配（/admin 宿主页）----
  function boot(win, doc) {
    var iframe = doc.getElementById && doc.getElementById("admin-plugin-frame");
    if (!iframe) return null;
    var perms = [];
    try {
      perms = JSON.parse(iframe.getAttribute("data-admin-permissions") || "[]");
    } catch (e) { perms = []; }
    var handle = create({
      iframe: iframe,
      permissions: perms,
      protocolVersion: iframe.getAttribute("data-protocol-version") || undefined,
      window: win,
      document: doc,
    });
    iframe.addEventListener("load", handle._handleIframeLoad);
    win.addEventListener("message", handle._handleWindowMessage);
    win.addEventListener("pagehide", function () { handle.invalidate("pagehide"); });
    var reloadBtn = doc.getElementById("admin-reload-btn");
    if (reloadBtn) reloadBtn.addEventListener("click", handle.reloadPlugin);
    return handle;
  }

  window.AdminBridgeHost = {
    PROTOCOL_VERSION: PROTOCOL_VERSION,
    METHOD_PERMISSIONS: METHOD_PERMISSIONS,
    METHOD_PARAM_SCHEMAS: METHOD_PARAM_SCHEMAS,
    maskLoginId: maskLoginId,
    create: create,
    boot: boot,
  };

  // 自动装配（脚本在 body 尾加载，DOM 已就绪；测试注入假 window 时无 frame → no-op）
  if (typeof window !== "undefined" && window.document &&
      typeof window.document.getElementById === "function") {
    if (window.document.readyState === "loading") {
      window.document.addEventListener("DOMContentLoaded", function () {
        boot(window, window.document);
      });
    } else {
      boot(window, window.document);
    }
  }
})();
