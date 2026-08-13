/* =========================================================================
   HistoPilot UI bundle —— AI 配置表单（load/render/fill/save）

   直接读写 /api/ai/config（同源网关），不依赖平台 apiFetch。配置脱敏回填、高级调优
   校验、window_tier 提交 null（手动模式）语义与原平台实现逐条一致。
   ========================================================================= */
(function () {
  "use strict";
  var HP = window.HistoPilot;
  var S = HP.s;
  var apiFetch = HP.api, t = HP.t, toast = HP.toast;

  function isUser() { return S.role === "user"; }

  // 把高级调优输入区 readonly/disabled（user 只读平台值，由管理员配置）
  function applyUserReadonlyTuning(user) {
    if (!S.els) return;
    var ids = ["aiMaxSteps", "aiWindowTier", "aiCtxWindow", "aiReserve",
               "aiSafetyMargin", "aiKeepRecent", "aiForkLimit", "aiLeaseTtl",
               "aiApiProtocol"];
    for (var i = 0; i < ids.length; i++) {
      var el = S.els[ids[i]];
      if (el) {
        if (ids[i] === "aiWindowTier" || ids[i] === "aiApiProtocol") el.disabled = !!user;
        else el.readOnly = !!user;
      }
    }
  }

  // 加载 AI 配置（GET /api/ai/config，api_key 脱敏）
  function loadAiConfig() {
    return apiFetch("/api/ai/config").then(function (r) { return r.json(); }).then(function (cfg) {
      S.aiConfig = cfg;
      renderAiConfigState();
      return cfg;
    }).catch(function () { /* 静默，面板里会提示未配置 */ });
  }

  function renderAiConfigState() {
    var aiConfig = S.aiConfig;
    var els = S.els;
    if (!aiConfig) return;
    // user：凭据区显示 use_platform 勾选 + 自己的 base_url/model/api_key
    if (isUser()) {
      if (els.aiUsePlatformWrap) els.aiUsePlatformWrap.style.display = "block";
      if (els.aiUsePlatform) {
        els.aiUsePlatform.checked = !!aiConfig.use_platform;
        // 平台未配置官方 API 时，无法走平台 → 禁用 use_platform 并提示
        if (aiConfig.platform_configured) {
          els.aiUsePlatform.disabled = false;
          if (els.aiUsePlatformWrap) els.aiUsePlatformWrap.title = "";
        } else {
          els.aiUsePlatform.disabled = true;
          if (els.aiUsePlatformWrap) {
            els.aiUsePlatformWrap.title = t("ai.config.platform.notconfigured");
          }
        }
      }
      els.aiBaseUrl.value = aiConfig.base_url || "";
      els.aiModel.value = aiConfig.model || "";
      fillAiTuningFields();
      return;
    }
    var configured = !!(aiConfig.base_url && aiConfig.api_key_set);
    if (configured) {
      els.aiConfigWrap.style.display = "none";
      els.aiConfigCollapsed.style.display = "flex";
      els.aiConfigSummary.textContent =
        (aiConfig.model || t("ai.config.no.model")) + " @ " + aiConfig.base_url;
    } else {
      els.aiConfigWrap.style.display = "block";
      els.aiConfigCollapsed.style.display = "none";
      els.aiBaseUrl.value = aiConfig.base_url || "";
      els.aiModel.value = aiConfig.model || "";
      fillAiTuningFields();
    }
  }

  // 把调优参数回填到表单（用 aiConfig 当前值或文档默认）
  function fillAiTuningFields() {
    var aiConfig = S.aiConfig;
    var els = S.els;
    if (!aiConfig) return;
    els.aiMaxSteps.value = aiConfig.max_steps != null ? aiConfig.max_steps : 50;
    if (els.aiApiProtocol) els.aiApiProtocol.value = aiConfig.api_protocol || "openai";
    // window_tier：null → 空选项（"不启用（手动配置）"），真实回显手动模式；
    // 默认 balanced 只在后端 DEFAULT_CONFIG 层体现（全新安装 GET 返回 balanced）。
    if (els.aiWindowTier) els.aiWindowTier.value = aiConfig.window_tier != null ? aiConfig.window_tier : "";
    if (els.aiCtxWindow) els.aiCtxWindow.value = aiConfig.context_window_tokens != null ? aiConfig.context_window_tokens : "";
    if (els.aiReserve) els.aiReserve.value = aiConfig.reserve_tokens != null ? aiConfig.reserve_tokens : "";
    if (els.aiSafetyMargin) els.aiSafetyMargin.value = aiConfig.safety_margin != null ? aiConfig.safety_margin : "";
    if (els.aiKeepRecent) els.aiKeepRecent.value = aiConfig.keep_recent_tokens != null ? aiConfig.keep_recent_tokens : "";
    if (els.aiForkLimit) els.aiForkLimit.value = aiConfig.fork_active_limit != null ? aiConfig.fork_active_limit : "";
    if (els.aiLeaseTtl) els.aiLeaseTtl.value = aiConfig.lease_ttl != null ? aiConfig.lease_ttl : "";
  }

  // 保存配置（PUT /api/ai/config）
  function saveAiConfig() {
    var els = S.els;
    var payload = {
      base_url: els.aiBaseUrl.value.trim(),
      model: els.aiModel.value.trim(),
    };
    var keyVal = els.aiApiKey.value;
    if (keyVal !== "") { payload.api_key = keyVal; }
    if (isUser()) {
      // user：只提交凭据四字段（use_platform 勾选），调优字段由管理员配置
      if (els.aiUsePlatform) payload.use_platform = els.aiUsePlatform.checked;
    } else {
      // owner：全字段（现状不变）
      var MAX_STEPS = 500;
      var steps = parseInt(els.aiMaxSteps.value, 10);
      if (isNaN(steps) || steps < 1 || steps > MAX_STEPS || String(steps) !== els.aiMaxSteps.value.trim()) {
        toast(t("ai.config.steps.range", { max: MAX_STEPS }), "error");
        els.aiMaxSteps.focus();
        return;
      }
      payload.max_steps = steps;
      if (els.aiApiProtocol) { payload.api_protocol = els.aiApiProtocol.value || "openai"; }
      // window_tier：选中档位提交字符串，选中空档（"不启用（手动配置）"）提交 null（手动模式）。
      if (els.aiWindowTier) { payload.window_tier = els.aiWindowTier.value !== "" ? els.aiWindowTier.value : null; }
      var advFields = [
        ["context_window_tokens", els.aiCtxWindow, "pos"],
        ["reserve_tokens", els.aiReserve, "reserve"],
        ["safety_margin", els.aiSafetyMargin, "nonneg"],
        ["keep_recent_tokens", els.aiKeepRecent, "nonneg"],
        ["fork_active_limit", els.aiForkLimit, "intpos"],
        ["lease_ttl", els.aiLeaseTtl, "intpos"],
      ];
      var fieldLabel = {};
      function labelFor(key) {
        if (fieldLabel[key]) return fieldLabel[key];
        var el = { context_window_tokens: els.aiCtxWindow, reserve_tokens: els.aiReserve,
                   safety_margin: els.aiSafetyMargin, keep_recent_tokens: els.aiKeepRecent,
                   fork_active_limit: els.aiForkLimit, lease_ttl: els.aiLeaseTtl }[key];
        if (el && el.parentElement) {
          var sp = el.parentElement.querySelector("span");
          if (sp && sp.textContent) { fieldLabel[key] = sp.textContent.trim(); return fieldLabel[key]; }
        }
        return key;
      }
      var parsed = {};
      for (var ai = 0; ai < advFields.length; ai++) {
        var entry = advFields[ai];
        var fkey = entry[0], fel = entry[1], fkind = entry[2];
        if (!fel) continue;
        var raw = String(fel.value || "").trim();
        if (raw === "") continue;
        var num = Number(raw);
        if (!isFinite(num)) { toast(t("ai.config.num.invalid", { f: labelFor(fkey) }), "error"); fel.focus(); return; }
        if (fkind === "intpos") {
          if (!/^\d+$/.test(raw) || num < 1) { toast(t("ai.config.num.int", { f: labelFor(fkey) }), "error"); fel.focus(); return; }
        } else if (fkind === "reserve") {
          var RESERVE_MIN = 128;
          if (!/^\d+$/.test(raw) || num < RESERVE_MIN) { toast(t("ai.config.reserve.min", { min: RESERVE_MIN }), "error"); fel.focus(); return; }
        } else if (fkind === "pos") {
          if (!(num > 0)) { toast(t("ai.config.num.positive", { f: labelFor(fkey) }), "error"); fel.focus(); return; }
        } else if (fkind === "nonneg") {
          if (!(num >= 0)) { toast(t("ai.config.num.nonneg", { f: labelFor(fkey) }), "error"); fel.focus(); return; }
        }
        parsed[fkey] = num;
        payload[fkey] = num;
      }
      if (parsed.context_window_tokens != null && parsed.reserve_tokens != null && parsed.keep_recent_tokens != null) {
        if (parsed.reserve_tokens + parsed.keep_recent_tokens >= parsed.context_window_tokens) {
          toast(t("ai.config.ctx.insufficient"), "error");
          els.aiCtxWindow.focus();
          return;
        }
      }
    }
    els.aiConfigHint.textContent = t("ai.config.saving");
    els.aiConfigSave.disabled = true;
    apiFetch("/api/ai/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (raw) {
          var msg = "";
          try { var body = JSON.parse(raw || ""); if (body && body.error) msg = String(body.error); } catch (e) {}
          if (!msg) {
            var title = (raw || "").match(/<title[^>]*>([^<]+)<\/title>/i);
            if (title && title[1]) msg = "HTTP " + r.status + " " + title[1].replace(/^\d+\s*/, "");
            else msg = "HTTP " + r.status;
          }
          throw new Error(msg);
        });
      }
      return r.json();
    }).then(function (cfg) {
      S.aiConfig = cfg;
      renderAiConfigState();
      els.aiApiKey.value = "";
      els.aiConfigHint.textContent = t("ai.config.saved.hint");
      toast(t("ai.config.saved"), "success");
    }).catch(function (e) {
      els.aiConfigHint.textContent = "";
      toast(t("ai.config.save.fail", { e: (e && e.message ? e.message : e) }), "error");
    }).then(function () { els.aiConfigSave.disabled = false; });
  }

  HP.loadAiConfig = loadAiConfig;
  HP.renderAiConfigState = renderAiConfigState;
  HP.fillAiTuningFields = fillAiTuningFields;
  HP.saveAiConfig = saveAiConfig;
  HP.applyUserReadonlyTuning = applyUserReadonlyTuning;
})();
