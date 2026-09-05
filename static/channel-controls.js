/* =========================================================================
   多通道「通道着色」共用组件（Batch 4，规格 §4.1–4.4 / §5.2 / §8.1–8.3）

   三种查看器页面（主站 app.js / Demo demo.js / 分享 share.js）共同加载本文件，
   各自只提供 adapter 与鉴权，不复制算法（§8.1）：

     - normalizeChannelInfo(info)       归一化 info 的 render additive 字段；
       flag 关（server_capability.render_context_endpoint !== true）时只回
       探测字段——页面必须保持原 viewer、不发新字段、不显示通道面板；
     - createDeepZoomTileSource(...)    OpenSeadragon inline custom TileSource
       （width/height/getTileUrl，§7.3：不依赖 DZI XML 是否保留 query）；
     - 通道说明卡渲染 / 键盘操作 / 颜色 picker（控件不只靠颜色传达状态：
       文本 + 勾选态 + 焦点样式 + aria-label，§4.2）；
     - localStorage 本地偏好（§8.3：key 含用户作用域 + 切片安全名 + asset
       revision；只存用户选择不存 token；失效只回默认 + 一次非阻塞提示）；
     - 当前 context 与 AI context fingerprint 比较 hook（真正的 AI 闭环是
       Batch 5，本文件只留 state.renderFingerprint / aiRenderFingerprint）。

   颜色变化的更新顺序（§8.2，applySelection 固定实现）：
     取消旧 tile 请求/关闭旧 world item（viewer.close()）
       → POST 服务端规范化 → 更新 renderContext/Fingerprint/Token
       → 换缩略图 → 打开新 TileSource → open 后恢复 viewport
       center/zoom/rotation/flip → 更新 AI 同步徽章占位。
   epoch 计数保证「旧请求晚到不得覆盖新 context」：快速连点时只有最后一次
   响应会被应用。
   ========================================================================= */
(function (root) {
  "use strict";

  // 与服务端 slide_render.py 同口径（§5.2）；只保证显示区分度，不声明荧光团
  var PALETTE = [
    "#00FFFF", "#FF00FF", "#FFD166", "#00E676",
    "#FF5C5C", "#4D7CFE", "#FF8C42", "#B388FF",
  ];
  var MAX_ACTIVE_CHANNELS = 8;    // 一次最多启用（§4.2；服务端 render_channel_limit）
  var DEFAULT_ACTIVE_CHANNELS = 4; // 默认最多启用前 4 个有效通道
  var HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/;
  // 服务端 manifest 对 Name 缺失的通道回填「通道 N」（1 基，slide_render.py
  // build_channel_manifest）；前端据此判定「有名称」（不猜荧光团）。
  var SERVER_UNNAMED_RE = /^通道 \d+$/;

  function isHexColor(s) {
    return typeof s === "string" && HEX_COLOR_RE.test(s);
  }

  function num(v) {
    return typeof v === "number" && isFinite(v) ? v : null;
  }

  // ------------------------------------------------------------------ #
  // normalizeChannelInfo（§6.1 additive 字段归一化）
  // ------------------------------------------------------------------ #
  function normalizeChannel(raw, i) {
    raw = raw || {};
    var idx = typeof raw.index === "number" ? raw.index : i;
    var name = typeof raw.name === "string" ? raw.name.trim() : "";
    var nameMissing = !name || SERVER_UNNAMED_RE.test(name);
    var source = raw.color_source === "ome" ? "ome" : "default";
    var color = isHexColor(raw.color) ? String(raw.color).toUpperCase() : null;
    if (!color) {
      // 缺失/非法 → 确定性色卡（与服务端同卡；来源降级为 default）
      color = PALETTE[idx % PALETTE.length];
      source = "default";
    }
    var alpha = num(raw.alpha);
    if (alpha == null || alpha < 0) alpha = 1;
    if (alpha > 1) alpha = 1;
    var inten = raw.intensity && typeof raw.intensity === "object" ? raw.intensity : null;
    return {
      index: idx,
      id: raw.id != null ? String(raw.id) : null,
      name: name,
      nameMissing: nameMissing,
      color: color,
      alpha: alpha,
      colorSource: source,           // "ome" | "default"（用户调整由 controller 覆盖层记录）
      defaultActive: !!raw.default_active,
      // empty_or_constant 通道启用会被服务端 400（canonicalize_selection）→
      // 前端直接禁用开关
      displayable: !(inten && inten.status === "empty_or_constant"),
      intensity: inten,
      dtype: raw.dtype != null ? String(raw.dtype) : null,
    };
  }

  function normalizeChannelInfo(info) {
    info = info || {};
    var cap = info.server_capability || {};
    // flag 判定唯一口径：render-context 规范化端点是否可用（flag 关时服务端
    // 不下发 channels/default_*，info 只有探测字段，§15.2 部署顺序）
    var flagEnabled = cap.render_context_endpoint === true;
    var out = {
      flagEnabled: flagEnabled,
      imageMode: info.image_mode === "multichannel" ? "multichannel" : "native_rgb",
      multichannel: false,
      assetRevision: typeof info.asset_revision === "string" ? info.asset_revision : null,
      slideId: info.slide_id != null ? String(info.slide_id) : (info.name != null ? String(info.name) : null),
      channels: [],
      warnings: Array.isArray(info.warnings) ? info.warnings : [],
      plane: info.plane && typeof info.plane === "object" ? info.plane : null,
      axes: info.axes != null ? String(info.axes) : null,
      deepzoom: info.deepzoom && typeof info.deepzoom === "object" ? info.deepzoom : null,
      defaultContext: info.default_render_context || null,
      defaultToken: info.default_render_token || null,
      namedCount: 0,
      omeColorCount: 0,
    };
    if (!flagEnabled || out.imageMode !== "multichannel") return out;
    var raws = Array.isArray(info.channels) ? info.channels : [];
    out.channels = raws.map(normalizeChannel);
    // 没有 manifest 通道（理论不应发生）时按 RGB 处理，不出面板
    out.multichannel = out.channels.length > 0;
    out.channels.forEach(function (ch) {
      if (!ch.nameMissing) out.namedCount += 1;
      if (ch.colorSource === "ome") out.omeColorCount += 1;
    });
    return out;
  }

  // ------------------------------------------------------------------ #
  // OpenSeadragon inline custom TileSource（§7.3/§8.1）
  // OSD Viewer.open(plainObject) 对带 getTileUrl 的对象走
  //   new OpenSeadragon.TileSource(obj) + 拷贝 getTileUrl（已核对 5.0.1 源码），
  // width/height/tileSize/tileOverlap/minLevel/maxLevel 全部生效，ready 立即置真。
  // 瓦片 URL 由页面 adapter 拼接并携带 render token（缓存内容寻址，§7.3）。
  // ------------------------------------------------------------------ #
  function createDeepZoomTileSource(info, adapter, renderToken) {
    var dz = info.deepzoom || {};
    var slideId = info.slideId || info.slide_id || info.name;
    return {
      width: dz.width,
      height: dz.height,
      tileSize: dz.tile_size || 512,
      tileOverlap: dz.overlap != null ? dz.overlap : 1,
      minLevel: dz.min_level || 0,
      maxLevel: dz.max_level != null ? dz.max_level : 0,
      getTileUrl: function (level, x, y) {
        return adapter.tileUrl(slideId, level, x, y, renderToken);
      },
    };
  }

  // ------------------------------------------------------------------ #
  // fingerprint 比较（AI 同步徽章 hook；Batch 5 接管真实闭环）
  // ------------------------------------------------------------------ #
  function sameFingerprint(a, b) {
    return typeof a === "string" && typeof b === "string" && a === b;
  }

  // "synced" | "stale" | "unknown"（AI 侧 fingerprint 未知时 unknown——
  // Batch 5 前不显示徽章，避免伪造「AI 已看见」，§9.3）
  function syncState(currentFp, aiFp) {
    if (typeof currentFp !== "string" || typeof aiFp !== "string") return "unknown";
    return currentFp === aiFp ? "synced" : "stale";
  }

  // ------------------------------------------------------------------ #
  // 有效不可见（F4）：勾选但对合成贡献为 0 的通道如实标注原因，
  // 不自动改色（OME 黑色 DAPI 保持 #000000，用户可自行改色后变可见）。
  // 优先级：disabled > alpha_zero > empty_window > black_mapping。
  // 返回原因码或 null（可见）；ch 应为已合并用户覆盖后的有效通道。
  // ------------------------------------------------------------------ #
  function invisibleReason(ch, enabled) {
    if (!enabled) return "disabled";
    var inten = ch && ch.intensity && typeof ch.intensity === "object" ? ch.intensity : {};
    var alpha = num(ch && ch.alpha);
    if (alpha != null && alpha <= 0) return "alpha_zero";
    var black = num(inten.black);
    var white = num(inten.white);
    if (inten.status === "empty_or_constant" ||
        (black != null && white != null && white <= black)) {
      return "empty_window";
    }
    if (String(ch && ch.color).toUpperCase() === "#000000") return "black_mapping";
    return null;
  }

  function effectiveVisible(ch, enabled) {
    return invisibleReason(ch, enabled) == null;
  }

  // 原因码 → i18n key 尾段（disabled 不出文案：未勾选的行不提示）
  var INVISIBLE_KEY = {
    alpha_zero: "alpha",
    empty_window: "window",
    black_mapping: "black",
  };

  // ------------------------------------------------------------------ #
  // 通道选择（§4.2：默认前 4 个有效通道；一次最多 8）
  // ------------------------------------------------------------------ #
  function isValidIndex(channels, idx) {
    return channels.some(function (c) { return c.index === idx; });
  }

  // 兼容 raw manifest 条目（displayable 未归一化时按 intensity.status 判定）
  function entryDisplayable(c) {
    if (c.displayable != null) return !!c.displayable;
    return !(c.intensity && c.intensity.status === "empty_or_constant");
  }

  function defaultSelection(channels) {
    // 服务端 manifest 的 default_active 已按「alpha>0 且强度窗可用」取前 4；
    // 旧/异常响应缺标记时前端按同一口径回退，不猜
    var alpha = function (c) { return num(c.alpha) == null ? 1 : c.alpha; };
    var flagged = channels.filter(function (c) {
      return c.defaultActive && alpha(c) > 0 && entryDisplayable(c);
    });
    var pool = flagged.length ? flagged : channels.filter(function (c) {
      return alpha(c) > 0 && entryDisplayable(c);
    });
    return pool.slice(0, DEFAULT_ACTIVE_CHANNELS).map(function (c) { return c.index; });
  }

  // 安全网：去重、剔除不存在索引、截到 8（UI 层会在第 9 个时阻止并提示）
  function clampSelection(channels, indexes) {
    var seen = {};
    var out = [];
    (Array.isArray(indexes) ? indexes : []).forEach(function (idx) {
      idx = Number(idx);
      if (!isFinite(idx) || seen[idx] || !isValidIndex(channels, idx)) return;
      seen[idx] = true;
      out.push(idx);
    });
    out.sort(function (a, b) { return a - b; });
    return out.slice(0, MAX_ACTIVE_CHANNELS);
  }

  // POST body 只提交用户选择（§6.2）：index 必填 + 用户改过的颜色；
  // alpha 在用户重选颜色时按 1（=alpha 255，§5.1）；black/white/gamma 省略，
  // 由服务端 manifest 全局强度窗补齐（禁止前端逐瓦片 min/max，§5.3）。
  function buildRequestBody(channels, selection, overrides) {
    var byIndex = {};
    channels.forEach(function (c) { byIndex[c.index] = c; });
    var active = (selection || []).slice().sort(function (a, b) { return a - b; })
      .map(function (idx) {
        var ch = byIndex[idx] || {};
        var ov = (overrides && overrides[idx]) || {};
        var item = { index: idx, color: ov.color || ch.color };
        if (ov.alpha != null) item.alpha = ov.alpha;
        return item;
      });
    return {
      active_channels: active,
      plane: { t: 0, z: 0 },   // 首版固定首平面（§3.2）
    };
  }

  // ------------------------------------------------------------------ #
  // localStorage 本地偏好（§8.3）
  // ------------------------------------------------------------------ #
  function storageKey(scope, slideSafeName, assetRevision) {
    // key 含用户作用域 + 切片安全名 + asset revision（不得只有文件名）；
    // revision 变化 → 新 key（旧选择自然丢弃回默认）。分隔符「|」保证可读且
    // 不与切片安全名/作用域冲突（localStorage key 无字符集限制）。
    return "pt.rc.v1|" + String(scope || "anonymous") +
      "|" + String(slideSafeName || "") +
      "|" + String(assetRevision || "");
  }

  function saveStoredSelection(storage, key, selection, overrides) {
    if (!storage || !key) return;
    try {
      // 只存用户选择（索引 + 颜色/alpha 覆盖），绝不存服务端签名 token
      storage.setItem(key, JSON.stringify({
        v: 1,
        selection: (selection || []).slice(0, MAX_ACTIVE_CHANNELS),
        overrides: overrides || {},
      }));
    } catch (e) { /* 存储满/禁用：非致命，忽略 */ }
  }

  // 解析失败只回默认 + broken 标记（调用方给一次非阻塞提示，§8.3）；
  // 索引/数量与当前通道不符时静默丢弃（不提示）。
  function loadStoredSelection(storage, key, channels) {
    var empty = { selection: null, overrides: {}, broken: false };
    if (!storage || !key) return empty;
    var raw = null;
    try { raw = storage.getItem(key); } catch (e) { return empty; }
    if (raw == null) return empty;
    var data;
    try { data = JSON.parse(raw); } catch (e) {
      return { selection: null, overrides: {}, broken: true };
    }
    if (!data || data.v !== 1 || !Array.isArray(data.selection) ||
        data.selection.length < 1 ||
        data.selection.length > MAX_ACTIVE_CHANNELS ||
        !data.selection.every(function (idx) { return isValidIndex(channels, Number(idx)); })) {
      return empty;   // 通道索引/数量/版本变化 → 丢弃回默认
    }
    var overrides = {};
    if (data.overrides && typeof data.overrides === "object") {
      Object.keys(data.overrides).forEach(function (k) {
        var idx = Number(k);
        if (!isValidIndex(channels, idx)) return;
        var ov = data.overrides[k];
        if (ov && typeof ov === "object") overrides[idx] = ov;
      });
    }
    return {
      selection: clampSelection(channels, data.selection.map(Number)),
      overrides: overrides,
      broken: false,
    };
  }

  // ------------------------------------------------------------------ #
  // 通道控制器（面板 UI + 应用流程）
  // ------------------------------------------------------------------ #
  function createChannelController(opts) {
    opts = opts || {};
    var adapter = opts.adapter || {};
    var viewer = opts.viewer || null;
    var t = opts.t || function (k, vars) {
      return k + (vars && vars.n != null ? ":" + vars.n + "/" + vars.m : "");
    };
    var toast = opts.toast || function () {};
    var doc = opts.doc || root.document;
    var storage = opts.storage != null ? opts.storage : root.localStorage;

    var ctrl = {
      epoch: 0,
      info: null,          // normalizeChannelInfo 结果
      slideMeta: null,     // {id, scope}
      selection: [],       // 启用的通道索引（升序，1..8）
      overrides: {},       // {index: {color?, alpha?}} 用户调整（来源「用户调整」）
      renderContext: null,
      renderFingerprint: null,
      renderToken: null,
      aiRenderFingerprint: null,   // Batch 5：由 AI 运行链路写入
      conflictRecovered: false,
      panelEls: null,
    };

    function hideChrome() {
      if (opts.button) opts.button.hidden = true;
      if (opts.badge) opts.badge.hidden = true;
      removePanel();
    }

    function removePanel() {
      // 宿主即面板根（模板/fixture 提供 #channel-panel 容器）：隐藏并清空，
      // 不再往 hidden 宿主里嵌套子面板（否则永远不可见）
      if (ctrl.panelEls && ctrl.panelEls.root) {
        var rootEl = ctrl.panelEls.root;
        try {
          while (rootEl.children.length) {
            rootEl.removeChild(rootEl.children[0]);
          }
        } catch (e) { /* 忽略 */ }
        rootEl.hidden = true;
      }
      ctrl.panelEls = null;
    }

    function el(tag, className, text) {
      var e = doc.createElement(tag);
      if (className) e.className = className;
      if (text != null) e.textContent = text;
      return e;
    }

    function hasPlaneWarning() {
      var info = ctrl.info;
      if (!info) return false;
      if (info.warnings.some(function (w) { return w && w.code === "first-plane-v1"; })) return true;
      var p = info.plane;
      return !!(p && ((p.size_t | 0) > 1 || (p.size_z | 0) > 1));
    }

    function originText(ch) {
      if (ctrl.overrides[ch.index] && ctrl.overrides[ch.index].color) {
        return t("channel.origin.user");
      }
      return ch.colorSource === "ome" ? t("channel.origin.ome") : t("channel.origin.default");
    }

    function channelName(ch) {
      return ch.nameMissing ? t("channel.n", { n: ch.index + 1 }) : ch.name;
    }

    // 有效通道：manifest 数据 + 用户覆盖（改色会把 alpha 归 1，§5.1）。
    // 有效不可见判定必须用这份合并结果，否则改色后原因不消失。
    function effectiveChannel(ch) {
      var ov = ctrl.overrides[ch.index] || {};
      return {
        index: ch.index,
        color: ov.color || ch.color,
        alpha: ov.alpha != null ? ov.alpha : ch.alpha,
        intensity: ch.intensity,
      };
    }

    function renderPanel() {
      removePanel();
      var info = ctrl.info;
      if (!info || !info.multichannel || !opts.panelHost) return;

      // 宿主元素即面板根（模板已提供 #channel-panel 容器）
      var rootEl = opts.panelHost;
      rootEl.className = "channel-panel";
      rootEl.setAttribute("role", "dialog");
      rootEl.setAttribute("aria-label", t("channel.panel.aria"));

      // 头部：标题 + 关闭（键盘可达）
      var header = el("div", "ch-header");
      header.appendChild(el("span", "ch-title", t("channel.panel.title")));
      var closeBtn = el("button", "ch-close", "×");
      closeBtn.type = "button";
      closeBtn.setAttribute("aria-label", t("channel.close.aria"));
      closeBtn.addEventListener("click", function () { setPanelOpen(false); });
      header.appendChild(closeBtn);
      rootEl.appendChild(header);

      // 语义边界说明（§2：通道着色只是显示映射，不是标注/染色事实）
      rootEl.appendChild(el("p", "ch-subtitle", t("channel.panel.subtitle")));

      // T/Z>1 持续提示（§4.2）
      if (hasPlaneWarning()) {
        var warn = el("div", "ch-plane-warn", t("channel.plane.warn"));
        warn.setAttribute("role", "status");
        rootEl.appendChild(warn);
      }

      // 「已显示 n/m 个通道」
      var count = el("div", "ch-count", t("channel.displayed",
        { n: ctrl.selection.length, m: info.channels.length }));
      rootEl.appendChild(count);

      // 元数据完整性摘要（§4.2）：如「4/6 有名称；2/6 有 OME 颜色；其余使用默认伪彩」
      var sep = (root.HP_I18N && HP_I18N.getLang && HP_I18N.getLang() === "en") ? "; " : "；";
      var parts = [
        t("channel.meta.named", { a: info.namedCount, m: info.channels.length }),
        t("channel.meta.ome", { b: info.omeColorCount, m: info.channels.length }),
      ];
      if (info.omeColorCount < info.channels.length) {
        parts.push(t("channel.meta.rest"));
      }
      rootEl.appendChild(el("div", "ch-meta", parts.join(sep)));

      // AI 同步徽章占位（Batch 5 接管；fingerprint 未知时不显示）
      var aiBadge = el("div", "ch-ai-badge");
      aiBadge.setAttribute("role", "status");
      rootEl.appendChild(aiBadge);

      // 通道行（开关 / 名称 / 索引 / 色块 / 颜色来源）
      var list = el("div", "ch-list");
      var rows = [];
      info.channels.forEach(function (ch) {
        var row = el("div", "ch-row" + (ch.displayable ? "" : " ch-row-disabled"));
        row.dataset.index = String(ch.index);

        var name = channelName(ch);
        var cb = doc.createElement("input");
        cb.type = "checkbox";
        cb.checked = ctrl.selection.indexOf(ch.index) >= 0;
        cb.disabled = !ch.displayable;
        cb.id = "ch-cb-" + ch.index;
        cb.setAttribute("aria-label", t("channel.toggle.aria", { name: name }));
        cb.addEventListener("change", function () {
          ctrl.setChannelActive(ch.index, cb.checked);
        });
        row.appendChild(cb);

        var textWrap = el("label", "ch-label");
        textWrap.setAttribute("for", cb.id);
        var nameEl = el("span", "ch-name", name);
        textWrap.appendChild(nameEl);
        textWrap.appendChild(el("span", "ch-index", "C" + ch.index));
        row.appendChild(textWrap);

        var color = doc.createElement("input");
        color.type = "color";
        color.className = "ch-color";
        color.value = (ctrl.overrides[ch.index] && ctrl.overrides[ch.index].color) || ch.color;
        color.setAttribute("aria-label", t("channel.color.aria", { name: name }));
        color.addEventListener("change", function () {
          ctrl.setChannelColor(ch.index, color.value);
        });
        row.appendChild(color);

        row.appendChild(el("span", "ch-origin", originText(ch)));
        // 有效不可见（F4）：勾选但对合成贡献为 0 → 色块旁短文本标注原因；
        // 不自动改色，checkbox 仍可勾选（用户改色后即变可见）
        var active = ctrl.selection.indexOf(ch.index) >= 0;
        var reason = active ? invisibleReason(effectiveChannel(ch), true) : null;
        if (active && reason && INVISIBLE_KEY[reason]) {
          row.className += " ch-invisible";
          var reasonEl = el("span", "ch-invisible-reason",
            t("channel.invisible." + INVISIBLE_KEY[reason]));
          // 窄面板下警示文本按省略号截断（max-width 46%），hover 给全文
          reasonEl.title = reasonEl.textContent;
          row.appendChild(reasonEl);
        }
        if (ch.intensity && ch.intensity.status === "empty_or_constant") {
          row.title = t("channel.not.displayable");
        }
        list.appendChild(row);
        rows.push(row);
      });
      rootEl.appendChild(list);

      ctrl.panelEls = { root: rootEl, rows: rows, count: count, aiBadge: aiBadge };
      updateAiBadge();
      rootEl.hidden = !ctrl.panelOpen;
    }

    function setPanelOpen(open) {
      ctrl.panelOpen = !!open;
      if (opts.button) {
        opts.button.setAttribute("aria-expanded", ctrl.panelOpen ? "true" : "false");
      }
      if (ctrl.panelEls) ctrl.panelEls.root.hidden = !ctrl.panelOpen;
    }

    function updateAiBadge() {
      var badgeEl = ctrl.panelEls && ctrl.panelEls.aiBadge;
      if (!badgeEl) return;
      // 名称可用性与配色同步（fingerprint）是两件事（F4）：通道名称是否提供
      // 由 manifest 决定，始终展示；fingerprint 同步态未知时只显示名称行
      var lines = [];
      var named = ctrl.info ? ctrl.info.namedCount : 0;
      lines.push(named > 0 ? t("channel.ai.names_ready") : t("channel.ai.names_unknown"));
      var st = syncState(ctrl.renderFingerprint, ctrl.aiRenderFingerprint);
      if (st !== "unknown") {
        lines.push(st === "synced" ? t("channel.ai.synced") : t("channel.ai.stale"));
      }
      badgeEl.hidden = false;
      badgeEl.textContent = lines.join(" ");
    }

    // Batch 5 桥接：HP UI 从 window.PathTogether.renderState 读当前配色，
    // 并监听 hp-render-context-changed 刷新「AI 已同步/未同步」徽章。
    // 不发布 render_token（token 不是授权，但也不该挂到全局）。
    function publishRenderState() {
      try {
        root.PathTogether = root.PathTogether || {};
        if (!ctrl.renderContext && !ctrl.renderFingerprint) {
          root.PathTogether.renderState = null;
        } else {
          var ctx = ctrl.renderContext || {};
          root.PathTogether.renderState = {
            renderContext: ctx,
            renderFingerprint: ctrl.renderFingerprint || ctx.fingerprint || null,
            // 名称可用性与配色同步分开（F4）：不能用 render fingerprint
            // 代替「通道名称已提供」
            namedCount: ctrl.info ? ctrl.info.namedCount : 0,
            channelSemanticsReady: !!(ctrl.info && ctrl.info.namedCount > 0),
          };
        }
        if (typeof root.dispatchEvent === "function" && typeof root.CustomEvent === "function") {
          root.dispatchEvent(new root.CustomEvent("hp-render-context-changed"));
        }
      } catch (e) { /* 忽略 */ }
    }

    // ---------------- viewport 快照 / 恢复（§8.2） ----------------
    function captureViewport() {
      try {
        if (!viewer || !viewer.viewport) return null;
        var vp = viewer.viewport;
        return {
          center: vp.getCenter ? vp.getCenter() : null,
          zoom: vp.getZoom ? vp.getZoom(true) : null,
          rotation: vp.getRotation ? vp.getRotation() : 0,
          flip: vp.getFlip ? !!vp.getFlip() : false,
        };
      } catch (e) { return null; }
    }

    function restoreViewport(snap) {
      if (!snap || !viewer || !viewer.viewport) return;
      try {
        var vp = viewer.viewport;
        if (vp.setRotation) vp.setRotation(snap.rotation || 0);
        if (vp.setFlip) vp.setFlip(!!snap.flip);
        if (snap.zoom != null) vp.zoomTo(snap.zoom, snap.center || null, true);
      } catch (e) { /* 视口恢复失败不阻塞查看 */ }
    }

    function legacyOpen() {
      if (!adapter.dziUrl || !ctrl.slideMeta || !viewer) return;
      try {
        if (opts.onReopening) opts.onReopening();
        viewer.open(adapter.dziUrl(ctrl.slideMeta.id));
      } catch (e) { /* 忽略 */ }
    }

    // ---------------- 应用流程（§8.2 顺序 + epoch 竞争） ----------------
    function applySelection() {
      if (!ctrl.info || !ctrl.info.multichannel || !ctrl.slideMeta) return;
      var epc = ++ctrl.epoch;
      var id = ctrl.slideMeta.id;
      var body = buildRequestBody(ctrl.info.channels, ctrl.selection, ctrl.overrides);
      var snap = captureViewport();
      // 1) 取消旧 tile 请求 / 关闭旧 world item（viewer.close 触发各页 "close"
      //    清理，如 app.js 的底图缩略图；稍后由 setThumbnail 重建）
      try { if (viewer && viewer.close) viewer.close(); } catch (e) { /* 忽略 */ }
      if (opts.onReopening) opts.onReopening();

      Promise.resolve()
        .then(function () { return adapter.normalizeRenderContext(id, body); })
        .then(function (resp) {
          if (epc !== ctrl.epoch) return null;   // 旧请求晚到：丢弃（§8.2）
          return resp.json().then(function (data) {
            return { ok: !!resp.ok, status: resp.status, data: data || {} };
          });
        })
        .then(function (res) {
          if (!res) return;
          if (!res.ok) { handleApplyError(res, epc); return; }
          // 2) 更新 state.renderContext（+ fingerprint/token）
          ctrl.renderContext = res.data.render_context || null;
          ctrl.renderFingerprint = res.data.render_context_fingerprint
            || (ctrl.renderContext && ctrl.renderContext.fingerprint) || null;
          ctrl.renderToken = res.data.render_token || null;
          publishRenderState();
          // 3) 换缩略图（缩略图与屏幕瓦片同 token，§4.4）
          if (opts.setThumbnail && adapter.thumbnailUrl && ctrl.renderToken) {
            opts.setThumbnail(adapter.thumbnailUrl(id, ctrl.renderToken));
          }
          // 4) 打开新 TileSource；5) open 后恢复 viewport；6) AI 徽章占位
          var ts = createDeepZoomTileSource(ctrl.info, adapter, ctrl.renderToken);
          var onOpen = function () {
            restoreViewport(snap);
            if (opts.onReopened) opts.onReopened();
            updateAiBadge();
          };
          if (viewer && viewer.addOnceHandler) viewer.addOnceHandler("open", onOpen);
          else if (viewer) {
            var h = function () { viewer.removeHandler("open", h); onOpen(); };
            viewer.addHandler("open", h);
          }
          try { viewer.open(ts); } catch (e) {
            toast(t("channel.ctx.fail", { e: e && e.message || e }), "error");
          }
          if (ctrl.panelEls) renderPanel();  // 重渲染（勾选态/计数）
        })
        .catch(function (e) {
          if (epc !== ctrl.epoch) return;
          toast(t("channel.ctx.fail", { e: (e && e.message) || e }), "error");
          legacyOpen();
        });
    }

    function handleApplyError(res, epc) {
      var data = res.data || {};
      var code = data.code || "";
      if (res.status === 409 && code === "slide_revision_conflict") {
        // §6.3：只刷新 info 并重建一次；连续变化沿用 503 语义，不无限重试
        if (!ctrl.conflictRecovered && typeof opts.refreshInfo === "function") {
          ctrl.conflictRecovered = true;
          toast(t("channel.ctx.conflict"), "info");
          Promise.resolve()
            .then(opts.refreshInfo)
            .then(function (newInfo) {
              if (epc !== ctrl.epoch || !newInfo) return;
              // 重建 token 时保留用户当前在看的配色（revision 变化不丢选择）
              ctrl.handleInfo(newInfo, ctrl.slideMeta, {
                keepSelection: ctrl.selection.slice(),
                keepOverrides: ctrl.overrides,
              });
            })
            .catch(function () { legacyOpen(); });
          return;
        }
        toast(t("channel.ctx.fail", { e: code || res.status }), "error");
        legacyOpen();
        return;
      }
      if (code === "multichannel_disabled") {
        // flag 被关：隐藏通道 UI，回退原 viewer（§15.3 回滚）
        hideChrome();
        ctrl.info = null;
        legacyOpen();
        return;
      }
      if (code === "render_channel_limit") {
        toast(t("channel.limit.block"), "error");
      } else {
        toast(t("channel.ctx.fail", { e: data.error || code || res.status }), "error");
      }
      legacyOpen();
    }

    function persist() {
      if (!ctrl.slideMeta || !ctrl.info) return;
      saveStoredSelection(
        storage,
        storageKey(ctrl.slideMeta.scope, ctrl.info.slideId || ctrl.slideMeta.id,
          ctrl.info.assetRevision || ""),
        ctrl.selection,
        ctrl.overrides
      );
    }

    // ---------------- 对外：切换通道 / 改色 ----------------
    ctrl.setChannelActive = function (index, on) {
      var ch = ctrl.info && ctrl.info.channels.filter(function (c) { return c.index === index; })[0];
      if (!ch) return false;
      var at = ctrl.selection.indexOf(index);
      if (on && at >= 0) return true;
      if (!on && at < 0) return true;
      if (on) {
        if (!ch.displayable) {
          toast(t("channel.not.displayable"), "info");
          return false;
        }
        if (ctrl.selection.length >= MAX_ACTIVE_CHANNELS) {
          // 第 9 个：阻止 + 可读提示（§4.2）
          toast(t("channel.limit.block"), "error");
          if (ctrl.panelEls) renderPanel();   // 回弹勾选态
          return false;
        }
        ctrl.selection.push(index);
      } else {
        if (ctrl.selection.length <= 1) {
          // 服务端要求启用数 1..8（§6.2）：最后一个不可关
          toast(t("channel.min.block"), "info");
          if (ctrl.panelEls) renderPanel();
          return false;
        }
        ctrl.selection.splice(at, 1);
      }
      ctrl.selection.sort(function (a, b) { return a - b; });
      persist();
      renderPanel();
      applySelection();
      return true;
    };

    ctrl.setChannelColor = function (index, color) {
      var ch = ctrl.info && ctrl.info.channels.filter(function (c) { return c.index === index; })[0];
      if (!ch || !isHexColor(color)) return false;
      // 用户重选颜色后按不透明处理（alpha 255，§5.1）
      ctrl.overrides[index] = Object.assign({}, ctrl.overrides[index], {
        color: String(color).toUpperCase(), alpha: 1,
      });
      persist();
      renderPanel();
      if (ctrl.selection.indexOf(index) >= 0) applySelection();
      return true;
    };

    ctrl.setAiFingerprint = function (fp) {
      ctrl.aiRenderFingerprint = typeof fp === "string" ? fp : null;
      updateAiBadge();
    };

    ctrl.isMultichannel = function () {
      return !!(ctrl.info && ctrl.info.multichannel);
    };
    ctrl.getToken = function () { return ctrl.renderToken; };
    ctrl.getFingerprint = function () { return ctrl.renderFingerprint; };
    ctrl.applySelectionForTest = function () { applySelection(); };

    ctrl.destroy = function () {
      ctrl.epoch += 1;         // 作废在途响应
      ctrl.info = null;
      ctrl.slideMeta = null;
      ctrl.selection = [];
      ctrl.overrides = {};
      ctrl.renderContext = null;
      ctrl.renderFingerprint = null;
      ctrl.renderToken = null;
      ctrl.conflictRecovered = false;
      ctrl.panelOpen = false;
      hideChrome();
      publishRenderState();
    };

    // ---------------- 入口：info 到达 ----------------
    // 返回打开计划：
    //   {kind:"legacy"}                            页面走原 DZI/thumbnail；
    //   {kind:"render", tileSource, renderToken,   页面用 inline TileSource 打开
    //    thumbnailUrl}
    ctrl.handleInfo = function (rawInfo, slideMeta, keep) {
      ctrl.destroy();
      var norm = normalizeChannelInfo(rawInfo);
      ctrl.info = norm;
      ctrl.slideMeta = slideMeta || { id: norm.slideId, scope: "anonymous" };
      var keptSelection = keep && Array.isArray(keep.keepSelection)
        ? clampSelection(norm.channels, keep.keepSelection) : null;
      var keptOverrides = (keep && keep.keepOverrides) || null;

      // flag 关：保持原 viewer，不发新字段、不显示通道面板（§15.2）
      if (!norm.flagEnabled) { hideChrome(); return { kind: "legacy" }; }
      // RGB：不显示占空间的面板；工具栏最多灰色小标识「原始 RGB」（§4.1）
      if (!norm.multichannel) {
        if (opts.button) opts.button.hidden = true;
        if (opts.badge) {
          opts.badge.hidden = false;
          opts.badge.textContent = t("channel.rgb.badge");
        }
        return { kind: "legacy" };
      }
      if (opts.badge) opts.badge.hidden = true;
      if (opts.button) opts.button.hidden = false;
      // 多通道切片打开即展示说明卡（来源/完整性/T-Z 提示无需额外点击）；
      // 工具栏「通道」按钮可随时收起
      ctrl.panelOpen = true;

      // 本地偏好（只含用户选择；失效回默认 + 一次非阻塞提示）
      var key = storageKey(ctrl.slideMeta.scope, norm.slideId || ctrl.slideMeta.id,
        norm.assetRevision || "");
      var stored = keptSelection && keptSelection.length
        ? { selection: keptSelection, overrides: keptOverrides || {}, broken: false }
        : loadStoredSelection(storage, key, norm.channels);
      if (stored.broken) toast(t("channel.pref.broken"), "info");
      if (stored.selection && stored.selection.length) {
        ctrl.selection = stored.selection;
        ctrl.overrides = stored.overrides || {};
      } else {
        // 无本地偏好：以服务端默认 context 为准（同一 manifest 生成）；
        // 异常缺失时前端按同口径回退（前 4 个有效通道）
        ctrl.selection = norm.defaultContext &&
            Array.isArray(norm.defaultContext.active_channels) &&
            norm.defaultContext.active_channels.length
          ? clampSelection(norm.channels, norm.defaultContext.active_channels.map(function (c) {
              return Number(c.index);
            }))
          : defaultSelection(norm.channels);
        ctrl.overrides = {};
      }

      renderPanel();

      // deepzoom 元数据缺失 → 无法构建 inline TileSource，保持原 viewer
      if (!norm.deepzoom || !norm.defaultToken || !norm.defaultContext) {
        return { kind: "legacy" };
      }
      // 首开直接用服务端默认 token（快速出图）；本地偏好与默认不同时随后
      // applySelection 覆盖（epoch 保证最后一次生效）。
      // 打开由控制器经 opts.open 负责：409 刷新 info 重建等「页面 openSlide
      // 不参与」的路径也会再次走到这里，统一入口避免 plan 返回值无人消费。
      ctrl.renderContext = norm.defaultContext;
      ctrl.renderFingerprint = (norm.defaultContext && norm.defaultContext.fingerprint) || null;
      ctrl.renderToken = norm.defaultToken;
      publishRenderState();
      var plan = {
        kind: "render",
        renderToken: norm.defaultToken,
        tileSource: createDeepZoomTileSource(norm, adapter, norm.defaultToken),
        thumbnailUrl: adapter.thumbnailUrl
          ? adapter.thumbnailUrl(ctrl.slideMeta.id, norm.defaultToken)
          : null,
      };
      if (typeof opts.open === "function") opts.open(plan);
      var defaultIdx = (norm.defaultContext.active_channels || [])
        .map(function (c) { return Number(c.index); }).sort(function (a, b) { return a - b; });
      var mine = ctrl.selection.slice().sort(function (a, b) { return a - b; });
      if (mine.length && mine.join(",") !== defaultIdx.join(",")) {
        applySelection();   // 恢复用户上次的配色
      }
      return plan;
    };

    // 面板开关（工具栏「通道」按钮）
    if (opts.button) {
      opts.button.setAttribute("aria-expanded", "false");
      opts.button.addEventListener("click", function () {
        setPanelOpen(!ctrl.panelOpen);
      });
    }

    return ctrl;
  }

  root.HP_Channels = {
    MAX_ACTIVE_CHANNELS: MAX_ACTIVE_CHANNELS,
    DEFAULT_ACTIVE_CHANNELS: DEFAULT_ACTIVE_CHANNELS,
    PALETTE: PALETTE,
    normalizeChannelInfo: normalizeChannelInfo,
    createDeepZoomTileSource: createDeepZoomTileSource,
    sameFingerprint: sameFingerprint,
    syncState: syncState,
    invisibleReason: invisibleReason,
    effectiveVisible: effectiveVisible,
    defaultSelection: defaultSelection,
    clampSelection: clampSelection,
    buildRequestBody: buildRequestBody,
    storageKey: storageKey,
    loadStoredSelection: loadStoredSelection,
    saveStoredSelection: saveStoredSelection,
    createChannelController: createChannelController,
  };
})(window);
