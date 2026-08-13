/* =========================================================================
   HistoPilot UI bundle —— SSE 帧解析与泵（fetch + ReadableStream + 手动 \n\n 分帧）

   - pumpAiSse：main/branch run 的通用 SSE 泵，解析帧 → handleSseFrame，推进 mainAiCtx.lastSeq。
   - pumpForkSse：fork 批注对话的轻量泵（session_ended 忽略）。
   切片世代隔离（slideName/epoch）+ runCtrl 配对由 main.js 的 isCurrentAiSlide/finishAiRun 保证。
   ========================================================================= */
(function () {
  "use strict";
  var HP = window.HistoPilot;
  var S = HP.s;

  // 通用 SSE 泵：解析帧 → handleAiEvent（同时推进 mainAiCtx.lastSeq）
  // slideName/epoch 可选：切片切换后丢弃过期流。
  function pumpAiSse(reader, slideName, epoch, runCtrl) {
    var decoder = new TextDecoder("utf-8");
    var buffer = "";
    function stillCurrent() {
      var slideCurrent = slideName == null || epoch == null || HP.isCurrentAiSlide(slideName, epoch);
      return slideCurrent && (!runCtrl || S.aiAbortCtrl === runCtrl);
    }
    function pump() {
      return reader.read().then(function (result) {
        if (!stillCurrent()) {
          try { reader.cancel(); } catch (e) {}
          return;
        }
        if (result.done) {
          if (stillCurrent()) HP.finishAiRun(runCtrl);
          return;
        }
        buffer += decoder.decode(result.value, { stream: true });
        var idx;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          if (!stillCurrent()) {
            try { reader.cancel(); } catch (e) {}
            return;
          }
          var frame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          handleSseFrame(frame, slideName, epoch);
        }
        return pump();
      });
    }
    return pump();
  }

  // 解析单条 SSE 帧（event:/data:/id: 行）
  function handleSseFrame(frame, slideName, epoch) {
    if (slideName != null && epoch != null && !HP.isCurrentAiSlide(slideName, epoch)) return;
    var eventType = null;
    var dataStr = "";
    var seq = null;
    var lines = frame.split("\n");
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.indexOf(":") === 0) continue; // 注释/心跳
      if (line.indexOf("id:") === 0) {
        seq = parseInt(line.slice(3).trim(), 10);
        if (!isFinite(seq)) seq = null;
      } else if (line.indexOf("event:") === 0) {
        eventType = line.slice(6).trim();
      } else if (line.indexOf("data:") === 0) {
        dataStr += line.slice(5).trim();
      }
    }
    if (!eventType) return;
    if (eventType === "session_ended") {
      // 统一收尾：finishAiRun + 刷新。agent_finished/paused/error 不得提前
      // finishAiRun（会清空 aiAbortCtrl → stillCurrent 取消流 → 永远读不到本事件）。
      // 状态落盘发生在终态事件之后、session_ended 之前，此处刷新才能拿到最终 status。
      // result.done 保留为缺少 session_ended 时的兜底收尾。
      HP.finishAiRun();
      HP.refreshAiSessionSwitcher(slideName, epoch);
      return;
    }
    if (eventType === "event_reset") {
      var rp = {};
      if (dataStr) { try { rp = JSON.parse(dataStr); } catch (e) { rp = { raw: dataStr }; } }
      if (seq != null) S.mainAiCtx.lastSeq = Math.max(S.mainAiCtx.lastSeq || 0, seq);
      HP.handleAiEventReset(rp, slideName, epoch);
      return;
    }
    var payload = {};
    if (dataStr) { try { payload = JSON.parse(dataStr); } catch (e) { payload = { raw: dataStr }; } }
    if (seq != null) S.mainAiCtx.lastSeq = Math.max(S.mainAiCtx.lastSeq || 0, seq);
    if (payload && payload.session_id) S.aiSessionId = payload.session_id;
    HP.handleAiEvent(eventType, payload);
  }

  // fork 批注对话的轻量 SSE 泵：session_ended 忽略，其余交 handleForkEvent
  function pumpForkSse(reader, streamEl, wrapEl) {
    var decoder = new TextDecoder("utf-8");
    var buffer = "";
    function pump() {
      return reader.read().then(function (result) {
        if (result.done) return;
        buffer += decoder.decode(result.value, { stream: true });
        var idx;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          var frame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          var type = null, dataStr = "";
          frame.split("\n").forEach(function (line) {
            if (line.indexOf(":") === 0) return;
            if (line.indexOf("event:") === 0) type = line.slice(6).trim();
            else if (line.indexOf("data:") === 0) dataStr += line.slice(5).trim();
          });
          if (type === "session_ended") continue;
          var payload = {};
          if (dataStr) { try { payload = JSON.parse(dataStr); } catch (e) {} }
          HP.handleForkEvent(type, payload, streamEl, wrapEl);
        }
        return pump();
      });
    }
    return pump();
  }

  HP.pumpAiSse = pumpAiSse;
  HP.pumpForkSse = pumpForkSse;
})();
