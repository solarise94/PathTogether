/* =========================================================================
   HistoPilot 用户反馈 · 客户端记录器（改版第四轮 §3）
   =========================================================================
   职责（docs/admin-viewer-round4-20261009.md §3「客户端记录」）：
     - 尽早装载（index.html 中先于 app.js）：包装 window.fetch、接管
       window.onerror / unhandledrejection / console.error / console.warn，
       并以捕获式 document 监听记录点击的控件标识。
     - 环形缓冲：只保留最近 300 条或 15 分钟内的事件，纯内存，不落盘、
       不自动上送；只有用户主动发送反馈时才随 POST /api/feedback 附带。
     - 隐私红线（绝不记录）：
         输入框内容、密码、请求/响应正文、Cookie、查询串、图像数据。
       - 路径一律去掉查询串；/s/<token> 记为 /s/***；
       - api 事件只记 方法 / 路径 / 状态码 / 耗时 / 响应 JSON 的 code 字段
         （若有）——读 code 用响应克隆，绝不消费调用方的响应流；
       - action 事件只记 控件标识（id / data-action / data-i18n 键 / role /
         标签与输入类型），绝不记 value。
     - 高频媒体端点（瓦片 / DZI / 缩略图 / region / 渲染输出）不记录：
       它们不是「接口调用」语义且会冲掉缓冲里的错误。
     - 全部入口 try/catch 兜底：记录器自身任何异常都不影响主流程。

   对外 API（window.HP_FEEDBACK）：
     log(kind, data)        追加事件（app.js 桥接 nav/slide 打开关闭）
     setCurrentSlide(id)    当前切片 id（快照用；不产生事件）
     snapshot()             发送时附带的整体对象
                            {captured_at, url_path, lang, viewport, user_agent,
                             current_slide_id, events:[...]}
     events() / maskPath()  测试与调试入口
   ========================================================================= */
(function (window, document) {
  "use strict";

  var MAX_EVENTS = 300;          // 环形缓冲条数上限
  var MAX_AGE_MS = 15 * 60 * 1000; // 15 分钟
  var MAX_TEXT = 500;            // 路径长度上限
  var MEDIA_PATH_RE =
    /(\/tiles(\/|$)|\/dzi(\/|$)|\.dzi$|\/thumbnail(\/|$)|\/region(\/|$)|\/render(\/|$)|\/output(\/|$)|\/static\/|\/plugins\/|\/shared\/)/;

  var events = [];
  var currentSlideId = null;
  var seq = 0;

  function now() {
    try { return Date.now(); } catch (e) { return 0; }
  }

  /* ---------- 路径规整：去查询串；/s/<token> → /s/*** ---------- */
  function maskPath(raw) {
    var p = String(raw == null ? "" : raw);
    var q = p.indexOf("?");
    if (q >= 0) p = p.slice(0, q);
    var h = p.indexOf("#");
    if (h >= 0) p = p.slice(0, h);
    try { p = decodeURI(p); } catch (e) { /* 保留原样 */ }
    // /s/<token> 分享令牌是凭证：路径中任何一段 /s/<seg> 都掩码为首段 ***
    //（/s/<token> 页面入口与 /api/.../s/<token> 形态的资源路径同口径）
    p = p.replace(/\/s\/([^/?#]+)/g, "/s/***");
    p = p.replace(/(\/api\/share\/)[^/]+(\/claim(?:\/|$))/, "$1***$2");
    p = p.replace(/(\/api\/annotation\/)[^/]+(\/\d+(?:\/|$))/, "$1***$2");
    return p;
  }

  function prune() {
    var cutoff = now() - MAX_AGE_MS;
    while (events.length && (events.length > MAX_EVENTS || (events[0].t || 0) < cutoff)) {
      events.shift();
    }
  }

  function push(evt) {
    try {
      evt.t = now();
      events.push(evt);
      prune();
    } catch (e) { /* 记录失败不影响主流程 */ }
  }

  /* ---------- api：fetch 包装 ---------- */
  function requestMeta(input) {
    // fetch(Request) 与 fetch(url, opts) 两种形态；method 默认 GET
    try {
      if (input && typeof input === "object" && typeof input.url === "string") {
        return { url: input.url, method: (input.method || "GET") };
      }
    } catch (e) {}
    var url = String(input == null ? "" : input);
    var method = "GET";
    try {
      if (arguments.length > 1 && arguments[1] && arguments[1].method) {
        method = String(arguments[1].method);
      }
    } catch (e) {}
    return { url: url, method: method };
  }

  function recordableApiPath(url) {
    try {
      var u = new URL(url, window.location && window.location.href);
      if (u.origin !== (window.location && window.location.origin)) return null;
      var path = maskPath(u.pathname);
      if (path.indexOf("/api/") !== 0) return null;
      if (MEDIA_PATH_RE.test(path)) return null;
      return path;
    } catch (e) { return null; }
  }

  function responseCode(resp) {
    // 只读 content-type 为 JSON 的响应克隆里的 code 字段；绝不消费主响应流。
    // 恒返回 Promise（非 JSON/异常 → null），调用方统一 .then。
    try {
      var ct = "";
      try { ct = String(resp.headers && resp.headers.get("content-type") || ""); } catch (e) {}
      if (ct.indexOf("json") < 0) return Promise.resolve(null);
      return resp.clone().json().then(function (body) {
        return (body && body.code != null) ? String(body.code) : null;
      }, function () { return null; });
    } catch (e) { return Promise.resolve(null); }
  }

  function wrapFetch() {
    if (typeof window.fetch !== "function") return;
    if (window.fetch.__hpFeedbackWrapped) return;
    var orig = window.fetch;
    var wrapped = function (input, init) {
      var started = now();
      var meta = requestMeta(input, init);
      var path = recordableApiPath(meta.url);
      var promise = orig.apply(this, arguments);
      if (!path) return promise;
      return promise.then(function (resp) {
        push({ kind: "api", method: String(meta.method || "GET").toUpperCase(),
               path: path, status: resp ? resp.status : 0, ms: now() - started });
        responseCode(resp).then(function (code) {
          if (code) push({ kind: "api_code", path: path, status: resp.status, code: code });
        });
        return resp;
      }, function (err) {
        push({ kind: "api", method: String(meta.method || "GET").toUpperCase(),
               path: path, status: 0, ms: now() - started,
               error: "network_error" });
        throw err;
      });
    };
    try { wrapped.__hpFeedbackWrapped = true; } catch (e) {}
    window.fetch = wrapped;
  }

  /* ---------- error / console ---------- */
  function trunc(s) {
    s = String(s == null ? "" : s);
    return s.length > MAX_TEXT ? s.slice(0, MAX_TEXT) : s;
  }

  // Exception messages and console arguments are arbitrary application data.
  // Record only a closed set of categories and a sanitized script location.
  function errorType(err) {
    var name = err && err.name;
    return ["Error", "TypeError", "ReferenceError", "SyntaxError", "RangeError",
      "URIError", "EvalError", "AbortError"].indexOf(name) >= 0 ? name : "Error";
  }
  function sourcePath(raw) {
    try {
      var u = new URL(raw, window.location.href);
      return u.origin === window.location.origin ? trunc(maskPath(u.pathname)) : "external_script";
    } catch (e) { return ""; }
  }
  function wrapErrors() {
    window.addEventListener("error", function (e) {
      try {
        push({ kind: "error", message: errorType(e && e.error),
          source: sourcePath((e && e.filename) || ""),
          line: Number(e && e.lineno) || 0, column: Number(e && e.colno) || 0 });
      } catch (err) {}
    });
    window.addEventListener("unhandledrejection", function (e) {
      try { push({ kind: "error", rejection: true, message: errorType(e && e.reason) }); }
      catch (err) {}
    });
  }
  function wrapConsole() {
    if (!window.console) return;
    ["error", "warn"].forEach(function (level) {
      var orig = window.console[level];
      if (typeof orig !== "function" || orig.__hpFeedbackWrapped) return;
      var wrapped = function () {
        try { push({ kind: "console", level: level }); } catch (e) {}
        return orig.apply(this, arguments);
      };
      wrapped.__hpFeedbackWrapped = true;
      window.console[level] = wrapped;
    });
  }

  /* ---------- action：捕获式点击（只记控件标识） ---------- */
  function controlOf(target) {
    var el = target;
    var depth = 0;
    while (el && depth < 6) {
      try {
        if (el.getAttribute) {
          var action = el.getAttribute("data-action");
          if (action) return { action: String(action), tag: tagOf(el) };
          var id = el.id || el.getAttribute("id");
          if (id) return { id: String(id), tag: tagOf(el) };
          var i18n = el.getAttribute("data-i18n") || el.getAttribute("data-i18n-aria");
          if (i18n) return { i18n: String(i18n), tag: tagOf(el) };
          var role = el.getAttribute("role");
          if (role) return { role: String(role), tag: tagOf(el) };
        }
      } catch (e) {}
      el = el.parentElement || el.parentNode || null;
      depth += 1;
    }
    return { tag: tagOf(target) };
  }

  function tagOf(el) {
    try {
      var tag = String((el && el.tagName) || "").toLowerCase();
      // 输入控件只记类型，绝不记 value / 文本
      if (tag === "input" || tag === "textarea" || tag === "select") {
        var type = "";
        try { type = String((el.getAttribute && el.getAttribute("type")) || ""); } catch (e) {}
        return tag + (type ? ":" + type : "");
      }
      return tag || "unknown";
    } catch (e) { return "unknown"; }
  }

  function wrapClicks() {
    if (!document || !document.addEventListener) return;
    document.addEventListener("click", function (e) {
      try { push({ kind: "action", control: controlOf(e && e.target) }); } catch (err) {}
    }, true);
  }

  /* ---------- nav：页面路径（不含查询串） ---------- */
  function pagePath() {
    try { return maskPath(window.location && window.location.pathname); } catch (e) { return "/"; }
  }

  function wrapNav() {
    push({ kind: "nav", path: pagePath(), phase: "load" });
    try {
      var hist = window.history;
      if (!hist) return;
      var wrap = function (name) {
        var orig = hist[name];
        if (typeof orig !== "function") return;
        hist[name] = function () {
          var out = orig.apply(this, arguments);
          push({ kind: "nav", path: pagePath(), phase: name });
          return out;
        };
      };
      wrap("pushState");
      wrap("replaceState");
      window.addEventListener("popstate", function () {
        push({ kind: "nav", path: pagePath(), phase: "popstate" });
      });
    } catch (e) { /* history 不可用：仅失去 SPA 路径跟踪 */ }
  }

  /* ---------- 对外 API ---------- */
  window.HP_FEEDBACK = {
    log: function (kind, data) {
      if (!kind) return;
      var evt = { kind: String(kind) };
      if (data && typeof data === "object") {
        for (var k in data) {
          if (Object.prototype.hasOwnProperty.call(data, k)) evt[k] = data[k];
        }
      }
      push(evt);
    },
    setCurrentSlide: function (id) { currentSlideId = id == null ? null : String(id); },
    snapshot: function () {
      prune();
      var lang = "";
      try {
        lang = (window.HP_I18N && window.HP_I18N.getLang && window.HP_I18N.getLang()) ||
          (document.documentElement && document.documentElement.lang) || "";
      } catch (e) {}
      return {
        captured_at: now(),
        url_path: pagePath(),
        lang: lang,
        viewport: {
          w: (window.innerWidth || 0),
          h: (window.innerHeight || 0),
        },
        user_agent: (window.navigator && window.navigator.userAgent) || "",
        current_slide_id: currentSlideId,
        events: events.slice(),
      };
    },
    events: function () { return events.slice(); },
    maskPath: maskPath,
  };

  /* ---------- 装配（越早越好；任何一步失败都不影响其余） ---------- */
  try { wrapFetch(); } catch (e) {}
  try { wrapErrors(); } catch (e) {}
  try { wrapConsole(); } catch (e) {}
  try { wrapClicks(); } catch (e) {}
  try { wrapNav(); } catch (e) {}
})(window, document);
