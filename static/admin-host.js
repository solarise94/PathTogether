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
     - 对称认证（P2 修订）：宿主发出的全部 result/error 回包都带当前 load 的
       nonce，插件侧以 event.source === window.parent + nonce 校验响应来源，
       其他 frame/窗口无法向插件伪造响应；
     - iframe reload / 登出（401）/ 插件切换 / 页面卸载 → 立即作废 nonce 与
       全部在途请求（reject + 通知 iframe）；
     - 宿主用 fetch + CSRF cookie（与 app.js 同机制）请求 Admin API；**绝不**
       向 iframe 暴露 CSRF token / session 内容 / 通用 fetch 能力。

   PR3a 只实现 admin.auth.get；PR3b 补齐只读方法：overview.get /
   users.list / billing.account.get / billing.usage.list / billing.ledger.list /
   billing.providerBalance.get(+refresh) / audit.list / turnBudgets.get。
   PR5（本版）补齐全部写方法的后端映射与参数 schema：users.create/
   setEnabled/setAiAccess/resetPassword、invites.list/create/revoke、
   turnBudgets.update/newPeriod、billing.account.updateCaps、billing.adjust
   （路径参数拒绝含 "/"；POST/PUT 均带 CSRF 双提交）。PR5 修订再补 UI parity
   四方法：users.startPreview（§10.2 身份预览，admin:users:write）与
   plugins.list/setEnabled/rotateSecret（插件管理页，admin:plugins:read|write；
   运行时 /install 不上桥——§16 发布走版本化 releases）。owner 回查带 5s 短
   TTL 缓存（见 makeOwnerGuard——缓存只是省一次每消息 HTTP，服务端每 API
   独立复核不受影响；401 登出仍立即作废整个桥会话）。
   ========================================================================= */
(function () {
  "use strict";
  if (window.AdminBridgeHost) return; // 防重复加载

  var BRIDGE = "admin";
  var PROTOCOL_VERSION = "1.0.0";
  var NONCE_BYTES = 32;        // 256-bit
  var REQUEST_TIMEOUT_MS = 20000;
  // 一次性修复包 D（§8.1）：握手超时——宿主进入可见 error 态而非永久等待
  var HANDSHAKE_TIMEOUT_MS = 5000;
  // 包 C（§7.1）：bootstrap JSON v1 的 schema 版本（与 app.py
  // ADMIN_BOOTSTRAP_SCHEMA_VERSION 同源；未知版本进可见错误态，不静默降级）
  var BOOTSTRAP_SCHEMA_VERSION = 1;

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
    // refresh 是 PR3b 对 §8.4 表的最小扩展：只抓取供应商自身余额写快照，
    // 不触碰任何用户数据，与 get 同级（admin:billing:read）；服务端有独立
    // 节流 + owner/CSRF 复核。
    "admin.billing.providerBalance.refresh": "admin:billing:read",
    "admin.acquisition.summary": "admin:acquisition:read",
    "admin.acquisition.list": "admin:acquisition:read",
    "admin.audit.list": "admin:audit:read",
    // PR5 修订（UI parity 恢复旧侧栏功能面）：身份预览（§10.2 用户行操作）
    // 走 admin:users:write（服务端 /api/admin/preview/start 是写 session 的
    // owner-only 操作）；插件管理（列表/健康/启停/凭证轮换）用独立的
    // admin:plugins:read/write，不复用 users/billing 权限。
    "admin.users.startPreview": "admin:users:write",
    "admin.plugins.list": "admin:plugins:read",
    "admin.plugins.setEnabled": "admin:plugins:write",
    "admin.plugins.rotateSecret": "admin:plugins:write",
  };

  // 参数 schema（§14.1：每方法白名单 + 类型/长度/枚举/范围；未声明属性
  // 一律拒绝——iframe 不能借桥传任意字段）。PR5 写方法补齐 required（必填
  // 字段缺键即拒）与 nonZero（manual_adjustment 金额非零）两类最小校验；
  // 权威校验仍在服务端，这里只是桥层最小门。
  var _cursorSpec = { type: "string", maxLength: 512, nullable: true };
  var _limitSpec = { type: "integer", min: 1, max: 200 };
  var _userIdSpec = { type: "string", minLength: 1, maxLength: 128 };
  var _slugSpec = { type: "string", maxLength: 64, nullable: true };
  var _budgetIntSpec = function (min) {
    return { type: "integer", min: min, max: 1000000 };
  };
  var METHOD_PARAM_SCHEMAS = {
    "admin.auth.get": { properties: {}, additionalProperties: false },
    "admin.overview.get": { properties: {}, additionalProperties: false },
    "admin.users.list": {
      properties: {
        cursor: _cursorSpec,
        limit: _limitSpec,
        q: { type: "string", maxLength: 128, nullable: true },
        enabled: { type: "boolean" },
        ai_access: { type: "boolean" },
      },
      additionalProperties: false,
    },
    "admin.billing.account.get": {
      properties: { user_id: { type: "string", maxLength: 128, nullable: true } },
      additionalProperties: false,
    },
    "admin.billing.usage.list": {
      properties: {
        cursor: _cursorSpec,
        limit: _limitSpec,
        model: { type: "string", maxLength: 128, nullable: true },
        user_id: { type: "string", maxLength: 128, nullable: true },
        status: { type: "string", enum: ["priced", "unpriced"] },
      },
      additionalProperties: false,
    },
    "admin.billing.ledger.list": {
      properties: { cursor: _cursorSpec, limit: _limitSpec },
      additionalProperties: false,
    },
    "admin.billing.providerBalance.get": {
      properties: {}, additionalProperties: false,
    },
    "admin.billing.providerBalance.refresh": {
      properties: {}, additionalProperties: false,
    },
    "admin.audit.list": {
      properties: {
        cursor: _cursorSpec,
        limit: _limitSpec,
        action: { type: "string", maxLength: 128, nullable: true },
      },
      additionalProperties: false,
    },
    // PR4 来源归因（§9 GET /api/admin/v1/acquisition/*；只读）
    "admin.acquisition.summary": { properties: {}, additionalProperties: false },
    "admin.acquisition.list": {
      properties: { cursor: _cursorSpec, limit: _limitSpec },
      additionalProperties: false,
    },
    "admin.turnBudgets.get": { properties: {}, additionalProperties: false },

    // ---- PR5 写方法（§9 Admin API v1 写端点；服务端 owner/CSRF 复核权威）----
    "admin.users.create": {
      properties: {
        login_id: { type: "string", minLength: 1, maxLength: 120 },
        password: { type: "string", minLength: 1, maxLength: 200 },
        display_name: { type: "string", maxLength: 120, nullable: true },
      },
      required: ["login_id", "password"],
      additionalProperties: false,
    },
    "admin.users.setEnabled": {
      properties: { user_id: _userIdSpec, enabled: { type: "boolean" } },
      required: ["user_id", "enabled"],
      additionalProperties: false,
    },
    "admin.users.setAiAccess": {
      properties: { user_id: _userIdSpec, enabled: { type: "boolean" } },
      required: ["user_id", "enabled"],
      additionalProperties: false,
    },
    "admin.users.resetPassword": {
      properties: {
        user_id: _userIdSpec,
        password: { type: "string", minLength: 1, maxLength: 200 },
      },
      required: ["user_id", "password"],
      additionalProperties: false,
    },
    "admin.invites.list": {
      properties: { cursor: _cursorSpec, limit: _limitSpec },
      additionalProperties: false,
    },
    "admin.invites.create": {
      properties: {
        login_id: { type: "string", maxLength: 120, nullable: true },
        ttl_hours: { type: "integer", min: 1, max: 720 },
        ai_access: { type: "boolean" },
        cohort: { type: "string", maxLength: 64, nullable: true },
        note: { type: "string", maxLength: 200, nullable: true },
        source_code: _slugSpec,
        campaign_id: _slugSpec,
      },
      additionalProperties: false,
    },
    "admin.invites.revoke": {
      properties: { invite_id: { type: "string", minLength: 1, maxLength: 128 } },
      required: ["invite_id"],
      additionalProperties: false,
    },
    // turn 预算限制字段（与服务端 _BUDGET_SETTINGS_FIELDS 同源；0 仅对
    // demo_turn_limit/owner_reserved_turn_limit/user_pool_turn_limit 合法=关闭子池）
    "admin.turnBudgets.update": {
      properties: {
        platform_turn_limit: _budgetIntSpec(1),
        demo_turn_limit: _budgetIntSpec(0),
        user_turn_limit: _budgetIntSpec(1),
        owner_reserved_turn_limit: _budgetIntSpec(0),
        user_pool_turn_limit: _budgetIntSpec(0),
        platform_task_max_steps: _budgetIntSpec(1),
        own_task_max_steps_limit: _budgetIntSpec(1),
        demo_task_max_steps: _budgetIntSpec(1),
        demo_enabled: { type: "boolean" },
        demo_per_browser_limit: _budgetIntSpec(1),
        demo_max_concurrency: _budgetIntSpec(1),
      },
      additionalProperties: false,
    },
    // confirm 由桥层固定置 true（插件的危险操作二次确认在 UI 层做，§3.3）
    "admin.turnBudgets.newPeriod": {
      properties: { limits: { type: "object", nullable: true } },
      additionalProperties: false,
    },
    "admin.billing.account.updateCaps": {
      properties: {
        user_id: _userIdSpec,
        // §5 v0.3（P2）：金额 wire 为十进制字符串（null = 清除该上限，
        // §9）；JSON number 在桥层即拒（JS Number 超 2^53 静默失真）
        soft_cap_nano_cny: {
          type: "string", pattern: "^[0-9]{1,19}$", nullable: true,
        },
        hard_cap_nano_cny: {
          type: "string", pattern: "^[0-9]{1,19}$", nullable: true,
        },
        version: { type: "integer", min: 1 },
      },
      required: ["user_id", "soft_cap_nano_cny", "hard_cap_nano_cny", "version"],
      additionalProperties: false,
    },
    "admin.billing.adjust": {
      properties: {
        user_id: _userIdSpec,
        kind: {
          type: "string",
          enum: ["grant", "topup", "refund", "manual_adjustment"],
        },
        // §5 v0.3（P2）：十进制字符串（manual_adjustment 可为负）；桥层
        // nonZero 拒 "0"/"-0"/"00" 等零值形态，kind 符号语义由服务端复核
        amount_nano_cny: {
          type: "string", pattern: "^-?[0-9]{1,19}$", nonZero: true,
        },
        reason: { type: "string", minLength: 1, maxLength: 500 },
        // §6.5 PR5 修订：幂等键必须由调用方（插件 UI）生成，桥层与服务端
        // 双重 required——服务端已入账 + 浏览器超时的重试必须带同一 key 命中
        // duplicate，缺失/空串在桥层即拒绝（不再 nullable）。
        idempotency_key: {
          type: "string", minLength: 1, maxLength: 128,
        },
      },
      required: ["user_id", "kind", "amount_nano_cny", "reason",
                 "idempotency_key"],
      additionalProperties: false,
    },

    // ---- PR5 修订（UI parity）：身份预览 + 插件管理 ----
    // 预览进入（旧侧栏「身份预览」入口恢复）：写 owner session，归
    // admin:users:write；停止预览不做桥方法——宿主页 Viewer 右上角自带
    // stop 按钮（iframe 无 allow-top-navigation，插件不能也不应跳转宿主）。
    "admin.users.startPreview": {
      properties: { user_id: _userIdSpec },
      required: ["user_id"],
      additionalProperties: false,
    },
    // 插件安装列表（含 sidecar 健康快照）；无分页（服务端全量返回）
    "admin.plugins.list": { properties: {}, additionalProperties: false },
    // 启停安装行（enable/disable 映射到两个后端路径）；installation_id
    // 走 pathId 防护（拒绝空值/含 "/"，见 METHOD_BACKENDS）
    "admin.plugins.setEnabled": {
      properties: {
        installation_id: { type: "string", minLength: 1, maxLength: 128 },
        enabled: { type: "boolean" },
      },
      required: ["installation_id", "enabled"],
      additionalProperties: false,
    },
    // 轮换安装凭证：响应原样透传——新明文 secret 仅此一次展示（插件 UI
    // 一次性显示 + 复制按钮，绝不落 localStorage）
    "admin.plugins.rotateSecret": {
      properties: {
        installation_id: { type: "string", minLength: 1, maxLength: 128 },
      },
      required: ["installation_id"],
      additionalProperties: false,
    },
  };

  // ---- 工具 ----
  function sameMajor(remote, local) {
    try {
      return String(remote || "").split(".")[0] === String(local).split(".")[0];
    } catch (e) { return false; }
  }

  // ------------------------------------------------------------------
  // bootstrap JSON v1 严格解析（包 C §7.1）：
  //   - 顶层对象 + schemaVersion 必须精确匹配（未知版本 → 可见错误，不静默降级）；
  //   - permissions 必须是已知字符串（METHOD_PERMISSIONS 值域）的去重数组；
  //   - assetUrl 必须是站内 admin 插件资源路径（拒绝外域/协议相对/绝对 URL）；
  //   - protocolVersion 必须是非空字符串；
  //   - 任何失败抛 {code, message}（宿主进入 bootstrap_invalid 错误态，
  //     桥接请求数为 0——绝不静默回退空权限数组）。
  // ------------------------------------------------------------------
  var KNOWN_PERMISSIONS = (function () {
    var set = {};
    Object.keys(METHOD_PERMISSIONS).forEach(function (m) {
      set[METHOD_PERMISSIONS[m]] = true;
    });
    return set;
  })();
  var ASSET_URL_RE =
      /^\/admin\/plugin-assets\/[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._\/-]*$/;

  function parseBootstrap(text) {
    var data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      throw { code: "bootstrap_invalid", message: "bootstrap 节点不是合法 JSON" };
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw { code: "bootstrap_invalid", message: "bootstrap 顶层必须是对象" };
    }
    if (data.schemaVersion !== BOOTSTRAP_SCHEMA_VERSION) {
      throw { code: "bootstrap_unsupported_version",
              message: "bootstrap schemaVersion=" + data.schemaVersion +
                       " 不受支持（宿主要求 " + BOOTSTRAP_SCHEMA_VERSION + "）" };
    }
    if (typeof data.protocolVersion !== "string" || !data.protocolVersion) {
      throw { code: "bootstrap_invalid", message: "protocolVersion 缺失" };
    }
    var perms = data.permissions;
    if (!Array.isArray(perms)) {
      throw { code: "bootstrap_invalid", message: "permissions 必须是数组" };
    }
    var seen = {};
    for (var i = 0; i < perms.length; i++) {
      var p = perms[i];
      if (typeof p !== "string" || !KNOWN_PERMISSIONS[p]) {
        throw { code: "bootstrap_invalid",
                message: "permissions 含未知值（第 " + (i + 1) + " 项）" };
      }
      if (seen[p]) {
        throw { code: "bootstrap_invalid", message: "permissions 含重复项 " + p };
      }
      seen[p] = true;
    }
    if (typeof data.assetUrl !== "string" || !ASSET_URL_RE.test(data.assetUrl)) {
      throw { code: "bootstrap_invalid", message: "assetUrl 非法（须为站内插件资源路径）" };
    }
    return {
      protocolVersion: data.protocolVersion,
      permissions: perms.slice(),
      assetUrl: data.assetUrl,
    };
  }

  // 常数时间字符串比较（nonce / requestId 用；高熵值下主要是防侥幸短路）。
  function timingSafeEqual(a, b) {
    if (typeof a !== "string" || typeof b !== "string") return false;
    if (a.length !== b.length) return false;
    var diff = 0;
    for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
    return diff === 0;
  }

  // 单值校验：类型（string/integer/boolean/object）+ minLength/maxLength +
  // pattern + enum + min/max + nonZero。null 只在显式 nullable 时接受（caps
  // 的 null=清除是唯一合法 null 语义；游标翻页用「缺键」表达，不用 null）。
  // 金额字段是十进制字符串（§5 v0.3：wire 禁 JSON number，防 >2^53 失真），
  // pattern 限定 ^-?[0-9]{1,19}$；字符串形态的 nonZero 用去前导零判零，
  // 全程不经 Number（大值中转会丢精度）。
  function validateValue(value, spec) {
    if (value === undefined) return false;
    if (value === null) return spec.nullable === true;
    switch (spec.type) {
      case "string":
        if (typeof value !== "string") return false;
        if (spec.minLength !== undefined && value.length < spec.minLength) return false;
        if (spec.maxLength && value.length > spec.maxLength) return false;
        if (spec.pattern && !(new RegExp(spec.pattern)).test(value)) return false;
        if (spec.enum && spec.enum.indexOf(value) === -1) return false;
        if (spec.nonZero && value.replace(/^-/, "").replace(/^0+/, "") === "") {
          return false;
        }
        return true;
      case "integer":
        if (typeof value !== "number" || !isFinite(value) ||
            Math.floor(value) !== value) return false;
        if (spec.nonZero && value === 0) return false;
        if (spec.min !== undefined && value < spec.min) return false;
        if (spec.max !== undefined && value > spec.max) return false;
        return true;
      case "boolean":
        return typeof value === "boolean";
      case "object":
        return typeof value === "object" && !Array.isArray(value);
      default:
        return true;
    }
  }

  function validateParams(method, payload) {
    if (payload == null) payload = {};
    if (typeof payload !== "object" || Array.isArray(payload)) return false;
    var schema = METHOD_PARAM_SCHEMAS[method];
    if (!schema) return true; // 未声明 schema：仅要求对象形态
    var props = schema.properties || {};
    var required = schema.required || [];
    for (var r = 0; r < required.length; r++) {
      if (!(required[r] in payload)) return false;
    }
    for (var k in payload) {
      if (!Object.prototype.hasOwnProperty.call(payload, k)) continue;
      if (!(k in props)) {
        if (schema.additionalProperties === false) return false;
        continue;
      }
      if (!validateValue(payload[k], props[k])) return false;
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
  // PR3b：加 5s 短 TTL 缓存——概览/用户/账单一次渲染会连发多条桥消息，
  // 每条都打一次 /api/auth/info 既无谓又放大时序抖动。缓存只省这次 HTTP：
  //  - 缓存过期即回查（最长 5s 的陈旧放行窗口）；
  //  - 任何后端调用收到 401 → observedFetchJson 立即 invalidate("logout")，
  //    nonce/在途请求全部作废（不依赖本缓存收敛）；
  //  - 服务端 owner 复核永远独立生效（本缓存不是授权）。
  var OWNER_GUARD_TTL_MS = 5000;
  function makeOwnerGuard(fetchJson) {
    var cache = { at: 0, value: false, inflight: null };
    return function () {
      var now = Date.now();
      if (now - cache.at < OWNER_GUARD_TTL_MS) {
        return Promise.resolve(cache.value);
      }
      if (cache.inflight) {
        return cache.inflight; // 同一批并发消息合并为一次回查
      }
      cache.inflight = fetchJson("/api/auth/info").then(function (res) {
        cache.at = Date.now();
        cache.value = !!(res && res.ok && res.body &&
                         (res.body.actor || {}).role === "owner");
        cache.inflight = null;
        return cache.value;
      }, function () {
        cache.at = 0; // 网络失败不缓存，下条消息重查
        cache.inflight = null;
        return false;
      });
      return cache.inflight;
    };
  }

  // ------------------------------------------------------------------
  // method → 后端调用映射表（**唯一一处**）。每项 fn(ctx, payload)，
  // ctx = { fetchJson, ensureOwner }；返回值即 iframe 收到的 result；
  // throw / reject → error 信封。PR3b：只读方法全部指向 Admin API v1。
  // ------------------------------------------------------------------
  // 服务端错误信封 {error:{code,message}} → 桥错误 {code,message}。
  function backendError(url, res) {
    var body = (res && res.body) || {};
    var err = body.error;
    if (err && typeof err === "object" && err.code) {
      return { code: err.code, message: err.message || "" };
    }
    var code = "backend_error";
    if (res && res.status === 401) code = "auth_required";
    else if (res && res.status === 403) code = "forbidden";
    else if (res && res.status === 503) code = "pg_backend_required";
    return { code: code, message: (res && res.status) + " " + url };
  }

  // 只保留有值的键（null/undefined/空串不进 query）。
  function buildQuery(params) {
    var parts = [];
    Object.keys(params || {}).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null || v === "") return;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(v)));
    });
    return parts.length ? "?" + parts.join("&") : "";
  }

  function jsonGet(url) {
    return function (ctx) {
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    };
  }

  // PR5 写端点路径参数：encodeURIComponent + 拒绝空值/含 "/" 或 "?" 的值
  // （防 iframe 借 user_id/invite_id 拼出任意后端路径）。
  function pathId(value, field) {
    var s = String(value == null ? "" : value);
    if (!s || s.indexOf("/") !== -1 || s.indexOf("?") !== -1) {
      throw { code: "invalid_params",
              message: field + " 非法（不能为空或含路径分隔符）" };
    }
    return encodeURIComponent(s);
  }

  // PR5 写端点通用 POST/PUT JSON 调用（makeFetchJson 对非安全方法自动附
  // CSRF 双提交 header；错误信封映射同只读方法）。
  function jsonWrite(url, method, body) {
    return function (ctx) {
      return ctx.fetchJson(url, {
        method: method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body === undefined ? {} : body),
      }).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    };
  }

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

    "admin.overview.get": jsonGet("/api/admin/v1/overview"),

    "admin.users.list": function (ctx, payload) {
      var url = "/api/admin/v1/users" + buildQuery({
        cursor: payload.cursor, limit: payload.limit, q: payload.q,
        enabled: payload.enabled, ai_access: payload.ai_access,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.billing.account.get": function (ctx, payload) {
      var uid = String(payload.user_id || "");
      var url = "/api/admin/v1/billing/accounts/" + encodeURIComponent(uid);
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.billing.usage.list": function (ctx, payload) {
      var url = "/api/admin/v1/billing/usage-events" + buildQuery({
        cursor: payload.cursor, limit: payload.limit, model: payload.model,
        user_id: payload.user_id, status: payload.status,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.billing.ledger.list": function (ctx, payload) {
      var url = "/api/admin/v1/billing/ledger" + buildQuery({
        cursor: payload.cursor, limit: payload.limit,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.billing.providerBalance.get":
      jsonGet("/api/admin/v1/billing/provider-balance"),

    // 手动刷新（§10.4 余额卡按钮）：POST 走 makeFetchJson 的 CSRF 双提交；
    // 429 refresh_throttled / 502 provider_* 由 backendError 透传类别。
    "admin.billing.providerBalance.refresh": function (ctx) {
      var url = "/api/admin/v1/billing/provider-balance/refresh";
      return ctx.fetchJson(url, { method: "POST" }).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.audit.list": function (ctx, payload) {
      var url = "/api/admin/v1/audit" + buildQuery({
        cursor: payload.cursor, limit: payload.limit, action: payload.action,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.turnBudgets.get": jsonGet("/api/admin/v1/turn-budgets"),

    // PR4 来源归因（§9/§10.3 只读）：漏斗汇总 + 用户来源明细（first/last
    // touch 分开）；json/dual 后端 503 pg_backend_required 由 backendError
    // 透传（插件页显示「不可用」，不降级伪造数据）。
    "admin.acquisition.summary":
      jsonGet("/api/admin/v1/acquisition/summary"),

    "admin.acquisition.list": function (ctx, payload) {
      var url = "/api/admin/v1/acquisition/users" + buildQuery({
        cursor: payload.cursor, limit: payload.limit,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    // ---- PR5 写方法 → Admin API v1 写端点（POST/PUT 走 makeFetchJson 的
    // CSRF 双提交；路径参数必须 encodeURIComponent 且拒绝含 "/" 的值，防止
    // iframe 借 user_id/invite_id 拼出任意路径）----
    "admin.users.create": function (ctx, payload) {
      return jsonWrite("/api/admin/v1/users", "POST", {
        login_id: payload.login_id,
        password: payload.password,
        display_name: payload.display_name,
      })(ctx);
    },

    "admin.users.setEnabled": function (ctx, payload) {
      var url = "/api/admin/v1/users/" + pathId(payload.user_id, "user_id") +
          (payload.enabled ? "/enable" : "/disable");
      return jsonWrite(url, "POST", {})(ctx);
    },

    "admin.users.setAiAccess": function (ctx, payload) {
      var url = "/api/admin/v1/users/" + pathId(payload.user_id, "user_id") +
          "/ai-access";
      return jsonWrite(url, "POST", { enabled: !!payload.enabled })(ctx);
    },

    "admin.users.resetPassword": function (ctx, payload) {
      var url = "/api/admin/v1/users/" + pathId(payload.user_id, "user_id") +
          "/password-reset";
      return jsonWrite(url, "POST", { password: payload.password })(ctx);
    },

    "admin.invites.list": function (ctx, payload) {
      var url = "/api/admin/v1/invites" + buildQuery({
        cursor: payload.cursor, limit: payload.limit,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.invites.create": function (ctx, payload) {
      return jsonWrite("/api/admin/v1/invites", "POST", {
        login_id: payload.login_id,
        ttl_hours: payload.ttl_hours,
        ai_access: payload.ai_access,
        cohort: payload.cohort,
        note: payload.note,
        source_code: payload.source_code,
        campaign_id: payload.campaign_id,
      })(ctx);
    },

    "admin.invites.revoke": function (ctx, payload) {
      var url = "/api/admin/v1/invites/" + pathId(payload.invite_id, "invite_id") +
          "/revoke";
      return jsonWrite(url, "POST", {})(ctx);
    },

    "admin.turnBudgets.update": function (ctx, payload) {
      // schema 已白名单限制为 _BUDGET_SETTINGS_FIELDS 子集，原样透传
      return jsonWrite("/api/admin/v1/turn-budgets", "PUT", payload)(ctx);
    },

    "admin.turnBudgets.newPeriod": function (ctx, payload) {
      return jsonWrite("/api/admin/v1/turn-budgets/new-period", "POST", {
        confirm: true, limits: payload.limits,
      })(ctx);
    },

    "admin.billing.account.updateCaps": function (ctx, payload) {
      var url = "/api/admin/v1/billing/accounts/" +
          pathId(payload.user_id, "user_id") + "/caps";
      return jsonWrite(url, "PUT", {
        soft_cap_nano_cny: payload.soft_cap_nano_cny,
        hard_cap_nano_cny: payload.hard_cap_nano_cny,
        version: payload.version,
      })(ctx);
    },

    "admin.billing.adjust": function (ctx, payload) {
      return jsonWrite("/api/admin/v1/billing/adjustments", "POST", {
        user_id: payload.user_id,
        kind: payload.kind,
        amount_nano_cny: payload.amount_nano_cny,
        reason: payload.reason,
        idempotency_key: payload.idempotency_key,
      })(ctx);
    },

    // ---- PR5 修订（UI parity）：身份预览 + 插件管理（旧 /api/admin/* 端点，
    // 非 v1——预览与插件管理 API 先于 Admin API v1 存在，沿用原路径）----
    "admin.users.startPreview": function (ctx, payload) {
      return jsonWrite("/api/admin/preview/start", "POST", {
        user_id: payload.user_id,
      })(ctx);
    },

    "admin.plugins.list": jsonGet("/api/admin/plugins"),

    "admin.plugins.setEnabled": function (ctx, payload) {
      var url = "/api/admin/plugins/" +
          pathId(payload.installation_id, "installation_id") +
          (payload.enabled ? "/enable" : "/disable");
      return jsonWrite(url, "POST", {})(ctx);
    },

    "admin.plugins.rotateSecret": function (ctx, payload) {
      var url = "/api/admin/plugins/" +
          pathId(payload.installation_id, "installation_id") + "/rotate-secret";
      return jsonWrite(url, "POST", {})(ctx);
    },
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
    // 包 D（§8.1）：invalidate（reload/登出/pagehide）时通知宿主状态机
    var onSessionEnd = typeof opts.onSessionEnd === "function" ? opts.onSessionEnd : null;

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

    // §8.3 P2 修订（对称认证）：宿主的全部 result/error 回包都带当前 load
    // 的 nonce（与请求侧同一值）——插件侧据此 + event.source 拒绝其他
    // frame/窗口伪造的响应。
    function respond(session, requestId, ok, data) {
      var env = { kind: "response", bridge: BRIDGE, protocolVersion: protocolVersion,
                  nonce: session.nonce, requestId: requestId, ok: ok };
      if (ok) env.result = data == null ? null : data;
      else env.error = data || { code: "bridge_error" };
      postToPlugin(session.contentWindow, env);
    }

    function failPending(session, err) {
      if (!session || !session.pending) return;
      Object.keys(session.pending).forEach(function (rid) {
        var entry = session.pending[rid];
        if (entry && entry.timer) clearTimeout(entry.timer);
        delete session.pending[rid];
        if (entry && entry.reject) entry.reject(err);
        if (session.contentWindow) respond(session, rid, false, err);
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
      if (onSessionEnd) {
        try { onSessionEnd(reason); } catch (e) { /* 状态机异常不波及桥 */ }
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
        respond(session, rid, false,
                { code: "bridge_timeout", message: "宿主处理 " + env.method + " 超时" });
      }, timeoutMs);
      session.pending[rid] = entry;

      var finish = function (ok, data) {
        if (load !== session || !session.pending[rid]) return; // 已作废/已应答
        clearTimeout(entry.timer);
        delete session.pending[rid];
        respond(session, rid, ok, data);
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
          // 防御性兜底：METHOD_PERMISSIONS 有映射但缺 backend 实现时稳定报错
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
        respond(load, rid, false,
                { code: "request_id_replayed", message: "requestId 已在本次会话中使用" });
        return;
      }
      load.seen[rid] = true;
      // ⑤ method 在固定表
      var method = env.method;
      if (typeof method !== "string" ||
          !Object.prototype.hasOwnProperty.call(METHOD_PERMISSIONS, method)) {
        stats.denied++;
        respond(load, rid, false,
                { code: "unknown_method", message: "未知或未登记的桥方法" });
        return;
      }
      // ⑥ 参数 schema
      if (!validateParams(method, env.payload)) {
        stats.denied++;
        respond(load, rid, false,
                { code: "invalid_params", message: "参数校验失败：" + method });
        return;
      }
      // ⑦ method→permission 映射所要求的 adminPermission 已在 manifest 申请
      var required = METHOD_PERMISSIONS[method];
      if (grantedPermissions.indexOf(required) === -1) {
        stats.denied++;
        respond(load, rid, false,
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

  // ---- 页面装配（/admin 宿主页；包 C/D：bootstrap v1 + 生命周期状态机）----
  // 状态机：idle → loading → waiting_handshake → ready / error
  //         ready/error → reload → loading（新 nonce，旧请求全部作废）
  //         pagehide / 登出 → disposed
  var STATE_TEXT = {
    idle: "正在初始化…",
    loading: "正在加载管理插件…",
    waiting_handshake: "等待插件握手…",
    ready: "桥接已建立",
    error: "出错",
    disposed: "已断开",
  };

  function boot(win, doc, bootOpts) {
    bootOpts = bootOpts || {};
    var iframe = doc.getElementById && doc.getElementById("admin-plugin-frame");
    if (!iframe) return null;
    var statusEl = doc.getElementById && doc.getElementById("admin-host-status");

    // 状态只经 DOM 属性 + 可测试事件表达（§8.1-5）；消息不含敏感内容
    function setState(state, code, message) {
      if (statusEl && statusEl.setAttribute) {
        statusEl.setAttribute("data-admin-host-state", state);
        statusEl.textContent = message ||
            (STATE_TEXT[state] + (code ? "（" + code + "）" : ""));
      }
      try {
        var ev = new (win.CustomEvent)("adminhoststatechange",
                                       { detail: { state: state, code: code || null } });
        win.dispatchEvent(ev);
      } catch (e) { /* 事件是可测试性通道，失败不阻断 */ }
    }

    // bootstrap 严格解析：失败进入可见 error 态，桥接请求数为 0（§7.1）
    var bootstrapEl = doc.getElementById && doc.getElementById("admin-bootstrap");
    var bootstrap;
    try {
      bootstrap = parseBootstrap(
        bootstrapEl && bootstrapEl.textContent ? String(bootstrapEl.textContent) : "");
    } catch (err) {
      setState("error", err && err.code,
               "管理工作台配置无效：" + ((err && err.message) || "bootstrap 缺失") +
               "（" + ((err && err.code) || "bootstrap_invalid") + "）");
      return null;
    }

    var handshakeTimeoutMs = bootOpts.handshakeTimeoutMs || HANDSHAKE_TIMEOUT_MS;
    var handshakeTimer = null;
    var readyAnnounced = false;

    function clearHandshakeTimer() {
      if (handshakeTimer) {
        clearTimeout(handshakeTimer);
        handshakeTimer = null;
      }
    }

    var handle = create({
      iframe: iframe,
      permissions: bootstrap.permissions,
      protocolVersion: bootstrap.protocolVersion,
      window: win,
      document: doc,
      onSessionEnd: function (reason) {
        clearHandshakeTimer();
        if (reason === "logout" || reason === "pagehide") {
          setState("disposed", reason === "logout" ? "logout" : null,
                   reason === "logout" ? "登录已失效，请重新登录（logout）" : "已断开");
        }
        // iframe_reload / plugin_reload：load 监听器会把状态带回 waiting_handshake
      },
    });

    // ① 先安装全部监听器（§8.1-1：消除初次 load race）
    iframe.addEventListener("load", function () {
      handle._handleIframeLoad(); // 生成新 nonce、作废旧会话
      readyAnnounced = false;
      setState("waiting_handshake");
      clearHandshakeTimer();
      handshakeTimer = setTimeout(function () {
        handshakeTimer = null;
        handle.invalidate("handshake_timeout");
        setState("error", "handshake_timeout",
                 "插件握手超时（handshake_timeout，" +
                 Math.round(handshakeTimeoutMs / 1000) + " 秒无响应）——" +
                 "可点「重新加载插件」重试");
      }, handshakeTimeoutMs);
    });
    win.addEventListener("message", function (event) {
      var handledBefore = handle.stats().handled;
      handle._handleWindowMessage(event);
      // 首条通过全部校验（WindowProxy/nonce/requestId/method/schema/权限）的
      // 请求 = 握手完成 → ready（§8.1 状态机）
      if (!readyAnnounced && handle.stats().handled > handledBefore) {
        readyAnnounced = true;
        clearHandshakeTimer();
        setState("ready", null,
                 "桥接已建立（已授权管理能力 " + bootstrap.permissions.length + " 项）");
      }
    });
    win.addEventListener("pagehide", function () { handle.invalidate("pagehide"); });
    var reloadBtn = doc.getElementById && doc.getElementById("admin-reload-btn");
    if (reloadBtn) {
      reloadBtn.addEventListener("click", function () {
        clearHandshakeTimer();
        setState("loading");
        handle.reloadPlugin();
      });
    }

    // ② 监听器就位后才赋业务 src（深链 hash 透传严格白名单，不回显其他内容）
    setState("loading");
    var src = bootstrap.assetUrl;
    try {
      var deepLink = (win.location && win.location.hash) || "";
      if (/^#[a-z][a-z0-9_-]{0,31}$/.test(deepLink)) {
        src = src + deepLink;
      }
    } catch (e) { /* 读不到 location/hash：保持原 src */ }
    iframe.setAttribute("src", src);
    return handle;
  }

  window.AdminBridgeHost = {
    PROTOCOL_VERSION: PROTOCOL_VERSION,
    BOOTSTRAP_SCHEMA_VERSION: BOOTSTRAP_SCHEMA_VERSION,
    METHOD_PERMISSIONS: METHOD_PERMISSIONS,
    METHOD_PARAM_SCHEMAS: METHOD_PARAM_SCHEMAS,
    parseBootstrap: parseBootstrap,
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
