/* =========================================================================
   pathtogether-admin 桥接客户端 + 业务页面（PR3b 只读 + PR5 写操作）

   docs/admin-billing-plugin-implementation-plan.md §8.3/§8.4/§10：
     - 本页运行在 /admin 宿主页的 opaque iframe（sandbox="allow-scripts"）内；
     - 宿主在每次 iframe load 后 postMessage 一条 {kind:"init"} 携带一次性
       256-bit nonce 与协议版本；本端保存 nonce（仅内存），此后所有请求回带
       nonce + 本次会话内递增的 requestId；
     - 响应来源认证（§8.3 P2 修订，与宿主对称）：onMessage 一律先验
       event.source === window.parent；init 之后的响应必须携带与 init 收到的
       session nonce 相等的 nonce 且命中在途 requestId，否则静默丢弃——其他
       frame/窗口无法向本页伪造响应；
     - 响应按 requestId 配对；超时（15s）与拒绝都有稳定错误码；
     - opaque origin：不能读写 localStorage/cookie，全部页面状态（当前页/
       筛选/分页游标/账户卡缓存）只在内存；
     - sandbox 无 allow-modals：window.confirm/prompt 被静默吞掉，危险操作
       二次确认一律用页内确认条（§3.3）；
     - PR5 写操作：创建用户、启停/AI access/重置密码、邀请创建/撤销、turn
       预算编辑与开新周期、caps 编辑（CAS）、人工调账（CNY→nano 全字符串
       换算，禁 float；幂等键由本端生成并在同一逻辑提交的重试间复用，§6.5）。
       表单校验只是体验层，权威校验在服务端。
   渲染只用 textContent / createElement（不拼 HTML，插件数据永不进标记）。
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
    page: "overview",       // 当前页（内存态导航）
    // 列表代际（§8.2 包 D）：showPage/筛选重置时 ++；在途响应回来时代际不符
    // 即丢弃——快速连续点击、reload 与晚到响应不把旧数据写回新页面
    listSeq: 0,
    // 分页游标（每列表独立；仅内存）
    cursors: { users: null, usage: null, unpriced: null, ledger: null,
               audit: null, acqUsers: null, invites: null },
    filters: { users: {}, usage: {}, audit: {} },
    // 金额账户卡当前查询结果（caps 编辑器提交 CAS version 用；仅内存）
    account: null,
    // 人工调账幂等键（§6.5 PR5 修订：必须由调用方生成，服务端缺失即 400）。
    // 一次逻辑提交（用户点确认执行那一刻）生成一个 key，失败重试复用同 key
    // （服务端已入账 + 浏览器超时的场景，重试必须命中 duplicate 而不是二次
    // 入账）；成功或表单任一字段（user/kind/金额/reason）被修改后才换新 key。
    adjustIdem: null,
    // adjustIdem 生成时的表单指纹（user/kind/金额/reason 四元组）：指纹变化
    // = 用户改了载荷 = 新的逻辑提交 → 下次提交前换新 key。
    adjustFingerprint: null,
    // 设置页快照（批次 D §6.1）：admin.settings.get 的响应 + 「调整当前窗口」
    // 的主体列表（demo/owner/各用户当前窗口，含 CAS version）——仅内存。
    settingsSnapshot: null,
    windowSubjects: [],
  };

  // 深链起始页（PR5 /admin#invites 兼容）：宿主把父页 hash 透传到本 iframe
  // 自身 URL；只接受已知页面 slug，其余回概览。
  function initialPageFromHash() {
    var pages = ["overview", "users", "invites", "settings", "billing",
                 "plugins", "audit"];
    var hash = "";
    try { hash = window.location.hash || ""; } catch (e) { hash = ""; }
    var name = hash.replace(/^#/, "");
    return pages.indexOf(name) !== -1 ? name : "overview";
  }
  var initialPage = initialPageFromHash();

  function $(id) { return document.getElementById(id); }

  var PAGE_TITLES = {
    overview: "概览", users: "用户", invites: "邀请与来源",
    settings: "设置", billing: "额度与账单", plugins: "插件", audit: "审计",
  };

  var els = {
    handshake: $("adm-handshake-status"),
    pageTitle: $("adm-page-title"),
    nav: $("adm-nav"),
    navToggle: $("adm-nav-toggle"),
    drawer: $("adm-user-drawer"),
    drawerMask: $("adm-drawer-mask"),
    drawerBody: $("adm-drawer-body"),
    drawerClose: $("adm-drawer-close"),
    pages: {
      overview: $("adm-page-overview"),
      users: $("adm-page-users"),
      invites: $("adm-page-invites"),
      settings: $("adm-page-settings"),
      billing: $("adm-page-billing"),
      plugins: $("adm-page-plugins"),
      audit: $("adm-page-audit"),
    },
    errorCard: $("adm-error-card"),
    errorText: $("adm-error-text"),
  };

  // ------------------------------------------------------------------
  // 页级状态组件（§9.2 包 E）：loading / empty / error / ready 四态。
  // error 态显示错误类别 + 本地请求序号（非敏感）+ 重试；空态解释为什么为
  // 空；ready 态显示更新时间。permission_denied 等错误绝不渲染成 empty。
  // ------------------------------------------------------------------
  function setPageState(page, stateName, opts) {
    var el = $("adm-state-" + page);
    if (!el || !el.setAttribute) return;
    el.setAttribute("data-page-state", stateName);
    el.textContent = "";
    if (stateName === "loading") {
      el.textContent = "加载中…";
    } else if (stateName === "empty") {
      el.textContent = (opts && opts.message) || "暂无数据";
    } else if (stateName === "error") {
      var code = (opts && opts.code) || "error";
      var rid = opts && opts.requestId ? "，请求 " + opts.requestId : "";
      el.textContent = "加载失败：" + code + rid +
        ((opts && opts.message) ? "（" + opts.message + "）" : "");
      if (opts && typeof opts.retry === "function") {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = "重试";
        btn.addEventListener("click", opts.retry);
        el.appendChild(btn);
      }
    } else if (stateName === "ready") {
      el.textContent = (opts && opts.message) || "";
    }
  }

  function setHandshake(text) {
    if (els.handshake) els.handshake.textContent = text;
  }

  // §8.2（包 D）：桥接可用 = 已收到 init 且未被作废。未 ready 时导航只切换
  // 本地骨架，不得发管理 API 请求，也不把 not_ready 渲染成全局错误。
  function bridgeReady() {
    return !!state.nonce && !state.dead;
  }

  function showError(code, message) {
    if (!els.errorCard || !els.errorText) return;
    els.errorCard.hidden = false;
    els.errorText.textContent = (code || "error") + (message ? (": " + message) : "");
  }

  function hideError() {
    if (els.errorCard) els.errorCard.hidden = true;
  }

  // ---- 格式化 ----
  function fmtTs(epoch) {
    if (epoch === null || epoch === undefined) return "—";
    try {
      var d = new Date(Number(epoch) * 1000);
      return d.toISOString().replace("T", " ").replace(".000Z", "Z");
    } catch (e) { return String(epoch); }
  }

  function fmtNum(v) {
    if (v === null || v === undefined) return "—";
    if (typeof v !== "number") return String(v);
    return v.toLocaleString("en-US");
  }

  // nano-CNY（1e9 = 1 CNY）：精确换算给 CNY 字符串 + 原始 nano。BigInt 运算
  //（语言特性，opaque sandbox 可用），全程不经 Number——wire 金额是十进制
  // 字符串（§5 v0.3），>2^53 的值经 Number 会静默失真。
  function fmtNano(v) {
    if (v === null || v === undefined) return "—";
    try {
      var b = BigInt(v);
      var neg = b < 0n;
      if (neg) b = -b;
      var whole = (b / 1000000000n).toString();
      var frac = (b % 1000000000n).toString()
        .padStart(9, "0").replace(/0+$/, "");
      return (neg ? "-" : "") + whole + (frac ? "." + frac : "") +
        " CNY（" + String(v) + " nano）";
    } catch (e) {
      return String(v);
    }
  }

  function fmtRatio(v) {
    if (v === null || v === undefined) return "—";
    return (Math.round(v * 1000) / 10) + "%";
  }

  function kvRow(dl, key, value) {
    var dt = document.createElement("dt");
    dt.textContent = key;
    var dd = document.createElement("dd");
    dd.textContent = value === null || value === undefined ? "—" : String(value);
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  function td(text) {
    var cell = document.createElement("td");
    cell.textContent = text === null || text === undefined ? "—" : String(text);
    return cell;
  }

  // ---- CNY ↔ nano 换算（§5：1 CNY = 1e9 nano；全程字符串/BigInt，禁 float）----
  // "12.345678901" → "12345678901"；小数最多 9 位；允许负号（manual_adjustment）。
  // 返回**字符串** nano（§5 v0.3：wire 金额是十进制字符串；去前导零后超 19
  // 位即非法——与桥/服务端 pattern 上限一致）。非法形态返回 null。
  function cnyToNano(text) {
    var s = String(text == null ? "" : text).trim();
    if (!/^-?\d{1,15}(\.\d{1,9})?$/.test(s)) return null;
    var neg = s.charAt(0) === "-";
    if (neg) s = s.slice(1);
    var parts = s.split(".");
    var digits = parts[0] + (parts[1] || "").padEnd(9, "0");
    digits = digits.replace(/^0+(?=\d)/, "");
    if (digits.length > 19) return null;
    if (digits === "0") return "0";
    return neg ? "-" + digits : digits;
  }

  // 精确 nano → CNY 字符串（cap/金额输入框回显用；BigInt 运算，不经 Number）
  function nanoToCnyString(n) {
    if (n === null || n === undefined || n === "") return "";
    try {
      var b = BigInt(n);
      var neg = b < 0n;
      var digits = (neg ? -b : b).toString().padStart(10, "0");
      var whole = digits.slice(0, -9);
      var frac = digits.slice(-9).replace(/0+$/, "");
      return (neg ? "-" : "") + whole + (frac ? "." + frac : "");
    } catch (e) {
      return String(n);
    }
  }

  // ---- 页内确认条（§3.3 危险操作二次确认）----
  // sandbox 无 allow-modals：window.confirm 会被浏览器静默吞掉（恒 false），
  // 因此禁用/重置密码/revoke/调账/开新周期一律走页内两步确认。
  function clearConfirm(box) {
    if (box) { box.hidden = true; box.textContent = ""; }
  }

  function askConfirm(box, text, onConfirm) {
    if (!box) { onConfirm(); return; }
    box.hidden = false;
    box.textContent = "";
    var msg = document.createElement("span");
    msg.className = "adm-confirm-text";
    msg.textContent = text;
    var ok = document.createElement("button");
    ok.type = "button";
    ok.className = "adm-btn-danger";
    ok.textContent = "确认执行";
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "取消";
    ok.addEventListener("click", function () {
      clearConfirm(box);
      onConfirm();
    });
    cancel.addEventListener("click", function () { clearConfirm(box); });
    box.appendChild(msg);
    box.appendChild(ok);
    box.appendChild(cancel);
  }

  function setStatus(id, text) {
    var el = $(id);
    if (el) el.textContent = text || "";
  }

  function actionBtn(label, handler, danger) {
    var btn = document.createElement("button");
    btn.type = "button";
    if (danger) btn.className = "adm-btn-danger";
    btn.textContent = label;
    btn.addEventListener("click", handler);
    return btn;
  }

  function copyToClipboard(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(function () { /* ignore */ });
        return;
      }
    } catch (e) { /* ignore */ }
    // 降级：选中文本由用户手动复制（opaque origin 下 execCommand 可能被拒）
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

  // 常数时间字符串比较（与宿主 admin-host.js 同款；nonce 高熵值下主要是
  // 防侥幸短路，双方值都在各自内存中）
  function timingSafeEqual(a, b) {
    if (typeof a !== "string" || typeof b !== "string") return false;
    if (a.length !== b.length) return false;
    var diff = 0;
    for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
    return diff === 0;
  }

  function onMessage(event) {
    // 来源认证第一道（§8.3 P2 修订，与宿主对称）：只接受宿主 window.parent。
    // opaque origin 读不出宿主 origin，event.origin 恒 "null" 不能用于鉴权；
    // init 本身即 nonce 的下发通道，因此 init 只依赖 source 认证。
    if (event.source !== window.parent) return;
    var env = event.data;
    if (!env || typeof env !== "object" || env.bridge !== BRIDGE) return;

    if (env.kind === "init") {
      // 握手：宿主每次 load 重新下发 nonce —— 直接覆盖旧值并丢弃旧在途请求
      state.nonce = typeof env.nonce === "string" ? env.nonce : null;
      state.protocolVersion = env.protocolVersion || PROTOCOL_FALLBACK;
      state.granted = Array.isArray(env.adminPermissions) ? env.adminPermissions.slice() : [];
      state.dead = false;
      setHandshake("桥接已建立（protocolVersion=" + state.protocolVersion +
        "，管理能力 " + state.granted.length + " 项）");
      resetLists();
      // 深链（/admin#invites 等旧入口 302）：宿主把父页 hash 透传到本 iframe
      // 的 URL，握手后直接落到目标页；无 hash 时回概览（showPage 自带加载）
      showPage(initialPage);
      return;
    }

    if (env.kind === "response") {
      // §8.3 P2 修订：init 之后所有响应必须携带与 init 收到的 session nonce
      // 相等的 nonce（恒时间比较）——不符（其他 frame/窗口伪造、旧 load 残留
      // 回包）一律静默丢弃；requestId 必须命中在途表（防重放/孤儿响应）。
      if (!state.nonce || !timingSafeEqual(env.nonce, state.nonce)) return;
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
      // 宿主作废（reload/登出/插件切换）：立即停止一切请求（本消息的来源
      // 认证即上面的 window.parent 检查——只有宿主能发出）
      state.dead = true;
      state.nonce = null;
      failAllPending({ code: "bridge_invalidated", message: env.message || "宿主已作废桥接会话" });
      setHandshake("桥接会话已作废（" + (env.reason || "unknown") + "），等待重新握手…");
    }
  }

  // ---- 错误呈现（稳定 code → 人话；不泄露任何敏感内容）----
  function errText(err) {
    var code = err && err.code ? err.code : "bridge_error";
    var msg = err && err.message ? err.message : "";
    if (code === "pg_backend_required") {
      return "该数据要求 PostgreSQL 后端（当前部署为 json/dual，稳定拒绝；不降级显示）";
    }
    if (code === "refresh_throttled") return "刷新过于频繁，请稍后再试（服务端限速）";
    if (code === "bridge_invalidated") return "桥接会话已失效，请重新加载插件";
    return code + (msg ? ("：" + msg) : "");
  }

  function handleErr(err, statusEl) {
    showError(err && err.code, err && err.message);
    if (statusEl) statusEl.textContent = errText(err);
  }

  // ------------------------------------------------------------------
  // 概览（§10.1）——「对话额度」与「金额余额」必须并列、不得只显示一个
  // 模糊「额度」；json/dual 后端 billing/turn 段标记不可用。
  // ------------------------------------------------------------------
  function loadOverview() {
    setPageState("overview", "loading");
    var actorDl = $("adm-actor-info");
    request("admin.auth.get", {}).then(function (identity) {
      if (!actorDl) return;
      actorDl.textContent = "";
      kvRow(actorDl, "当前 actor 角色", identity && identity.role);
      kvRow(actorDl, "登录账号（掩码）", identity && identity.loginIdMasked);
      kvRow(actorDl, "预览态", identity && identity.previewActive ? "是" : "否");
    }).catch(function () { /* 身份卡失败不阻塞概览 */ });

    request("admin.overview.get", {}).then(function (ov) {
      hideError();
      renderOverview(ov);
      setPageState("overview", "ready",
        { message: "已更新（" + new Date().toISOString().replace("T", " ").slice(0, 19) + "Z）" });
    }).catch(function (err) {
      handleErr(err, null);
      setPageState("overview", "error",
        { code: err && err.code, message: err && err.message,
          retry: function () { loadOverview(); } });
    });
  }

  // KPI 卡（§9.1 包 E）：用户 / 活跃 / 模型调用 / 缓存命中 / 模拟扣费 /
  // unpriced —— billing 段不可用（json/dual）时只渲染用户侧 KPI 并说明。
  function kpiCard(label, value, note, danger) {
    var card = document.createElement("div");
    card.className = danger ? "adm-kpi adm-kpi--danger" : "adm-kpi";
    var l = document.createElement("p");
    l.className = "adm-kpi-label";
    l.textContent = label;
    var v = document.createElement("p");
    v.className = "adm-kpi-value";
    v.textContent = value;
    card.appendChild(l);
    card.appendChild(v);
    if (note) {
      var n = document.createElement("p");
      n.className = "adm-kpi-note";
      n.textContent = note;
      card.appendChild(n);
    }
    return card;
  }

  function renderOverviewKpis(ov) {
    var wrap = $("adm-ov-kpis");
    if (!wrap) return;
    wrap.textContent = "";
    var u = ov.users || {};
    var b = ov.billing || {};
    wrap.appendChild(kpiCard("用户总数", fmtNum(u.total),
      "启用 " + fmtNum(u.active) + " · 禁用 " + fmtNum(u.disabled)));
    wrap.appendChild(kpiCard("AI access 用户", fmtNum(u.ai_access)));
    if (b.available !== false) {
      wrap.appendChild(kpiCard("模型调用（本周期）", fmtNum(b.model_calls_period),
        "今日 " + fmtNum(b.model_calls_today)));
      wrap.appendChild(kpiCard("缓存命中率", fmtRatio(b.cache_hit_ratio),
        "命中 " + fmtNum(b.cache_hit_input_tokens) + " / 未命中 " +
        fmtNum(b.cache_miss_input_tokens) + " tokens"));
      wrap.appendChild(kpiCard("用户 charge 合计（本周期）", fmtNano(b.charge_nano_cny),
        "PR6 模拟软扣费口径"));
      wrap.appendChild(kpiCard("unpriced 事件（本周期）", fmtNum(b.unpriced_count),
        "未计价 ≠ 0 元", Number(b.unpriced_count) > 0));
    } else {
      wrap.appendChild(kpiCard("计费数据", "不可用",
        "要求 PostgreSQL 后端（" + (b.code || "pg_backend_required") + "）"));
    }
  }

  function renderOverview(ov) {
    renderOverviewKpis(ov);
    var usersDl = $("adm-ov-users");
    if (usersDl) {
      usersDl.textContent = "";
      var u = ov.users || {};
      kvRow(usersDl, "总用户数", fmtNum(u.total));
      kvRow(usersDl, "启用中", fmtNum(u.active));
      kvRow(usersDl, "已禁用", fmtNum(u.disabled));
      kvRow(usersDl, "AI access 用户", fmtNum(u.ai_access));
    }

    // 对话额度（turn budget）
    var turnDl = $("adm-ov-turn");
    if (turnDl) {
      turnDl.textContent = "";
      var t = ov.turn_budget || {};
      if (t.available === false) {
        kvRow(turnDl, "可用性", "不可用（" + (t.code || "pg_backend_required") + "）");
      } else {
        kvRow(turnDl, "周期", "#" + fmtNum(t.period_id) + "（" + fmtTs(t.period_started_at) + " 起）");
        kvRow(turnDl, "平台总额度（用/上限）",
              fmtNum(t.platform && t.platform.total) + " / " + fmtNum(t.platform && t.platform.limit));
        kvRow(turnDl, "用户共享池（用/上限）",
              fmtNum(t.user_pool && t.user_pool.total) + " / " + fmtNum(t.user_pool && t.user_pool.limit));
        kvRow(turnDl, "Demo（24h 窗口/日上限）",
              fmtNum(t.demo && t.demo.total) + " / " + fmtNum(t.demo && t.demo.limit));
        kvRow(turnDl, "owner 保留池（用/保留）",
              fmtNum(t.owner && t.owner.total) + " / " + fmtNum(t.owner && t.owner.reserved_limit));
      }
    }

    // 金额余额（billing）
    var billDl = $("adm-ov-billing");
    if (billDl) {
      billDl.textContent = "";
      var b = ov.billing || {};
      if (b.available === false) {
        kvRow(billDl, "可用性", "不可用（" + (b.code || "pg_backend_required") + "）");
      } else {
        kvRow(billDl, "provider 成本合计（本周期）", fmtNano(b.provider_cost_nano_cny));
        kvRow(billDl, "用户 charge 合计（本周期）", fmtNano(b.charge_nano_cny));
        var snap = b.provider_balance_snapshot;
        kvRow(billDl, "DeepSeek 总余额", snap ? fmtNano(snap.total_balance_nano) : "（暂无快照）");
        kvRow(billDl, "余额快照年龄",
              b.provider_balance_age_seconds === null || b.provider_balance_age_seconds === undefined
                ? "—" : Math.round(b.provider_balance_age_seconds) + " 秒前");
        kvRow(billDl, "unpriced 事件数（本周期）", fmtNum(b.unpriced_count));
        kvRow(billDl, "usage 入库滞后（最大/均值）",
              Math.round(b.ingestion_lag_seconds_max || 0) + "s / " +
              Math.round(b.ingestion_lag_seconds_avg || 0) + "s");
      }
    }

    var usageDl = $("adm-ov-usage");
    if (usageDl) {
      usageDl.textContent = "";
      var b2 = ov.billing || {};
      if (b2.available === false) {
        kvRow(usageDl, "可用性", "不可用（" + (b2.code || "pg_backend_required") + "）");
      } else {
        kvRow(usageDl, "模型调用（今日/本周期）",
              fmtNum(b2.model_calls_today) + " / " + fmtNum(b2.model_calls_period));
        kvRow(usageDl, "cache 命中输入 tokens", fmtNum(b2.cache_hit_input_tokens));
        kvRow(usageDl, "cache 未命中输入 tokens", fmtNum(b2.cache_miss_input_tokens));
        kvRow(usageDl, "输出 tokens", fmtNum(b2.output_tokens));
        kvRow(usageDl, "cache 命中率", fmtRatio(b2.cache_hit_ratio));
      }
    }

    // 告警区（unpriced 单独告警，不混入 0 元）
    var alertCard = $("adm-ov-alert-card");
    var alertList = $("adm-ov-alerts");
    if (alertCard && alertList) {
      alertList.textContent = "";
      var b3 = ov.billing || {};
      var alerts = [];
      if (b3.available !== false) {
        if (Number(b3.unpriced_count) > 0) {
          alerts.push("本周期有 " + b3.unpriced_count + " 条 unpriced 事件（未计价 ≠ 0 元，见「额度与账单」页告警区）");
        }
        var maxLag = Number(b3.ingestion_lag_seconds_max || 0);
        if (maxLag > 900) {
          alerts.push("usage 入库滞后最大 " + Math.round(maxLag) + "s（>15 分钟，检查 HistoPilot outbox 投递）");
        }
        var snapAge = b3.provider_balance_age_seconds;
        if (b3.provider_balance_snapshot && snapAge !== null && snapAge !== undefined
            && Number(snapAge) > 86400) {
          alerts.push("DeepSeek 余额快照已超过 24 小时未更新（" +
                      Math.round(Number(snapAge) / 3600) + " 小时）");
        }
      }
      alertCard.hidden = alerts.length === 0;
      alerts.forEach(function (text) {
        var li = document.createElement("li");
        li.textContent = text;
        alertList.appendChild(li);
      });
    }
  }

  // ------------------------------------------------------------------
  // 用户（§10.2 只读）
  // ------------------------------------------------------------------
  function resetLists() {
    state.cursors = { users: null, usage: null, unpriced: null, ledger: null,
                      audit: null, acqUsers: null, invites: null };
    state.account = null;
    ["adm-users-tbody", "adm-usage-tbody", "adm-unpriced-tbody",
     "adm-ledger-tbody", "adm-audit-tbody", "adm-invites-tbody",
     "adm-acq-funnel-tbody", "adm-acq-users-tbody",
     "adm-plugins-tbody"].forEach(function (id) {
      var el = $(id);
      if (el) el.textContent = "";
    });
  }

  function loadUsers(append) {
    var seq = state.listSeq;
    var f = state.filters.users || {};
    var payload = {
      limit: 50,
      cursor: append ? state.cursors.users : null,
    };
    if (f.q) payload.q = f.q;
    if (f.enabled === "true" || f.enabled === "false") payload.enabled = f.enabled === "true";
    if (f.ai === "true" || f.ai === "false") payload.ai_access = f.ai === "true";
    var status = $("adm-users-status");
    setPageState("users", "loading");
    request("admin.users.list", payload).then(function (res) {
      if (seq !== state.listSeq) return; // 页面已切换/新筛选已发起：晚到响应丢弃
      hideError();
      var items = res.items || [];
      renderUsers(items, append);
      if (!append && !items.length) {
        var f2 = state.filters.users || {};
        setPageState("users", "empty", {
          message: (f2.q || f2.enabled || f2.ai)
            ? "没有匹配筛选条件的用户；调整筛选或清空后重试。"
            : "暂无用户。可通过上方「创建用户」新增（role=user）。",
        });
      } else {
        setPageState("users", "ready", {
          message: "已更新（" + new Date().toISOString().replace("T", " ").slice(0, 19) + "Z）",
        });
      }
      state.cursors.users = res.next_cursor || null;
      var more = $("adm-users-more-btn");
      if (more) more.disabled = !res.next_cursor;
      if (status) status.textContent = res.next_cursor ? "还有更多" : "已到底";
    }).catch(function (err) {
      if (seq !== state.listSeq) return;
      handleErr(err, status);
      setPageState("users", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadUsers(false); },
      });
    });
  }

  // §9.1 包 E：表格只保留高频列（显示名/账号/角色/状态/AI/最近调用/操作），
  // 余额、来源、注册方式、对话额度、caps 与全部行操作收纳进详情抽屉。
  var drawerUser = null;

  function renderUsers(items, append) {
    var tbody = $("adm-users-tbody");
    if (!tbody) return;
    if (!append) tbody.textContent = "";
    items.forEach(function (u) {
      var tr = document.createElement("tr");
      tr.appendChild(td(u.display_name));
      tr.appendChild(td(u.login_id_masked));
      tr.appendChild(td(u.role));
      tr.appendChild(td(u.enabled ? "启用" : "禁用"));
      tr.appendChild(td(u.ai_access ? "是" : "否"));
      tr.appendChild(td(fmtTs(u.last_ai_call_at)));
      var cell = document.createElement("td");
      cell.className = "adm-actions-cell";
      cell.appendChild(actionBtn("详情", function () { openUserDrawer(u); }));
      tr.appendChild(cell);
      tbody.appendChild(tr);
    });
  }

  // 剩余额度的可见状态（§6.2/E2E）：负值 = overage 观测（下次预占拒绝）；
  // 0 = 已用尽；正值正常展示。金额全程字符串/BigInt，不经 Number。
  function remainingText(remainingNano) {
    if (remainingNano === null || remainingNano === undefined) return "—";
    var neg = false;
    var b = BigInt(remainingNano);
    if (b < 0n) { neg = true; b = -b; }
    var cny = nanoToCnyString((neg ? "-" : "") + b.toString());
    if (neg) return "超支 " + cny + " CNY（overage 观测；下次预占将被拒绝）";
    if (b === 0n) return "0 CNY（已用尽：下次预占将被拒绝）";
    return cny + " CNY";
  }

  // 月额度覆盖编辑器（§6.2）：输入 CNY → nano 十进制字符串；空值提交=清除
  // （DELETE，下个窗口回退全局默认）。owner 无覆盖语义（服务端同样拒绝）。
  function renderOverrideEditor(u) {
    var wrap = document.createElement("div");
    wrap.className = "adm-drawer-override";
    var title = document.createElement("p");
    title.className = "adm-note";
    title.textContent = "月额度覆盖（calendar_month）：设置只影响之后新建的窗口；清除后下个窗口回退全局默认。";
    var input = document.createElement("input");
    input.type = "text";
    input.placeholder = "新覆盖额度（CNY，如 30.5）";
    input.autocomplete = "off";
    var setBtn = actionBtn("设置/更新覆盖", function () {
      var text = (input.value || "").trim();
      var limit = text ? cnyToNano(text) : null;
      if (text && limit === null) {
        setStatus("adm-users-status", "覆盖金额非法（CNY，最多 9 位小数）");
        return;
      }
      request("admin.users.setSpendOverride", {
        user_id: u.user_id,
        monthly_limit_nano_cny: limit,
      }).then(function () {
        setStatus("adm-users-status", limit === null
          ? "已清除月额度覆盖（下个窗口起回退全局默认）"
          : "已设置月额度覆盖 " + nanoToCnyString(limit) + " CNY（下个窗口起生效）");
        loadUsers(false);
        closeUserDrawer();
      }).catch(function (err) {
        showError(err && err.code, err && err.message);
        setStatus("adm-users-status", errText(err));
      });
    });
    var clearBtn = actionBtn("清除覆盖", function () {
      request("admin.users.setSpendOverride", {
        user_id: u.user_id, monthly_limit_nano_cny: null,
      }).then(function (res) {
        setStatus("adm-users-status", (res && res.cleared
          ? "已清除月额度覆盖" : "本无覆盖（无变化）") +
          "（下个窗口起回退全局默认）");
        loadUsers(false);
        closeUserDrawer();
      }).catch(function (err) {
        showError(err && err.code, err && err.message);
        setStatus("adm-users-status", errText(err));
      });
    });
    wrap.appendChild(title);
    wrap.appendChild(input);
    wrap.appendChild(setBtn);
    if (u.role !== "owner") wrap.appendChild(clearBtn);
    return wrap;
  }

  function openUserDrawer(u) {
    if (!els.drawer || !els.drawerBody) return;
    drawerUser = u;
    els.drawerBody.textContent = "";
    var dl = document.createElement("dl");
    dl.className = "adm-kv";
    kvRow(dl, "user_id", u.user_id);
    kvRow(dl, "显示名", u.display_name);
    kvRow(dl, "登录账号（掩码）", u.login_id_masked);
    kvRow(dl, "角色", u.role);
    kvRow(dl, "状态", u.enabled ? "启用" : "禁用");
    kvRow(dl, "AI access", u.ai_access ? "是" : "否");
    kvRow(dl, "创建时间", fmtTs(u.created_at));
    kvRow(dl, "注册方式", u.registration_method);
    // 批次 F：对话额度（turn_used/turn_limit）字段已随 turn 消费闸退役删除
    // 金额余额：null = 尚未开户（不显示 0）
    kvRow(dl, "金额余额", u.billing ? fmtNano(u.billing.balance_nano) : "未开户");
    kvRow(dl, "soft/hard cap（兼容字段，非当前月额度）", u.billing
          ? ((u.billing.soft_spend_cap_nano === null ? "—" : fmtNano(u.billing.soft_spend_cap_nano)) +
             " / " +
             (u.billing.hard_spend_cap_nano === null ? "—" : fmtNano(u.billing.hard_spend_cap_nano)))
          : "—");
    // 当前月金额窗口 + 默认/覆盖状态（§6.2，批次 D）
    if (u.spend && u.spend.window && !u.spend.error) {
      var w = u.spend.window;
      kvRow(dl, "本月额度（limit snapshot）", fmtNano(w.limit_nano_snapshot));
      kvRow(dl, "本月已消费（spent）", fmtNano(w.spent_nano_cny));
      kvRow(dl, "本月预占（reserved）", fmtNano(w.reserved_nano_cny));
      kvRow(dl, "本月剩余（remaining）", remainingText(w.remaining_nano));
      kvRow(dl, "窗口边界",
            fmtTs(w.window_start) + " → " + fmtTs(w.window_end) + "（上海时区自然月/周）");
    } else if (u.spend && u.spend.error) {
      kvRow(dl, "本月金额窗口", "不可用（" + u.spend.error + "）");
    } else {
      kvRow(dl, "本月金额窗口", "不可用（要求 PostgreSQL 后端）");
    }
    kvRow(dl, "月额度策略",
          u.role === "owner" ? "owner 独立策略（不与用户共池）"
          : (u.spend && u.spend.policy_scope === "user_override")
            ? "用户覆盖（user_override，policy " + (u.spend.policy_id || "?") + "）"
            : "继承全局默认（user_default）");
    kvRow(dl, "最近 AI 调用", fmtTs(u.last_ai_call_at));
    els.drawerBody.appendChild(dl);
    var actions = document.createElement("div");
    actions.className = "adm-drawer-actions";
    actions.appendChild(renderUserActions(u));
    els.drawerBody.appendChild(actions);
    if (u.role !== "owner") {
      els.drawerBody.appendChild(renderOverrideEditor(u));
    }
    els.drawer.hidden = false;
    if (els.drawerMask) els.drawerMask.hidden = false;
    if (els.drawerClose && els.drawerClose.focus) els.drawerClose.focus();
  }

  function closeUserDrawer() {
    drawerUser = null;
    if (els.drawer) els.drawer.hidden = true;
    if (els.drawerMask) els.drawerMask.hidden = true;
  }

  // 行内操作（§10.2）：身份预览 / 启停 / AI access / 重置密码（仅普通用户）/
  // 打开账本。owner 行不提供禁用与重置（break-glass 不变量，服务端同样 409）；
  // 禁用、重置密码与身份预览为危险操作，走页内确认条。
  function renderUserActions(u) {
    var cell = document.createElement("td");
    cell.className = "adm-actions-cell";
    var isOwner = u.role === "owner";
    if (!isOwner) {
      cell.appendChild(actionBtn(u.enabled ? "禁用" : "启用", function () {
        if (u.enabled) {
          askConfirm($("adm-users-confirm"),
            "确认禁用用户 " + (u.display_name || u.user_id) + "？其全部会话将立即失效。",
            function () { setUserEnabled(u, false); });
        } else {
          setUserEnabled(u, true);
        }
      }, u.enabled));
    }
    cell.appendChild(actionBtn(u.ai_access ? "收回 AI" : "授予 AI", function () {
      setAiAccess(u, !u.ai_access);
    }));
    if (!isOwner) {
      cell.appendChild(actionBtn("重置密码", function () {
        askResetPassword(u);
      }));
    }
    cell.appendChild(actionBtn("身份预览", function () {
      askConfirm($("adm-users-confirm"),
        "确认以 " + (u.display_name || u.user_id) + " 的身份进入只读预览？" +
        "预览期间以该用户视角使用 Viewer；管理写操作仍要求真实 owner（被拒绝）。",
        function () { startPreviewFor(u); });
    }));
    cell.appendChild(actionBtn("打开账本", function () {
      openLedgerForUser(u.user_id);
    }));
    return cell;
  }

  // 身份预览（§10.2，PR5 修订恢复入口）：进入后宿主页 effective subject 变为
  // 目标用户。本 iframe 是 opaque origin（sandbox 无 allow-top-navigation），
  // 不能也不应把宿主导航到 Viewer——只提示 owner 手动切换标签页；停止按钮
  // 在宿主页右上角的预览横幅（app.js），不经桥。
  function startPreviewFor(u) {
    request("admin.users.startPreview", { user_id: u.user_id })
      .then(function (res) {
        var until = res && res.preview && res.preview.expires_at
          ? "（至 " + fmtTs(res.preview.expires_at) + " 自动退出）" : "";
        setStatus("adm-users-status",
          "预览已开启，切到 Viewer 标签页查看，右上角可停止" + until);
      })
      .catch(function (err) {
        userWriteDone(err, null, null, "adm-users-status");
      });
  }

  function userWriteDone(err, res, okText, statusId) {
    if (err) {
      showError(err && err.code, err && err.message);
      setStatus(statusId, errText(err));
      return;
    }
    setStatus(statusId, okText);
    loadUsers(false);
  }

  function setUserEnabled(u, enabled) {
    request("admin.users.setEnabled",
            { user_id: u.user_id, enabled: enabled })
      .then(function (res) {
        userWriteDone(null, res,
          enabled ? "已启用（auth_version 已推进，旧会话失效）" : "已禁用（旧会话立即失效）",
          "adm-users-status");
      })
      .catch(function (err) { userWriteDone(err, null, null, "adm-users-status"); });
  }

  function setAiAccess(u, enabled) {
    request("admin.users.setAiAccess",
            { user_id: u.user_id, enabled: enabled })
      .then(function () {
        userWriteDone(null, null,
          enabled ? "已授予 AI access" : "已收回 AI access",
          "adm-users-status");
      })
      .catch(function (err) { userWriteDone(err, null, null, "adm-users-status"); });
  }

  // 重置密码：页内输入新密码（sandbox 下 window.prompt 不可用）+ 确认执行
  function askResetPassword(u) {
    var box = $("adm-users-confirm");
    if (!box) return;
    box.hidden = false;
    box.textContent = "";
    var label = document.createElement("span");
    label.className = "adm-confirm-text";
    label.textContent = "为 " + (u.display_name || u.user_id) + " 设置新密码（≥15 位）：";
    var input = document.createElement("input");
    input.type = "password";
    input.minLength = 15;
    input.maxLength = 200;
    input.autocomplete = "new-password";
    input.placeholder = "新密码（≥15 位）";
    var ok = actionBtn("确认重置", function () {
      var np = input.value || "";
      if (np.length < 15) {
        setStatus("adm-users-status", "新密码至少 15 位（当前 " + np.length + " 位）");
        return;
      }
      clearConfirm(box);
      request("admin.users.resetPassword", { user_id: u.user_id, password: np })
        .then(function () {
          userWriteDone(null, null,
            "密码已重置，该用户全部会话已退出", "adm-users-status");
        })
        .catch(function (err) {
          userWriteDone(err, null, null, "adm-users-status");
        });
    }, true);
    var cancel = actionBtn("取消", function () { clearConfirm(box); });
    box.appendChild(label);
    box.appendChild(input);
    box.appendChild(ok);
    box.appendChild(cancel);
  }

  // 行内「打开账本」→ 额度与账单页：usage 明细按该 user_id 过滤 + 账户卡带入
  function openLedgerForUser(userId) {
    state.filters.usage = { model: "", user_id: userId, status: "" };
    var input = $("adm-usage-user");
    if (input) input.value = userId;
    var acct = $("adm-acct-user");
    if (acct) acct.value = userId;
    showPage("billing");
  }

  function submitCreateUser() {
    var loginId = ($("adm-users-new-login") && $("adm-users-new-login").value || "").trim();
    var display = ($("adm-users-new-display") && $("adm-users-new-display").value || "").trim();
    var password = $("adm-users-new-password") ? $("adm-users-new-password").value : "";
    var limitText = ($("adm-users-new-limit") && $("adm-users-new-limit").value || "").trim();
    if (!loginId) { setStatus("adm-users-create-status", "缺少登录账号"); return; }
    if (password.length < 15) {
      setStatus("adm-users-create-status",
                "初始密码至少 15 位（当前 " + password.length + " 位）");
      return;
    }
    var payload = { login_id: loginId, password: password };
    if (display) payload.display_name = display;
    // 批次 D §5.1：可选月额度覆盖（CNY 输入 → nano 十进制字符串；留空 =
    // 继承全局默认，不建 override；建号+覆盖+audit 服务端同一事务）
    if (limitText) {
      var limit = cnyToNano(limitText);
      if (limit === null) {
        setStatus("adm-users-create-status",
                  "每月金额覆盖非法（CNY，最多 9 位小数，如 20 或 12.5）");
        return;
      }
      payload.monthly_limit_nano_cny = limit;
    }
    setStatus("adm-users-create-status", "创建中…");
    request("admin.users.create", payload).then(function (res) {
      ["adm-users-new-login", "adm-users-new-display", "adm-users-new-password",
       "adm-users-new-limit"].forEach(function (id) {
        var el = $(id); if (el) el.value = "";
      });
      setStatus("adm-users-create-status",
        "已创建 " + ((res && res.user && res.user.user_id) || loginId) +
        (payload.monthly_limit_nano_cny
          ? "（月额度覆盖 " + nanoToCnyString(payload.monthly_limit_nano_cny) + " CNY）"
          : "（继承默认月额度）"));
      loadUsers(false);
    }).catch(function (err) {
      showError(err && err.code, err && err.message);
      setStatus("adm-users-create-status", errText(err));
    });
  }

  // ------------------------------------------------------------------
  // 邀请与来源（§10.3 只读部分，PR4）：注册模式 + campaign 漏斗 +
  // 用户来源明细（first/last touch 分列）。邀请创建/撤销按钮属 PR5。
  // ------------------------------------------------------------------
  // 来源空状态（§9.2 包 E 前置）：零数据必须解释「仅覆盖上线后的有效访问
  // 触点、历史用户尚未回填」，且 permission_denied / 网络失败 / handshake
  // 失败绝不渲染成这个 empty 状态（错误走 handleErr 的错误呈现）。
  var ACQ_EMPTY_TEXT = "暂无来源归因数据。来源统计仅覆盖功能上线后的有效访问触点，历史用户尚未回填。";

  function setAcqEmpty(show) {
    // 子区块只留简短提示；完整空态文案（含「历史用户尚未回填」说明）由
    // 页级状态条统一展示一次（复核 P2：两处重复展示长文案属冗余）
    var funnelEmpty = $("adm-acq-empty");
    if (funnelEmpty) {
      funnelEmpty.hidden = !show;
      if (show) funnelEmpty.textContent = "本周期暂无来源访问记录。";
    }
    var usersEmpty = $("adm-acq-users-empty");
    if (usersEmpty) {
      usersEmpty.hidden = !show;
      if (show) usersEmpty.textContent = "暂无已归因用户。";
    }
  }

  // 来源页聚合加载（复核 P2 2026-08-29：三个首屏请求由协调器统一计算
  // 页面终态——此前没有任何一处写入 ready/empty/error，生产零数据场景
  // 永久停留「加载中…」）。语义：
  //   - 任一请求有数据 → ready（部分失败时附 partial 提示）；
  //   - 三组都成功且都无数据 → empty（完整说明文案只在此展示一次）；
  //   - 全部失败 → error（含重试）。
  function loadAcquisitionPage() {
    var seq = state.listSeq;
    setPageState("invites", "loading");
    loadAcqModeCard();
    var settled = Promise.allSettled([
      loadAcqFunnel(),
      loadAcqUsers(false),
      loadInvites(false),
    ]);
    settled.then(function (results) {
      if (seq !== state.listSeq) return; // 页面已切换：不写终态
      var ok = [], failed = [];
      results.forEach(function (r) {
        (r.status === "fulfilled" ? ok : failed).push(r);
      });
      if (!ok.length) {
        var err = results[0].reason;
        setPageState("invites", "error", {
          code: err && err.code, message: err && err.message,
          retry: function () { loadAcquisitionPage(); },
        });
        return;
      }
      var anyData = ok.some(function (r) { return r.value === true; });
      if (anyData) {
        setPageState("invites", "ready", {
          message: "已更新（" +
            new Date().toISOString().replace("T", " ").slice(0, 19) + "Z）" +
            (failed.length ? "（部分数据加载失败，可刷新重试）" : ""),
        });
      } else {
        setPageState("invites", "empty", { message: ACQ_EMPTY_TEXT });
      }
    });
  }

  function loadAcqModeCard() {
    // 注册模式随漏斗汇总一并返回；失败时单独给出错误卡内容
    var dl = $("adm-acq-mode");
    if (!dl) return;
    dl.textContent = "";
    kvRow(dl, "可用性", "等待漏斗汇总加载…");
  }

  function renderAcqMode(mode) {
    var dl = $("adm-acq-mode");
    if (!dl) return;
    dl.textContent = "";
    kvRow(dl, "当前模式", mode || "—");
  }

  function loadAcqFunnel() {
    var seq = state.listSeq;
    return request("admin.acquisition.summary", {}).then(function (res) {
      if (seq !== state.listSeq) return;
      hideError();
      setAcqEmpty(!(res.items && res.items.length));
      renderAcqMode(res.registration_mode);
      var tbody = $("adm-acq-funnel-tbody");
      if (tbody) {
        tbody.textContent = "";
        (res.items || []).forEach(function (row) {
          var tr = document.createElement("tr");
          tr.appendChild(td(row.source_code));
          tr.appendChild(td(row.campaign_id));
          tr.appendChild(td(row.campaign_id === null ? "—" : (row.campaign_status || "—")));
          tr.appendChild(td(fmtNum(row.visits)));
          tr.appendChild(td(fmtNum(row.visitors)));
          tr.appendChild(td(fmtNum(row.registrations)));
          tr.appendChild(td(fmtNum(row.first_ai_count)));
          tbody.appendChild(tr);
        });
      }
      var totals = $("adm-acq-totals");
      if (totals) {
        totals.textContent = "";
        var t = res.totals || {};
        kvRow(totals, "合计", "访问 " + fmtNum(t.visits) + " · 注册 " +
              fmtNum(t.registrations) + " · 首次 AI " + fmtNum(t.first_ai_count));
      }
      return !!(res.items && res.items.length); // 供协调器计算终态
    }).catch(function (err) {
      if (seq !== state.listSeq) throw err;
      setAcqEmpty(false); // 错误不是空状态
      var dl = $("adm-acq-mode");
      if (dl) { dl.textContent = ""; kvRow(dl, "可用性", errText(err)); }
      handleErr(err, null);
      throw err; // 交给协调器计数（partial-error 语义）
    });
  }

  // 触点列：source/campaign + 时间（visitor 只给 hash 前缀；无完整 IP/query）
  function acqTouchText(touch) {
    if (!touch) return "—（无触点）";
    var label = (touch.source_code || "?") +
      (touch.campaign_id ? (" / " + touch.campaign_id) : "");
    return label + "（" + fmtTs(touch.touched_at) + "）";
  }

  function loadAcqUsers(append) {
    var seq = state.listSeq;
    var payload = { limit: 50, cursor: append ? state.cursors.acqUsers : null };
    var status = $("adm-acq-status");
    return request("admin.acquisition.list", payload).then(function (res) {
      if (seq !== state.listSeq) return;
      hideError();
      var usersEmpty = $("adm-acq-users-empty");
      if (usersEmpty) usersEmpty.hidden = !!(res.items && res.items.length);
      var tbody = $("adm-acq-users-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      (res.items || []).forEach(function (u) {
        var tr = document.createElement("tr");
        tr.appendChild(td(u.display_name));
        tr.appendChild(td(u.login_id_masked));
        tr.appendChild(td(u.source_code));
        tr.appendChild(td(u.campaign_id));
        tr.appendChild(td(u.attribution_method));
        tr.appendChild(td(acqTouchText(u.first_touch)));
        tr.appendChild(td(acqTouchText(u.last_touch)));
        tr.appendChild(td(fmtTs(u.attributed_at)));
        tbody.appendChild(tr);
      });
      state.cursors.acqUsers = res.next_cursor || null;
      var more = $("adm-acq-more-btn");
      if (more) more.disabled = !res.next_cursor;
      if (status) status.textContent = res.next_cursor ? "还有更多" : "已到底";
      return !!(res.items && res.items.length); // 供协调器计算终态
    }).catch(function (err) {
      if (seq === state.listSeq) handleErr(err, status);
      throw err;
    });
  }

  // ------------------------------------------------------------------
  // 邀请管理（§10.3，PR5 写部分）：列表 + 创建（token 仅一次）+ 撤销。
  // ------------------------------------------------------------------
  function inviteStatusLabel(inv) {
    if (inv.revoked_at) return "已撤销";
    if (inv.consumed_at) return "已消费";
    if (inv.expires_at !== null && inv.expires_at !== undefined
        && inv.expires_at <= Date.now() / 1000) return "已过期";
    return "开放中";
  }

  function loadInvites(append) {
    var seq = state.listSeq;
    var payload = { limit: 50, cursor: append ? state.cursors.invites : null };
    var status = $("adm-invites-status");
    return request("admin.invites.list", payload).then(function (res) {
      if (seq !== state.listSeq) return;
      hideError();
      var tbody = $("adm-invites-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      (res.invites || []).forEach(function (inv) {
        var tr = document.createElement("tr");
        tr.appendChild(td(inv.invite_id));
        tr.appendChild(td(inv.login_id_masked || "（不绑定）"));
        tr.appendChild(td(inv.ai_access ? "开" : "关"));
        // 月额度模板（批次 D §5.2）：null=兑换继承默认；nano → CNY 精确换算
        tr.appendChild(td(inv.monthly_limit_nano_cny === null ||
                          inv.monthly_limit_nano_cny === undefined
          ? "默认" : nanoToCnyString(inv.monthly_limit_nano_cny) + " CNY"));
        tr.appendChild(td(inv.cohort));
        tr.appendChild(td((inv.source_code || "—") +
                          (inv.campaign_id ? (" / " + inv.campaign_id) : "")));
        tr.appendChild(td(inviteStatusLabel(inv)));
        tr.appendChild(td(fmtTs(inv.expires_at)));
        var cell = document.createElement("td");
        cell.className = "adm-actions-cell";
        if (inviteStatusLabel(inv) === "开放中") {
          cell.appendChild(actionBtn("撤销", function () {
            askConfirm($("adm-invites-confirm"),
              "确认撤销邀请 " + inv.invite_id + "？撤销后立即不可兑换。",
              function () { revokeInvite(inv.invite_id); });
          }, true));
        }
        tr.appendChild(cell);
        tbody.appendChild(tr);
      });
      state.cursors.invites = res.next_cursor || null;
      var more = $("adm-invites-more-btn");
      if (more) more.disabled = !res.next_cursor;
      if (status) status.textContent = res.next_cursor ? "还有更多" : "已到底";
      return !!(res.invites && res.invites.length); // 供协调器计算终态
    }).catch(function (err) {
      if (seq === state.listSeq) handleErr(err, status);
      throw err;
    });
  }

  function revokeInvite(inviteId) {
    request("admin.invites.revoke", { invite_id: inviteId })
      .then(function () {
        setStatus("adm-invites-status", "已撤销 " + inviteId);
        loadInvites(false);
      })
      .catch(function (err) { handleErr(err, $("adm-invites-status")); });
  }

  function showInviteTokenOnce(token) {
    var box = $("adm-invite-token-box");
    var code = $("adm-invite-token");
    if (!box || !code) return;
    code.textContent = token;
    box.hidden = false;
  }

  function submitCreateInvite() {
    var loginId = ($("adm-invite-login") && $("adm-invite-login").value || "").trim();
    var ttlRaw = $("adm-invite-ttl") ? $("adm-invite-ttl").value : "";
    var ai = $("adm-invite-ai") ? !!$("adm-invite-ai").checked : false;
    var limitText = ($("adm-invite-limit") && $("adm-invite-limit").value || "").trim();
    var cohort = ($("adm-invite-cohort") && $("adm-invite-cohort").value || "").trim();
    var note = ($("adm-invite-note") && $("adm-invite-note").value || "").trim();
    var source = ($("adm-invite-source") && $("adm-invite-source").value || "").trim();
    var campaign = ($("adm-invite-campaign") && $("adm-invite-campaign").value || "").trim();
    var ttl = parseInt(ttlRaw, 10);
    if (!ttl || ttl < 1 || ttl > 720) {
      setStatus("adm-invite-create-status", "有效期需为 1–720 小时");
      return;
    }
    var payload = { ttl_hours: ttl, ai_access: ai };
    // 批次 D §5.2：可选月额度模板（兑换时同事务为新用户建 override）
    if (limitText) {
      var limit = cnyToNano(limitText);
      if (limit === null) {
        setStatus("adm-invite-create-status",
                  "月额度模板非法（CNY，最多 9 位小数，如 20 或 12.5）");
        return;
      }
      payload.monthly_limit_nano_cny = limit;
    }
    if (loginId) payload.login_id = loginId;
    if (cohort) payload.cohort = cohort;
    if (note) payload.note = note;
    if (source) payload.source_code = source;
    if (campaign) payload.campaign_id = campaign;
    setStatus("adm-invite-create-status", "创建中…");
    request("admin.invites.create", payload).then(function (res) {
      setStatus("adm-invite-create-status",
        "已创建 " + ((res && res.invite && res.invite.invite_id) || "?") +
        "；明文邀请码只显示这一次：");
      showInviteTokenOnce((res && res.invite && res.invite.token) || "");
      ["adm-invite-login", "adm-invite-limit", "adm-invite-cohort",
       "adm-invite-note", "adm-invite-source", "adm-invite-campaign"].forEach(
        function (id) {
          var el = $(id);
          if (el) el.value = "";
        });
      loadInvites(false);
    }).catch(function (err) {
      showError(err && err.code, err && err.message);
      setStatus("adm-invite-create-status", errText(err));
    });
  }

  // ------------------------------------------------------------------
  // 设置（§6.1，批次 D）：注册模式 + 金额策略 + enforcement + 运行时安全
  // 参数 + 「调整当前窗口」（独立按钮 + 二次确认 + 影响展示）。
  // 金额输入（CNY）→ wire 十进制字符串（cnyToNano/nanoToCnyString 全程
  // BigInt，>2^53 不失真）；策略额度更新携带 CAS（policy_id + version）。
  // ------------------------------------------------------------------
  var SPEND_POLICY_FIELDS = [
    ["adm-spend-demo-week", "demo_global", "demo_weekly_limit"],
    ["adm-spend-user-month", "user_default", "user_default_monthly_limit"],
    ["adm-spend-owner-month", "owner", "owner_monthly_limit"],
  ];

  function settingsUpdateResult(res, statusId, okText) {
    if (res && res.failed && res.failed.error) {
      var f = res.failed;
      setStatus(statusId,
        "部分失败：第 " + (f.step || "?") + " 步（" + f.error.code +
        (f.error.message ? "：" + f.error.message : "") +
        "）未保存；此前 " + (res.applied || []).length + " 项已保存（各项" +
        "独立事务+审计，不回滚）。请刷新后重试剩余项。");
      showError(f.error.code, f.error.message);
      return;
    }
    setStatus(statusId, okText + "（已保存 " +
      ((res && res.applied) || []).length + " 项）");
  }

  function loadSettingsPage() {
    setPageState("settings", "loading");
    var seq = state.listSeq;
    request("admin.settings.get", {}).then(function (settings) {
      if (seq !== state.listSeq) return;
      hideError();
      state.settingsSnapshot = settings;
      renderSettings(settings);
      // 「调整当前窗口」主体 = demo 周 + owner 月（settings 聚合）+ 各用户月
      // 窗口（users 列表的 spend 字段，服务端单事务批量解析）
      return request("admin.users.list", { limit: 50 }).then(function (res) {
        if (seq !== state.listSeq) return;
        buildWindowSubjects(settings, (res && res.items) || []);
        setPageState("settings", "ready", {
          message: "已更新（" +
            new Date().toISOString().replace("T", " ").slice(0, 19) + "Z）",
        });
      });
    }).catch(function (err) {
      if (seq !== state.listSeq) return;
      handleErr(err, null);
      setPageState("settings", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadSettingsPage(); },
      });
    });
  }

  function renderSettings(settings) {
    // 注册模式卡
    var reg = settings.registration || {};
    var regSelect = $("adm-regmode-select");
    if (regSelect) regSelect.value = reg.mode || "closed";
    var regDl = $("adm-regmode-info");
    if (regDl) {
      regDl.textContent = "";
      kvRow(regDl, "当前生效模式", reg.mode);
      kvRow(regDl, "存储模式", reg.stored_mode);
      kvRow(regDl, "前置条件",
        (reg.precondition_failures || []).length
          ? "不满足：" + (reg.precondition_failures || []).join("；")
          : "满足（HTTPS / Secure Cookie / PostgreSQL）");
      kvRow(regDl, "支持的模式", (reg.supported_modes || []).join(" / ") +
        "（public 本阶段不支持）");
    }
    // 金额策略卡
    var spend = settings.spend || {};
    var modeSelect = $("adm-spend-mode");
    if (spend.available !== false) {
      if (modeSelect) modeSelect.value = spend.enforcement_mode || "shadow";
      SPEND_POLICY_FIELDS.forEach(function (pair) {
        var el = $(pair[0]);
        var policy = (spend.policies || {})[pair[1]];
        if (el && policy && policy.limit_nano_cny !== null &&
            policy.limit_nano_cny !== undefined) {
          el.value = nanoToCnyString(policy.limit_nano_cny);
        } else if (el && !policy) {
          el.value = "";
          el.placeholder = "（无有效策略：" + pair[1] + "）";
        }
      });
    }
    var spendDl = $("adm-spend-info");
    if (spendDl) {
      spendDl.textContent = "";
      if (spend.available === false) {
        kvRow(spendDl, "可用性", "不可用（" + (spend.code || "pg_backend_required") + "）");
      } else {
        kvRow(spendDl, "enforcement 模式", spend.enforcement_mode +
          "（registered/all 只应在批次 C2 验收后切换）");
        SPEND_POLICY_FIELDS.forEach(function (pair) {
          var policy = (spend.policies || {})[pair[1]];
          kvRow(spendDl, pair[1] + " 策略", policy
            ? ("额度 " + fmtNano(policy.limit_nano_cny) + "；版本 v" +
               policy.version + "；生效自 " + fmtTs(policy.effective_from))
            : "无有效策略（fail-closed）");
        });
        var bounds = spend.next_window_bounds || {};
        kvRow(spendDl, "Demo 下个周期边界",
          bounds.demo_week ? (fmtTs(bounds.demo_week[0]) + " → " +
                              fmtTs(bounds.demo_week[1])) : "—");
        kvRow(spendDl, "用户月度下个周期边界",
          bounds.user_month ? (fmtTs(bounds.user_month[0]) + " → " +
                               fmtTs(bounds.user_month[1])) : "—");
        var wins = spend.current_windows || {};
        ["demo", "owner"].forEach(function (k) {
          var w = wins[k];
          kvRow(spendDl, "当前窗口（" + k + "）", !w ? "—"
            : w.error ? ("不可用（" + w.error + "）")
            : ("额度 " + fmtNano(w.limit_nano_snapshot) + "；已消费 " +
               fmtNano(w.spent_nano_cny) + "；预占 " +
               fmtNano(w.reserved_nano_cny) + "；剩余 " +
               remainingText(w.remaining_nano) + "；窗口 v" + w.version));
        });
      }
    }
    // 记录「加载时回显值」：保存只提交被修改的字段（未改字段沿用现值——
    // 与 turn 预算编辑器「未填沿用现值」同一语义；预填表单不能每次全量提交）
    SPEND_POLICY_FIELDS.forEach(function (pair) {
      var el = $(pair[0]);
      if (el && el.setAttribute) {
        el.setAttribute("data-loaded", String(el.value || "").trim());
      }
    });
    // 运行时安全参数卡
    var rt = settings.runtime || {};
    var demoEnabled = $("adm-rt-demo-enabled");
    if (rt.available !== false) {
      var limits = rt.limits || {};
      if (demoEnabled) demoEnabled.checked = !!limits.demo_enabled;
      [["adm-rt-psteps", "platform_task_max_steps"],
       ["adm-rt-demosteps", "demo_task_max_steps"],
       ["adm-rt-ownsteps", "own_task_max_steps_limit"],
       ["adm-rt-concurrency", "demo_max_concurrency"]].forEach(
        function (pair) {
          var el = $(pair[0]);
          if (el && limits[pair[1]] !== null && limits[pair[1]] !== undefined) {
            el.value = String(limits[pair[1]]);
          }
        });
    }
    var rtDl = $("adm-rt-info");
    if (rtDl) {
      rtDl.textContent = "";
      if (rt.available === false) {
        kvRow(rtDl, "可用性", "不可用（" + (rt.code || "pg_backend_required") + "）");
      } else {
        // 批次 E 起：IP 保护为每分钟请求数短窗口（防刷/防 DoS，env 配置只读）
        kvRow(rtDl, "Demo IP 短窗口请求速率（只读，env 配置）",
          rt.demo_ip_request_rate_per_minute + " 次 / " +
          Math.round((rt.demo_ip_request_rate_window_seconds || 60) / 60) +
          " 分钟窗口（DEMO_IP_RATE_PER_MINUTE，≤0 关闭；非消费额度）");
      }
    }
  }

  function saveRegistrationMode() {
    var regSelect = $("adm-regmode-select");
    var mode = regSelect ? regSelect.value : "";
    if (!mode) return;
    setStatus("adm-regmode-status", "保存中…");
    request("admin.settings.update", { registration_mode: mode })
      .then(function (res) {
        settingsUpdateResult(res, "adm-regmode-status",
          "注册模式已提交为 " + mode);
        loadSettingsPage();
      }).catch(function (err) {
        showError(err && err.code, err && err.message);
        setStatus("adm-regmode-status", errText(err));
      });
  }

  function saveSpendPolicies() {
    var spend = (state.settingsSnapshot || {}).spend || {};
    if (spend.available === false) {
      setStatus("adm-spend-status", "金额策略要求 PostgreSQL 后端");
      return;
    }
    var payload = {};
    try {
      SPEND_POLICY_FIELDS.forEach(function (pair) {
        var el = $(pair[0]);
        var text = el ? String(el.value || "").trim() : "";
        if (!text) return;
        // 未修改的字段沿用现值（与加载回显一致时不重复提交）
        var loaded = el ? String(el.getAttribute("data-loaded") || "") : "";
        if (text === loaded) return;
        var limit = cnyToNano(text);
        if (limit === null) {
          throw { message: pair[1] + " 金额非法（CNY，最多 9 位小数）" };
        }
        var policy = (spend.policies || {})[pair[1]];
        if (!policy || !policy.policy_id) {
          throw { message: pair[1] + " 无有效策略，无法更新额度" };
        }
        payload[pair[2]] = {
          policy_id: policy.policy_id,
          version: policy.version,
          limit_nano_cny: limit,
        };
      });
    } catch (e) {
      setStatus("adm-spend-status", (e && e.message) || "金额输入非法");
      return;
    }
    var modeSelect = $("adm-spend-mode");
    var newMode = modeSelect ? modeSelect.value : "";
    if (newMode && newMode !== spend.enforcement_mode) {
      payload.spend_enforcement_mode = newMode;
      payload.expected_enforcement_mode = spend.enforcement_mode;
    }
    if (!Object.keys(payload).length) {
      setStatus("adm-spend-status", "无变更可保存");
      return;
    }
    setStatus("adm-spend-status", "保存中…");
    var doSave = function () {
      request("admin.settings.update", payload).then(function (res) {
        settingsUpdateResult(res, "adm-spend-status", "金额策略已提交");
        loadSettingsPage();
      }).catch(function (err) {
        showError(err && err.code, err && err.message);
        setStatus("adm-spend-status", errText(err));
      });
    };
    if (payload.spend_enforcement_mode &&
        payload.spend_enforcement_mode !== "shadow") {
      askConfirm($("adm-spend-confirm"),
        "确认把金额 enforcement 模式从 " + spend.enforcement_mode +
        " 切换为 " + payload.spend_enforcement_mode +
        "？该操作开启金额硬拒绝（余额不足时 provider 调用被拒），" +
        "只应在批次 C2 验收后执行；切换写审计，可切回 shadow。",
        doSave);
      return;
    }
    doSave();
  }

  function saveRuntimeLimits() {
    var rt = (state.settingsSnapshot || {}).runtime || {};
    if (rt.available === false) {
      setStatus("adm-rt-status", "运行时参数要求 PostgreSQL 后端");
      return;
    }
    var payload = {};
    try {
      [["adm-rt-psteps", "platform_task_max_steps"],
       ["adm-rt-demosteps", "demo_task_max_steps"],
       ["adm-rt-ownsteps", "own_task_max_steps_limit"],
       ["adm-rt-concurrency", "demo_max_concurrency"]].forEach(
        function (pair) {
          var el = $(pair[0]);
          var raw = el ? String(el.value || "").trim() : "";
          if (!raw) return;
          var v = Number(raw);
          if (!Number.isSafeInteger(v) || v < 1 || v > 1000000) {
            throw { message: pair[1] + " 需为 1–1000000 整数" };
          }
          payload[pair[1]] = v;
        });
    } catch (e) {
      setStatus("adm-rt-status", (e && e.message) || "参数输入非法");
      return;
    }
    var demoEnabled = $("adm-rt-demo-enabled");
    if (demoEnabled) payload.demo_enabled = !!demoEnabled.checked;
    setStatus("adm-rt-status", "保存中…");
    request("admin.settings.update", payload).then(function (res) {
      settingsUpdateResult(res, "adm-rt-status", "运行时参数已提交");
      loadSettingsPage();
    }).catch(function (err) {
      showError(err && err.code, err && err.message);
      setStatus("adm-rt-status", errText(err));
    });
  }

  // 「调整当前窗口」主体列表：demo 周 + owner 月 + 各用户月窗口
  function buildWindowSubjects(settings, users) {
    var subjects = [];
    var spend = settings.spend || {};
    var wins = spend.current_windows || {};
    if (wins.demo && !wins.demo.error) {
      subjects.push({ key: "demo", label: "Demo（全站共享周窗口）",
                      window: wins.demo });
    }
    if (wins.owner && !wins.owner.error) {
      subjects.push({ key: "owner", label: "Owner（月窗口）",
                      window: wins.owner });
    }
    users.forEach(function (u) {
      var s = u.spend;
      if (s && s.window && !s.error) {
        subjects.push({
          key: "user:" + u.user_id,
          label: (u.display_name || u.user_id) + "（月窗口）",
          window: s.window,
        });
      }
    });
    state.windowSubjects = subjects;
    var select = $("adm-window-subject");
    if (!select) return;
    select.textContent = "";
    subjects.forEach(function (s, i) {
      var opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = s.label;
      select.appendChild(opt);
    });
    renderWindowInfo();
  }

  function currentWindowSubject() {
    var select = $("adm-window-subject");
    var idx = select ? parseInt(select.value, 10) : 0;
    return state.windowSubjects[idx] || state.windowSubjects[0] || null;
  }

  function renderWindowInfo() {
    var dl = $("adm-window-info");
    if (!dl) return;
    dl.textContent = "";
    var subject = currentWindowSubject();
    if (!subject) {
      kvRow(dl, "主体", "无可用窗口（策略缺失或后端不可用）");
      return;
    }
    var w = subject.window;
    kvRow(dl, "主体", subject.label);
    kvRow(dl, "当前额度（limit snapshot）", fmtNano(w.limit_nano_snapshot));
    kvRow(dl, "已消费（spent）", fmtNano(w.spent_nano_cny));
    kvRow(dl, "预占（reserved）", fmtNano(w.reserved_nano_cny));
    kvRow(dl, "剩余（remaining）", remainingText(w.remaining_nano));
    kvRow(dl, "窗口边界",
          fmtTs(w.window_start) + " → " + fmtTs(w.window_end));
    kvRow(dl, "窗口 version（CAS）", String(w.version));
    kvRow(dl, "window_id", w.window_id);
  }

  function adjustCurrentWindow() {
    var subject = currentWindowSubject();
    if (!subject || !subject.window) {
      setStatus("adm-window-status", "无可用窗口可调整");
      return;
    }
    var w = subject.window;
    var text = ($("adm-window-newlimit") &&
                $("adm-window-newlimit").value || "").trim();
    var limit = text ? cnyToNano(text) : null;
    if (limit === null) {
      setStatus("adm-window-status",
                "新额度非法（CNY，最多 9 位小数，如 30.5）");
      return;
    }
    var newRemaining = (BigInt(limit) - BigInt(w.spent_nano_cny) -
                        BigInt(w.reserved_nano_cny)).toString();
    askConfirm($("adm-window-confirm"),
      "确认调整 " + subject.label + " 的当前窗口额度？" +
      "当前额度 " + nanoToCnyString(w.limit_nano_snapshot) + " CNY → 新额度 " +
      nanoToCnyString(limit) + " CNY。影响：已消费 " +
      nanoToCnyString(w.spent_nano_cny) + " / 预占 " +
      nanoToCnyString(w.reserved_nano_cny) + " 不回退；新剩余 " +
      remainingText(newRemaining) + "。操作立即生效并写审计，不等下个窗口。",
      function () {
        setStatus("adm-window-status", "调整中…");
        request("admin.spend.currentWindow.adjust", {
          window_id: w.window_id,
          limit_nano_snapshot: limit,
          version: w.version,
        }).then(function (res) {
          setStatus("adm-window-status",
            "已调整（窗口 version " +
            ((res && res.window && res.window.version) || "?") + "）");
          if ($("adm-window-newlimit")) $("adm-window-newlimit").value = "";
          loadSettingsPage();
        }).catch(function (err) {
          if (err && err.code === "version_conflict") {
            setStatus("adm-window-status",
              "窗口已被他人调整（409 version_conflict），已刷新，请重选后重试");
            loadSettingsPage();
          } else {
            setStatus("adm-window-status", errText(err));
          }
          showError(err && err.code, err && err.message);
        });
      });
  }

  // ------------------------------------------------------------------
  // 额度与账单（§10.4 只读部分）
  // ------------------------------------------------------------------
  function loadBillingPage() {
    setPageState("billing", "loading");
    var seq = state.listSeq;
    Promise.all([
      new Promise(function (res, rej) {
        request("admin.turnBudgets.get", {}).then(
          function (r) { res(r); }, function (e) { rej(e); });
      }),
      new Promise(function (res, rej) {
        request("admin.billing.usage.list", { limit: 1 }).then(
          function (r) { res(r); }, function (e) { rej(e); });
      }),
    ]).then(function () {
      if (seq !== state.listSeq) return;
      setPageState("billing", "ready", {
        message: "已更新（" + new Date().toISOString().replace("T", " ").slice(0, 19) + "Z）",
      });
    }, function (err) {
      if (seq !== state.listSeq) return;
      setPageState("billing", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadBillingPage(); },
      });
    });
    loadTurnBudgetCard();
    loadLegacyPricingCard();
    loadProviderBalanceCard();
    loadUsage(false);
    loadUnpriced(false);
    loadLedger(false);
  }

  // 批次 A 遗留口径（§7.2）：cutover 前旧错误价格的影子数据单独标记展示，
  // 不与修复后的金额混排；legacy_priced_events=0 时隐藏整卡。
  function loadLegacyPricingCard() {
    request("admin.overview.get", {}).then(function (ov) {
      var b = ov && ov.billing;
      var card = $("adm-legacy-card");
      var dl = $("adm-legacy-info");
      if (!card || !dl) return;
      var legacyCount = b ? Number(b.legacy_priced_events || 0) : 0;
      if (!b || b.available === false || legacyCount === 0) {
        card.hidden = true;
        return;
      }
      card.hidden = false;
      dl.textContent = "";
      kvRow(dl, "legacy 计价事件数（cutover 前）", String(legacyCount));
      kvRow(dl, "价格 cutover 时间",
        b.pricing_cutover_epoch ? fmtTs(b.pricing_cutover_epoch) : "—");
      kvRow(dl, "口径", b.legacy_pricing_note ||
        "legacy pricing scale invalid; excluded from hard enforcement");
    }).catch(function () {
      var card = $("adm-legacy-card");
      if (card) card.hidden = true;
    });
  }

  // 批次 F：turn 消费闸退役——本卡降级为只读 legacy 展示（GET 冻结历史）；
  // 编辑/保存/开新周期的桥方法已删除（服务端写端点 410 turn_budgets_
  // retired），仅保留只读 get。
  function loadTurnBudgetCard() {
    var dl = $("adm-billing-turn");
    request("admin.turnBudgets.get", {}).then(function (res) {
      hideError();
      if (!dl) return;
      dl.textContent = "";
      var period = res.period || {};
      var usage = res.usage || {};
      kvRow(dl, "状态", "已退役（金额预算接管）");
      kvRow(dl, "周期", "#" + fmtNum(period.id) + "（" + fmtTs(period.started_at) + " 起）");
      kvRow(dl, "平台（用/上限）",
            fmtNum(usage.platform && usage.platform.total) + " / " +
            fmtNum(usage.platform && usage.platform.limit));
      kvRow(dl, "用户共享池（用/上限）",
            fmtNum(usage.user_pool && usage.user_pool.total) + " / " +
            fmtNum(usage.user_pool && usage.user_pool.limit));
      kvRow(dl, "Demo（24h 窗口/日上限）",
            fmtNum(usage.demo && usage.demo.total) + " / " + fmtNum(usage.demo && usage.demo.limit));
      kvRow(dl, "owner 保留池（用/保留）",
            fmtNum(usage.owner && usage.owner.total) + " / " +
            fmtNum(usage.owner && usage.owner.reserved_limit));
      var per = (usage.per_user || []).length;
      kvRow(dl, "本周期有用量的用户数", fmtNum(per));
    }).catch(function (err) {
      if (dl) { dl.textContent = ""; kvRow(dl, "可用性", errText(err)); }
      else handleErr(err, null);
    });
  }

  // ------------------------------------------------------------------
  // 金额账户管理（§10.4，PR5）：caps 编辑器（版本 CAS）+ 人工调整。
  // 当前查询到的账户缓存在内存 state.account（含 version，供 CAS 提交）。
  // ------------------------------------------------------------------
  // 调用方生成幂等键（§6.5 PR5 修订：服务端不再代生成）：16 字节随机 →
  // "adj_" + 32 hex。用 crypto.getRandomValues（opaque sandbox 内可用），
  // 不依赖 randomUUID 的 secure-context 假设；熵源缺失时宁可拒绝提交也
  // 不退化为 Math.random（弱 key 会破坏「重试不重复入账」保证）。
  function newAdjustIdemKey() {
    var cryptoObj = window.crypto;
    if (!cryptoObj || typeof cryptoObj.getRandomValues !== "function") {
      return null;
    }
    var buf = new Uint8Array(16);
    cryptoObj.getRandomValues(buf);
    var hex = "";
    for (var i = 0; i < buf.length; i++) {
      hex += ("0" + buf[i].toString(16)).slice(-2);
    }
    return "adj_" + hex;
  }

  // 当前调整表单指纹：任一字段变化即视为新的逻辑提交（换新幂等键）
  function adjustFingerprint(uid, kind, amount, reason) {
    return [uid, kind, amount, reason].join("\u0000");
  }

  function loadBillingAccount() {
    var uid = ($("adm-acct-user") && $("adm-acct-user").value || "").trim();
    var dl = $("adm-acct-info");
    if (!uid) {
      setStatus("adm-caps-status", "请输入 user_id");
      return;
    }
    request("admin.billing.account.get", { user_id: uid }).then(function (res) {
      hideError();
      state.account = { user_id: uid, account: res.account || null,
                        balance_nano: res.balance_nano };
      if (!dl) return;
      dl.textContent = "";
      if (!res.account) {
        kvRow(dl, "user_id", uid);
        kvRow(dl, "账户", "尚未开户（account:null；首次 grant/topup 会自动开户）");
        var closedForm = $("adm-caps-form");
        if (closedForm) closedForm.hidden = true;
        return;
      }
      var acct = res.account;
      kvRow(dl, "user_id", uid);
      kvRow(dl, "account_id", acct.account_id);
      kvRow(dl, "状态", acct.status);
      kvRow(dl, "version", acct.version);
      kvRow(dl, "余额", fmtNano(res.balance_nano));
      kvRow(dl, "当前 soft cap",
            acct.soft_spend_cap_nano === null ? "（未设置）"
            : nanoToCnyString(acct.soft_spend_cap_nano) + " CNY");
      kvRow(dl, "当前 hard cap",
            acct.hard_spend_cap_nano === null ? "（未设置）"
            : nanoToCnyString(acct.hard_spend_cap_nano) + " CNY");
      var capsForm = $("adm-caps-form");
      if (capsForm) capsForm.hidden = false;
      var softInput = $("adm-caps-soft");
      var hardInput = $("adm-caps-hard");
      if (softInput) {
        softInput.value = acct.soft_spend_cap_nano === null
          ? "" : nanoToCnyString(acct.soft_spend_cap_nano);
      }
      if (hardInput) {
        hardInput.value = acct.hard_spend_cap_nano === null
          ? "" : nanoToCnyString(acct.hard_spend_cap_nano);
      }
    }).catch(function (err) {
      state.account = null;
      if (dl) { dl.textContent = ""; kvRow(dl, "可用性", errText(err)); }
      else handleErr(err, null);
    });
  }

  function saveCaps() {
    var acctState = state.account;
    if (!acctState || !acctState.account) {
      setStatus("adm-caps-status", "请先查询已开户用户");
      return;
    }
    var softText = ($("adm-caps-soft") && $("adm-caps-soft").value || "").trim();
    var hardText = ($("adm-caps-hard") && $("adm-caps-hard").value || "").trim();
    // 留空 = 清除该上限（null）；填值按 CNY 字符串精确换算 nano（禁 float）
    var soft = softText ? cnyToNano(softText) : null;
    var hard = hardText ? cnyToNano(hardText) : null;
    if (softText && soft === null) {
      setStatus("adm-caps-status", "soft cap 金额非法（最多 9 位小数，如 12.5）");
      return;
    }
    if (hardText && hard === null) {
      setStatus("adm-caps-status", "hard cap 金额非法（最多 9 位小数，如 12.5）");
      return;
    }
    if (soft !== null && hard !== null && BigInt(soft) > BigInt(hard)) {
      setStatus("adm-caps-status", "soft cap 不可大于 hard cap（客户端拦截；服务端同样校验）");
      return;
    }
    setStatus("adm-caps-status", "保存中…");
    request("admin.billing.account.updateCaps", {
      user_id: acctState.user_id,
      soft_cap_nano_cny: soft,
      hard_cap_nano_cny: hard,
      version: acctState.account.version,
    }).then(function (res) {
      setStatus("adm-caps-status",
        "已保存（新 version " + ((res && res.account && res.account.version) || "?") + "）");
      loadBillingAccount();
    }).catch(function (err) {
      if (err && err.code === "version_conflict") {
        setStatus("adm-caps-status",
          "数据已被他人修改（409 version_conflict），请重新查询后重填");
      } else {
        setStatus("adm-caps-status", errText(err));
      }
      showError(err && err.code, err && err.message);
    });
  }

  function submitAdjustment() {
    var uid = ($("adm-acct-user") && $("adm-acct-user").value || "").trim();
    if (!uid) { setStatus("adm-adjust-status", "请先在上方输入 user_id"); return; }
    var kind = $("adm-adjust-kind") ? $("adm-adjust-kind").value : "";
    var amountText = ($("adm-adjust-amount") && $("adm-adjust-amount").value || "").trim();
    var reason = ($("adm-adjust-reason") && $("adm-adjust-reason").value || "").trim();
    var amount = amountText ? cnyToNano(amountText) : null;
    if (amount === null) {
      setStatus("adm-adjust-status",
        "金额非法：最多 9 位小数的 CNY 数值（如 12.5；manual_adjustment 可为负）");
      return;
    }
    if (kind !== "manual_adjustment" && BigInt(amount) <= 0n) {
      setStatus("adm-adjust-status", kind + " 金额必须为正数");
      return;
    }
    if (!reason) {
      setStatus("adm-adjust-status", "原因必填（≤500 字符）");
      return;
    }
    // 幂等键生命周期（§6.5 PR5 修订，调用方生成）：
    //   - 表单指纹与上次不同（首次提交或用户改过任一字段）→ 换新 key；
    //   - 指纹相同（同一逻辑提交的失败重试）→ 复用旧 key，服务端按 duplicate
    //     幂等重放，绝不二次入账；
    //   - 成功后清空，下次提交重新生成。
    var fingerprint = adjustFingerprint(uid, kind, amount, reason);
    if (!state.adjustIdem || state.adjustFingerprint !== fingerprint) {
      var fresh = newAdjustIdemKey();
      if (!fresh) {
        setStatus("adm-adjust-status",
          "浏览器不支持 crypto.getRandomValues，无法生成幂等键（拒绝提交以防重复入账）");
        return;
      }
      state.adjustIdem = fresh;
      state.adjustFingerprint = fingerprint;
    }
    var idemKey = state.adjustIdem;
    askConfirm($("adm-adjust-confirm"),
      "确认对用户 " + uid + " 入账 " + kind + " " + amountText + " CNY（" +
      amount + " nano）？原因：" + reason,
      function () {
        setStatus("adm-adjust-status", "提交中…（失败后可直接重试：将复用同一幂等键，不会重复入账）");
        request("admin.billing.adjust", {
          user_id: uid, kind: kind,
          amount_nano_cny: amount, reason: reason,
          idempotency_key: idemKey,
        }).then(function (res) {
          // 成功：作废当前 key（下次提交是新的逻辑操作，换新 key）
          state.adjustIdem = null;
          state.adjustFingerprint = null;
          setStatus("adm-adjust-status",
            (res && res.duplicate ? "幂等重放（未重复入账）" : "已入账") +
            "：" + ((res && res.entry && res.entry.kind) || kind) + " " +
            ((res && res.entry && res.entry.amount_nano_cny) || amount) +
            " nano，余额 " + fmtNano(res && res.balance_nano));
          if ($("adm-adjust-amount")) $("adm-adjust-amount").value = "";
          if ($("adm-adjust-reason")) $("adm-adjust-reason").value = "";
          loadBillingAccount();
          loadLedger(false);
        }).catch(function (err) {
          // 失败：保留 key + 指纹 → 用户重试（表单未动）时复用同一幂等键
          setStatus("adm-adjust-status",
            errText(err) + "（重试将使用同一幂等键，不会重复入账）");
          showError(err && err.code, err && err.message);
        });
      });
  }

  function renderProviderBalance(payload) {
    var dl = $("adm-billing-provider");
    if (!dl) return;
    dl.textContent = "";
    var snap = payload.snapshot;
    kvRow(dl, "供应商", payload.provider);
    if (!snap) {
      kvRow(dl, "最新快照", "（暂无；可点下方按钮抓取）");
      return;
    }
    kvRow(dl, "总余额", fmtNano(snap.total_balance_nano));
    kvRow(dl, "赠送余额", fmtNano(snap.granted_balance_nano));
    kvRow(dl, "充值余额", fmtNano(snap.topped_up_balance_nano));
    kvRow(dl, "可用状态", snap.is_available ? "可用" : "不可用");
    kvRow(dl, "快照时间", fmtTs(snap.observed_at));
    kvRow(dl, "快照年龄",
          payload.age_seconds === null || payload.age_seconds === undefined
          ? "—" : Math.round(payload.age_seconds) + " 秒前");
  }

  function loadProviderBalanceCard() {
    request("admin.billing.providerBalance.get", {}).then(function (payload) {
      hideError();
      renderProviderBalance(payload);
    }).catch(function (err) {
      var dl = $("adm-billing-provider");
      if (dl) { dl.textContent = ""; kvRow(dl, "可用性", errText(err)); }
      else handleErr(err, null);
    });
  }

  function refreshProviderBalance() {
    var btn = $("adm-balance-refresh-btn");
    var status = $("adm-balance-refresh-status");
    if (btn) btn.disabled = true;
    if (status) status.textContent = "抓取中…";
    request("admin.billing.providerBalance.refresh", {}).then(function (res) {
      if (btn) btn.disabled = false;
      if (status) status.textContent = "已更新（" + fmtTs(res.snapshot && res.snapshot.observed_at) + "）";
      renderProviderBalance({
        provider: res.provider,
        snapshot: res.snapshot,
        age_seconds: res.age_seconds,
      });
    }).catch(function (err) {
      if (btn) btn.disabled = false;
      if (status) status.textContent = errText(err);
    });
  }

  function loadUsage(append) {
    var seq = state.listSeq;
    var f = state.filters.usage || {};
    var payload = { limit: 50, cursor: append ? state.cursors.usage : null };
    if (f.model) payload.model = f.model;
    if (f.user_id) payload.user_id = f.user_id;
    if (f.status) payload.status = f.status;
    var status = $("adm-usage-status");
    request("admin.billing.usage.list", payload).then(function (res) {
      if (seq !== state.listSeq) return;
      hideError();
      renderUsage(res.items || [], append);
      state.cursors.usage = res.next_cursor || null;
      var more = $("adm-usage-more-btn");
      if (more) more.disabled = !res.next_cursor;
      if (status) status.textContent = res.next_cursor ? "还有更多" : "已到底";
    }).catch(function (err) { if (seq === state.listSeq) handleErr(err, status); });
  }

  function renderUsage(items, append) {
    var tbody = $("adm-usage-tbody");
    if (!tbody) return;
    if (!append) tbody.textContent = "";
    items.forEach(function (e) {
      var tr = document.createElement("tr");
      tr.appendChild(td(fmtTs(e.occurred_at)));
      tr.appendChild(td(e.model));
      tr.appendChild(td(e.subject_type + (e.user_id ? "" : "（无用户镜像）")));
      tr.appendChild(td(e.status === "priced" ? "priced" : ("unpriced：" + (e.unpriced_reason || "?"))));
      tr.appendChild(td(fmtNum(e.cache_hit_input_tokens) + " / " +
                        fmtNum(e.cache_miss_input_tokens) + " / " +
                        fmtNum(e.output_tokens)));
      // unpriced 金额保持 null 呈现「—」，绝不显示成 0 元（§10.4 红线）
      tr.appendChild(td(e.provider_cost_nano_cny === null ? "—" : String(e.provider_cost_nano_cny)));
      tr.appendChild(td(e.charge_nano_cny === null ? "—" : String(e.charge_nano_cny)));
      tr.appendChild(td(e.event_id));
      tbody.appendChild(tr);
    });
  }

  function loadUnpriced(append) {
    var seq = state.listSeq;
    var payload = { limit: 50, status: "unpriced", cursor: append ? state.cursors.unpriced : null };
    request("admin.billing.usage.list", payload).then(function (res) {
      if (seq !== state.listSeq) return;
      var card = $("adm-unpriced-card");
      var tbody = $("adm-unpriced-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      (res.items || []).forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.occurred_at)));
        tr.appendChild(td(e.model));
        tr.appendChild(td(e.unpriced_reason));
        tr.appendChild(td(e.event_id));
        tbody.appendChild(tr);
      });
      state.cursors.unpriced = res.next_cursor || null;
      var more = $("adm-unpriced-more-btn");
      if (more) more.disabled = !res.next_cursor;
      if (card) card.hidden = false; // 有无条目都显示告警区（明确「当前 0 条」）
      if (!res.next_cursor && !res.items) tbody.textContent = "";
    }).catch(function () {
      // json 后端：告警区静默隐藏（概览页已给出 pg_backend_required 说明）
      var card = $("adm-unpriced-card");
      if (card) card.hidden = true;
    });
  }

  function loadLedger(append) {
    var seq = state.listSeq;
    var payload = { limit: 50, cursor: append ? state.cursors.ledger : null };
    request("admin.billing.ledger.list", payload).then(function (res) {
      if (seq !== state.listSeq) return;
      hideError();
      var tbody = $("adm-ledger-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      (res.items || []).forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.created_at)));
        tr.appendChild(td(e.account_id));
        var kindCell = td(e.kind);
        // PR6 模拟扣费条目：metadata.simulated=true → kind 旁加「模拟」徽标
        // （真实扣费落地后该徽标即自然消失；metadata 为服务端受控写入的
        // 非敏感字段，此处只读渲染）
        if (e.metadata && e.metadata.simulated === true) {
          var badge = document.createElement("span");
          badge.className = "adm-badge";
          badge.textContent = "模拟";
          kindCell.appendChild(document.createTextNode(" "));
          kindCell.appendChild(badge);
        }
        tr.appendChild(kindCell);
        tr.appendChild(td(e.amount_nano_cny));
        tr.appendChild(td(e.reason));
        tr.appendChild(td(e.entry_id));
        tbody.appendChild(tr);
      });
      state.cursors.ledger = res.next_cursor || null;
      var more = $("adm-ledger-more-btn");
      if (more) more.disabled = !res.next_cursor;
    }).catch(function (err) { if (seq === state.listSeq) handleErr(err, null); });
  }

  // ------------------------------------------------------------------
  // 审计（§10.5）
  // ------------------------------------------------------------------
  function loadAudit(append) {
    var seq = state.listSeq;
    var f = state.filters.audit || {};
    var payload = { limit: 50, cursor: append ? state.cursors.audit : null };
    if (f.action) payload.action = f.action;
    var status = $("adm-audit-status");
    setPageState("audit", "loading");
    request("admin.audit.list", payload).then(function (res) {
      if (seq !== state.listSeq) return;
      hideError();
      var tbody = $("adm-audit-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      var auditItems = res.items || [];
      if (!append && !auditItems.length) {
        setPageState("audit", "empty", {
          message: state.filters.audit && state.filters.audit.action
            ? "没有匹配该 action 的审计记录；清空筛选后查看全部。"
            : "暂无审计记录。管理操作发生后会在此留痕。",
        });
      } else {
        setPageState("audit", "ready", {
          message: "已更新（" + new Date().toISOString().replace("T", " ").slice(0, 19) + "Z）",
        });
      }
      auditItems.forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.ts)));
        tr.appendChild(td((e.actor_role || "") + (e.actor_user_id ? ("·" + e.actor_user_id) : "")));
        tr.appendChild(td(e.action));
        tr.appendChild(td((e.target_type || "") + (e.target_id ? ("·" + e.target_id) : "")));
        tr.appendChild(td(JSON.stringify(e.detail || {})));
        tbody.appendChild(tr);
      });
      state.cursors.audit = res.next_cursor || null;
      var more = $("adm-audit-more-btn");
      if (more) more.disabled = !res.next_cursor;
      if (status) status.textContent = res.next_cursor ? "还有更多" : "已到底";
    }).catch(function (err) {
      if (seq !== state.listSeq) return;
      handleErr(err, status);
      setPageState("audit", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadAudit(false); },
      });
    });
  }

  // ------------------------------------------------------------------
  // 插件管理（PR5 修订：恢复旧侧栏插件管理功能面）。运行时安装/更新不上
  // UI——§16 的发布方向是版本化 releases + 原子切换（宿主机动作），本页只
  // 做：列表 + sidecar 健康提示 + 启停 + 凭证轮换（secret 仅一次展示）。
  // ------------------------------------------------------------------
  function pluginHealthText(h) {
    if (h === "reachable") return "可达";
    if (h === "unreachable") return "不可达（仅影响依赖 sidecar 的能力）";
    return "未知（未配置探测地址）";
  }

  function loadPlugins() {
    var status = $("adm-plugins-status");
    setPageState("plugins", "loading");
    request("admin.plugins.list", {}).then(function (res) {
      hideError();
      setPageState("plugins", "ready", {
        message: "已更新（" + new Date().toISOString().replace("T", " ").slice(0, 19) + "Z）",
      });
      var tbody = $("adm-plugins-tbody");
      if (!tbody) return;
      tbody.textContent = "";
      ((res && res.installations) || []).forEach(function (inst) {
        var tr = document.createElement("tr");
        tr.appendChild(td(inst.installation_id));
        tr.appendChild(td(inst.plugin_id));
        tr.appendChild(td(inst.version || "—"));
        tr.appendChild(td(inst.enabled ? "启用" : "停用"));
        tr.appendChild(td(pluginHealthText(inst.health)));
        var caps = inst.capabilities || [];
        tr.appendChild(td(caps.length
          ? caps.map(function (c) { return c && c.name; }).join(", ")
          : "（纯 UI 插件）"));
        var cell = document.createElement("td");
        cell.className = "adm-actions-cell";
        var enabled = !!inst.enabled;
        cell.appendChild(actionBtn(enabled ? "停用" : "启用", function () {
          if (enabled) {
            askConfirm($("adm-plugins-confirm"),
              "确认停用 " + inst.plugin_id + "？该安装全部在途 JWT 立即失效，" +
              "依赖其能力的调用会被拒绝。",
              function () { setPluginEnabled(inst, false); });
          } else {
            setPluginEnabled(inst, true);
          }
        }, enabled));
        cell.appendChild(actionBtn("轮换凭证", function () {
          askConfirm($("adm-plugins-confirm"),
            "确认轮换 " + inst.plugin_id + " 的安装凭证？旧 secret 立即失效，" +
            "使用方必须同步更新；新明文只显示一次。",
            function () { rotatePluginSecret(inst); });
        }, true));
        tr.appendChild(cell);
        tbody.appendChild(tr);
      });
      if (status) status.textContent = "";
    }).catch(function (err) {
      handleErr(err, status);
      setPageState("plugins", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadPlugins(); },
      });
    });
  }

  function setPluginEnabled(inst, enabled) {
    request("admin.plugins.setEnabled",
            { installation_id: inst.installation_id, enabled: enabled })
      .then(function () {
        setStatus("adm-plugins-status",
          (enabled ? "已启用 " : "已停用 ") + inst.plugin_id);
        loadPlugins();
      })
      .catch(function (err) { handleErr(err, $("adm-plugins-status")); });
  }

  // 轮换凭证：响应里的新明文 secret 只在此处展示一次（内存 DOM），不落
  // localStorage/cookie（opaque origin 本也写不了）——与邀请码同款一次性语义
  function rotatePluginSecret(inst) {
    request("admin.plugins.rotateSecret",
            { installation_id: inst.installation_id })
      .then(function (res) {
        setStatus("adm-plugins-status",
          "已轮换 " + inst.plugin_id + " 的凭证（旧 secret 已失效）：");
        var box = $("adm-plugin-secret-box");
        var code = $("adm-plugin-secret");
        if (box && code) {
          code.textContent = (res && res.secret) || "";
          box.hidden = false;
        }
        loadPlugins();
      })
      .catch(function (err) { handleErr(err, $("adm-plugins-status")); });
  }

  // ------------------------------------------------------------------
  // 内存态导航（opaque origin：不用 location.hash，避免任何存储型状态）
  // ------------------------------------------------------------------
  function showPage(name) {
    state.page = name;
    state.listSeq++; // 作废在途列表响应（§8.2：晚到响应不写回新页面）
    closeUserDrawer();
    if (els.pageTitle) els.pageTitle.textContent = PAGE_TITLES[name] || name;
    // 手机端：切页后收起导航抽屉
    if (els.nav && els.nav.classList && els.nav.classList.remove) {
      els.nav.classList.remove("adm-nav--open");
    }
    if (els.navToggle && els.navToggle.setAttribute) {
      els.navToggle.setAttribute("aria-expanded", "false");
    }
    Object.keys(els.pages).forEach(function (key) {
      if (els.pages[key]) els.pages[key].hidden = key !== name;
    });
    if (els.nav && els.nav.querySelectorAll) {
      var buttons = els.nav.querySelectorAll(".adm-nav-btn");
      for (var i = 0; i < buttons.length; i++) {
        var on = buttons[i].getAttribute("data-page") === name;
        buttons[i].className = on ? "adm-nav-btn adm-nav-btn--active" : "adm-nav-btn";
        if (buttons[i].setAttribute) {
          buttons[i].setAttribute("aria-current", on ? "page" : "false");
        }
      }
    }
    hideError();
    // §8.2（包 D）：未 ready（未握手/已作废）只显示等待态，不发管理 API 请求，
    // 也不渲染成错误——握手建立后 init 处理器会重新 showPage 加载真实数据
    if (!bridgeReady()) {
      setHandshake(state.dead
        ? "桥接会话已作废，等待宿主重新握手…（当前页：" + name + "）"
        : "等待宿主初始化消息…（当前页：" + name + "）");
      return;
    }
    if (name === "overview") loadOverview();
    else if (name === "users") loadUsers(false);
    else if (name === "invites") loadAcquisitionPage();
    else if (name === "settings") loadSettingsPage();
    else if (name === "billing") loadBillingPage();
    else if (name === "plugins") loadPlugins();
    else if (name === "audit") loadAudit(false);
  }

  function bindNav() {
    if (els.nav && els.nav.addEventListener) {
      els.nav.addEventListener("click", function (ev) {
        var btn = ev.target && ev.target.closest ? ev.target.closest(".adm-nav-btn") : null;
        if (!btn) return;
        var name = btn.getAttribute("data-page");
        if (name) showPage(name);
      });
    }
    // 手机端导航抽屉（§9.3：键盘可达，aria-expanded 表达开合）
    if (els.navToggle && els.navToggle.addEventListener) {
      els.navToggle.addEventListener("click", function () {
        var open = !!(els.nav && els.nav.classList &&
                     els.nav.classList.contains &&
                     els.nav.classList.contains("adm-nav--open"));
        if (els.nav && els.nav.classList) {
          if (open) els.nav.classList.remove("adm-nav--open");
          else els.nav.classList.add("adm-nav--open");
        }
        els.navToggle.setAttribute("aria-expanded", open ? "false" : "true");
      });
    }
    // 用户详情抽屉：关闭按钮 / 遮罩 / Esc（键盘可达，§9.3）
    if (els.drawerClose && els.drawerClose.addEventListener) {
      els.drawerClose.addEventListener("click", closeUserDrawer);
    }
    if (els.drawerMask && els.drawerMask.addEventListener) {
      els.drawerMask.addEventListener("click", closeUserDrawer);
    }
    document.addEventListener("keydown", function (ev) {
      if ((ev.key === "Escape" || ev.keyCode === 27) &&
          els.drawer && !els.drawer.hidden) {
        closeUserDrawer();
      }
    });
    function onClick(id, handler) {
      var el = $(id);
      if (el) el.addEventListener("click", handler);
    }
    // 用户页筛选
    onClick("adm-users-search-btn", function () {
      state.filters.users = {
        q: ($("adm-users-q") && $("adm-users-q").value || "").trim(),
        enabled: $("adm-users-enabled") ? $("adm-users-enabled").value : "",
        ai: $("adm-users-ai") ? $("adm-users-ai").value : "",
      };
      state.cursors.users = null;
      state.listSeq++;
      loadUsers(false);
    });
    onClick("adm-users-more-btn", function () { loadUsers(true); });
    // 用户页写操作（PR5）
    onClick("adm-users-create-btn", submitCreateUser);
    // 邀请与来源页
    onClick("adm-acq-more-btn", function () { loadAcqUsers(true); });
    // 邀请写操作（PR5）
    onClick("adm-invite-create-btn", submitCreateInvite);
    onClick("adm-invites-more-btn", function () { loadInvites(true); });
    onClick("adm-invite-token-copy", function () {
      var code = $("adm-invite-token");
      if (code) copyToClipboard(code.textContent || "");
    });
    // 账单页
    onClick("adm-balance-refresh-btn", refreshProviderBalance);
    // 设置页（批次 D）
    onClick("adm-regmode-save-btn", saveRegistrationMode);
    onClick("adm-spend-save-btn", saveSpendPolicies);
    onClick("adm-rt-save-btn", saveRuntimeLimits);
    onClick("adm-window-adjust-btn", adjustCurrentWindow);
    var windowSubjectSelect = $("adm-window-subject");
    if (windowSubjectSelect && windowSubjectSelect.addEventListener) {
      windowSubjectSelect.addEventListener("change", renderWindowInfo);
    }
    // turn 预算 / 金额账户写操作（PR5）
    onClick("adm-acct-load-btn", loadBillingAccount);
    onClick("adm-caps-save-btn", saveCaps);
    onClick("adm-adjust-btn", submitAdjustment);
    onClick("adm-usage-search-btn", function () {
      state.filters.usage = {
        model: ($("adm-usage-model") && $("adm-usage-model").value || "").trim(),
        user_id: ($("adm-usage-user") && $("adm-usage-user").value || "").trim(),
        status: $("adm-usage-status") ? $("adm-usage-status").value : "",
      };
      state.cursors.usage = null;
      state.listSeq++;
      loadUsage(false);
    });
    onClick("adm-usage-more-btn", function () { loadUsage(true); });
    onClick("adm-unpriced-more-btn", function () { loadUnpriced(true); });
    onClick("adm-ledger-more-btn", function () { loadLedger(true); });
    // 审计页
    onClick("adm-audit-search-btn", function () {
      state.filters.audit = {
        action: ($("adm-audit-action") && $("adm-audit-action").value || "").trim(),
      };
      state.cursors.audit = null;
      state.listSeq++;
      loadAudit(false);
    });
    onClick("adm-audit-more-btn", function () { loadAudit(true); });
    // 插件页（PR5 修订）
    onClick("adm-plugins-refresh-btn", function () { loadPlugins(); });
    onClick("adm-plugin-secret-copy", function () {
      var code = $("adm-plugin-secret");
      if (code) copyToClipboard(code.textContent || "");
    });
  }

  window.addEventListener("message", onMessage);
  bindNav();

  // 导出（仅调试/测试用；不含 nonce 读取器）。金额换算函数一并导出供
  // tests/js/admin-plugin-ui.test.ts 锁定「字符串进、字符串出」契约。
  window.PathTogetherAdminClient = {
    request: request,
    showPage: showPage,
    cnyToNano: cnyToNano,
    nanoToCnyString: nanoToCnyString,
    fmtNano: fmtNano,
    handshakeState: function () {
      return {
        ready: !!state.nonce && !state.dead,
        protocolVersion: state.protocolVersion,
        grantedCount: state.granted.length,
      };
    },
  };
})();
