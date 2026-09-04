/* =========================================================================
   PathTogether 运行模式 + API adapter

   Demo 不是另一套前端：同一套外壳/Viewer 通过 mode=demo|official 切换。
   安全边界在服务端 capability 与 /api/demo/* 写拒绝；前端 capabilities 只负责
   不渲染上传/标注/分享/配置/分支等入口，降低误操作。
   ========================================================================= */
(function (root) {
  "use strict";

  var boot = root.HP_APP_BOOTSTRAP || {};
  var mode = boot.mode === "demo" ? "demo" : "official";

  // capabilities 只信服务端下发（HP_APP_BOOTSTRAP.capabilities，snake_case，
  // app.py _app_capabilities()）：不前端硬编码默认字典（旧 DEMO_CAPS/
  // OFFICIAL_CAPS 已删——键名 camelCase 与服务端不一致且 OFFICIAL_CAPS 静态
  // 写死 annotate:true 对分享访客是错的，属无人消费的死代码）。
  var caps = Object.assign({}, boot.capabilities || {});

  function credFetch(url, opts) {
    opts = opts || {};
    if (!opts.credentials) opts.credentials = "same-origin";
    return fetch(url, opts);
  }

  function enc(id) {
    return encodeURIComponent(id == null ? "" : id);
  }

  // CSRF 双提交（与 app.js apiFetch 同一设施）：非安全方法附 X-CSRF-Token。
  // Demo 走 capability cookie（/api/demo/* 在 app.py CSRF 豁免前缀内），不带。
  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  // 瓦片/缩略图/crop 的 render 查询串（§6.3 资源端点 ?render=<token>）。
  // token 为空（RGB/legacy）时返回 ""，URL 与旧路径完全一致。
  function renderQuery(renderToken) {
    return renderToken ? "?render=" + encodeURIComponent(renderToken) : "";
  }

  function officialAdapter() {
    return {
      mode: "official",
      listSlides: function () { return credFetch("/api/slides"); },
      slideInfoUrl: function (id) { return "/api/slide/" + enc(id) + "/info"; },
      dziUrl: function (id) { return "/api/slide/" + enc(id) + ".dzi"; },
      thumbnailUrl: function (id, renderToken) {
        return "/api/slide/" + enc(id) + "/thumbnail" + renderQuery(renderToken);
      },
      // ---- Batch 4 多通道（§8.2 adapter 扩展；HP_Channels 消费）----
      // 服务端规范化（§6.2）：POST render-context，返回 canonical context +
      // fingerprint + render_token。flag 关时端点 403 multichannel_disabled，
      // 通道 UI 不会走到这里（info 无 render_context_endpoint 能力）。
      normalizeRenderContext: function (id, body) {
        var headers = { "Content-Type": "application/json" };
        var tok = csrfToken();
        if (tok) headers["X-CSRF-Token"] = tok;
        return credFetch("/api/slide/" + enc(id) + "/render-context", {
          method: "POST",
          headers: headers,
          body: JSON.stringify(body || {}),
        });
      },
      tileUrl: function (id, level, x, y, renderToken) {
        return "/api/slide/" + enc(id) + "_files/" + level + "/" + x + "_" + y +
          ".jpeg" + renderQuery(renderToken);
      },
      cropUrl: function (id, x, y, size, renderToken) {
        // crop 已带 query（x/y/size），render 参数用 & 追加
        return "/api/slide/" + enc(id) + "/crop?x=" + x + "&y=" + y +
          "&size=" + size +
          (renderToken ? "&render=" + encodeURIComponent(renderToken) : "");
      },
    };
  }

  function demoAdapter() {
    return {
      mode: "demo",
      config: function () { return credFetch("/api/demo/config"); },
      listSlides: function () { return credFetch("/api/demo/slides"); },
      slideInfo: function (id) { return credFetch("/api/demo/slides/" + enc(id) + "/info"); },
      slideInfoUrl: function (id) { return "/api/demo/slides/" + enc(id) + "/info"; },
      dziUrl: function (id) { return "/api/demo/slides/" + enc(id) + ".dzi"; },
      thumbnailUrl: function () { return ""; },
      // ---- Batch 4 多通道（§8.2）：Demo 面向匿名 capability cookie，
      // /api/demo/* 不套 CSRF（与 aiRun 同一鉴权语义）----
      normalizeRenderContext: function (id, body) {
        return credFetch("/api/demo/slides/" + enc(id) + "/render-context", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body || {}),
        });
      },
      tileUrl: function (id, level, x, y, renderToken) {
        return "/api/demo/slides/" + enc(id) + "_files/" + level + "/" + x + "_" + y +
          ".jpeg" + renderQuery(renderToken);
      },
      // Demo 无缩略图/导出端点（thumbnailUrl 恒空、无 cropUrl）
      aiRun: function (body, opts) {
        opts = opts || {};
        return credFetch("/api/demo/ai/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body || {}),
          signal: opts.signal,
        });
      },
      aiSession: function (id) {
        return credFetch("/api/demo/ai/session/" + enc(id));
      },
      aiStreamUrl: function (id, afterSeq) {
        return "/api/demo/ai/session/" + enc(id) +
          "/stream?after_seq=" + (afterSeq == null ? 0 : afterSeq);
      },
    };
  }

  root.HP_APP_MODE = mode;
  root.HP_CAPABILITIES = caps;
  root.HP_API = mode === "demo" ? demoAdapter() : officialAdapter();
  root.HP_can = function (name) { return !!caps[name]; };
})(window);
