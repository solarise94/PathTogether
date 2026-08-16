/* =========================================================================
   HistoPilot UI bundle —— 轨迹/卡片/气泡渲染器（纯 DOM）

   所有 append-xxx / render-xxx 卡片只往传入的 container（主轨迹 / fork 流）写 DOM，不触碰
   平台全局 viewer/state。快照跳转原本直接 viewer.viewport.fitBounds，现改为发
   viewer.navigate 请求由平台跟随（Stage 2 验收：插件不持有 OpenSeadragon 实例）。
   ========================================================================= */
(function () {
  "use strict";
  var HP = window.HistoPilot;
  var S = HP.s;
  var t = HP.t, esc = HP.esc, fmtAiMag = HP.fmtAiMag, fmtNum = HP.fmtNum,
      truncateStr = HP.truncateStr, fmtMsgTs = HP.fmtMsgTs;

  function aiTrace() { return (S.els && S.els.aiTrace) || null; }

  function clearAiEmpty(container) {
    if (!container) return;
    var empty = container.querySelector(".ai-trace-empty");
    if (empty) empty.remove();
  }

  // 轻量 Markdown（助手气泡）：先转义再套有限语法，避免 XSS
  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function inlineMarkdown(s) {
    var md = escHtml(s);
    var codes = [];
    md = md.replace(/`([^`]+)`/g, function (_, code) {
      codes.push("<code>" + code + "</code>");
      return "\u0000C" + (codes.length - 1) + "\u0000";
    });
    md = md.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    md = md.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    md = md.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, "$1<em>$2</em>");
    md = md.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    md = md.replace(/(^|[^_])_([^_]+)_(?!_)/g, "$1<em>$2</em>");
    md = md.replace(/\u0000C(\d+)\u0000/g, function (_, i) { return codes[Number(i)] || ""; });
    return md;
  }

  function renderMarkdown(src) {
    var text = String(src == null ? "" : src).replace(/\r\n/g, "\n");
    // 空白输入返回空串：兜底 <p></p> 会叠出灰色空气泡
    if (!text.trim()) return "";
    var fences = [];
    text = text.replace(/```[\w-]*\n?([\s\S]*?)```/g, function (_, code) {
      fences.push("<pre class=\"ai-md-pre\"><code>" + escHtml(code.replace(/\n$/, "")) + "</code></pre>");
      return "\n\u0000F" + (fences.length - 1) + "\u0000\n";
    });
    var lines = text.split("\n");
    var out = [];
    var i = 0;
    var para = [];
    var inUl = false;
    var inOl = false;
    function flushPara() {
      if (!para.length) return;
      out.push("<p>" + inlineMarkdown(para.join("\n")).replace(/\n/g, "<br>") + "</p>");
      para = [];
    }
    function closeLists() {
      if (inUl) { out.push("</ul>"); inUl = false; }
      if (inOl) { out.push("</ol>"); inOl = false; }
    }
    while (i < lines.length) {
      var line = lines[i];
      var fence = line.match(/^\u0000F(\d+)\u0000$/);
      if (fence) { flushPara(); closeLists(); out.push(fences[Number(fence[1])] || ""); i += 1; continue; }
      if (/^\s*$/.test(line)) { flushPara(); closeLists(); i += 1; continue; }
      var hm = line.match(/^(#{1,3})\s+(.+)$/);
      if (hm) { flushPara(); closeLists(); out.push("<h" + hm[1].length + ">" + inlineMarkdown(hm[2]) + "</h" + hm[1].length + ">"); i += 1; continue; }
      var um = line.match(/^\s*[-*+]\s+(.+)$/);
      if (um) {
        flushPara();
        if (inOl) { out.push("</ol>"); inOl = false; }
        if (!inUl) { out.push("<ul>"); inUl = true; }
        out.push("<li>" + inlineMarkdown(um[1]) + "</li>"); i += 1; continue;
      }
      var om = line.match(/^\s*\d+\.\s+(.+)$/);
      if (om) {
        flushPara();
        if (inUl) { out.push("</ul>"); inUl = false; }
        if (!inOl) { out.push("<ol>"); inOl = true; }
        out.push("<li>" + inlineMarkdown(om[1]) + "</li>"); i += 1; continue;
      }
      var qm = line.match(/^\s*>\s?(.*)$/);
      if (qm) { flushPara(); closeLists(); out.push("<blockquote>" + inlineMarkdown(qm[1]) + "</blockquote>"); i += 1; continue; }
      closeLists();
      para.push(line);
      i += 1;
    }
    flushPara();
    closeLists();
    return out.join("") || ("<p>" + inlineMarkdown(text) + "</p>");
  }

  function setBubbleContent(el, side, text) {
    if (!el) return;
    el._rawText = text == null ? "" : String(text);
    if (side === "assistant") {
      el.classList.add("ai-md");
      // 空白文本清空内容，不走 markdown（避免空 <p> 空气泡）
      el.innerHTML = el._rawText.trim() ? renderMarkdown(el._rawText) : "";
    } else {
      el.classList.remove("ai-md");
      el.textContent = el._rawText;
    }
  }

  // 聊天气泡：side="user"（右，纯文本）/ "assistant"（左，Markdown）。
  function appendChatBubble(container, side, text) {
    // 空串 / 纯空白不建 DOM（历史重建与流式共用）
    if (!String(text == null ? "" : text).trim()) return null;
    clearAiEmpty(container);
    var prev = container.lastElementChild;
    var row = document.createElement("div");
    row.className = "ai-chat-bubble " + side;
    if (prev && prev.classList.contains("ai-chat-bubble") && prev.classList.contains(side)) {
      prev.classList.remove("tail");
      row.classList.add("grp");
    }
    row.classList.add("tail");
    setBubbleContent(row, side, text);
    container.appendChild(row);
    if (side === "assistant") markUserBubblesRead(container);
    container.scrollTop = container.scrollHeight;
    return row;
  }

  function markUserBubblesRead(container) {
    if (!container) return;
    var unread = container.querySelectorAll(".ai-chat-bubble.user:not(.read)");
    for (var i = 0; i < unread.length; i++) {
      var b = unread[i];
      b.classList.add("read");
      var r = document.createElement("div");
      r.className = "ai-read-receipt";
      r.textContent = fmtMsgTs(new Date()) + " " + t("ai.bubble.read");
      if (b.nextSibling) container.insertBefore(r, b.nextSibling);
      else container.appendChild(r);
    }
  }

  function appendMsgTs(container, ts) {
    if (!container) return null;
    clearAiEmpty(container);
    var d = ts instanceof Date ? ts
      : (typeof ts === "number" ? new Date(ts > 1e12 ? ts : ts * 1000) : new Date());
    var row = document.createElement("div");
    row.className = "ai-msg-ts";
    row.textContent = fmtMsgTs(d);
    container.appendChild(row);
    return row;
  }

  function appendReconnectStatus(container, p) {
    if (!container) return null;
    clearAiEmpty(container);
    var attempt = (p && p.attempt) || 1;
    var max = (p && p.max) || 3;
    var delay = (p && p.delay != null) ? p.delay : 2;
    var text = "reconnection " + attempt + "/" + max + " (" + delay + "s)";
    var last = container.lastElementChild;
    if (last && last.classList && last.classList.contains("ai-reconnect")) {
      last.textContent = text;
      container.scrollTop = container.scrollHeight;
      return last;
    }
    var row = document.createElement("div");
    row.className = "ai-reconnect";
    row.textContent = text;
    container.appendChild(row);
    container.scrollTop = container.scrollHeight;
    return row;
  }

  function appendStatusRow(container, cls, text) {
    if (!text) return null;
    clearAiEmpty(container);
    var row = document.createElement("div");
    row.className = "ai-status " + (cls || "info");
    row.textContent = text;
    container.appendChild(row);
    container.scrollTop = container.scrollHeight;
    return row;
  }

  function appendToolCall(container, name, detail) {
    if (!container) return null;
    clearAiEmpty(container);
    var el = document.createElement("details");
    el.className = "ai-tool-call";
    var sum = document.createElement("summary");
    sum.className = "ai-tool-summary";
    var label = document.createElement("span");
    label.className = "ai-tool-name";
    label.textContent = toolCallLabel(name);
    sum.appendChild(label);
    if (detail) {
      var brief = document.createElement("span");
      brief.className = "ai-tool-brief";
      brief.textContent = truncateStr(String(detail), 72);
      sum.appendChild(brief);
    }
    el.appendChild(sum);
    if (detail && String(detail).length > 72) {
      var body = document.createElement("div");
      body.className = "ai-tool-body";
      body.textContent = String(detail);
      el.appendChild(body);
    }
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
    return el;
  }

  function toolCallLabel(name) {
    var map = { goto: "tool.goto", snapshot: "tool.snapshot", result: "tool.result",
                context: "tool.context", compact: "tool.compact" };
    return t(map[name] || "tool.fallback", { n: (name || "tool") });
  }

  function appendProcessStep(container, text, cls) { return appendToolCall(container, cls || "tool", text); }
  function appendSysRow(container, text, sub) {
    if (sub === "tool" || sub === "snapshot") return appendToolCall(container, sub, text);
    return appendStatusRow(container, "info", text);
  }
  function appendSnapshotPlaceholder(container, imgRef) {
    return appendSnapshotCard(container, { magnification: imgRef && imgRef.magnification, bbox: (imgRef && imgRef.src) || {} });
  }

  // 快照跳转：导航矩形外扩 20%（与 jumpToAnno 一致），overlay 用原始 bbox，
  // 虚线框得以在视野内一圈而不是贴边。
  function navigateWithOverlay(bbox, mag) {
    if (!bbox || bbox.x == null || !bbox.w) return;
    HP.setOverlay([{ x: bbox.x, y: bbox.y, w: bbox.w, h: bbox.h, magnification: mag || "" }]);
    var h = bbox.h || bbox.w;
    var pad = Math.max(bbox.w, h) * 0.2;
    HP.bridge.request("viewer.navigate", {
      x: bbox.x - pad, y: bbox.y - pad, w: bbox.w + pad * 2, h: h + pad * 2,
    });
  }

  // 快照跳转：原 viewer.viewport.fitBounds → 改为 viewer.navigate 请求（平台跟随）
  function bindSnapshotJump(el, bbox, mag) {
    if (!el || !bbox || bbox.x == null || !bbox.w) return;
    el.dataset.bbox = JSON.stringify(bbox);
    el.style.cursor = "pointer";
    el.title = t("ai.attach.jump.title");
    el.addEventListener("click", function () {
      try {
        navigateWithOverlay(JSON.parse(el.dataset.bbox || "{}"), mag);
      } catch (e) {}
    });
  }

  function appendSnapshotCard(container, opts) {
    opts = opts || {};
    clearAiEmpty(container);
    var card = document.createElement("div");
    card.className = "ai-attach snapshot";
    var title = document.createElement("div");
    title.className = "ai-attach-title";
    title.textContent = t("ai.snapshot.title") + (opts.magnification != null && opts.magnification !== ""
      ? " · " + fmtAiMag(opts.magnification) : "") + t("ai.snapshot.jump");
    card.appendChild(title);
    bindSnapshotJump(card, opts.bbox || {}, opts.magnification);
    container.appendChild(card);
    container.scrollTop = container.scrollHeight;
    return card;
  }

  // 标注卡片正文折叠（正文常与紧邻观察段重复）：默认 2 行 + 右下“展开/收起”
  // 角标。展开只由角标控制（stopPropagation，不冒泡触发卡片 focus）。
  // 须在卡片已入 DOM 后调用（刷新角标要读 scrollHeight）。
  function makeClampableBody(body) {
    var hint = document.createElement("span");
    hint.className = "ai-clamp-hint";
    body.appendChild(hint);
    function refresh() {
      var expanded = body.classList.contains("expanded");
      var overflows = body.scrollHeight > body.clientHeight + 2;
      hint.style.display = (expanded || overflows) ? "" : "none";
      hint.textContent = expanded ? t("ai.attach.collapse") : t("ai.attach.expand");
    }
    hint.addEventListener("click", function (e) {
      e.stopPropagation();
      body.classList.toggle("expanded");
      refresh();
    });
    setTimeout(refresh, 0);
    return body;
  }

  function appendObservationCard(p, container) {
    var target = container || aiTrace();
    clearAiEmpty(target);
    var card = document.createElement("div");
    card.className = "ai-attach ai-mutter observation";
    var title = document.createElement("div");
    title.className = "ai-attach-title";
    title.textContent = p.label || t("ai.observation.default");
    card.appendChild(title);
    if (p.note) {
      var body = document.createElement("div");
      body.className = "ai-attach-body";
      body.textContent = p.note;
      card.appendChild(body);
    }
    var reason = p.no_annotation_reason;
    if (reason && String(reason).trim()) {
      var sub = document.createElement("div");
      sub.className = "ai-attach-sub";
      sub.textContent = t("ai.observation.no.anno", { s: reason });
      card.appendChild(sub);
    }
    target.appendChild(card);
    target.scrollTop = target.scrollHeight;
    return card;
  }

  function appendReviewCard(title, summary, reason, container) {
    var target = container || aiTrace();
    clearAiEmpty(target);
    var card = document.createElement("div");
    card.className = "ai-attach ai-mutter review";
    var head = document.createElement("div");
    head.className = "ai-attach-title";
    head.textContent = title;
    card.appendChild(head);
    if (summary) {
      var body = document.createElement("div");
      body.className = "ai-attach-body";
      body.textContent = summary;
      card.appendChild(body);
    }
    if (reason && String(reason).trim()) {
      var sub = document.createElement("div");
      sub.className = "ai-attach-sub";
      sub.textContent = t("ai.observation.reason", { s: reason });
      card.appendChild(sub);
    }
    target.appendChild(card);
    target.scrollTop = target.scrollHeight;
    return card;
  }

  function appendAnnotationCard(p, container, opts) {
    opts = opts || {};
    var target = container || aiTrace();
    clearAiEmpty(target);
    var card = document.createElement("div");
    card.className = "ai-attach annotation";
    var title = document.createElement("div");
    title.className = "ai-attach-title";
    title.textContent = p.label || t("ai.anno.default.label");
    card.appendChild(title);
    var body = null;
    if (p.note) {
      body = document.createElement("div");
      body.className = "ai-attach-body";
      body.textContent = p.note;
      card.appendChild(body);
    }
    if (p.annotation_id) card.dataset.annotationId = p.annotation_id;
    // 机器坐标只进 dataset（不进正文），annotation_id 匹配不到时供 focus 兜底定位
    if (p.x != null || p.y != null || p.side_px != null) {
      card.dataset.geo = JSON.stringify({ x: p.x, y: p.y, side_px: p.side_px });
    }
    if (opts.showFork && p.annotation_id) {
      var actions = document.createElement("div");
      actions.className = "ai-attach-actions";
      card.appendChild(actions);
      HP.attachForkBtn(actions, p.annotation_id);
    }
    // 点击卡片 = 选中/聚焦该标注（复用平台 jumpToAnno 的选中高亮），动作按钮区除外
    card.style.cursor = "pointer";
    card.title = t("ai.attach.focus.title");
    card.addEventListener("click", function (ev) {
      if (ev.target && ev.target.closest && ev.target.closest(".ai-attach-actions")) return;
      HP.bridge.request("annotation.focus", {
        annotation_id: p.annotation_id || null,
        x: p.x != null ? p.x : null,
        y: p.y != null ? p.y : null,
        side_px: p.side_px != null ? p.side_px : null,
      }).catch(function () {});
    });
    target.appendChild(card);
    if (body) makeClampableBody(body);
    target.scrollTop = target.scrollHeight;
    return card;
  }

  function parseToolArgs(argsStr) {
    if (typeof argsStr === "string") { try { return JSON.parse(argsStr) || {}; } catch (e) { return {}; } }
    if (argsStr && typeof argsStr === "object") return argsStr;
    return {};
  }

  function friendlyToolResult(text) {
    var rt = String(text || "");
    if (/(?:call_|toolu_)[A-Za-z0-9_-]{4,}/.test(rt) && /snapshot_id|pending|必须引用|不匹配/.test(rt)) {
      return t("tool.unlinked");
    }
    rt = rt.replace(/snapshot_id[（(]?\s*[:：]?\s*(?:call_|toolu_)[A-Za-z0-9_-]+[)）]?/gi, "");
    rt = rt.replace(/(?:call_|toolu_)[A-Za-z0-9_-]{4,}/g, "");
    return truncateStr(rt.trim(), 200);
  }

  // ---------- 轨迹流 DOM 辅助 ----------
  function appendTraceRow(cls, text, container) {
    var target = container || aiTrace();
    var empty = target.querySelector(".ai-trace-empty");
    if (empty) empty.remove();
    var row = document.createElement("div");
    row.className = "ai-trace-row " + cls;
    row.textContent = text;
    if (cls === "snapshot") {
      row.style.cursor = "pointer";
      row.title = t("ai.attach.jump.title");
      row.addEventListener("click", function () {
        try {
          var bb = JSON.parse(row.dataset.bbox || "{}");
          if (bb.x != null && bb.w) {
            HP.bridge.request("viewer.navigate", { x: bb.x, y: bb.y, w: bb.w, h: bb.h });
          }
        } catch (e) {}
      });
    }
    target.appendChild(row);
    target.scrollTop = target.scrollHeight;
    return row;
  }

  function userDisplayText(m) {
    if (m && m.display_text) return String(m.display_text);
    var text = messageText(m) || "";
    var taskMatch = text.match(/任务：([\s\S]*)$/);
    if (taskMatch) return taskMatch[1].trim();
    var qMatch = text.match(/用户的问题：([\s\S]*)$/);
    if (qMatch) return qMatch[1].trim();
    return text;
  }

  function isSuccessfulToolResult(name, resultText) {
    if (resultText == null || resultText === "") return false;
    var tx = String(resultText);
    if (/必须引用|不匹配|必须提供|必须是|不允许|落标注失败|未知工具|用户已取消/.test(tx)) return false;
    if (name === "mark_observation") return /已记录观察/.test(tx);
    if (name === "complete_snapshot_review") return /已关闭快照/.test(tx);
    if (name === "create_annotation") return /已落标注/.test(tx);
    return true;
  }

  function messageText(m) {
    var c = m.content;
    if (typeof c === "string") return c;
    if (Array.isArray(c)) {
      var out = [];
      for (var i = 0; i < c.length; i++) {
        var p = c[i] || {};
        if (p.type === "text" && p.text) out.push(p.text);
        else if (p.type === "image_ref" || p.type === "image_url") out.push(t("ai.image.ref"));
      }
      return out.join(" ");
    }
    return "";
  }

  function findImageRef(m) {
    var c = m.content;
    if (Array.isArray(c)) {
      for (var i = 0; i < c.length; i++) {
        var p = c[i] || {};
        if (p.type === "image_ref") return p;
      }
    }
    return null;
  }

  // 把 GET session 的脱敏 transcript 渲染成统一聊天气泡（主/fork 共用）
  function renderAiTranscript(msgs, opts) {
    var target = (opts && opts.container) || aiTrace();
    opts = opts || {};
    if (opts.emphasis !== "fork") appendMsgTs(target, opts.ts);
    var isForkRestore = (opts.emphasis === "fork");
    var toolResults = {};
    var toolResultMeta = {};
    for (var ri = 0; ri < msgs.length; ri++) {
      var rm = msgs[ri] || {};
      if (rm.role === "tool" && rm.tool_call_id) {
        toolResults[rm.tool_call_id] = messageText(rm);
        if (rm.annotation_id) toolResultMeta[rm.tool_call_id] = { annotation_id: rm.annotation_id };
      }
    }
    for (var i = 0; i < msgs.length; i++) {
      var m = msgs[i] || {};
      var role = m.role || "";
      var text = messageText(m);
      var isSystem = (role === "system") ||
                     (role === "user" && (m.spot_updated || m.spot_deleted ||
                                          /^spot_(updated|deleted)/.test(text)));
      if (isSystem) {
        if (text) appendToolCall(target, "context", truncateStr(text, 400));
        continue;
      }
      if (role === "user") {
        appendChatBubble(target, "user", userDisplayText(m));
      } else if (role === "assistant") {
        var tcs = m.tool_calls || [];
        if (text) appendChatBubble(target, "assistant", text);
        for (var j = 0; j < tcs.length; j++) {
          var tc = tcs[j] || {};
          var fn = tc.function || {};
          var nm = fn.name || "";
          var args = parseToolArgs(fn.arguments);
          var tcId = tc.id || "";
          var tcOk = isSuccessfulToolResult(nm, toolResults[tcId]);
          if (nm === "mark_observation") {
            if (!tcOk) continue;
            appendObservationCard({ label: args.label || "", note: args.note || "",
              no_annotation_reason: args.no_annotation_reason || "", bbox: {} }, target);
          } else if (nm === "complete_snapshot_review") {
            if (!tcOk) continue;
            var disp = args.disposition || "";
            appendReviewCard(
              disp === "annotated" ? t("ai.review.annotated")
              : disp === "no_annotation" ? t("ai.review.no.anno")
              : t("ai.review.done"),
              args.summary || "",
              disp === "no_annotation" ? (args.no_annotation_reason || "") : "", target);
          } else if (nm === "goto") {
            appendToolCall(target, "goto",
              "(" + fmtNum(args.x) + "," + fmtNum(args.y) + ") @ " +
              (args.level != null ? args.level : "?") + (args.reason ? " · " + args.reason : ""));
          } else if (nm === "create_annotation") {
            if (!tcOk) continue;
            var meta = toolResultMeta[tcId] || {};
            appendAnnotationCard({
              label: args.label || t("ai.anno.default.label"), note: args.note || "",
              x: args.x, y: args.y, side_px: args.side_px, annotation_id: meta.annotation_id || null,
            }, target, { showFork: !isForkRestore && !!meta.annotation_id });
          } else if (nm === "snapshot") {
            appendToolCall(target, "snapshot", t("ai.tool.snapshot"));
          } else if (nm === "finish") {
            // finish 总结已在 assistant 文本 / 状态行
          } else {
            appendToolCall(target, nm, "");
          }
        }
      } else if (role === "tool") {
        var imgRef = findImageRef(m);
        if (imgRef) {
          appendSnapshotCard(target, { magnification: imgRef.magnification, bbox: imgRef.src || {} });
        }
        var resText = messageText(m);
        if (resText && !imgRef) {
          var trimmed = String(resText).trim();
          if (/^已记录观察|^已关闭快照|^已落标注/.test(trimmed)) continue;
          appendToolCall(target, "result", friendlyToolResult(resText));
        }
      }
    }
    target.scrollTop = target.scrollHeight;
  }

  // 导出
  HP.clearAiEmpty = clearAiEmpty;
  HP.navigateWithOverlay = navigateWithOverlay;
  HP.setBubbleContent = setBubbleContent;
  HP.renderAiTranscript = renderAiTranscript;
  HP.appendChatBubble = appendChatBubble;
  HP.markUserBubblesRead = markUserBubblesRead;
  HP.appendMsgTs = appendMsgTs;
  HP.appendReconnectStatus = appendReconnectStatus;
  HP.appendStatusRow = appendStatusRow;
  HP.appendToolCall = appendToolCall;
  HP.appendSnapshotCard = appendSnapshotCard;
  HP.appendObservationCard = appendObservationCard;
  HP.appendReviewCard = appendReviewCard;
  HP.appendAnnotationCard = appendAnnotationCard;
  HP.appendTraceRow = appendTraceRow;
  HP.userDisplayText = userDisplayText;
  HP.messageText = messageText;
})();
