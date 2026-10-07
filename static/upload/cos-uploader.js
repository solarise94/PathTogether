/* =========================================================================
   COS 直传引擎（共享）—— 工作台（static/app.js，classic script）与本地切片
   工具页（/tools/slides，ES module 页面 + 严格 CSP script-src 'self'）共用。
   C4 从 app.js 原样抽出（docs/browser-slide-tools-and-baidu-plugin-agent-
   plan-20260929.md §9 C4：产物验证后复用 COS 分块上传器，不能整体物化）。

   形态约束：classic script（两种页面都以 <script src> 加载，CSP 'self' 允许；
   vitest 载入 harness 与 app.js 同一 Function realm 执行）。无 DOM、无 i18n、
   无 localStorage 直接访问——页面差异全部经注入依赖表达：

     window.HP_COS_UPLOAD.resolveConfig(payload)
        capability payload → 规范化配置 | null（缺字段/非法一律 null，
        宁可不用 COS 也不拿坏参数拼请求；并发/批量夹上界）。

     window.HP_COS_UPLOAD.contentLockName(account, file) / acquireContentLock
        跨标签内容锁（review 2026-10-07 复核：双标签并发直传同一文件各建一
        个 ingestion）。页面在「读回执/候选 → 创建 ingestion」临界区外套锁；
        拿锁后重读回执/记录再决定复用、续传或新建。

     window.HP_COS_UPLOAD.createUpload(opts) -> { cancel(), done }
        opts = {
          source:    { name, size, slice(start, end) }   // File 或 OPFS 产物视图；
                                                           // 只按分块 slice，绝不整体物化
          apiFetch:  (url, opts) => Promise<Response>     // 认证/CSRF 由调用方注入
          config:    resolveConfig(...) 的结果（进入即冻结）
          storage: {                                      // 持久化适配器（可返回 Promise）
            save(rec)          // rec = {job_id, filename, size, slide_id?, confirmed}
                               // 创建后的首次 save 失败 = 致命（不传输、取消新任务）；
                               // 其后的续传提示写入失败忽略
            complete(id, out)  // out = {succeeded:true, slide_id?} | {terminal:true, fail_code?}
            remove(id)         // 取消/放弃续传旧任务
            findResumable(src) // 同名同大小候选 | null（不提供则跳过询问分支）
            readConfirmed(id)  // 已确认分块编号数组
          },
          resumeJobId: string | null,   // 显式续传：跳过创建与询问
          skipConfirm: bool,            // 续传不询问（重试按钮语义）
          confirmResume: () => bool,    // 发现可续传任务时询问（默认 true）
          createBody: object | null,    // 阶段 1：POST /api/ingestions 附加
                                        // 字段（direct_class 声明；仅新任务
                                        // 创建时合并，续传不带）
          retryCompleteOnNetworkError: bool,  // 工具页 true：完成响应丢失→重发
                                              // complete（409 ingestion_state_conflict
                                              // 视为已完成过）；工作台 false 保持
                                              // 原行为（网络失败交给行级失败）
          useResumeEndpoint: bool,      // 工具页 true：续传时服务端已离开
                                        // uploading 且仍有未确认分块 → POST /resume
          onEvent: (ev) => void         // {type:'status', body} | {type:'progress', ...}
                                       //  | {type:'created', jobId, body}
                                       //  | {type:'retry', part, attempt}
                                       // progress（U1 字节级，frac 保持兼容）：
                                       //  {type:'progress', phase:'uploading', frac,
                                       //   loadedBytes, confirmedBytes, totalBytes,
                                       //   determinate, sentAll, force}
                                       //  - 字节事件节流（≥120ms）发；force=true
                                       //    为状态推进（分块确认/sentAll 翻转）
                                       //    需立即更新；
                                       //  - determinate=false：未收到可计算长度
                                       //    的事件 → frac/loadedBytes 退化为已
                                       //    确认字节（活动态 + 已确认字节后备）；
                                       //  - sentAll：body 已 100% 发出但 HTTP 响应
                                       //    未确认 → 显示「数据已发送，等待确认」，
                                       //    绝不显示「完成」。
        }

        done：viewable → resolve {ok:true, body}；取消 → 立即 resolve {cancelled:true}
        （进行中的等待随之结束，此后不再发控制请求）；失败 → reject {status, data} |
        {terminal, data} | {part, status, network} |
        {persist:true, ingestionId, reconciled}（记录未落盘；reconciled=服务端已确认取消）。

   请求语义（与抽出前的 app.js 逐条一致；U1 仅传输载体 fetch→XHR）：
   - 控制 API（/api/ingestions*）全部经注入的 apiFetch（CSRF/认证由它负责）；
   - COS 分块 PUT 绝不走 apiFetch——XHR、withCredentials=false、不设任何
     请求头（Content-Length 与签名绑定值一致，手动设置会被 CORS 拒绝）、
     绝不输出预签名 URL；upload.onprogress 提供字节级进度，监听先于 send
     注册，settle/取消后注销；abortController.abort() 停止全部在途 XHR；
   - 分批签名（sign_batch_max_parts，429/503 退避 ≤3 次同批重试）+ 批内并发
     （max_concurrent_parts）；单片同 URL 重试 ≤3 → 重新签名一次 → 再 ≤3；
   - upload-complete 幂等：409 ingestion_state_conflict = 已完成过，转轮询。
     review #10：完成请求网络失败 → 先查任务状态再决定（已收口→轮询；仍
     uploading→幂等重发 ≤4 次）——工作台与工具页同一语义。
   ========================================================================= */
(function () {
  "use strict";

  // ---------- capability payload → 规范化配置（D3：十进制字节整数原样使用） ----------
  function resolveConfig(payload) {
    try {
      var c = payload;
      if (!c || c.available !== true) return null;
      var nums = {
        max_size_bytes: Number(c.max_size_bytes),
        part_bytes: Number(c.part_bytes),
        url_ttl_seconds: Number(c.url_ttl_seconds),
        max_concurrent_parts: Number(c.max_concurrent_parts),
        sign_batch_max_parts: Number(c.sign_batch_max_parts),
      };
      for (var k in nums) {
        if (typeof nums[k] !== "number" || !isFinite(nums[k]) || nums[k] <= 0) {
          return null;
        }
      }
      if (!Array.isArray(c.formats) || !c.formats.length) return null;
      var fmts = [];
      for (var i = 0; i < c.formats.length; i++) {
        if (typeof c.formats[i] !== "string" || !c.formats[i]) return null;
        fmts.push(c.formats[i].toLowerCase());
      }
      return {
        max_size_bytes: nums.max_size_bytes,
        part_bytes: nums.part_bytes,
        url_ttl_seconds: nums.url_ttl_seconds,
        // 并发/批量夹到合理上界：服务端值异常大时别把浏览器与签名速率打爆
        max_concurrent_parts: Math.max(1, Math.min(16, Math.floor(nums.max_concurrent_parts))),
        sign_batch_max_parts: Math.max(1, Math.min(64, Math.floor(nums.sign_batch_max_parts))),
        formats: fmts,
      };
    } catch (e) {
      return null;
    }
  }

  // ---------- COS PUT 独立传输（U1：XHR upload.onprogress；不经 apiFetch） ----------
  // fetch 不给上传字节回调（F2）；XHR.upload.onprogress 有。CORS 合同与 fetch
  // 版逐条一致：withCredentials=false、不设任何请求头（Content-Length 由浏览
  // 器按 body 推导，手动设置会被 CORS 拒绝）、绝不输出预签名 URL。所有监听
  // 在 send 之前注册；settle（成功/失败/取消/超时）后注销，AbortController
  // 一次 abort 让全部在途 XHR 停止。
  //
  // review 2026-10-07 #8：两层超时（文档化数字，createUpload 可配置）——
  //   无进度超时 progressTimeoutMs（默认 60 000ms）：连续该时长没有上传
  //   进度事件 → abort，按**可重试**网络错误处理（err.network + timeout）；
  //   单片整体上限 partTimeoutMs（默认 600 000ms，写入 xhr.timeout）：单片
  //   无论如何不得超过该时长。计时器在 settle/取消后清理（不留活性）。
  var DEFAULT_PROGRESS_TIMEOUT_MS = 60000;
  var DEFAULT_PART_TIMEOUT_MS = 600000;

  function putPart(url, blob, abortCtl, onProgress, timeouts) {
    var progressTimeoutMs = (timeouts && timeouts.progressTimeoutMs) ||
      DEFAULT_PROGRESS_TIMEOUT_MS;
    var partTimeoutMs = (timeouts && timeouts.partTimeoutMs) ||
      DEFAULT_PART_TIMEOUT_MS;
    return new Promise(function (resolve, reject) {
      var xhr;
      try {
        xhr = new XMLHttpRequest();
      } catch (e) {
        reject({ network: true });
        return;
      }
      var settled = false;
      var onAbortSignal = null;
      var noProgressTimer = null;
      var progressTimedOut = false;
      function clearNoProgressTimer() {
        if (noProgressTimer !== null) {
          clearTimeout(noProgressTimer);
          noProgressTimer = null;
        }
      }
      function armNoProgressTimer() {
        clearNoProgressTimer();
        if (!(progressTimeoutMs > 0)) return;
        noProgressTimer = setTimeout(function () {
          // 无进度 = 请求可能悬死：主动 abort，走可重试网络错误合同
          progressTimedOut = true;
          try { xhr.abort(); } catch (e) { settle(reject, { network: true, timeout: true }); }
        }, progressTimeoutMs);
      }
      function detach() {
        clearNoProgressTimer();
        try {
          xhr.upload.onprogress = null;
          xhr.onload = null;
          xhr.onerror = null;
          xhr.onabort = null;
          xhr.ontimeout = null;
        } catch (e) { /* 老 UA 属性只读：监听随 XHR 被 GC */ }
        if (onAbortSignal && abortCtl && abortCtl.signal &&
            abortCtl.signal.removeEventListener) {
          try { abortCtl.signal.removeEventListener("abort", onAbortSignal); }
          catch (e) {}
        }
      }
      function settle(fn, arg) {
        if (settled) return;
        settled = true;
        detach();
        fn(arg);
      }
      try {
        xhr.open("PUT", url, true);
        xhr.withCredentials = false;
        if (partTimeoutMs > 0) {
          try { xhr.timeout = partTimeoutMs; } catch (e) { /* 老 UA 只读 */ }
        }
        // 监听先于 send（XHR 规范要求 upload 事件在 send 后才派发，先注册
        // 才不丢早期进度）
        xhr.upload.onprogress = function (ev) {
          if (settled) return;
          armNoProgressTimer();   // 有进度：重置无进度计时器（review #8）
          if (!onProgress) return;
          var loaded = (ev && typeof ev.loaded === "number") ? ev.loaded : 0;
          onProgress(loaded, !!(ev && ev.lengthComputable));
        };
        xhr.onload = function () {
          var etag = null;
          try { etag = xhr.getResponseHeader("ETag"); } catch (e) {}
          // upload.load/loadend 不是成功凭据：只有 HTTP 2xx 响应算成功。
          // status 0（CORS 拦截/连接层异常的怪异路径）不是 HTTP 响应——
          // fetch 时代从不以 status 0 resolve，这里同样映射进 network 合同
          if (xhr.status >= 200 && xhr.status < 300) {
            settle(resolve, { etag: etag });
          } else if (xhr.status === 0) {
            settle(reject, { network: true });
          } else {
            settle(reject, { status: xhr.status, etag: etag });
          }
        };
        xhr.onerror = function () { settle(reject, { network: true }); };
        xhr.onabort = function () {
          // 无进度超时触发的 abort = 可重试网络错误（区别于用户取消）
          if (progressTimedOut) {
            settle(reject, { network: true, timeout: true });
            return;
          }
          settle(reject, { name: "AbortError" });
        };
        xhr.ontimeout = function () { settle(reject, { network: true, timeout: true }); };
        if (abortCtl && abortCtl.signal) {
          if (abortCtl.signal.aborted) { settle(reject, { name: "AbortError" }); return; }
          onAbortSignal = function () { try { xhr.abort(); } catch (e) {} };
          abortCtl.signal.addEventListener("abort", onAbortSignal);
        }
        armNoProgressTimer();
        xhr.send(blob);
      } catch (e) {
        // 同步抛出（坏 URL / 不支持的方案等）按网络层失败进入重试合同
        settle(reject, { network: true });
      }
    });
  }

  // 服务端冻结计划 {part_number,length} → 前端切片表：编号排序后按顺序
  // 累加推导 offset（worker 按同一顺序初始化，编号连续；计划不含 offset）
  function buildPlan(parts) {
    var byNum = {};
    var nums = [];
    for (var i = 0; i < parts.length; i++) {
      byNum[parts[i].part_number] = parts[i];
      nums.push(parts[i].part_number);
    }
    nums.sort(function (a, b) { return a - b; });
    var out = [];
    var offset = 0;
    for (var j = 0; j < nums.length; j++) {
      var p = byNum[nums[j]];
      out.push({ part_number: p.part_number, offset: offset, length: p.length });
      offset += p.length;
    }
    return out;
  }

  function jsonBody(r) {
    return r.json().then(function (b) { return { ok: r.ok, status: r.status, body: b }; },
                           function () { return { ok: r.ok, status: r.status, body: null }; });
  }

  // ---------- 续传内容身份（review 2026-10-07 #1） ----------
  // 分片 SHA-256（WebCrypto，浏览器/Node ≥20 同一全局）；不可用 = 无法
  // 记录/核验内容身份 → 相关记录按 legacy 处理（绝不凭名称/大小续传）。
  function bytesHex(d) {
    var bytes = new Uint8Array(d);
    var out = "";
    for (var i = 0; i < bytes.length; i++) {
      out += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16);
    }
    return out;
  }

  function sha256Hex(buf) {
    var subtle = (typeof crypto !== "undefined" && crypto && crypto.subtle)
      ? crypto.subtle : null;
    if (!subtle) return Promise.resolve(null);
    return subtle.digest("SHA-256", buf).then(bytesHex, function () { return null; });
  }

  // 分片摘要方案（有界内存，复核第二轮：4c38ed7f 曾把整个分片读入内存既做
  // 哈希又做 PUT body——32 MB × 通道数的驻留把渲染器 RSS 顶到 ~0.8 GiB，
  // 见修复文档内存实测）。PUT body 恢复为 Blob 切片（浏览器从盘流式发送）；
  // 摘要单独按 DIGEST_CHUNK_BYTES 子块流式读取，分片摘要 =
  // SHA-256(子块摘要逐字节拼接)。磁盘读两次，同一时刻每通道只有一个子块在
  // 内存。方案随记录携带 digest_scheme：缺 scheme/其他 scheme 的记录一律按
  // legacy 处理（弃旧任务、全新上传，绝不凭名称/大小复用）。
  var DIGEST_SCHEME = "sha256-chunked-4MiB-v1";
  var DIGEST_CHUNK_BYTES = 4 * 1024 * 1024;

  function sha256HexChunked(blob) {
    var subtle = (typeof crypto !== "undefined" && crypto && crypto.subtle)
      ? crypto.subtle : null;
    if (!subtle || !blob || !(blob.size > 0)) return Promise.resolve(null);
    var acc = null;          // 子块摘要拼接（每子块 32 B）
    var offset = 0;
    function step() {
      if (offset >= blob.size) {
        if (!acc) return Promise.resolve(null);
        return subtle.digest("SHA-256", acc).then(bytesHex,
          function () { return null; });
      }
      var end = Math.min(blob.size, offset + DIGEST_CHUNK_BYTES);
      var p;
      try {
        p = blob.slice(offset, end).arrayBuffer();
      } catch (e) {
        p = Promise.reject(e);
      }
      return p.then(function (buf) {
        if (!buf || buf.byteLength !== end - offset) {
          throw new Error("short read");
        }
        return subtle.digest("SHA-256", buf);
      }).then(function (d) {
        var nd = new Uint8Array(d);
        var next = new Uint8Array((acc ? acc.length : 0) + nd.length);
        if (acc) next.set(acc, 0);
        next.set(nd, acc ? acc.length : 0);
        acc = next;
        offset = end;
        return step();
      }, function () { return null; });   // 读失败/哈希失败 → 无摘要（重传）
    }
    return step();
  }

  function createUpload(opts) {
    var cfg = opts.config;
    var apiFetch = opts.apiFetch;
    var storage = opts.storage;
    var source = opts.source;
    var emit = opts.onEvent || function () {};
    var jobId = opts.resumeJobId || null;
    var account = typeof opts.account === "string" ? opts.account : "";
    var confirmedMap = {};         // part_number -> ETag|""（ETag 仅提示）
    var digestMap = {};            // 本次会话确认的分片 -> SHA-256（review #1）
    var resumeDigests = {};        // 续传记录里已核验的旧分片摘要（review #1）
    var resumeRejected = false;    // 续传身份核验失败：本轮不再尝试续传
    var plan = null;               // [{part_number, offset, length}]
    var planByNum = {};            // part_number -> {offset, length}（字节折算）
    var totalConfirmed = 0;
    var totalBytes = 0;            // 冻结计划长度之和（必须 == source.size）
    var confirmedBytes = 0;        // 已确认唯一分块长度之和
    var activeAttempts = {};       // part_number -> {id, loaded, length}
    var attemptSeq = 0;            // 每次尝试新 ID：旧尝试的迟到回调无效
    var computableSeen = false;    // 收到过 lengthComputable 进度事件
    var sentAllSeen = false;       // 「已发送全部字节、等待确认」已提示过
    var lastProgressAt = 0;        // 字节进度节流（状态类更新不受节流）
    var PROGRESS_MIN_MS = 120;     // 计划 §2.2：DOM 更新 100–250ms
    var abortCtl = (typeof AbortController === "function") ? new AbortController() : null;
    var stopped = false;           // 用户取消后停一切后续动作
    var waits = new Set();         // 进行中的等待 {h, reject}：取消时立即以 cancelled 结束
    var settleCancelled = null;
    var cancelledOutcome = new Promise(function (r) { settleCancelled = r; });

    function delay(ms) {
      if (stopped) return Promise.reject({ cancelled: true });
      return new Promise(function (resolve, reject) {
        var w = { reject: reject };
        w.h = setTimeout(function () { waits.delete(w); resolve(); }, ms);
        waits.add(w);
      });
    }

    function stopTimers() {
      waits.forEach(function (w) {
        clearTimeout(w.h);
        w.reject({ cancelled: true });
      });
      waits.clear();
    }

    // 取消之后状态机不得再发任何控制请求（取消请求本身除外，直接用 apiFetch）
    function api(url, init) {
      if (stopped) return Promise.reject({ cancelled: true });
      return apiFetch(url, init);
    }

    // 续传提示类写入（已确认分块、slide_id）失败不致命：服务端 ListParts 才是权威
    function quiet(p) {
      if (p && typeof p.then === "function") p.then(null, function () {});
    }

    function confirmedList() {
      var out = [];
      for (var k in confirmedMap) {
        if (confirmedMap.hasOwnProperty(k)) out.push(parseInt(k, 10));
      }
      return out;
    }

    function saveRecord(extra) {
      // review #1：记录带账号绑定与分片内容摘要（已核验的续传摘要 +
      // 本次会话确认的摘要）。摘要缺失的分块按未确认语义处理（重传）。
      // plan 冻结后随记录持久化（分片编号→长度；工具页 published receipt
      // 靠它对「重新选中的文件」做内容凭证核验）。
      var digests = {};
      var k;
      for (k in resumeDigests) {
        if (resumeDigests.hasOwnProperty(k) && confirmedMap.hasOwnProperty(k)) {
          digests[k] = resumeDigests[k];
        }
      }
      for (k in digestMap) {
        if (digestMap.hasOwnProperty(k) && confirmedMap.hasOwnProperty(k)) {
          digests[k] = digestMap[k];
        }
      }
      var planSnap = null;
      if (plan) {
        planSnap = plan.map(function (p) {
          return { part_number: p.part_number, length: p.length };
        });
      }
      return storage.save(Object.assign({
        job_id: jobId, filename: source.name, size: source.size,
        account: account,
        confirmed: confirmedList(),
        digests: confirmedList().length ? digests : {},
        // 摘要方案标记：无标记/其他方案 = legacy（续传弃旧任务、全新上传）
        digest_scheme: (confirmedList().length && Object.keys(digests).length)
          ? DIGEST_SCHEME : "",
        plan: planSnap,
      }, extra || {}));
    }

    // ---------- 字节级进度聚合（计划 §2.2） ----------
    // confirmedBytes = 冻结计划中已确认的唯一分块长度之和；
    // activeBytes = 各分块最新有效尝试的 loaded 之和（逐片夹到实际长度）；
    // loadedBytes = min(totalBytes, confirmedBytes + activeBytes)。
    // 片号只记一次；重试分配新的 attempt ID，旧尝试的迟到回调无效，失败尝试
    // 的 loaded 不带入下一次尝试；PUT 成功后在同一更新里 active→confirmed。

    function activeBytesSum() {
      var s = 0;
      for (var k in activeAttempts) {
        if (activeAttempts.hasOwnProperty(k)) s += activeAttempts[k].loaded;
      }
      return s;
    }

    function dropAttempt(n, attemptId) {
      var a = activeAttempts[n];
      if (a && (attemptId === undefined || a.id === attemptId)) {
        delete activeAttempts[n];
      }
    }

    function registerAttempt(n, length) {
      attemptSeq += 1;
      activeAttempts[n] = { id: attemptSeq, loaded: 0, length: length };
      return attemptSeq;
    }

    function onPartProgress(n, attemptId, loaded, computable) {
      var a = activeAttempts[n];
      if (!a || a.id !== attemptId) return;   // 旧尝试的迟到回调无效
      if (computable) computableSeen = true;
      var clamped = Math.max(0, Math.min(
        typeof loaded === "number" ? loaded : 0, a.length));
      if (clamped <= a.loaded) return;        // 同一尝试内不回退（重投递保护）
      a.loaded = clamped;
      emitUploadProgress(false);              // 字节事件走节流
    }

    function emitUploadProgress(force) {
      if (stopped || !plan || !totalBytes) return;
      var now = Date.now();
      var active = computableSeen ? activeBytesSum() : 0;
      var loaded = Math.min(totalBytes, confirmedBytes + active);
      var sentAll = loaded >= totalBytes && confirmedBytes < totalBytes;
      // sentAll 翻转（body 发满/开始确认）是状态推进：不受节流窗约束，
      // 否则最后一拍 progress 被合并后 sentAll 可能永远发不出去
      if (force !== true && sentAll === sentAllSeen &&
          now - lastProgressAt < PROGRESS_MIN_MS) return;
      lastProgressAt = now;
      if (sentAll !== sentAllSeen) { sentAllSeen = sentAll; force = true; }
      // 扩展事件（frac 保持向后兼容；determinate=false → 调用方显示活动态 +
      // 已确认字节后备；sentAll →「数据已发送，等待确认」，绝不显示完成）。
      // determinate：见过可计算长度的进度事件，或已全部确认（最终态是确定的）
      emit({
        type: "progress", phase: "uploading", force: force === true,
        frac: loaded / totalBytes,
        loadedBytes: loaded,
        confirmedBytes: Math.min(confirmedBytes, totalBytes),
        totalBytes: totalBytes,
        determinate: computableSeen || confirmedBytes >= totalBytes,
        sentAll: sentAll,
      });
    }

    function confirmPart(n, etag, attemptId, hex) {
      // 取消后才落地的 PUT 不得把已移除的续传记录写回来
      if (stopped || confirmedMap.hasOwnProperty(n)) {
        dropAttempt(n, attemptId);
        return;
      }
      confirmedMap[n] = etag || "";
      // review #1：分片 SHA-256 与 PUT 并发计算（分片摘要 = 4 MiB 子块摘要
      // 的 SHA-256 拼接再哈希，见 sha256-chunked-4MiB-v1）——确认落地时摘要
      // 已就绪，记录写入是同步完整的。hash 失败/不可用 → 该分块无摘要
      // （续传时按未确认处理，安全重传）。
      if (hex) digestMap[n] = hex;
      totalConfirmed++;
      var len = (planByNum[n] && planByNum[n].length) || 0;
      confirmedBytes += len;
      dropAttempt(n, attemptId);   // 同一更新：active 移除、confirmed 入账
      // 分块确认 = 状态推进：立即更新（不受节流）；百分比按字节而非片数
      emitUploadProgress(true);
      quiet(saveRecord());
    }

    function loadConfirmedFromStorage() {
      // 续传起点以本地记录为准（编号即已确认；worker ListParts 才是权威，
      // 多传的分块只是同编号覆盖，绑定长度保证不越界——resume 语义）。
      // 适配器可同步（localStorage）或异步（OPFS 任务记录）——统一经
      // Promise.resolve 归一。
      return Promise.resolve(storage.readRecord
        ? storage.readRecord(jobId)
        : Promise.resolve(null).then(function () {
            // 适配器未升级：退回 readConfirmed——无摘要即 legacy（调用方
            // prepareResume 会按“有确认无凭证”放弃续传）
            return { confirmed: storage.readConfirmed(jobId) || [],
                     digests: null, account: "" };
          })
      ).then(function (rec) {
        rec = rec || {};
        (rec.confirmed || []).forEach(function (n) {
          if (!confirmedMap.hasOwnProperty(n)) {
            confirmedMap[n] = "";
            totalConfirmed++;
          }
        });
        resumeDigests = (rec.digests && typeof rec.digests === "object")
          ? rec.digests : {};
      });
    }

    // —— 续传记录的账号/凭证预检（review #1）——
    // 返回 "resume"（带已确认分块续传）| "discard"（弃旧任务、全新创建，
    // 与既有“用户拒绝续传”同一路径）| "foreign"（他号记录：绝不 offered/
    // used，也不动他号的服务端任务——本地视为无候选）。
    function prepareResumeRecord(rec) {
      if (rec.account !== undefined && rec.account !== null &&
          String(rec.account) !== account) {
        return "foreign";
      }
      var confirmed = rec.confirmed || [];
      // 有确认分块但无内容摘要，或摘要方案不符/缺失（legacy 记录——含
      // 4c38ed7f..改动前无 scheme 标记的整片哈希记录）：无法证明字节同源 →
      // 弃旧任务、全新创建（绝不凭名称/大小复用）
      if (confirmed.length && !(rec.digests && Object.keys(rec.digests).length &&
          rec.digest_scheme === DIGEST_SCHEME)) {
        return "discard";
      }
      return "resume";
    }

    function cancelJob(jobIdToCancel) {
      // 与既有“用户拒绝续传”同一弃单路径：取消服务端任务 + 清本地记录
      var cancelPrev = api("/api/ingestions/" +
        encodeURIComponent(jobIdToCancel) + "/cancel", { method: "POST" });
      return cancelPrev.then(function () {
        quiet(storage.remove(jobIdToCancel));
      }, function () {
        quiet(storage.remove(jobIdToCancel));
      });
    }

    // 计划冻结后、跳过任何分块前：逐片比对「新选中文件」的分片 SHA-256。
    //  - 不符 → {mismatch: n}（调用方弃旧任务、全新创建，绝不混用）；
    //  - 缺摘要的已确认分块 → 从跳过表移除（重传，同编号覆盖，安全）；
    //  - 全部相符 → 保留（且把这些摘要并入后续记录）。
    function verifyResumeDigests() {
      var nums = confirmedList().filter(function (n) {
        return resumeDigests.hasOwnProperty(n);
      });
      var verified = {};
      var bad = null;
      var seq = Promise.resolve();
      nums.forEach(function (n) {
        seq = seq.then(function () {
          if (bad || stopped) return null;
          var item = planByNum[n];
          if (!item) return null;   // 计划外编号：交由服务端状态裁定
          // 同一分片摘要方案流式核验（4 MiB 子块；不整片读入内存）
          return sha256HexChunked(source.slice(item.offset, item.offset + item.length))
            .then(function (hex) {
              if (!hex || hex !== resumeDigests[n]) { bad = bad || n; return; }
              verified[n] = hex;
            });
        });
      });
      return seq.then(function () {
        if (bad) return { mismatch: bad };
        // 只保留已验证分块进跳过表；缺摘要的已确认分块移出（重传，
        // 同编号覆盖安全；confirmedBytes 折算回退）
        confirmedList().forEach(function (n) {
          if (verified.hasOwnProperty(n)) return;
          delete confirmedMap[n];
          totalConfirmed--;
          confirmedBytes -= (planByNum[n] && planByNum[n].length) || 0;
        });
        resumeDigests = verified;
        return null;
      });
    }

    // 续传身份核验失败 → 弃旧任务、清状态、以全新任务重启（同引擎实例，
    // 绝不混用任何旧分块）
    function resetForFreshCreate() {
      confirmedMap = {};
      digestMap = {};
      resumeDigests = {};
      totalConfirmed = 0;
      confirmedBytes = 0;
      plan = null;
      planByNum = {};
      totalBytes = 0;
      jobId = null;
    }

    function fetchStatus() {
      return api("/api/ingestions/" + encodeURIComponent(jobId)).then(jsonBody);
    }

    // review 2026-10-07 #9：状态轮询容忍瞬时失败——429/5xx/网络层失败按
    // 退避重查（指数退避 1s/2s/4s/8s，封顶 8s；连续失败上限 5 次），超界
    // 后以 recheck 标记失败（UI 提供「重新检查状态」= 以同 job 重入状态机
    // 重新查询，绝不重建任务/重传分片）。4xx（404/401/403…）非瞬态：立即
    // 失败，不退避。
    var STATUS_FAIL_LIMIT = 5;
    var statusFails = 0;

    function transientOrThrow(status, data) {
      var transient = status === 0 || status === 429 || status >= 500;
      if (!transient) {
        statusFails = 0;
        throw { status: status, data: data };
      }
      statusFails++;
      if (statusFails > STATUS_FAIL_LIMIT) {
        statusFails = 0;
        throw { status: status, data: data, recheck: true,
                network: status === 0 };
      }
      var backoff = Math.min(1000 * Math.pow(2, statusFails - 1), 8000);
      return delay(backoff).then(drive);
    }

    // 服务端冻结计划落位：长度索引 + totalBytes 一致性校验 + 续传已确认字节
    // 折算。mismatch=true 时调用方必须拒绝上传（不签名、不 PUT、不发事件）。
    function freezePlan(parts) {
      var built = buildPlan(parts);
      var byNum = {};
      var sum = 0;
      for (var i = 0; i < built.length; i++) {
        byNum[built[i].part_number] = built[i];
        sum += built[i].length;
      }
      if (!(typeof source.size === "number") ||
          !isFinite(source.size) || source.size <= 0 || source.size !== sum) {
        return { mismatch: true, sum: sum, size: source.size };
      }
      planByNum = byNum;
      totalBytes = sum;
      confirmedBytes = 0;
      for (var k in confirmedMap) {
        if (confirmedMap.hasOwnProperty(k) && byNum[k]) {
          confirmedBytes += byNum[k].length;
        }
      }
      return { mismatch: false, plan: built };
    }

    function drive() {
      // 统一状态机：waiting_space(5s 轮询) → uploading(拿计划传分块) →
      // upload-complete → 服务端阶段(2s 轮询) → viewable/terminal
      return fetchStatus().then(function (res) {
        if (stopped) throw { cancelled: true };
        if (!res.ok) return transientOrThrow(res.status, res.body);
        statusFails = 0;
        var b = res.body || {};
        // status 响应出现 slide_id（随 slide 出现）即回填本地记录
        if (b.slide_id && jobId) quiet(saveRecord({ slide_id: b.slide_id }));
        var st = b.stage;
        if (st === "waiting_space") {
          emit({ type: "status", body: b });
          return delay(5000).then(drive);   // 等待期间可取消
        }
        if (st === "uploading") {
          if (b.parts && b.parts.length) {
            // 冻结计划时校验 totalBytes == source.size == 计划长度之和：
            // 不一致即拒绝（typed error），绝不制造虚假百分比
            var frozen = freezePlan(b.parts);
            if (frozen.mismatch) {
              throw {
                status: 0, planMismatch: true,
                data: {
                  code: "plan_size_mismatch",
                  error: "upload part plan does not match source size",
                  plan_bytes: frozen.sum, declared_bytes: frozen.size,
                },
              };
            }
            plan = frozen.plan;
            // review #1：跳过任何分块之前，先逐片比对「新选中文件」的
            // 分片摘要；不符 → 弃旧任务、全新创建（绝不混用）
            return verifyResumeDigests().then(function (bad) {
              if (bad) throw { __restartResume: true, mismatchPart: bad.mismatch };
              return uploadPendingParts();
            });
          }
          // preparing：worker 尚未初始化 multipart（无分块计划）→ 短间隔再查
          emit({ type: "progress", frac: 0 });
          return delay(2000).then(drive);
        }
        if (st === "awaiting_server" || st === "downloading" ||
            st === "validating" || st === "processing" ||
            st === "readiness" || st === "viewable") {
          if (st === "viewable") return succeed(b);
          emit({ type: "status", body: b });
          return delay(2000).then(drive);
        }
        if (st === "terminal") {
          // 等待超时/过期/失败：终态任务不再恢复
          quiet(storage.complete(jobId, { terminal: true, fail_code: b.fail_code }));
          throw { terminal: true, data: b };
        }
        return delay(2000).then(drive);   // 未知 stage：以服务端为准再查
      }, function (err) {
        if (err && err.cancelled) throw err;
        // 网络层失败（fetch TypeError / api 拒绝）：与 5xx 同一退避合同
        return transientOrThrow(0, null);
      });
    }

    function signBatch(parts) {
      // 按 sign_batch_max_parts 分批申请绑定长度的 UploadPart URL（A 合同
      // 唯一授权接口）；429/503 退避重试同一批（同 uploadId 续签幂等）
      var attempt = 0;
      function go() {
        attempt++;
        return api("/api/ingestions/" + encodeURIComponent(jobId) + "/parts/sign", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            part_numbers: parts.map(function (p) { return p.part_number; }),
          }),
        }).then(jsonBody).then(function (res) {
          if (res.ok && res.body && Array.isArray(res.body.urls)) {
            var byNum = {};
            res.body.urls.forEach(function (u) { byNum[u.part_number] = u; });
            return parts.map(function (p) {
              return { part: p, url: byNum[p.part_number] && byNum[p.part_number].url };
            });
          }
          if ((res.status === 429 || res.status === 503) && attempt < 4) {
            return delay(3000).then(go);
          }
          throw { status: res.status, data: res.body };
        });
      }
      return go();
    }

    function putPartRobust(item, freshUrl) {
      // 单片容错：同 URL 重试 ≤3 → 重新签名一次（短 TTL URL 可能过期/损坏）
      // → 新 URL 再试 ≤3 → 仍失败抛给行级失败（confirmed 保留，可续传重试）。
      // 每次尝试分配新的 attempt ID：旧尝试的迟到进度回调无效；失败尝试的
      // loaded 在进入重试/重新签名前丢弃（暂态百分比可回退，显示「正在重试」）。
      var url = freshUrl || item.url;
      var attempt = 0;
      var timeouts = {
        progressTimeoutMs: opts.progressTimeoutMs,
        partTimeoutMs: opts.partTimeoutMs,
      };
      function go() {
        if (stopped) return Promise.reject({ cancelled: true });
        attempt++;
        if (!url) return Promise.reject({ status: 0, data: null });
        // 重新签名分支递归出的新闭包 attempt 从 1 重新计数，但它是真实的
        // 重试（新 URL）——同样发 retry 事件，消费者显示「正在重试」
        if (attempt > 1 || freshUrl) {
          emit({ type: "retry", part: item.part.part_number, attempt: attempt });
        }
        // attempt ID 必须绑定在本次 go() 调用内：闭包若读外层可变量，
        // 重试后旧尝试的迟到回调会拿到新 ID 而绕过失效判定
        var attemptId = registerAttempt(item.part.part_number, item.part.length);
        // PUT body = Blob 切片（浏览器从盘流式发送，不整片进内存——复核第
        // 二轮：整片读入曾把渲染器 RSS 顶到 ~0.8 GiB）。摘要与 PUT 并发：
        // 磁盘读两次；内存同一时刻每通道只有一个 4 MiB 子块
        // （sha256-chunked-4MiB-v1）。哈希失败/不可用 → 该分块无摘要
        // （续传时按未确认处理，安全重传；绝不退回按名称复用）。
        return Promise.all([
          sha256HexChunked(
            source.slice(item.part.offset, item.part.offset + item.part.length)),
          putPart(url,
                  source.slice(item.part.offset, item.part.offset + item.part.length),
                  abortCtl, function (loaded, computable) {
            onPartProgress(item.part.part_number, attemptId, loaded, computable);
          }, timeouts),
        ]).then(function (arr) {
          confirmPart(item.part.part_number, arr[1].etag, attemptId, arr[0]);
        })
          .catch(function (err) {
            var retried = stopped || (err && err.name === "AbortError");
            dropAttempt(item.part.part_number, attemptId);
            if (!retried) {
              // 失败尝试的字节立即出账并强制重画：暂态百分比如实回退
              emitUploadProgress(true);
            }
            if (retried) {
              return Promise.reject({ cancelled: true });
            }
            if (attempt < 3) return delay(600).then(go);
            if (!freshUrl) {
              return signBatch([item.part]).then(function (signed) {
                return putPartRobust(item, signed[0] && signed[0].url);
              });
            }
            // XHR onerror/ontimeout → err.network=true（fetch 时代靠
            // TypeError；XHR 网络错误没有 status，映射进同一 network 合同）
            throw { part: item.part.part_number, status: err && err.status,
                    network: !!(err && err.network) };
          });
      }
      return go();
    }

    function uploadPendingParts() {
      // pending = 计划编号 − 已确认（服务端计划是权威；本地 confirmed 只用于
      // 跳过，误判多传的分块会被同编号覆盖且长度受签名约束）
      // review 2026-10-07 #7：某个分片终局失败 → 所有 lane 停止调度新分片，
      // 等在途请求收口（drain：在途分片自然完成或失败，confirmed 照常入账）
      // 之后才 surface 错误——done reject 时不再有任何在途请求，用户重试
      // 不会与旧请求重叠。
      var pending = plan.filter(function (p) {
        return !confirmedMap.hasOwnProperty(p.part_number);
      });
      var i = 0;
      var failure = null;   // 首个终局失败（后续失败只留第一个）
      function nextBatch() {
        if (stopped) return Promise.reject({ cancelled: true });
        if (failure) return Promise.resolve();   // 已有终局失败：不再签新批
        var batch = pending.slice(i, i + cfg.sign_batch_max_parts);
        i += batch.length;
        if (!batch.length) return requestComplete();
        return signBatch(batch).then(function (signed) {
          // 批内并发 max_concurrent_parts（默认 3）：签名批与并发解耦
          var conc = cfg.max_concurrent_parts || 3;
          var next = 0;
          function lane() {
            if (stopped) return Promise.reject({ cancelled: true });
            if (failure || next >= signed.length) return Promise.resolve();
            var item = signed[next++];
            return putPartRobust(item).then(lane, function (e) {
              if (!failure && !stopped && !(e && (e.cancelled ||
                  e.name === "AbortError"))) {
                failure = e;
              }
              return lane();   // drain：本 lane 收口后退出
            });
          }
          var lanes = [];
          for (var k = 0; k < Math.min(conc, signed.length); k++) lanes.push(lane());
          return Promise.all(lanes).then(function () {
            // 全部在途收口后才 surface（绝不带着悬置请求把错误抛给调用方）
            if (failure) throw failure;
            return nextBatch();
          });
        });
      }
      if (!pending.length) return requestComplete();
      // 起点（含续传恢复）：按已确认字节重画，不拿历史最大值冒充已确认传输
      emitUploadProgress(true);
      return nextBatch();
    }

    function requestComplete() {
      // 全部 confirmed → 幂等记录「浏览器侧完成」；409 状态冲突视为已完成过
      //（服务端状态是唯一权威，直接转入阶段轮询）。重复回放（complete 后
      // 状态仍停在 uploading，如 worker 尚未处理完成请求）做限速重放，避免
      // 无延时的热循环打爆控制 API。
      // review 2026-10-07 #10：完成请求**网络层失败 ≠ 失败**（请求可能已达
      // 服务端、只是响应丢失）——先重查任务状态再决定：
      //   已离开 uploading（completing/awaiting_server/…/viewable）→ 直接
      //   进入轮询收口（不重发）；仍在 uploading → 幂等重发 complete（409
      //   冲突 = 已完成过），有界（≤4 次）。工作台与工具页同一语义（状态查
      //   不到时按仍 uploading 处理，同样有界）。
      var attempts = 0;
      var COMPLETE_LIMIT = 4;
      function post() {
        attempts++;
        return api("/api/ingestions/" + encodeURIComponent(jobId) +
                   "/upload-complete", { method: "POST" })
          .then(jsonBody, function (netErr) {
            if (netErr && netErr.cancelled) throw netErr;
            if (attempts >= COMPLETE_LIMIT) {
              throw { network: true, status: 0, data: null };
            }
            return delay(1500).then(function () {
              if (stopped) throw { cancelled: true };
              return fetchStatus().then(function (res) {
                var st = res.ok && res.body && res.body.stage;
                if (st && st !== "uploading") {
                  return null;   // 已在收口/已完成 → 直接进入轮询
                }
                return post();   // 仍 uploading（或状态不可得）→ 幂等重发
              }, function () {
                return post();
              });
            });
          });
      }
      function send() {
        return post().then(function (res) {
          if (res === null) return drive();
          if (res.ok || (res.status === 409 && res.body &&
                         res.body.code === "ingestion_state_conflict")) {
            return drive();
          }
          throw { status: res.status, data: res.body };
        });
      }
      if (attempts > 0) return delay(1500).then(send);
      return send();
    }

    function succeed(b) {
      quiet(storage.complete(jobId, { succeeded: true, slide_id: b.slide_id || null }));
      return { ok: true, body: b };
    }

    // 创建后的任务记录没能落盘：本次不得再发送任何东西；刚建的服务端任务
    // 立即请求取消，结果（是否确认取消）随错误交给调用方，由它决定能否再建。
    function abandonUnsaved(orphan, cause) {
      return apiFetch("/api/ingestions/" + encodeURIComponent(orphan) + "/cancel",
                      { method: "POST" })
        .then(jsonBody)
        .then(function (res) {
          return res.ok || res.status === 404 || res.status === 409;
        }, function () { return false; })
        .then(function (reconciled) {
          throw { persist: true, ingestionId: orphan, reconciled: reconciled,
                  cause: cause && (cause.message || cause.name || String(cause)) };
        });
    }

    // —— 取消：结束所有等待 + abort 在途 PUT + 清记录 + POST cancel（幂等）；
    //    done 立即以 cancelled 收口（不等悬置的控制请求） ——
    function cancel() {
      if (stopped) return;
      stopped = true;
      settleCancelled({ cancelled: true });
      stopTimers();
      if (abortCtl) { try { abortCtl.abort(); } catch (e) {} }
      if (jobId) {
        quiet(storage.remove(jobId));
        // 网络失败也照常停 UI：服务端等待超时/容量调度器会兜底清理
        apiFetch("/api/ingestions/" + encodeURIComponent(jobId) + "/cancel",
                 { method: "POST" }).catch(function () {});
      }
    }

    // 创建新任务（持久化先行；落盘失败 = 弃单，见 abandonUnsaved）
    function createNewJob() {
      // 创建体：基础字段 + 调用方附加（direct_class 声明等；仅新任务——
      // 续传/重试走既有 job id，不重发声明）
      var createBody = Object.assign({
        filename: source.name, declared_size: source.size,
      }, (opts.createBody && typeof opts.createBody === "object")
        ? opts.createBody : {});
      return api("/api/ingestions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(createBody),
      }).then(jsonBody).then(function (res) {
        if (res.ok && res.body && res.body.job_id) {
          jobId = res.body.job_id;
          // 任何请求之前先持久化任务记录；异步适配器（OPFS）返回 Promise，
          // 等它落盘——否则此刻刷新会留下无记录的服务端任务，再点就建第二个。
          // 落盘失败（含同步抛出）= 致命：不传输，取消刚建的服务端任务。
          return Promise.resolve().then(function () { return saveRecord(); }).then(function () {
            emit({ type: "created", jobId: jobId, body: res.body });
            // 创建响应自带初始阶段（waiting_capacity/preparing）：先照实展示
            if (res.body.stage) emit({ type: "status", body: res.body });
            return null;
          }, function (cause) {
            return abandonUnsaved(jobId, cause);
          });
        }
        throw { status: res.status, data: res.body };   // 422/409 → 稳定码映射
      });
    }

    // —— 续传准备（review #1：账号绑定 + 内容凭证预检）——
    // 显式 resumeJobId：读完整记录，账号不符 = 他号任务（绝不 offered/
    // used，也不动它）；有确认分块但无摘要 = legacy（弃旧任务、全新创建，
    // 与既有“用户拒绝续传”同一路径）；否则带已确认分块续传（内容比对在
    // 计划冻结后逐片进行，见 verifyResumeDigests）。
    function readResumeRecord(id) {
      return Promise.resolve(storage.readRecord
        ? storage.readRecord(id)
        : { confirmed: storage.readConfirmed(id) || [], digests: null,
            account: "" });
    }

    function prepareResume() {
      if (resumeRejected) return Promise.resolve(null);
      if (jobId) {
        return readResumeRecord(jobId).then(function (rec) {
          if (rec) {
            var verdict = prepareResumeRecord(rec);
            if (verdict === "foreign") {
              jobId = null;          // 本地视为无候选；他号服务端任务不动
              return null;
            }
            if (verdict === "discard") {
              return cancelJob(jobId).then(function () {
                resetForFreshCreate();
                resumeRejected = true;
                return null;
              });
            }
          }
          return loadConfirmedFromStorage().then(function () {
            if (!opts.useResumeEndpoint) return null;
            // 工具页：服务端已离开 uploading 而任务未收口（如卡在
            // completing）按 /resume 语义拉回 uploading（服务端拒绝即忽略，
            // 交由后续轮询判定）
            return fetchStatus().then(function (res) {
              var st = res.ok && res.body && res.body.stage;
              if (st && st !== "uploading" && st !== "viewable" && st !== "terminal") {
                return api("/api/ingestions/" + encodeURIComponent(jobId) + "/resume",
                  { method: "POST" }).then(jsonBody).catch(function () { return null; });
              }
              return null;
            });
          });
        });
      }
      // 同名同大小未完任务 → 询问后续传；用户拒绝 = 换新任务语义，
      // 先取消旧任务再全新创建（不双占、不静默复用）。
      // review #1：候选记录必须绑定同一账号（他号记录绝不 offered/used）；
      // legacy 候选（有确认分块、无摘要）按弃单处理，绝不凭名称/大小复用。
      var prev = storage.findResumable ? storage.findResumable(source) : null;
      if (!prev) return Promise.resolve(null);
      if (prev.account !== undefined && prev.account !== null &&
          String(prev.account) !== account) {
        return Promise.resolve(null);
      }
      var doResume = opts.skipConfirm ||
        (opts.confirmResume ? opts.confirmResume() : true);
      if (!doResume) return cancelJob(prev.job_id);
      var verdict = prepareResumeRecord(prev);
      if (verdict === "discard") {
        return cancelJob(prev.job_id).then(function () {
          resumeRejected = true;
          return null;
        });
      }
      jobId = prev.job_id;
      return loadConfirmedFromStorage();
    }

    var main = Promise.resolve().then(function () {
      return prepareResume();
    }).then(function () {
      if (jobId) return null;
      return createNewJob();
    }).then(function () {
      return drive();
    }).then(null, function (err) {
      // review #1：续传内容身份核验失败 → 弃旧任务（同弃单路径）、清状态、
      // 以全新任务重启（同引擎实例；绝不混用任何旧分块）
      if (err && err.__restartResume && jobId && !resumeRejected) {
        var stale = jobId;
        resumeRejected = true;
        return cancelJob(stale).then(function () {
          resetForFreshCreate();
          return prepareResume().then(function () {
            if (jobId) throw err;   // 防御：不应发生（resumeRejected 已置位）
            return createNewJob().then(drive);
          });
        });
      }
      if (stopped || (err && err.cancelled)) return { cancelled: true };
      // 网络层失败统一打标（适配器只看 network 标志，不做跨 realm instanceof）
      if (err instanceof TypeError) throw { network: true, status: 0, data: null };
      throw err;
    });
    var done = Promise.race([main, cancelledOutcome]);

    return {
      cancel: cancel,
      done: done,
    };
  }

  // ---------- 跨标签内容锁（review 2026-10-07 复核：双标签并发直传） ----
  // 键 = 账号 + 文件身份（name/size/lastModified）：同一磁盘文件在两个标签
  // 的 File 对象三元组一致 → 同一把锁；不同文件键碰撞只会互相串行（可接受，
  // 绝不误判为同一文件）。与产物上传的 uploadLockName(jobId) 同一原语
  // （navigator.locks）。锁不可用（旧浏览器/非安全上下文）时 acquire 返回
  // held=false，调用方按既有行为继续（无跨标签互斥、绝不阻塞——与产物上传
  // 对 Web Lock 的同一前提）。
  function contentLockName(account, file) {
    return "pt:upload:content:" + String(account || "") + ":" +
      String(file && file.name) + ":" + String(file && file.size) + ":" +
      String(file && file.lastModified);
  }

  // acquireContentLock(name, opts) -> Promise<{ held, release() }>
  //   held=false：navigator.locks 不可用——照常执行临界区（退化行为）。
  //   立即可得 → {held:true}，锁已持有，直到 release()（临界区 finally 调）。
  //   被其他标签占用 → onWaiting() 回调一次（页面提示「另一标签正在上传」）
  //   后排队等待，拿到锁才 resolve；等待可经 opts.signal 取消（reject
  //   AbortError，此时锁未被持有）。
  function acquireContentLock(name, opts) {
    opts = opts || {};
    var locks = (typeof navigator !== "undefined" && navigator.locks &&
                 typeof navigator.locks.request === "function")
      ? navigator.locks : null;
    if (!locks) return Promise.resolve({ held: false, release: function () {} });
    // release 是稳定的包装：holdUntilRelease() 每个持有期重新绑定真正的
    // resolve——grant 时捕获到的 release 必须能释放之后才建立的持有。
    var releaseHold = null;
    var release = function () {
      if (releaseHold) {
        var r = releaseHold;
        releaseHold = null;
        r();
      }
    };
    function holdUntilRelease() {
      releaseHold = null;
      return new Promise(function (r) { releaseHold = r; });
    }
    return new Promise(function (resolve, reject) {
      var settled = false;
      function grant(got) {
        if (settled) return;
        if (got) {
          settled = true;
          resolve({ held: true, release: release });
          return;
        }
        // 被其他标签占用：先提示（页面显示「另一标签正在上传」）再排队等待
        if (opts.onWaiting) {
          try { opts.onWaiting(); } catch (e) { /* 提示失败不阻塞等待 */ }
        }
        var queued = true;
        locks.request(name, { signal: opts.signal || undefined }, function (lock) {
          if (!lock) return;
          queued = false;
          if (!settled) { settled = true; resolve({ held: true, release: release }); }
          return holdUntilRelease();   // 持有直到 release()
        }).catch(function (e) {
          // 等待期被取消（AbortError）等：锁未被持有，把原因交给调用方
          if (queued && !settled) { settled = true; reject(e); }
        });
      }
      // 立即探测不带 signal（规范禁止 signal + ifAvailable 组合；探测本就
      // 不排队等待，无需取消）。等待排队的请求才带 signal（等待可取消）。
      locks.request(name, { ifAvailable: true },
        function (lock) {
          if (!lock) { grant(false); return; }
          grant(true);
          return holdUntilRelease();   // 持有直到 release()
        }).catch(function (e) {
          if (!settled) { settled = true; reject(e); }
        });
    });
  }

  window.HP_COS_UPLOAD = {
    resolveConfig: resolveConfig,
    createUpload: createUpload,
    buildPlan: buildPlan,
    putPart: putPart,
    contentLockName: contentLockName,
    acquireContentLock: acquireContentLock,
    // 分片摘要方案（published receipt 的内容凭证核验与引擎同一实现）
    digestScheme: DIGEST_SCHEME,
    sha256HexChunked: sha256HexChunked,
  };
})();
