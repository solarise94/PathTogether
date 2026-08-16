/* =========================================================================
   HistoPilot UI bundle —— 入口：状态对象 / run 控制 / 事件分派 / init

   - 持有全部 AI 运行状态（activeAiSession 三态状态机、aiSlideEpoch 切片世代隔离、
     aiAbortCtrl/aiStreamCtrl 收尾顺序）。
   - handleAiEvent：按 SSE 事件类型渲染轨迹 + 通过 HostBridge 驱动平台叠加层
     （viewer.highlight 替代直接写 aiOverlay/redrawAnnoCanvas）。
   - init：查询 #ai-panel 等 DOM、绑定事件、加载配置、注册 HostBridge 处理器。
   对 window.HistoPilot 缺失的平台侧做静默降级（HISTOPILOT_UI_ENABLED=0 时人工读片正常）。
   ========================================================================= */
(function () {
  "use strict";
  var HP = window.HistoPilot;
  var S = HP.s;
  var t = HP.t, esc = HP.esc, toast = HP.toast;
  if (HP._sessionsBind) HP._sessionsBind(); // sessions.js 的 renderer 别名绑定

  // ---------- 共享状态（原 app.js 内 IIFE 私有变量，整体迁入，未拆散） ----------
  S.slide = null;             // 当前切片 {name,width,height,mppX,mppY}（由 slide.opened 写入）
  S.aiPanelOpen = false;
  S.aiConfig = null;
  S.aiRunning = false;
  S.aiAbortCtrl = null;
  S.aiSessionId = null;
  S.aiStreamCtrl = null;
  S.aiPaused = false;
  S.aiSlideEpoch = 0;
  S.mainAiCtx = { container: null, bubbleEl: null, thinkingEl: null, isFork: false, lastSeq: 0 };
  S.activeAiSession = null;   // {id, kind:"main"|"branch", annotation_id?} | null
  S.aiSessionListCache = [];
  S.aiSessionRefreshGen = 0;
  S.overlay = [];             // 插件侧镜像的叠加层 boxes（同步给平台 via viewer.highlight）
  S.els = {};

  // renderer 句柄（renderer.js 先加载，此处别名以保持事件分派代码与原版一致）
  var appendStatusRow = HP.appendStatusRow, appendMsgTs = HP.appendMsgTs,
      appendChatBubble = HP.appendChatBubble, appendReconnectStatus = HP.appendReconnectStatus,
      appendToolCall = HP.appendToolCall, appendSnapshotCard = HP.appendSnapshotCard,
      appendObservationCard = HP.appendObservationCard, appendReviewCard = HP.appendReviewCard,
      appendAnnotationCard = HP.appendAnnotationCard;

  function $(id) { return document.getElementById(id); }

  // 输入框自适应高度（pill 内 1~5 行）
  function autoGrowAiTask() {
    var el = S.els.aiTask;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 96) + "px";
  }

  function openAiPanel() {
    if (!S.slide) { toast(t("roi.need.slide"), "info"); return; }
    S.aiPanelOpen = true;
    S.els.aiPanel.style.display = "flex";
    HP.bridge.emit("panel.stateChanged", { open: true });
    // 关闭再开：轨迹被清空（空占位/只剩 status 行）且无进行中 run → 从当前
    // 会话恢复 transcript，避免「正在开启分支会话…」卡死后主对话永久消失。
    restoreAiTraceOnReopen();
  }

  function restoreAiTraceOnReopen() {
    var trace = S.els.aiTrace;
    if (!trace || S.aiRunning || !S.aiSessionId || !S.slide) return;
    if (trace.querySelector(".ai-chat-bubble, .ai-attach")) return;
    HP.loadAndRenderTranscript(S.aiSessionId, trace, {
      emphasis: "main", slideName: S.slide.name, epoch: S.aiSlideEpoch,
    });
  }
  function closeAiPanel() {
    S.aiPanelOpen = false;
    S.els.aiPanel.style.display = "none";
    // 关面板不停止进行中的 run（后台继续，结果仍落标注）
    HP.bridge.emit("panel.stateChanged", { open: false });
  }

  // context-aware 提交入口：发送按钮与回车共用。
  function submitAiComposer() {
    if (S.aiRunning) { toast(t("ai.busy"), "info"); return; }
    if (S.activeAiSession && S.activeAiSession.kind === "branch" && S.activeAiSession.annotation_id) {
      var q = (S.els.aiTask.value || "").trim();
      if (!q) { toast(t("ai.branch.need.q"), "info"); return; }
      var started = HP.startBranchRun(S.activeAiSession.annotation_id, q);
      if (started) { S.els.aiTask.value = ""; autoGrowAiTask(); }
    } else {
      startAiRun();
    }
  }

  // 开始 AI run（POST /api/ai/run?fresh=1，SSE）
  function startAiRun() {
    var els = S.els;
    if (!S.slide) { toast(t("roi.need.slide"), "info"); return; }
    if (S.aiRunning) { toast(t("ai.busy"), "info"); return; }
    if (!S.aiConfig || !S.aiConfig.base_url || !S.aiConfig.api_key_set) {
      toast(t("ai.need.config"), "error");
      els.aiConfigWrap.style.display = "block"; els.aiConfigCollapsed.style.display = "none";
      return;
    }
    var DEFAULT_AI_TASK = t("ai.default.task");
    var task = (els.aiTask.value || "").trim() || DEFAULT_AI_TASK;
    if (!(els.aiTask.value || "").trim()) els.aiTask.value = task;

    HP.resetAiTrace();
    appendMsgTs(els.aiTrace, new Date());
    S.aiRunning = true; S.aiPaused = false; setAiRunningUi(true);
    S.aiSessionId = null; S.mainAiCtx.lastSeq = 0;
    S.activeAiSession = { id: null, kind: "main", annotation_id: null };
    applyActiveSessionUi();
    appendChatBubble(els.aiTrace, "user", task);
    var slideName = S.slide.name;
    var epoch = S.aiSlideEpoch;
    S.aiAbortCtrl = new AbortController();
    var runCtrl = S.aiAbortCtrl;

    fetch("/api/ai/run?fresh=1", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slide: slideName, task: task, fresh: true }),
      signal: runCtrl.signal, credentials: "same-origin",
    }).then(function (resp) {
      if (!HP.isCurrentAiSlide(slideName, epoch)) return;
      var sid = resp.headers.get("X-AI-Session-ID");
      if (sid) {
        S.aiSessionId = sid;
        if (S.activeAiSession && S.activeAiSession.kind === "main") S.activeAiSession.id = sid;
        HP.refreshAiSessionSwitcher(slideName, epoch);
      }
      // Stage 4-3 降级：sidecar 不可达（Flask 503）→ 可恢复提示 + 30s 重试探测。
      if (resp.status === 503) { handleAiUnavailable(slideName, epoch, runCtrl); return; }
      if (!resp.ok || !resp.body) { return HP.aiResponseError(resp).then(function (msg) { throw new Error(msg); }); }
      return HP.pumpAiSse(resp.body.getReader(), slideName, epoch, runCtrl);
    }).catch(function (e) {
      if (!HP.isCurrentAiSlide(slideName, epoch)) return;
      if (e && e.name === "AbortError") return;
      if (S.aiAbortCtrl !== runCtrl) return;
      var em = (e && e.message ? e.message : e);
      toast(t("ai.run.fail", { e: em }), "error");
      appendStatusRow(els.aiTrace, "error", t("ai.run.fail", { e: em }));
      HP.finishAiRun(runCtrl);
    });
  }

  // 继续 paused 的主 run（POST /api/ai/continue，SSE）
  function continueAiRun() {
    var els = S.els;
    if (!S.slide) { toast(t("roi.need.slide"), "info"); return; }
    if (S.aiRunning) { toast(t("ai.busy"), "info"); return; }
    if (!S.aiConfig || !S.aiConfig.base_url || !S.aiConfig.api_key_set) { toast(t("ai.need.config"), "error"); return; }
    S.aiRunning = true; S.aiPaused = false; setAiRunningUi(true);
    appendStatusRow(els.aiTrace, "info", t("ai.continue.trace"));
    var slideName = S.slide.name;
    var epoch = S.aiSlideEpoch;
    S.aiAbortCtrl = new AbortController();
    var runCtrl = S.aiAbortCtrl;
    fetch("/api/ai/continue", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slide: slideName }), signal: runCtrl.signal, credentials: "same-origin",
    }).then(function (resp) {
      if (!HP.isCurrentAiSlide(slideName, epoch)) return;
      var sid = resp.headers.get("X-AI-Session-ID");
      if (sid) {
        S.aiSessionId = sid;
        if (S.activeAiSession && S.activeAiSession.kind === "main") S.activeAiSession.id = sid;
        HP.refreshAiSessionSwitcher(slideName, epoch);
      }
      if (!resp.ok || !resp.body) { return HP.aiResponseError(resp).then(function (msg) { throw new Error(msg); }); }
      return HP.pumpAiSse(resp.body.getReader(), slideName, epoch, runCtrl);
    }).catch(function (e) {
      if (!HP.isCurrentAiSlide(slideName, epoch)) return;
      if (e && e.name === "AbortError") return;
      if (S.aiAbortCtrl !== runCtrl) return;
      var cem = (e && e.message ? e.message : e);
      toast(t("ai.continue.fail", { e: cem }), "error");
      appendStatusRow(els.aiTrace, "error", t("ai.continue.fail", { e: cem }));
      HP.finishAiRun(runCtrl);
    });
  }

  // 新会话（fresh）：归档旧 main 开新
  function freshAiRun() {
    if (!S.slide) { toast(t("roi.need.slide"), "info"); return; }
    if (S.aiRunning) { toast(t("ai.busy.stop"), "info"); return; }
    if (!S.aiConfig || !S.aiConfig.base_url || !S.aiConfig.api_key_set) { toast(t("ai.need.config"), "error"); return; }
    if (!confirm(t("ai.fresh.confirm"))) return;
    startAiRun();
  }

  function stopAiRun() {
    var slideName = S.slide && S.slide.name;
    HP.api("/api/ai/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: S.aiSessionId, slide: slideName }),
    }).catch(function () {});
    if (S.aiAbortCtrl) { try { S.aiAbortCtrl.abort(); } catch (e) {} }
    if (S.aiStreamCtrl) { try { S.aiStreamCtrl.abort(); } catch (e) {} }
    S.aiPaused = true;
    appendStatusRow(S.els.aiTrace, "paused", t("ai.stopped"));
    HP.finishAiRun();
    HP.refreshAiSessionSwitcher();
  }

  function finishAiRun(expectedCtrl) {
    if (expectedCtrl && S.aiAbortCtrl !== expectedCtrl) return;
    S.aiRunning = false;
    setAiRunningUi(false);
    S.aiAbortCtrl = null;
    S.aiStreamCtrl = null;
  }

  // ---------- AI 服务降级监测（Stage 4-3） ----------
  // sidecar 不可达时 /api/ai/* 返回 503（Flask 代理 _sidecar_unavailable_response），
  // 前端在此显示可恢复提示「AI 服务暂不可用，平台功能正常」，并每 30s 轮询一次
  // /api/ai/config（平台可达性）与 /api/ai/run 触发点的 503 探测；恢复后横幅消失。
  // 判定 503 的来源：统一走 _handleAiUnavailable(respStatus, slideName, epoch, runCtrl)。
  var _degradeTimer = null;
  var _degradeShown = false;

  // 显示/隐藏降级横幅。
  function setAiDegraded(show) {
    if (S.els.aiDegradeBanner) {
      S.els.aiDegradeBanner.style.display = show ? "block" : "none";
      S.els.aiDegradeBanner.textContent = show ? t("ai.degraded") : "";
    }
    if (show !== _degradeShown) {
      _degradeShown = show;
      if (show) startAiDegradeProbe();
    }
  }

  // 每 30s 探测一次平台 /healthz（Flask 自身端点，返回 {ok, backend, sidecar}）。
  // sidecar==="reachable" 即在线 → 隐藏横幅并停止轮询。
  function startAiDegradeProbe() {
    if (_degradeTimer) return;
    _degradeTimer = setInterval(function () {
      if (!S.els.aiDegradeBanner) { stopAiDegradeProbe(); return; }
      HP.api("/healthz").then(function (r) {
        return r.json().then(function (body) {
          if (r.ok && body && body.sidecar === "reachable") {
            stopAiDegradeProbe();
            setAiDegraded(false);
          }
          // 仍是 unreachable → 保持横幅，下轮再试。
        });
      }).catch(function () { /* 网络抖动：保持横幅，下轮再试 */ });
    }, 30000);
  }

  function stopAiDegradeProbe() {
    if (_degradeTimer) { clearInterval(_degradeTimer); _degradeTimer = null; }
  }

  // 统一 503 处理：进入降级态（显示横幅 + 启动 30s 重试探测），并结束本次 run。
  function handleAiUnavailable(slideName, epoch, runCtrl) {
    if (!HP.isCurrentAiSlide(slideName, epoch)) return;
    if (S.aiAbortCtrl !== runCtrl) return;
    setAiDegraded(true);
    toast(t("ai.degraded"), "info");
    appendStatusRow(S.els.aiTrace, "error", t("ai.degraded"));
    HP.finishAiRun(runCtrl);
  }

  // 运行中/暂停/空闲的按钮组状态（branch 活跃时隐藏继续/新会话）
  function setAiRunningUi(running) {
    var els = S.els;
    var isBranch = !!(S.activeAiSession && S.activeAiSession.kind === "branch");
    if (running) {
      els.aiStartBtn.style.display = "none"; els.aiContinueBtn.style.display = "none";
      els.aiFreshBtn.style.display = "none"; els.aiStopBtn.style.display = "inline-flex";
      els.aiComposerAux.style.display = "none";
    } else if (S.aiPaused) {
      els.aiStartBtn.style.display = "inline-flex";
      els.aiContinueBtn.style.display = isBranch ? "none" : "inline-block";
      els.aiFreshBtn.style.display = isBranch ? "none" : "inline-block";
      els.aiStopBtn.style.display = "none"; els.aiComposerAux.style.display = "flex";
    } else {
      els.aiStartBtn.style.display = "inline-flex"; els.aiContinueBtn.style.display = "none";
      els.aiFreshBtn.style.display = "none"; els.aiStopBtn.style.display = "none";
      els.aiComposerAux.style.display = "none";
    }
  }

  function applyActiveSessionUi() { setAiRunningUi(S.aiRunning); }

  function isCurrentAiSlide(slideName, epoch) {
    return !!(S.slide && slideName && S.slide.name === slideName &&
              epoch != null && epoch === S.aiSlideEpoch);
  }

  // 切片切换时完整隔离 AI 主会话状态（DOM / 游标 / SSE / running）。
  function resetAiForSlide() {
    var els = S.els;
    S.aiSlideEpoch += 1;
    if (S.aiStreamCtrl) { try { S.aiStreamCtrl.abort(); } catch (e) {} }
    if (S.aiAbortCtrl) { try { S.aiAbortCtrl.abort(); } catch (e) {} }
    S.aiStreamCtrl = null; S.aiAbortCtrl = null;
    S.aiRunning = false; S.aiPaused = false; S.aiSessionId = null;
    S.activeAiSession = null;
    S.aiSessionListCache = [];
    S.mainAiCtx.lastSeq = 0; S.mainAiCtx.bubbleEl = null; S.mainAiCtx.thinkingEl = null;
    S.mainAiCtx.container = els.aiTrace;
    S.overlay = [];
    if (els.aiTrace) { els.aiTrace.innerHTML = '<div class="ai-trace-empty">' + esc(t("ai.trace.empty")) + "</div>"; }
    if (els.aiSessionBar) els.aiSessionBar.style.display = "none";
    if (els.aiSessionSelect) els.aiSessionSelect.innerHTML = "";
    setAiRunningUi(false);
    HP.setOverlay([]);
    return S.aiSlideEpoch;
  }

  function resetAiTrace() {
    S.els.aiTrace.innerHTML = "";
    S.overlay = [];
    S.mainAiCtx.bubbleEl = null; S.mainAiCtx.thinkingEl = null; S.mainAiCtx.container = S.els.aiTrace;
    HP.setOverlay([]);
  }

  // ctx: {container, bubbleEl, thinkingEl, isFork}
  function ensureAiCtx(ctx) {
    if (ctx) { if (!ctx.container) ctx.container = S.els.aiTrace; return ctx; }
    if (!S.mainAiCtx.container) S.mainAiCtx.container = S.els.aiTrace;
    return S.mainAiCtx;
  }

  // 按 SSE 事件类型渲染轨迹流 + 通过 HostBridge 驱动平台叠加层
  function handleAiEvent(type, p, ctx) {
    ctx = ensureAiCtx(ctx);
    var container = ctx.container;
    if (type === "slide_opened") {
      if (p.viewport) {
        S.overlay = [{ x: p.viewport.x, y: p.viewport.y, w: p.viewport.w, h: p.viewport.h,
                       magnification: t("ai.mag.overview") }];
        HP.setOverlay(S.overlay);
      }
      appendStatusRow(container, "info", t("ai.slide.opened", { name: (p.slide || "") }));
      return;
    }
    if (type === "session_resumed" || type === "fork_resumed" || type === "fork_created") {
      appendStatusRow(container, "info", type === "fork_created" ? t("ai.fork.created") : t("ai.session.restored"));
      return;
    }
    if (type === "branch_created" || type === "branch_resumed") {
      appendStatusRow(container, "info", type === "branch_created" ? t("ai.branch.created") : t("ai.branch.restored"));
      if (!ctx.isFork) {
        var bSid = (p && p.session_id) || S.aiSessionId;
        if (bSid) {
          S.aiSessionId = bSid;
          S.activeAiSession = { id: bSid, kind: "branch",
            annotation_id: (p && p.annotation_id) || (S.activeAiSession && S.activeAiSession.annotation_id) || null };
          applyActiveSessionUi();
          HP.refreshAiSessionSwitcher();
        }
      }
      return;
    }
    if (type === "agent_thinking") { setThinkingRow(ctx); return; }
    if (type === "text_delta") { appendTextBubble(p.text || "", ctx); return; }
    if (type === "tool_started") {
      clearThinkingRow(ctx);
      if (p.tool === "goto") {
        appendToolCall(container, "goto", "(" + HP.fmtNum(p.x) + "," + HP.fmtNum(p.y) + ") @ " +
          (p.magnification || "") + (p.reason ? " · " + p.reason : ""));
      } else if (p.tool === "snapshot") {
        appendToolCall(container, "snapshot", t("ai.tool.snapshot"));
      } else if (p.tool === "finish" || p.tool === "mark_observation" ||
                 p.tool === "complete_snapshot_review" || p.tool === "create_annotation") {
        // 结果会以附件/状态呈现，过程区不重复刷工具名
      } else {
        appendToolCall(container, p.tool || "tool", "");
      }
      return;
    }
    if (type === "snapshot_captured") {
      clearThinkingRow(ctx);
      var bb = p.bboxLevel0 || {};
      S.overlay.push({ x: bb.x, y: bb.y, w: bb.w, h: bb.h, magnification: p.magnification || "" });
      if (S.overlay.length > 1) S.overlay = S.overlay.slice(-1);
      // 导航外扩 20% + overlay 用原始 bbox：虚线框留在视野内一圈（HP.setOverlay 已含在内）
      HP.navigateWithOverlay(bb, p.magnification);
      appendSnapshotCard(container, { magnification: p.magnification, bbox: bb });
      return;
    }
    if (type === "observation") { clearThinkingRow(ctx); appendObservationCard(p, container); return; }
    if (type === "snapshot_reviewed") {
      clearThinkingRow(ctx);
      var disp = p.disposition || "";
      var title = (disp === "annotated") ? t("ai.review.annotated")
                : (disp === "no_annotation") ? t("ai.review.no.anno") : t("ai.review.done");
      appendReviewCard(title, p.summary || "", disp === "no_annotation" ? (p.no_annotation_reason || "") : "", container);
      return;
    }
    if (type === "annotation_created") {
      clearThinkingRow(ctx);
      appendAnnotationCard(p, container, { showFork: !ctx.isFork });
      // 插件落标注 → 通知平台刷新标注面板与索引（替代直接调 refreshCurrentAnnotations）
      HP.bridge.emit("annotation.changed", { source: "ai", slide: S.slide && S.slide.name });
      if (!ctx.isFork) HP.refreshAiSessionSwitcher();
      return;
    }
    if (type === "session_compacted") {
      clearThinkingRow(ctx);
      appendToolCall(container, "compact", (p && p.reason === "context_length_exceeded") ? t("ai.compact.full") : t("ai.compact.cont"));
      return;
    }
    if (type === "agent_paused") {
      clearThinkingRow(ctx);
      appendStatusRow(container, "paused", t("ai.paused.summary", { s: (p.summary || "") }));
      if (!ctx.isFork) S.aiPaused = true;
      return;
    }
    if (type === "agent_finished") {
      clearThinkingRow(ctx);
      appendStatusRow(container, "finished", p.summary || t("ai.finished.fallback"));
      if (!ctx.isFork) HP.setOverlay(S.overlay);
      return;
    }
    if (type === "agent_retrying") { appendReconnectStatus(container, p); return; }
    if (type === "agent_error") {
      clearThinkingRow(ctx);
      appendStatusRow(container, "error", p.error || t("ai.error.fallback"));
      return;
    }
  }

  // event_reset：事件缓冲滚过断点 → 清轨迹，GET session 全量重建，再接 live 流
  function handleAiEventReset(payload, slideName, epoch) {
    if (slideName != null && epoch != null && !HP.isCurrentAiSlide(slideName, epoch)) return;
    HP.resetAiTrace();
    appendStatusRow(S.els.aiTrace, "info", t("ai.history.long.refresh"));
    if (!S.aiSessionId) { toast(t("ai.history.long.done"), "info"); return; }
    var sid = S.aiSessionId;
    HP.api("/api/ai/session/" + encodeURIComponent(sid))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (slideName != null && epoch != null && !HP.isCurrentAiSlide(slideName, epoch)) return;
        var s = data && data.session, tx = data && data.transcript;
        HP.resetAiTrace();
        if (tx && tx.length) HP.renderAiTranscript(tx);
        else appendStatusRow(S.els.aiTrace, "info", t("ai.history.long.done"));
        if (s && s.last_event_seq != null) {
          S.mainAiCtx.lastSeq = Math.max(S.mainAiCtx.lastSeq || 0, Number(s.last_event_seq) || 0);
        }
        toast(t("ai.refreshed"), "success");
      })
      .catch(function () {
        if (slideName != null && epoch != null && !HP.isCurrentAiSlide(slideName, epoch)) return;
        appendStatusRow(S.els.aiTrace, "info", t("ai.history.long.done.trace"));
        toast(t("ai.history.long.done"), "info");
      });
  }

  // ---------- 轨迹流 DOM 辅助（与 ctx 强耦合，驻留入口文件） ----------
  function appendTextBubble(text, ctx) {
    ctx = ensureAiCtx(ctx);
    var container = ctx.container;
    // 可续写的气泡累计文本仍为空白 → 不建 DOM（流式空 text_delta 不留空气泡）
    var alive = ctx.bubbleEl && !ctx.bubbleEl.closed && ctx.bubbleEl.parentNode === container;
    if (!((alive ? (ctx.bubbleEl._rawText || "") : "") + (text || "")).trim()) return;
    HP.clearAiEmpty(container);
    if (ctx.thinkingEl && ctx.thinkingEl.parentNode) {
      ctx.thinkingEl.parentNode.removeChild(ctx.thinkingEl); ctx.thinkingEl = null;
    }
    if (!alive) {
      var prev = container.lastElementChild;
      ctx.bubbleEl = document.createElement("div");
      ctx.bubbleEl.className = "ai-chat-bubble assistant";
      if (prev && prev.classList.contains("ai-chat-bubble") && prev.classList.contains("assistant")) {
        prev.classList.remove("tail"); ctx.bubbleEl.classList.add("grp");
      }
      ctx.bubbleEl.classList.add("tail");
      ctx.bubbleEl._rawText = ""; ctx.bubbleEl.closed = false;
      container.appendChild(ctx.bubbleEl);
      HP.markUserBubblesRead(container);
    }
    ctx.bubbleEl._rawText = (ctx.bubbleEl._rawText || "") + (text || "");
    HP.setBubbleContent(ctx.bubbleEl, "assistant", ctx.bubbleEl._rawText);
    container.scrollTop = container.scrollHeight;
  }

  function setThinkingRow(ctx) {
    ctx = ensureAiCtx(ctx);
    var target = ctx.container;
    if (ctx.thinkingEl && ctx.thinkingEl.parentNode === target) return;
    HP.clearAiEmpty(target);
    ctx.thinkingEl = document.createElement("div");
    ctx.thinkingEl.className = "ai-chat-thinking";
    ctx.thinkingEl.setAttribute("aria-label", t("ai.thinking.aria"));
    for (var i = 0; i < 3; i++) { var dot = document.createElement("span"); dot.className = "dot"; ctx.thinkingEl.appendChild(dot); }
    target.appendChild(ctx.thinkingEl);
    HP.markUserBubblesRead(target);
    target.scrollTop = target.scrollHeight;
  }
  function clearThinkingRow(ctx) {
    ctx = ensureAiCtx(ctx);
    if (ctx.thinkingEl && ctx.thinkingEl.parentNode) ctx.thinkingEl.parentNode.removeChild(ctx.thinkingEl);
    ctx.thinkingEl = null;
    // 空白气泡不留 DOM：移除置空，而不是 close 一个空泡挂在轨迹里
    if (ctx.bubbleEl && !String(ctx.bubbleEl._rawText || "").trim()) {
      if (ctx.bubbleEl.parentNode) ctx.bubbleEl.parentNode.removeChild(ctx.bubbleEl);
      ctx.bubbleEl = null;
      return;
    }
    if (ctx.bubbleEl) { ctx.bubbleEl.closed = true; }
  }

  // ---------- init ----------
  function init() {
    S.els = {
      aiPanel: $("ai-panel"), aiPanelClose: $("ai-panel-close"),
      aiConfigWrap: $("ai-config-wrap"), aiConfigCollapsed: $("ai-config-collapsed"),
      aiConfigSummary: $("ai-config-summary"), aiReconfigBtn: $("ai-reconfig-btn"),
      aiBaseUrl: $("ai-base-url"), aiApiKey: $("ai-api-key"), aiModel: $("ai-model"),
      aiMaxSteps: $("ai-max-steps"), aiApiProtocol: $("ai-api-protocol"),
      aiWindowTier: $("ai-window-tier"), aiCtxWindow: $("ai-ctx-window"),
      aiReserve: $("ai-reserve"), aiSafetyMargin: $("ai-safety-margin"),
      aiKeepRecent: $("ai-keep-recent"), aiForkLimit: $("ai-fork-limit"),
      aiLeaseTtl: $("ai-lease-ttl"), aiConfigSave: $("ai-config-save"),
      aiConfigHint: $("ai-config-hint"), aiTask: $("ai-task"),
      aiComposerAux: $("ai-composer-aux"), aiTaskJump: $("ai-task-jump"),
      aiStartBtn: $("ai-start-btn"), aiContinueBtn: $("ai-continue-btn"),
      aiFreshBtn: $("ai-fresh-btn"), aiStopBtn: $("ai-stop-btn"),
      aiTrace: $("ai-trace"), aiSessionBar: $("ai-session-bar"),
      aiSessionSelect: $("ai-session-select"),
      aiUsePlatform: $("ai-use-platform"), aiUsePlatformWrap: $("ai-use-platform-wrap"),
      aiConfigSourceHint: $("ai-config-source-hint"),
      aiTuneAdminNote: $("ai-tune-admin-note"),
      aiDegradeBanner: $("ai-degrade-banner"),
    };
    S.mainAiCtx.container = S.els.aiTrace;
    var els = S.els;

    els.aiPanelClose.addEventListener("click", closeAiPanel);
    els.aiConfigSave.addEventListener("click", HP.saveAiConfig);
    els.aiReconfigBtn.addEventListener("click", function () {
      els.aiConfigCollapsed.style.display = "none";
      els.aiConfigWrap.style.display = "block";
      if (S.aiConfig && S.aiConfig.api_key_mask) {
        els.aiApiKey.value = S.aiConfig.api_key_mask;
        els.aiApiKey.placeholder = t("ai.apikey.ph");
      }
      HP.fillAiTuningFields();
    });
    els.aiStartBtn.addEventListener("click", submitAiComposer);
    els.aiContinueBtn.addEventListener("click", continueAiRun);
    els.aiFreshBtn.addEventListener("click", freshAiRun);
    els.aiStopBtn.addEventListener("click", stopAiRun);
    els.aiTaskJump.addEventListener("click", function () {
      // 当前选区由平台持有（ROI/选中标注）→ 通过 HostBridge 取 bbox
      HP.bridge.request("selection.getBbox", {}).then(function (bbox) {
        if (!bbox) { toast(t("ai.taskjump.need"), "info"); return; }
        var prefix = t("ai.task.prefix", { x: bbox.x, y: bbox.y, w: bbox.w, h: bbox.h });
        var cur = els.aiTask.value || "";
        els.aiTask.value = prefix + cur;
        els.aiTask.focus();
        autoGrowAiTask();
      }).catch(function () { /* 平台未启用插件能力：静默 */ });
    });
    els.aiTask.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submitAiComposer(); }
    });
    els.aiTask.addEventListener("input", autoGrowAiTask);
    autoGrowAiTask();
    if (els.aiSessionSelect) els.aiSessionSelect.addEventListener("change", HP.onAiSessionChange);

    // 加载 AI 配置（渲染设置区/折叠区）
    HP.loadAiConfig();

    // Stage 4-3 降级：启动时探测一次 sidecar 可达性（平台 /healthz 的 sidecar 字段）。
    // 不可达 → 显示降级横幅并启动 30s 自动重试探测；恢复后横幅消失。
    HP.api("/healthz").then(function (r) {
      return r.json().then(function (body) {
        if (r.ok && body && body.sidecar === "unreachable") setAiDegraded(true);
        else setAiDegraded(false);
      });
    }).catch(function () { setAiDegraded(false); /* 平台自身不可达不误报降级 */ });

    // 获取当前身份 role（Stage 3a-2b）：owner 全配置；user 只读调优、可配自有凭据。
    // AUTH_ENABLED=False（内网）→ /api/auth/info 返回 role=null，按 owner 处理。
    HP.api("/api/auth/info").then(function (r) { return r.json(); }).then(function (info) {
      S.role = info && info.role;
      if (S.role === "user") {
        if (S.els.aiTuneAdminNote) S.els.aiTuneAdminNote.style.display = "block";
        HP.applyUserReadonlyTuning(true);
        HP.renderAiConfigState(); // 重渲染凭据区（use_platform 勾选/禁用）
      }
    }).catch(function () { /* 内网/AUTH 关闭：保持 owner 语义 */ });

    // ---------- HostBridge 处理器注册 ----------
    // Host→Plugin request
    HP.onRequest("panel.toggle", function (payload) {
      var desired = payload && payload.open;
      if (desired === true) openAiPanel();
      else if (desired === false) closeAiPanel();
      else { if (S.aiPanelOpen) closeAiPanel(); else openAiPanel(); }
      return { open: !!S.aiPanelOpen };
    });
    // 批注行 ⑂ 按钮：在 AI 面板开/续分支会话
    HP.onRequest("branch.open", function (payload) {
      HP.openBranchFromAnno(payload && payload.annotationId);
      return { ok: true };
    });
    // fork 就地展开。anchorEl 为 DOM 引用（STAGE2-DEVIATION：信封夹带不可序列化对象，
    // 仅同窗口可用；iframe 阶段改为插件自有容器，见 docs §7.5 旁注）。
    HP.onRequest("fork.open", function (payload) {
      var aid = payload && payload.annotationId;
      var anchor = payload && payload.anchorEl;
      if (aid) HP.openForkChat(aid, anchor); // sessions.js：已有则切换显隐，否则挂到 anchor 之后
      return { ok: true };
    });
    // Host→Plugin event
    HP.onEvent("slide.opened", function (p) {
      var slide = p && p.slide;
      S.slide = slide || null;
      var epoch = resetAiForSlide();
      if (slide && slide.name) HP.restoreAiSession(slide.name, epoch);
    });
    // 注：lang.changed 本阶段不实现为 bridge event——插件直接监听 i18n.js 的
    // hp-lang-change CustomEvent（共享库）。iframe 阶段改为 host 转 lang.changed event。
    // auth.* 与 viewport.changed 本阶段不实现（Stage 3/4 补）。
  }

  // 语言切换：重渲染 AI 配置摘要 / 会话切换器（直接监听 i18n.js 的 hp-lang-change）
  document.addEventListener("hp-lang-change", function () {
    try {
      if (S.aiConfig) {
        HP.renderAiConfigState();
        if (S.aiSessionListCache) HP.renderAiSessionOptions(S.aiSessionListCache, !!S.aiConfig);
      }
    } catch (e) {}
  });

  // 导出
  HP.openAiPanel = openAiPanel;
  HP.closeAiPanel = closeAiPanel;
  HP.submitAiComposer = submitAiComposer;
  HP.startAiRun = startAiRun;
  HP.continueAiRun = continueAiRun;
  HP.freshAiRun = freshAiRun;
  HP.stopAiRun = stopAiRun;
  HP.finishAiRun = finishAiRun;
  HP.setAiRunningUi = setAiRunningUi;
  HP.isCurrentAiSlide = isCurrentAiSlide;
  HP.resetAiForSlide = resetAiForSlide;
  HP.resetAiTrace = resetAiTrace;
  HP.ensureAiCtx = ensureAiCtx;
  HP.handleAiEvent = handleAiEvent;
  HP.handleAiEventReset = handleAiEventReset;
  HP.setThinkingRow = setThinkingRow;
  HP.clearThinkingRow = clearThinkingRow;
  HP.setAiDegraded = setAiDegraded;
  HP.handleAiUnavailable = handleAiUnavailable;

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
