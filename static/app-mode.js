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

  var DEMO_CAPS = {
    upload: false,
    projects: false,
    unfiled: false,
    share: false,
    annotate: false,
    roi: false,
    saveImage: false,
    mpp: false,
    aiConfig: false,
    aiContinue: false,
    aiAsk: false,
    aiBranch: false,
    aiHistory: false,
    adminUsers: false,
    adminPlugins: false,
    adminBudget: false,
    logout: false,
    demoCatalog: true,
    demoQuota: true,
    loginCta: true,
    readonlyBadge: true,
    aiRun: true,
    viewTools: true,
    aiPanel: true,
  };

  var OFFICIAL_CAPS = {
    upload: true,
    projects: true,
    unfiled: true,
    share: true,
    annotate: true,
    roi: true,
    saveImage: true,
    mpp: true,
    aiConfig: true,
    aiContinue: true,
    aiAsk: true,
    aiBranch: true,
    aiHistory: true,
    adminUsers: true,
    adminPlugins: true,
    adminBudget: true,
    logout: true,
    demoCatalog: false,
    demoQuota: false,
    loginCta: false,
    readonlyBadge: false,
    aiRun: true,
    viewTools: true,
    aiPanel: true,
  };

  var caps = Object.assign(
    {},
    mode === "demo" ? DEMO_CAPS : OFFICIAL_CAPS,
    boot.capabilities || {}
  );

  function credFetch(url, opts) {
    opts = opts || {};
    if (!opts.credentials) opts.credentials = "same-origin";
    return fetch(url, opts);
  }

  function enc(id) {
    return encodeURIComponent(id == null ? "" : id);
  }

  function officialAdapter() {
    return {
      mode: "official",
      listSlides: function () { return credFetch("/api/slides"); },
      slideInfoUrl: function (id) { return "/api/slide/" + enc(id) + "/info"; },
      dziUrl: function (id) { return "/api/slide/" + enc(id) + ".dzi"; },
      thumbnailUrl: function (id) { return "/api/slide/" + enc(id) + "/thumbnail"; },
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
