/* =========================================================================
   切片分享页 —— 前端逻辑（OpenSeadragon + ROI + 选区保存）
   ========================================================================= */
(function () {
  "use strict";

  // 中英双语：i18n.js 在本脚本之前加载，提供 window.HP_I18N.t
  // Wave 3（普通图片兼容）：新增键放本文件兜底表（不改 i18n.js 既有行）；
  // i18n.js 未来若收录同名键则优先走 i18n.js（与 app.js tt() 同策略）。
  var _SHARE_I18N = {
    "roi.no.scale.mm": {
      zh: "普通图片无物理标尺，不支持毫米预设标记；查看与像素坐标不受影响",
      en: "This image has no physical scale; millimeter preset marks are unavailable. Viewing and pixel coordinates still work",
    },
  };
  function t(key, vars) {
    var s = window.HP_I18N ? window.HP_I18N.t(key, vars) : key;
    if (s !== key) return s;
    var e = _SHARE_I18N[key];
    var lang = (window.HP_I18N && window.HP_I18N.getLang) ? window.HP_I18N.getLang() : "zh";
    return (e && (e[lang] || e.zh)) || key;
  }

  // ---------- token 与 API 前缀 ----------
  // token 从 location.pathname（/s/<token>）解析
  var TOKEN = window.__SHARE_TOKEN__ || "";
  if (!TOKEN) {
    var m = location.pathname.match(/\/s\/([^/]+)/);
    if (m) TOKEN = decodeURIComponent(m[1]);
  }
  var API = "/s/" + encodeURIComponent(TOKEN);

  // ---------- 多通道通道着色 adapter（Batch 4；channel-controls.js 共用组件） ----------
  // share.js 不加载 app-mode.js：分享页鉴权 = URL 内 share token（capability），
  // 自建同接口 adapter（normalizeRenderContext/tileUrl/thumbnailUrl/dziUrl），
  // 算法全部在 channel-controls.js，不复制（§8.1）。
  function strHash(s) {
    // djb2：localStorage 作用域里只放 share token 的指纹，不落 token 原文
    var h = 5381;
    s = String(s || "");
    for (var i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
    return h.toString(16);
  }

  // ---------- slide ID 化（P2 合同 §5.8） ----------
  // 分享页整页切 slide_id：chips/openSlide/读端点按 ID；列表条目缺 slide_id
  // （旧后端/过渡期）时键回落 legacy 名——两键空间不相交（sld_ 前缀 vs 文件
  // 名带扩展名），回落用 console.debug 标注。后端补齐列表 slide_id 后回落
  // 分支自然消亡（P6 拆除）。
  function shareSlideKey(s) {
    return String((s && (s.slide_id || s.id)) || (s && s.name) || "");
  }

  // 当前打开切片的 id/name 视图（openSlide 维护；channel adapter 拼 URL 用）
  var shareView = { id: null, name: null };

  function shareRestBase(key) {
    // key 是当前切片 slide_id → ID 通道 /api/slides/<id>/...（info/dzi/tiles/
    // crop/thumbnail 族已上线）；否则按名走旧 /api/slide/<name>/...
    if (shareView.id && String(key) === String(shareView.id)) {
      return API + "/api/slides/" + encodeURIComponent(key);
    }
    console.debug("[slide-id] 分享列表条目缺 slide_id，按名回落（旧后端兼容）:", key);
    return API + "/api/slide/" + encodeURIComponent(key);
  }

  function shareChannelAdapter() {
    function renderQuery(renderToken) {
      return renderToken ? "?render=" + encodeURIComponent(renderToken) : "";
    }
    // 画质 query 前置（?profile=&dv=）、render 后置（image-transport-upgrade
    // §5.2；与 app-mode.js 同一拼接顺序，策略在 HP_ViewerEncoding）
    function tileUrlQuery(renderToken, qualityQuery) {
      var parts = [];
      if (qualityQuery) parts.push(String(qualityQuery).replace(/^\?/, ""));
      if (renderToken) parts.push("render=" + encodeURIComponent(renderToken));
      return parts.length ? "?" + parts.join("&") : "";
    }
    return {
      mode: "share",
      normalizeRenderContext: function (_id, body) {
        // render-context 无 by-id 端点（P2 后端缺口，报告待补）：按名走旧通道
        return fetch(API + "/api/slide/" +
          encodeURIComponent(shareView.name || _id) + "/render-context", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body || {}),
        });
      },
      tileUrl: function (id, level, x, y, renderToken, qualityQuery) {
        var base = (shareView.id && String(id) === String(shareView.id))
          ? API + "/api/slides/" + encodeURIComponent(id) + "/tiles/" + level +
            "/" + x + "_" + y + ".jpeg"
          : API + "/api/slide/" + encodeURIComponent(id) + "_files/" + level +
            "/" + x + "_" + y + ".jpeg";
        return base + tileUrlQuery(renderToken, qualityQuery);
      },
      thumbnailUrl: function (id, renderToken, qualityQuery) {
        return shareRestBase(id) + "/thumbnail" + tileUrlQuery(renderToken, qualityQuery);
      },
      dziUrl: function (id) {
        return (shareView.id && String(id) === String(shareView.id))
          ? API + "/api/slides/" + encodeURIComponent(id) + "/dzi"
          : API + "/api/slide/" + encodeURIComponent(id) + ".dzi";
      },
    };
  }
  function initChannelController() {
    return window.HP_Channels.createChannelController({
      adapter: shareChannelAdapter(),
      viewer: viewer,
      button: $("channel-btn"),
      badge: $("rgb-badge"),
      panelHost: $("channel-panel"),
      t: t,
      toast: function (msg, type) { toast(msg, type); },
      storage: window.localStorage,
      // 通道重开（同一切片换配色）：open 处理跳过绘制/标注重置，仅补重绘
      onReopening: function () { state.channelReopening = true; },
      onReopened: function () { syncBaseThumb(); redrawAnnoCanvas(); },
      // render 计划的打开统一由控制器发起（含 409 刷新 info 重建路径）
      open: function (plan) {
        if (viewer && plan && plan.tileSource) viewer.open(plan.tileSource);
      },
      // 底图缩略图与屏幕瓦片同 token（§4.4）；viewer.close 清掉后这里重建
      setThumbnail: function (url) {
        if (!url) return;
        if (!baseThumbEl && viewer && viewer.container) {
          baseThumbEl = document.createElement("img");
          baseThumbEl.className = "osd-base-thumb";
          baseThumbEl.alt = "";
          viewer.container.insertBefore(baseThumbEl, viewer.canvas);
          applyBaseThumbFlip();
        }
        if (baseThumbEl) baseThumbEl.src = url;
      },
      // 409 slide_revision_conflict：只刷新 info 并重建一次（§6.3）。
      // P2：按当前切片操作键（id 优先；列表缺 slide_id 回落名）刷新
      refreshInfo: function () {
        if (!state.slide) return Promise.resolve(null);
        return fetch(shareRestBase(state.slide.id || state.slide.name) + "/info")
          .then(function (r) { return r.json(); });
      },
    });
  }

  // ---------- 全局状态 ----------
  var state = {
    slides: [],          // 该分享的切片集
    slide: null,         // 当前切片 {id,name,width,height,mppX,mppY,mppSource}（P2：id=slide_id）
    mppX: null,
    roiMode: null,
    roi: { x: 0, y: 0, side: 0 },
    rotation: 0,
    flipped: false,      // 是否水平翻转（镜像）
    drawMode: null,      // null | "arrow" | "freehand"（与 roiMode 互斥）
    showAnno: true,      // 默认始终显示（用户需要看到管理员标记）
    focusAnno: null,     // null=显示全部；否则只显示该条标注（currentRois 中的引用；
                         // §6.1 起只由 ⋯「只看此标记」设置，选中不再隐藏其它标记）
    roiGeometryLocked: false, // §6.3：矩形草稿位置锁（保存为标记时随 POST 带上）
    polygonVertexEdit: null,  // §6.2：polygon「编辑轮廓点」模式的目标 item
    roiSizes: [6, 6.5],  // 本次分享允许的矩形标记尺寸（fetch config 后填充）
    // Wave 3（普通图片兼容）：当前切片无物理标尺（mpp_source="missing"）时为
    // true——毫米预设标记按钮禁用、滑块段禁用（服务端本就拒绝缺 MPP 的毫米
    // 预设，前端不把用户引向必失败路径）；查看与像素标注（arrow/freehand）不受影响
    noPhysicalScale: false,
    channelReopening: false, // 通道配色重开（同一切片换 TileSource，非新切片）
    channelCtrl: null,   // 多通道通道着色控制器（Batch 4，channel-controls.js 共用）
  };

  var viewer = null;
  var roiBox = null;
  var dragInfo = null;
  // 底图缩略图层：铺在瓦片层后面的模糊预览，慢网下避免瓦片未到区域变白
  var baseThumbEl = null;
  // 当前切片的标注（本 token + 管理员），扁平数组
  var currentRois = [];

  // 编辑模式状态：选中/拖动
  // editItem：当前选中的标注（currentRois 中的引用副本，可改本地几何）
  // editDrag：拖动会话 {handle, pointerId, ...起点快照}
  // state.editing：备注卡的「保存/取消」模式（§6.2 改版后柄显示与拖动只看
  // 选中 + canDragShareItem；分享端拖完仍显式保存，契约不变）
  var editItem = null;
  var editDrag = null;

  // ---------- DOM ----------
  function $(id) { return document.getElementById(id); }
  var els = {
    invalidMask: $("invalid-mask"),
    currentSlide: $("current-slide"),
    slideChips: $("slide-chips"),
    zoomIn: $("zoom-in"),
    zoomOut: $("zoom-out"),
    rotateBtn: $("rotate-btn"),
    flipBtn: $("flip-btn"),
    roi6: $("roi-6"),
    roi65: $("roi-6-5"),
    saveRoiBtn: $("save-roi-btn"),
    exportBtn: $("export-btn"),
    resetBtn: $("reset-btn"),
    mppSetter: $("mpp-setter"),
    mppInput: $("mpp-input"),
    mppSetBtn: $("mpp-set-btn"),
    zoomBadge: $("zoom-badge"),
    zoomNative: $("zoom-native"),
    roiLabel: $("roi-label"),
    panelToggle: $("roi-panel-toggle"),
    panel: $("roi-panel"),
    panelMask: $("roi-panel-mask"),
    panelClose: $("roi-panel-close"),
    panelList: $("roi-panel-list"),
    annoArrowBtn: $("anno-arrow-btn"),
    annoFreeBtn: $("anno-free-btn"),
    annoAllBtn: $("anno-all-btn"),
    annoCanvas: $("anno-canvas"),
    toastContainer: $("toast-container"),
    roiNote: $("roi-note"),
    panelEdit: $("roi-panel-edit"),
    roiBoxBtn: $("roi-box-btn"),
    annoAllToggle: $("anno-all-toggle"),
  };

  // ---------- 工具函数 ----------
  function toast(msg, type) {
    type = type || "info";
    var el = document.createElement("div");
    el.className = "toast " + type;
    el.textContent = msg;
    els.toastContainer.appendChild(el);
    setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 3000);
  }

  function clamp(v, lo, hi) {
    if (hi < lo) hi = lo;
    return Math.max(lo, Math.min(hi, v));
  }

  // label -> 颜色（哈希着色）
  function labelColor(label) {
    var s = String(label || "");
    var h = 0;
    for (var i = 0; i < s.length; i++) {
      h = (h * 31 + s.charCodeAt(i)) >>> 0;
    }
    var hue = h % 360;
    return { fill: "hsla(" + hue + ",70%,55%,0.12)", stroke: "hsl(" + hue + ",70%,45%)" };
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // 标注几何 helper（static/anno-geometry.js；主站与分享页共用，模板保证先加载）
  var AG = window.HP_AnnoGeometry || {
    pointsBBox: function () { return null; },
    pointSegDist: function (px, py, x1, y1, x2, y2) { return 0; },
    pointInPolygon: function () { return false; },
    polygonSelfIntersects: function () { return false; },
    translatePoints: function (pts) { return pts; },
    scalePointsAbout: function (pts, ax, ay, s) { return pts; },
    pointsShiftIntoRect: function () { return null; },
    rectQuad: function () { return []; },
    pointInQuad: function () { return false; },
    quadBBox: function () { return null; },
    clonePoints: function (pts) { return pts; },
  };

  // §6.1 统一选中视觉：单一蓝色轮廓；去掉旧「蓝外框 + 黄内框 + 常驻黄角点」
  var ANNO_SELECTED_STROKE = "#007AFF";
  var ANNO_SELECTED_FILL = "rgba(0,122,255,0.10)";

  // ---- 位置锁 / 可编辑判定（§6.3；条目顶层 geometry_locked，缺省 false） ----
  function itemGeometryLocked(it) {
    return !!(it && (it.geometry_locked === true || it.geometry_locked === "true"));
  }
  // 分享端可编辑 = 本人创建（既有 source==="me" 语义）；服务端能力位优先
  function itemCanEdit(it) {
    if (!it) return false;
    if (it.can_edit === false || it.can_edit === "false") return false;
    return isEditable(it);
  }
  // 矩形：四顶点经 OSD 投影成屏幕四边形（§6.2 旋转/镜像正确投影）
  function rectScreenQuad(it) {
    var quad = AG.rectQuad(it.x, it.y, rectItemW(it), rectItemH(it), imgToCanvas);
    return { quad: quad, bbox: AG.quadBBox(quad) };
  }

  // ---------- 初始化 OpenSeadragon ----------
  function initViewer() {
    viewer = OpenSeadragon({
      element: $("viewer"),
      showNavigationControl: false,
      // Tiles are already rendered to RGB by the server. Canvas draws them
      // directly, avoiding OSD 5's offscreen WebGL-to-2D copy on every pan frame.
      drawer: "canvas",
      // 慢网下适度限制并发图像请求（略增并发以更快填补空隙）
      imageLoaderLimit: 8,
      // 瓦片未到位时透明：露出后面铺好的缩略图底图，避免白/灰块
      placeholderFillStyle: null,
      compositeOperation: "source-over",
      minZoomImageRatio: 0.5,
      maxZoomPixelRatio: 10,
      // 偏保守选层（默认 0.5 → 0.4）：同屏瓦片更倾向取自同一层，
      // 减少高低层混排的“割裂”接缝，同时降低慢网请求量（轻微变柔，可接受）
      minPixelRatio: 0.4,
      defaultZoomLevel: 0,           // 自适应初始缩放
      immediateRender: false,
      preload: false,
      wrapHorizontal: false,
      wrapVertical: false,
      preserveImageSizeOnResize: true,
      // 滚轮缩放更细腻，便于慢网手动控缩放，避免一次跳太多层
      pixelsPerWheelLine: 40,
      gestureSettingsMouse: {
        scrollToZoom: true,
        clickToZoom: false,
        dblClickToZoom: true,
      },
      gestureSettingsTouch: {
        pinchToZoom: true,
        flickEnabled: false,
      },
      animationTime: 0.3,
      visibilityRatio: 0.1,
      prefixUrl: "",
    });
    // 图外空白区用深色背景，减少慢网下大面积空白的刺眼感
    viewer.container.style.backgroundColor = "#262a30";
    viewer.addHandler("zoom", function () { updateZoomBadge(); syncBaseThumb(); });
    viewer.addHandler("open", function () {
      updateZoomBadge(); syncBaseThumb();
      // 通道配色重开（同一切片换 TileSource，§8.2）：走轻量路径，不重置
      // 绘制/标注状态（viewport 恢复由控制器负责）
      if (state.channelReopening) {
        state.channelReopening = false;
        return;
      }
      exitDrawMode();
      resizeAnnoCanvas();
      // 打开后加载标注并重绘
      loadCurrentRois();
    });
    // 底图随平移/缩放实时跟随（animation 每帧触发，跟随最平滑）
    viewer.addHandler("animation", function () { syncBaseThumb(); redrawAnnoCanvas(true); });
    // 动画结束补画文本（标签/气泡）：动画期间为流畅省略了文本绘制
    viewer.addHandler("animation-finish", function () { redrawAnnoCanvas(); });
    viewer.addHandler("rotate", function () { syncBaseThumb(); redrawAnnoCanvas(); });
    // 镜像翻转：OSD 'flip' 事件 → 同步底图 transform / 重绘标注画布 / ROI 框重对位
    viewer.addHandler("flip", function () {
      state.flipped = !!viewer.viewport.getFlip();
      applyBaseThumbFlip();
      syncBaseThumb();
      redrawAnnoCanvas();
      updateRoiOverlay();
    });
    viewer.addHandler("resize", function () { resizeAnnoCanvas(); redrawAnnoCanvas(); });
    // 切片关闭时清理旧底图
    viewer.addHandler("close", clearBaseThumb);
  }

  // 倍率徽章唯一口径：HP_ViewerCore.zoomText（viewer-core.js，与正式版/Demo
  // 同一公式 imageZoom × 10/mpp；share.html 已保证先加载 viewer-core）。
  // 旧本地副本（按屏幕 DPI 反演的"物理倍率"公式）已删除——那套公式方向相反
  // 且与主站口径漂移（review 2026-08-29 G8）。
  function updateZoomBadge() {
    var core = window.HP_ViewerCore;
    els.zoomBadge.textContent = (core && core.zoomText && viewer)
      ? core.zoomText(viewer, state.mppX)
      : "—";
  }

  // ---------- 加载切片集 ----------
  function loadSlides() {
    fetch(API + "/api/slides")
      .then(function (r) {
        if (r.status === 404) { showInvalid(); throw new Error("invalid"); }
        return r.json();
      })
      .then(function (slides) {
        state.slides = slides || [];
        renderChips();
        if (state.slides.length === 0) {
          els.currentSlide.textContent = t("share.slide.unavailable");
          return;
        }
        // P2：默认打开首选切片的操作键（slide_id；列表缺 id 回落名）
        openSlide(shareSlideKey(state.slides[0]));
      })
      .catch(function (e) {
        if (e.message !== "invalid") toast(t("share.load.fail", { e: e }), "error");
      });
  }

  function showInvalid() {
    els.invalidMask.style.display = "flex";
  }

  // 拉取本次分享配置（矩形标记允许尺寸），再据此渲染工具栏按钮可用性
  function loadConfig() {
    return fetch(API + "/api/config")
      .then(function (r) {
        if (!r.ok) throw new Error("config " + r.status);
        return r.json();
      })
      .then(function (cfg) {
        var sizes = cfg && cfg.roi_sizes;
        if (sizes && sizes.length) {
          state.roiSizes = sizes.map(function (s) { return Number(s); });
        }
      })
      .catch(function () {
        // 失败时保持默认（两者皆可）
      })
      .then(function () { applyRoiSizeRestriction(); });
  }

  // 根据允许尺寸禁用/启用 ROI 分段按钮；若当前 roiMode 被禁则退出 ROI。
  // Wave 3：noPhysicalScale（普通图片缺物理标尺）在此与 roi_sizes 白名单汇合
  // ——两个闸任一不满足即禁用；提示文案区分「分享不允许该尺寸」与「无物理标尺」。
  function applyRoiSizeRestriction() {
    var allowed = {};
    state.roiSizes.forEach(function (s) { allowed[Number(s)] = true; });
    function setBtn(btn, sizeKey) {
      var ok = !!allowed[sizeKey] && !state.noPhysicalScale;
      btn.disabled = !ok;
      btn.title = ok ? ""
        : (state.noPhysicalScale ? t("roi.no.scale.mm") : t("share.size.disallowed"));
      btn.classList.toggle("disabled", !ok);
    }
    setBtn(els.roi6, 6);
    setBtn(els.roi65, 6.5);
    // 同步移动端滑块（disabled 段 + 拇指定位）
    syncRoiSlider();
    // 若当前 roiMode 被禁则退出 ROI 模式
    if (state.roiMode != null && (!allowed[state.roiMode] || state.noPhysicalScale)) {
      exitRoi();
    }
  }

  function renderChips() {
    if (state.slides.length <= 1) {
      els.slideChips.style.display = "none";
      return;
    }
    els.slideChips.style.display = "flex";
    els.slideChips.innerHTML = "";
    state.slides.forEach(function (s) {
      var chip = document.createElement("div");
      chip.className = "chip";
      // P2 合同 §5.8：chips 操作键 dataset.slideId（= slide_id；列表缺 id 的
      // 条目回落名——两键空间不相交）；dataset.name 保留供显示/调试
      var key = shareSlideKey(s);
      var display = s.display_name || s.alias || s.name;
      chip.textContent = display;
      chip.title = ((s.display_name || s.alias)
        ? (s.display_name || s.alias) + " (" + s.name + ")" : s.name) +
        (s.error ? t("share.chip.read.fail") : "");
      chip.dataset.slideId = key;
      chip.dataset.name = s.name;
      if (state.slide && String(state.slide.id || state.slide.name) === key) {
        chip.classList.add("active");
      }
      chip.addEventListener("click", function () { openSlide(key); });
      els.slideChips.appendChild(chip);
    });
  }

  // ---------- 底图缩略图层（慢网下瓦片未到区域的模糊预览） ----------
  function applyBaseThumbFlip() {
    if (!baseThumbEl) return;
    baseThumbEl.style.transformOrigin = "center";
    baseThumbEl.style.transform = state.flipped ? "scaleX(-1)" : "";
  }

  function clearBaseThumb() {
    if (baseThumbEl) {
      if (baseThumbEl.parentNode) baseThumbEl.parentNode.removeChild(baseThumbEl);
      baseThumbEl = null;
    }
  }

  function syncBaseThumb() {
    if (!baseThumbEl || !viewer || !viewer.viewport || !state.slide) return;
    var W = state.slide.width, H = state.slide.height;
    if (!W || !H) return;
    try {
      var tl = viewer.viewport.imageToViewerElementCoordinates(new OpenSeadragon.Point(0, 0));
      var br = viewer.viewport.imageToViewerElementCoordinates(new OpenSeadragon.Point(W, H));
      var left = Math.min(tl.x, br.x);
      var top = Math.min(tl.y, br.y);
      var width = Math.abs(br.x - tl.x);
      var height = Math.abs(br.y - tl.y);
      baseThumbEl.style.left = left + "px";
      baseThumbEl.style.top = top + "px";
      baseThumbEl.style.width = width + "px";
      baseThumbEl.style.height = height + "px";
      // 仅当旋转角为 0/180 时显示底图，避免 90/270 错位（瓦片本身正常旋转显示）
      baseThumbEl.style.display = (state.rotation % 180 === 0) ? "block" : "none";
    } catch (e) {}
  }

  // ---------- 打开切片 ----------
  // P2 合同 §5.8：ref = slide_id（操作键）；列表条目缺 slide_id 时是 legacy 名
  function openSlide(ref) {
    // 切换切片前移除旧底图
    clearBaseThumb();
    var info = null;
    for (var i = 0; i < state.slides.length; i++) {
      if (shareSlideKey(state.slides[i]) === String(ref)) { info = state.slides[i]; break; }
    }
    if (!info) { toast(t("share.not.in.share"), "error"); return; }
    if (info.error || !info.width) {
      toast(t("share.read.fail", { e: (info.error || t("share.unknown.error")) }), "error");
      return;
    }

    state.slide = {
      id: info.slide_id || null,   // P2：slide_id 操作键（列表缺 id 时为 null → 按名回落）
      name: info.name,
      width: info.width,
      height: info.height,
      mppX: info.mpp_x,
      mppY: info.mpp_y,
      mppSource: info.mpp_source,
    };
    // channel adapter 的 ID 通道判定依据（当前打开切片的 id/name）
    shareView.id = state.slide.id;
    shareView.name = state.slide.name;
    state.mppX = info.mpp_x;
    state.rotation = 0;

    els.currentSlide.textContent = info.display_name || info.alias || info.name;
    els.currentSlide.title = info.name + (info.note ? " · " + info.note : "");
    updateMppSetterVisibility();
    exitRoi();
    // Wave 3（普通图片兼容）：无物理标尺切片禁用毫米预设入口（提示文案按
    // 「普通图片无物理标尺」方向，不引导去填 mpp——那不会改变服务端判定）
    syncRoiScaleAvailability();

    // 高亮 chip（P2：按 dataset.slideId 操作键）
    var activeKey = String(ref);
    var chips = els.slideChips.querySelectorAll(".chip");
    chips.forEach(function (c) {
      c.classList.toggle("active", String(c.dataset.slideId) === activeKey);
    });

    // 创建底图缩略图层：铺在瓦片 canvas 之前（下层），慢网下透出模糊预览
    // （src 由通道控制器 setThumbnail 或 legacy 路径填充）
    baseThumbEl = document.createElement("img");
    baseThumbEl.className = "osd-base-thumb";
    baseThumbEl.alt = "";
    viewer.container.insertBefore(baseThumbEl, viewer.canvas);
    applyBaseThumbFlip();

    // 多通道通道着色（Batch 4）：分享列表条目自带 render additive 字段
    // （share_server /api/slides 已并入）。render 计划的 viewer.open 由控制器
    // 经 opts.open 完成；本函数只负责 legacy 打开。
    var plan = null;
    if (window.HP_Channels) {
      state.channelCtrl = state.channelCtrl || initChannelController();
      plan = state.channelCtrl.handleInfo(info, {
        id: String(ref),
        scope: "share:" + strHash(TOKEN),
      });
    }
    if (!plan || plan.kind !== "render") {
      baseThumbEl.src = shareRestBase(ref) + "/thumbnail";
      viewer.open((shareView.id && String(ref) === String(shareView.id))
        ? API + "/api/slides/" + encodeURIComponent(ref) + "/dzi"
        : API + "/api/slide/" + encodeURIComponent(ref) + ".dzi");
    }
  }

  // ---------- mpp 设置区显示控制 ----------
  // Wave 3（普通图片兼容）：mpp_source="missing"（普通图片）不再显示 mpp 输入——
  // 手动 mpp 只改前端状态，服务端仍按缺可信 MPP 拒绝毫米预设；展示这个输入会
  // 暗示「填了就能标」，与实际能力不符（校准系统不在本期范围）。
  // "estimated"（扫描切片估算 mpp）保持原有入口不变。
  function updateMppSetterVisibility() {
    if (!state.slide) { els.mppSetter.style.display = "none"; return; }
    var src = state.slide.mppSource;
    if (src === "estimated") {
      els.mppSetter.style.display = "flex";
      els.mppInput.value = state.mppX != null ? state.mppX : "";
    } else {
      els.mppSetter.style.display = "none";
    }
  }

  // ---------- 无物理标尺（普通图片 mpp_source="missing"）毫米预设可用性 ----------
  // Wave 3：无物理标尺时毫米预设入口整体下闸（服务端 _reject_preset_rect_mm
  // 本就拒绝缺可信 MPP 的毫米预设，前端不把用户引向必失败路径）。尺寸白名单
  // （roi_sizes）与标尺闸在 applyRoiSizeRestriction 汇合，避免互相覆盖。
  function slideHasPhysicalScale() {
    return !!(state.mppX && state.mppX > 0);
  }
  function syncRoiScaleAvailability() {
    state.noPhysicalScale = !slideHasPhysicalScale();
    applyRoiSizeRestriction();
  }

  // ---------- 缩放 / 旋转 / 复位 ----------
  function zoomIn() {
    if (!viewer || !viewer.viewport) return;
    viewer.viewport.zoomBy(1.4);
    viewer.viewport.applyConstraints();
  }
  function zoomOut() {
    if (!viewer || !viewer.viewport) return;
    viewer.viewport.zoomBy(1 / 1.4);
    viewer.viewport.applyConstraints();
  }
  // 1:1 原始像素（F3）：口径唯一在 HP_ViewerCore.zoomToNative（viewer-core 必加载）
  function zoomNative() {
    var core = window.HP_ViewerCore;
    if (core && core.zoomToNative) core.zoomToNative(viewer);
  }
  function rotate() {
    if (!viewer || !viewer.viewport) return;
    state.rotation = (state.rotation + 90) % 360;
    viewer.viewport.setRotation(state.rotation);
    updateRoiOverlay();
    redrawAnnoCanvas();
  }
  function flip() {
    if (!viewer || !viewer.viewport || !viewer.viewport.toggleFlip) return;
    viewer.viewport.toggleFlip();
    // 'flip' 事件负责同步 state/各层；toggleFlip 可能未触发事件时兜底
    state.flipped = !!viewer.viewport.getFlip();
    applyBaseThumbFlip();
    syncBaseThumb();
    redrawAnnoCanvas();
    updateRoiOverlay();
  }
  function reset() {
    if (!viewer || !viewer.viewport) return;
    state.rotation = 0;
    viewer.viewport.setRotation(0);
    // 复位时取消镜像（回到默认朝向）
    if (viewer.viewport.getFlip && viewer.viewport.getFlip()) {
      viewer.viewport.toggleFlip();
    }
    viewer.viewport.goHome(true);
  }

  // ---------- ROI 功能 ----------
  function roiSide(sizeMm) {
    if (!state.mppX || state.mppX <= 0) return 0;
    return Math.round((sizeMm * 1000) / state.mppX);
  }

  function toggleRoi(sizeMm) {
    if (!state.slide) { toast(t("roi.need.slide"), "error"); return; }
    // 本次分享不允许该尺寸 → 直接拒绝（按钮亦为禁用态）
    var allowed = {};
    state.roiSizes.forEach(function (s) { allowed[Number(s)] = true; });
    if (!allowed[Number(sizeMm)]) {
      toast(t("share.size.disallowed.mm", { s: sizeMm }), "info");
      return;
    }
    if (!state.mppX || state.mppX <= 0) {
      // Wave 3（普通图片兼容）：missing（普通图片）给准确说明，不再提示
      // 「请先在工具栏设置 mpp」——分享端手动 mpp 不持久化，服务端仍会拒绝
      if (state.slide.mppSource === "missing") {
        toast(t("roi.no.scale.mm"), "info");
      } else {
        toast(t("roi.need.mpp"), "error");
      }
      return;
    }
    if (state.slide.mppSource === "estimated") {
      toast(t("roi.estimate.tip"), "info");
    }
    // 进入 ROI 模式时退出箭头/描图绘制模式（互斥）
    exitDrawMode();
    if (state.roiMode === sizeMm) { exitRoi(); return; }

    var newSide = roiSide(sizeMm);
    if (newSide <= 0) { toast(t("roi.calc.fail"), "error"); return; }

    var W0 = state.slide.width, H0 = state.slide.height;
    if (newSide > W0 || newSide > H0) {
      var physW = (W0 * state.mppX / 1000).toFixed(1);
      var physH = (H0 * state.mppX / 1000).toFixed(1);
      toast(t("roi.out.of.range", { w: physW, h: physH, s: sizeMm, mpp: state.mppX }), "info");
    }

    var W = state.slide.width, H = state.slide.height;
    var cx, cy;
    if (state.roiMode != null && state.roi.side > 0) {
      cx = state.roi.x + state.roi.side / 2;
      cy = state.roi.y + state.roi.side / 2;
    } else {
      cx = W / 2; cy = H / 2;
      try {
        var center = viewer.viewport.getCenter();
        var imgPt = viewer.viewport.viewportToImageCoordinates(center);
        cx = imgPt.x; cy = imgPt.y;
      } catch (e) {}
    }
    var x = clamp(cx - newSide / 2, 0, Math.max(0, W - newSide));
    var y = clamp(cy - newSide / 2, 0, Math.max(0, H - newSide));

    state.roiMode = sizeMm;
    state.roi.x = Math.round(x);
    state.roi.y = Math.round(y);
    state.roi.side = newSide;

    createRoiBox();
    updateRoiOverlay();
    updateRoiButtons();
    els.saveRoiBtn.disabled = false;
    els.exportBtn.disabled = false;
    updateCtxBar();
  }

  function exitRoi() {
    state.roiMode = null;
    state.roiGeometryLocked = false;  // §6.3：草稿锁只对当前草稿有效
    closeAnnoToolbar();
    if (roiBox && viewer && viewer.currentOverlays) {
      try { viewer.removeOverlay(roiBox); } catch (e) {}
    }
    if (roiBox && roiBox.parentNode) {
      roiBox.parentNode.removeChild(roiBox);
    }
    roiBox = null;
    updateRoiButtons();
    els.saveRoiBtn.disabled = true;
    els.exportBtn.disabled = true;
    updateCtxBar();
  }

  function updateRoiButtons() {
    els.roi6.classList.toggle("active", state.roiMode === 6);
    els.roi65.classList.toggle("active", state.roiMode === 6.5);
    syncRoiSlider(); // 同步移动端滑块分段 + 滑动拇指
  }

  // ---------- 移动端 ROI 滑块分段（取代旧弹窗选择器） ----------
  // #roi-box-btn 现为分段容器：滑块拇指滑到激活段；点一段切换/再点退出。
  // 段集合由 state.roiSizes 决定，不支持的尺寸置 disabled（拇指跳过该段）。
  function syncRoiSlider() {
    var box = els.roiBoxBtn;
    if (!box) return;
    var segs = box.querySelectorAll(".roi-slider-seg");
    if (!segs.length) return;
    var allowed = {};
    state.roiSizes.forEach(function (s) { allowed[Number(s)] = true; });
    var activeIdx = -1;
    segs.forEach(function (seg, i) {
      var sz = Number(seg.getAttribute("data-size"));
      // Wave 3：无物理标尺（普通图片）时段一并禁用（thumb 跳过）
      var ok = !!allowed[sz] && !state.noPhysicalScale;
      seg.classList.toggle("disabled", !ok);
      seg.disabled = !ok;
      var on = state.roiMode === sz;
      seg.classList.toggle("active", on);
      if (on) activeIdx = i;
    });
    box.classList.toggle("active", activeIdx >= 0);
    // 滑动拇指：定位到激活段，按"可用段"等分宽度（跳过 disabled 段）
    var thumb = box.querySelector(".roi-slider-thumb");
    if (thumb) {
      var usable = Array.prototype.filter.call(segs, function (s) { return !s.classList.contains("disabled"); });
      var n = usable.length || 1;
      var uIdx = 0;
      for (var k = 0; k < segs.length; k++) {
        if (segs[k].classList.contains("disabled")) continue;
        if (k === activeIdx) break;
        uIdx++;
      }
      thumb.style.width = (100 / n) + "%";
      thumb.style.transform = "translateX(" + (uIdx * 100) + "%)";
      thumb.style.opacity = activeIdx >= 0 ? "1" : "0";
    }
  }

  function createRoiBox() {
    if (roiBox) return;
    roiBox = document.createElement("div");
    roiBox.id = "roi-box";
    var tl = document.createElement("div"); tl.className = "roi-corner tl";
    var tr = document.createElement("div"); tr.className = "roi-corner tr";
    var bl = document.createElement("div"); bl.className = "roi-corner bl";
    var br = document.createElement("div"); br.className = "roi-corner br";
    roiBox.appendChild(tl); roiBox.appendChild(tr);
    roiBox.appendChild(bl); roiBox.appendChild(br);
    var label = document.createElement("div");
    label.className = "roi-label";
    roiBox.appendChild(label);
    roiBox.addEventListener("pointerdown", onRoiPointerDown);
    viewer.container.appendChild(roiBox);
  }

  function updateRoiOverlay() {
    if (!roiBox || !state.slide) return;
    var r = state.roi;
    if (r.side <= 0) return;
    var label = roiBox.querySelector(".roi-label");
    if (label) label.textContent = state.roiMode + "mm × " + state.roiMode + "mm";

    var rect = viewer.viewport.imageToViewportRectangle(r.x, r.y, r.side, r.side);
    var existing = viewer.getOverlayById(roiBox);
    if (existing) {
      viewer.updateOverlay(roiBox, rect, OpenSeadragon.Placement.TOP_LEFT);
    } else {
      var opts = {
        element: roiBox,
        location: rect,
        placement: OpenSeadragon.Placement.TOP_LEFT,
      };
      if (state.rotation % 360 !== 0 &&
          OpenSeadragon.OverlayRotationMode &&
          OpenSeadragon.OverlayRotationMode.BOUNDING_BOX) {
        opts.rotationMode = OpenSeadragon.OverlayRotationMode.BOUNDING_BOX;
      }
      viewer.addOverlay(opts);
    }
    // §6.1：临时框创建后出现就近工具条（[保存为标记][锁定位置][⋯]）
    if (rectModeActive() && state.roi.side > 0) openAnnoToolbar();
  }

  // 用指定的图像坐标重建 ROI 框（跳转账使用）
  function rebuildRoi(x, y, side, sizeMm) {
    if (!state.slide) return;
    state.roiMode = sizeMm;
    state.roi.x = Math.round(x);
    state.roi.y = Math.round(y);
    state.roi.side = Math.round(side);
    createRoiBox();
    updateRoiOverlay();
    updateRoiButtons();
    els.saveRoiBtn.disabled = false;
    els.exportBtn.disabled = false;
  }

  // ---------- ROI 拖拽 ----------
  function onRoiPointerDown(e) {
    if (!state.slide) return;
    e.preventDefault();
    e.stopPropagation();
    try { roiBox.setPointerCapture(e.pointerId); } catch (err) {}
    dragInfo = {
      pointerId: e.pointerId,
      startRoiX: state.roi.x,
      startRoiY: state.roi.y,
      startImg: viewer.viewport.viewerElementToImageCoordinates(
        new OpenSeadragon.Point(e.clientX - getViewerRect().left,
                                e.clientY - getViewerRect().top)),
    };
    viewer.setMouseNavEnabled(false);
    roiBox.addEventListener("pointermove", onRoiPointerMove);
    roiBox.addEventListener("pointerup", onRoiPointerUp);
    // 工单 D：pointercancel = 取消恢复（绝不按「完成」提交当前位移）
    roiBox.addEventListener("pointercancel", onRoiPointerCancel);
  }

  function onRoiPointerMove(e) {
    if (!dragInfo) return;
    e.preventDefault();
    e.stopPropagation();
    var rect = getViewerRect();
    var curImg = viewer.viewport.viewerElementToImageCoordinates(
      new OpenSeadragon.Point(e.clientX - rect.left, e.clientY - rect.top));
    var grabX = dragInfo.startImg.x - dragInfo.startRoiX;
    var grabY = dragInfo.startImg.y - dragInfo.startRoiY;
    var W = state.slide.width, H = state.slide.height, side = state.roi.side;
    var nx = clamp(Math.round(curImg.x - grabX), 0, Math.max(0, W - side));
    var ny = clamp(Math.round(curImg.y - grabY), 0, Math.max(0, H - side));
    state.roi.x = nx; state.roi.y = ny;
    viewer.updateOverlay(
      roiBox,
      viewer.viewport.imageToViewportRectangle(nx, ny, side, side),
      OpenSeadragon.Placement.TOP_LEFT
    );
  }

  function onRoiPointerUp(e) {
    if (!dragInfo) return;
    e.preventDefault();
    e.stopPropagation();
    try { roiBox.releasePointerCapture(dragInfo.pointerId); } catch (err) {}
    roiBox.removeEventListener("pointermove", onRoiPointerMove);
    roiBox.removeEventListener("pointerup", onRoiPointerUp);
    roiBox.removeEventListener("pointercancel", onRoiPointerCancel);
    dragInfo = null;
    viewer.setMouseNavEnabled(true);
  }

  // 工单 D：拖拽被打断（pointercancel / 丢失捕获）→ 恢复拖前位置，不提交位移
  function onRoiPointerCancel(e) {
    if (!dragInfo) return;
    if (e && e.preventDefault) { try { e.preventDefault(); } catch (err) {} }
    try { roiBox.releasePointerCapture(dragInfo.pointerId); } catch (err) {}
    roiBox.removeEventListener("pointermove", onRoiPointerMove);
    roiBox.removeEventListener("pointerup", onRoiPointerUp);
    roiBox.removeEventListener("pointercancel", onRoiPointerCancel);
    var sx = dragInfo.startRoiX, sy = dragInfo.startRoiY;
    var side = state.roi.side;
    dragInfo = null;
    state.roi.x = sx; state.roi.y = sy;
    if (roiBox && viewer && side > 0) {
      try {
        viewer.updateOverlay(
          roiBox,
          viewer.viewport.imageToViewportRectangle(sx, sy, side, side),
          OpenSeadragon.Placement.TOP_LEFT
        );
      } catch (err) {}
    }
    viewer.setMouseNavEnabled(true);
  }

  function getViewerRect() {
    return viewer.container.getBoundingClientRect();
  }

  // ---------- 保存选区（rect ROI） ----------
  function saveRoi() {
    if (!state.slide || state.roiMode == null) return;
    // label 必填校验
    var label = (els.roiLabel.value || "").trim();
    if (!label) {
      toast(t("share.label.need"), "error");
      try { els.roiLabel.focus(); } catch (e) {}
      return;
    }
    var r = state.roi;
    // P2 合同 §5.8：ROI POST 带 slide_id（与 slide 名快照并存；服务端 ID 优先）
    var roiBody = {
      slide: state.slide.name,
      type: "rect",
      x: Math.round(r.x),
      y: Math.round(r.y),
      size_mm: state.roiMode,
      side_px: Math.round(r.side),
      label: label,
      note: (els.roiNote ? els.roiNote.value : "") || "",
    };
    if (state.slide.id) roiBody.slide_id = state.slide.id;
    // §6.3：草稿位置锁随保存落库（POST 可选 geometry_locked）
    if (state.roiGeometryLocked) roiBody.geometry_locked = true;
    fetch(API + "/api/roi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(roiBody),
    })
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (j) {
            throw new Error(j.error || (t("save.fail") + " " + res.status));
          });
        }
        return res.json();
      })
      .then(function () {
        toast(t("share.roi.saved"), "success");
        refreshRoisOnce();
      })
      .catch(function (e) { toast(t("save.fail2", { e: e.message }), "error"); });
  }

  // ---------- 导出图片（裁剪） ----------
  function exportCrop() {
    if (!state.slide || state.roiMode == null) return;
    var r = state.roi;
    var name = state.slide.name;
    // P2 合同 §5.8：crop 走 /s/<t>/api/slides/<slide_id>/crop（ID 通道）；
    // 列表缺 slide_id 时按名回落旧端点
    var token = (state.channelCtrl && state.channelCtrl.isMultichannel())
      ? state.channelCtrl.getToken() : null;
    var multi = !!token;
    var url = shareRestBase(state.slide.id || name) +
      "/crop?x=" + Math.round(r.x) + "&y=" + Math.round(r.y) +
      "&size=" + Math.round(r.side) +
      (multi ? "&render=" + encodeURIComponent(token) : "");
    var fp8 = ((state.channelCtrl && state.channelCtrl.getFingerprint()) || "").slice(0, 8);

    var originalText = els.exportBtn.textContent;
    els.exportBtn.textContent = t("export.busy");
    els.exportBtn.disabled = true;

    fetch(url)
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (j) {
            throw new Error(j.error || (t("export.fail") + " " + res.status));
          });
        }
        return res.blob();
      })
      .then(function (blob) {
        var stem = name.replace(/\.[^.]+$/, "");
        // 与后端 Content-Disposition 同形：多通道带 fp 前 8 位（§4.4）
        var fname = stem + "_" + state.roiMode + "mm_x" + Math.round(r.x) +
          "_y" + Math.round(r.y) +
          (multi && fp8 ? "_fp" + fp8 : "") + ".png";
        var a = document.createElement("a");
        var objUrl = URL.createObjectURL(blob);
        a.href = objUrl;
        a.download = fname;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(objUrl); }, 1000);
        // 确认文案明确「导出当前伪彩合成图」，不冒充原始数据（§3.2/§4.4）
        toast(multi
          ? t("export.pseudo.done", { name: fname })
          : t("export.done", { name: fname }), "success");
      })
      .catch(function (e) { toast(t("export.fail2", { s: e.message }), "error"); })
      .finally(function () {
        els.exportBtn.textContent = originalText;
        els.exportBtn.disabled = state.roiMode == null;
      });
  }

  // ---------- 手动设置 mpp ----------
  function setMpp() {
    var v = parseFloat(els.mppInput.value);
    if (!isFinite(v) || v <= 0) { toast(t("mpp.invalid"), "error"); return; }
    state.mppX = v;
    if (state.slide) {
      state.slide.mppX = v;
      state.slide.mppSource = "manual";
    }
    if (state.roiMode != null) {
      var newSide = roiSide(state.roiMode);
      var W = state.slide.width, H = state.slide.height;
      var cx = state.roi.x + state.roi.side / 2;
      var cy = state.roi.y + state.roi.side / 2;
      var x = clamp(cx - newSide / 2, 0, Math.max(0, W - newSide));
      var y = clamp(cy - newSide / 2, 0, Math.max(0, H - newSide));
      state.roi.x = Math.round(x);
      state.roi.y = Math.round(y);
      state.roi.side = newSide;
      updateRoiOverlay();
    }
    updateMppSetterVisibility();
    toast(t("mpp.set.ok", { v: v }), "success");
  }

  // =========================================================================
  // 标注画布层（rect/arrow/freehand 统一绘制）
  // =========================================================================
  var annoCtx = null;
  var annoCanvasSize = { width: 0, height: 0 };
  var annoCanvasHasContent = false;

  function resizeAnnoCanvas() {
    var c = els.annoCanvas;
    if (!c || !viewer) return;
    var rect = viewer.container.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    c.width = Math.max(1, Math.floor(rect.width * dpr));
    c.height = Math.max(1, Math.floor(rect.height * dpr));
    c.style.width = rect.width + "px";
    c.style.height = rect.height + "px";
    annoCtx = c.getContext("2d");
    annoCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    annoCanvasSize = { width: rect.width, height: rect.height };
    annoCanvasHasContent = false;
  }

  function imgToCanvas(ix, iy) {
    if (!viewer || !viewer.viewport) return { x: 0, y: 0 };
    var p = viewer.viewport.imageToViewerElementCoordinates(
      new OpenSeadragon.Point(ix, iy));
    return { x: p.x, y: p.y };
  }

  function redrawAnnoCanvas(viewMoving) {
    var c = els.annoCanvas;
    if (!c || !annoCtx) { if (c) resizeAnnoCanvas(); }
    if (!annoCtx) return;
    var hasContent = !!(state.slide && ((state.showAnno && (currentRois.length || editItem)) ||
      (state.drawMode != null && drawPreview)));
    // An already-empty canvas has nothing to clear during viewport movement.
    // Clear once when annotations/AI previews are hidden or the slide closes.
    if (!hasContent && !annoCanvasHasContent) return;
    annoCtx.clearRect(0, 0, annoCanvasSize.width, annoCanvasSize.height);
    annoCanvasHasContent = hasContent;
    if (!hasContent) return;
    // OSD exposes isAnimating on Viewer, not Viewport. The explicit frame flag
    // also covers the first animation event before OSD updates its public flag.
    var animating = viewMoving === true || !!(viewer &&
      typeof viewer.isAnimating === "function" && viewer.isAnimating());
    // 拖动编辑中只保留选中项的气泡，其余气泡暂停（视图静止时减少文本重绘）
    var dragging = !!(editDrag && editItem);
    // 已保存标注（focus 过滤：有 focusAnno 时只画它）
    if (state.showAnno) {
      currentRois.forEach(function (it) {
        if (state.focusAnno && it !== state.focusAnno) return;
        var selected = (editItem === it);
        drawAnnoItem(it, labelColor(it.label), selected, !animating);
      });
    }
    // 编辑手柄（§6.2：首次点击选择即可直接拖控制点；§6.3：锁定/只读无柄）
    if (editItem && canDragShareItem(editItem) && state.showAnno) {
      drawEditHandles(editItem);
    }
    // 绘制中预览
    if (state.drawMode === "arrow" && drawPreview && drawPreview.type === "arrow") {
      drawArrow(drawPreview.x1, drawPreview.y1, drawPreview.x2, drawPreview.y2, "#FFD700", t("draw.preview"));
    }
    if (state.drawMode === "freehand" && drawPreview && drawPreview.type === "freehand" && drawPreview.points.length >= 2) {
      drawFreehand(drawPreview.points, { fill: "rgba(255,215,0,0.12)", stroke: "#FFD700" }, t("draw.preview"));
    }
    // 备注气泡（在标注与手柄之上；动画/拖动期间按需精简；focus 过滤同步）
    if (state.showAnno && !animating) {
      currentRois.forEach(function (it) {
        if (state.focusAnno && it !== state.focusAnno) return; // focus 模式只显示该条气泡
        if (dragging && it !== editItem) return; // 拖动中只画选中项气泡
        var note = String(it.note || "");
        if (!note) return;
        var selected = (editItem === it);
        drawNoteBubble(it, note, selected);
      });
    }
    // §6.1：就近工具条跟随视图（隐藏时不读布局）
    if (annoToolbarEl && !annoToolbarEl.hidden) updateAnnoToolbarPos();
  }

  // 绘制编辑手柄（方块/圆点）；§6.1 统一蓝色控制点
  function drawEditHandles(it) {
    var hs = editHandles(it);
    annoCtx.fillStyle = "#fff";
    annoCtx.strokeStyle = ANNO_SELECTED_STROKE;
    annoCtx.lineWidth = 2;
    hs.forEach(function (h) {
      var isMid = (h.id === "mid" || h.id === "fmid" || h.id === "move");
      if (h.vertex) {
        annoCtx.beginPath();
        annoCtx.arc(h.x, h.y, 5, 0, Math.PI * 2);
        annoCtx.fill(); annoCtx.stroke();
      } else if (h.insertAfter != null) {
        annoCtx.globalAlpha = 0.85;
        annoCtx.fillRect(h.x - 4, h.y - 4, 8, 8);
        annoCtx.strokeRect(h.x - 4, h.y - 4, 8, 8);
        annoCtx.globalAlpha = 1;
      } else if (isMid) {
        annoCtx.beginPath();
        annoCtx.arc(h.x, h.y, 6, 0, Math.PI * 2);
        annoCtx.fill(); annoCtx.stroke();
      } else {
        annoCtx.fillRect(h.x - 5, h.y - 5, 10, 10);
        annoCtx.strokeRect(h.x - 5, h.y - 5, 10, 10);
      }
    });
  }

  // 升级 C：rect 读取真实 w/h（v2 成对 w/h 权威；旧 side_px 正方形兼容）。
  // 展示/命中/编辑不重新正方形化，非正方形不冒充。
  function rectItemW(it) {
    var w = Number(it && it.w);
    if (isFinite(w) && w > 0) return w;
    var s = Number(it && it.side_px);
    return (isFinite(s) && s > 0) ? s : 0;
  }
  function rectItemH(it) {
    var h = Number(it && it.h);
    if (isFinite(h) && h > 0) return h;
    var s = Number(it && it.side_px);
    return (isFinite(s) && s > 0) ? s : 0;
  }

  // 矩形描边 + 半透明填充（quad 多边形路径）；选中叠加单层蓝轮廓。
  // §6.1：不再画黄内框与常驻黄角点；未选中 = label 色细线（更轻）。
  function drawRectQuad(it, color, selected) {
    var sq = rectScreenQuad(it);
    if (!sq.quad || sq.quad.length < 3) return;
    annoCtx.save();
    annoCtx.beginPath();
    annoCtx.moveTo(sq.quad[0][0], sq.quad[0][1]);
    for (var i = 1; i < sq.quad.length; i++) annoCtx.lineTo(sq.quad[i][0], sq.quad[i][1]);
    annoCtx.closePath();
    annoCtx.fillStyle = selected ? ANNO_SELECTED_FILL : (color.fill || "rgba(0,0,0,0)");
    annoCtx.fill();
    annoCtx.lineWidth = selected ? 3 : 2;
    annoCtx.strokeStyle = selected ? ANNO_SELECTED_STROKE : color.stroke;
    annoCtx.stroke();
    annoCtx.restore();
    return sq.bbox;
  }

  function drawAnnoItem(it, color, selected, showText) {
    var typ = it.type || "rect";
    var hlStroke = selected ? ANNO_SELECTED_STROKE : null;
    var lbl = showText ? it.label : null;
    if (typ === "rect") {
      var bbox = drawRectQuad(it, color, selected);
      if (lbl && bbox) {
        drawLabel(it.label, bbox.x, bbox.y,
          (it.size_mm > 0 && rectItemW(it) === rectItemH(it)) ? (it.size_mm + "mm") : "");
      }
    } else if (typ === "arrow") {
      drawArrow(it.x1, it.y1, it.x2, it.y2, hlStroke || color.stroke, lbl);
    } else if (typ === "polygon") {
      // §5.4：polygon 独立类型（闭合简单多边形）；绝不伪装成 freehand
      drawPolygon(it.points, { fill: color.fill, stroke: hlStroke || color.stroke }, lbl);
    } else if (typ === "freehand") {
      drawFreehand(it.points, { fill: color.fill, stroke: hlStroke || color.stroke }, lbl);
    }
  }

  function drawArrow(x1, y1, x2, y2, stroke, label) {
    var a = imgToCanvas(x1, y1), b = imgToCanvas(x2, y2);
    annoCtx.lineWidth = 3;
    annoCtx.strokeStyle = stroke;
    annoCtx.fillStyle = stroke;
    annoCtx.beginPath();
    annoCtx.moveTo(a.x, a.y);
    annoCtx.lineTo(b.x, b.y);
    annoCtx.stroke();
    var ang = Math.atan2(b.y - a.y, b.x - a.x);
    var head = 12;
    annoCtx.beginPath();
    annoCtx.moveTo(b.x, b.y);
    annoCtx.lineTo(b.x - head * Math.cos(ang - Math.PI / 6), b.y - head * Math.sin(ang - Math.PI / 6));
    annoCtx.lineTo(b.x - head * Math.cos(ang + Math.PI / 6), b.y - head * Math.sin(ang + Math.PI / 6));
    annoCtx.closePath();
    annoCtx.fill();
    if (label) drawLabel(label, b.x + 6, b.y - 6, "", stroke);
  }

  function drawFreehand(points, color, label) {
    if (!points || points.length < 2) return;
    annoCtx.lineWidth = 3;
    annoCtx.strokeStyle = color.stroke;
    annoCtx.fillStyle = color.fill || "rgba(0,0,0,0.12)";
    annoCtx.beginPath();
    var p0 = imgToCanvas(points[0][0], points[0][1]);
    annoCtx.moveTo(p0.x, p0.y);
    for (var i = 1; i < points.length; i++) {
      var p = imgToCanvas(points[i][0], points[i][1]);
      annoCtx.lineTo(p.x, p.y);
    }
    annoCtx.closePath();
    annoCtx.fill();
    annoCtx.stroke();
    if (label) drawLabel(label, p0.x, p0.y, "", color.stroke);
  }

  // §5.4：polygon（简单多边形）画布呈现（闭合；类型语义保留，不降级 freehand）
  function drawPolygon(points, color, label) {
    if (!points || points.length < 3) return;
    annoCtx.lineWidth = 3;
    annoCtx.strokeStyle = color.stroke;
    annoCtx.fillStyle = color.fill || "rgba(0,0,0,0.12)";
    annoCtx.beginPath();
    var p0 = imgToCanvas(points[0][0], points[0][1]);
    annoCtx.moveTo(p0.x, p0.y);
    for (var i = 1; i < points.length; i++) {
      var p = imgToCanvas(points[i][0], points[i][1]);
      annoCtx.lineTo(p.x, p.y);
    }
    annoCtx.closePath();
    annoCtx.fill();
    annoCtx.stroke();
    if (label) drawLabel(label, p0.x, p0.y, "", color.stroke);
  }

  function drawLabel(label, x, y, sizeText, strokeColor) {
    var text = String(label || "");
    if (sizeText) text = (text ? text + " · " : "") + sizeText;
    if (!text) return;
    annoCtx.font = "600 11px " + "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif";
    var padX = 5;
    var m = annoCtx.measureText(text);
    var w = m.width + padX * 2;
    var h = 16;
    var bx = x, by = y - h - 2;
    annoCtx.fillStyle = (strokeColor && strokeColor !== "#FFD700") ? strokeColor : "#FFD700";
    annoCtx.fillRect(bx, by, w, h);
    annoCtx.fillStyle = (strokeColor && strokeColor !== "#FFD700") ? "#fff" : "#5a3500";
    annoCtx.textBaseline = "middle";
    annoCtx.fillText(text, bx + padX, by + h / 2 + 0.5);
  }

  // ---------- 备注气泡（macOS callout 风格） ----------
  var BUBBLE_FONT = "12px " + "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif";
  // 布局缓存：note 文本 → {lines, boxW, boxH}（font/maxWidth 固定，布局与视图无关，
  // 避免每帧逐字符 measureText——标注多时这是动画卡顿的主因）
  var _bubbleLayoutCache = {};
  function bubbleLayout(note) {
    var hit = _bubbleLayoutCache[note];
    if (hit) return hit;
    annoCtx.font = BUBBLE_FONT;
    var maxWidth = 240;
    var lines = wrapText(note, maxWidth);
    var padX = 8, padY = 6, lineH = 15;
    var textW = 0;
    lines.forEach(function (ln) {
      var w = annoCtx.measureText(ln).width;
      if (w > textW) textW = w;
    });
    var out = {
      lines: lines,
      boxW: Math.min(maxWidth, Math.max(20, textW)) + padX * 2,
      boxH: lines.length * lineH + padY * 2,
    };
    if (Object.keys(_bubbleLayoutCache).length > 300) _bubbleLayoutCache = {};
    _bubbleLayoutCache[note] = out;
    return out;
  }

  // 文本折行：按 maxWidth 切分，支持显式 \n
  function wrapText(text, maxWidth) {
    annoCtx.font = BUBBLE_FONT;
    var lines = [];
    String(text).split("\n").forEach(function (para) {
      if (para === "") { lines.push(""); return; }
      var cur = "";
      for (var i = 0; i < para.length; i++) {
        var test = cur + para[i];
        if (annoCtx.measureText(test).width > maxWidth && cur) {
          lines.push(cur);
          cur = para[i];
        } else {
          cur = test;
        }
      }
      if (cur) lines.push(cur);
    });
    return lines;
  }

  // 取标注锚点（屏幕坐标）+ 标记 bbox 最短边
  function annoAnchor(it) {
    var typ = it.type || "rect";
    if (typ === "rect") {
      // §6.2 旋转/镜像下取投影四边形外接框
      var bb = (rectScreenQuad(it).bbox) || { x: 0, y: 0, w: 0, h: 0 };
      return { x: bb.x + bb.w / 2, y: bb.y, minSide: Math.min(bb.w, bb.h) };
    } else if (typ === "arrow") {
      var a = imgToCanvas(it.x1, it.y1), b = imgToCanvas(it.x2, it.y2);
      return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, minSide: 40 };
    } else if (typ === "freehand" || typ === "polygon") {
      var pts = (it.points || []).map(function (p) { return imgToCanvas(p[0], p[1]); });
      var xs = pts.map(function (p) { return p.x; });
      var ys = pts.map(function (p) { return p.y; });
      var minx = Math.min.apply(null, xs), maxx = Math.max.apply(null, xs);
      var miny = Math.min.apply(null, ys), maxy = Math.max.apply(null, ys);
      return { x: (minx + maxx) / 2, y: miny, minSide: Math.min(maxx - minx, maxy - miny) };
    }
    return { x: 0, y: 0, minSide: 0 };
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function drawNoteBubble(it, note, selected) {
    var anchor = annoAnchor(it);
    // 缩放阈值：标记屏幕 bbox 最短边 < 24px 不画气泡（太挤）
    if (anchor.minSide < 24) return;
    var c = els.annoCanvas;
    var canvasW = c.clientWidth, canvasH = c.clientHeight;

    // 布局走缓存（避免每帧逐字符 measureText）
    var layout = bubbleLayout(note);
    var lines = layout.lines;
    var boxW = layout.boxW, boxH = layout.boxH;
    var padX = 8, padY = 6, lineH = 15;

    // 默认位置：锚点正上方
    var cx = anchor.x;
    var above = true;
    var boxX = cx - boxW / 2;
    var boxY = anchor.y - 8 - boxH;  // 上方，留 8px 给三角

    // 边界处理：上方不够 → 翻到下方
    if (boxY < 4) {
      above = false;
      boxY = anchor.y + 10;
    }
    // 左右溢出 → 平移到内侧
    if (boxX < 4) boxX = 4;
    if (boxX + boxW > canvasW - 4) boxX = canvasW - 4 - boxW;
    // 下方溢出 → 限制
    if (boxY + boxH > canvasH - 4) {
      boxY = Math.max(4, canvasH - 4 - boxH);
    }

    var borderColor = selected ? "#007AFF" : "rgba(0,0,0,0.15)";
    // 三角引线
    var triSize = 6;
    var triTipX = cx;
    annoCtx.fillStyle = "rgba(0,0,0,0.15)";
    annoCtx.strokeStyle = borderColor;
    annoCtx.lineWidth = 1;
    annoCtx.save();
    annoCtx.globalAlpha = 0.85;
    annoCtx.fillStyle = "#ffffff";
    roundRect(annoCtx, boxX, boxY, boxW, boxH, 8);
    annoCtx.fill();
    annoCtx.globalAlpha = 1;
    annoCtx.stroke();
    annoCtx.restore();

    // 三角（指向锚点）：根据 above 决定朝向
    annoCtx.save();
    annoCtx.fillStyle = "#ffffff";
    annoCtx.strokeStyle = borderColor;
    annoCtx.lineWidth = 1;
    annoCtx.beginPath();
    if (above) {
      var baseY = boxY + boxH;
      annoCtx.moveTo(triTipX - triSize, baseY - 0.5);
      annoCtx.lineTo(triTipX, baseY + triSize);
      annoCtx.lineTo(triTipX + triSize, baseY - 0.5);
    } else {
      var baseY2 = boxY;
      annoCtx.moveTo(triTipX - triSize, baseY2 + 0.5);
      annoCtx.lineTo(triTipX, baseY2 - triSize);
      annoCtx.lineTo(triTipX + triSize, baseY2 + 0.5);
    }
    annoCtx.closePath();
    annoCtx.fill();
    // 三角朝锚点的那条边不描边（用白线盖掉）
    annoCtx.stroke();
    annoCtx.restore();

    // 文字
    annoCtx.fillStyle = "#333";
    annoCtx.font = "12px " + "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif";
    annoCtx.textBaseline = "top";
    lines.forEach(function (ln, i) {
      annoCtx.fillText(ln, boxX + padX, boxY + padY + i * lineH);
    });
  }

  // ---------- 显示/隐藏标记（默认开） ----------
  // 同步所有相关按钮的 active 态：旧 #anno-all-btn + 新面板头部 #anno-all-toggle
  function syncAnnoAllBtns() {
    if (els.annoAllBtn) els.annoAllBtn.classList.toggle("active", state.showAnno);
    if (els.annoAllToggle) {
      els.annoAllToggle.classList.toggle("active", state.showAnno);
      els.annoAllToggle.setAttribute("aria-pressed", state.showAnno ? "true" : "false");
    }
  }
  function toggleAnnoAll() {
    // 👁 =「显示全部标记」语义：若当前处于"只看选中那条"的 focus 状态，
    // 先清空 focus 恢复显示全部；否则在「显示全部 ↔ 全部隐藏」之间切换。
    // （画布层非绘制时 pointer-events:none，无法点空白取消 focus，故由该钮兜底。）
    if (state.focusAnno) {
      state.focusAnno = null;
      state.showAnno = true;
    } else {
      state.showAnno = !state.showAnno;
    }
    syncAnnoAllBtns();
    redrawAnnoCanvas();
  }

  // =========================================================================
  // 编辑模式：非绘制模式下点击标注画布层，命中检测 + 选中 + 拖动手柄
  // =========================================================================
  // 点到线段距离（屏幕坐标）
  function pointSegDist(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1, dy = y2 - y1;
    var len2 = dx * dx + dy * dy;
    if (len2 <= 0) return Math.hypot(px - x1, py - y1);
    var t = ((px - x1) * dx + (py - y1) * dy) / len2;
    if (t < 0) t = 0; else if (t > 1) t = 1;
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }

  // 射线法判断点是否在多边形内（屏幕坐标）
  function pointInPolygon(px, py, pts) {
    var inside = false;
    for (var i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      var xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
      var intersect = ((yi > py) !== (yj > py)) &&
        (px < (xj - xi) * (py - yi) / ((yj - yi) || 1e-9) + xi);
      if (intersect) inside = !inside;
    }
    return inside;
  }

  // 命中检测：屏幕坐标 → 命中的 currentRois 项（倒序遍历，取最上层）
  // 返回命中的 item（currentRois 中的原始对象）或 null
  function hitAnno(sx, sy) {
    for (var i = currentRois.length - 1; i >= 0; i--) {
      var it = currentRois[i];
      var typ = it.type || "rect";
      if (typ === "rect") {
        // §6.2：旋转/镜像下命中投影四边形（内部或边线 ≤6px）
        var sq = rectScreenQuad(it);
        if (sq.quad && AG.pointInQuad(sx, sy, sq.quad, 6)) return it;
      } else if (typ === "arrow") {
        var a = imgToCanvas(it.x1, it.y1), b = imgToCanvas(it.x2, it.y2);
        if (AG.pointSegDist(sx, sy, a.x, a.y, b.x, b.y) <= 8) return it;
      } else if (typ === "polygon" || typ === "freehand") {
        // 注意：必须是 [x, y] 数组（pointInPolygon 按 pts[i][0/1] 索引；
        // 旧实现传 {x,y} 对象 → 内部命中恒假，仅边线距离可用）
        var pts = (it.points || []).map(function (p) {
          var c = imgToCanvas(p[0], p[1]);
          return [c.x, c.y];
        });
        if (pts.length >= 3 && AG.pointInPolygon(sx, sy, pts)) return it;
        for (var k = 0; k < pts.length - 1; k++) {
          if (AG.pointSegDist(sx, sy, pts[k].x, pts[k].y,
                              pts[k + 1].x, pts[k + 1].y) <= 8) return it;
        }
      }
    }
    return null;
  }

  // 判断某 item 是否可编辑（分享端仅 source===me 可编辑）
  function isEditable(it) {
    return it && it.source === "me";
  }

  // 取编辑手柄列表（屏幕坐标）。rect：4角+4边中点；arrow：两端点；freehand/polygon：
  // bbox 4角+中心。polygon「编辑轮廓点」模式 → 顶点+边中点加顶点柄。
  // 返回 [{id, x, y}]；只读/锁定项返回空（无手柄，§6.1/§6.3）
  function editHandles(it) {
    if (!isEditable(it) || itemGeometryLocked(it)) return [];
    var typ = it.type || "rect";
    var out = [];
    if (typ === "rect") {
      // §6.2：手柄位置 = 图像坐标角/边中点逐一投影（旋转/镜像跟随真实顶点）
      var w0 = rectItemW(it), h0 = rectItemH(it);
      var corners = [
        { id: "tl", ix: it.x, iy: it.y },
        { id: "t", ix: it.x + w0 / 2, iy: it.y },
        { id: "tr", ix: it.x + w0, iy: it.y },
        { id: "r", ix: it.x + w0, iy: it.y + h0 / 2 },
        { id: "br", ix: it.x + w0, iy: it.y + h0 },
        { id: "b", ix: it.x + w0 / 2, iy: it.y + h0 },
        { id: "bl", ix: it.x, iy: it.y + h0 },
        { id: "l", ix: it.x, iy: it.y + h0 / 2 },
      ];
      out = corners.map(function (c) {
        var p = imgToCanvas(c.ix, c.iy);
        return { id: c.id, x: p.x, y: p.y };
      });
    } else if (typ === "arrow") {
      var a = imgToCanvas(it.x1, it.y1), b = imgToCanvas(it.x2, it.y2);
      out = [
        { id: "p1", x: a.x, y: a.y },
        { id: "p2", x: b.x, y: b.y },
        { id: "mid", x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
      ];
    } else if (typ === "polygon") {
      if (state.polygonVertexEdit === it) {
        var vp = (it.points || []).map(function (p) {
          var c = imgToCanvas(p[0], p[1]);
          return { x: c.x, y: c.y };
        });
        for (var vi = 0; vi < vp.length; vi++) {
          out.push({ id: "v" + vi, x: vp[vi].x, y: vp[vi].y, vertex: true });
        }
        for (var ei = 0; ei < vp.length; ei++) {
          var e1 = vp[ei], e2 = vp[(ei + 1) % vp.length];
          out.push({ id: "n" + ei, x: (e1.x + e2.x) / 2, y: (e1.y + e2.y) / 2,
                     insertAfter: ei });
        }
        return out;
      }
      var pb = AG.pointsBBox(it.points || []);
      if (!pb) return out;
      var ptl = imgToCanvas(pb.x, pb.y), pbr = imgToCanvas(pb.x + pb.w, pb.y + pb.h);
      var px = Math.min(ptl.x, pbr.x), py = Math.min(ptl.y, pbr.y);
      var pw = Math.abs(pbr.x - ptl.x), ph = Math.abs(pbr.y - ptl.y);
      out = [
        { id: "ftl", x: px, y: py },
        { id: "ftr", x: px + pw, y: py },
        { id: "fbr", x: px + pw, y: py + ph },
        { id: "fbl", x: px, y: py + ph },
        { id: "fmid", x: px + pw / 2, y: py + ph / 2 },
      ];
    } else if (typ === "freehand") {
      var xs = it.points.map(function (p) { return p[0]; });
      var ys = it.points.map(function (p) { return p[1]; });
      var minx = Math.min.apply(null, xs), miny = Math.min.apply(null, ys);
      var maxx = Math.max.apply(null, xs), maxy = Math.max.apply(null, ys);
      var tl2 = imgToCanvas(minx, miny), br2 = imgToCanvas(maxx, maxy);
      var x2 = Math.min(tl2.x, br2.x), y2 = Math.min(tl2.y, br2.y);
      var w2 = Math.abs(br2.x - tl2.x), h2 = Math.abs(br2.y - tl2.y);
      out = [
        { id: "ftl", x: x2, y: y2 },
        { id: "ftr", x: x2 + w2, y: y2 },
        { id: "fbr", x: x2 + w2, y: y2 + h2 },
        { id: "fbl", x: x2, y: y2 + h2 },
        { id: "fmid", x: x2 + w2 / 2, y: y2 + h2 / 2 },
      ];
    }
    return out;
  }

  // 手柄命中容差（CSS px）：视觉柄可小，命中范围桌面 ≈16px、触摸 ≈28px（§6.1）
  function handleHitTolerance() {
    var coarse = false;
    try {
      coarse = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
    } catch (e) {}
    return coarse ? 14 : 8;
  }

  // 命中手柄：返回柄对象（含 id/insertAfter）或 null
  function hitHandle(sx, sy, it) {
    var hs = editHandles(it);
    var tol = handleHitTolerance();
    for (var i = 0; i < hs.length; i++) {
      if (Math.hypot(sx - hs[i].x, sy - hs[i].y) <= tol) return hs[i];
    }
    return null;
  }

  // 选中标注：设置 editItem，打开就近工具条（可编辑时），重绘。
  // §6.1：选中只是"查看"（高亮），不再隐藏其它标记；无可拖手柄；
  // 要移动/缩放必须先点「✎ 编辑」（或对未锁可编辑对象直接拖动——§6.2 路由）。
  function selectEditItem(it) {
    editItem = it;
    state.editing = false;
    state.polygonVertexEdit = null;
    redrawAnnoCanvas();
    if (isEditable(it)) {
      openAnnoToolbar();
      openEditCard(it);
    } else {
      openAnnoToolbar();  // 只读：给一枚说明态徽章（为什么没有手柄）
      closeEditCard();
    }
  }

  function clearEditItem() {
    editItem = null;
    state.editing = false;
    state.polygonVertexEdit = null;
    closeAnnoToolbar();
    closeEditCard();
    redrawAnnoCanvas();
  }

  // 编辑卡：选区面板顶部，备注 textarea + 操作按钮
  // 显式编辑态：非编辑态只显示「✎ 编辑」入口，点它才进入可拖动编辑态；
  // 备注 textarea 两种状态下都可直接改（备注改动不属于"移动"）。
  function openEditCard(it) {
    // 面板未开时自动打开
    if (els.panel.style.display === "none") {
      els.panel.style.display = "flex";
      if (els.panelMask) els.panelMask.style.display = "block";
      loadRoiPanel();
    }
    if (!els.panelEdit) return;
    var typ = it.type || "rect";
    var titleText =
      typ === "arrow" ? t("edit.title.arrow") :
      typ === "polygon" ? t("edit.title.polygon") :
      (typ === "freehand" ? t("edit.title.free") : t("edit.title.rect"));
    if (itemGeometryLocked(it)) titleText += " 🔒";
    els.panelEdit.innerHTML = "";
    var card = document.createElement("div");
    card.className = "anno-edit-card";
    var head = document.createElement("div");
    head.className = "aec-head";
    head.textContent = titleText;
    card.appendChild(head);
    var ta = document.createElement("textarea");
    ta.className = "aec-note";
    ta.maxLength = 500;
    ta.placeholder = t("edit.note.ph");
    ta.value = it.note || "";
    ta.rows = 2;
    card.appendChild(ta);
    var ops = document.createElement("div");
    ops.className = "aec-ops";
    if (state.editing) {
      // 编辑态：保存 / 取消 / 删除
      var saveB = document.createElement("button");
      saveB.className = "aec-btn primary"; saveB.textContent = t("edit.save");
      var cancelB = document.createElement("button");
      cancelB.className = "aec-btn"; cancelB.textContent = t("edit.cancel");
      var delB = document.createElement("button");
      delB.className = "aec-btn danger"; delB.textContent = t("edit.del");
      ops.appendChild(delB); ops.appendChild(cancelB); ops.appendChild(saveB);
      card.appendChild(ops);
      els.panelEdit.appendChild(card);
      els.panelEdit.style.display = "block";

      saveB.addEventListener("click", function () { commitEdit(it, ta.value); });
      cancelB.addEventListener("click", function () { cancelEdit(it); });
      delB.addEventListener("click", function () {
        delB.disabled = true;
        deleteRoi(it.index);
      });
      ta.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); commitEdit(it, ta.value); }
      });
    } else {
      // 非编辑态：✎ 编辑 / 保存 / 删除
      var editB = document.createElement("button");
      editB.className = "aec-btn"; editB.textContent = t("edit.enter");
      editB.title = t("edit.enter.title");
      var saveB2 = document.createElement("button");
      saveB2.className = "aec-btn primary"; saveB2.textContent = t("edit.save");
      var delB2 = document.createElement("button");
      delB2.className = "aec-btn danger"; delB2.textContent = t("edit.del");
      ops.appendChild(delB2); ops.appendChild(editB); ops.appendChild(saveB2);
      card.appendChild(ops);
      els.panelEdit.appendChild(card);
      els.panelEdit.style.display = "block";

      editB.addEventListener("click", function () {
        state.editing = true;
        it.__editStartGeom = snapshotGeom(it);  // §6.3 差量比较基准
        redrawAnnoCanvas();
        openEditCard(it);
      });
      saveB2.addEventListener("click", function () { commitEdit(it, ta.value); });
      delB2.addEventListener("click", function () {
        delB2.disabled = true;
        deleteRoi(it.index);
      });
      ta.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); commitEdit(it, ta.value); }
      });
    }
  }

  function closeEditCard() {
    if (els.panelEdit) { els.panelEdit.innerHTML = ""; els.panelEdit.style.display = "none"; }
  }

  // 收集编辑后几何（图片坐标，round 整数，clamp ≥0）
  // §5.4：polygon 保留原类型（points 序列化），不改写成 freehand
  function buildEditGeom(it) {
    var typ = it.type || "rect";
    var g = {};
    if (typ === "rect") {
      // 升级 C：成对 w/h 提交（v2 契约；正方形 side_px 兼容由服务端归一）
      g.x = Math.max(0, Math.round(it.x));
      g.y = Math.max(0, Math.round(it.y));
      g.w = clamp(Math.round(rectItemW(it)), 1, 40000);
      g.h = clamp(Math.round(rectItemH(it)), 1, 40000);
    } else if (typ === "arrow") {
      g.x1 = Math.max(0, Math.round(it.x1));
      g.y1 = Math.max(0, Math.round(it.y1));
      g.x2 = Math.max(0, Math.round(it.x2));
      g.y2 = Math.max(0, Math.round(it.y2));
    } else if (typ === "freehand" || typ === "polygon") {
      g.points = (it.points || []).map(function (p) {
        return [Math.max(0, Math.round(p[0])), Math.max(0, Math.round(p[1]))];
      });
    }
    return g;
  }

  // 几何差量比较（§6.3：备注更新不夹带 geom，否则锁定后改备注会被当成移动拒绝）
  function normalizeGeomForCompare(g) {
    if (!g) return {};
    var out = {};
    if (g.points) {
      out.points = g.points.map(function (p) {
        return [Math.round(Number(p[0]) || 0), Math.round(Number(p[1]) || 0)];
      });
      return out;
    }
    ["x", "y", "w", "h", "x1", "y1", "x2", "y2"].forEach(function (k) {
      if (g[k] != null) out[k] = Math.round(Number(g[k]) || 0);
    });
    return out;
  }
  function geomEqual(a, b) {
    return JSON.stringify(normalizeGeomForCompare(a)) ===
           JSON.stringify(normalizeGeomForCompare(b));
  }

  // 提交编辑：差量 PATCH（几何没变只发 note）+ expected_revision。
  // 409 按错误码区分：annotation_locked ≠ revision_conflict（§6.4）。
  function commitEdit(it, noteVal) {
    var geom = buildEditGeom(it);
    var before = it.__editStartGeom || null;
    var geomChanged = !before || !geomEqual(before, geom);
    var body = { note: noteVal };
    if (geomChanged) body.geom = geom;
    if (Number(it.revision) > 0) body.expected_revision = Number(it.revision);
    // rect 的 size_mm 前端重算（仅真正方形的兼容展示字段）
    if (geomChanged && (it.type || "rect") === "rect" && geom.w === geom.h &&
        state.mppX && state.mppX > 0) {
      body.geom.size_mm = Math.round(geom.w * state.mppX / 1000 * 100) / 100;
    }
    fetch(API + "/api/roi/" + it.index, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        if (r.status === 409) {
          // CAS 冲突按错误码区分（§6.4）：他人锁定 → 结束编辑说明原因；
          // 版本冲突 → 保留本地草稿 + 读回服务端版本（refreshRoisOnce）供比较
          return r.json().catch(function () { return {}; }).then(function (j) {
            if (j.error === "annotation_locked") {
              toast(t("anno.error.locked", {
                rev: j.current && j.current.revision != null ? j.current.revision : "?",
              }), "error");
              editItem = null;
              state.editing = false;
              state.polygonVertexEdit = null;
              closeAnnoToolbar();
              closeEditCard();
              refreshRoisOnce();
            } else {
              var serverRev = (j.current && j.current.revision != null)
                ? j.current.revision : j.current_revision;
              if (serverRev != null) it.revision = serverRev;
              toast(t("edit.conflict.keep", {
                rev: serverRev != null ? serverRev : "?",
              }), "error");
              // 保留本地草稿与编辑卡（不覆盖远端；用户可基于最新版本重试保存）
            }
            return { conflict: true };
          });
        }
        if (!r.ok) return r.json().then(function (j2) { throw new Error(j2.error || t("save.fail")); });
        return r.json();
      })
      .then(function (out) {
        if (out && out.conflict) return;
        toast(t("edit.saved"), "success");
        editItem = null;
        state.editing = false;
        state.polygonVertexEdit = null;
        closeAnnoToolbar();
        closeEditCard();
        refreshRoisOnce();
      })
      .catch(function (e) { toast(t("save.fail2", { e: e.message }), "error"); });
  }

  // 取消编辑：恢复原几何，清除选中
  function cancelEdit(it) {
    // 从服务端重新拉取以恢复（最稳妥）
    editItem = null;
    state.editing = false;
    closeEditCard();
    loadCurrentRois();
  }

  // =========================================================================
  // §6.1/§6.2/§6.3 对象交互核心（分享页）：
  //   - viewer 容器捕获阶段路由（一次 pointer 会话只交给一个处理器）
  //   - 就近工具条（可编辑/只读两形态；出视窗翻转）
  //   - geometry_locked 锁定/解锁（单独请求；PATCH /s/<token>/api/roi/<index>）
  //   - polygon 编辑轮廓点（⋯ 入口；完成时校验非自交）
  // =========================================================================
  var viewerSpacePan = false;
  var pendingAnnoSelect = null;
  var SHARE_EDIT_CLICK_SLOP_PX = 6;

  function canDragShareItem(it) {
    return !!it && itemCanEdit(it) && !itemGeometryLocked(it);
  }

  // 矩形草稿工具是否激活（与 arrow/freehand 互斥）
  function rectModeActive() {
    return state.roiMode != null;
  }

  function consumePointer(e) {
    if (e && e.preventDefault) e.preventDefault();
    if (e && e.stopPropagation) e.stopPropagation();
  }

  function isViewerSurfaceTarget(e) {
    var t = e && e.target;
    if (!t || !t.closest) return true;
    if (t.closest(".anno-ui-layer")) return false;
    if (t.closest("#roi-box")) return false;
    if (t.closest(".channel-panel")) return false;
    return true;
  }

  function routeObjectPointerDown(e) {
    if (!state.slide || !viewer) return;
    if (e && e.button === 2) return;
    if (!isViewerSurfaceTarget(e)) return;
    if (viewerSpacePan) return;                      // §6.2：Space 平移 > 显式绘制
    if (rectModeActive() || state.drawMode) return;  // 显式绘制：画布层自有路径
    if (!state.showAnno) return;
    var sp = screenPt(e);
    // 1) 已选中可编辑对象：控制点命中 → 调整（§6.2「可直接拖控制点调整」；
    //    拖完仍等用户点「保存」——分享端显式保存契约不变）
    if (editItem && canDragShareItem(editItem)) {
      var hd = hitHandle(sp.x, sp.y, editItem);
      if (hd) {
        consumePointer(e);
        startEditDrag(e, editItem, hd, viewer.container);
        return;
      }
    }
    var hit = hitAnno(sp.x, sp.y);
    // 2) 已选中、未锁、可编辑本体 → 移动整个对象（消费）
    if (hit && hit === editItem && canDragShareItem(hit)) {
      consumePointer(e);
      startEditDrag(e, hit, "move", viewer.container);
      updateAnnoToolbarPos();
      return;
    }
    // 3) 其它：不消费——拖动=平移切片；单击=选择/锁定态/取消选择
    pendingAnnoSelect = { hit: hit, sx: e.clientX, sy: e.clientY, id: e.pointerId };
  }

  function routeObjectPointerMove(e) {
    if (pendingAnnoSelect && e.pointerId === pendingAnnoSelect.id &&
        Math.hypot(e.clientX - pendingAnnoSelect.sx,
                   e.clientY - pendingAnnoSelect.sy) > SHARE_EDIT_CLICK_SLOP_PX) {
      pendingAnnoSelect = null;
    }
    if (!editDrag || editDrag.captureEl !== viewer.container) return;
    consumePointer(e);
    applyEditDrag(e);
    updateAnnoToolbarPos();
  }

  function routeObjectPointerUp(e) {
    if (editDrag && editDrag.captureEl === viewer.container) {
      consumePointer(e);
      endEditDrag(e);
      return;
    }
    if (pendingAnnoSelect && e.pointerId === pendingAnnoSelect.id) {
      var sel = pendingAnnoSelect;
      pendingAnnoSelect = null;
      if (Math.hypot(e.clientX - sel.sx, e.clientY - sel.sy) > SHARE_EDIT_CLICK_SLOP_PX) return;
      if (sel.hit) {
        if (sel.hit !== editItem) selectEditItem(sel.hit);
        else openAnnoToolbar();
      } else {
        clearEditItem();
      }
    }
  }

  function routeObjectPointerCancel(e) {
    pendingAnnoSelect = null;
    if (editDrag && editDrag.captureEl === viewer.container) {
      cancelEditDragRestore();
    }
  }

  // polygon 编辑轮廓点：双击顶点删除（保持 ≥3 点）
  function routeObjectDblClick(e) {
    if (!state.polygonVertexEdit || !editItem) return;
    var sp = screenPt(e);
    var hd = hitHandle(sp.x, sp.y, editItem);
    if (!hd || hd.id.charAt(0) !== "v") return;
    var vi = parseInt(hd.id.slice(1), 10) || 0;
    if (!editItem.points || editItem.points.length <= 3) {
      toast(t("anno.polygon.minpoints"), "info");
      return;
    }
    editItem.points.splice(vi, 1);
    consumePointer(e);
    redrawAnnoCanvas();
  }

  function onSpaceKeyForPan(e) {
    var tgt = e.target;
    var tag = tgt && tgt.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
        (tgt && tgt.isContentEditable)) return;
    if (e.key !== " ") return;
    if (e.type === "keydown" && !e.repeat) viewerSpacePan = true;
    else if (e.type === "keyup") viewerSpacePan = false;
  }

  function togglePolygonVertexEdit(it) {
    if (!it || (it.type || "rect") !== "polygon" || !canDragShareItem(it)) {
      if (it) toast(t("anno.polygon.readonly"), "info");
      return;
    }
    if (state.polygonVertexEdit === it) { finishPolygonVertexEdit(it); return; }
    state.polygonVertexEdit = it;
    it.__vertexEditStart = snapshotGeom(it);
    // （顶点/加顶点柄由 editHandles 的 polygonVertexEdit 分支渲染；
    //   state.editing 只表示备注卡模式，不再承担柄显示职责）
    closeAnnoToolbarMenu();
    renderAnnoToolbar();
    redrawAnnoCanvas();
    toast(t("anno.polygon.editpoints.tip"), "info");
  }

  function finishPolygonVertexEdit(it) {
    var pts = (it.points || []).map(function (p) {
      return [Math.max(0, Math.round(p[0])), Math.max(0, Math.round(p[1]))];
    });
    if (pts.length < 3) { toast(t("anno.polygon.minpoints"), "error"); return; }
    if (AG.polygonSelfIntersects(pts)) {
      // §6.2：完成时校验非自交——不通过留在编辑模式（不静默修正/提交）
      toast(t("anno.polygon.selfintersects"), "error");
      return;
    }
    state.polygonVertexEdit = null;
    it.__vertexEditStart = null;
    renderAnnoToolbar();
    redrawAnnoCanvas();
    toast(t("edit.need.save"), "info");  // 分享端显式保存（编辑卡「保存」提交）
  }

  // ---- 就近工具条（§6.1；分享页形态：已存标记 / 锁定对象 / 只读） ----
  var annoToolbarEl = null;
  var annoToolbarMenuEl = null;
  var annoToolbarRenderKey = null;

  function ensureAnnoToolbar() {
    if (annoToolbarEl && annoToolbarEl.parentNode) return annoToolbarEl;
    annoToolbarEl = document.createElement("div");
    annoToolbarEl.id = "anno-obj-toolbar";
    annoToolbarEl.className = "anno-ui-layer";
    annoToolbarEl.hidden = true;
    annoToolbarEl.addEventListener("pointerdown", function (e) {
      e.stopPropagation();
    });
    if (viewer && viewer.container) viewer.container.appendChild(annoToolbarEl);
    return annoToolbarEl;
  }

  function openAnnoToolbar(force) {
    var el = ensureAnnoToolbar();
    if (!el) return;
    var it = editItem;
    var key;
    if (it) {
      key = "item:" + it.index + "|" + (it.type || "rect") + "|" + Number(it.ts || 0);
    } else if (rectModeActive() && state.roi.side > 0) {
      key = "draft";
    } else {
      key = null;
    }
    if (!force && key !== null && key === annoToolbarRenderKey && el.childNodes.length) {
      el.hidden = false;
      updateAnnoToolbarPos();
      return;
    }
    annoToolbarRenderKey = key;
    renderAnnoToolbar();
    el.hidden = !key;
    updateAnnoToolbarPos();
  }

  function closeAnnoToolbar() {
    annoToolbarRenderKey = null;
    if (annoToolbarEl) annoToolbarEl.hidden = true;
    closeAnnoToolbarMenu();
  }

  function closeAnnoToolbarMenu() {
    if (annoToolbarMenuEl) { annoToolbarMenuEl.remove(); annoToolbarMenuEl = null; }
  }

  function annoToolbarButton(cls, text, title, onClick, disabled) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "aot-btn " + cls;
    b.textContent = text;
    if (title) b.title = title;
    if (disabled) b.disabled = true;
    if (onClick && !disabled) b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      closeAnnoToolbarMenu();
      onClick();
    });
    return b;
  }

  function renderAnnoToolbar() {
    var el = ensureAnnoToolbar();
    if (!el) return;
    closeAnnoToolbarMenu();
    el.innerHTML = "";
    // §6.1 临时框形态：无选中且矩形草稿存在 → [保存为标记] [锁定位置] [⋯]
    if (!editItem) {
      if (rectModeActive() && state.roi.side > 0) {
        el.appendChild(annoToolbarButton("aot-save", t("anno.toolbar.save"),
          t("anno.toolbar.save.tip"), function () { saveRoi(); },
          els.saveRoiBtn && els.saveRoiBtn.disabled));
        el.appendChild(annoToolbarButton(
          "aot-lock" + (state.roiGeometryLocked ? " on" : ""),
          state.roiGeometryLocked ? "🔒 " + t("anno.lock.locked.short")
                                  : "🔒 " + t("anno.lock.lock"),
          state.roiGeometryLocked ? t("anno.lock.unlock") : t("anno.lock.lock.tip"),
          function () { toggleDraftGeometryLocked(); }));
        el.appendChild(annoToolbarButton("aot-more", "⋯", t("anno.toolbar.more"),
          function () { openAnnoToolbarMenu("draft"); }));
      } else {
        el.hidden = true;
      }
      return;
    }
    var it = editItem;
    el.hidden = false;
    var editable = itemCanEdit(it);
    var locked = itemGeometryLocked(it);
    if (!editable) {
      // 只读：无可拖手柄；徽章说明原因（§6.1/§6.3）
      el.appendChild(annoToolbarButton("aot-readonly", "🔒 " + t("anno.lock.readonly"),
        t("anno.lock.readonly.tip"), null, true));
      el.appendChild(annoToolbarButton("aot-more", "⋯", t("anno.toolbar.more"),
        function () { openAnnoToolbarMenu(it); }));
      return;
    }
    el.appendChild(annoToolbarButton("aot-note", "✎ " + t("anno.toolbar.note"),
      t("anno.toolbar.note.tip"), function () {
        state.editing = true;
        it.__editStartGeom = snapshotGeom(it);
        openEditCard(it);
        redrawAnnoCanvas();
      }));
    el.appendChild(annoToolbarButton(
      "aot-lock" + (locked ? " on" : ""),
      locked ? "🔓 " + t("anno.lock.unlock") : "🔒 " + t("anno.lock.lock"),
      locked ? t("anno.lock.unlock.tip") : t("anno.lock.lock.tip"),
      function () { toggleItemGeometryLocked(it, !locked); }));
    el.appendChild(annoToolbarButton("aot-more", "⋯", t("anno.toolbar.more"),
      function () { openAnnoToolbarMenu(it); }));
  }

  // 临时框草稿位置锁（§6.3：draft.geometryLocked 只对当前草稿有效；
  // saveRoi 的 POST 随之带上，保存成标记后不因刷新意外解锁）
  function toggleDraftGeometryLocked() {
    state.roiGeometryLocked = !state.roiGeometryLocked;
    renderAnnoToolbar();
    toast(state.roiGeometryLocked ? t("anno.lock.locked") : t("anno.lock.unlocked"),
          "info");
  }

  function openAnnoToolbarMenu(target) {
    closeAnnoToolbarMenu();
    var el = ensureAnnoToolbar();
    if (!el) return;
    var menu = document.createElement("div");
    menu.className = "aot-menu";
    function addItem(label, title, onClick, disabled) {
      menu.appendChild(annoToolbarButton("aot-menu-item", label, title, onClick, disabled));
    }
    if (target === "draft") {
      addItem(t("anno.solo.off"), "", function () {
        state.focusAnno = null;
        redrawAnnoCanvas();
      }, !state.focusAnno);
      el.appendChild(menu);
      annoToolbarMenuEl = menu;
      return;
    }
    var it = target;
    var solo = (state.focusAnno === it);
    addItem(solo ? "👁 " + t("anno.solo.off") : "👁 " + t("anno.solo"),
      t("anno.solo.tip"), function () {
        state.focusAnno = solo ? null : it;
        redrawAnnoCanvas();
        renderAnnoToolbar();
      });
    if ((it.type || "rect") === "polygon" && canDragShareItem(it)) {
      var inEdit = (state.polygonVertexEdit === it);
      addItem(inEdit ? "✓ " + t("anno.polygon.finish") : "⬡ " + t("anno.polygon.editpoints"),
        t("anno.polygon.editpoints.tip"), function () {
          if (inEdit) finishPolygonVertexEdit(it);
          else togglePolygonVertexEdit(it);
          renderAnnoToolbar();
        });
    }
    var delDisabled = itemGeometryLocked(it);
    addItem("🗑 " + t("edit.del"),
      delDisabled ? t("anno.lock.delete.denied") : t("edit.del"),
      function () { deleteRoi(it.index); },
      delDisabled || !itemCanEdit(it));
    el.appendChild(menu);
    annoToolbarMenuEl = menu;
  }

  // 选中对象屏幕外接框（旋转/镜像取投影 bbox）
  function itemScreenBBox(it) {
    var typ = it.type || "rect";
    if (typ === "rect") return rectScreenQuad(it).bbox;
    var pts;
    if (typ === "arrow") {
      var a = imgToCanvas(it.x1, it.y1), b = imgToCanvas(it.x2, it.y2);
      pts = [[a.x, a.y], [b.x, b.y]];
    } else {
      pts = (it.points || []).map(function (p) {
        var c = imgToCanvas(p[0], p[1]);
        return [c.x, c.y];
      });
    }
    return AG.quadBBox(pts);
  }

  // 工具条定位：对象上方，顶部放不下翻到下方/内部（§6.1 出视窗翻转）
  function updateAnnoToolbarPos() {
    if (!annoToolbarEl || annoToolbarEl.hidden || !viewer) return;
    var bb;
    if (editItem) {
      bb = itemScreenBBox(editItem);
    } else if (rectModeActive() && state.roi.side > 0) {
      var q = AG.rectQuad(state.roi.x, state.roi.y, state.roi.side, state.roi.side,
                          imgToCanvas);
      bb = AG.quadBBox(q);
    } else {
      return;
    }
    if (!bb) { annoToolbarEl.hidden = true; return; }
    var tw = annoToolbarEl.offsetWidth, th = annoToolbarEl.offsetHeight;
    if (!tw) {
      annoToolbarEl.style.visibility = "hidden";
      tw = annoToolbarEl.offsetWidth; th = annoToolbarEl.offsetHeight;
      annoToolbarEl.style.visibility = "";
    }
    var vw = viewer.container.clientWidth, vh = viewer.container.clientHeight;
    var x = clamp(bb.x, 8, Math.max(8, vw - tw - 8));
    var y = bb.y - th - 8;
    if (y < 8) y = bb.y + bb.h + 8;
    if (y + th > vh - 8) y = clamp(bb.y + 8, 8, Math.max(8, vh - th - 8));
    annoToolbarEl.style.left = Math.round(x) + "px";
    annoToolbarEl.style.top = Math.round(y) + "px";
  }

  // ---- geometry_locked 切换（§6.3）：单独请求，绝不夹带 geom ----
  function toggleItemGeometryLocked(it, lockTarget) {
    if (!it || it.index == null) return;
    if (!itemCanEdit(it)) {
      toast(t("anno.lock.readonly.tip"), "info");
      return;
    }
    var body = { geometry_locked: !!lockTarget };
    if (Number(it.revision) > 0) body.expected_revision = Number(it.revision);
    fetch(API + "/api/roi/" + it.index, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: r.ok, status: r.status, j: j };
      });
    }).then(function (res) {
      if (res.ok) {
        it.geometry_locked = !!lockTarget;
        if (res.j && res.j.revision != null) it.revision = res.j.revision;
        toast(lockTarget ? t("anno.lock.locked") : t("anno.lock.unlocked"), "success");
        renderAnnoToolbar();
        renderRoiPanel(currentRois);
        redrawAnnoCanvas();
        return;
      }
      var j = res.j || {};
      if (res.status === 409 && j.error === "annotation_locked") {
        toast(t("anno.error.locked", {
          rev: j.current && j.current.revision != null ? j.current.revision : "?",
        }), "error");
      } else if (res.status === 409) {
        toast(t("edit.conflict", {
          rev: (j.current && j.current.revision) || j.current_revision || "?",
        }), "error");
      } else {
        toast(t("anno.update.fail3", { e: j.error || res.status }), "error");
      }
      refreshRoisOnce();
    }).catch(function (e) {
      toast(t("anno.update.fail3", { e: (e && e.message) || e }), "error");
    });
  }

  // ---------- 绘制工具（arrow / freehand） ----------
  var drawPreview = null;
  var drawPointer = null;

  function enterDrawMode(mode) {
    if (!state.slide) { toast(t("roi.need.slide"), "error"); return; }
    exitRoi();
    state.drawMode = mode;
    els.annoArrowBtn.classList.toggle("active", mode === "arrow");
    els.annoFreeBtn.classList.toggle("active", mode === "freehand");
    els.annoCanvas.classList.add("drawing");
    if (viewer) viewer.setMouseNavEnabled(false);
    syncAnnoAllBtns();
    redrawAnnoCanvas();
    updateCtxBar();
    toast(mode === "arrow" ? t("draw.arrow.tip") : t("draw.free.tip"), "info");
  }

  function exitDrawMode() {
    state.drawMode = null;
    drawPreview = null;
    drawPointer = null;
    if (els.annoArrowBtn) els.annoArrowBtn.classList.remove("active");
    if (els.annoFreeBtn) els.annoFreeBtn.classList.remove("active");
    if (els.annoCanvas) els.annoCanvas.classList.remove("drawing");
    if (viewer) viewer.setMouseNavEnabled(true);
    redrawAnnoCanvas();
    updateCtxBar();
  }

  function toggleDrawMode(mode) {
    if (state.drawMode === mode) { exitDrawMode(); return; }
    enterDrawMode(mode);
  }

  function onAnnoPointerDown(e) {
    if (!state.slide) return;
    // 绘制模式优先：走原有绘制逻辑
    if (state.drawMode) {
      if (e.button === 2) return; // 右键不启动绘制（工单 D）
      e.preventDefault(); e.stopPropagation();
      var c = els.annoCanvas;
      try { c.setPointerCapture(e.pointerId); } catch (err) {}
      drawPointer = { id: e.pointerId };
      var img0 = screenToImg(e);
      if (state.drawMode === "arrow") {
        drawPreview = { type: "arrow", x1: img0.x, y1: img0.y, x2: img0.x, y2: img0.y,
                        sx0: e.clientX, sy0: e.clientY };
      } else {
        drawPreview = { type: "freehand", points: [[img0.x, img0.y]], lastScreen: screenPt(e),
                        sx0: e.clientX, sy0: e.clientY };
      }
      redrawAnnoCanvas();
      return;
    }
    // 非绘制模式：编辑/选中（普通对象编辑走 viewer 容器捕获路由
    // routeObjectPointerDown；本画布路径只在 drawing 态可达）
    if (!state.showAnno) return;
    e.preventDefault(); e.stopPropagation();
    var sp = screenPt(e);
    // 点中手柄 → 拖动手柄（§6.2：选中即可拖柄；锁定/只读无柄）
    if (editItem && canDragShareItem(editItem)) {
      var hd = hitHandle(sp.x, sp.y, editItem);
      if (hd) {
        startEditDrag(e, editItem, hd);
        return;
      }
    }
    // 命中标注 → 重新选中查看（editing 复位，不直接平移；要改需先点"✎ 编辑"）
    var hit = hitAnno(sp.x, sp.y);
    if (hit) {
      selectEditItem(hit);
      return;
    }
    // 点空白 → 取消选中
    clearEditItem();
  }

  function onAnnoPointerMove(e) {
    if (state.drawMode && drawPreview) {
      e.preventDefault(); e.stopPropagation();
      var img = screenToImg(e);
      if (drawPreview.type === "arrow") {
        drawPreview.x2 = img.x; drawPreview.y2 = img.y;
      } else {
        var sp0 = screenPt(e);
        var last = drawPreview.lastScreen;
        if (Math.hypot(sp0.x - last.x, sp0.y - last.y) > 4) {
          drawPreview.points.push([img.x, img.y]);
          drawPreview.lastScreen = sp0;
          if (drawPreview.points.length >= 500) { finishDraw(); return; }
        }
      }
      redrawAnnoCanvas();
      return;
    }
    // 编辑拖动
    if (!editDrag) return;
    e.preventDefault(); e.stopPropagation();
    applyEditDrag(e);
  }

  function onAnnoPointerUp(e) {
    if (state.drawMode && drawPreview) {
      e.preventDefault(); e.stopPropagation();
      // 纯单击（无位移）的描图不产生有效几何：清草稿留在工具内，不保存（工单 D）
      if (drawPreview.type === "freehand" && drawPreview.points.length < 3 &&
          !drawPointerMoved(e)) {
        releaseDrawPointer();
        drawPreview = null;
        redrawAnnoCanvas();
        return;
      }
      finishDraw();
      return;
    }
    if (!editDrag) return;
    e.preventDefault(); e.stopPropagation();
    endEditDrag(e);
  }

  // 工单 D：pointercancel / 丢失指针捕获 = 取消恢复，绝不走完成/保存路径
  function onAnnoPointerCancel(e) {
    if (state.drawMode && drawPreview) {
      if (e && e.preventDefault) { try { e.preventDefault(); } catch (err) {} }
      releaseDrawPointer();
      drawPreview = null; // 丢弃被中断的笔画
      redrawAnnoCanvas();
      return;
    }
    if (editDrag) { cancelEditDragRestore(); }
  }

  function releaseDrawPointer() {
    var c = els.annoCanvas;
    if (drawPointer) { try { c.releasePointerCapture(drawPointer.id); } catch (err) {} }
    drawPointer = null;
  }

  // 本次按下是否有明显位移（屏幕 px）：区分「单击」与「拖动释放」
  function drawPointerMoved(e) {
    if (!drawPreview || drawPreview.sx0 == null || !e) return true; // 无记录按拖动处理
    return Math.hypot(e.clientX - drawPreview.sx0, e.clientY - drawPreview.sy0) > 4;
  }

  // ---------- 编辑拖动会话 ----------
  function startEditDrag(e, it, handle, el) {
    var c = el || els.annoCanvas;
    try { c.setPointerCapture(e.pointerId); } catch (err) {}
    editDrag = {
      pointerId: e.pointerId,
      captureEl: c,
      handle: handle && typeof handle === "object" ? handle.id : handle,
      insertAfter: handle && typeof handle === "object" ? handle.insertAfter : null,
      // 快照起始几何（图片坐标）+ 起始指针图像坐标
      item: it,
      start: snapshotGeom(it),
      startImg: screenToImg(e),
    };
    if (viewer) viewer.setMouseNavEnabled(false);
  }

  function snapshotGeom(it) {
    var typ = it.type || "rect";
    if (typ === "rect") {
      return { x: it.x, y: it.y, w: rectItemW(it), h: rectItemH(it) };
    } else if (typ === "arrow") {
      return { x1: it.x1, y1: it.y1, x2: it.x2, y2: it.y2 };
    } else if (typ === "freehand" || typ === "polygon") {
      return {
        points: (it.points || []).map(function (p) { return [p[0], p[1]]; }),
      };
    }
    return {};
  }

  function applyEditDrag(e) {
    var d = editDrag;
    var it = d.item;
    var typ = it.type || "rect";
    var cur = screenToImg(e);
    var dx = cur.x - d.startImg.x;
    var dy = cur.y - d.startImg.y;
    var s = d.start;

    if (typ === "rect") {
      if (d.handle === "move") {
        var Ws = state.slide.width, Hs = state.slide.height;
        it.x = clamp(Math.round(s.x + dx), 0, Math.max(0, Ws - s.w));
        it.y = clamp(Math.round(s.y + dy), 0, Math.max(0, Hs - s.h));
      } else {
        // 升级 C：角改双轴、边改单轴（不重新正方形化）
        var moveL = d.handle.indexOf("l") >= 0;
        var moveR = d.handle.indexOf("r") >= 0;
        var moveT = d.handle.indexOf("t") >= 0;
        var moveB = d.handle.indexOf("b") >= 0;
        var anchorX = (moveL || moveR) ? (moveL ? s.x + s.w : s.x) : s.x;
        var anchorY = (moveT || moveB) ? (moveT ? s.y + s.h : s.y) : s.y;
        var x0 = (moveL || moveR) ? anchorX : s.x;
        var x1 = (moveL || moveR) ? cur.x : s.x + s.w;
        var y0 = (moveT || moveB) ? anchorY : s.y;
        var y1 = (moveT || moveB) ? cur.y : s.y + s.h;
        var W2 = state.slide.width, H2 = state.slide.height;
        if ([x0, y0, x1, y1].every(isFinite)) {
          var nxx = Math.max(0, Math.round(Math.min(x0, x1)));
          var nyy = Math.max(0, Math.round(Math.min(y0, y1)));
          var nw = Math.min(Math.max(Math.round(Math.abs(x1 - x0)), 1), 40000);
          var nh = Math.min(Math.max(Math.round(Math.abs(y1 - y0)), 1), 40000);
          nxx = Math.min(nxx, Math.max(0, W2 - nw));
          nyy = Math.min(nyy, Math.max(0, H2 - nh));
          it.x = nxx; it.y = nyy; it.w = nw; it.h = nh;
        }
      }
    } else if (typ === "arrow") {
      if (d.handle === "p1") {
        it.x1 = Math.max(0, Math.round(s.x1 + dx));
        it.y1 = Math.max(0, Math.round(s.y1 + dy));
      } else if (d.handle === "p2") {
        it.x2 = Math.max(0, Math.round(s.x2 + dx));
        it.y2 = Math.max(0, Math.round(s.y2 + dy));
      } else if (d.handle === "mid") {
        // 整体平移
        var n1x = Math.max(0, Math.round(s.x1 + dx));
        var n1y = Math.max(0, Math.round(s.y1 + dy));
        var n2x = Math.max(0, Math.round(s.x2 + dx));
        var n2y = Math.max(0, Math.round(s.y2 + dy));
        it.x1 = n1x; it.y1 = n1y; it.x2 = n2x; it.y2 = n2y;
      }
    } else if (typ === "freehand" || typ === "polygon") {
      if (d.handle === "fmid" || d.handle === "move") {
        // 整体平移（§6.2）；越界整体收回边界（保持形状）
        var moved = AG.translatePoints(s.points, Math.round(dx), Math.round(dy));
        var shift = AG.pointsShiftIntoRect(moved, state.slide.width, state.slide.height);
        if (shift) moved = AG.translatePoints(moved, shift.dx, shift.dy);
        it.points = moved;
      } else if (d.handle && d.handle.charAt(0) === "v") {
        // polygon 编辑轮廓点：拖单个顶点
        var vi = parseInt(d.handle.slice(1), 10) || 0;
        var vpts = AG.clonePoints(s.points);
        if (vi < vpts.length) {
          vpts[vi] = [Math.max(0, Math.round(s.points[vi][0] + dx)),
                      Math.max(0, Math.round(s.points[vi][1] + dy))];
          it.points = vpts;
        }
      } else if (d.handle && d.handle.charAt(0) === "n" && d.insertAfter != null) {
        // polygon 加顶点（工作副本，不污染 start 快照）
        if (!d.inserted) {
          var base = AG.clonePoints(d.dragPoints || s.points);
          base.splice(d.insertAfter + 1, 0, [Math.round(cur.x), Math.round(cur.y)]);
          d.inserted = true;
          d.dragPoints = base;
        }
        var npts = AG.clonePoints(d.dragPoints);
        npts[d.insertAfter + 1] = [Math.max(0, Math.round(cur.x)), Math.max(0, Math.round(cur.y))];
        d.dragPoints = npts;
        it.points = npts;
      } else {
        // 角手柄：等比缩放（以对角为锚）——freehand 与 polygon 同形
        var pts = s.points;
        var xs0 = pts.map(function (p) { return p[0]; });
        var ys0 = pts.map(function (p) { return p[1]; });
        var minx0 = Math.min.apply(null, xs0), maxx0 = Math.max.apply(null, xs0);
        var miny0 = Math.min.apply(null, ys0), maxy0 = Math.max.apply(null, ys0);
        var w0 = Math.max(1, maxx0 - minx0), h0 = Math.max(1, maxy0 - miny0);
        // 锚角（对角）
        var aX = (d.handle === "ftl") ? maxx0 : minx0;
        var aY = (d.handle === "ftl") ? maxy0 : miny0;
        if (d.handle === "ftr") { aX = minx0; aY = maxy0; }
        if (d.handle === "fbr") { aX = minx0; aY = miny0; }
        if (d.handle === "fbl") { aX = maxx0; aY = miny0; }
        // 新尺寸（以指针位置为参考，保持纵横比用统一 scale）
        var newW = Math.max(2, Math.abs(cur.x - aX));
        var newH = Math.max(2, Math.abs(cur.y - aY));
        var scale = Math.max(newW / w0, newH / h0);
        var newPts = AG.scalePointsAbout(pts, aX, aY, scale).map(function (p) {
          return [Math.round(p[0]), Math.round(p[1])];
        });
        var shift2 = AG.pointsShiftIntoRect(newPts, state.slide.width, state.slide.height);
        if (shift2) newPts = AG.translatePoints(newPts, shift2.dx, shift2.dy);
        it.points = newPts;
      }
    }
    redrawAnnoCanvas();
  }

  function endEditDrag(e) {
    var d = editDrag;
    var c = d ? (d.captureEl || els.annoCanvas) : els.annoCanvas;
    if (d) {
      try { c.releasePointerCapture(d.pointerId); } catch (err) {}
    }
    editDrag = null;
    if (viewer) viewer.setMouseNavEnabled(true);
    // 分享端契约保持「拖完不立即保存，等用户点保存」（编辑卡保存按钮提交）
  }

  // 工单 D：编辑拖拽被打断 → 恢复拖前几何（本地快照），不保留半截修改
  function cancelEditDragRestore() {
    var d = editDrag;
    var c = d ? (d.captureEl || els.annoCanvas) : els.annoCanvas;
    if (d) { try { c.releasePointerCapture(d.pointerId); } catch (err) {} }
    editDrag = null;
    if (d && d.item && d.start) {
      var s = d.start, it = d.item;
      var typ = it.type || "rect";
      if (typ === "rect") {
        it.x = s.x; it.y = s.y; it.w = s.w; it.h = s.h;
      } else if (typ === "arrow") {
        it.x1 = s.x1; it.y1 = s.y1; it.x2 = s.x2; it.y2 = s.y2;
      } else if (typ === "freehand" || typ === "polygon") {
        it.points = s.points;
      }
    }
    if (viewer) viewer.setMouseNavEnabled(true);
    redrawAnnoCanvas();
    updateAnnoToolbarPos();
  }

  function finishDraw() {
    var dp = drawPreview;
    var c = els.annoCanvas;
    if (drawPointer) { try { c.releasePointerCapture(drawPointer.id); } catch (err) {} }
    drawPointer = null;
    drawPreview = null;
    if (!dp) { exitDrawMode(); return; }
    if (dp.type === "arrow") {
      var dist = Math.hypot(dp.x2 - dp.x1, dp.y2 - dp.y1);
      if (dist < 10) { toast(t("draw.short.cancel"), "info"); redrawAnnoCanvas(); return; }
      saveAnnotation({ type: "arrow", x1: dp.x1, y1: dp.y1, x2: dp.x2, y2: dp.y2 });
    } else {
      var pts = dp.points;
      if (pts.length < 3) { toast(t("draw.few.cancel"), "info"); redrawAnnoCanvas(); return; }
      var xs = pts.map(function (p) { return p[0]; });
      var ys = pts.map(function (p) { return p[1]; });
      var bb = Math.max(Math.max.apply(null, xs) - Math.min.apply(null, xs),
                        Math.max.apply(null, ys) - Math.min.apply(null, ys));
      if (bb < 10) { toast(t("draw.small.cancel"), "info"); redrawAnnoCanvas(); return; }
      saveAnnotation({ type: "freehand", points: pts });
    }
  }

  function screenToImg(e) {
    var rect = viewer.container.getBoundingClientRect();
    var p = viewer.viewport.viewerElementToImageCoordinates(
      new OpenSeadragon.Point(e.clientX - rect.left, e.clientY - rect.top));
    return { x: Math.round(p.x), y: Math.round(p.y) };
  }
  function screenPt(e) {
    var rect = viewer.container.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  // 保存用户标注（arrow/freehand）。工单 D：保存失败保留可重试草稿——
  // 恢复预览、不退工具、不清几何；只读/无标注权限的链接在服务端 403，
  // 前端不新增任何写入口。
  function saveAnnotation(geom) {
    if (!state.slide) return;
    var slideName = state.slide.name; // 冻结提交时的切片（回包不串片）
    var slideId = state.slide.id || null;  // P2：冻结 slide_id（操作键）
    var label = (els.roiLabel.value || "").trim();
    if (!label) {
      toast(t("share.label.need"), "error");
      try { els.roiLabel.focus(); } catch (e) {}
      // 草稿保留：恢复预览等用户补完用户名后重画/重试
      restorePreviewFromGeom(geom);
      return;
    }
    // P2 合同 §5.8：slide_id（权威）+ slide（名称快照）双字段并存
    var body = { slide: slideName, type: geom.type, label: label };
    if (slideId) body.slide_id = slideId;
    for (var k in geom) body[k] = geom[k];
    body.note = (els.roiNote ? els.roiNote.value : "") || "";
    fetch(API + "/api/roi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("save.fail")); });
        return r.json();
      })
      .then(function () {
        // 切片已切换：回包不落到新切片（只提示，不改新切片状态；P2：按 id
        // 比对，缺 id 回落名）
        var switched = !state.slide || (slideId
          ? String(state.slide.id) !== String(slideId)
          : state.slide.name !== slideName);
        if (switched) return;
        toast(t("anno.saved"), "success");
        exitDrawMode();
        refreshRoisOnce();
      })
      .catch(function (e) {
        // 失败：可重试草稿——恢复预览、留在工具内（工单 D）；切片已切换
        // 则不动新切片
        var switched = !state.slide || (slideId
          ? String(state.slide.id) !== String(slideId)
          : state.slide.name !== slideName);
        if (switched) return;
        restorePreviewFromGeom(geom);
        toast(t("draw.unsaved.retry", { e: e.message }), "error");
      });
  }

  // 保存路径恢复绘制预览（几何不清空）
  function restorePreviewFromGeom(geom) {
    if (!geom || !state.drawMode) return;
    if (geom.type === "arrow") {
      drawPreview = { type: "arrow", x1: geom.x1, y1: geom.y1,
                      x2: geom.x2, y2: geom.y2 };
    } else if (geom.type === "freehand" && geom.points) {
      drawPreview = { type: "freehand",
                      points: geom.points.map(function (p) { return [p[0], p[1]]; }),
                      lastScreen: null };
    }
    redrawAnnoCanvas();
  }

  // 加载当前切片的标注（本 token + 管理员）供画布层绘制
  // P2：roi 行携 slide_id 时按 id 匹配当前切片；无 id 历史行按名称快照匹配
  function roiMatchesCurrentSlide(r) {
    if (!state.slide || !r) return false;
    if (state.slide.id && r.slide_id) {
      return String(r.slide_id) === String(state.slide.id);
    }
    return r.slide === state.slide.name;
  }

  function loadCurrentRois() {
    if (!state.slide) { currentRois = []; editItem = null; state.focusAnno = null; state.editing = false; closeEditCard(); redrawAnnoCanvas(); return; }
    fetch(API + "/api/rois")
      .then(function (r) { return r.json(); })
      .then(function (rois) {
        currentRois = (rois || []).filter(roiMatchesCurrentSlide);
        // 若 editItem 已不在新列表，清除选中
        if (editItem && currentRois.indexOf(editItem) < 0) { editItem = null; state.editing = false; closeEditCard(); }
        if (state.focusAnno && currentRois.indexOf(state.focusAnno) < 0) { state.focusAnno = null; }
        redrawAnnoCanvas();
      })
      .catch(function () { currentRois = []; editItem = null; state.editing = false; closeEditCard(); state.focusAnno = null; redrawAnnoCanvas(); });
  }

  // ---------- 选区面板 ----------
  function loadRoiPanel() {
    fetch(API + "/api/rois")
      .then(function (r) {
        if (!r.ok) throw new Error(t("share.load.roi.fail"));
        return r.json();
      })
      .then(function (rois) {
        renderRoiPanel(rois || []);
      })
      .catch(function (e) { toast(e.message, "error"); });
  }

  // 一次拉取 /api/rois，同时刷新面板与画布层 currentRois。
  // 合并原先 loadRoiPanel + loadCurrentRois 的两次重复请求为一次拉取、两路渲染
  // （慢网/外网通道下每省一次请求就省一个 RTT）。
  function refreshRoisOnce() {
    return fetch(API + "/api/rois")
      .then(function (r) {
        if (!r.ok) throw new Error(t("share.load.roi.fail"));
        return r.json();
      })
      .then(function (rois) {
        rois = rois || [];
        // 面板渲染（原 loadRoiPanel 逻辑）
        renderRoiPanel(rois);
        // 画布层数据（原 loadCurrentRois 逻辑；P2：按 id 匹配，无 id 历史行按名）
        if (state.slide) {
          currentRois = rois.filter(roiMatchesCurrentSlide);
        } else {
          currentRois = [];
        }
        // 若 editItem 已不在新列表，清除选中
        if (editItem && currentRois.indexOf(editItem) < 0) { editItem = null; state.editing = false; closeEditCard(); }
        if (state.focusAnno && currentRois.indexOf(state.focusAnno) < 0) { state.focusAnno = null; }
        redrawAnnoCanvas();
      })
      .catch(function (e) { toast(e.message, "error"); });
  }

  function renderRoiPanel(rois) {
    els.panelToggle.textContent = t("share.roi.count.toggle", { n: rois.length });
    if (rois.length === 0) {
      els.panelList.innerHTML = '<div class="roi-panel-empty">' + esc(t("share.roi.panel.empty")) + "</div>";
      return;
    }
    els.panelList.innerHTML = "";
    rois.forEach(function (r, i) {
      var item = document.createElement("div");
      item.className = "roi-panel-item";

      var label = (r.label && String(r.label).trim()) || t("share.unnamed");
      var typ = r.type || "rect";
      var isAdmin = (r.source === "admin");
      var isShared = (r.source === "shared");
      var isMe = (r.source === "me");
      var icon = typ === "arrow" ? "↗" : (typ === "polygon" ? "⬠" : (typ === "freehand" ? "〰" : "▭"));

      // 类型图标
      var iconEl = document.createElement("div");
      iconEl.className = "rpi-icon";
      iconEl.textContent = icon;
      item.appendChild(iconEl);

      var info = document.createElement("div");
      info.className = "ri-info";
      var title = document.createElement("div");
      title.className = "ri-title";
      var lblEl = document.createElement("span");
      lblEl.className = "ri-label";
      lblEl.textContent = label;
      title.appendChild(lblEl);
      // 尺寸/坐标摘要
      var szEl = document.createElement("span");
      szEl.className = "rpi-size";
      if (typ === "rect" && r.size_mm > 0 && rectItemW(r) === rectItemH(r)) szEl.textContent = " · " + r.size_mm + "mm";
      else if (typ === "arrow") szEl.textContent = " · (" + r.x1 + "," + r.y1 + ")";
      else szEl.textContent = " · " + t("anno.free.points", { n: (r.points ? r.points.length : 0) });
      title.appendChild(szEl);
      // §6.3：锁定标记给锁徽章
      if (itemGeometryLocked(r)) {
        var lk = document.createElement("span");
        lk.className = "rpi-badge locked";
        lk.textContent = "🔒";
        lk.title = t("anno.lock.badge.tip");
        title.appendChild(lk);
      }
      if (isAdmin) {
        var badge = document.createElement("span");
        badge.className = "rpi-badge";
        badge.textContent = t("anno.default.user");
        title.appendChild(badge);
      } else if (isShared) {
        var sBadge = document.createElement("span");
        sBadge.className = "rpi-badge shared";
        sBadge.textContent = t("share.public");
        title.appendChild(sBadge);
      }
      var sub = document.createElement("div");
      sub.className = "ri-sub";
      sub.textContent = fmtTime(r.ts);
      info.appendChild(title);
      info.appendChild(sub);

      // 点击条目 → 跳转
      info.style.cursor = "pointer";
      info.addEventListener("click", function () { jumpToRoi(r); });

      item.appendChild(info);

      // 删除钮：仅本人的可删（admin / shared 不可删）
      if (isMe) {
        // 编辑钮：跳转到该标注并选中进入编辑态
        var editBtn = document.createElement("button");
        editBtn.className = "ri-edit";
        editBtn.textContent = "✎";
        editBtn.title = t("share.edit.title.short");
        editBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          jumpAndEdit(r);
        });
        item.appendChild(editBtn);

        var del = document.createElement("button");
        del.className = "ri-del";
        del.textContent = "×";
        del.title = t("edit.del");
        del.addEventListener("click", function (ev) {
          ev.stopPropagation();
          deleteRoi(r.index);
        });
        item.appendChild(del);
      }

      els.panelList.appendChild(item);
    });
  }

  function deleteRoi(index) {
    fetch(API + "/api/roi/" + index, { method: "DELETE" })
      .then(function (r) {
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (j) {
            // §6.3：锁定对象 DELETE → 409 annotation_locked（不当幂等成功）
            if (r.status === 409 && j.error === "annotation_locked") {
              toast(t("anno.lock.delete.denied"), "error");
              return { blocked: true };
            }
            throw new Error(j.error || t("del.fail"));
          });
        }
        return r.json();
      })
      .then(function (out) {
        if (out && out.blocked) { refreshRoisOnce(); return; }
        // ---- 乐观更新（同步执行，立即反馈）----
        // 从 currentRois 移除该项（仅本人 source==="me" 的可删；
        // 按 source 限定避免与 admin/shared 条目的 index 数值冲突误删）
        for (var i = currentRois.length - 1; i >= 0; i--) {
          var it = currentRois[i];
          if (it.index === index && it.source === "me") {
            if (editItem === it) { editItem = null; state.editing = false; }
            if (state.focusAnno === it) { state.focusAnno = null; }
            currentRois.splice(i, 1);
          }
        }
        closeEditCard();
        redrawAnnoCanvas();
        toast(t("share.del.done"), "success");
        // ---- 后台异步同步真实状态（一次拉取，面板与画布同时刷新）----
        refreshRoisOnce();
      })
      .catch(function (e) {
        toast(e.message, "error");
        // 失败恢复：重新拉取真实状态
        refreshRoisOnce();
      });
  }

  // 跳转到指定 ROI：切到对应切片，OSD open 后定位。
  // P2：优先按 roi 的 slide_id 定位操作键；无 id 历史行按名称快照
  function jumpToRoi(r) {
    var needSwitch = !roiMatchesCurrentSlide(r);
    // §6.1：定位 = 选中高亮（不再隐藏其它标记；「只看这条」在 ⋯ 内）
    var focusAfter = function () {
      var match = null;
      for (var i = 0; i < currentRois.length; i++) {
        var it = currentRois[i];
        if (it.index === r.index && it.token === r.token) { match = it; break; }
      }
      editItem = match || null;
      state.editing = false;
      state.polygonVertexEdit = null;
      closeEditCard();
      openAnnoToolbar();
      redrawAnnoCanvas();
    };
    if (needSwitch) {
      // 一次性监听 open 完成后定位
      var handler = function () {
        viewer.removeHandler("open", handler);
        doJump(r);
        // 等标注加载完再聚焦
        setTimeout(focusAfter, 300);
      };
      viewer.addHandler("open", handler);
      openSlide(r.slide_id || r.slide);
    } else {
      doJump(r);
      focusAfter();
    }
  }

  // 跳转并在加载完成后选中该标注进入编辑态
  function jumpAndEdit(r) {
    var needSwitch = !roiMatchesCurrentSlide(r);
    var doSelect = function () {
      // 在 currentRois 中找到匹配项（按 index+token）并选中
      var match = null;
      for (var i = 0; i < currentRois.length; i++) {
        var it = currentRois[i];
        if (it.index === r.index && it.token === r.token) { match = it; break; }
      }
      if (match) selectEditItem(match);
    };
    if (needSwitch) {
      var handler = function () {
        viewer.removeHandler("open", handler);
        doJump(r);
        // 等标注加载完再选中
        setTimeout(doSelect, 300);
      };
      viewer.addHandler("open", handler);
      openSlide(r.slide_id || r.slide);
    } else {
      doJump(r);
      doSelect();
    }
  }

  function doJump(r) {
    var typ = r.type || "rect";
    var x, y, side;
    if (typ === "arrow") {
      x = Math.min(r.x1, r.x2); y = Math.min(r.y1, r.y2);
      side = Math.max(Math.abs(r.x2 - r.x1), Math.abs(r.y2 - r.y1));
    } else if (typ === "freehand" || typ === "polygon") {
      var xs = r.points.map(function (p) { return p[0]; });
      var ys = r.points.map(function (p) { return p[1]; });
      x = Math.min.apply(null, xs); y = Math.min.apply(null, ys);
      side = Math.max(Math.max.apply(null, xs) - x, Math.max.apply(null, ys) - y);
    } else {
      // 升级 C：真实 w/h 包围；非正方形不冒充 side_px
      x = r.x; y = r.y; side = Math.max(rectItemW(r), rectItemH(r));
    }
    side = Math.max(side, 1);
    var pad = side * 0.2;
    // rect 用 rebuildRoi 重建选区框（仅正方形预设可选回）；非正方形仅定位
    if (typ === "rect" && rectItemW(r) === rectItemH(r)) {
      rebuildRoi(r.x, r.y, rectItemW(r), r.size_mm);
    }
    try {
      var rect = viewer.viewport.imageToViewportRectangle(
        x - pad, y - pad, side + pad * 2, side + pad * 2);
      viewer.viewport.fitBounds(rect);
    } catch (e) {}
    // 隐藏面板与遮罩
    els.panel.style.display = "none";
    if (els.panelMask) els.panelMask.style.display = "none";
  }

  function fmtTime(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    var p = function (n) { return n < 10 ? "0" + n : n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  // ---------- 移动端上下文动作条显隐 ----------
  // ROI 模式或箭头/描图绘制模式任一激活时，显示底部主栏上方的上下文条
  // （标记人/备注输入 + 保存选区/导出）。桌面端不受影响（display:contents）。
  function updateCtxBar() {
    var on = state.roiMode != null || state.drawMode != null;
    document.body.classList.toggle("ctx-on", on);
  }

  // ---------- 事件绑定 ----------
  function bindEvents() {
    els.zoomIn.addEventListener("click", zoomIn);
    els.zoomOut.addEventListener("click", zoomOut);
    if (els.zoomNative) els.zoomNative.addEventListener("click", zoomNative);
    els.rotateBtn.addEventListener("click", rotate);
    els.flipBtn.addEventListener("click", flip);
    els.resetBtn.addEventListener("click", reset);

    els.roi6.addEventListener("click", function () { toggleRoi(6); });
    els.roi65.addEventListener("click", function () { toggleRoi(6.5); });

    els.saveRoiBtn.addEventListener("click", saveRoi);
    els.exportBtn.addEventListener("click", exportCrop);
    els.mppSetBtn.addEventListener("click", setMpp);
    els.mppInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") setMpp();
    });

    // 标注绘制工具 + 显示切换
    els.annoArrowBtn.addEventListener("click", function () { toggleDrawMode("arrow"); });
    els.annoFreeBtn.addEventListener("click", function () { toggleDrawMode("freehand"); });
    els.annoAllBtn.addEventListener("click", toggleAnnoAll);
    // 面板头部「显示全部标记」切换钮（与 toggleAnnoAll 同一逻辑）
    if (els.annoAllToggle) els.annoAllToggle.addEventListener("click", toggleAnnoAll);
    // 移动端 ROI 滑块分段：点一段切换该尺寸，再点同段退出（toggleRoi 自带切换语义）
    if (els.roiBoxBtn) {
      els.roiBoxBtn.querySelectorAll(".roi-slider-seg").forEach(function (seg) {
        seg.addEventListener("click", function () {
          if (seg.classList.contains("disabled")) return;
          toggleRoi(Number(seg.getAttribute("data-size")));
        });
      });
      syncRoiSlider();
    }

    // 标注画布层绘制事件
    var c = els.annoCanvas;
    c.addEventListener("pointerdown", onAnnoPointerDown);
    c.addEventListener("pointermove", onAnnoPointerMove);
    c.addEventListener("pointerup", onAnnoPointerUp);
    // 工单 D：pointercancel ≠ pointerup——取消恢复，绝不触发完成/保存
    c.addEventListener("pointercancel", onAnnoPointerCancel);
    // §6.2：viewer 容器「捕获阶段」对象路由——只在确认对象编辑时消费事件，
    // 其余交给 OSD 平移（不给透明画布开常驻 pointer-events:auto）
    if (viewer && viewer.container) {
      var vc = viewer.container;
      vc.addEventListener("pointerdown", routeObjectPointerDown, true);
      vc.addEventListener("pointermove", routeObjectPointerMove, true);
      vc.addEventListener("pointerup", routeObjectPointerUp, true);
      vc.addEventListener("pointercancel", routeObjectPointerCancel, true);
      vc.addEventListener("dblclick", routeObjectDblClick, true);
    }
    // §6.2：Space 平移（输入框外）优先于对象编辑
    window.addEventListener("keydown", onSpaceKeyForPan, true);
    window.addEventListener("keyup", onSpaceKeyForPan, true);
    window.addEventListener("resize", function () { resizeAnnoCanvas(); redrawAnnoCanvas(); });

    // 选区面板开关（底部抽屉 + 遮罩）
    els.panelToggle.addEventListener("click", function () {
      var showing = els.panel.style.display !== "none";
      if (showing) {
        els.panel.style.display = "none";
        if (els.panelMask) els.panelMask.style.display = "none";
      } else {
        els.panel.style.display = "flex";
        if (els.panelMask) els.panelMask.style.display = "block";
        loadRoiPanel();
      }
    });
    els.panelClose.addEventListener("click", function () {
      els.panel.style.display = "none";
      if (els.panelMask) els.panelMask.style.display = "none";
    });
    if (els.panelMask) {
      els.panelMask.addEventListener("click", function () {
        els.panel.style.display = "none";
        els.panelMask.style.display = "none";
      });
    }
  }

  // ---------- 启动 ----------
  // viewer 画质档（image-transport-upgrade §3.3/§5.2）：三入口共用模块；
  // 能力缺失/旧服务端时自动隐藏（RGB 走 legacy DZI 语义）
  function initQualityControl() {
    if (!window.HP_ViewerEncoding) return;
    HP_ViewerEncoding.mount({
      host: document.getElementById("quality-control"),
      t: t,
      toast: function (msg, type) { toast(msg, type); },
      onQualityReopen: function () {
        if (state.channelCtrl) state.channelCtrl.reopenForQuality();
      },
    });
    HP_ViewerEncoding.installConflictRecovery({
      viewer: viewer,
      onConflict: function () {
        if (state.channelCtrl) state.channelCtrl.recoverDisplayConflict();
      },
    });
  }

  function init() {
    initViewer();
    initQualityControl();
    bindEvents();
    // 先拉取分享配置（允许的 ROI 尺寸）再加载切片，确保工具栏按钮状态正确
    loadConfig().then(function () {
      loadSlides();
    });
    loadRoiPanel();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // 语言切换：重渲染选区面板（动态文本走 t()，重渲染即换语言）
  document.addEventListener("hp-lang-change", function () {
    try {
      if (currentRois && currentRois.length >= 0) renderRoiPanel(currentRois);
    } catch (e) {}
  });

  // ---------- 最小测试导出（不改 IIFE 结构）----------
  // tests/js/share-write.test.ts 用这些函数断言分享页写请求的 method/URL/body
  // 契约。按实际代码测试：分享页鉴权 = URL 内 share token（capability），
  // 写端点不带主站 X-CSRF-Token 头（share_server 无 CSRF 中间件）。
  window.HP_SHARE = {
    state: state,
    saveRoi: saveRoi,
    saveAnnotation: saveAnnotation,
    commitEdit: commitEdit,
    deleteRoi: deleteRoi,
    // Wave 3（普通图片兼容）：无物理标尺语义入口
    toggleRoi: toggleRoi,
    syncRoiScaleAvailability: syncRoiScaleAvailability,
    slideHasPhysicalScale: slideHasPhysicalScale,
    updateMppSetterVisibility: updateMppSetterVisibility,
  };
})();
