/* =========================================================================
   pathtogether-admin 桥接客户端 + 业务页面（PR3b 只读 + PR5 写操作 +
   2026-09-03 wave 2 收敛，review-2026-09-02-upload-user-limits-admin-ui-cleanup.md
   §4 / Batch C5-6 / D1 / D2-3）

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
       筛选/分页游标）只在内存；
     - sandbox 无 allow-modals：window.confirm/prompt 被静默吞掉，危险操作
       二次确认一律用页内确认条（§3.3）。表单校验只是体验层，权威校验在服务端。

   wave 2 页面契约（r3-wave2 单轨收口）：
     - 注册 user 恒为一次性总额度（spend.total；spend_target 恒
       "total_allowance"，纯展示标注），owner 恒为月窗口（spend.window）；
       两种形态同时出现 = 契约错误（显式报错，不任选其一）；
     - user 抽屉唯一金额动作 = 设置总额度 / 恢复默认（CAS，绝不重置已用）；
       owner = 既有 currentWindow.adjust，不出现 total 动作；
     - turn 冻结历史 / 人工调整 / caps / 历史影子 / 来源归因入口全部删除；
     - 费用页 = KPI + [仅异常]告警条 + Demo 消耗卡 + 三页内标签明细
       （只有当前标签发请求；切换时代际递增，迟到旧响应一律丢弃）；
     - 概览下半部「站点访问」匿名统计卡：siteStats 桥不可达（D2 未发布）时
       整卡隐藏，绝不显示为错误；
     - 面向人的 CNY 统一两位小数（formatCny2：十进制字符串/BigInt 半分进位，
       不经 JS Number）；原始 nano 只在技术详情/原始值展开区。
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
               audit: null, invites: null, slides: null },
    filters: { users: {}, usage: {}, audit: {} },
    // 设置页快照（批次 D §6.1）：admin.settings.get 的响应（含 spend
    // current_windows 的 demo/owner 窗口 CAS version）——仅内存。
    settingsSnapshot: null,
    // 费用页数据快照（KPI/告警条聚合用；仅内存）
    billOverview: null,
    billProviderBalance: null,
    billProviderBalanceError: null,
    billDemo: null,
  };

  // 深链起始页（PR5 /admin#invites 兼容）：宿主把父页 hash 透传到本 iframe
  // 自身 URL；只接受已知页面 slug，其余回概览。
  function initialPageFromHash() {
    var pages = ["overview", "users", "slides", "invites", "settings",
                 "billing", "plugins", "audit"];
    var hash = "";
    try { hash = window.location.hash || ""; } catch (e) { hash = ""; }
    var name = hash.replace(/^#/, "");
    return pages.indexOf(name) !== -1 ? name : "overview";
  }
  var initialPage = initialPageFromHash();

  function $(id) { return document.getElementById(id); }

  // wave 2：顶级页名收敛（「邀请与来源」→「邀请」、「额度与账单」→「费用」）；
  // slug 保持不变，宿主深链 #invites/#billing 兼容。
  var PAGE_TITLES = {
    overview: "概览", users: "用户", slides: "切片可见性", invites: "邀请",
    settings: "设置", billing: "费用", plugins: "插件", audit: "审计",
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
      slides: $("adm-page-slides"),
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

  // §4.9：健康握手缩成紧凑「绿色状态点 + 已连接」；protocolVersion 与权限
  // 数量放进可展开的握手详情（屏幕阅读器与桌面 tooltip 都可及）。异常、
  // 等待与重新握手一律走 setHandshake 恢复完整文字。
  function setHandshakeConnected() {
    if (!els.handshake) return;
    els.handshake.textContent = "";
    var dot = document.createElement("span");
    dot.className = "adm-status-dot adm-status-dot--ok";
    dot.setAttribute("aria-hidden", "true");
    els.handshake.appendChild(dot);
    els.handshake.appendChild(document.createTextNode("已连接"));
    var details = document.createElement("details");
    details.className = "adm-handshake-detail";
    var summary = document.createElement("summary");
    summary.textContent = "握手详情";
    var body = document.createElement("span");
    body.textContent = "protocolVersion=" + state.protocolVersion +
      "，管理能力 " + state.granted.length + " 项";
    details.appendChild(summary);
    details.appendChild(body);
    els.handshake.appendChild(details);
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
  // §4.8：绝对时间统一按 Asia/Shanghai 显示并明确 GMT+8，不再以 ISO UTC 为主
  // 显示（epoch 秒是安全整数，Number 转换只发生在时间轴上，与金额无关）。
  var SHANGHAI_FMT = null;
  function shanghaiParts(d) {
    if (!SHANGHAI_FMT) {
      SHANGHAI_FMT = new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit",
        day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
        hourCycle: "h23",
      });
    }
    var out = {};
    SHANGHAI_FMT.formatToParts(d).forEach(function (p) { out[p.type] = p.value; });
    return out;
  }

  function fmtTs(epoch) {
    if (epoch === null || epoch === undefined) return "—";
    try {
      // 服务端时间主口径是 epoch 秒；个别字段（如 spend policy
      // effective_from）经 Flask JSON 序列化成 HTTP 日期字符串——数字按
      // 秒解释，可解析的日期字符串直接交给 Date，其余显式回显原值不吞错
      var asNum = Number(epoch);
      var d = Number.isFinite(asNum) ? new Date(asNum * 1000) : new Date(epoch);
      var p = shanghaiParts(d);
      return p.year + "-" + p.month + "-" + p.day + " " +
        p.hour + ":" + p.minute + ":" + p.second + " GMT+8";
    } catch (e) { return String(epoch); }
  }

  // 「已更新（…）」等当前时刻文案：同一上海格式（单元测试用固定 epoch 锁定）
  function nowText() {
    return fmtTs(Math.floor(Date.now() / 1000));
  }

  function fmtNum(v) {
    if (v === null || v === undefined) return "—";
    if (typeof v !== "number") return String(v);
    return v.toLocaleString("en-US");
  }

  function fmtRatio(v) {
    if (v === null || v === undefined) return "—";
    return (Math.round(v * 1000) / 10) + "%";
  }

  // ------------------------------------------------------------------
  // 金额格式化（§4.7 wave 2 统一口径）
  //   - formatCny2(nano)：十进制字符串/BigInt → 恰好两位小数的 CNY 字符串；
  //     「半分进位、绝对值方向舍入」（half away from zero），全程不经
  //     JS Number/toFixed——wire 金额是十进制字符串（§5 v0.3），>2^53 的
  //     值经 Number 会静默失真。非法/空值返回 null（调用方回显原值）。
  //     验收锚点：17806450800→"17.81"、17804000000→"17.80"、
  //     -1235000000→"-1.24"。
  //   - fmtCny(v)：所有面向人的 CNY 展示唯一出口（计费策略/总额度/已用/
  //     预占/剩余/Demo 统计/供应商余额/usage/ledger）= formatCny2 + " CNY"；
  //     null/缺失 →「—」；非法值显式回显原值（不吞错、绝不伪造 0）。
  //     「耗尽/超额/告警色」仍按原始 nano 判断（见 remainingInfo 等）。
  //   - fmtNano/nanoToCnyString：保留给技术详情（原始值展开区）与输入框
  //     回显，不再用于常规展示。
  // ------------------------------------------------------------------
  function formatCny2(v) {
    if (v === null || v === undefined || v === "") return null;
    var b;
    try { b = BigInt(v); } catch (e) { return null; }
    var neg = b < 0n;
    if (neg) b = -b;
    // 1 分 = 1e7 nano；+5e6 后整除 = 半分进位（away from zero）
    var cents = (b + 5000000n) / 10000000n;
    var whole = cents / 100n;
    var frac = (cents % 100n).toString().padStart(2, "0");
    return (neg && cents !== 0n ? "-" : "") + whole.toString() + "." + frac;
  }

  function fmtCny(v) {
    if (v === null || v === undefined || v === "") return "—";
    var s = formatCny2(v);
    return s === null ? String(v) : s + " CNY";
  }

  function cnyFromNano(v) {
    if (v === null || v === undefined) return null;
    try {
      var b = BigInt(v);
      var neg = b < 0n;
      if (neg) b = -b;
      var whole = (b / 1000000000n).toString();
      var frac = (b % 1000000000n).toString()
        .padStart(9, "0").replace(/0+$/, "");
      return (neg ? "-" : "") + whole + (frac ? "." + frac : "");
    } catch (e) {
      return null;
    }
  }

  function fmtNano(v) {
    if (v === null || v === undefined) return "—";
    var cny = cnyFromNano(v);
    if (cny === null) return String(v);
    return cny + " CNY（" + String(v) + " nano）";
  }

  function kvRow(dl, key, value) {
    var dt = document.createElement("dt");
    dt.textContent = key;
    var dd = document.createElement("dd");
    dd.textContent = value === null || value === undefined ? "—" : String(value);
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  // 值为已构建节点（如窗口边界 span、堆叠条）的 kv 行
  function kvRowNode(dl, key, node) {
    var dt = document.createElement("dt");
    dt.textContent = key;
    var dd = document.createElement("dd");
    dd.appendChild(node);
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  function td(text, cls) {
    var cell = document.createElement("td");
    if (cls) cell.className = cls;
    cell.textContent = text === null || text === undefined ? "—" : String(text);
    return cell;
  }

  // ---- CNY ↔ nano 换算（§5：1 CNY = 1e9 nano；全程字符串/BigInt，禁 float）----
  // "12.345678901" → "12345678901"；小数最多 9 位；允许负号。
  // 返回**字符串** nano（§5 v0.3：wire 金额是十进制字符串）。非法形态 null。
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

  // 精确 nano → CNY 字符串（输入框回显/技术详情用；BigInt 运算，不经
  // Number；输出长尾小数——只允许出现在编辑回显与原始值区，不做常规展示）
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
    cancel.className = "adm-btn-secondary";
    cancel.textContent = "取消";
    ok.addEventListener("click", function () {
      clearConfirm(box);
      onConfirm();
    });
    cancel.addEventListener("click", function () { clearConfirm(box); });
    box.appendChild(msg);
    box.appendChild(ok);
    box.appendChild(cancel);
    // 确认条出现时焦点移到确认按钮（新交互内容可达；抽屉内即在 Tab 圈定内）
    if (ok.focus) ok.focus();
  }

  function setStatus(id, text) {
    var el = $(id);
    if (el) el.textContent = text || "";
  }

  // 分页提示与操作反馈共用同一 status 元素时的竞态防护：操作成功文案不得
  // 被紧随的列表刷新覆盖——仅当元素为空或当前就是分页提示时才写分页提示。
  function setPageHint(el, text) {
    if (!el) return;
    var cur = el.textContent || "";
    if (cur && cur !== "还有更多" && cur !== "已到底") return;
    el.textContent = text;
  }

  // §4.5 按钮语义：primary（组内主提交）/ secondary（普通操作默认）/
  // danger（实心危险，每区域最多一个）/ danger-outline（次要危险）。
  function actionBtn(label, handler, variant) {
    var btn = document.createElement("button");
    btn.type = "button";
    var v = variant === true ? "danger"
      : (typeof variant === "string" ? variant : "secondary");
    btn.className = "adm-btn-" + v;
    btn.textContent = label;
    btn.addEventListener("click", handler);
    return btn;
  }

  // §4.1/§4.4：校验失败保留输入与焦点；aria-invalid 表达校验态（CSS 红边）
  function markInvalid(el, message, statusId) {
    if (el) {
      el.setAttribute("aria-invalid", "true");
      if (el.focus) el.focus();
    }
    setStatus(statusId, message);
  }

  function clearInvalid(el) {
    if (el && el.removeAttribute) el.removeAttribute("aria-invalid");
  }

  // §4.2：原始 nano 的可展开运维区（details.adm-raw-values）——主视图只显示
  // CNY，原始 wire 十进制字符串在这里按字段可复制查看，精度永不丢失。
  function rawValuesDetails(rows) {
    var details = document.createElement("details");
    details.className = "adm-raw-values";
    var summary = document.createElement("summary");
    summary.textContent = "原始 nano 值（wire 十进制字符串）";
    details.appendChild(summary);
    rows.forEach(function (row) {
      if (row[1] === null || row[1] === undefined || row[1] === "") return;
      var line = document.createElement("div");
      line.className = "adm-raw-values-row";
      var key = document.createElement("span");
      key.className = "adm-raw-values-key";
      key.textContent = row[0];
      var code = document.createElement("code");
      code.textContent = String(row[1]);
      var copy = actionBtn("复制", function () {
        copyToClipboard(String(row[1]));
      }, "secondary");
      line.appendChild(key);
      line.appendChild(code);
      line.appendChild(copy);
      details.appendChild(line);
    });
    return details;
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

  // 常数时间字符串比较（与宿主 admin-host.js 同款）
  function timingSafeEqual(a, b) {
    if (typeof a !== "string" || typeof b !== "string") return false;
    if (a.length !== b.length) return false;
    var diff = 0;
    for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
    return diff === 0;
  }

  function onMessage(event) {
    // 来源认证第一道（§8.3 P2 修订，与宿主对称）：只接受宿主 window.parent。
    if (event.source !== window.parent) return;
    var env = event.data;
    if (!env || typeof env !== "object" || env.bridge !== BRIDGE) return;

    if (env.kind === "init") {
      state.nonce = typeof env.nonce === "string" ? env.nonce : null;
      state.protocolVersion = env.protocolVersion || PROTOCOL_FALLBACK;
      state.granted = Array.isArray(env.adminPermissions) ? env.adminPermissions.slice() : [];
      state.dead = false;
      setHandshakeConnected();
      resetLists();
      // 深链（/admin#invites 等旧入口 302）：握手后直接落到目标页
      showPage(initialPage);
      return;
    }

    if (env.kind === "response") {
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
  // 概览（§4.2 wave 2）：首屏只保留用户/AI、user 累计已用、今日/近 7 天
  // 调用、cache hit、供应商余额+快照年龄；告警条仅在 unpriced>0、供应商余额
  // 不可用/过旧、reconcile 不一致时出现。turn 冻结历史卡已删除。
  // ------------------------------------------------------------------
  function loadOverview() {
    setPageState("overview", "loading");
    // §4.9：当前身份整卡收成顶栏下方的一行次要信息
    var actorLine = $("adm-actor-line");
    request("admin.auth.get", {}).then(function (identity) {
      if (!actorLine) return;
      actorLine.hidden = false;
      actorLine.textContent = "当前身份：" + ((identity && identity.role) || "—") +
        " · " + ((identity && identity.loginIdMasked) || "—") +
        " · 预览态 " + (identity && identity.previewActive
          ? "开（管理写操作仍要求真实 owner）" : "关");
    }).catch(function () { /* 身份行失败不阻塞概览 */ });

    request("admin.overview.get", {}).then(function (ov) {
      hideError();
      renderOverview(ov);
      setPageState("overview", "ready", { message: "已更新（" + nowText() + "）" });
    }).catch(function (err) {
      handleErr(err, null);
      setPageState("overview", "error",
        { code: err && err.code, message: err && err.message,
          retry: function () { loadOverview(); } });
    });
    // 站点访问（匿名统计）：独立降级——任何错误（含 unknown_method /
    // not_implemented / 404 / permission_denied，即 D2 未发布）整卡隐藏。
    loadSiteStats();
  }

  // KPI 卡（§9.1 包 E）
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
      wrap.appendChild(kpiCard("User 累计已用", fmtCny(b.charge_nano_cny),
        "本周期 charge 合计"));
      wrap.appendChild(kpiCard("unpriced 事件（本周期）", fmtNum(b.unpriced_count),
        "未计价 ≠ 0 元", Number(b.unpriced_count) > 0));
    } else {
      wrap.appendChild(kpiCard("计费数据", "不可用",
        "要求 PostgreSQL 后端（" + (b.code || "pg_backend_required") + "）"));
    }
  }

  // 概览告警条（§4.2）：只在真实、当前、可行动的异常出现——unpriced>0 /
  // 供应商余额不可用或过旧 / 金额 reconcile 不一致；正常状态无告警卡。
  function overviewAlerts(b) {
    var alerts = [];
    if (!b || b.available === false) return alerts;
    if (Number(b.unpriced_count) > 0) {
      alerts.push("本周期有 " + b.unpriced_count + " 条 unpriced 事件（未计价 ≠ 0 元），见「费用」页告警区与计费异常标签");
    }
    if (b.reconcile_drift === true) {
      alerts.push("金额对账（reconcile）发现不一致：系统只报告不自动修账，请结合「费用」页账务流水排查");
    }
    var snapAge = b.provider_balance_age_seconds;
    if (b.provider_balance_snapshot) {
      if (snapAge !== null && snapAge !== undefined && Number(snapAge) > 86400) {
        alerts.push("DeepSeek 余额快照已超过 24 小时未更新（" +
                    Math.round(Number(snapAge) / 3600) + " 小时）");
      }
    } else {
      alerts.push("DeepSeek 供应商余额暂无快照（余额不可用，成本监控缺失）");
    }
    return alerts;
  }

  function renderOverview(ov) {
    renderOverviewKpis(ov);

    // 供应商余额（billing）：主视图 CNY；原始 nano 收进 raw 展开区（§4.2）
    var billDl = $("adm-ov-billing");
    var billRaw = $("adm-ov-billing-raw");
    if (billRaw) billRaw.textContent = "";
    if (billDl) {
      billDl.textContent = "";
      var b = ov.billing || {};
      if (b.available === false) {
        kvRow(billDl, "可用性", "不可用（" + (b.code || "pg_backend_required") + "）");
      } else {
        kvRow(billDl, "provider 成本合计（本周期）", fmtCny(b.provider_cost_nano_cny));
        kvRow(billDl, "User 累计已用（本周期）", fmtCny(b.charge_nano_cny));
        var snap = b.provider_balance_snapshot;
        kvRow(billDl, "DeepSeek 总余额",
          snap ? fmtCny(snap.total_balance_nano) : "（暂无快照）");
        kvRow(billDl, "余额快照年龄",
              b.provider_balance_age_seconds === null || b.provider_balance_age_seconds === undefined
                ? "—" : Math.round(b.provider_balance_age_seconds) + " 秒前");
        if (billRaw) {
          billRaw.appendChild(rawValuesDetails([
            ["provider_cost_nano_cny", b.provider_cost_nano_cny],
            ["charge_nano_cny", b.charge_nano_cny],
            ["total_balance_nano", snap && snap.total_balance_nano],
          ]));
        }
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

    // 告警区：仅真实异常（unpriced / 余额不可用或过旧 / reconcile）
    var alertCard = $("adm-ov-alert-card");
    var alertList = $("adm-ov-alerts");
    if (alertCard && alertList) {
      alertList.textContent = "";
      var alerts = overviewAlerts(ov.billing || {});
      alertCard.hidden = alerts.length === 0;
      alerts.forEach(function (text) {
        var li = document.createElement("li");
        li.textContent = text;
        alertList.appendChild(li);
      });
    }
  }

  // ------------------------------------------------------------------
  // 站点访问（匿名统计，§4.2/D2-3）：低频站长工具，独立降级。任何错误
  // （D2 未发布的 unknown_method/not_implemented/404/permission_denied，
  // 以及网络/后端错误）都整卡隐藏——绝不显示成错误，也不阻塞概览。
  // 禁止出现用户/邀请/注册/首次 AI/first·last touch/转化率任何内容。
  // ------------------------------------------------------------------
  function loadSiteStats() {
    var card = $("adm-site-card");
    request("admin.siteStats.get", {}).then(function (res) {
      if (!card) return;
      card.hidden = false;
      renderSiteStats(res || {});
    }).catch(function () {
      if (card) card.hidden = true;
    });
  }

  function siteKindLabel(kind, botName) {
    if (kind === "signed_in_human") return "已登录";
    if (kind === "suspected_bot") {
      return "疑似爬虫" + (botName ? "（" + botName + "）" : "");
    }
    return "匿名访客";
  }

  function renderSiteStats(res) {
    var card = $("adm-site-card");
    if (!card) return;
    var empty = $("adm-site-empty");
    var today = res.today || {};
    var d7 = res.d7 || {};
    var d30 = res.d30 || {};
    var totalVisits = Number((d30 && d30.visits) || 0) +
      Number((d7 && d7.visits) || 0) + Number((today && today.visits) || 0);
    var hasData = totalVisits > 0 || (res.daily || []).length > 0;
    if (empty) {
      empty.hidden = hasData;
      empty.textContent = "当前没有站点访问记录（仅统计公开页面的匿名访问）。";
    }

    var kpis = $("adm-site-kpis");
    if (kpis) {
      kpis.textContent = "";
      if (hasData) {
        kpis.appendChild(kpiCard("今日访问", fmtNum(today.visits)));
        kpis.appendChild(kpiCard("近 7 天访问", fmtNum(d7.visits)));
        kpis.appendChild(kpiCard("近 30 天访问", fmtNum(d30.visits)));
        // 日轮换 hash 不能跨日识别同一个人：禁止命名为「独立用户数」
        kpis.appendChild(kpiCard("匿名访客日去重次数（30 天累计）",
          fmtNum(d30.unique_visitors), "按日去重的近似次数，不是独立用户数"));
        kpis.appendChild(kpiCard("疑似爬虫（30 天）", fmtNum(d30.bots),
          "观测标签，不是封禁依据"));
      }
    }

    var dailyBody = $("adm-site-daily-tbody");
    if (dailyBody) {
      dailyBody.textContent = "";
      (res.daily || []).forEach(function (d) {
        var tr = document.createElement("tr");
        tr.appendChild(td(d.date));
        tr.appendChild(td(fmtNum(d.visits)));
        tr.appendChild(td(fmtNum(d.unique_visitors)));
        tr.appendChild(td(fmtNum(d.bots)));
        dailyBody.appendChild(tr);
      });
    }

    function fillTop(tbodyId, rows, keyFn, valFn) {
      var tbody = $(tbodyId);
      if (!tbody) return;
      tbody.textContent = "";
      (rows || []).slice(0, 10).forEach(function (r) {
        var tr = document.createElement("tr");
        tr.appendChild(td(keyFn(r)));
        tr.appendChild(td(fmtNum(valFn(r))));
        tbody.appendChild(tr);
      });
    }
    fillTop("adm-site-referrers-tbody", res.top_referrers,
      function (r) { return r.domain || "（直接）"; },
      function (r) { return r.visits; });
    fillTop("adm-site-pages-tbody", res.top_pages,
      function (r) { return r.page_key; },
      function (r) { return r.visits; });

    // 国家/地区：仅在本机离线库已配置且存在非 unknown 数据时显示
    var countriesBlock = $("adm-site-countries-block");
    if (countriesBlock) {
      var rows = res.top_countries || [];
      var meaningful = res.geo_configured === true &&
        rows.some(function (r) { return r.country_code && r.country_code !== "unknown"; });
      countriesBlock.hidden = !meaningful;
      if (meaningful) {
        fillTop("adm-site-countries-tbody", rows,
          function (r) { return r.country_code; },
          function (r) { return r.visits; });
      }
    }

    var kindsDl = $("adm-site-kinds");
    if (kindsDl) {
      kindsDl.textContent = "";
      var vk = res.visitor_kinds || {};
      if (hasData) {
        kvRow(kindsDl, "匿名人类访问（30 天）", fmtNum(vk.anonymous_human));
        kvRow(kindsDl, "已登录访问（30 天）", fmtNum(vk.signed_in_human));
        kvRow(kindsDl, "疑似爬虫（30 天）", fmtNum(vk.suspected_bot));
        kvRow(kindsDl, "统计生成时间", fmtTs(res.generated_at));
      }
    }

    var recentBody = $("adm-site-recent-tbody");
    if (recentBody) {
      recentBody.textContent = "";
      (res.recent || []).slice(0, 20).forEach(function (r) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(r.occurred_at), "adm-cell-time"));
        tr.appendChild(td(r.page_key));
        tr.appendChild(td(r.referrer_domain || "（直接）"));
        tr.appendChild(td(r.country_code && r.country_code !== "unknown"
          ? r.country_code : "—"));
        tr.appendChild(td(siteKindLabel(r.visitor_kind, r.bot_name)));
        recentBody.appendChild(tr);
      });
    }
  }

  // ------------------------------------------------------------------
  // 用户（§4.3 wave 2）：列表 5 列不变；额度列按角色/形态渲染。
  // ------------------------------------------------------------------
  function resetLists() {
    state.cursors = { users: null, usage: null, unpriced: null, ledger: null,
                      audit: null, invites: null, slides: null };
    ["adm-users-tbody", "adm-usage-tbody", "adm-unpriced-tbody",
     "adm-ledger-tbody", "adm-audit-tbody", "adm-invites-tbody",
     "adm-plugins-tbody", "adm-slides-tbody"].forEach(function (id) {
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
        message: "已更新（" + nowText() + "）",
      });
      }
      state.cursors.users = res.next_cursor || null;
      var more = $("adm-users-more-btn");
      if (more) more.disabled = !res.next_cursor;
      setPageHint(status, res.next_cursor ? "还有更多" : "已到底");
    }).catch(function (err) {
      if (seq !== state.listSeq) return;
      handleErr(err, status);
      setPageState("users", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadUsers(false); },
      });
    });
  }

  var drawerUser = null;
  // 触发抽屉的「详情」按钮（§4.10：关闭后焦点恢复到这里）
  var drawerTrigger = null;

  // ------------------------------------------------------------------
  // spend 形态解析（§4.3；R3 Wave1-Money 起单轨，形态纯由主体角色驱动）：
  //   - role=user → 恒 spend.total（一次性总额度投影；user_spend_target
  //     双轨已拆除，不存在「切换前」user 的窗口形态）；
  //   - role=owner → 恒 spend.window（月窗口）；
  //   - 两种形态同时出现 = 契约错误：显式报错态，绝不任选其一渲染；
  //   - 数据缺失/错误 = 不可用（原因），绝不伪造 0。金额运算全程 BigInt。
  // ------------------------------------------------------------------
  function userSpendInfo(u) {
    var s = u && u.spend;
    if (!s || s.error) {
      return { shape: "unavailable",
               reason: s && s.error ? String(s.error) : "额度数据缺失" };
    }
    var t = s.total;
    var w = s.window;
    var hasTotal = !!(t && t.total_limit_nano_cny !== null &&
                      t.total_limit_nano_cny !== undefined);
    var hasWindow = !!w;
    if (hasTotal && hasWindow) return { shape: "invalid" };
    if (hasTotal) {
      try {
        return {
          shape: "total",
          limit: BigInt(t.total_limit_nano_cny),
          spent: BigInt(t.spent_nano_cny),
          reserved: BigInt(t.reserved_nano_cny),
          remaining: (t.remaining_nano === null || t.remaining_nano === undefined)
            ? null : BigInt(t.remaining_nano),
          overage: (t.overage_nano === null || t.overage_nano === undefined)
            ? null : BigInt(t.overage_nano),
          raw: t,
        };
      } catch (e) {
        return { shape: "unavailable", reason: "总额度字段非法" };
      }
    }
    if (hasWindow) {
      try {
        return {
          shape: "window",
          limit: BigInt(w.limit_nano_snapshot),
          spent: BigInt(w.spent_nano_cny),
          reserved: BigInt(w.reserved_nano_cny),
          remaining: (w.remaining_nano === null || w.remaining_nano === undefined)
            ? null : BigInt(w.remaining_nano),
          raw: w,
        };
      } catch (e) {
        return { shape: "unavailable", reason: "窗口字段非法" };
      }
    }
    return { shape: "unavailable", reason: "额度数据缺失" };
  }

  // 剩余状态的统一呈现：短文案四态——剩余 X CNY / 已用尽 / 超支 X CNY /
  // 不可用（原因）+ 契约错误。耗尽/超额/告警色全部按**原始 nano**判断，
  // 不按四舍五入后的 0.00 判断（0 < remaining < 0.005 显示「剩余 0.00 CNY」
  // 但状态仍是非耗尽）。
  function remainingInfo(u) {
    var info = userSpendInfo(u);
    if (info.shape === "invalid") {
      return { text: "契约错误（total 与 window 同时返回）", danger: true };
    }
    if (info.shape === "unavailable") {
      return { text: "不可用（" + info.reason + "）", danger: false };
    }
    if (info.remaining === null) {
      return { text: "不可用（remaining 缺失）", danger: false };
    }
    if (info.shape === "total") {
      // total 形态：remaining = max(0, limit-spent-reserved)，恒非负；
      // 超额由 overage 明示
      if (info.overage !== null && info.overage > 0n) {
        return { text: "超支 " + fmtCny(info.overage), danger: true };
      }
      if (info.remaining === 0n) {
        return { text: "已用尽", danger: false };
      }
      return { text: "剩余 " + fmtCny(info.remaining), danger: false };
    }
    // window 形态：负 remaining = overage 观测（danger 色）
    if (info.remaining < 0n) {
      return { text: "超支 " + fmtCny(-info.remaining), danger: true };
    }
    if (info.remaining === 0n) {
      return { text: "已用尽", danger: false };
    }
    return { text: "剩余 " + fmtCny(info.remaining), danger: false };
  }

  function renderRemainCell(u) {
    var info = remainingInfo(u);
    var cell = document.createElement("td");
    cell.className = "adm-cell-remaining" + (info.danger ? " adm-usage-overage" : "");
    cell.textContent = info.text;
    return cell;
  }

  function renderUsers(items, append) {
    var tbody = $("adm-users-tbody");
    if (!tbody) return;
    if (!append) tbody.textContent = "";
    items.forEach(function (u) {
      var tr = document.createElement("tr");
      tr.appendChild(td(u.display_name));
      tr.appendChild(td(u.role, "adm-col-secondary"));
      // 状态 + AI（P0-2）：桌面一列；≤767px 状态格内堆叠 AI 补行
      var statusCell = document.createElement("td");
      statusCell.appendChild(document.createTextNode(u.enabled ? "启用" : "禁用"));
      var aiStack = document.createElement("div");
      aiStack.className = "adm-stack-mobile";
      aiStack.textContent = u.ai_access ? "AI" : "无 AI";
      statusCell.appendChild(aiStack);
      tr.appendChild(statusCell);
      tr.appendChild(renderRemainCell(u));
      var cell = document.createElement("td");
      cell.className = "adm-actions-cell";
      var detailBtn = actionBtn("详情", function () {
        openUserDrawer(u, detailBtn);
      }, "secondary");
      cell.appendChild(detailBtn);
      tr.appendChild(cell);
      tbody.appendChild(tr);
    });
  }

  // owner 当前月金额窗口（window 形态；可能缺失：owner 策略被禁用 / 后端异常。
  // 单轨后 user 行不再出现 window 形态，此读取仅服务 owner 抽屉与窗口调整）
  function currentUserWindow(u) {
    return u && u.spend && u.spend.window && !u.spend.error
      ? u.spend.window : null;
  }

  // 剩余额度的短文案（window 形态抽屉主视图用）：「剩余 X CNY / 已用尽 /
  // 超支 X CNY」；状态判断按原始 nano，显示值统一两位小数。
  function remainingText(remainingNano) {
    if (remainingNano === null || remainingNano === undefined) return "—";
    var b = BigInt(remainingNano);
    if (b < 0n) return "超支 " + fmtCny((-b).toString());
    if (b === 0n) return "已用尽";
    return "剩余 " + fmtCny(b.toString());
  }

  // CAS 版本：total 形态（user 恒）用 allowance version；window 形态
  // （owner 恒）用窗口 version。缺版本 = 拒绝盲写（禁用动作并说明）。
  function spendVersionOf(u) {
    var info = userSpendInfo(u);
    if (info.raw && info.raw.version !== null && info.raw.version !== undefined) {
      var v = Number(info.raw.version);
      if (Number.isSafeInteger(v) && v >= 1) return v;
    }
    return null;
  }

  function allowanceSourceText(source) {
    if (source === null || source === undefined || source === "") return "默认";
    if (source === "invite" || source === "invite_template") return "邀请初始额度";
    if (source === "default" || source === "restore_default") return "全局默认";
    if (source === "admin" || source === "manual" || source === "set") return "管理员设置";
    return String(source);
  }

  // ------------------------------------------------------------------
  // user 抽屉金额编辑器（wave 2 §4.3）：唯一两个金额动作——
  //   「设置总额度」admin.spend.userTotalLimit.set（绝对 limit + CAS）
  //   「恢复默认」  admin.spend.userTotalLimit.restoreDefault
  // 两者只修改绝对总上限，绝不重置已用/预占；确认条明示该语义。
  // ------------------------------------------------------------------
  function renderTotalLimitEditor(u) {
    var info = userSpendInfo(u);
    var version = spendVersionOf(u);
    var wrap = document.createElement("div");
    wrap.className = "adm-drawer-override";
    var title = document.createElement("p");
    title.className = "adm-note";
    title.textContent = "对这个用户设置一次性总额度（绝对总上限）：只改额度、绝不重置已用金额；额度不按月刷新。";
    var field = document.createElement("div");
    field.className = "adm-field";
    var label = document.createElement("label");
    label.className = "adm-field-label";
    var inputId = "adm-total-limit-input";
    label.htmlFor = inputId;
    label.textContent = "总额度（CNY，必填）";
    var input = document.createElement("input");
    input.type = "text";
    input.id = inputId;
    input.placeholder = "如 30.5";
    input.autocomplete = "off";
    input.setAttribute("aria-describedby", "adm-total-limit-help");
    var help = document.createElement("small");
    help.className = "adm-field-help";
    help.id = "adm-total-limit-help";
    help.textContent = "最多 9 位小数；调低到低于已用+预占时剩余显示 0 并进入超额，后续调用被拒";
    field.appendChild(label);
    field.appendChild(input);
    field.appendChild(help);
    var statusEl = document.createElement("p");
    statusEl.className = "adm-status";
    var actions = document.createElement("div");
    actions.className = "adm-form-actions";

    function editorStatus(text) {
      statusEl.textContent = text;
      setStatus("adm-users-status", text);
    }

    function markLimitInvalid(message) {
      input.setAttribute("aria-invalid", "true");
      if (input.focus) input.focus();
      statusEl.textContent = message;
    }

    var saveBtn = actionBtn("设置总额度", function () {
      var text = (input.value || "").trim();
      var limit = text ? cnyToNano(text) : null;
      if (!text || limit === null) {
        markLimitInvalid("总额度非法（CNY，最多 9 位小数，如 30.5）");
        return;
      }
      clearInvalid(input);
      askConfirm($("adm-drawer-confirm"),
        "确认把 " + (u.display_name || u.user_id) + " 的总额度设为 " +
        fmtCny(limit) + "（" + limit + " nano）？" +
        "这是绝对总上限：已用/预占不清零、不重置；若低于已用+预占，剩余将显示 0.00 并按原始值判定超额。",
        function () {
          editorStatus("保存中…");
          request("admin.spend.userTotalLimit.set", {
            user_id: u.user_id,
            total_limit_nano_cny: limit,
            expected_version: version,
          }).then(function () {
            editorStatus("已设置总额度 " + fmtCny(limit) +
              "（不重置已用金额；不按月刷新）");
            input.value = "";
            loadUsers(false);
          }).catch(function (err) {
            if (err && err.code === "version_conflict") {
              editorStatus("额度已被他人修改（409 version_conflict），已刷新，请重试");
              loadUsers(false);
            } else {
              editorStatus(errText(err));
            }
            showError(err && err.code, err && err.message);
          });
        });
    }, "primary");

    // 恢复默认：把该用户的绝对总上限显式改为当时全局默认总额度；
    // 已用金额保留。默认值只从 spend 设置响应的新键读取。
    var restoreBtn = actionBtn("恢复默认", function () {
      editorStatus("读取全局默认总额度…");
      request("admin.settings.get", {}).then(function (settings) {
        var spend = (settings || {}).spend || {};
        var defNano = spend.user_default_total_limit_nano_cny;
        if (defNano === null || defNano === undefined || defNano === "") {
          editorStatus("未能读取全局默认总额度；未做任何修改，请刷新后重试");
          return;
        }
        askConfirm($("adm-drawer-confirm"),
          "确认把总额度恢复为全局默认 " + fmtCny(defNano) +
          "？这是绝对总上限的显式修改：已用金额保留，不会清零、不会重置。",
          function () {
            editorStatus("保存中…");
            request("admin.spend.userTotalLimit.restoreDefault", {
              user_id: u.user_id,
              expected_version: version,
            }).then(function () {
              editorStatus("已恢复默认总额度 " + fmtCny(defNano) +
                "（已用金额保留）");
              loadUsers(false);
            }).catch(function (err) {
              if (err && err.code === "version_conflict") {
                editorStatus("额度已被他人修改（409 version_conflict），已刷新，请重试");
                loadUsers(false);
              } else {
                editorStatus(errText(err));
              }
              showError(err && err.code, err && err.message);
            });
          });
      }).catch(function (err) {
        editorStatus("默认总额度读取失败（" + errText(err) + "）；未做任何修改");
        showError(err && err.code, err && err.message);
      });
    }, "secondary");

    if (version === null) {
      // 拒绝盲写：读不到 CAS 版本就不提供动作
      saveBtn.disabled = true;
      restoreBtn.disabled = true;
      editorStatus("额度版本缺失（无法安全提交 CAS），请刷新列表后重试");
    }
    actions.appendChild(saveBtn);
    actions.appendChild(restoreBtn);
    wrap.appendChild(title);
    wrap.appendChild(field);
    wrap.appendChild(actions);
    wrap.appendChild(statusEl);
    return wrap;
  }

  // ------------------------------------------------------------------
  // window 形态抽屉编辑器（owner 恒 window——单轨后不存在「切换前 user」，
  // 该过渡分支已删）：既有 currentWindow.adjust CAS——只改当前窗口额度
  // 快照；不出现 total 动作。
  // ------------------------------------------------------------------
  function renderWindowAdjustEditor(u) {
    var w = currentUserWindow(u);
    var wrap = document.createElement("div");
    wrap.className = "adm-drawer-override";
    var title = document.createElement("p");
    title.className = "adm-note";
    title.textContent = "对 Owner 当前月窗口立即调整额度快照：已消费/预占不回退；调低到低于已消费后，下一次预占即被拒绝。";
    var field = document.createElement("div");
    field.className = "adm-field";
    var label = document.createElement("label");
    label.className = "adm-field-label";
    var inputId = "adm-window-adjust-input";
    label.htmlFor = inputId;
    label.textContent = "当前窗口新额度（CNY，必填）";
    var input = document.createElement("input");
    input.type = "text";
    input.id = inputId;
    input.placeholder = "如 30.5";
    input.autocomplete = "off";
    input.setAttribute("aria-describedby", "adm-window-adjust-help");
    var help = document.createElement("small");
    help.className = "adm-field-help";
    help.id = "adm-window-adjust-help";
    help.textContent = "最多 9 位小数；立即生效并写审计；窗口版本冲突（409）时刷新后重试";
    field.appendChild(label);
    field.appendChild(input);
    field.appendChild(help);
    var statusEl = document.createElement("p");
    statusEl.className = "adm-status";
    var actions = document.createElement("div");
    actions.className = "adm-form-actions";

    function editorStatus(text) {
      statusEl.textContent = text;
      setStatus("adm-users-status", text);
    }

    var saveBtn = actionBtn("调整当前窗口", function () {
      if (!w || w.error) {
        editorStatus("当前窗口不可用，无法调整");
        return;
      }
      var text = (input.value || "").trim();
      var limit = text ? cnyToNano(text) : null;
      if (!text || limit === null) {
        input.setAttribute("aria-invalid", "true");
        if (input.focus) input.focus();
        editorStatus("新额度非法（CNY，最多 9 位小数，如 30.5）");
        return;
      }
      clearInvalid(input);
      var newRemaining = (BigInt(limit) - BigInt(w.spent_nano_cny) -
                          BigInt(w.reserved_nano_cny)).toString();
      askConfirm($("adm-drawer-confirm"),
        "确认调整 " + (u.display_name || u.user_id) + " 的当前窗口额度？" +
        "当前额度 " + fmtCny(w.limit_nano_snapshot) + " → 新额度 " + fmtCny(limit) +
        "（" + limit + " nano）。影响：已消费 " + fmtCny(w.spent_nano_cny) +
        " / 预占 " + fmtCny(w.reserved_nano_cny) + " 不回退；新剩余 " +
        remainingPhrase(newRemaining) + "。操作立即生效并写审计，不等下个窗口。",
        function () {
          editorStatus("调整中…");
          request("admin.spend.currentWindow.adjust", {
            window_id: w.window_id,
            limit_nano_snapshot: limit,
            version: w.version,
          }).then(function (res) {
            editorStatus("已调整（窗口 version " +
              ((res && res.window && res.window.version) || "?") + "）");
            input.value = "";
            loadUsers(false);
          }).catch(function (err) {
            if (err && err.code === "version_conflict") {
              editorStatus("窗口已被他人调整（409 version_conflict），已刷新，请重试");
              loadUsers(false);
            } else {
              editorStatus(errText(err));
            }
            showError(err && err.code, err && err.message);
          });
        });
    }, "primary");

    if (!w || w.error) saveBtn.disabled = true;
    actions.appendChild(saveBtn);
    wrap.appendChild(title);
    wrap.appendChild(field);
    wrap.appendChild(actions);
    wrap.appendChild(statusEl);
    return wrap;
  }

  // 抽屉底部默认折叠的「技术细节」（§4.3 wave 2）：
  //   - user（恒 total 形态）：allowance id/version、cutover 时间、
  //     opening spent、原始 nano；
  //   - owner（恒 window 形态）：window id/version、起止、原始 nano。
  // 金额余额、soft/hard caps 及一切暗示账户余额控制可用额度的文字已删除。
  function drawerTechDetails(u) {
    var details = document.createElement("details");
    details.className = "adm-raw-values adm-drawer-tech";
    var summary = document.createElement("summary");
    summary.textContent = "技术细节（排障用）";
    details.appendChild(summary);
    var dl = document.createElement("dl");
    dl.className = "adm-kv";
    var info = userSpendInfo(u);
    var rawRows = [];
    if (info.shape === "total") {
      var t = info.raw;
      kvRow(dl, "allowance id", t.allowance_id);
      kvRow(dl, "allowance version", t.version);
      kvRow(dl, "切换时间（cutover_at）", fmtTs(t.cutover_at));
      kvRow(dl, "迁移基线已用（opening_spent）", fmtNano(t.opening_spent_nano_cny));
      rawRows = [
        ["total_limit_nano_cny", t.total_limit_nano_cny],
        ["spent_nano_cny", t.spent_nano_cny],
        ["reserved_nano_cny", t.reserved_nano_cny],
        ["remaining_nano", t.remaining_nano],
        ["overage_nano", t.overage_nano],
        ["opening_spent_nano_cny", t.opening_spent_nano_cny],
      ];
    } else if (info.shape === "window") {
      var w = info.raw;
      kvRow(dl, "window id", w.window_id);
      kvRow(dl, "window version", w.version);
      var boundary = document.createElement("span");
      boundary.className = "adm-boundary";
      boundary.textContent = fmtTs(w.window_start) + " → " + fmtTs(w.window_end) +
        "（上海时区自然月/周）";
      kvRowNode(dl, "窗口边界", boundary);
      rawRows = [
        ["limit_nano_snapshot", w.limit_nano_snapshot],
        ["spent_nano_cny", w.spent_nano_cny],
        ["reserved_nano_cny", w.reserved_nano_cny],
        ["remaining_nano", w.remaining_nano],
      ];
    } else if (info.shape === "invalid") {
      kvRow(dl, "契约状态", "total 与 window 同时返回（服务端契约破坏）");
    } else {
      kvRow(dl, "额度数据", "不可用（" + info.reason + "）");
    }
    details.appendChild(dl);
    // 原始 nano（wire 十进制字符串）逐行可复制
    rawRows.forEach(function (row) {
      if (row[1] === null || row[1] === undefined || row[1] === "") return;
      var line = document.createElement("div");
      line.className = "adm-raw-values-row";
      var key = document.createElement("span");
      key.className = "adm-raw-values-key";
      key.textContent = row[0];
      var code = document.createElement("code");
      code.textContent = String(row[1]);
      line.appendChild(key);
      line.appendChild(code);
      details.appendChild(line);
    });
    return details;
  }

  function openUserDrawer(u, trigger) {
    if (!els.drawer || !els.drawerBody) return;
    drawerUser = u;
    drawerTrigger = trigger || null;
    els.drawerBody.textContent = "";
    // 身份字段（§4.3 主视图保留）
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
    kvRow(dl, "最近 AI 调用", u.last_ai_call_at === null || u.last_ai_call_at === undefined
          ? "—" : fmtTs(u.last_ai_call_at) + "（Asia/Shanghai）");
    // 金额主视图按形态渲染（§4.3）
    var info = userSpendInfo(u);
    if (info.shape === "total") {
      kvRow(dl, "总额度", fmtCny(info.limit));
      kvRow(dl, "累计已用", fmtCny(info.spent));
      kvRow(dl, "预占", fmtCny(info.reserved));
      var avail = remainingInfo(u);
      kvRow(dl, "可用金额", avail.text);
      kvRow(dl, "额度来源", allowanceSourceText(info.raw && info.raw.source));
    } else if (info.shape === "window") {
      // window 形态仅 owner（单轨后 user 行恒 total）——月窗口显示保留
      kvRow(dl, "本月额度", fmtCny(info.limit));
      kvRow(dl, "本月已用", fmtCny(info.spent));
      kvRow(dl, "本月预占", fmtCny(info.reserved));
      kvRow(dl, "本月剩余", remainingText(info.remaining));
      // 旧 user_override/policy_scope 覆盖分支已随 user 窗口形态一起退役
      kvRow(dl, "额度来源", "默认（owner 独立策略）");
    } else if (info.shape === "invalid") {
      kvRow(dl, "额度", "契约错误：total 与 window 同时返回（服务端契约破坏，已禁止操作）");
    } else {
      kvRow(dl, "额度", "不可用（" + info.reason + "）");
    }
    els.drawerBody.appendChild(dl);
    // 低频技术细节默认折叠在底部
    els.drawerBody.appendChild(drawerTechDetails(u));
    var actions = document.createElement("div");
    actions.className = "adm-drawer-actions";
    actions.appendChild(renderUserActions(u));
    els.drawerBody.appendChild(actions);
    // 金额动作按形态二选一（互斥）：total → 总额度编辑器；window → 既有
    // 当前窗口调整；invalid → 不提供任何金额动作（显式报错态）
    if (info.shape === "total") {
      els.drawerBody.appendChild(renderTotalLimitEditor(u));
    } else if (info.shape === "window") {
      els.drawerBody.appendChild(renderWindowAdjustEditor(u));
    }
    els.drawer.hidden = false;
    if (els.drawerMask) els.drawerMask.hidden = false;
    // §4.10：dialog 打开后焦点进入关闭按钮
    if (els.drawerClose && els.drawerClose.focus) els.drawerClose.focus();
  }

  function closeUserDrawer() {
    if (!els.drawer || els.drawer.hidden) {
      drawerUser = null;
      return;
    }
    var trigger = drawerTrigger;
    drawerTrigger = null;
    drawerUser = null;
    clearConfirm($("adm-drawer-confirm"));
    els.drawer.hidden = true;
    if (els.drawerMask) els.drawerMask.hidden = true;
    if (trigger && trigger.focus) trigger.focus();
  }

  // 行内操作（§10.2）：授予/收回 AI / 身份预览 / 启停 / 重置密码（仅普通用户）。
  // §4.5：普通操作在前，禁用/重置密码等危险操作放独立「危险操作」区；
  // owner 行不提供禁用与重置（break-glass 不变量，服务端同样 409）。
  function renderUserActions(u) {
    var wrap = document.createElement("div");
    wrap.className = "adm-actions";
    var isOwner = u.role === "owner";
    wrap.appendChild(actionBtn(u.ai_access ? "收回 AI" : "授予 AI", function () {
      setAiAccess(u, !u.ai_access);
    }, "secondary"));
    wrap.appendChild(actionBtn("身份预览", function () {
      askConfirm($("adm-drawer-confirm"),
        "确认以 " + (u.display_name || u.user_id) + " 的身份进入只读预览？" +
        "预览期间以该用户视角使用 Viewer；管理写操作仍要求真实 owner（被拒绝）。",
        function () { startPreviewFor(u); });
    }, "secondary"));
    var zone = document.createElement("div");
    zone.className = "adm-danger-zone";
    var zoneTitle = document.createElement("p");
    zoneTitle.className = "adm-danger-zone-title";
    zoneTitle.textContent = "危险操作";
    zone.appendChild(zoneTitle);
    if (!isOwner) {
      zone.appendChild(actionBtn(u.enabled ? "禁用" : "启用", function () {
        if (u.enabled) {
          askConfirm($("adm-drawer-confirm"),
            "确认禁用用户 " + (u.display_name || u.user_id) + "？其全部会话将立即失效。",
            function () { setUserEnabled(u, false); });
        } else {
          setUserEnabled(u, true);
        }
      }, u.enabled ? "danger" : "secondary"));
      zone.appendChild(actionBtn("重置密码", function () {
        askResetPassword(u);
      }, "danger-outline"));
    }
    wrap.appendChild(zone);
    return wrap;
  }

  // 身份预览（§10.2，PR5 修订恢复入口）
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

  // 重置密码：页内输入新密码（sandbox 下 window.prompt 不可用）+ 确认执行。
  function askResetPassword(u) {
    var box = $("adm-drawer-confirm");
    if (!box) return;
    box.hidden = false;
    box.textContent = "";
    var field = document.createElement("div");
    field.className = "adm-field";
    var label = document.createElement("label");
    label.className = "adm-field-label";
    label.htmlFor = "adm-reset-password-input";
    label.textContent = "为 " + (u.display_name || u.user_id) +
      " 设置新密码（必填，≥15 位；确认后该用户全部会话立即退出）";
    var input = document.createElement("input");
    input.type = "password";
    input.id = "adm-reset-password-input";
    input.minLength = 15;
    input.maxLength = 200;
    input.autocomplete = "new-password";
    input.placeholder = "如 correct-horse-battery-staple";
    var ok = actionBtn("确认重置", function () {
      var np = input.value || "";
      if (np.length < 15) {
        markInvalid(input, "新密码至少 15 位（当前 " + np.length + " 位）",
          "adm-users-status");
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
    var cancel = actionBtn("取消", function () { clearConfirm(box); }, "secondary");
    field.appendChild(label);
    field.appendChild(input);
    box.appendChild(field);
    box.appendChild(ok);
    box.appendChild(cancel);
    if (input.focus) input.focus();
  }

  function submitCreateUser() {
    var loginId = ($("adm-users-new-login") && $("adm-users-new-login").value || "").trim();
    var display = ($("adm-users-new-display") && $("adm-users-new-display").value || "").trim();
    var password = $("adm-users-new-password") ? $("adm-users-new-password").value : "";
    var limitText = ($("adm-users-new-limit") && $("adm-users-new-limit").value || "").trim();
    if (!loginId) {
      markInvalid($("adm-users-new-login"), "缺少登录账号", "adm-users-create-status");
      return;
    }
    if (password.length < 15) {
      markInvalid($("adm-users-new-password"),
        "初始密码至少 15 位（当前 " + password.length + " 位）",
        "adm-users-create-status");
      return;
    }
    var payload = { login_id: loginId, password: password };
    if (display) payload.display_name = display;
    // Batch B：可选初始总额度（CNY → nano 十进制字符串；留空 = 继承全局
    // 默认；建号+allowance+audit 服务端同一事务）。旧 monthly 字段不再发送。
    if (limitText) {
      var limit = cnyToNano(limitText);
      if (limit === null) {
        markInvalid($("adm-users-new-limit"),
          "初始总额度非法（CNY，最多 9 位小数，如 20 或 12.5）",
          "adm-users-create-status");
        return;
      }
      payload.total_limit_nano_cny = limit;
    }
    setStatus("adm-users-create-status", "创建中…");
    request("admin.users.create", payload).then(function (res) {
      ["adm-users-new-login", "adm-users-new-display", "adm-users-new-password",
       "adm-users-new-limit"].forEach(function (id) {
        var el = $(id); if (el) { el.value = ""; clearInvalid(el); }
      });
      setStatus("adm-users-create-status",
        "已创建 " + ((res && res.user && res.user.user_id) || loginId) +
        (payload.total_limit_nano_cny
          ? "（初始总额度 " + fmtCny(payload.total_limit_nano_cny) + "）"
          : "（继承全局默认总额度）"));
      loadUsers(false);
    }).catch(function (err) {
      showError(err && err.code, err && err.message);
      setStatus("adm-users-create-status", errText(err));
    });
  }

  // ------------------------------------------------------------------
  // 邀请（§4.4 wave 2）：注册模式只读摘要 + 创建/列表/撤销。
  // 来源漏斗 / 用户来源明细 / source·campaign·cohort 全部退役；
  // 注册模式只在设置页可写，本页仅展示 + 跳转。
  // ------------------------------------------------------------------
  function loadInvitesPage() {
    var seq = state.listSeq;
    setPageState("invites", "loading");
    var settled = Promise.allSettled([
      loadInviteMode(),
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
          retry: function () { loadInvitesPage(); },
        });
        return;
      }
      setPageState("invites", "ready", {
        message: "已更新（" + nowText() + "）" +
          (failed.length ? "（部分数据加载失败，可刷新重试）" : ""),
      });
    });
  }

  // 注册模式只读摘要：来自 admin.settings.get（旧 acquisition summary 已删）
  function loadInviteMode() {
    return request("admin.settings.get", {}).then(function (settings) {
      renderInviteMode(((settings || {}).registration || {}).mode);
      return true;
    }).catch(function (err) {
      var dl = $("adm-invite-mode");
      if (dl) { dl.textContent = ""; kvRow(dl, "可用性", errText(err)); }
      throw err; // 交给协调器计数（partial-error 语义）
    });
  }

  function renderInviteMode(mode) {
    var dl = $("adm-invite-mode");
    if (!dl) return;
    dl.textContent = "";
    kvRow(dl, "当前模式", mode || "—");
  }

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
      if (seq !== state.listSeq) return false;
      hideError();
      var tbody = $("adm-invites-tbody");
      if (!tbody) return false;
      if (!append) tbody.textContent = "";
      var invites = res.invites || [];
      invites.forEach(function (inv) {
        var tr = document.createElement("tr");
        tr.appendChild(td(inv.invite_id));
        tr.appendChild(td(inv.login_id_masked || "（不绑定）"));
        tr.appendChild(td(inv.ai_access ? "开" : "关"));
        // 初始总额度模板（Batch B/D1）：null=兑换继承默认；两位小数 CNY
        tr.appendChild(td(inv.total_limit_nano_cny === null ||
                          inv.total_limit_nano_cny === undefined
          ? "默认" : fmtCny(inv.total_limit_nano_cny)));
        tr.appendChild(td(inv.note));
        tr.appendChild(td(inviteStatusLabel(inv)));
        tr.appendChild(td(fmtTs(inv.expires_at), "adm-cell-time"));
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
      if (!append && !invites.length) {
        setPageHint(status, "暂无邀请；可在上方「新建邀请」创建。");
      }
      state.cursors.invites = res.next_cursor || null;
      var more = $("adm-invites-more-btn");
      if (more) more.disabled = !res.next_cursor;
      setPageHint(status, res.next_cursor ? "还有更多" : "已到底");
      return invites.length > 0;
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
    var note = ($("adm-invite-note") && $("adm-invite-note").value || "").trim();
    var ttlHours = parseInt(ttlRaw, 10);
    if (!ttlHours || ttlHours < 1 || ttlHours > 720) {
      markInvalid($("adm-invite-ttl"), "有效期需为 1–720 小时",
        "adm-invite-create-status");
      return;
    }
    // 新契约（§3.4）：{login_id?, ttl_seconds?, ai_access, total_limit_nano_cny?, note?}
    // ——不再有 source_code/campaign_id/cohort/monthly_limit_nano_cny。
    var payload = {
      ttl_seconds: ttlHours * 3600,
      ai_access: ai,
    };
    if (limitText) {
      var limit = cnyToNano(limitText);
      if (limit === null) {
        markInvalid($("adm-invite-limit"),
          "初始总额度非法（CNY，最多 9 位小数，如 20 或 12.5）",
          "adm-invite-create-status");
        return;
      }
      payload.total_limit_nano_cny = limit;
    }
    if (loginId) payload.login_id = loginId;
    if (note) payload.note = note;
    setStatus("adm-invite-create-status", "创建中…");
    request("admin.invites.create", payload).then(function (res) {
      setStatus("adm-invite-create-status",
        "已创建 " + ((res && res.invite && res.invite.invite_id) || "?") +
        "；明文邀请码只显示这一次：");
      showInviteTokenOnce((res && res.invite && res.invite.token) || "");
      ["adm-invite-login", "adm-invite-limit", "adm-invite-note"]
        .forEach(function (id) {
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
  // 设置（§6.1 批次 D + wave 2 §4.5）：注册模式 + 消费额度策略（三键拆分）+
  // enforcement + 运行时安全参数 + Demo/Owner「立即调整当前周期」。
  // 金额输入（CNY）→ wire 十进制字符串（cnyToNano 全程 BigInt，禁 float）。
  // ------------------------------------------------------------------
  // 三键拆分（Batch B）：user 默认额度键改为 user_default_total_limit；
  // 回显值只读 spend 响应的新键（*_nano_cny）；CAS 上下文（policy_id+version）
  // 短期仍从 policies 兼容字段读取——缺上下文拒绝保存（不盲写）。
  var SPEND_POLICY_FIELDS = [
    ["adm-spend-user-total", "user_default", "user_default_total_limit",
     "user_default_total_limit_nano_cny"],
    ["adm-spend-demo-week", "demo_global", "demo_weekly_limit",
     "demo_weekly_limit_nano_cny"],
    ["adm-spend-owner-month", "owner", "owner_monthly_limit",
     "owner_monthly_limit_nano_cny"],
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
      setPageState("settings", "ready", {
        message: "已更新（" +
          nowText() + "）",
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

  // §5.5：当前窗口小型摘要卡只回答「额度 / 剩余」（Demo 一张、Owner 一张）
  function windowSummaryCard(title, w) {
    var card = document.createElement("div");
    card.className = "adm-summary-card";
    var h = document.createElement("h3");
    h.textContent = title;
    card.appendChild(h);
    var dl = document.createElement("dl");
    dl.className = "adm-kv";
    if (!w) {
      kvRow(dl, "可用性", "—");
    } else if (w.error) {
      kvRow(dl, "可用性", "不可用（" + w.error + "）");
    } else {
      kvRow(dl, "额度", fmtCny(w.limit_nano_snapshot));
      kvRow(dl, "剩余", fmtCny(w.remaining_nano));
    }
    card.appendChild(dl);
    return card;
  }

  // §4.7：enforcement registered/all 的警示卡——切换影响必须先于保存可见
  function updateEnforcementWarning() {
    var sel = $("adm-spend-mode");
    var warn = $("adm-spend-mode-warning");
    if (!sel || !warn) return;
    if (sel.value === "registered") {
      warn.hidden = false;
      warn.textContent = "警示：registered 将对注册用户开启金额硬拒绝（额度不足时 provider 调用被拒）。切换写审计、可切回 shadow；只应在金额硬闸验收后执行。";
    } else if (sel.value === "all") {
      warn.hidden = false;
      warn.textContent = "警示：all 将连 Demo 一起开启金额硬拒绝（Demo 调用同样会被拒）。切换写审计、可切回 shadow；只应在金额硬闸验收后执行。";
    } else {
      warn.hidden = true;
      warn.textContent = "";
    }
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
    // 消费额度策略卡（三键拆分）
    var spend = settings.spend || {};
    var modeSelect = $("adm-spend-mode");
    if (spend.available !== false) {
      if (modeSelect) modeSelect.value = spend.enforcement_mode || "shadow";
      SPEND_POLICY_FIELDS.forEach(function (quadruple) {
        var el = $(quadruple[0]);
        // 回显只读新键（旧 policies 字段短期仍在，但不再用于回显）
        var v = spend[quadruple[3]];
        if (el && v !== null && v !== undefined && v !== "") {
          el.value = nanoToCnyString(v);
        } else if (el) {
          el.value = "";
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
          "（registered/all 只应在金额硬闸验收后切换）");
        SPEND_POLICY_FIELDS.forEach(function (quadruple) {
          var v = spend[quadruple[3]];
          kvRow(spendDl, quadruple[1] + " 额度",
            (v !== null && v !== undefined && v !== "")
              ? fmtCny(v) : "未设置");
        });
        var bounds = spend.next_window_bounds || {};
        kvRow(spendDl, "Demo 下个周期边界",
          bounds.demo_week ? (fmtTs(bounds.demo_week[0]) + " → " +
                              fmtTs(bounds.demo_week[1])) : "—");
      }
    }
    // §5.5：当前 demo/owner 窗口拆成小型摘要卡（只回答额度/剩余）
    var grid = $("adm-window-summaries");
    if (grid) {
      grid.textContent = "";
      var wins = spend.current_windows || {};
      ["demo", "owner"].forEach(function (k) {
        grid.appendChild(windowSummaryCard(
          k === "demo" ? "Demo（周窗口）" : "Owner（月窗口）", wins[k]));
      });
    }
    // 策略额度的原始 nano 展开区（§4.2：主视图 CNY，wire 字符串可复制）
    var spendRaw = $("adm-spend-raw");
    if (spendRaw) {
      spendRaw.textContent = "";
      if (spend.available !== false) {
        var rawRows = SPEND_POLICY_FIELDS.map(function (quadruple) {
          return [quadruple[3], spend[quadruple[3]]];
        }).filter(function (row) {
          return row[1] !== null && row[1] !== undefined && row[1] !== "";
        });
        if (rawRows.length) spendRaw.appendChild(rawValuesDetails(rawRows));
      }
    }
    updateEnforcementWarning();
    // 记录「加载时回显值」：保存只提交被修改的字段
    SPEND_POLICY_FIELDS.forEach(function (quadruple) {
      var el = $(quadruple[0]);
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
      // 自带 API 步数上限（own_task_max_steps_limit）已从 UI 移除
      //（后端字段兼容保留）；注册用户步数字段统一 1..500。
      [["adm-rt-psteps", "platform_task_max_steps"],
       ["adm-rt-demosteps", "demo_task_max_steps"],
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
      setStatus("adm-spend-status", "额度策略要求 PostgreSQL 后端");
      return;
    }
    var payload = {};
    try {
      SPEND_POLICY_FIELDS.forEach(function (quadruple) {
        var el = $(quadruple[0]);
        var text = el ? String(el.value || "").trim() : "";
        if (!text) return;
        // 未修改的字段沿用现值（与加载回显一致时不重复提交）
        var loaded = el ? String(el.getAttribute("data-loaded") || "") : "";
        if (text === loaded) return;
        var limit = cnyToNano(text);
        if (limit === null) {
          throw { message: quadruple[1] + " 金额非法（CNY，最多 9 位小数）" };
        }
        // CAS 上下文：demo/owner 来自 policies 兼容字段（policy_id+version）；
        // user 默认总额度来自 settings 响应扁平新键（source=total_defaults →
        // 专用单例端点；source=user_default_policy → policies 兼容路径）。
        // 缺上下文拒绝保存，绝不盲写
        if (quadruple[1] === "user_default") {
          var udVersion = spend.user_default_total_limit_version;
          if (udVersion === null || udVersion === undefined) {
            throw { message: quadruple[1] + " 缺少默认总额度 CAS 上下文，无法更新额度（请刷新后重试）" };
          }
          payload[quadruple[2]] = {
            limit_nano_cny: limit,
            version: udVersion,
            source: spend.user_default_total_limit_source || "user_default_policy",
            policy_id: spend.user_default_total_policy_id || undefined,
          };
          return;
        }
        var policy = (spend.policies || {})[quadruple[1]];
        if (!policy || !policy.policy_id) {
          throw { message: quadruple[1] + " 缺少策略 CAS 上下文，无法更新额度（请刷新后重试）" };
        }
        payload[quadruple[2]] = {
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
        settingsUpdateResult(res, "adm-spend-status", "额度策略已提交");
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
        "？该操作开启金额硬拒绝（额度不足时 provider 调用被拒），" +
        "只应在金额硬闸验收后执行；切换写审计，可切回 shadow。",
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
      // 注册用户单任务安全上限：1..500（默认/最高 500；>100 为异常长任务
      // 观测线，500 为安全暂停线；消费额度由总金额控制）。
      // Demo 步数/并发沿用各自现有边界；自带 API 步数上限已从 UI 移除。
      [["adm-rt-psteps", "platform_task_max_steps", 1, 500],
       ["adm-rt-demosteps", "demo_task_max_steps", 1, 1000000],
       ["adm-rt-concurrency", "demo_max_concurrency", 1, 1000000]].forEach(
        function (triple) {
          var el = $(triple[0]);
          var raw = el ? String(el.value || "").trim() : "";
          if (!raw) return;
          var v = Number(raw);
          if (!Number.isSafeInteger(v) || v < triple[2] || v > triple[3]) {
            throw { message: triple[1] + " 需为 " + triple[2] + "–" + triple[3] + " 整数" };
          }
          payload[triple[1]] = v;
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

  // ------------------------------------------------------------------
  // 「立即调整当前周期」（§5.5）：主体固定 Demo / Owner，调用同一个
  // admin.spend.currentWindow.adjust（window_id + limit CAS）。
  // ------------------------------------------------------------------

  // 剩余金额的确认条用语（确认条允许细节，但不输出长拒绝说明）
  function remainingPhrase(nanoStr) {
    var b = BigInt(nanoStr);
    if (b < 0n) return "超支 " + fmtCny((-b).toString());
    if (b === 0n) return "0.00 CNY（已用尽）";
    return fmtCny(b.toString());
  }

  function adjustFixedWindow(subjectKey) {
    var isDemo = subjectKey === "demo";
    var inputId = isDemo ? "adm-win-demo-limit" : "adm-win-owner-limit";
    var statusId = isDemo ? "adm-win-demo-status" : "adm-win-owner-status";
    var confirmId = isDemo ? "adm-win-demo-confirm" : "adm-win-owner-confirm";
    var label = isDemo ? "Demo（全站共享周窗口）" : "Owner（月窗口）";
    var spend = (state.settingsSnapshot || {}).spend || {};
    if (spend.available === false) {
      setStatus(statusId, "额度策略要求 PostgreSQL 后端");
      return;
    }
    var w = (spend.current_windows || {})[subjectKey];
    if (!w || w.error) {
      setStatus(statusId, "当前窗口不可用（策略缺失或后端不可用）");
      return;
    }
    var text = ($(inputId) && $(inputId).value || "").trim();
    var limit = text ? cnyToNano(text) : null;
    if (limit === null) {
      setStatus(statusId, "新额度非法（CNY，最多 9 位小数，如 30.5）");
      return;
    }
    var newRemaining = (BigInt(limit) - BigInt(w.spent_nano_cny) -
                        BigInt(w.reserved_nano_cny)).toString();
    askConfirm($(confirmId),
      "确认调整 " + label + " 的当前周期额度？" +
      "当前额度 " + fmtCny(w.limit_nano_snapshot) + " → 新额度 " + fmtCny(limit) +
      "（" + limit + " nano）。影响：已消费 " + fmtCny(w.spent_nano_cny) +
      " / 预占 " + fmtCny(w.reserved_nano_cny) + " 不回退；新剩余 " +
      remainingPhrase(newRemaining) + "。操作立即生效并写审计，不等下个窗口。",
      function () {
        setStatus(statusId, "调整中…");
        request("admin.spend.currentWindow.adjust", {
          window_id: w.window_id,
          limit_nano_snapshot: limit,
          version: w.version,
        }).then(function (res) {
          setStatus(statusId,
            "已调整（窗口 version " +
            ((res && res.window && res.window.version) || "?") + "）");
          if ($(inputId)) $(inputId).value = "";
          loadSettingsPage();
        }).catch(function (err) {
          if (err && err.code === "version_conflict") {
            setStatus(statusId,
              "窗口已被他人调整（409 version_conflict），已刷新，请重试");
            loadSettingsPage();
          } else {
            setStatus(statusId, errText(err));
          }
          showError(err && err.code, err && err.message);
        });
      });
  }

  // ------------------------------------------------------------------
  // 费用页（§4.6 wave 2）：KPI → [仅异常]计费异常条 → Demo 消耗卡 →
  // 费用明细（三个页内标签、单一内容区、只有当前标签发请求）。
  // ------------------------------------------------------------------
  var BILL_TABS = ["usage", "ledger", "unpriced"];
  var billTab = "usage";
  // 标签激活代际：每次切换 ++；迟到的旧标签响应按代际丢弃（§4.6：
  // 切换标签时取消/忽略旧请求结果，防止迟到响应覆盖当前视图）
  var billTabSeq = 0;

  function loadBillingPage() {
    setPageState("billing", "loading");
    var seq = state.listSeq;
    state.billOverview = null;
    state.billProviderBalance = null;
    state.billProviderBalanceError = null;
    state.billDemo = null;
    // 页内标签复位到「模型调用」，并激活（触发首屏请求）
    setBillingTab("usage");
    var settled = Promise.allSettled([
      loadBillOverview(),
      loadProviderBalanceCard(),
      loadDemoStats("current"),
    ]);
    settled.then(function (results) {
      if (seq !== state.listSeq) return;
      var ok = [], failed = [];
      results.forEach(function (r) {
        (r.status === "fulfilled" ? ok : failed).push(r);
      });
      if (!ok.length) {
        var err = results[0].reason;
        setPageState("billing", "error", {
          code: err && err.code, message: err && err.message,
          retry: function () { loadBillingPage(); },
        });
        return;
      }
      setPageState("billing", "ready", {
        message: "已更新（" + nowText() + "）" +
          (failed.length ? "（部分数据加载失败，可刷新重试）" : ""),
      });
    });
  }

  // KPI 聚合（overview 已有聚合 + 供应商余额 + Demo 周统计；禁止逐用户拼表）
  function loadBillOverview() {
    return request("admin.overview.get", {}).then(function (ov) {
      state.billOverview = ov || {};
      renderBillKpis();
      renderBillAlert();
      return true;
    });
  }

  function renderBillKpis() {
    var wrap = $("adm-bill-kpis");
    if (!wrap) return;
    wrap.textContent = "";
    var b = (state.billOverview || {}).billing || {};
    // 供应商余额
    var pb = state.billProviderBalance;
    if (pb && pb.snapshot) {
      wrap.appendChild(kpiCard("供应商余额", fmtCny(pb.snapshot.total_balance_nano),
        "快照 " + fmtTs(pb.snapshot.observed_at)));
    } else {
      wrap.appendChild(kpiCard("供应商余额", "—", "暂无快照"));
    }
    // User 累计已用
    wrap.appendChild(kpiCard("User 累计已用",
      b.available === false ? "不可用" : fmtCny(b.charge_nano_cny),
      "本周期 charge 合计"));
    // Demo 本周已用
    var demo = state.billDemo;
    wrap.appendChild(kpiCard("Demo 本周已用",
      demo ? fmtCny(demo.spent_nano_cny) : "—",
      demo ? (demo.virtual ? "本周暂无记录（按策略边界的空统计）" : "自然周窗口")
           : "统计暂不可用"));
    // 未计价
    wrap.appendChild(kpiCard("未计价事件",
      b.available === false ? "不可用" : fmtNum(b.unpriced_count),
      "未计价 ≠ 0 元", Number(b.unpriced_count) > 0));
  }

  // [仅异常]计费异常条：unpriced=0 时不渲染红框；>0 或余额/reconcile 异常
  // 才出现，且「查看未计价明细」可跳到计费异常标签。
  function renderBillAlert() {
    var card = $("adm-bill-alert");
    var list = $("adm-bill-alert-list");
    var gotoBtn = $("adm-bill-alert-goto");
    if (!card || !list) return;
    list.textContent = "";
    var alerts = [];
    var b = (state.billOverview || {}).billing;
    if (b && b.available !== false) {
      if (Number(b.unpriced_count) > 0) {
        alerts.push("有 " + b.unpriced_count + " 条未计价事件（未计价 ≠ 0 元）：点下方按钮查看明细。");
      }
      if (b.reconcile_drift === true) {
        alerts.push("金额对账（reconcile）发现不一致：系统只报告不自动修账，请结合账务流水排查。");
      }
    }
    var pb = state.billProviderBalance;
    if (pb && pb.snapshot) {
      var age = pb.age_seconds;
      if (age !== null && age !== undefined && Number(age) > 86400) {
        alerts.push("供应商余额快照已超过 24 小时未更新（" +
          Math.round(Number(age) / 3600) + " 小时）。");
      }
    } else if (state.billProviderBalanceError) {
      alerts.push("供应商余额不可用（" +
        (state.billProviderBalanceError.code || "error") + "）。");
    }
    card.hidden = alerts.length === 0;
    alerts.forEach(function (text) {
      var li = document.createElement("li");
      li.textContent = text;
      list.appendChild(li);
    });
    if (gotoBtn) gotoBtn.hidden = !(b && b.available !== false &&
      Number(b.unpriced_count) > 0);
  }

  // Demo 消耗卡（§4.6 统计口径）：admin.spend.demoStats.get（只读，无副作用）。
  // 无数据中性空态；unpriced>0 或统计不完整（db_unavailable_denials_included）
  // 才用异常色。
  function loadDemoStats(windowKey) {
    var status = $("adm-demo-status");
    return request("admin.spend.demoStats.get", { window: windowKey || "current" })
      .then(function (res) {
        state.billDemo = res || null;
        renderDemoStats(res || {});
        if (status) status.textContent = "已更新（" + nowText() + "）";
        renderBillKpis();
        return true;
      }).catch(function (err) {
        state.billDemo = null;
        renderDemoStats(null);
        if (status) {
          status.textContent = "Demo 统计暂不可用（" + errText(err) + "）";
        }
        renderBillKpis();
        throw err; // 交给 loadBillingPage 的协调器计 partial
      });
  }

  function renderDemoStats(res) {
    var dl = $("adm-demo-info");
    var empty = $("adm-demo-empty");
    var rawBox = $("adm-demo-raw");
    var card = $("adm-demo-card");
    if (!dl) return;
    dl.textContent = "";
    if (rawBox) rawBox.textContent = "";
    if (card) card.classList.remove("adm-card--anomaly");
    var hasStat = res && res.limit_nano_cny !== null &&
      res.limit_nano_cny !== undefined;
    if (!hasStat) {
      // 中性空态：统计不可用/无数据不是异常
      if (empty) {
        empty.hidden = false;
        empty.textContent = "暂无 Demo 消耗统计（功能未发布或本周期尚无数据）。";
      }
      return;
    }
    if (empty) empty.hidden = true;
    var anomaly = Number(res.unpriced_calls || 0) > 0 ||
      res.db_unavailable_denials_included === true ||
      (res.overage_nano_cny !== null && res.overage_nano_cny !== undefined &&
       BigInt(res.overage_nano_cny) > 0n);
    if (anomaly && card) card.classList.add("adm-card--anomaly");
    var winLabel = (res.window === "previous" ? "上周" : "本周") +
      "（" + fmtTs(res.window_start) + " → " + fmtTs(res.window_end) + "）" +
      (res.virtual ? " · 本周暂无窗口记录（按策略边界计算的空统计）" : "");
    kvRow(dl, "统计窗口", winLabel);
    kvRow(dl, "周额度", fmtCny(res.limit_nano_cny));
    kvRow(dl, "已用", fmtCny(res.spent_nano_cny));
    kvRow(dl, "预占", fmtCny(res.reserved_nano_cny));
    kvRow(dl, "剩余", fmtCny(res.remaining_nano_cny));
    if (res.overage_nano_cny !== null && res.overage_nano_cny !== undefined &&
        BigInt(res.overage_nano_cny) > 0n) {
      kvRow(dl, "超额", fmtCny(res.overage_nano_cny));
    }
    kvRow(dl, "调用（已计价/未计价）",
      fmtNum(res.priced_calls) + " / " + fmtNum(res.unpriced_calls));
    kvRow(dl, "输入 tokens（cache 命中/未命中）",
      fmtNum(res.cache_hit_tokens) + " / " + fmtNum(res.cache_miss_tokens));
    kvRow(dl, "输出 tokens", fmtNum(res.output_tokens));
    if (res.reasoning_tokens !== null && res.reasoning_tokens !== undefined) {
      kvRow(dl, "推理 tokens", fmtNum(res.reasoning_tokens));
    }
    kvRow(dl, "用户费用（charge）", fmtCny(res.charge_nano_cny));
    kvRow(dl, "供应商成本", fmtCny(res.provider_cost_nano_cny));
    var holds = res.holds || {};
    kvRow(dl, "hold（授权中/在途/已结算/已释放/已过期）",
      fmtNum(holds.authorized) + " / " + fmtNum(holds.open) + " / " +
      fmtNum(holds.settled) + " / " + fmtNum(holds.released) + " / " +
      fmtNum(holds.expired));
    kvRow(dl, "hold 拒绝（按原因聚合）", fmtNum(res.denials_total) +
      (res.denials && res.denials.length
        ? "（" + res.denials.map(function (d) {
            return d.reason + " × " + d.count;
          }).join("，") + "）" : ""));
    if (res.db_unavailable_denials_included === true) {
      kvRow(dl, "统计口径提示", "数据库不可用期间的拒绝只进外部 metric，未计入以上 DB 聚合");
    }
    if (rawBox) {
      rawBox.appendChild(rawValuesDetails([
        ["limit_nano_cny", res.limit_nano_cny],
        ["spent_nano_cny", res.spent_nano_cny],
        ["reserved_nano_cny", res.reserved_nano_cny],
        ["remaining_nano_cny", res.remaining_nano_cny],
        ["charge_nano_cny", res.charge_nano_cny],
        ["provider_cost_nano_cny", res.provider_cost_nano_cny],
      ]));
    }
  }

  // ---- 页内标签（§4.6）：aria-selected / 键盘方向键 / 可见焦点 ----
  function setBillingTab(name) {
    if (BILL_TABS.indexOf(name) === -1) name = "usage";
    billTab = name;
    billTabSeq++;
    var seq = billTabSeq;
    BILL_TABS.forEach(function (t) {
      var btn = $("adm-tab-" + t);
      if (btn) {
        btn.setAttribute("aria-selected", t === name ? "true" : "false");
        btn.setAttribute("tabindex", t === name ? "0" : "-1");
      }
    });
    var panel = $("adm-tabpanel-detail");
    if (panel) panel.setAttribute("aria-labelledby", "adm-tab-" + name);
    var sections = { usage: "adm-usage-section", ledger: "adm-ledger-section",
                     unpriced: "adm-unpriced-section" };
    Object.keys(sections).forEach(function (t) {
      var sec = $(sections[t]);
      if (sec) sec.hidden = t !== name;
    });
    // 只有当前标签发请求；同一激活代际内的迟到旧响应按代际丢弃
    if (name === "usage") loadUsage(false, seq);
    else if (name === "ledger") loadLedger(false, seq);
    else loadUnpriced(false, seq);
  }

  function billingTabKeydown(ev) {
    var key = ev && ev.key;
    var idx = BILL_TABS.indexOf(billTab);
    var next = null;
    if (key === "ArrowRight" || key === "ArrowDown") next = BILL_TABS[(idx + 1) % BILL_TABS.length];
    else if (key === "ArrowLeft" || key === "ArrowUp") next = BILL_TABS[(idx - 1 + BILL_TABS.length) % BILL_TABS.length];
    else if (key === "Home") next = BILL_TABS[0];
    else if (key === "End") next = BILL_TABS[BILL_TABS.length - 1];
    if (next) {
      ev.preventDefault();
      setBillingTab(next);
      var btn = $("adm-tab-" + next);
      if (btn && btn.focus) btn.focus();
    }
  }

  // ---- 费用明细：模型调用（usage events）----
  // 默认列：时间/用户/模型/状态/输入/输出 token/用户费用；
  // provider 成本、cache hit/miss、event_id 放行详情（行详情折叠区）。
  function loadUsage(append, seqGuard) {
    var seq = seqGuard !== undefined ? seqGuard : billTabSeq;
    var f = state.filters.usage || {};
    var payload = { limit: 50, cursor: append ? state.cursors.usage : null };
    if (f.model) payload.model = f.model;
    if (f.user_id) payload.user_id = f.user_id;
    if (f.status) payload.status = f.status;
    var status = $("adm-usage-status");
    request("admin.billing.usage.list", payload).then(function (res) {
      if (seq !== billTabSeq) return; // 标签已切换：迟到旧响应一律丢弃
      hideError();
      renderUsage(res.items || [], append);
      state.cursors.usage = res.next_cursor || null;
      var more = $("adm-usage-more-btn");
      if (more) more.disabled = !res.next_cursor;
      setPageHint(status, res.next_cursor ? "还有更多" : "已到底");
    }).catch(function (err) {
      if (seq !== billTabSeq) return; // 旧标签的迟到失败也忽略
      handleErr(err, status);
    });
  }

  // 行详情折叠区：单行「详情」按钮切换（colspan 全宽子行）
  function toggleDetailRow(tr, buildContent) {
    var tbody = tr.parentNode;
    var next = tr.nextSibling;
    if (next && next.className && next.className.indexOf("adm-detail-row") !== -1) {
      tbody.removeChild(next);
      return;
    }
    var sub = document.createElement("tr");
    sub.className = "adm-detail-row";
    var cell = document.createElement("td");
    cell.colSpan = tr.children.length;
    cell.appendChild(buildContent());
    sub.appendChild(cell);
    if (next) tbody.insertBefore(sub, next);
    else tbody.appendChild(sub);
  }

  function detailDl(rows) {
    var dl = document.createElement("dl");
    dl.className = "adm-kv adm-detail-kv";
    rows.forEach(function (row) {
      kvRow(dl, row[0], row[1] === null || row[1] === undefined ? "—" : String(row[1]));
    });
    return dl;
  }

  function renderUsage(items, append) {
    var tbody = $("adm-usage-tbody");
    if (!tbody) return;
    if (!append) tbody.textContent = "";
    items.forEach(function (e) {
      var tr = document.createElement("tr");
      tr.appendChild(td(fmtTs(e.occurred_at), "adm-cell-time"));
      tr.appendChild(td(e.user_id || (e.subject_type === "demo" ? "Demo" : e.subject_type)));
      tr.appendChild(td(e.model));
      tr.appendChild(td(e.status === "priced" ? "已计价" : "未计价"));
      tr.appendChild(td(fmtNum((Number(e.cache_hit_input_tokens) || 0) +
                               (Number(e.cache_miss_input_tokens) || 0))));
      tr.appendChild(td(fmtNum(e.output_tokens)));
      // §4.7：用户费用两位小数 CNY；unpriced 金额保持 null 呈现「—」，
      // 绝不显示成 0 元
      tr.appendChild(td(e.charge_nano_cny === null || e.charge_nano_cny === undefined
        ? "—" : fmtCny(e.charge_nano_cny), "adm-cell-time"));
      var cell = document.createElement("td");
      cell.className = "adm-actions-cell";
      var btn = actionBtn("详情", function () {
        toggleDetailRow(tr, function () {
          return detailDl([
            ["event_id", e.event_id],
            ["主体", e.subject_type + (e.user_id ? " · " + e.user_id : "")],
            ["provider 成本", e.provider_cost_nano_cny === null ||
              e.provider_cost_nano_cny === undefined
              ? "—" : fmtCny(e.provider_cost_nano_cny)],
            ["cache 命中/未命中 tokens",
              fmtNum(e.cache_hit_input_tokens) + " / " + fmtNum(e.cache_miss_input_tokens)],
            ["未计价原因", e.unpriced_reason],
            ["原始 nano（charge）", e.charge_nano_cny],
          ]);
        });
      }, "secondary");
      cell.appendChild(btn);
      tr.appendChild(cell);
      tbody.appendChild(tr);
    });
  }

  // ---- 费用明细：账务流水（ledger，只读不可变）----
  // 默认列：时间/用户/类型/金额（CNY）/原因；account_id/entry_id/nano/
  // metadata 放行详情。
  function loadLedger(append, seqGuard) {
    var seq = seqGuard !== undefined ? seqGuard : billTabSeq;
    var payload = { limit: 50, cursor: append ? state.cursors.ledger : null };
    var status = $("adm-ledger-status");
    request("admin.billing.ledger.list", payload).then(function (res) {
      if (seq !== billTabSeq) return;
      hideError();
      var tbody = $("adm-ledger-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      (res.items || []).forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.created_at), "adm-cell-time"));
        tr.appendChild(td(e.user_id || e.account_id));
        var kindCell = td(e.kind);
        // PR6 模拟扣费条目：metadata.simulated=true → kind 旁加「模拟」徽标
        if (e.metadata && e.metadata.simulated === true) {
          var badge = document.createElement("span");
          badge.className = "adm-badge";
          badge.textContent = "模拟";
          kindCell.appendChild(document.createTextNode(" "));
          kindCell.appendChild(badge);
        }
        tr.appendChild(kindCell);
        tr.appendChild(td(fmtCny(e.amount_nano_cny), "adm-cell-time"));
        tr.appendChild(td(e.reason));
        var cell = document.createElement("td");
        cell.className = "adm-actions-cell";
        var btn = actionBtn("详情", function () {
          toggleDetailRow(tr, function () {
            return detailDl([
              ["account_id", e.account_id],
              ["entry_id", e.entry_id],
              ["金额（原始 nano）", e.amount_nano_cny],
              ["metadata", e.metadata ? JSON.stringify(e.metadata) : null],
            ]);
          });
        }, "secondary");
        cell.appendChild(btn);
        tr.appendChild(cell);
        tbody.appendChild(tr);
      });
      state.cursors.ledger = res.next_cursor || null;
      var more = $("adm-ledger-more-btn");
      if (more) more.disabled = !res.next_cursor;
      setPageHint(status, res.next_cursor ? "还有更多" : "已到底");
    }).catch(function (err) {
      if (seq !== billTabSeq) return;
      handleErr(err, status);
    });
  }

  // ---- 费用明细：计费异常（unpriced）----
  // 0 条 = 中性空态（无红框红边）；>0 的主告警在页级告警条与概览各一处。
  function loadUnpriced(append, seqGuard) {
    var seq = seqGuard !== undefined ? seqGuard : billTabSeq;
    var payload = { limit: 50, status: "unpriced", cursor: append ? state.cursors.unpriced : null };
    request("admin.billing.usage.list", payload).then(function (res) {
      if (seq !== billTabSeq) return;
      var empty = $("adm-unpriced-empty");
      var tbody = $("adm-unpriced-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      var items = res.items || [];
      items.forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.occurred_at), "adm-cell-time"));
        tr.appendChild(td(e.model));
        tr.appendChild(td(e.unpriced_reason));
        tr.appendChild(td(e.event_id));
        tbody.appendChild(tr);
      });
      if (empty) empty.hidden = items.length > 0 || Boolean(res.next_cursor);
      state.cursors.unpriced = res.next_cursor || null;
      var more = $("adm-unpriced-more-btn");
      if (more) more.disabled = !res.next_cursor;
    }).catch(function () {
      // json 后端等：保持中性空态文案（不渲染红框），错误只进全局错误条
      var empty = $("adm-unpriced-empty");
      if (empty) { empty.hidden = false; }
    });
  }

  // 供应商余额卡（KPI + 异常条数据源）
  function renderProviderBalanceSnapshot(payload) {
    state.billProviderBalance = payload || null;
    renderBillKpis();
    renderBillAlert();
  }

  function loadProviderBalanceCard() {
    return request("admin.billing.providerBalance.get", {}).then(function (payload) {
      hideError();
      state.billProviderBalanceError = null;
      renderProviderBalanceSnapshot(payload);
      return true;
    }).catch(function (err) {
      state.billProviderBalanceError = err;
      renderProviderBalanceSnapshot(null);
      throw err; // 交给 loadBillingPage 决定页面终态
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
      state.billProviderBalanceError = null;
      renderProviderBalanceSnapshot({
        provider: res.provider,
        snapshot: res.snapshot,
        age_seconds: res.age_seconds,
      });
    }).catch(function (err) {
      if (btn) btn.disabled = false;
      if (status) status.textContent = errText(err);
    });
  }

  // ------------------------------------------------------------------
  // 审计（§10.5）
  // ------------------------------------------------------------------
  var AUDIT_KEY_LABELS = {
    from_cny: "调整前", to_cny: "调整后",
    from_limit_nano_cny: "原额度（nano）", to_limit_nano_cny: "新额度（nano）",
    limit_nano_cny: "额度（nano）", amount_nano_cny: "金额（nano）",
    monthly_limit_nano_cny: "月额度模板（nano，历史）",
    total_limit_nano_cny: "总额度（nano）",
    expected_version: "预期版本", version: "版本",
    soft_cap_nano_cny: "soft cap（nano，历史）",
    hard_cap_nano_cny: "hard cap（nano，历史）",
    login_id_masked: "登录账号（掩码）", registration_mode: "注册模式",
    spend_enforcement_mode: "enforcement 模式",
    demo_weekly_limit_nano_cny: "Demo 周额度（nano）",
    user_default_total_limit_nano_cny: "用户默认总额度（nano）",
    owner_monthly_limit_nano_cny: "Owner 月额度（nano）",
    enabled: "启用", ai_access: "AI access", reason: "原因",
    user_id: "user_id", invite_id: "invite_id", kind: "类型",
    password: "（已脱敏）", token: "（已脱敏）", secret: "（已脱敏）",
  };

  function auditDetailCell(e) {
    var cell = document.createElement("td");
    cell.className = "adm-cell-detail";
    var detail = e.detail || {};
    var keys = Object.keys(detail);
    if (keys.length) {
      var sum = document.createElement("div");
      sum.className = "adm-audit-summary";
      sum.textContent = keys.map(function (k) {
        return (AUDIT_KEY_LABELS[k] || k) + "：" + String(detail[k]);
      }).join("；");
      cell.appendChild(sum);
    }
    var details = document.createElement("details");
    details.className = "adm-raw-values";
    var summary = document.createElement("summary");
    summary.textContent = "原始详情";
    var code = document.createElement("code");
    code.textContent = JSON.stringify(detail);
    details.appendChild(summary);
    details.appendChild(code);
    cell.appendChild(details);
    return cell;
  }

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
          message: "已更新（" + nowText() + "）",
        });
      }
      auditItems.forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.ts), "adm-cell-time"));
        tr.appendChild(td((e.actor_role || "") + (e.actor_user_id ? ("·" + e.actor_user_id) : "")));
        tr.appendChild(td(e.action));
        tr.appendChild(td((e.target_type || "") + (e.target_id ? ("·" + e.target_id) : "")));
        tr.appendChild(auditDetailCell(e));
        tbody.appendChild(tr);
      });
      state.cursors.audit = res.next_cursor || null;
      var more = $("adm-audit-more-btn");
      if (more) more.disabled = !res.next_cursor;
      setPageHint(status, res.next_cursor ? "还有更多" : "已到底");
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
  // 切片可见性（2026-09-05，review P0 owner 读隔离）：
  //   - inventory 是 owner 唯一「看全部」出口（切片元数据清点，不含图像
  //     内容）；
  //   - 每行「授权/收回」调用 admin.slides.setVisibility（幂等；view 级）；
  //   - 归属列显示 owner 展示名 + 掩码 login_id（无归属显示「无主」——
  //     无主切片不因读隔离失联，在此可授权恢复可见）。
  // ------------------------------------------------------------------
  function fmtBytes(n) {
    if (n === null || n === undefined || typeof n !== "number" || !(n >= 0)) {
      return "—";
    }
    var units = ["B", "KB", "MB", "GB", "TB"];
    var v = n;
    var i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return (i === 0 ? String(v) : (Math.round(v * 10) / 10).toFixed(1)) +
      " " + units[i];
  }

  function ownerCellText(item) {
    if (!item.owner_user_id) return "无主";
    var name = item.owner_display_name;
    var masked = item.owner_login_id_masked;
    if (name && masked) return name + "（" + masked + "）";
    return name || masked || item.owner_user_id;
  }

  function grantStatusText(item) {
    // 升级 B（2026-09-05）：owner 工作区 = 本人 ∪ 显式添加——public 只对
    // 普通用户默认可见，不再自动计入 owner 集合；included 以
    // granted_to_owner 为准（资产生代失效的授权不计入）。
    if (item.public && item.granted_to_owner) return "公开 + 已加入工作区";
    if (item.public) return "公开（仅普通用户默认可见）";
    if (item.granted_to_owner) return "已加入我的工作区";
    return "未加入";
  }

  function loadSlides(append) {
    var seq = state.listSeq;
    var status = $("adm-slides-status");
    setPageState("slides", "loading");
    request("admin.slides.inventory", {
      limit: 50,
      cursor: append ? state.cursors.slides : null,
    }).then(function (res) {
      if (seq !== state.listSeq) return; // 页面已切换：晚到响应丢弃
      hideError();
      var items = (res && res.items) || [];
      var tbody = $("adm-slides-tbody");
      if (!append && tbody) tbody.textContent = "";
      items.forEach(function (item) { renderSlideRow(item); });
      if (!append && !items.length) {
        setPageState("slides", "empty", {
          message: "没有切片文件。上传切片后在此清点与授权。",
        });
      } else {
        setPageState("slides", "ready", {
          message: "已更新（" + nowText() + "）",
        });
      }
      state.cursors.slides = (res && res.next_cursor) || null;
      var more = $("adm-slides-more-btn");
      if (more) more.disabled = !(res && res.next_cursor);
      setPageHint(status, res && res.next_cursor ? "还有更多" : "已到底");
    }).catch(function (err) {
      if (seq !== state.listSeq) return;
      handleErr(err, status);
      setPageState("slides", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadSlides(false); },
      });
    });
  }

  function renderSlideRow(item) {
    var tbody = $("adm-slides-tbody");
    if (!tbody) return;
    var tr = document.createElement("tr");
    tr.appendChild(td(item.name));
    tr.appendChild(td(ownerCellText(item)));
    tr.appendChild(td(item.public ? "是" : "—"));
    tr.appendChild(td(item.archived ? "是" : "—"));
    tr.appendChild(td(fmtBytes(item.size_bytes)));
    tr.appendChild(td(grantStatusText(item)));
    var cell = document.createElement("td");
    cell.className = "adm-actions-cell";
    var granted = !!item.granted_to_owner;
    cell.appendChild(actionBtn(granted ? "移除" : "加入", function () {
      if (granted) {
        askConfirm($("adm-slides-confirm"),
          "确认将 " + item.name + " 移出我的工作区？移除后新请求立即拒绝，" +
          "进行中的 AI 任务会被取消，后续工具访问被拒（归属者与公开状态不受影响）。",
          function () { setSlideVisibility(item, false); });
      } else {
        setSlideVisibility(item, true);
      }
    }, granted ? "danger-outline" : "secondary"));
    tr.appendChild(cell);
    tbody.appendChild(tr);
  }

  function setSlideVisibility(item, granted) {
    request("admin.slides.setVisibility",
            { name: item.name, granted: granted })
      .then(function (res) {
        var already = res && granted && res.already_granted;
        var cancelled = (res && res.runs_cancelled || []).length;
        setStatus("adm-slides-status",
          (granted ? "已加入工作区 " : "已移出工作区 ") + item.name +
          (already ? "（此前已加入，幂等成功）" : "") +
          (!granted && cancelled
            ? "（已请求取消 " + cancelled + " 个运行中任务）" : ""));
        loadSlides(false);
      })
      .catch(function (err) { handleErr(err, $("adm-slides-status")); });
  }

  // ------------------------------------------------------------------
  // 插件管理（PR5 修订：恢复旧侧栏插件管理功能面）
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
        message: "已更新（" + nowText() + "）",
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
        // §4.5：每行最多一个实心 danger（停用）；轮换凭证是次要危险 →
        // danger-outline
        cell.appendChild(actionBtn(enabled ? "停用" : "启用", function () {
          if (enabled) {
            askConfirm($("adm-plugins-confirm"),
              "确认停用 " + inst.plugin_id + "？该安装全部在途 JWT 立即失效，" +
              "依赖其能力的调用会被拒绝。",
              function () { setPluginEnabled(inst, false); });
          } else {
            setPluginEnabled(inst, true);
          }
        }, enabled ? "danger" : "secondary"));
        cell.appendChild(actionBtn("轮换凭证", function () {
          askConfirm($("adm-plugins-confirm"),
            "确认轮换 " + inst.plugin_id + " 的安装凭证？旧 secret 立即失效，" +
            "使用方必须同步更新；新明文只显示一次。",
            function () { rotatePluginSecret(inst); });
        }, "danger-outline"));
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

  // 轮换凭证：响应里的新明文 secret 只在此处展示一次（内存 DOM）
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
    billTabSeq++;    // 作废在途费用明细响应（跨页迟到不写回）
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
    else if (name === "slides") loadSlides(false);
    else if (name === "invites") loadInvitesPage();
    else if (name === "settings") loadSettingsPage();
    else if (name === "billing") loadBillingPage();
    else if (name === "plugins") loadPlugins();
    else if (name === "audit") loadAudit(false);
  }

  // §4.10：Tab/Shift+Tab 圈定在抽屉内（首尾循环）；焦点若因任何原因落到
  // 抽屉外，下一个 Tab 也会被拉回，保证背景不可键盘操作。
  var DRAWER_FOCUSABLE = "button, input, select, textarea, a[href], " +
    "[tabindex]:not([tabindex='-1'])";

  function drawerFocusables() {
    if (!els.drawer || !els.drawer.querySelectorAll) return [];
    var raw = els.drawer.querySelectorAll(DRAWER_FOCUSABLE) || [];
    return Array.prototype.filter.call(raw, function (el) {
      return !el.disabled && el.hidden !== true;
    });
  }

  function trapDrawerTab(ev) {
    var items = drawerFocusables();
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    var active = null;
    try { active = document.activeElement; } catch (e) { active = null; }
    var inside = !!(active && els.drawer.contains && els.drawer.contains(active));
    if (ev.shiftKey) {
      if (!inside || active === first) {
        if (ev.preventDefault) ev.preventDefault();
        last.focus();
      }
    } else {
      if (!inside || active === last) {
        if (ev.preventDefault) ev.preventDefault();
        first.focus();
      }
    }
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
    // 用户详情抽屉：关闭按钮 / 遮罩 / Esc / Tab 圈定（§4.10 焦点管理）。
    if (els.drawerClose && els.drawerClose.addEventListener) {
      els.drawerClose.addEventListener("click", closeUserDrawer);
    }
    if (els.drawerMask && els.drawerMask.addEventListener) {
      els.drawerMask.addEventListener("click", closeUserDrawer);
    }
    document.addEventListener("keydown", function (ev) {
      if (!els.drawer || els.drawer.hidden) return;
      if (ev.key === "Escape" || ev.keyCode === 27) {
        closeUserDrawer();
        return;
      }
      if (ev.key === "Tab") trapDrawerTab(ev);
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
    // 邀请页（wave 2：注册模式只读 + 跳设置）
    onClick("adm-invite-goto-settings-btn", function () { showPage("settings"); });
    onClick("adm-invite-create-btn", submitCreateInvite);
    onClick("adm-invites-more-btn", function () { loadInvites(true); });
    onClick("adm-invite-token-copy", function () {
      var code = $("adm-invite-token");
      if (code) copyToClipboard(code.textContent || "");
    });
    // 设置页（批次 D + wave 2）
    onClick("adm-regmode-save-btn", saveRegistrationMode);
    onClick("adm-spend-save-btn", saveSpendPolicies);
    onClick("adm-rt-save-btn", saveRuntimeLimits);
    onClick("adm-win-demo-adjust-btn", function () { adjustFixedWindow("demo"); });
    onClick("adm-win-owner-adjust-btn", function () { adjustFixedWindow("owner"); });
    // §4.7：enforcement 模式切换的即时警示卡
    var spendModeSelect = $("adm-spend-mode");
    if (spendModeSelect && spendModeSelect.addEventListener) {
      spendModeSelect.addEventListener("change", updateEnforcementWarning);
    }
    // 费用页（wave 2）：KPI 刷新 / Demo 统计 / 页内标签
    onClick("adm-balance-refresh-btn", refreshProviderBalance);
    var demoSelect = $("adm-demo-window");
    if (demoSelect && demoSelect.addEventListener) {
      demoSelect.addEventListener("change", function () {
        loadDemoStats(demoSelect.value || "current").catch(function () { /* 已在卡内中性呈现 */ });
      });
    }
    onClick("adm-demo-refresh-btn", function () {
      var sel = $("adm-demo-window");
      loadDemoStats(sel ? sel.value : "current").catch(function () { /* 已在卡内中性呈现 */ });
    });
    onClick("adm-bill-alert-goto", function () { setBillingTab("unpriced"); });
    BILL_TABS.forEach(function (t) {
      onClick("adm-tab-" + t, function () { setBillingTab(t); });
    });
    // 角色化键盘导航：在三个标签按钮上挂同一 keydown（方向键/Home/End）
    BILL_TABS.forEach(function (t) {
      var btn = $("adm-tab-" + t);
      if (btn && btn.addEventListener) {
        btn.addEventListener("keydown", billingTabKeydown);
      }
    });
    onClick("adm-usage-search-btn", function () {
      state.filters.usage = {
        model: ($("adm-usage-model") && $("adm-usage-model").value || "").trim(),
        user_id: ($("adm-usage-user") && $("adm-usage-user").value || "").trim(),
        status: $("adm-usage-status") ? $("adm-usage-status").value : "",
      };
      state.cursors.usage = null;
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
    // 切片可见性页（2026-09-05 读隔离）
    onClick("adm-slides-refresh-btn", function () { loadSlides(false); });
    onClick("adm-slides-more-btn", function () { loadSlides(true); });
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
  // tests/js/admin-plugin-ui.test.ts 锁定「字符串进、字符串出」契约与
  // 两位小数口径（formatCny2）。
  window.PathTogetherAdminClient = {
    request: request,
    showPage: showPage,
    cnyToNano: cnyToNano,
    nanoToCnyString: nanoToCnyString,
    formatCny2: formatCny2,
    fmtNano: fmtNano,
    fmtCny: fmtCny,
    fmtTs: fmtTs,
    handshakeState: function () {
      return {
        ready: !!state.nonce && !state.dead,
        protocolVersion: state.protocolVersion,
        grantedCount: state.granted.length,
      };
    },
  };
})();
