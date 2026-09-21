/* =========================================================================
   P3 人工读片行为采集模块（docs/agent-plan-20260921-registration-consent-
   research.md §6.3/§7.1/§7.2/§7.3）。

   装配边界（§7.1）：
   - 本文件是**独立采集模块**，只由正式登录态 app（static/app.js 在 official
     模式且服务端 capabilities.research_collection 开启时）显式 create+attach；
     viewer-core.js 不内置采集——demo、公开分享、管理员预览页不加载本模块
     也不发起任何研究网络请求。
   - 全量关闭研究采集（capabilities.research_collection=false）时没有该网络
     请求；用户级授权/资源/撤回判定以服务端为唯一权威（403 → 本页停采）。

   事件语义（§7.1/§7.2）：
   - 用底层真实输入（滚轮/触控/拖拽/按钮/快捷键/双击）建立短期 gesture
     context，由 animation-finish 等完成回调**归并**成一条事件；连续滚轮按
     250ms 静默归一，拖动按 pointerup/cancel 完成，键盘/按钮按一次交互完成。
   - 缩放引发中心变化仍是一条 zoom 事件（changed_center=true），不再重复产出
     pan。OSD 自动 fit、加载时居中、resize、跟随 agent、程序回放没有 gesture
     context，不算人工动作。
   - observe_pause：人工打开/移动/缩放完成后，页面可见、窗口聚焦、切片
     ready、无加载遮罩/手势/弹窗/绘制中的视野稳定 **2 秒**记一次（evidence=
     inferred_stable_view）。2 秒只是客户端分类阈值；每个稳定周期最多一次；
     离开标签页/失焦/加载失败/阻塞均取消，下一次有效人工交互才开启新周期。
   - **不上传任何时长字段**（duration_ms/dwell_ms/起止时间）；事件以 seq 保留
     先后关系。坐标是 level-0 视野转 [0,1] 的四位小数归一化范围；image zoom
     ratio 是屏幕像素/level-0 像素之比，不是物理倍数。研究事件不含任意文本、
     标注原文、患者信息、鼠标轨迹、逐键内容或截图。
   - 不把研究事件持久化到 localStorage/IndexedDB（§6.3-2）；撤回/登出/切切片
     时清除内存缓冲（旧数据不贴到新授权上）。

   传输（§7.3）：
   - POST /api/research/viewing-sessions（切切片时建会话，服务端绑定主体/
     epoch）；POST /api/research/viewer-events 批次 ≤50 条 / 64 KiB，内存缓冲
     最大 200 条（超限丢最旧观测事件）；发送失败不影响读片；403/409 停采。
   - 建会话与上传回包都按会话代次（sessionReqId）校验：A 切片在途请求的
     回包在切到 B 后迟到 → 丢弃（成功不删 B 的缓冲、403/409 不停 B 的采集）；
     成功回包按实际发出的事件 ID 确认删除，不误删在途期间新入队的事件。
   ========================================================================= */
(function (root) {
  "use strict";

  var SCHEMA_VERSION = "research-viewer-events-v1";
  var BATCH_MAX_EVENTS = 50;
  var BATCH_MAX_BYTES = 64 * 1024;
  var BUFFER_MAX_EVENTS = 200;
  var WHEEL_SILENCE_MS = 250;   // 连续滚轮/触控缩放静默归一阈值（仅客户端）
  var OBSERVE_STABLE_MS = 2000; // 稳定观察阈值（仅客户端分类，不是时长字段）
  var FLUSH_INTERVAL_MS = 15000;
  var ZOOM_EPS = 1e-4;
  var CENTER_EPS = 1e-4;
  var INPUT_KINDS = ["wheel", "pinch", "drag", "keyboard", "button", "dblclick"];

  function round4(v) { return Math.round(v * 10000) / 10000; }

  function csrfTokenFromCookie(doc) {
    try {
      var m = (doc.cookie || "").match(/(?:^|;\s*)csrf_token=([^;]+)/);
      return m ? m[1] : null;
    } catch (e) { return null; }
  }

  function defaultFetch() {
    return root.fetch.apply(root, arguments);
  }

  function create(options) {
    options = options || {};
    var fetchImpl = options.fetchImpl || defaultFetch;
    var doc = options.document || root.document;
    var win = options.window || root;
    var randomId = options.randomId || function () {
      var s = "";
      var alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
      for (var i = 0; i < 12; i++) {
        s += alphabet.charAt(Math.floor(Math.random() * alphabet.length));
      }
      return s;
    };
    var endpoints = options.endpoints || {
      sessions: "/api/research/viewing-sessions",
      events: "/api/research/viewer-events",
    };

    // ---- 运行态 ----
    var viewer = null;
    var attached = false;
    var enabled = true;         // 页面级停采（403/409/withdraw/logout）
    var session = null;         // {id, epoch, slide}（服务端创建后才有）
    var creatingSession = false;
    var sessionReqId = 0;       // 会话代次：切切片/清态/新建会话时自增，作废
                                // 在途建会话请求与事件上传回包
    var slideSpec = null;       // {slide, width, height}
    var ready = false;          // viewer open 且 source 可读
    var buffer = [];
    var seq = 0;
    var droppedCount = 0;       // 粗粒度丢弃计数（仅本地诊断，不上传）
    var flushing = false;
    var retryTimer = null;
    var flushTimer = null;

    // gesture context（§7.1：底层真实输入建立，animation-finish 归并）
    var gesture = null;         // {inputKind, kind, before, released, animDone,
                                //  lastInputAt}；animDone=drag 动画先于松手结束，
                                //  改由 canvas-release 完成归并
    var wheelTimer = null;
    var sawAnimation = false;

    // observe_pause（§7.2）
    var observeArmed = false;
    var observeTimer = null;
    var busy = {};              // {drawing: bool, loading: bool, dialog: bool, ...}

    var annoLocalIds = {};      // 业务标注 id → 匿名局部标注 id（会话内稳定）

    // ---- 工具 ----
    function pageVisible() {
      try {
        return !doc.hidden && (doc.visibilityState
          ? doc.visibilityState === "visible" : true);
      } catch (e) { return false; }
    }
    function windowFocused() {
      try { return typeof doc.hasFocus === "function" ? doc.hasFocus() : true; }
      catch (e) { return false; }
    }
    function anyBusy() {
      for (var k in busy) { if (busy[k]) return true; }
      return false;
    }
    function anyGesture() { return gesture !== null; }

    // level-0 视野 → [0,1] 四位小数归一化 bbox（边界裁剪；非有限值拒绝）
    function snapshot() {
      try {
        var vp = viewer.viewport;
        if (!vp || !viewer.source) return null;
        var dims = viewer.source.dimensions;
        if (!(dims.x > 0) || !(dims.y > 0)) return null;
        var b = vp.getBounds(true);
        var ir = vp.viewportToImageRectangle(b);
        var out = [];
        var vals = [ir.x / dims.x, ir.y / dims.y,
                    ir.width / dims.x, ir.height / dims.y];
        for (var i = 0; i < 4; i++) {
          var v = vals[i];
          if (!isFinite(v)) return null;
          if (v < 0) v = 0;
          if (v > 1) v = 1;
          out.push(round4(v));
        }
        if (!(out[2] > 0) || !(out[3] > 0)) return null;
        // image zoom ratio：屏幕像素 / level-0 像素（不是物理倍数；MPP 缺失
        // 不伪造物理倍率——本字段与 MPP 无关）
        var zoom = (vp.getZoom(true) * vp.getContainerSize().x) / dims.x;
        if (!isFinite(zoom) || zoom <= 0) return null;
        var c = vp.getCenter();
        if (!isFinite(c.x) || !isFinite(c.y)) return null;
        return { bbox: out, zoom: round4(zoom), center: { x: c.x, y: c.y } };
      } catch (e) { return null; }
    }

    // ---- 事件入队 / 发送 ----
    function queueEvent(action, payload) {
      if (!enabled || !session) return;
      seq += 1;
      buffer.push({
        event_id: "evt_" + randomId(),
        seq: seq,
        action: action,
        schema_version: SCHEMA_VERSION,
        payload: payload,
      });
      if (buffer.length > BUFFER_MAX_EVENTS) {
        // 观测事件可丢（§7.3）：超限丢最旧，不阻塞主业务
        buffer.shift();
        droppedCount += 1;
      }
      if (buffer.length >= BATCH_MAX_EVENTS) flush();
    }

    function encodeBatch(events) {
      return JSON.stringify({
        viewing_session_id: session.id,
        consent_epoch: session.epoch,
        events: events,
      });
    }

    function flush() {
      if (!enabled || flushing || !session || buffer.length === 0) return;
      flushing = true;
      sendNext()
        .catch(function () { /* 失败不影响读片；事件留在有界缓冲 */ })
        .then(function () { flushing = false; });
    }

    function sendNext() {
      if (!enabled || !session || buffer.length === 0) {
        return Promise.resolve();
      }
      var count = Math.min(BATCH_MAX_EVENTS, buffer.length);
      var body = encodeBatch(buffer.slice(0, count));
      while (body.length > BATCH_MAX_BYTES && count > 1) {
        count = Math.max(1, Math.floor(count / 2));
        body = encodeBatch(buffer.slice(0, count));
      }
      if (body.length > BATCH_MAX_BYTES) {
        // 单事件超限（理论不可达）：丢该事件，继续
        buffer.shift();
        droppedCount += 1;
        return sendNext();
      }
      var sent = buffer.slice(0, count);
      // 上传回包按会话代次保护：发送时捕获当前代次（sessionReqId 在切切片/
      // 清态/新建会话时自增）。A 切片上传在途时切到 B，A 的回包迟到 → 代次
      // 已变 → 整个回包丢弃：成功不得 splice 当前（B）的 buffer，A 的
      // 403/409/404 不得 hardStop B 的采集（B 的授权由 B 自己的请求判定）。
      var reqGen = sessionReqId;
      var sentIds = {};
      for (var i = 0; i < sent.length; i++) sentIds[sent[i].event_id] = true;
      var headers = { "Content-Type": "application/json" };
      var tok = csrfTokenFromCookie(doc);
      if (tok) headers["X-CSRF-Token"] = tok;
      return fetchImpl(endpoints.events, {
        method: "POST",
        headers: headers,
        body: body,
        credentials: "same-origin",
      }).then(function (resp) {
        if (reqGen !== sessionReqId) return null; // 旧代次回包：丢弃
        if (resp.ok) {
          // 成功按"实际发送的事件 ID"确认删除：仅当代次一致时移除这批确实
          // 发出的事件。不用盲目 splice(0,count)——在途期间同代次可能追加了
          // 新事件、或 200 上限丢最旧移动了队头，按 id 删除才不会误删待发事件
          buffer = buffer.filter(function (ev) {
            return !sentIds[ev.event_id];
          });
          return sendNext();
        }
        if (resp.status === 403 || resp.status === 409 || resp.status === 404) {
          // 未授权/epoch 失效/会话失效：停采并清缓冲（旧数据不贴到新授权）
          hardStop();
          return null;
        }
        // 429/5xx/其它：保留事件待下次发送（有界缓冲兜底）
        return null;
      });
    }

    function schedulePeriodicFlush() {
      if (flushTimer !== null) return;
      flushTimer = win.setTimeout(function () {
        flushTimer = null;
        flush();
        if (enabled) schedulePeriodicFlush();
      }, FLUSH_INTERVAL_MS);
    }

    // ---- gesture context（§7.1） ----
    function beginGesture(inputKind, kind) {
      if (!enabled || !session) return;
      cancelObserve();
      // 同类输入的连续手势（滚轮连击/触控连续捏合）**延续**同一 gesture：
      // before 快照保持手势最初状态（一次连续人工交互 = 一条事件），只刷新
      // 最后输入时刻（250ms 静默窗口重置）。
      if (gesture && gesture.inputKind === inputKind
          && (inputKind === "wheel" || inputKind === "pinch")) {
        gesture.lastInputAt = Date.now();
        if (wheelTimer !== null) win.clearTimeout(wheelTimer);
        wheelTimer = win.setTimeout(onWheelSilence, WHEEL_SILENCE_MS);
        return;
      }
      var snap = snapshot();
      if (!snap) return;
      // 新手势覆盖未完成的旧手势（旧手势的 viewport 状态被新输入继续改变，
      // 归并语义 = 一次连续人工交互）
      gesture = {
        inputKind: inputKind,
        kind: kind,
        before: snap,
        released: false,
        lastInputAt: Date.now(),
      };
      sawAnimation = false;
      if (inputKind === "wheel" || inputKind === "pinch") {
        if (wheelTimer !== null) win.clearTimeout(wheelTimer);
        wheelTimer = win.setTimeout(onWheelSilence, WHEEL_SILENCE_MS);
      }
    }

    function touchGesture() {
      if (gesture) gesture.lastInputAt = Date.now();
    }

    function onWheelSilence() {
      wheelTimer = null;
      if (!gesture) return;
      if (gesture.inputKind !== "wheel" && gesture.inputKind !== "pinch") return;
      if (Date.now() - gesture.lastInputAt < WHEEL_SILENCE_MS) return;
      // 静默到期：有动画在跑 → 等 animation-finish；没有动画（如已到缩放
      // 边界没有引发动画）→ 直接归并
      if (!sawAnimation) finalizeGesture();
    }

    function finalizeGesture() {
      var g = gesture;
      gesture = null;
      if (wheelTimer !== null) { win.clearTimeout(wheelTimer); wheelTimer = null; }
      if (!g || !enabled || !session) return;
      var after = snapshot();
      if (!after) return;
      var zoomDelta = after.zoom - g.before.zoom;
      var centerMoved =
        Math.abs(after.center.x - g.before.center.x) > CENTER_EPS ||
        Math.abs(after.center.y - g.before.center.y) > CENTER_EPS;
      var emitted = false;
      if (Math.abs(zoomDelta) > ZOOM_EPS) {
        // 缩放一条（§7.1）：中心变化只置 changed_center，不重复产出 pan
        queueEvent(zoomDelta > 0 ? "zoom_in" : "zoom_out", {
          bbox_before: g.before.bbox,
          bbox_after: after.bbox,
          image_zoom_ratio: after.zoom,
          input_kind: g.inputKind,
          changed_center: centerMoved,
        });
        emitted = true;
      } else if (centerMoved) {
        queueEvent("pan", {
          bbox_before: g.before.bbox,
          bbox_after: after.bbox,
          input_kind: g.inputKind,
        });
        emitted = true;
      }
      // 无可见变化（点击未拖动/约束钳回原位）：不产出事件
      if (emitted) armObserve();
    }

    // ---- observe_pause（§7.2：动作不是时长） ----
    function armObserve() {
      cancelObserve();
      if (!enabled || !session || !ready || anyGesture() || anyBusy()) return;
      observeArmed = true;
      observeTimer = win.setTimeout(function () {
        observeTimer = null;
        var ok = observeArmed && enabled && session && ready
          && !anyGesture() && !anyBusy()
          && pageVisible() && windowFocused();
        observeArmed = false;
        if (!ok) return; // 周期被打断：下一次有效人工交互才开启新周期
        var snap = snapshot();
        if (!snap) return;
        queueEvent("observe_pause", {
          bbox: snap.bbox,
          image_zoom_ratio: snap.zoom,
          evidence: "inferred_stable_view",
        });
        // 每个稳定周期最多一次：保持静止不再记（armed 保持 false）
      }, OBSERVE_STABLE_MS);
    }

    function cancelObserve() {
      observeArmed = false;
      if (observeTimer !== null) {
        win.clearTimeout(observeTimer);
        observeTimer = null;
      }
    }

    // ---- 会话生命周期（§7.3） ----
    function clearSessionState() {
      // 作废在途建会话请求：旧切片的回包不得建立会话、也不得触发本页停采
      // （快速切切片时 A 的回包晚到，不能把 A 的会话/403 贴到当前切片 B 上）
      sessionReqId += 1;
      creatingSession = false;
      cancelObserve();
      gesture = null;
      if (wheelTimer !== null) { win.clearTimeout(wheelTimer); wheelTimer = null; }
      buffer = [];
      seq = 0;
      session = null;
      annoLocalIds = {};
    }

    function hardStop() {
      enabled = false;
      clearSessionState();
      if (flushTimer !== null) { win.clearTimeout(flushTimer); flushTimer = null; }
      if (retryTimer !== null) { win.clearTimeout(retryTimer); retryTimer = null; }
    }

    function createSession() {
      if (!enabled || creatingSession || !slideSpec) return;
      creatingSession = true;
      sessionReqId += 1;
      var reqId = sessionReqId;    // 本次请求标识：切切片作废旧请求
      var slide = slideSpec.slide; // 冻结发起时的切片：回包只允许绑定回该切片
      var headers = { "Content-Type": "application/json" };
      var tok = csrfTokenFromCookie(doc);
      if (tok) headers["X-CSRF-Token"] = tok;
      fetchImpl(endpoints.sessions, {
        method: "POST",
        headers: headers,
        body: JSON.stringify({ slide: slide }),
        credentials: "same-origin",
      }).then(function (resp) {
        // 请求已作废（期间切了切片）：丢弃回包——不建会话、也不因旧切片的
        // 403 停掉当前切片的采集（B 的权限检查由 B 自己的请求承担）
        if (reqId !== sessionReqId) return null;
        creatingSession = false;
        if (!resp.ok) {
          // 未授权/资源不属于本人/开关关闭：本切片停采（换切片再试）
          enabled = false;
          return null;
        }
        return resp.json();
      }).then(function (body) {
        if (reqId !== sessionReqId) return; // json 解包期间又切了切片：丢弃
        if (!body || !body.viewing_session_id) return;
        if (!slideSpec || slideSpec.slide !== slide) return; // 冻结切片≠当前切片：丢弃
        session = {
          id: body.viewing_session_id,
          epoch: body.consent_epoch,
          slide: slide,
        };
        seq = 0;
        buffer = [];
        schedulePeriodicFlush();
        // 人工打开切片（§7.2：完成一次人工打开后开始稳定观察检测）
        if (ready) armObserve();
      }).catch(function () {
        if (reqId === sessionReqId) creatingSession = false;
        // 网络失败：不重试建会话（观测事件本就允许缺失）；下次换切片再试
      });
    }

    // ---- OSD 绑定（采集只挂在本模块，viewer-core.js 保持无采集） ----
    function attach(v) {
      if (attached || !v) return;
      viewer = v;
      attached = true;
      v.addHandler("canvas-scroll", function () {
        beginGesture("wheel", "zoom");
      });
      v.addHandler("canvas-pinch", function () {
        beginGesture("pinch", "zoom");
      });
      v.addHandler("canvas-double-click", function () {
        beginGesture("dblclick", "zoom");
      });
      v.addHandler("canvas-press", function () {
        beginGesture("drag", "pan");
      });
      v.addHandler("canvas-drag", function () {
        if (gesture && gesture.inputKind === "drag") touchGesture();
      });
      v.addHandler("canvas-release", function () {
        if (!gesture) return;
        // 拖动-稍停-松手：动画已先于松手结束（animDone），不会再有
        // animation-finish 来归并——松开时立即归并，否则该手势永不
        // finalize（漏记 pan 及后续 observe_pause）。归并一次即清
        // gesture，后续 animation-finish 不会重复产出。
        if (gesture.inputKind === "drag" && gesture.animDone) {
          finalizeGesture();
          return;
        }
        gesture.released = true;
      });
      v.addHandler("animation-start", function () {
        if (!gesture) return;
        sawAnimation = true;
        // drag 在"动画已结束待松手"期间又启动新动画：回到等 animation-finish
        if (gesture.inputKind === "drag") gesture.animDone = false;
      });
      v.addHandler("animation-finish", function () {
        if (!gesture) return;
        if (gesture.inputKind === "drag" && !gesture.released) {
          // 动画先于松手结束：不在此归并，标记后等 canvas-release 归并
          //（同一手势同一时间只归并一次）
          gesture.animDone = true;
          return;
        }
        if ((gesture.inputKind === "wheel" || gesture.inputKind === "pinch")
            && Date.now() - gesture.lastInputAt < WHEEL_SILENCE_MS) {
          return; // 连续滚轮尚未静默：归并到同一条（§7.1）
        }
        finalizeGesture();
      });
      v.addHandler("open", function () {
        ready = true;
        if (enabled && session) armObserve();
      });
      v.addHandler("close", function () {
        ready = false;
        cancelObserve();
      });
      v.addHandler("open-failed", function () {
        // 切片加载失败：取消检测、不开新周期（§7.2）
        ready = false;
        cancelObserve();
      });
      v.addHandler("resize", function () {
        // resize 不算人工动作（§7.1）：只取消在途检测
        cancelObserve();
      });
      // 页面可见性 / 焦点：离开即取消；回来不自动开新周期（§7.2）
      if (doc.addEventListener) {
        doc.addEventListener("visibilitychange", function () {
          if (doc.hidden) cancelObserve();
        });
      }
      if (root.addEventListener) {
        root.addEventListener("blur", function () { cancelObserve(); });
      }
    }

    // ---- 对外 API ----
    var api = {
      attach: attach,

      // 切换切片：清旧缓冲（旧数据不贴到新授权），为新切片建研究会话
      startSlide: function (spec) {
        if (!spec || !spec.slide) return;
        if (session && session.slide === spec.slide && enabled) return;
        clearSessionState();
        slideSpec = {
          slide: String(spec.slide),
          width: Number(spec.width) || 0,
          height: Number(spec.height) || 0,
        };
        if (!enabled) return; // 页面级停采后不自动复活（重载页面才重新评估）
        createSession();
      },

      endSlide: function () {
        clearSessionState();
        slideSpec = null;
      },

      // 缩放按钮/快捷键（app.js 在 zoomIn/zoomOut 调用；一次交互一条）
      notifyToolInteraction: function (detail) {
        detail = detail || {};
        var inputKind = INPUT_KINDS.indexOf(detail.inputKind) >= 0
          ? detail.inputKind : "button";
        beginGesture(inputKind, "zoom");
      },

      // 标注业务写入成功后由 app.js 调用（失败不产事件；§7.1）
      notifyAnnotation: function (detail) {
        if (!enabled || !session || !detail) return;
        var action = detail.action;
        var shapeType = detail.shapeType;
        if (["annotation_create", "annotation_update"].indexOf(action) >= 0) {
          if (["rect", "arrow", "freehand"].indexOf(shapeType) < 0) return;
          var bbox = normalizeGeom(detail.geom);
          if (!bbox) return; // 几何不可归一化 → 丢弃该观测事件
          queueEvent(action, {
            tool_type: shapeType,
            shape_type: shapeType,
            bbox: bbox,
            annotation_local_id: localAnnoId(detail.annotationId),
            origin: "human",
          });
          return;
        }
        if (["annotation_delete", "annotation_accept", "annotation_reject"]
            .indexOf(action) >= 0) {
          queueEvent(action, {
            annotation_local_id: localAnnoId(detail.annotationId),
            origin: action.indexOf("accept") >= 0
              || action.indexOf("reject") >= 0 ? "human_review" : "human",
          });
          return;
        }
      },

      // app.js 汇报阻塞态（绘制中/加载遮罩/弹窗）：取消并阻止稳定观察检测
      setBusy: function (kind, on) {
        busy[kind] = !!on;
        if (anyBusy()) cancelObserve();
      },

      // 撤回/登出/切用户：停监听、清内存队列（§6.3-1/§7.3）
      stop: hardStop,

      flush: flush,

      isCollecting: function () {
        return !!(enabled && session);
      },

      // 测试/诊断（不进生产依赖）
      _state: function () {
        return {
          enabled: enabled,
          session: session,
          ready: ready,
          buffered: buffer.length,
          droppedCount: droppedCount,
          seq: seq,
          observeArmed: observeArmed,
          gesture: gesture ? {
            inputKind: gesture.inputKind,
            kind: gesture.kind,
          } : null,
          busy: shallowCopyBusy(),
        };
      },
    };

    function shallowCopyBusy() {
      var out = {};
      for (var k in busy) out[k] = busy[k];
      return out;
    }

    // 业务标注 id → 会话内稳定的匿名局部标注 id（绝不上传业务标注 ID）
    function localAnnoId(annotationId) {
      var key = (annotationId == null || annotationId === "")
        ? "_anon_" + randomId() : String(annotationId);
      if (!annoLocalIds[key]) {
        annoLocalIds[key] = "al_" + randomId().slice(0, 10);
      }
      return annoLocalIds[key];
    }

    // 标注几何（level-0 像素）→ [0,1] 归一化 bbox（缺切片尺寸/非有限 → null）
    function normalizeGeom(geom) {
      if (!geom || !slideSpec || !(slideSpec.width > 0)
          || !(slideSpec.height > 0)) return null;
      var x = null, y = null, w = null, h = null;
      if (geom.w != null && geom.h != null) {
        x = Number(geom.x); y = Number(geom.y);
        w = Number(geom.w); h = Number(geom.h);
      } else if (geom.x1 != null && geom.y1 != null
                 && geom.x2 != null && geom.y2 != null) {
        x = Math.min(geom.x1, geom.x2); y = Math.min(geom.y1, geom.y2);
        w = Math.abs(geom.x2 - geom.x1); h = Math.abs(geom.y2 - geom.y1);
      } else {
        return null;
      }
      var vals = [x / slideSpec.width, y / slideSpec.height,
                  w / slideSpec.width, h / slideSpec.height];
      var out = [];
      for (var i = 0; i < 4; i++) {
        var v = vals[i];
        if (!isFinite(v)) return null;
        if (v < 0) v = 0;
        if (v > 1) v = 1;
        out.push(round4(v));
      }
      if (!(out[2] > 0) || !(out[3] > 0)) return null;
      return out;
    }

    return api;
  }

  root.HP_ResearchTelemetry = {
    create: create,
    SCHEMA_VERSION: SCHEMA_VERSION,
    BATCH_MAX_EVENTS: BATCH_MAX_EVENTS,
    BUFFER_MAX_EVENTS: BUFFER_MAX_EVENTS,
    OBSERVE_STABLE_MS: OBSERVE_STABLE_MS,
    WHEEL_SILENCE_MS: WHEEL_SILENCE_MS,
  };
})(typeof window !== "undefined" ? window : this);
