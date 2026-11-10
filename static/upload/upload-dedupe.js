/* =========================================================================
   上传去重提示（upload-resilience 2026-11-10）——纯逻辑，无 DOM。

   生产证据：30 天内 5 个用户把同名同大小文件在完成后 2 秒–6 分钟又传了
   一遍；一次 3.5 GB 重传在完成后 2 秒开始、5 分钟后被取消；两个用户最终
   出现重复切片。本模块只回答两个问题，对话框/入队由 app.js 负责：

     1. 这个文件是否已经在**面板队列**里（非终态或刚完成的同一行）？
     2. 服务端是否已有同一 owner 的（a）已完成切片（90 天内）或
        （b）未完成上传？——GET /api/ingestions/duplicates。

   网络失败/超时（默认 3 秒）一律 **fail open**：返回「无重复」继续上传，
   绝不阻塞用户；批量添加（多选/文件夹）按同一份服务端结果给出**一个**
   合并对话框所需的数据，而不是 N 个串行对话框。

   形态：classic script（app.js 之前加载），无依赖；可 vitest 直测。
   ========================================================================= */
(function () {
  "use strict";

  var DUP_TIMEOUT_MS = 3000;          // 单次查询失败放行上限（产品口径）
  var DUP_BATCH_DEADLINE_MS = 3000;   // 整批预检的墙钟上限（多选/文件夹）
  var MAX_SERVER_CHECKS = 200;        // 超大文件夹条目上限（其余只看本地队列）
  var CHECK_CONCURRENCY = 6;          // 并发查询数（别把控制 API 打爆）

  //: 面板队列里「仍算已存在」的状态：非终态 + 刚完成（done）。
  //: 失败/取消/已移除的行不再代表一份有效上传——重复提示没有意义。
  var QUEUE_EXCLUDE = {
    failed: 1, cancelled: 1, rejected: 1, discarded: 1,
    convert_failed: 1, upload_failed: 1,
  };

  function fileOf(item) {
    return (item && item.file) ? item.file : item;
  }

  function keyOf(name, size) {
    return String(name == null ? "" : name) + "\u0000" + String(size);
  }

  function nameOf(file) {
    return (file && typeof file.name === "string") ? file.name : "";
  }

  function sizeOf(file) {
    return (file && typeof file.size === "number" && isFinite(file.size))
      ? file.size : -1;
  }

  /// 队列中同 name+size 且仍有效的行（可多条；呼叫做「已存在」判定）
  function queueMatches(entries, file) {
    var out = [];
    var name = nameOf(file);
    var size = sizeOf(file);
    if (!name || size < 0) return out;
    (entries || []).forEach(function (e) {
      if (!e || e.name !== name || Number(e.size) !== size) return;
      if (QUEUE_EXCLUDE[e.state]) return;
      out.push(e);
    });
    return out;
  }

  /// 服务端响应规范化：只接受契约字段；任何异常 → null（fail open）。
  /// {completed:{slide_id,display_name,completed_at}|null,
  ///  unfinished:{job_id,state,uploaded_parts,total_parts}|null}
  function normalizeServerBody(body) {
    if (!body || typeof body !== "object") return null;
    var completed = null;
    var c = body.completed;
    if (c && typeof c === "object" &&
        typeof c.slide_id === "string" && c.slide_id) {
      completed = {
        slide_id: c.slide_id,
        display_name: (typeof c.display_name === "string" && c.display_name)
          ? c.display_name : "",
        completed_at: (typeof c.completed_at === "number")
          ? c.completed_at : null,
      };
    }
    var unfinished = null;
    var u = body.unfinished;
    if (u && typeof u === "object" &&
        typeof u.job_id === "string" && u.job_id) {
      unfinished = {
        job_id: u.job_id,
        state: (typeof u.state === "string") ? u.state : "",
        uploaded_parts: (typeof u.uploaded_parts === "number")
          ? u.uploaded_parts : null,
        total_parts: (typeof u.total_parts === "number") ? u.total_parts : null,
      };
    }
    return (completed || unfinished)
      ? { completed: completed, unfinished: unfinished }
      : null;
  }

  function duplicatesUrl(file) {
    return "/api/ingestions/duplicates?filename=" +
      encodeURIComponent(nameOf(file)) + "&size=" + encodeURIComponent(
        String(sizeOf(file)));
  }

  /// 单文件查询。永不 reject；超时/网络/非 2xx/坏 JSON → null（放行）。
  function requestOne(apiFetch, file, opts) {
    opts = opts || {};
    var timeout = (typeof opts.timeoutMs === "number" && opts.timeoutMs >= 0)
      ? opts.timeoutMs : DUP_TIMEOUT_MS;
    var setTimerFn = (typeof opts.setTimer === "function")
      ? opts.setTimer : setTimeout;
    var ctl = (typeof AbortController === "function")
      ? new AbortController() : null;
    return new Promise(function (resolve) {
      var settled = false;
      var timer = null;
      function done(v) {
        if (settled) return;
        settled = true;
        if (timer !== null && typeof clearTimeout === "function") {
          try { clearTimeout(timer); } catch (e) { /* */ }
        }
        resolve(v);
      }
      timer = setTimerFn(function () {
        if (ctl) { try { ctl.abort(); } catch (e) { /* */ } }
        done(null);
      }, timeout);
      var p;
      try {
        p = apiFetch(duplicatesUrl(file), ctl ? { signal: ctl.signal } : undefined);
      } catch (e) {
        p = Promise.reject(e);
      }
      Promise.resolve(p).then(function (res) {
        if (!res || !res.ok) { done(null); return; }
        return Promise.resolve(res.json()).then(function (body) {
          done(normalizeServerBody(body));
        }, function () { done(null); });
      }, function () { done(null); });
    });
  }

  /// 批量查询（唯一 name+size 去重；并发 CHECK_CONCURRENCY）。
  /// 返回 { "<name>\0<size>": response|null }。apiFetch 缺失 → 空表。
  function requestDuplicates(apiFetch, items, opts) {
    opts = opts || {};
    if (typeof apiFetch !== "function") return Promise.resolve({});
    var maxChecks = (typeof opts.maxChecks === "number")
      ? opts.maxChecks : MAX_SERVER_CHECKS;
    var conc = (typeof opts.concurrency === "number")
      ? Math.max(1, opts.concurrency) : CHECK_CONCURRENCY;
    var seen = {};
    var files = [];
    (items || []).forEach(function (item) {
      var f = fileOf(item);
      if (!f || !nameOf(f) || sizeOf(f) <= 0) return;
      var k = keyOf(nameOf(f), sizeOf(f));
      if (seen[k]) return;
      seen[k] = true;
      files.push(f);
    });
    if (files.length > maxChecks) files = files.slice(0, maxChecks);
    var out = {};
    var i = 0;
    // 整批共享一个墙钟上限：多选/文件夹不会因为逐条 3 秒超时而越等越久。
    // 到点后未查的文件按「无重复」放行（fail open，绝不阻塞上传）。
    var deadline = Date.now() + ((typeof opts.deadlineMs === "number")
      ? opts.deadlineMs : DUP_BATCH_DEADLINE_MS);
    function worker() {
      if (i >= files.length) return Promise.resolve();
      var f = files[i++];
      var k = keyOf(nameOf(f), sizeOf(f));
      var left = deadline - Date.now();
      if (left <= 0) { out[k] = null; return worker(); }
      var one = Object.assign({}, opts, {
        timeoutMs: Math.min(
          (typeof opts.timeoutMs === "number") ? opts.timeoutMs
                                                : DUP_TIMEOUT_MS,
          left),
      });
      return requestOne(apiFetch, f, one).then(function (body) {
        out[k] = body;
        return worker();
      });
    }
    var lanes = [];
    for (var c = 0; c < Math.min(conc, files.length); c++) lanes.push(worker());
    return Promise.all(lanes).then(function () { return out; });
  }

  /// 合并本地队列与服务端结果，产出对话框所需的数据。
  /// items: [{file, handle?}] | File[]
  /// opts: {queueEntries, serverResults}
  /// 返回 {total, duplicates:[{index,file,name,size,kind,queueMatch,
  ///        server,completed,unfinished}]}
  ///   kind: "queue"（面板已有）| "completed"（切片库已有）|
  ///         "unfinished"（上次未完成，可续传）
  /// 队列命中优先：它比服务端更「近」，文案也不同。
  function buildPlan(items, opts) {
    opts = opts || {};
    var queueEntries = opts.queueEntries || [];
    var server = opts.serverResults || {};
    var duplicates = [];
    (items || []).forEach(function (item, index) {
      var f = fileOf(item);
      if (!f || !nameOf(f)) return;
      var q = queueMatches(queueEntries, f);
      var s = server[keyOf(nameOf(f), sizeOf(f))] || null;
      if (!q.length && !s) return;
      var completed = s ? s.completed : null;
      var unfinished = s ? s.unfinished : null;
      var kind = q.length ? "queue"
        : completed ? "completed" : "unfinished";
      duplicates.push({
        index: index,
        file: f,
        handle: (item && item.handle) ? item.handle : null,
        name: nameOf(f),
        size: sizeOf(f),
        kind: kind,
        queueMatch: q.length ? q[0] : null,
        server: s,
        completed: completed,
        unfinished: unfinished,
      });
    });
    return { total: (items || []).length, duplicates: duplicates };
  }

  window.HP_UPLOAD_DEDUPE = {
    DUP_TIMEOUT_MS: DUP_TIMEOUT_MS,
    DUP_BATCH_DEADLINE_MS: DUP_BATCH_DEADLINE_MS,
    MAX_SERVER_CHECKS: MAX_SERVER_CHECKS,
    CHECK_CONCURRENCY: CHECK_CONCURRENCY,
    QUEUE_EXCLUDE: QUEUE_EXCLUDE,
    key: keyOf,
    fileOf: fileOf,
    queueMatches: queueMatches,
    normalizeServerBody: normalizeServerBody,
    duplicatesUrl: duplicatesUrl,
    requestOne: requestOne,
    requestDuplicates: requestDuplicates,
    buildPlan: buildPlan,
  };
})();
