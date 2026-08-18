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
    if (window.HP_API && window.HP_API.mode === "demo") return window.HP_API;
    return {
      mode: "demo",
      config: function () {
        return fetch("/api/demo/config", { credentials: "same-origin" });
      },
      listSlides: function () {
        return fetch("/api/demo/slides", { credentials: "same-origin" });
      },
      slideInfo: function (id) {
        return fetch("/api/demo/slides/" + encodeURIComponent(id) + "/info",
                     { credentials: "same-origin" });
      },
      dziUrl: function (id) {
        return "/api/demo/slides/" + encodeURIComponent(id) + ".dzi";
      },
      aiRun: function (body, opts) {
        return fetch("/api/demo/ai/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify(body || {}),
          signal: opts && opts.signal,
        });
      },
      aiSession: function (id) {
        return fetch("/api/demo/ai/session/" + encodeURIComponent(id),
                     { credentials: "same-origin" });
      },
      aiStreamUrl: function (id, afterSeq) {
        return "/api/demo/ai/session/" + encodeURIComponent(id) +
          "/stream?after_seq=" + (afterSeq == null ? 0 : afterSeq);
      },
    };
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
    observations: [],     // {x,y,w,h,label,note}（level-0 坐标，临时 overlay）
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
      try {
        var zoom = state.viewer.viewport.getZoom(true);
        if (state.info && state.info.mpp_x > 0 && zoom > 0) {
          var um = state.info.mpp_x / zoom;
          text = (um >= 1000 ? (um / 1000).toFixed(2) + " mm/px" : um.toFixed(2) + " µm/px");
        } else {
          text = (zoom * 100).toFixed(0) + "%";
        }
      } catch (e) { text = "—"; }
    }
    if ($("zoom-badge")) $("zoom-badge").textContent = text;
    if ($("header-zoom-badge")) $("header-zoom-badge").textContent = text;
  }

  // ---------- 临时 overlay（observation，只画不写） ----------
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

  function drawObservations() {
    var cv = $("obs-canvas");
    if (!cv || !state.viewer || !state.viewer.viewport) return;
    if (cv.width === 1) resizeObsCanvas();
    var ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, cv.width, cv.height);
    var vp = state.viewer.viewport;
    state.observations.forEach(function (o, i) {
      if (!(o.w > 0 && o.h > 0)) return;
      try {
        var tl = vp.imageToViewerElementCoordinates(new OpenSeadragon.Point(o.x, o.y));
        var br = vp.imageToViewerElementCoordinates(new OpenSeadragon.Point(o.x + o.w, o.y + o.h));
        var left = Math.min(tl.x, br.x), top = Math.min(tl.y, br.y);
        var w = Math.abs(br.x - tl.x), h = Math.abs(br.y - tl.y);
        ctx.strokeStyle = "rgba(52,199,89,0.95)";
        ctx.lineWidth = 2;
        ctx.strokeRect(left, top, w, h);
        var label = o.label || ("#" + (i + 1));
        ctx.font = "600 12px -apple-system, PingFang SC, sans-serif";
        var tw = ctx.measureText(label).width + 10;
        ctx.fillStyle = "rgba(52,199,89,0.92)";
        ctx.fillRect(left, Math.max(0, top - 18), tw, 17);
        ctx.fillStyle = "#fff";
        ctx.fillText(label, left + 5, Math.max(0, top - 18) + 12.5);
      } catch (e) { /* 视口未开/坐标非法：跳过该框 */ }
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
        var remain = state.config && state.config.per_browser_remaining;
        label = (remain != null)
          ? t("demo.ai.run.available.n", { n: remain })
          : t("demo.ai.run.available");
        setQuotaChip(label);
        break;
      case "running":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.running");
        setQuotaChip(label);
        break;
      case "used":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.used");
        setQuotaChip(label);
        var usedN = (state.config && state.config.per_browser_used) || 1;
        var limN = (state.config && state.config.per_browser_limit) || 1;
        if (status) {
          status.innerHTML = esc(t("demo.ai.login.hint", { used: usedN, limit: limN })) +
            ' <a href="/login">' + esc(t("demo.footer.login")) + "</a>";
        }
        break;
      case "demo_budget_exhausted":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.demo.exhausted");
        setQuotaChip(label);
        if (status) status.innerHTML = '<a href="/login">' + esc(t("demo.footer.login")) + "</a>";
        break;
      case "demo_ip_rate_limited":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.ip.limited");
        setQuotaChip(label);
        if (status) status.innerHTML = '<a href="/login">' + esc(t("demo.footer.login")) + "</a>";
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
      hint.textContent = cfg && cfg.budget
        ? t("demo.ai.steps.hint", {
            steps: cfg.task_max_steps,
            demo: cfg.budget.demo_used, demoLimit: cfg.budget.demo_limit })
        : "";
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
    var limit = cfg.per_browser_limit != null ? Number(cfg.per_browser_limit) : 1;
    var used = cfg.per_browser_used != null ? Number(cfg.per_browser_used) : 0;
    var remaining = cfg.per_browser_remaining != null
      ? Number(cfg.per_browser_remaining) : Math.max(0, limit - used);
    if (cfg.run_state === "consumed" && remaining <= 0) {
      setAiButton("used"); return;
    }
    setAiButton("available");
  }

  function loadConfig(opts) {
    var restore = !!(opts && opts.restore);
    return demoApi().config()
      .then(function (r) { return r.json(); })
      .then(function (cfg) {
        applyConfig(cfg);
        // 仅页面首次加载、尚未附着流、且确实需要恢复轨迹时才重连。
        // finishRun 刷新配额时 restore=false，禁止对终态 session 再开流。
        var sid = cfg.histopilot_session_id || null;
        if (restore && !state.sessionAttached && cfg.run_state === "consumed" && sid) {
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
      .catch(function () { setAiButton("unavailable"); });
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

  function handleEvent(type, payload) {
    var p = payload || {};
    if (type === "observation") {
      closeLiveTextBubble();
      state.observations.push({
        x: p.bbox && p.bbox.x, y: p.bbox && p.bbox.y,
        w: p.bbox && p.bbox.w, h: p.bbox && p.bbox.h,
        label: p.label, note: p.note,
      });
      drawObservations();
      appendTrace("ai-obs", "<b>" + esc(p.label || "观察") + "</b>" +
        (p.note ? "<br/>" + esc(p.note) : ""), true);
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
      closeLiveTextBubble();
      try {
        var vp = state.viewer.viewport;
        if (p.x != null && p.y != null) {
          vp.panTo(vp.imageToViewportCoordinates(
            new OpenSeadragon.Point(p.x, p.y)), true);
          if (p.zoom != null) vp.zoomTo(p.zoom, null, true);
        }
      } catch (e) { /* 忽略导航事件坐标异常 */ }
      appendTrace("ai-row", t("demo.ai.goto"));
      return;
    }
    if (type === "tool_started") {
      closeLiveTextBubble();
      if (p.tool === "snapshot") appendTrace("ai-row", t("demo.ai.snapshot"));
      else if (p.tool && p.tool !== "goto" && p.tool !== "finish" &&
               p.tool !== "mark_observation" && p.tool !== "complete_snapshot_review") {
        appendTrace("ai-row", p.tool);
      }
      return;
    }
    if (type === "snapshot_captured") {
      closeLiveTextBubble();
      appendTrace("ai-row", t("demo.ai.snapshot"));
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
      var s = data && data.session;
      var tx = (data && data.transcript) || [];
      var obs = (s && s.observations) || [];
      state.observations = obs.map(function (o) {
        var b = (o && o.bbox) || o || {};
        return {
          x: b.x, y: b.y, w: b.w, h: b.h,
          label: o.label, note: o.note,
        };
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
    setAiButton("used");
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
    state.observations = [];
    closeLiveTextBubble();
    drawObservations();
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
          else if (code === "demo_ip_rate_limited") setAiButton("demo_ip_rate_limited");
          else if (code === "platform_ai_budget_exhausted") setAiButton("platform_ai_budget_exhausted");
          else if (code === "demo_run_already_used") setAiButton("used");
          else if (code === "demo_disabled") setAiButton("disabled");
          else if (code === "histopilot_unreachable" ||
                   code === "histopilot_legacy_adapter" ||
                   code === "platform_credentials_missing" ||
                   code === "pg_backend_required") setAiButton("unavailable");
          else setAiButton("used");
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
    state: state,
  };
})();
