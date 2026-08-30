/* =========================================================================
   Demo 只读运行模式（docs §5.5/§5.6）

   与正式版共享外壳 / Viewer / 工具栏 / HistoPilot 面板 DOM（_app_shell.html）。
   本脚本不加载 app.js：正式脚本默认走登录态与 /api/*。Demo 只接 HP_API
   （/api/demo/*）并驱动一次性 AI run。写操作入口由服务端按 capabilities
   不渲染；安全边界仍是 capability cookie 与后端拒绝。
   ========================================================================= */
(function () {
  "use strict";
  var t = (window.HP_I18N && HP_I18N.t) || function (k) { return k; };
  var $ = function (id) { return document.getElementById(id); };

  function demoApi() {
    // 硬依赖 app-mode.js 的 demoAdapter（demo.html 恒定先加载 app-mode.js）。
    // 旧的内联兜底是 demoAdapter 的过时副本（缺 slideInfoUrl/thumbnailUrl），
    // 已删除：HP_API 缺失属脚本加载顺序故障，直接报错暴露而非静默降级。
    if (window.HP_API && window.HP_API.mode === "demo") return window.HP_API;
    console.error("[demo] window.HP_API 不可用：app-mode.js 未加载或非 demo 模式");
    return null;
  }

  var state = {
    viewer: null,
    slides: [],           // Demo 目录（slide_id/name/display_name/...）
    current: null,        // 当前 slide entry
    info: null,           // 当前切片 info（width/height/mpp_x）
    config: null,
    running: false,
    sessionId: null,      // HistoPilot session（X-AI-Session-ID）
    lastSeq: 0,           // SSE 游标（id: 行）
    terminal: false,      // 已收到终止事件（agent_finished/agent_error/session_ended）
    // ---- AI 视角 / 临时观察状态拆分（fix-plan §7.1）----
    currentSnapshotView: null, // 唯一当前 AI 视角：仅 snapshot_captured / Session 重建更新
    observations: [],     // 归一化观察：{id,snapshot_id,scope,label,note,bbox,magnification,region_ok}
    selectedObservationId: null, // 用户在观察卡中选中的观察（额外高亮）
    snapshotViews: {},    // snapshot_id → 视角索引：观察卡回跳恢复该快照 bbox（§7.4，对齐正式插件）
    obsSeq: 0,            // 观察自增 id 计数
    abortCtrl: null,      // POST /run 的 AbortController
    streamAbort: null,    // 只读重连/恢复流的 AbortController
    sessionAttached: false, // 本页已附着过该 session 的流（避免终态循环重连）
    rebuilding: false,    // event_reset 正在拉 snapshot
    pendingEvents: [],    // 重建期间缓存的原流事件，完成后按序回放
    liveBubble: null,     // 当前可续写的 text_delta 回答区域
  };

  // ---------- 小工具 ----------
  function toast(msg) {
    var box = document.createElement("div");
    box.className = "toast";
    box.textContent = msg;
    $("toast-container").appendChild(box);
    setTimeout(function () { box.remove(); }, 3200);
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function newRequestId() {
    // 每次用户动作生成一次 UUID；同一动作的重试复用同一 id（幂等预占去重）
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    return "req-" + Date.now().toString(36) + "-" +
      Math.random().toString(36).slice(2, 12);
  }

  // ---------- Viewer（共享 HP_ViewerCore；无则回退直连 OSD） ----------
  function initViewer() {
    var core = window.HP_ViewerCore;
    if (core && core.create) {
      state.viewer = core.create($("viewer"));
      core.bindViewTools(state.viewer, {
        zoomIn: $("zoom-in"),
        zoomOut: $("zoom-out"),
        rotate: $("rotate-btn"),
        flip: $("flip-btn"),
        reset: $("reset-btn"),
      });
    } else {
      state.viewer = OpenSeadragon({
        element: $("viewer"),
        showNavigationControl: false,
        minZoomImageRatio: 0.5,
        maxZoomPixelRatio: 10,
        minPixelRatio: 0.4,
        defaultZoomLevel: 0,
        preserveImageSizeOnResize: true,
        prefixUrl: "",
      });
      if ($("zoom-in")) $("zoom-in").addEventListener("click", function () { state.viewer.viewport.zoomBy(1.4); });
      if ($("zoom-out")) $("zoom-out").addEventListener("click", function () { state.viewer.viewport.zoomBy(1 / 1.4); });
      if ($("rotate-btn")) $("rotate-btn").addEventListener("click", function () {
        state.viewer.viewport.setRotation(state.viewer.viewport.getRotation() + 90);
      });
      if ($("flip-btn")) $("flip-btn").addEventListener("click", function () {
        state.viewer.viewport.setFlip(!state.viewer.viewport.getFlip());
      });
      if ($("reset-btn")) $("reset-btn").addEventListener("click", function () {
        state.viewer.viewport.setRotation(0);
        state.viewer.viewport.setFlip(false);
        state.viewer.viewport.goHome();
      });
    }
    state.viewer.addHandler("zoom", updateZoomBadge);
    state.viewer.addHandler("open", updateZoomBadge);
    state.viewer.addHandler("animation", drawObservations);
    state.viewer.addHandler("animation-finish", drawObservations);
    state.viewer.addHandler("rotate", drawObservations);
    state.viewer.addHandler("flip", drawObservations);
    state.viewer.addHandler("resize", function () { resizeObsCanvas(); drawObservations(); });
  }

  function updateZoomBadge() {
    var text = "—";
    if (window.HP_ViewerCore && HP_ViewerCore.zoomText) {
      text = HP_ViewerCore.zoomText(state.viewer, state.info && state.info.mpp_x);
    } else if (state.viewer && state.viewer.viewport) {
      // 兜底（仅 viewer-core 加载失败时可见）：与 viewer-core 无 mpp 时的
      // 口径一致，只显示百分比，不自行换算 µm/px
      try {
        var zoom = state.viewer.viewport.getZoom(true);
        text = zoom > 0 ? (zoom * 100).toFixed(0) + "%" : "—";
      } catch (e) { text = "—"; }
    }
    if ($("zoom-badge")) $("zoom-badge").textContent = text;
    if ($("header-zoom-badge")) $("header-zoom-badge").textContent = text;
  }

  // ---------- 事件字段归一化（fix-plan §5.3：旧事件/旧 Session 兼容） ----------
  function osd() {
    // 浏览器里 OpenSeadragon 由 <script> 先行加载；window 兜底供测试注入。
    if (typeof OpenSeadragon !== "undefined") return OpenSeadragon;
    return (window && window.OpenSeadragon) || null;
  }

  function validBbox(b) {
    if (!b) return null;
    var x = Number(b.x), y = Number(b.y), w = Number(b.w), h = Number(b.h);
    if (!isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h)) return null;
    if (x < 0 || y < 0 || w <= 0 || h <= 0) return null;
    return { x: x, y: y, w: w, h: h };
  }

  function pick(o, k, fb) {
    if (!o) return fb;
    return o[k] != null ? o[k] : fb;
  }

  // snapshotBbox = p.bbox_level0 || p.bboxLevel0（§5.3）
  function normalizeSnapshotView(raw, prev) {
    if (!raw) return prev || null;
    var bbox = validBbox(raw.bbox_level0 || raw.bboxLevel0);
    if (!bbox) return prev || null; // 无有效 bbox：不得覆盖旧视角、不得导航
    return {
      snapshot_id: raw.snapshot_id || (prev && prev.snapshot_id) || null,
      bbox: bbox,
      scope: raw.scope === "whole_slide" ? "whole_slide" : "viewport",
      level: pick(raw, "level", prev && prev.level != null ? prev.level : null),
      magnification: raw.magnification || (prev && prev.magnification) || "",
      out_w: pick(raw, "out_w", prev ? prev.out_w : null),
      out_h: pick(raw, "out_h", prev ? prev.out_h : null),
      captured_at: pick(raw, "captured_at", prev ? prev.captured_at : null),
    };
  }

  // observationBbox = p.bbox_level0 || p.bbox（§5.3；旧 Session 的平铺 x/y/w/h 一并兼容）
  function observationBboxFrom(o) {
    var b = (o && (o.bbox_level0 || o.bbox)) ||
      (o && o.x != null && o.y != null && o.w != null && o.h != null ? o : null);
    return validBbox(b);
  }

  // 旧 observation 缺 scope 的推断（§5.3）：
  //   bbox 与来源快照近似相同 → viewport；明显小于且位于快照内 → region；
  //   无 bbox/零面积/非法 bbox → viewport（只出卡片）；
  //   有 bbox 但关联不到来源快照 → 无法归类（只出卡片，不画框）。
  function approxSameBbox(a, s) {
    var e = 0.01;
    return Math.abs(a.x - s.x) <= Math.max(1, s.w * e) &&
      Math.abs(a.y - s.y) <= Math.max(1, s.h * e) &&
      Math.abs(a.w - s.w) <= Math.max(1, s.w * e) &&
      Math.abs(a.h - s.h) <= Math.max(1, s.h * e);
  }

  function smallerInsideBbox(a, s) {
    return a.w <= s.w * 0.99 && a.h <= s.h * 0.99 &&
      a.x >= s.x && a.y >= s.y &&
      a.x + a.w <= s.x + s.w && a.y + a.h <= s.y + s.h;
  }

  function classifyLegacyObservation(bbox, src) {
    if (!bbox) return "viewport";
    if (!src || !src.bbox) return "none";
    if (approxSameBbox(bbox, src.bbox)) return "viewport";
    if (smallerInsideBbox(bbox, src.bbox)) return "region";
    return "none";
  }

  // 把实时 observation 事件 / Session 里的 observation 记录归一化为同一结构。
  // cur 为当前快照视角（state.currentSnapshotView）。
  function normalizeObservationEntry(raw, cur) {
    var o = raw || {};
    var bbox = observationBboxFrom(o);
    var snapshotId = o.snapshot_id || (cur && cur.snapshot_id) || null;
    var scope = (o.scope === "viewport" || o.scope === "region") ? o.scope : null;
    var regionOk = false;
    if (!scope) {
      var src = (cur && cur.snapshot_id === snapshotId) ? cur : null;
      var cls = classifyLegacyObservation(bbox, src);
      if (cls === "viewport") scope = "viewport";
      else if (cls === "region") { scope = "region"; regionOk = true; }
      // cls === "none"：scope 保持 null，只显示卡片，任何情况下不画框
    } else if (scope === "region") {
      regionOk = !!bbox; // 新契约 region 缺有效 bbox → 只出卡片
    }
    return {
      id: null,
      snapshot_id: snapshotId,
      scope: scope,
      label: o.label || "",
      note: o.note || "",
      magnification: o.magnification || (cur && cur.magnification) || "",
      bbox: bbox,
      region_ok: regionOk,
    };
  }

  // 旧 Session 无 last_snapshot_view：从 transcript 最近一条有效 image_ref.src 推导；
  // 推导不出返回 null（不伪造当前视角框）。
  function deriveLastSnapshotViewFromTranscript(tx) {
    if (!Array.isArray(tx)) return null;
    for (var i = tx.length - 1; i >= 0; i--) {
      var c = tx[i] && tx[i].content;
      if (!Array.isArray(c)) continue;
      for (var j = c.length - 1; j >= 0; j--) {
        var part = c[j];
        if (!part || part.type !== "image_ref") continue;
        var bbox = validBbox(part.src);
        if (bbox) {
          return {
            snapshot_id: null, // image_ref.ref_id 不是 snapshot_id，不伪造关联
            bbox: bbox,
            level: null,
            magnification: part.magnification || "",
            out_w: null, out_h: null, captured_at: null,
          };
        }
      }
    }
    return null;
  }

  // ---------- Viewer 导航与 overlay 绘制（fix-plan §7.2） ----------
  function resizeObsCanvas() {
    var cv = $("obs-canvas");
    if (!cv || !cv.getBoundingClientRect) return;
    var rect = cv.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    cv.width = Math.max(1, Math.round(rect.width * dpr));
    cv.height = Math.max(1, Math.round(rect.height * dpr));
    var ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // 当前可见的局部观察：属于当前快照，或被用户在观察卡中选中（§7.2.2）。
  // snapshot_id 为 null 的旧观察在归一化时已按“当前快照”安全关联（§5.3），
  // 因此 null === null 视为同源；一旦新快照带有真实 id，旧 null 关联自然失效。
  function visibleRegionObservations() {
    var cur = state.currentSnapshotView;
    return state.observations.filter(function (o) {
      if (!o.region_ok || !o.bbox) return false;
      if (o.id === state.selectedObservationId) return true;
      return !!(cur && o.snapshot_id === cur.snapshot_id);
    });
  }

  // 按实际 bbox fitBounds，四周外扩约 20%（与正式插件同一导航口径，§4.1.5）
  function navigateToBbox(bbox) {
    var O = osd();
    var vp = state.viewer && state.viewer.viewport;
    if (!O || !O.Rect || !vp || !vp.fitBounds) return false;
    if (!bbox || !(bbox.w > 0) || !(bbox.h > 0)) return false;
    try {
      var pad = Math.max(bbox.w, bbox.h) * 0.2;
      var rect = new O.Rect(bbox.x - pad, bbox.y - pad,
        bbox.w + pad * 2, bbox.h + pad * 2);
      var vRect = vp.imageToViewportRectangle ? vp.imageToViewportRectangle(rect) : rect;
      vp.fitBounds(vRect);
      return true;
    } catch (e) {
      return false;
    }
  }

  function drawObservations() {
    var cv = $("obs-canvas");
    if (!cv || !state.viewer || !state.viewer.viewport) return;
    var O = osd();
    if (!O || !O.Point) return;
    if (cv.width === 1) resizeObsCanvas();
    var ctx = cv.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, cv.width, cv.height);
    var vp = state.viewer.viewport;
    var cssW = cv.width / (window.devicePixelRatio || 1);
    var labels = []; // 已画标签矩形，用于视口内约束与上下避让（§7.2.6）

    function elemRect(bbox) {
      try {
        var tl = vp.imageToViewerElementCoordinates(new O.Point(bbox.x, bbox.y));
        var br = vp.imageToViewerElementCoordinates(new O.Point(bbox.x + bbox.w, bbox.y + bbox.h));
        var left = Math.min(tl.x, br.x), top = Math.min(tl.y, br.y);
        return { left: left, top: top, w: Math.abs(br.x - tl.x), h: Math.abs(br.y - tl.y) };
      } catch (e) { return null; }
    }

    function drawLabel(text, left, top, bg) {
      try {
        ctx.font = "600 12px -apple-system, PingFang SC, sans-serif";
        var tw = (ctx.measureText ? ctx.measureText(text).width : text.length * 6) + 10;
        var x = Math.min(Math.max(left, 0), Math.max(0, cssW - tw));
        var y = Math.max(0, top - 18);
        for (var k = 0; k < 6; k++) {
          var hit = labels.some(function (l) {
            return x < l.x + l.w && x + tw > l.x && y < l.y + 17 && y + 17 > l.y;
          });
          if (!hit) break;
          y += 18; // 基本上下避让
        }
        labels.push({ x: x, y: y, w: tw });
        ctx.fillStyle = bg;
        ctx.fillRect(x, y, tw, 17);
        ctx.fillStyle = "#fff";
        if (ctx.fillText) ctx.fillText(text, x + 5, y + 12.5);
      } catch (e) { /* 标签绘制失败不影响框 */ }
    }

    // 1) 唯一当前 AI 视角：青色虚线框（全片概览框盖住整张切片，跳过）
    var cur = state.currentSnapshotView;
    if (cur && cur.bbox && cur.scope !== "whole_slide") {
      var r = elemRect(cur.bbox);
      if (r) {
        try {
          ctx.save();
          ctx.strokeStyle = "rgba(41,199,222,0.95)";
          ctx.lineWidth = 2;
          if (ctx.setLineDash) ctx.setLineDash([7, 5]);
          ctx.strokeRect(r.left, r.top, r.w, r.h);
          if (ctx.setLineDash) ctx.setLineDash([]);
          ctx.restore();
          drawLabel(cur.magnification || t("demo.ai.view.current"), r.left, r.top,
            "rgba(0,155,178,0.92)");
        } catch (e) { /* 视口未开/坐标非法：跳过 */ }
      }
    }

    // 2) 局部临时观察：绿色实线；仅当前快照所属或被选中（§7.2.2）
    visibleRegionObservations().forEach(function (o) {
      var r2 = elemRect(o.bbox);
      if (!r2) return;
      var selected = o.id === state.selectedObservationId;
      try {
        ctx.save();
        ctx.strokeStyle = selected ? "rgba(52,199,89,1)" : "rgba(52,199,89,0.95)";
        ctx.lineWidth = selected ? 2.5 : 2;
        if (ctx.setLineDash) ctx.setLineDash([]);
        ctx.strokeRect(r2.left, r2.top, r2.w, r2.h);
        ctx.restore();
        drawLabel(o.label || t("demo.ai.obs.region"), r2.left, r2.top,
          "rgba(52,199,89,0.92)");
      } catch (e) { /* 跳过该框 */ }
    });
  }

  // ---------- 切片加载（仅 Demo 目录，侧栏列表） ----------
  function renderDemoSlideList(activeId) {
    var list = $("demo-slide-list");
    if (!list) return;
    list.innerHTML = "";
    if (!state.slides.length) {
      var empty = document.createElement("div");
      empty.className = "unfiled-empty";
      empty.textContent = t("demo.slides.empty");
      list.appendChild(empty);
      return;
    }
    state.slides.forEach(function (s) {
      var row = document.createElement("div");
      row.className = "slide-row" + (s.slide_id === activeId ? " active" : "");
      row.dataset.slideId = s.slide_id;
      var mid = document.createElement("div");
      mid.className = "slide-mid";
      var name = document.createElement("div");
      name.className = "slide-name";
      name.textContent = s.display_name || s.name || s.slide_id;
      var meta = document.createElement("div");
      meta.className = "slide-meta";
      meta.textContent = s.description || s.name || "";
      mid.appendChild(name);
      mid.appendChild(meta);
      row.appendChild(mid);
      row.addEventListener("click", function () { openSlide(s.slide_id); });
      list.appendChild(row);
    });
  }

  function loadSlides() {
    return demoApi().listSlides()
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        state.slides = (data && data.slides) || [];
        if (!state.slides.length) {
          renderDemoSlideList(null);
          return null;
        }
        var chosen = state.slides.filter(function (s) { return s.is_default; })[0]
          || state.slides[0];
        renderDemoSlideList(chosen.slide_id);
        return openSlide(chosen.slide_id);
      });
  }

  function openSlide(slideId) {
    var entry = state.slides.filter(function (s) { return s.slide_id === slideId; })[0];
    if (!entry) return;
    state.current = entry;
    // 切片切换：清空当前视角与临时观察高亮（§7.2.5）
    clearRunOverlays();
    renderDemoSlideList(slideId);
    if ($("current-slide")) {
      $("current-slide").textContent = entry.display_name || entry.name || slideId;
      $("current-slide").title = entry.description || entry.name || "";
    }
    var api = demoApi();
    return api.slideInfo(slideId)
      .then(function (r) { return r.json(); })
      .then(function (info) {
        state.info = info;
        if (state.viewer) state.viewer.open(api.dziUrl(slideId));
        resizeObsCanvas();
        updateZoomBadge();
      })
      .catch(function (e) { toast(t("demo.slide.open.fail", { e: e })); });
  }

  // ---------- AI 面板状态机（按钮文案 docs §5.6） ----------
  function setRunButtonLabel(btn, label) {
    if (!btn) return;
    btn.title = label;
    if (btn.setAttribute) btn.setAttribute("aria-label", label);
    var svg = btn.querySelector && btn.querySelector("svg");
    btn.textContent = label;
    if (svg) {
      btn.textContent = "";
      btn.appendChild(svg);
    }
  }

  function setQuotaChip(text) {
    var chip = $("demo-quota-chip");
    if (chip && text) chip.textContent = text;
  }

  function setAiButton(kind, extra) {
    var btn = $("ai-run-btn");
    var status = $("ai-status");
    if (status) {
      status.classList.remove("error");
      status.innerHTML = "";
    }
    var label = kind || "—";
    switch (kind) {
      case "available":
        if (btn) btn.disabled = false;
        // 批次 E：无每浏览器累计上限——顺序多次体验不受次数限制
        label = t("demo.ai.run.available");
        setQuotaChip(label);
        break;
      case "running":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.running");
        setQuotaChip(label);
        break;
      case "refreshing":
        // run 结束后的状态刷新过渡态：保持禁用，最终态由 applyConfig() 落定
        if (btn) btn.disabled = true;
        label = t("demo.ai.refreshing");
        setQuotaChip(label);
        break;
      case "demo_run_in_progress":
        // 同 capability 已有在途 run（单 active 并发闸）：按运行中呈现
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.running");
        setQuotaChip(label);
        break;
      case "demo_budget_exhausted":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.demo.exhausted");
        setQuotaChip(label);
        if (status) status.innerHTML = '<a href="/login">' + esc(t("demo.footer.login")) + "</a>";
        break;
      case "demo_ip_request_rate_limited":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.ip.limited");
        setQuotaChip(label);
        break;
      case "platform_ai_budget_exhausted":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.platform.exhausted");
        setQuotaChip(label);
        break;
      case "unavailable":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.unavailable");
        setQuotaChip(label);
        break;
      case "disabled":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.demo.off");
        setQuotaChip(label);
        break;
      default:
        if (btn) btn.disabled = true;
        label = kind || "—";
    }
    setRunButtonLabel(btn, label);
    if (extra && status) {
      status.classList.add("error");
      status.textContent = extra;
    }
    if ($("ai-state")) $("ai-state").textContent = state.running ? t("demo.ai.state.running") : "";
  }

  function applyConfig(cfg) {
    state.config = cfg;
    var hint = $("ai-steps-hint");
    if (hint) {
      // 提示行显示单次步数上限（批次 E：无每浏览器次数额度，不显示剩余次数）
      if (cfg) {
        hint.textContent = t("demo.ai.steps.hint", {
          steps: cfg.task_max_steps });
      } else {
        hint.textContent = "";
      }
    }
    if (!cfg) { setAiButton("unavailable"); return; }
    if (!cfg.demo_enabled) { setAiButton("disabled"); return; }
    if (!cfg.ai_available) {
      setAiButton("unavailable");
      return;
    }
    if (cfg.budget && cfg.budget.demo_exhausted) {
      setAiButton("demo_budget_exhausted"); return;
    }
    if (cfg.budget && cfg.budget.platform_exhausted) {
      setAiButton("platform_ai_budget_exhausted"); return;
    }
    // run_state 来自最近一次 demo_runs 流水：在途（reserved/accepted）→ 运行中；
    // 终态（finished/released/expired）或未跑过 → 可再跑（顺序多次，无次数上限）
    if (cfg.run_state === "reserved" || cfg.run_state === "accepted") {
      setAiButton("running"); return;
    }
    setAiButton("available");
  }

  function loadConfig(opts) {
    var restore = !!(opts && opts.restore);
    return demoApi().config()
      .then(function (r) { return r.json(); })
      .then(function (cfg) {
        applyConfig(cfg);
        // 仅页面首次加载、尚未附着流、且确有在途 accepted run 时才重连轨迹。
        // finishRun 刷新 config 时 restore=false，禁止对终态 session 再开流。
        var sid = cfg.histopilot_session_id || null;
        if (restore && !state.sessionAttached && cfg.run_state === "accepted" && sid) {
          state.sessionId = sid;
          state.sessionAttached = true;
          state.running = true;
          state.terminal = false;
          setAiButton("running");
          reconnectStream(0, true);
        } else if (!state.sessionId && sid) {
          state.sessionId = sid;
        }
      })
      .catch(function () {
        // 页面首次加载失败 → AI 不可用；finishRun 后的状态刷新失败 → 保守
        // 禁用（本轮 run 状态未知，不能误显示可再跑）
        setAiButton(restore ? "unavailable" : "refreshing");
      });
  }

  // ---------- SSE 消费（与正式 UI 同语义：event_reset 全量重建；断线按 last id） ----------
  function appendTrace(cls, text, isHtml) {
    var el = document.createElement("div");
    el.className = cls;
    if (isHtml) el.innerHTML = text; else el.textContent = text;
    $("ai-trace").appendChild(el);
    $("ai-trace").scrollTop = $("ai-trace").scrollHeight;
    return el;
  }

  function closeLiveTextBubble() {
    if (state.liveBubble) state.liveBubble.closed = true;
    state.liveBubble = null;
  }

  function appendLiveText(text) {
    if (!text) return;
    var bubble = state.liveBubble;
    if (bubble && !bubble.closed && bubble.parentNode) {
      bubble._rawText = (bubble._rawText || "") + text;
      bubble.textContent = bubble._rawText;
      $("ai-trace").scrollTop = $("ai-trace").scrollHeight;
      return;
    }
    var el = appendTrace("ai-msg agent", text);
    el._rawText = text;
    el.closed = false;
    state.liveBubble = el;
  }

  function assistantTextFromMessage(msg) {
    var c = msg && msg.content;
    if (typeof c === "string") return c;
    if (Array.isArray(c)) {
      return c.map(function (part) {
        if (!part) return "";
        if (typeof part === "string") return part;
        if (part.type === "text") return part.text || "";
        return "";
      }).join("");
    }
    return msg && (msg.display_text || msg.text) || "";
  }

  // ---------- 观察卡与选中（fix-plan §7.1/§7.2：临时观察统一称「观察/观察区」） ----------
  function appendObservationCard(obs) {
    if (!obs) return;
    var el = document.createElement("div");
    el.className = "ai-obs" +
      (obs.scope === "viewport" ? " obs-viewport" : " obs-region");
    el.dataset.obsId = obs.id;
    var head = document.createElement("b");
    head.textContent = obs.label || t("demo.ai.obs.default");
    el.appendChild(head);
    if (obs.note) {
      var note = document.createElement("div");
      note.className = "ai-obs-note";
      note.textContent = obs.note;
      el.appendChild(note);
    }
    var tag = document.createElement("span");
    tag.className = "ai-obs-scope";
    tag.textContent = t(obs.scope === "viewport"
      ? "demo.ai.obs.scope.viewport"
      : "demo.ai.obs.scope.region");
    el.appendChild(tag);
    if (obs.region_ok && obs.bbox) {
      // 仅可画框的局部观察支持点选高亮；整视野小结不承载 Viewer 交互
      el.style.cursor = "pointer";
      el.addEventListener("click", function () { toggleObservationSelect(obs.id); });
    }
    $("ai-trace").appendChild(el);
    $("ai-trace").scrollTop = $("ai-trace").scrollHeight;
  }

  // 登记快照视角索引（§7.4，对齐正式插件 S.snapshotViews）：仅带 snapshot_id
  // 与有效 bbox 的视角进入索引；同 id 重复登记覆盖为最新视角。
  function registerSnapshotView(view) {
    if (view && view.snapshot_id && view.bbox) {
      state.snapshotViews[view.snapshot_id] = view;
    }
  }

  // 观察卡点击：切换选中，并对「来自其它快照」的局部观察回跳来源快照（§7.4）。
  // 有索引视角时先恢复 currentSnapshotView 为来源快照（对齐正式插件）再按该
  // 视角 bbox 导航——画面、青色当前视角框、倍率标签与可见观察集合四者一致；
  // 取消选中不回退（停留在来源快照，状态与画面保持一致）。索引缺失（旧会话
  // 重建数据不全）时降级：仅用观察自身 bbox 导航、不恢复视角（无法重构完整
  // 视角状态）。属于当前快照的选中维持原行为（不导航，仅高亮）。
  function toggleObservationSelect(id) {
    var target = null;
    state.observations.forEach(function (o) { if (o && o.id === id) target = o; });
    if (!target) return;
    var nextSel = state.selectedObservationId === id ? null : id;
    if (nextSel && target.region_ok && target.bbox &&
        (!state.currentSnapshotView ||
          state.currentSnapshotView.snapshot_id !== target.snapshot_id)) {
      var view = target.snapshot_id ? state.snapshotViews[target.snapshot_id] : null;
      if (view) {
        state.currentSnapshotView = view;
        navigateToBbox(view.bbox);
      } else {
        navigateToBbox(target.bbox);
      }
    }
    state.selectedObservationId = nextSel;
    updateObservationCardStates();
    drawObservations();
  }

  function updateObservationCardStates() {
    var trace = $("ai-trace");
    if (!trace || !trace.children) return;
    for (var i = 0; i < trace.children.length; i++) {
      var el = trace.children[i];
      var oid = el && el.dataset && el.dataset.obsId;
      if (!oid) continue;
      var on = oid === state.selectedObservationId;
      if (el.classList && el.classList.add && el.classList.remove) {
        if (on) el.classList.add("selected");
        else el.classList.remove("selected");
      }
    }
  }

  // 切片切换 / 新 run 开始 / Session 明确重置：清空当前视角、临时高亮与
  // 快照视角索引（§7.2.5/§7.4）
  function clearRunOverlays() {
    state.currentSnapshotView = null;
    state.observations = [];
    state.selectedObservationId = null;
    state.snapshotViews = {};
    state.obsSeq = 0;
    drawObservations();
  }

  function appendGotoRow(p) {
    // goto 只是移动意图：只更新「AI 正在移动」轨迹状态，不导航、不画框（§4.1.6/7.3）
    var bits = [];
    if (p.x != null && p.y != null) bits.push("(" + fmtCoord(p.x) + ", " + fmtCoord(p.y) + ")");
    if (p.magnification) bits.push(String(p.magnification));
    else if (p.level != null) bits.push("level " + p.level);
    if (p.reason) bits.push(String(p.reason));
    appendTrace("ai-row", t("demo.ai.goto") + (bits.length ? " · " + bits.join(" · ") : ""));
  }

  function fmtCoord(n) {
    n = Number(n);
    if (!isFinite(n)) return "?";
    return Math.abs(n - Math.round(n)) < 0.05 ? String(Math.round(n)) : n.toFixed(1);
  }

  function handleEvent(type, payload) {
    var p = payload || {};
    if (type === "observation") {
      closeLiveTextBubble();
      var obs = normalizeObservationEntry(p, state.currentSnapshotView);
      obs.id = "obs-" + (++state.obsSeq);
      state.observations.push(obs);
      appendObservationCard(obs);
      drawObservations();
      return;
    }
    if (type === "event_reset") {
      // 游标老化出事件窗口：用 session snapshot 全量重建。重建期间暂停应用
      // 原流事件（缓存），snapshot 完成后再按序回放，避免清空时丢掉新事件。
      if (state.rebuilding) return;
      state.rebuilding = true;
      state.pendingEvents = [];
      appendTrace("ai-row", t("demo.ai.reset"));
      rebuildFromSnapshot();
      return;
    }
    if (type === "run_deduplicated") {
      appendTrace("ai-row", t("demo.ai.dedup"));
      return;
    }
    if (type === "slide_opened" || type === "agent_started") {
      appendTrace("ai-row", t("demo.ai.started"));
      return;
    }
    if (type === "goto") {
      // 兼容期旧独立 goto 事件：与 tool_started{tool:"goto"} 归一化为同一轨迹
      // 状态；不读取 p.zoom、不导航 Viewer（§4.1.7）。确认部署基线均不发送
      // 独立 goto 后再删除本分支。
      closeLiveTextBubble();
      appendGotoRow(p);
      return;
    }
    if (type === "tool_started") {
      closeLiveTextBubble();
      if (p.tool === "goto") {
        appendGotoRow(p);
        return;
      }
      if (p.tool === "snapshot") appendTrace("ai-row", t("demo.ai.snapshot"));
      else if (p.tool && p.tool !== "finish" && p.tool !== "mark_observation" &&
               p.tool !== "complete_snapshot_review" && p.tool !== "create_annotation") {
        appendTrace("ai-row", p.tool);
      }
      return;
    }
    if (type === "snapshot_captured") {
      // 唯一权威取景来源：用实际 bbox 更新当前视角虚线框。不自动平移人眼 Viewer
      // （Agent 有自己的看图视角）。无效 bbox 不得覆盖旧视角。
      closeLiveTextBubble();
      var prevView = state.currentSnapshotView;
      state.currentSnapshotView = normalizeSnapshotView(p, prevView);
      // 登记视角索引：观察卡回跳据此恢复该快照的 bbox（§7.4）
      registerSnapshotView(state.currentSnapshotView);
      drawObservations();
      var mag = state.currentSnapshotView && state.currentSnapshotView.magnification;
      appendTrace("ai-row", t("demo.ai.snapshot") + (mag ? " · " + mag : ""));
      return;
    }
    if (type === "annotation_created") {
      // 正式标注：写入 PathTogether 标注库、待人工审核。不复用临时观察
      // overlay 冒充成功状态；Demo 只读，仅以状态行呈现（§7.3）。
      closeLiveTextBubble();
      appendTrace("ai-row", t("demo.ai.annotation.created"));
      return;
    }
    if (type === "security_profile_applied") {
      appendTrace("ai-row", t("demo.ai.security.applied"));
      return;
    }
    if (type === "text_delta") {
      appendLiveText(p.text || p.delta || "");
      return;
    }
    if (type === "agent_message" || type === "message") {
      closeLiveTextBubble();
      var txt = p.text || p.content || p.message || "";
      if (txt) appendTrace("ai-msg agent", txt);
      return;
    }
    if (type === "agent_paused") {
      // 匿名 Demo 没有 continue 入口：暂停即本轮结束，禁止随后重连回放。
      closeLiveTextBubble();
      state.terminal = true;
      appendTrace("ai-row", p.summary
        ? (t("demo.ai.paused") + " · " + p.summary)
        : t("demo.ai.paused"));
      appendTrace("ai-row", t("demo.ai.ended"));
      finishRun();
      return;
    }
    if (type === "agent_finished") {
      var hadLive = state.liveBubble && String(state.liveBubble._rawText || "").trim();
      closeLiveTextBubble();
      state.terminal = true;
      if (p.summary && !hadLive) {
        appendTrace("ai-msg agent", p.summary);
      }
      appendTrace("ai-row", t("demo.ai.finished"));
      finishRun();
      return;
    }
    if (type === "agent_error") {
      closeLiveTextBubble();
      state.terminal = true;
      appendTrace("ai-row error", t("demo.ai.error", { e: p.error || p.message || "" }));
      finishRun();
      return;
    }
    if (type === "session_ended") {
      closeLiveTextBubble();
      state.terminal = true;
      finishRun();
      return;
    }
  }

  function pumpSse(reader) {
    var decoder = new TextDecoder();
    var buf = "";
    function parseFrame(frame) {
      var id = null, event = "message", dataLines = [];
      frame.split("\n").forEach(function (line) {
        if (line.indexOf("id:") === 0) id = line.slice(3).trim();
        else if (line.indexOf("event:") === 0) event = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) dataLines.push(line.slice(5).trim());
      });
      if (id !== null) state.lastSeq = parseInt(id, 10) || state.lastSeq;
      if (!dataLines.length) return;
      var payload;
      try { payload = JSON.parse(dataLines.join("\n")); }
      catch (e) { payload = { text: dataLines.join("\n") }; }
      if (state.rebuilding) {
        if (event === "event_reset") return;
        state.pendingEvents.push({
          type: event, payload: payload,
          seq: id !== null ? parseInt(id, 10) : null,
        });
        return;
      }
      handleEvent(event, payload);
    }
    function step() {
      return reader.read().then(function (r) {
        if (r.done) return;
        buf += decoder.decode(r.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          var frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          parseFrame(frame);
        }
        return step();
      });
    }
    return step();
  }

  function closeActiveStream() {
    if (state.streamAbort) {
      try { state.streamAbort.abort(); } catch (e) { /* ignore */ }
      state.streamAbort = null;
    }
    if (state.abortCtrl) {
      try { state.abortCtrl.abort(); } catch (e) { /* ignore */ }
      state.abortCtrl = null;
    }
  }

  function onStreamClosed() {
    // 正常收尾（terminal）不动作；否则按 last id 增量重连（不扣额度，docs §5.5）
    if (state.terminal || !state.running || !state.sessionId) return;
    appendTrace("ai-row", t("demo.ai.reconnecting"));
    setTimeout(function () { reconnectStream(state.lastSeq, false); }, 1500);
  }

  function reconnectStream(afterSeq, silent) {
    if (!state.sessionId || state.terminal) return;
    if (state.streamAbort) {
      try { state.streamAbort.abort(); } catch (e) { /* ignore */ }
    }
    state.streamAbort = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var url = demoApi().aiStreamUrl(state.sessionId, afterSeq);
    fetch(url, {
      credentials: "same-origin",
      signal: state.streamAbort ? state.streamAbort.signal : undefined,
    })
      .then(function (resp) {
        if (resp.status === 410 || resp.status === 403) {
          // 重连窗口已过 / 会话归属不符：终止本地运行态，不报错刷屏
          state.terminal = true;
          finishRun();
          return null;
        }
        if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
        return pumpSse(resp.body.getReader()).then(onStreamClosed, onStreamClosed);
      })
      .catch(function (e) {
        if (e && e.name === "AbortError") return;
        if (state.terminal || !state.sessionId || !state.running) return;
        var delay = silent ? 1500 : 3000;
        setTimeout(function () { reconnectStream(state.lastSeq, false); }, delay);
      });
  }

  function rebuildFromSnapshot() {
    if (!state.sessionId) {
      state.rebuilding = false;
      state.pendingEvents = [];
      appendTrace("ai-row error", t("demo.ai.reset.fail"));
      return;
    }
    demoApi().aiSession(state.sessionId).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    }).then(function (data) {
      var s = (data && data.session) || {};
      var tx = (data && data.transcript) || [];
      // 当前视角：优先 Session 的 last_snapshot_view；旧 Session 从 transcript
      // 最近有效 image_ref.src 推导；仍推不出则不显示当前视角框，不伪造（§5.3）
      var view = normalizeSnapshotView(s.last_snapshot_view, null) ||
        deriveLastSnapshotViewFromTranscript(tx);
      state.currentSnapshotView = view;
      state.selectedObservationId = null;
      state.obsSeq = 0;
      // 快照视角索引随重建归零后补种（§7.4）：last_snapshot_view 是权威来源；
      // 旧会话其余快照的视角按契约从 viewport 观察携带的快照 bbox 补种
      // （§5.2「供内部定位和卡片回跳使用」）。region 观察的 bbox 是子区域、
      // 不代表快照视角，不补种——其回跳走观察自身 bbox 的降级路径。
      state.snapshotViews = {};
      registerSnapshotView(view);
      // observation 按 snapshot_id/scope/bbox_level0/倍率完整重建；旧记录走
      // 同一归一化函数（含 scope 推断与安全降级）
      state.observations = ((s && s.observations) || []).map(function (o) {
        var n = normalizeObservationEntry(o, view);
        n.id = "obs-" + (++state.obsSeq);
        return n;
      });
      state.observations.forEach(function (o) {
        if (o && o.scope === "viewport" && o.snapshot_id &&
            !state.snapshotViews[o.snapshot_id]) {
          registerSnapshotView({ snapshot_id: o.snapshot_id, bbox: o.bbox, level: null,
            magnification: o.magnification || "", out_w: null, out_h: null,
            captured_at: null });
        }
      });
      drawObservations();
      $("ai-trace").innerHTML = "";
      closeLiveTextBubble();
      tx.forEach(function (msg) {
        if (!msg) return;
        if (msg.role === "user") {
          var ut = msg.display_text || assistantTextFromMessage(msg);
          if (typeof ut !== "string") ut = "";
          if (ut) appendTrace("ai-msg user", ut);
        } else if (msg.role === "assistant") {
          var at = assistantTextFromMessage(msg);
          if (at && String(at).trim()) appendTrace("ai-msg agent", at);
        }
      });
      // 观察卡随重建恢复（历史观察保留在轨迹中，但不全部铺到 Viewer 上，§7.1）
      state.observations.forEach(appendObservationCard);
      var snapSeq = (s && s.last_event_seq != null) ? Number(s.last_event_seq) || 0 : 0;
      state.lastSeq = Math.max(state.lastSeq || 0, snapSeq);
      var queued = state.pendingEvents;
      state.pendingEvents = [];
      state.rebuilding = false;
      queued.forEach(function (ev) {
        if (ev.seq != null && Number(ev.seq) <= snapSeq) return;
        handleEvent(ev.type, ev.payload);
      });
    }).catch(function () {
      var queued = state.pendingEvents;
      state.pendingEvents = [];
      state.rebuilding = false;
      appendTrace("ai-row error", t("demo.ai.reset.fail"));
      queued.forEach(function (ev) { handleEvent(ev.type, ev.payload); });
    });
  }

  // ---------- 起跑 ----------
  function finishRun() {
    state.running = false;
    closeActiveStream();
    // 过渡态保持禁用：run 终态（finished）由 loadConfig → applyConfig 落定为
    // 「可再次体验」（批次 E：顺序多次 run，无每浏览器次数上限）
    setAiButton("refreshing");
    loadConfig({ restore: false });
  }

  function startRun() {
    if (state.running) return;
    var slideId = state.current && state.current.slide_id;
    if (!slideId) { toast(t("demo.ai.need.slide")); return; }
    closeActiveStream();
    var taskEl = $("ai-task");
    var task = ((taskEl && taskEl.value) || "").trim();
    state.running = true;
    state.terminal = false;
    state.sessionAttached = false;
    state.lastSeq = 0;
    // 新 run 开始：清空旧当前视角与临时观察高亮（§7.2.5）
    clearRunOverlays();
    closeLiveTextBubble();
    $("ai-trace").innerHTML = "";
    setAiButton("running");
    appendTrace("ai-msg user", task || t("demo.ai.default.task"));

    var rid = newRequestId();
    state.abortCtrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    demoApi().aiRun({ slide_id: slideId, task: task, request_id: rid }, {
      signal: state.abortCtrl ? state.abortCtrl.signal : undefined,
    }).then(function (resp) {
      var sid = resp.headers.get("X-AI-Session-ID");
      if (sid) {
        state.sessionId = sid;
        state.sessionAttached = true;
      }
      if (resp.status === 429 || resp.status === 409 || resp.status === 403 ||
          resp.status === 400 || resp.status === 503) {
        return resp.json().then(function (body) {
          var code = body && body.code;
          if (code === "demo_budget_exhausted") setAiButton("demo_budget_exhausted");
          else if (code === "demo_ip_request_rate_limited") setAiButton("demo_ip_request_rate_limited");
          else if (code === "platform_ai_budget_exhausted") setAiButton("platform_ai_budget_exhausted");
          else if (code === "demo_run_in_progress") setAiButton("demo_run_in_progress");
          else if (code === "demo_disabled") setAiButton("disabled");
          else if (code === "histopilot_unreachable" ||
                   code === "histopilot_legacy_adapter" ||
                   code === "platform_credentials_missing" ||
                   code === "pg_backend_required") setAiButton("unavailable");
          else if (code === "demo_run_request_final") setAiButton("available");
          else setAiButton("refreshing");
          state.running = false;
          appendTrace("ai-row error", (body && body.error) || ("HTTP " + resp.status));
        });
      }
      if (!resp.ok || !resp.body) {
        return resp.text().then(function (tx) {
          throw new Error(tx || ("HTTP " + resp.status));
        });
      }
      return pumpSse(resp.body.getReader()).then(onStreamClosed, onStreamClosed);
    }).catch(function (e) {
      if (e && e.name === "AbortError") return;
      state.running = false;
      setAiButton("available");
      appendTrace("ai-row error", t("demo.ai.error", { e: (e && e.message) || e }));
    });
  }

  function setAiPanelOpen(open) {
    var panel = $("ai-panel");
    if (!panel) return;
    panel.style.display = open ? "flex" : "none";
  }

  function bindShellChrome() {
    var menu = $("menu-btn");
    var sidebar = $("sidebar");
    var mask = $("sidebar-mask");
    if (menu && sidebar) {
      menu.addEventListener("click", function () {
        var open = !sidebar.classList.contains("open");
        sidebar.classList.toggle("open", open);
        if (mask) mask.classList.toggle("open", open);
      });
    }
    if (mask && sidebar) {
      mask.addEventListener("click", function () {
        sidebar.classList.remove("open");
        mask.classList.remove("open");
      });
    }
    function toggleAiPanel() {
      var panel = $("ai-panel");
      if (!panel) return;
      var hidden = panel.style.display === "none";
      setAiPanelOpen(hidden);
    }
    if ($("ai-btn")) $("ai-btn").addEventListener("click", toggleAiPanel);
    if ($("tbb-more-ai")) $("tbb-more-ai").addEventListener("click", toggleAiPanel);
    if ($("ai-panel-close")) {
      $("ai-panel-close").addEventListener("click", function () { setAiPanelOpen(false); });
    }
    if ($("ai-run-btn")) $("ai-run-btn").addEventListener("click", startRun);
    var moreBtn = $("tbb-more-btn");
    var more = $("tbb-more");
    var moreMask = $("tbb-more-mask");
    if (moreBtn && more) {
      moreBtn.addEventListener("click", function () {
        more.classList.toggle("open");
        if (moreMask) moreMask.classList.toggle("open", more.classList.contains("open"));
      });
    }
    if (moreMask && more) {
      moreMask.addEventListener("click", function () {
        more.classList.remove("open");
        moreMask.classList.remove("open");
      });
    }
  }

  // ---------- 启动 ----------
  document.addEventListener("DOMContentLoaded", function () {
    if (typeof OpenSeadragon === "undefined") return;
    initViewer();
    resizeObsCanvas();
    bindShellChrome();
    setAiPanelOpen(true);
    loadConfig({ restore: true });
    loadSlides();
  });

  window.HP_DEMO = {
    loadConfig: loadConfig,
    finishRun: finishRun,
    startRun: startRun,
    closeActiveStream: closeActiveStream,
    handleEvent: handleEvent,
    openSlide: openSlide,
    clearRunOverlays: clearRunOverlays,
    toggleObservationSelect: toggleObservationSelect,
    visibleRegionObservations: visibleRegionObservations,
    drawObservations: drawObservations,
    state: state,
  };
})();
