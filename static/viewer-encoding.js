/* =========================================================================
   viewer 画质档（image-transport-upgrade §3.3/§5.2）：三入口共用。

   职责与边界（与 render context 严格分离）：
   - 消费 info.display（服务端白名单 + 每档 display_version）；
   - 画质偏好仅存浏览器端且按模式隔离（"rgb"/"mc" 各自一份），不写
     render_context、不参与 render fingerprint、不发 hp-render-context-changed、
     不重绑 AI 会话、不重抓 AI 概览；
   - 多通道恒为「荧光保真」（服务端只给 preserve 档），无会降 4:2:0 的省流档；
   - tile/thumbnail URL 的 ?profile=&dv= 由本模块拼装；旧服务端（info 无
     display 能力）一律回退旧 URL 并隐藏画质切换（仅「成功 info 明确缺字段」
     才算旧服务端；网络错误/403/畸形不伪装）；
   - 409 display_version_conflict 有界恢复：合并同一 viewer 的并发失败瓦片，
     错误路径单次带 credentials 诊断请求（正常请求绝不下载两遍）。episode
     以「可见版本化瓦片成功」为界：viewer.open 本身不清零计数（恢复重开
     会再发 open，但瓦片仍可能失败）；只有 tile-loaded 且 URL 含 profile=
     才结束 episode。同一 episode 第二次 409 停止自动重试；401/403 不降级。
   ========================================================================= */
(function (root) {
  "use strict";

  var PREF_KEY = "pt.viewerQuality.";   // + "rgb" | "mc"
  var PROFILE_RGB_STANDARD = "native-standard-v1";
  var PROFILE_RGB_DETAIL = "native-detail-v1";
  var PROFILE_MC = "fluorescence-preserve-v1";
  var PROFILE_RGB_THUMB = "native-thumb-v1";
  var PROFILE_MC_THUMB = "fluorescence-thumb-v1";

  var state = {
    available: false,      // 成功 info 明确带 display 能力
    imageMode: null,       // "native_rgb" | "multichannel"
    modeClass: null,       // "rgb" | "mc"
    tileVersions: {},      // profile_id -> dv（当前 context 的 tile purpose）
    thumbVersions: {},     // profile_id -> dv（thumbnail purpose）
    profiles: [],          // tile 档位描述（服务端白名单顺序）
    defaultProfile: null,
    selected: null,        // 当前 tile profile
    conflictCount: 0,      // 连续 409 恢复次数（有界）
  };

  var ui = { host: null, control: null, t: null, toast: null,
             onQualityReopen: null };

  function loadPref(modeClass) {
    try {
      return root.localStorage.getItem(PREF_KEY + modeClass);
    } catch (e) { return null; }
  }

  function savePref(modeClass, profileId) {
    try {
      root.localStorage.setItem(PREF_KEY + modeClass, profileId);
    } catch (e) { /* 隐私模式等：偏好不持久化，不影响会话内行为 */ }
  }

  function resetState() {
    state.available = false;
    state.imageMode = null;
    state.modeClass = null;
    state.tileVersions = {};
    state.thumbVersions = {};
    state.profiles = [];
    state.defaultProfile = null;
    state.selected = null;
    state.conflictCount = 0;
  }

  /* info.display 到达（channel-controls.handleInfo 调用）。
     返回 {available, modeClass, selected} 供调用方渲染徽章/控制。 */
  function handleDisplay(display) {
    if (!display || !display.profiles || !display.profiles.length) {
      // 旧服务端 / 能力缺失：回退旧 URL，隐藏画质切换（不伪装）
      resetState();
      renderControl();
      return { available: false };
    }
    state.available = true;
    state.imageMode = display.image_mode === "multichannel"
      ? "multichannel" : "native_rgb";
    state.modeClass = state.imageMode === "multichannel" ? "mc" : "rgb";
    state.tileVersions = {};
    state.thumbVersions = {};
    state.profiles = [];
    (display.profiles || []).forEach(function (p) {
      if (p && p.profile_id && p.display_version) {
        state.tileVersions[p.profile_id] = p.display_version;
        state.profiles.push(p);
      }
    });
    ((display.thumbnail && display.thumbnail.profiles) || [])
      .forEach(function (p) {
        if (p && p.profile_id && p.display_version) {
          state.thumbVersions[p.profile_id] = p.display_version;
        }
      });
    state.defaultProfile = display.default_profile
      || (state.profiles[0] && state.profiles[0].profile_id) || null;
    // 偏好仅按模式隔离存取；缺失/非法回默认（多通道恒 preserve，不存偏好）
    var allowed = state.profiles.map(function (p) { return p.profile_id; });
    if (state.modeClass === "mc") {
      state.selected = allowed.indexOf(PROFILE_MC) >= 0
        ? PROFILE_MC : state.defaultProfile;
    } else {
      var pref = loadPref("rgb");
      state.selected = allowed.indexOf(pref) >= 0 ? pref : state.defaultProfile;
    }
    renderControl();
    return { available: true, modeClass: state.modeClass,
             selected: state.selected, profiles: state.profiles,
             isDefault: state.selected === state.defaultProfile };
  }

  /* render-context 响应 display_versions（自定义 context fp 的 dv）到达。
     tile 与 thumbnail 的 profile 词表不相交，单 map 分派安全。 */
  function handleDisplayVersions(dvs) {
    if (!dvs || typeof dvs !== "object") return;
    Object.keys(dvs).forEach(function (pid) {
      if (!dvs[pid]) return;
      if (pid in state.tileVersions) state.tileVersions[pid] = dvs[pid];
      if (pid in state.thumbVersions) state.thumbVersions[pid] = dvs[pid];
    });
  }

  /* tile URL 附加 query（"" 或 "?profile=..&dv=.."；由 adapter 拼进 URL）。 */
  function tileQuery() {
    if (!state.available || !state.selected) return "";
    var dv = state.tileVersions[state.selected];
    if (!dv) return "";
    return "?profile=" + encodeURIComponent(state.selected)
      + "&dv=" + encodeURIComponent(dv);
  }

  /* thumbnail URL 附加 query：purpose 对应的 profile（RGB→thumb、MC→preserve）。 */
  function thumbnailQuery() {
    if (!state.available) return "";
    var pid = state.modeClass === "mc" ? PROFILE_MC_THUMB : PROFILE_RGB_THUMB;
    var dv = state.thumbVersions[pid];
    if (!dv) return "";
    return "?profile=" + encodeURIComponent(pid)
      + "&dv=" + encodeURIComponent(dv);
  }

  function available() { return state.available; }

  function selectedProfile() { return state.selected; }

  function modeClass() { return state.modeClass; }

  /* 真实 OSD 5.0.1 tile-load-failed / tile-loaded 载荷是
     { tile, tiledImage, time, message, tileRequest }，URL 在 tile.getUrl()。 */
  function versionedTileUrl(ev) {
    var tile = ev && ev.tile;
    if (!tile || typeof tile.getUrl !== "function") return "";
    try {
      var url = tile.getUrl();
      return typeof url === "string" ? url : "";
    } catch (e) {
      return "";
    }
  }

  /* 用户切换画质（仅 RGB 两档）。持久化偏好并请求轻量重开（不改 context）。 */
  function setPreference(profileId) {
    var allowed = state.profiles.map(function (p) { return p.profile_id; });
    if (allowed.indexOf(profileId) < 0) return false;
    state.selected = profileId;
    savePref(state.modeClass || "rgb", profileId);
    renderControl();
    return true;
  }

  // ------------------------------------------------------------------ #
  // 409 display_version_conflict 有界恢复（§5.2）
  // ------------------------------------------------------------------ //
  function installConflictRecovery(opts) {
    opts = opts || {};
    var viewer = opts.viewer || null;
    if (!viewer || !viewer.addHandler) return;
    var episodeAt = 0;      // 合并窗口起点（同一 viewer 的并发失败只诊断一次）
    var diagnosing = false;
    viewer.addHandler("tile-loaded", function (ev) {
      // 确认新版本瓦片已经可见后才结束当前 409 episode；open 本身不算成功。
      var loaded = versionedTileUrl(ev);
      if (loaded && loaded.indexOf("profile=") >= 0) state.conflictCount = 0;
    });
    viewer.addHandler("tile-load-failed", function (ev) {
      var url = versionedTileUrl(ev);
      if (!url || url.indexOf("profile=") < 0) return;  // 仅版本化 URL 适用
      var now = Date.now();
      if (diagnosing || now - episodeAt < 1500) return;  // 合并并发失败瓦片
      episodeAt = now;
      diagnosing = true;
      // OSD 默认 loader 读不到错误 JSON：单次带 credentials 的诊断请求
      // （只在错误路径发生，正常请求不下载两遍）
      root.fetch(url, { credentials: "same-origin", cache: "no-store" })
        .then(function (r) {
          diagnosing = false;
          if (r.status === 409) {
            state.conflictCount += 1;
            if (state.conflictCount > 1) {
              // 第二次冲突停止自动重试：显示可重试错误，不无限刷新
              if (ui.toast && ui.t) {
                ui.toast(ui.t("quality.conflict.stuck"), "error");
              }
              return;
            }
            if (typeof opts.onConflict === "function") opts.onConflict();
          } else if (r.status === 401 || r.status === 403) {
            // 权限错误不降级（不回退旧 URL 掩盖失败）
            if (ui.toast && ui.t) {
              ui.toast(ui.t("quality.auth.denied"), "error");
            }
          }
          // 其它状态（5xx/网络）：OSD 自身失败语义已呈现，不额外动作
        })
        .catch(function () { diagnosing = false; });
    });
  }

  // ------------------------------------------------------------------ #
  // 控件渲染：RGB「标准 / 精细」分段；多通道「荧光保真」标识（不可切）
  // ------------------------------------------------------------------ //
  function ensureControl() {
    if (!ui.host) return null;
    if (ui.control) return ui.control;   // 已创建：复用（renderControl 可重入）
    var ctl = root.document.createElement("div");
    ctl.className = "viewer-quality-control";
    ctl.setAttribute("role", "group");
    ui.host.appendChild(ctl);
    ui.control = ctl;
    return ctl;
  }

  function renderControl() {
    var ctl = ensureControl();
    if (!ctl) return;
    if (!state.available) {
      ui.host.hidden = true;
      ctl.textContent = "";
      return;
    }
    ui.host.hidden = false;
    ctl.textContent = "";
    var t = ui.t || function (k) { return k; };
    if (state.modeClass === "mc") {
      // 多通道：荧光保真（恒 q95/4:4:4，无省流档）
      var badge = root.document.createElement("span");
      badge.className = "viewer-quality-badge";
      badge.setAttribute("data-i18n", "quality.mc.badge");
      badge.setAttribute("role", "note");
      badge.tabIndex = 0;
      badge.textContent = t("quality.mc.badge");
      badge.title = t("quality.mc.tip");
      badge.setAttribute("aria-label", t("quality.mc.tip"));
      ctl.appendChild(badge);
      return;
    }
    var tip = t("quality.detail.tip");
    [{ pid: PROFILE_RGB_STANDARD, label: t("quality.standard"),
       key: "quality.standard" },
     { pid: PROFILE_RGB_DETAIL, label: t("quality.detail"),
       key: "quality.detail" }].forEach(function (item) {
      var btn = root.document.createElement("button");
      btn.type = "button";
      btn.className = "viewer-quality-btn"
        + (state.selected === item.pid ? " active" : "");
      btn.setAttribute("data-i18n", item.key);
      btn.textContent = item.label;
      if (item.pid === PROFILE_RGB_DETAIL) btn.title = tip;
      btn.addEventListener("click", function () {
        if (state.selected === item.pid) return;
        setPreference(item.pid);
        if (typeof ui.onQualityReopen === "function") ui.onQualityReopen();
      });
      ctl.appendChild(btn);
    });
  }

  /* 页面初始化：注入宿主/文案/toast 与画质重开回调。 */
  function mount(opts) {
    opts = opts || {};
    ui.host = opts.host || null;
    ui.t = opts.t || null;
    ui.toast = opts.toast || null;
    ui.onQualityReopen = opts.onQualityReopen || null;
    if (ui.host) ui.host.hidden = true;
    renderControl();
    try {
      root.document.addEventListener("hp-lang-change", function () {
        renderControl();
      });
    } catch (e) { /* 测试环境无 document */ }
  }

  root.HP_ViewerEncoding = {
    PROFILE_RGB_STANDARD: PROFILE_RGB_STANDARD,
    PROFILE_RGB_DETAIL: PROFILE_RGB_DETAIL,
    PROFILE_MC: PROFILE_MC,
    handleDisplay: handleDisplay,
    handleDisplayVersions: handleDisplayVersions,
    tileQuery: tileQuery,
    thumbnailQuery: thumbnailQuery,
    available: available,
    selectedProfile: selectedProfile,
    modeClass: modeClass,
    setPreference: setPreference,
    installConflictRecovery: installConflictRecovery,
    mount: mount,
    // 测试辅助
    _state: state,
  };
})(window);
