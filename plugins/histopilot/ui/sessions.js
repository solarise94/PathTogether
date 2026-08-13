/* =========================================================================
   HistoPilot UI bundle —— 会话切换器 + branch + fork 批注对话

   - restore/attach/abort：断线重挂与切片隔离
   - 切换器：列出本切片 main + branch（fork 不进列表），切换活跃会话
   - branch：POST /api/ai/branch（标注行 ⑂ 深读入口）
   - fork：就地展开的轻量批注对话（状态机 loading/sending/readonly/idle）

   activeAiSession 三态状态机（main/branch/null）整体驻留插件，未拆散。
   fork 就地展开的容器由 HostBridge fork.open 请求的 anchorEl 提供（STAGE2-DEVIATION：
   信封夹带 DOM 引用，仅同窗口可用；iframe 阶段改为插件自有容器）。
   ========================================================================= */
(function () {
  "use strict";
  var HP = window.HistoPilot;
  var S = HP.s;
  var apiFetch = HP.api, t = HP.t, tt = HP.tt, toast = HP.toast, esc = HP.esc;
  var appendStatusRow = null, appendMsgTs = null, appendChatBubble = null,
      setThinkingRow = null, clearThinkingRow = null, renderAiTranscript = null;

  // GET session detail，把脱敏 transcript 渲染成 SMS 聊天气泡。
  // container=主 AI 轨迹 或 fork 对话流；opts 透传给 renderAiTranscript。
  function loadAndRenderTranscript(sessionId, container, opts) {
    if (!sessionId || !container) return Promise.resolve();
    opts = opts || {};
    var slideName = opts.slideName;
    var epoch = opts.epoch;
    var isMain = !(opts.emphasis === "fork");
    var forkGen = opts.forkGen;
    var forkWrap = opts.forkWrap;
    function forkStale() { return !isMain && forkWrap && forkWrap._loadGen !== forkGen; }
    return apiFetch("/api/ai/session/" + encodeURIComponent(sessionId))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (isMain && slideName != null && !HP.isCurrentAiSlide(slideName, epoch)) return;
        if (forkStale()) return;
        var s = data && data.session;
        var tx = (data && data.transcript) || [];
        container.innerHTML = "";
        if (tx && tx.length) {
          renderAiTranscript(tx, { container: container, emphasis: opts.emphasis || "main", ts: s && s.created_at });
        } else {
          appendStatusRow(container, "info", t("ai.history.empty"));
        }
        if (s && s.last_event_seq != null) {
          var seqN = Number(s.last_event_seq) || 0;
          if (opts.emphasis === "fork") {
            var fctx = forkTraceCtx(container);
            fctx.lastSeq = Math.max(fctx.lastSeq || 0, seqN);
          } else if (!slideName || HP.isCurrentAiSlide(slideName, epoch)) {
            S.mainAiCtx.lastSeq = Math.max(S.mainAiCtx.lastSeq || 0, seqN);
          }
        }
        if (!isMain && forkWrap && forkWrap._forkState === "loading") setForkState(forkWrap, "idle");
      })
      .catch(function () {
        if (isMain && slideName != null && !HP.isCurrentAiSlide(slideName, epoch)) return;
        if (forkStale()) return;
        appendStatusRow(container, "info", t("ai.history.fail"));
        if (!isMain && forkWrap && forkWrap._forkState === "loading") setForkState(forkWrap, "idle");
      });
  }

  // 断线重挂：页面刷新/重开切片后，GET session + 带 Last-Event-ID 重放进行中的 run。
  function restoreAiSession(slideName, epoch) {
    if (!slideName) return;
    if (epoch == null) epoch = S.aiSlideEpoch;
    if (!S.aiConfig || !S.aiConfig.base_url || !S.aiConfig.api_key_set) return;
    if (!HP.isCurrentAiSlide(slideName, epoch)) return;
    apiFetch("/api/ai/sessions?slide=" + encodeURIComponent(slideName))
      .then(function (r) { if (!HP.isCurrentAiSlide(slideName, epoch)) return null; return r.json(); })
      .then(function (data) {
        if (!data || !HP.isCurrentAiSlide(slideName, epoch)) return;
        var sessions = (data && data.sessions) || [];
        var main = null;
        for (var i = 0; i < sessions.length; i++) { if (sessions[i].kind === "main") { main = sessions[i]; break; } }
        S.activeAiSession = main ? { id: main.id, kind: "main", annotation_id: null } : null;
        applyActiveSessionUi();
        renderAiSessionOptions(sessions.filter(function (s) { return s && (s.kind === "main" || s.kind === "branch"); }),
          !!(S.aiConfig && S.aiConfig.base_url && S.aiConfig.api_key_set));
        if (!main) { HP.setAiRunningUi(false); return; }
        if (!HP.isCurrentAiSlide(slideName, epoch)) return;
        S.aiSessionId = main.id;
        var txOpts = { emphasis: "main", slideName: slideName, epoch: epoch };
        if (main.status === "running") {
          S.aiRunning = true; S.aiPaused = false; HP.setAiRunningUi(true);
          appendStatusRow(S.els.aiTrace, "info", t("ai.restore.main"));
          loadAndRenderTranscript(main.id, S.els.aiTrace, txOpts).then(function () {
            if (HP.isCurrentAiSlide(slideName, epoch)) attachAiStream(main.id, slideName, epoch);
          });
        } else if (main.status === "paused" || main.status === "finished" || main.status === "error") {
          S.aiPaused = (main.status === "paused");
          HP.setAiRunningUi(false);
          loadAndRenderTranscript(main.id, S.els.aiTrace, txOpts).then(function () {
            if (!HP.isCurrentAiSlide(slideName, epoch)) return;
            if (main.status === "paused") appendStatusRow(S.els.aiTrace, "paused", t("ai.paused.paren"));
            else if (main.status === "finished") appendStatusRow(S.els.aiTrace, "finished", t("ai.finished.trace"));
            else appendStatusRow(S.els.aiTrace, "error", t("ai.error.last"));
          });
        }
      })
      .catch(function () { /* 静默 */ });
  }

  function attachAiStream(sessionId, slideName, epoch) {
    if (slideName == null) slideName = S.slide && S.slide.name;
    if (epoch == null) epoch = S.aiSlideEpoch;
    if (!HP.isCurrentAiSlide(slideName, epoch)) return;
    var url = "/api/ai/session/" + encodeURIComponent(sessionId) + "/stream?after_seq=" + (S.mainAiCtx.lastSeq || 0);
    if (S.aiStreamCtrl) { try { S.aiStreamCtrl.abort(); } catch (e) {} }
    S.aiStreamCtrl = new AbortController();
    fetch(url, {
      headers: { "Last-Event-ID": String(S.mainAiCtx.lastSeq || 0) },
      signal: S.aiStreamCtrl.signal,
      credentials: "same-origin",
    }).then(function (resp) {
      if (!HP.isCurrentAiSlide(slideName, epoch)) return;
      if (!resp.ok || !resp.body) { throw new Error("HTTP " + resp.status); }
      return HP.pumpAiSse(resp.body.getReader(), slideName, epoch);
    }).catch(function (e) {
      if (e && e.name === "AbortError") return;
      if (!HP.isCurrentAiSlide(slideName, epoch)) return;
      toast(t("ai.restream.fail"), "error");
    });
  }

  // 中止当前活跃会话的 SSE（run 流 + 重挂流），但不清切片状态/游标。
  function abortActiveAiStream() {
    if (S.aiAbortCtrl) { try { S.aiAbortCtrl.abort(); } catch (e) {} }
    if (S.aiStreamCtrl) { try { S.aiStreamCtrl.abort(); } catch (e) {} }
    S.aiAbortCtrl = null;
    S.aiStreamCtrl = null;
    S.aiRunning = false;
  }

  // 刷新切换器下拉：GET /api/ai/sessions?slide= → 只列 main+branch。
  // aiSessionRefreshGen 单调递增：只允许最后一次请求渲染，过期响应一律丢弃。
  function refreshAiSessionSwitcher(slideName, epoch) {
    var els = S.els;
    if (!els.aiSessionBar || !els.aiSessionSelect) return;
    if (!S.slide) { els.aiSessionBar.style.display = "none"; els.aiSessionSelect.innerHTML = ""; return; }
    if (slideName == null) slideName = S.slide.name;
    if (epoch == null) epoch = S.aiSlideEpoch;
    var myGen = ++S.aiSessionRefreshGen;
    var configured = !!(S.aiConfig && S.aiConfig.base_url && S.aiConfig.api_key_set);
    apiFetch("/api/ai/sessions?slide=" + encodeURIComponent(slideName))
      .then(function (r) {
        if (myGen !== S.aiSessionRefreshGen) return null;
        if (!HP.isCurrentAiSlide(slideName, epoch)) return null;
        return r.json();
      })
      .then(function (data) {
        if (!data || myGen !== S.aiSessionRefreshGen) return;
        if (!HP.isCurrentAiSlide(slideName, epoch)) return;
        var sessions = (data && data.sessions) || [];
        var list = [];
        for (var i = 0; i < sessions.length; i++) {
          var s = sessions[i];
          if (!s || (s.kind !== "main" && s.kind !== "branch")) continue;
          list.push(s);
        }
        renderAiSessionOptions(list, configured);
      })
      .catch(function () {});
  }

  function renderAiSessionOptions(list, configured) {
    var els = S.els;
    S.aiSessionListCache = (list || []).slice();
    var sel = els.aiSessionSelect;
    sel.innerHTML = "";
    if (!configured || S.aiSessionListCache.length === 0) { els.aiSessionBar.style.display = "none"; return; }
    els.aiSessionBar.style.display = "flex";
    var activeId = S.activeAiSession && S.activeAiSession.id;
    var mainId = null;
    for (var i = 0; i < S.aiSessionListCache.length; i++) {
      if (S.aiSessionListCache[i].kind === "main") { mainId = S.aiSessionListCache[i].id; break; }
    }
    var selectedId = activeId || mainId;
    for (var k = 0; k < S.aiSessionListCache.length; k++) {
      var s = S.aiSessionListCache[k];
      var opt = document.createElement("option");
      opt.value = s.id;
      var title = s.title || (s.kind === "main" ? t("ai.session.main") : t("ai.session.branch"));
      var badge = s.kind === "main" ? "main" : "branch";
      var status = s.status || "";
      opt.textContent = title + " · " + badge + (status ? " · " + status : "");
      sel.appendChild(opt);
    }
    sel.value = selectedId || S.aiSessionListCache[0].id;
  }

  function onAiSessionChange() {
    if (!S.slide) return;
    var sel = S.els.aiSessionSelect;
    var sid = sel.value;
    if (!sid) return;
    var meta = null;
    for (var i = 0; i < S.aiSessionListCache.length; i++) {
      if (S.aiSessionListCache[i].id === sid) { meta = S.aiSessionListCache[i]; break; }
    }
    if (!meta) return;
    switchAiSession(meta, S.slide.name, S.aiSlideEpoch);
  }

  function switchAiSession(meta, slideName, epoch) {
    if (!meta || !meta.id) return;
    if (slideName == null) slideName = S.slide && S.slide.name;
    if (epoch == null) epoch = S.aiSlideEpoch;
    if (!HP.isCurrentAiSlide(slideName, epoch)) return;
    abortActiveAiStream();
    S.activeAiSession = { id: meta.id, kind: meta.kind || "main", annotation_id: meta.annotation_id || null };
    S.aiSessionId = meta.id;
    S.aiPaused = false;
    S.mainAiCtx.lastSeq = 0;
    HP.resetAiTrace();
    HP.setAiRunningUi(false);
    var txOpts = { emphasis: "main", slideName: slideName, epoch: epoch };
    var status = meta.status || "";
    if (status === "running") {
      S.aiRunning = true; HP.setAiRunningUi(true);
      appendStatusRow(S.els.aiTrace, "info",
        (S.activeAiSession.kind === "branch" ? t("ai.restore.branch") : t("ai.restore.main")));
      loadAndRenderTranscript(meta.id, S.els.aiTrace, txOpts).then(function () {
        if (HP.isCurrentAiSlide(slideName, epoch)) attachAiStream(meta.id, slideName, epoch);
      });
    } else {
      S.aiPaused = (status === "paused");
      HP.setAiRunningUi(false);
      loadAndRenderTranscript(meta.id, S.els.aiTrace, txOpts).then(function () {
        if (!HP.isCurrentAiSlide(slideName, epoch)) return;
        if (status === "paused") appendStatusRow(S.els.aiTrace, "paused", t("ai.paused.paren"));
        else if (status === "finished") appendStatusRow(S.els.aiTrace, "finished", t("ai.finished.trace"));
        else if (status === "error") appendStatusRow(S.els.aiTrace, "error", t("ai.error.last"));
      });
    }
    applyActiveSessionUi();
  }

  function applyActiveSessionUi() { HP.setAiRunningUi(S.aiRunning); }

  // =========================================================================
  // branch 起跑/续聊：POST /api/ai/branch {slide, annotation_id, question?}。
  // 返回 true=已发起（调用方可清草稿），false=前置校验未过（保留草稿）。
  function startBranchRun(annotationId, question) {
    var els = S.els;
    if (!S.slide) { toast(t("roi.need.slide"), "info"); return false; }
    if (!annotationId) { toast(t("ai.no.fork.id"), "error"); return false; }
    if (S.aiRunning) { toast(t("ai.busy.stop"), "info"); return false; }
    if (!S.aiConfig || !S.aiConfig.base_url || !S.aiConfig.api_key_set) {
      toast(t("ai.need.config"), "error");
      els.aiConfigWrap.style.display = "block"; els.aiConfigCollapsed.style.display = "none";
      return false;
    }
    abortActiveAiStream();
    HP.setAiRunningUi(false);
    S.activeAiSession = null;
    HP.resetAiTrace();
    S.mainAiCtx.lastSeq = 0;

    var slideName = S.slide.name;
    var epoch = S.aiSlideEpoch;
    var body = { slide: slideName, annotation_id: annotationId };
    var hasQuestion = typeof question === "string" && question.trim();
    if (hasQuestion) {
      body.question = question.trim();
      appendMsgTs(S.els.aiTrace, new Date());
      appendChatBubble(S.els.aiTrace, "user", question.trim());
    }
    appendStatusRow(S.els.aiTrace, "info", t("ai.branch.opening"));
    setThinkingRow(S.mainAiCtx);

    S.aiRunning = true; S.aiPaused = false; HP.setAiRunningUi(true);
    S.aiAbortCtrl = new AbortController();
    var runCtrl = S.aiAbortCtrl;

    fetch("/api/ai/branch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), signal: runCtrl.signal, credentials: "same-origin",
    }).then(function (resp) {
      if (!HP.isCurrentAiSlide(slideName, epoch)) return;
      var sid = resp.headers.get("X-AI-Session-ID");
      if (sid) S.aiSessionId = sid;
      if (resp.status === 410) {
        appendStatusRow(S.els.aiTrace, "error", t("ai.branch.deleted"));
        HP.finishAiRun(runCtrl);
        throw new Error("gone");
      }
      if (!resp.ok || !resp.body) { return HP.aiResponseError(resp).then(function (msg) { throw new Error(msg); }); }
      return HP.pumpAiSse(resp.body.getReader(), slideName, epoch, runCtrl);
    }).catch(function (e) {
      if (!HP.isCurrentAiSlide(slideName, epoch)) return;
      if (e && e.name === "AbortError") return;
      if (e && e.message === "gone") return;
      if (S.aiAbortCtrl !== runCtrl) return;
      var bem = (e && e.message ? e.message : e);
      toast(t("ai.branch.fail", { e: bem }), "error");
      appendStatusRow(S.els.aiTrace, "error", t("ai.branch.fail", { e: bem }));
      HP.finishAiRun(runCtrl);
    });
    return true;
  }

  // 批注条 ⑂ 按钮入口：确保 AI 面板打开 → 若该标注已有 branch 则复用，否则 POST 新建。
  function openBranchFromAnno(annotationId) {
    if (!S.slide) { toast(t("roi.need.slide"), "info"); return; }
    if (!annotationId) { toast(t("ai.no.fork.id"), "error"); return; }
    if (S.aiRunning) { toast(t("ai.busy.stop"), "info"); return; }
    if (!S.aiConfig || !S.aiConfig.base_url || !S.aiConfig.api_key_set) {
      toast(t("ai.need.config"), "error");
      HP.openAiPanel();
      S.els.aiConfigWrap.style.display = "block"; S.els.aiConfigCollapsed.style.display = "none";
      return;
    }
    if (!S.aiPanelOpen) HP.openAiPanel();
    var slideName = S.slide.name;
    var epoch = S.aiSlideEpoch;
    apiFetch("/api/ai/sessions?slide=" + encodeURIComponent(slideName))
      .then(function (r) { if (!HP.isCurrentAiSlide(slideName, epoch)) return null; return r.json(); })
      .then(function (data) {
        if (!data || !HP.isCurrentAiSlide(slideName, epoch)) return;
        var sessions = (data && data.sessions) || [];
        var existing = null;
        for (var i = 0; i < sessions.length; i++) {
          var s = sessions[i];
          if (s.kind === "branch" && s.annotation_id === annotationId) { existing = s; break; }
        }
        if (existing) {
          if (S.activeAiSession && S.activeAiSession.id === existing.id) return;
          switchAiSession(existing, slideName, epoch);
        } else {
          startBranchRun(annotationId, null);
        }
      })
      .catch(function (e) {
        if (!HP.isCurrentAiSlide(slideName, epoch)) return;
        toast(t("ai.branch.fail2", { e: (e && e.message ? e.message : e) }), "error");
      });
  }

  // =========================================================================
  // fork 批注对话（就地展开）。状态机：loading/sending/readonly/idle。
  function setForkState(wrap, st) {
    if (!wrap) return;
    wrap._forkState = st;
    var input = wrap.querySelector("input");
    var send = wrap.querySelector(".fork-chat-input button");
    if (st === "readonly") {
      wrap.classList.add("fork-readonly");
      if (input) input.disabled = true;
      if (send) { send.disabled = true; send.textContent = t("ai.fork.send"); }
      return;
    }
    wrap.classList.remove("fork-readonly");
    var busy = (st === "loading" || st === "sending");
    if (input) input.disabled = busy;
    if (send) { send.disabled = busy; send.textContent = busy ? tt("ai.fork.sending") : t("ai.fork.send"); }
  }

  // anchorEl：挂载锚点（标注行 / 对话卡片动作区）。HostBridge fork.open 传入（STAGE2-DEVIATION）。
  function openForkChat(annotationId, anchorEl) {
    if (!S.slide) return;
    var existing = document.getElementById("fork-chat-" + annotationId);
    if (existing) { existing.style.display = existing.style.display === "none" ? "block" : "none"; return; }
    var wrap = document.createElement("div");
    wrap.id = "fork-chat-" + annotationId;
    wrap.className = "fork-chat";
    var head = document.createElement("div");
    head.className = "fork-chat-head";
    head.textContent = t("ai.fork.chat.head");
    var close = document.createElement("button");
    close.className = "icon-btn close";
    close.textContent = "×";
    close.addEventListener("click", function () { wrap.style.display = "none"; });
    head.appendChild(close);
    var stream = document.createElement("div");
    stream.className = "fork-chat-stream";
    stream.innerHTML = '<div class="ai-trace-empty">' + esc(t("ai.fork.stream.empty")) + "</div>";
    var inputRow = document.createElement("div");
    inputRow.className = "fork-chat-input";
    var input = document.createElement("input");
    input.type = "text";
    input.placeholder = t("ai.fork.input.ph");
    var send = document.createElement("button");
    send.className = "btn primary small";
    send.textContent = t("ai.fork.send");
    send.addEventListener("click", function () { sendForkQuestion(annotationId, input.value, stream, wrap); });
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") sendForkQuestion(annotationId, input.value, stream, wrap); });
    inputRow.appendChild(input);
    inputRow.appendChild(send);
    wrap.appendChild(head);
    wrap.appendChild(stream);
    wrap.appendChild(inputRow);
    // 挂载：优先锚点之后；无锚点则挂到 body（不再触碰平台 annoPanelList）。
    if (anchorEl && anchorEl.parentNode) {
      anchorEl.parentNode.insertBefore(wrap, anchorEl.nextSibling);
    } else {
      document.body.appendChild(wrap);
    }
    setForkState(wrap, "loading");
    restoreForkTranscript(annotationId, stream, wrap);
  }

  function restoreForkTranscript(annotationId, streamEl, wrap) {
    if (!S.slide || !annotationId || !streamEl) return;
    if (wrap) wrap._loadGen = (wrap._loadGen || 0) + 1;
    var myGen = wrap ? wrap._loadGen : 0;
    apiFetch("/api/ai/sessions?slide=" + encodeURIComponent(S.slide.name))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (wrap && wrap._loadGen !== myGen) return;
        var sessions = (data && data.sessions) || [];
        var fork = null;
        for (var i = 0; i < sessions.length; i++) {
          if (sessions[i].kind === "fork" && sessions[i].annotation_id === annotationId) { fork = sessions[i]; break; }
        }
        if (!fork) { if (wrap) setForkState(wrap, "idle"); return; }
        loadAndRenderTranscript(fork.id, streamEl, { emphasis: "fork", forkGen: myGen, forkWrap: wrap });
      })
      .catch(function () { if (wrap) setForkState(wrap, "idle"); });
  }

  function sendForkQuestion(annotationId, question, streamEl, wrapEl) {
    var st = wrapEl ? wrapEl._forkState : "idle";
    if (st === "loading" || st === "sending") return;
    if (st === "readonly") return;
    question = (question || "").trim();
    if (!question) return;
    if (!S.aiConfig || !S.aiConfig.base_url || !S.aiConfig.api_key_set) {
      toast(t("ai.need.config"), "error");
      HP.openAiPanel();
      S.els.aiConfigWrap.style.display = "block"; S.els.aiConfigCollapsed.style.display = "none";
      return;
    }
    setForkState(wrapEl, "sending");
    appendChatBubble(streamEl, "user", question);
    var input = wrapEl ? wrapEl.querySelector("input") : null;
    if (input) input.value = "";
    var ctx = forkTraceCtx(streamEl);
    setThinkingRow(ctx);
    fetch("/api/ai/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slide: S.slide.name, annotation_id: annotationId, question: question }),
      credentials: "same-origin",
    }).then(function (resp) {
      if (resp.status === 410) {
        appendStatusRow(streamEl, "error", t("ai.fork.deleted"));
        if (wrapEl) setForkState(wrapEl, "readonly");
        throw new Error("gone");
      }
      if (!resp.ok || !resp.body) {
        return resp.text().then(function (tx) {
          var msg = "";
          try { var body = JSON.parse(tx || ""); if (body && body.error) msg = String(body.error); } catch (e2) {}
          throw new Error(msg || tx || ("HTTP " + resp.status));
        });
      }
      return HP.pumpForkSse(resp.body.getReader(), streamEl, wrapEl);
    }).catch(function (e) {
      if (e && e.message === "gone") return;
      appendStatusRow(streamEl, "error", t("ai.fork.send.fail", { e: (e && e.message ? e.message : e) }));
    }).then(function () {
      if (wrapEl && wrapEl._forkState !== "readonly") setForkState(wrapEl, "idle");
    });
  }

  function forkTraceCtx(streamEl) {
    if (!streamEl._aiCtx) {
      streamEl._aiCtx = { container: streamEl, bubbleEl: null, thinkingEl: null, isFork: true, lastSeq: 0 };
    }
    streamEl._aiCtx.container = streamEl;
    return streamEl._aiCtx;
  }

  function handleForkEvent(type, p, streamEl, wrapEl) {
    var ctx = forkTraceCtx(streamEl);
    HP.handleAiEvent(type, p, ctx);
    if (type === "agent_paused" || type === "agent_finished" || type === "agent_error") {
      var waits = streamEl.querySelectorAll(".ai-chat-thinking, .ai-trace-row.fork-wait, .ai-trace-row.thinking");
      waits.forEach(function (w) { if (w.parentNode) w.parentNode.removeChild(w); });
      ctx.thinkingEl = null;
    }
  }

  // =========================================================================
  // 对话内标注卡片的 fork/branch 按钮（插件自有 DOM，直接调用，不经 HostBridge）。
  function buildAnnoAiActions(container, annotationId, style) {
    if (!annotationId || !container) return;
    var op = (style === "op");
    var forkBtn = document.createElement("button");
    forkBtn.type = "button";
    forkBtn.className = op ? "ai-op ai-fork" : "ai-action-chip ai-fork";
    forkBtn.title = tt("anno.fork.quick.tip");
    forkBtn.innerHTML = '<span class="ai-act-ic">💬</span><span class="ai-act-tx">' + esc(tt("anno.fork.quick")) + "</span>";
    forkBtn.addEventListener("click", function (e) { e.stopPropagation(); openForkChat(annotationId, container); });
    container.appendChild(forkBtn);

    var branchBtn = document.createElement("button");
    branchBtn.type = "button";
    branchBtn.className = op ? "ai-op ai-branch" : "ai-action-chip ai-branch";
    branchBtn.title = tt("anno.branch.deep.tip");
    branchBtn.innerHTML = '<span class="ai-act-ic">⑂</span><span class="ai-act-tx">' + esc(tt("anno.branch.deep")) + "</span>";
    branchBtn.addEventListener("click", function (e) { e.stopPropagation(); openBranchFromAnno(annotationId); });
    container.appendChild(branchBtn);
  }

  function attachForkBtn(row, annotationId, opts) {
    if (!annotationId) return;
    opts = opts || {};
    buildAnnoAiActions(row, annotationId, opts.style || "chip");
  }

  // 绑定 renderer 的导出（renderer.js 先于本文件加载）
  function _bind() {
    appendStatusRow = HP.appendStatusRow;
    appendMsgTs = HP.appendMsgTs;
    appendChatBubble = HP.appendChatBubble;
    setThinkingRow = HP.setThinkingRow;
    clearThinkingRow = HP.clearThinkingRow;
    renderAiTranscript = HP.renderAiTranscript;
  }

  HP.loadAndRenderTranscript = loadAndRenderTranscript;
  HP.restoreAiSession = restoreAiSession;
  HP.attachAiStream = attachAiStream;
  HP.abortActiveAiStream = abortActiveAiStream;
  HP.refreshAiSessionSwitcher = refreshAiSessionSwitcher;
  HP.renderAiSessionOptions = renderAiSessionOptions;
  HP.onAiSessionChange = onAiSessionChange;
  HP.switchAiSession = switchAiSession;
  HP.applyActiveSessionUi = applyActiveSessionUi;
  HP.startBranchRun = startBranchRun;
  HP.openBranchFromAnno = openBranchFromAnno;
  HP.handleForkEvent = handleForkEvent;
  HP.attachForkBtn = attachForkBtn;
  HP.buildAnnoAiActions = buildAnnoAiActions;
  HP.openForkChat = openForkChat;
  HP._sessionsBind = _bind;
})();
