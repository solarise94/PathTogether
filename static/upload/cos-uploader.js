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

     window.HP_COS_UPLOAD.createUpload(opts) -> { cancel(), done }
        opts = {
          source:    { name, size, slice(start, end) }   // File 或 OPFS 产物视图；
                                                           // 只按分块 slice，绝不整体物化
          apiFetch:  (url, opts) => Promise<Response>     // 认证/CSRF 由调用方注入
          config:    resolveConfig(...) 的结果（进入即冻结）
          storage: {                                      // 持久化适配器
            save(rec)          // rec = {job_id, filename, size, slide_id?, confirmed}
            complete(id, out)  // out = {succeeded:true, slide_id?} | {terminal:true, fail_code?}
            remove(id)         // 取消/放弃续传旧任务
            findResumable(src) // 同名同大小候选 | null（不提供则跳过询问分支）
            readConfirmed(id)  // 已确认分块编号数组
          },
          resumeJobId: string | null,   // 显式续传：跳过创建与询问
          skipConfirm: bool,            // 续传不询问（重试按钮语义）
          confirmResume: () => bool,    // 发现可续传任务时询问（默认 true）
          retryCompleteOnNetworkError: bool,  // 工具页 true：完成响应丢失→重发
                                              // complete（409 ingestion_state_conflict
                                              // 视为已完成过）；工作台 false 保持
                                              // 原行为（网络失败交给行级失败）
          useResumeEndpoint: bool,      // 工具页 true：续传时服务端已离开
                                        // uploading 且仍有未确认分块 → POST /resume
          onEvent: (ev) => void         // {type:'status', body} | {type:'progress', frac}
                                       //  | {type:'created', jobId, body}
        }

        done：viewable → resolve {ok:true, body}；取消 → resolve {cancelled:true}；
        失败 → reject {status, data} | {terminal, data} | {part, status, network}。

   请求语义（与抽出前的 app.js 逐条一致）：
   - 控制 API（/api/ingestions*）全部经注入的 apiFetch（CSRF/认证由它负责）；
   - COS 分块 PUT 绝不走 apiFetch——mode:"cors"、credentials:"omit"、不设
     请求头（Content-Length 与签名绑定值一致，手动设置会被 CORS 拒绝）；
   - 分批签名（sign_batch_max_parts，429/503 退避 ≤3 次同批重试）+ 批内并发
     （max_concurrent_parts）；单片同 URL 重试 ≤3 → 重新签名一次 → 再 ≤3；
   - upload-complete 幂等：409 ingestion_state_conflict = 已完成过，转轮询；
     重复回放限速（1.5s）防热循环。
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

  // ---------- COS PUT 独立传输（Phase 4-2 合同：不经 apiFetch） ----------
  function putPart(url, blob, abortCtl) {
    return fetch(url, {
      method: "PUT",
      body: blob,
      mode: "cors",
      credentials: "omit",
      signal: abortCtl ? abortCtl.signal : undefined,
    }).then(function (resp) {
      var etag = null;
      try {
        etag = (resp.headers && resp.headers.get) ? resp.headers.get("ETag") : null;
      } catch (e) { /* ETag 读不到不影响成功判定（仅提示） */ }
      if (!resp.ok) throw { status: resp.status, etag: etag };
      return { etag: etag };
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

  function createUpload(opts) {
    var cfg = opts.config;
    var apiFetch = opts.apiFetch;
    var storage = opts.storage;
    var source = opts.source;
    var emit = opts.onEvent || function () {};
    var jobId = opts.resumeJobId || null;
    var confirmedMap = {};         // part_number -> ETag|""（ETag 仅提示）
    var plan = null;               // [{part_number, offset, length}]
    var totalConfirmed = 0;
    var abortCtl = (typeof AbortController === "function") ? new AbortController() : null;
    var stopped = false;           // 用户取消后停一切后续动作
    var timers = new Set();        // 可 clearTimeout 的等待（取消后不再推进状态机）
    var completePosts = 0;         // upload-complete 回放计数（限速热循环）

    function delay(ms) {
      return new Promise(function (resolve) {
        var h = setTimeout(function () { timers.delete(h); resolve(); }, ms);
        timers.add(h);
      });
    }

    function stopTimers() {
      timers.forEach(function (h) { clearTimeout(h); });
      timers.clear();
    }

    function confirmedList() {
      var out = [];
      for (var k in confirmedMap) {
        if (confirmedMap.hasOwnProperty(k)) out.push(parseInt(k, 10));
      }
      return out;
    }

    function saveRecord(extra) {
      return storage.save(Object.assign({
        job_id: jobId, filename: source.name, size: source.size,
        confirmed: confirmedList(),
      }, extra || {}));
    }

    function confirmPart(n, etag) {
      // 取消后才落地的 PUT 不得把已移除的续传记录写回来
      if (stopped || confirmedMap.hasOwnProperty(n)) return;
      confirmedMap[n] = etag || "";
      totalConfirmed++;
      // 进度 = 已确认分块/总块数：只代表上传阶段，重试不重复计数
      if (plan && plan.length) {
        emit({ type: "progress", frac: totalConfirmed / plan.length });
      }
      saveRecord();
    }

    function loadConfirmedFromStorage() {
      // 续传起点以本地记录为准（编号即已确认；worker ListParts 才是权威，
      // 多传的分块只是同编号覆盖，绑定长度保证不越界——resume 语义）。
      // 适配器可同步（localStorage）或异步（OPFS 任务记录）——统一经
      // Promise.resolve 归一。
      return Promise.resolve(storage.readConfirmed(jobId) || []).then(function (saved) {
        (saved || []).forEach(function (n) {
          if (!confirmedMap.hasOwnProperty(n)) {
            confirmedMap[n] = "";
            totalConfirmed++;
          }
        });
      });
    }

    function fetchStatus() {
      return apiFetch("/api/ingestions/" + encodeURIComponent(jobId)).then(jsonBody);
    }

    function drive() {
      // 统一状态机：waiting_space(5s 轮询) → uploading(拿计划传分块) →
      // upload-complete → 服务端阶段(2s 轮询) → viewable/terminal
      return fetchStatus().then(function (res) {
        if (stopped) throw { cancelled: true };
        if (!res.ok) throw { status: res.status, data: res.body };
        var b = res.body || {};
        // status 响应出现 slide_id（随 slide 出现）即回填本地记录
        if (b.slide_id && jobId) saveRecord({ slide_id: b.slide_id });
        var st = b.stage;
        if (st === "waiting_space") {
          emit({ type: "status", body: b });
          return delay(5000).then(drive);   // 等待期间可取消
        }
        if (st === "uploading") {
          if (b.parts && b.parts.length) {
            plan = buildPlan(b.parts);
            return uploadPendingParts();
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
          storage.complete(jobId, { terminal: true, fail_code: b.fail_code });
          throw { terminal: true, data: b };
        }
        return delay(2000).then(drive);   // 未知 stage：以服务端为准再查
      });
    }

    function signBatch(parts) {
      // 按 sign_batch_max_parts 分批申请绑定长度的 UploadPart URL（A 合同
      // 唯一授权接口）；429/503 退避重试同一批（同 uploadId 续签幂等）
      var attempt = 0;
      function go() {
        attempt++;
        return apiFetch("/api/ingestions/" + encodeURIComponent(jobId) + "/parts/sign", {
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
      // → 新 URL 再试 ≤3 → 仍失败抛给行级失败（confirmed 保留，可续传重试）
      var url = freshUrl || item.url;
      var attempt = 0;
      function go() {
        if (stopped) return Promise.reject({ cancelled: true });
        attempt++;
        if (!url) return Promise.reject({ status: 0, data: null });
        return putPart(url, source.slice(item.part.offset,
                                         item.part.offset + item.part.length), abortCtl)
          .then(function (r) { confirmPart(item.part.part_number, r.etag); })
          .catch(function (err) {
            if (stopped || (err && err.name === "AbortError")) {
              return Promise.reject({ cancelled: true });
            }
            if (attempt < 3) return delay(600).then(go);
            if (!freshUrl) {
              return signBatch([item.part]).then(function (signed) {
                return putPartRobust(item, signed[0] && signed[0].url);
              });
            }
            throw { part: item.part.part_number, status: err && err.status,
                    network: err instanceof TypeError };
          });
      }
      return go();
    }

    function uploadPendingParts() {
      // pending = 计划编号 − 已确认（服务端计划是权威；本地 confirmed 只用于
      // 跳过，误判多传的分块会被同编号覆盖且长度受签名约束）
      var pending = plan.filter(function (p) {
        return !confirmedMap.hasOwnProperty(p.part_number);
      });
      var i = 0;
      function nextBatch() {
        if (stopped) return Promise.reject({ cancelled: true });
        var batch = pending.slice(i, i + cfg.sign_batch_max_parts);
        i += batch.length;
        if (!batch.length) return requestComplete();
        return signBatch(batch).then(function (signed) {
          // 批内并发 max_concurrent_parts（默认 3）：签名批与并发解耦
          var conc = cfg.max_concurrent_parts || 3;
          var next = 0;
          function lane() {
            if (next >= signed.length) return Promise.resolve();
            var item = signed[next++];
            return putPartRobust(item).then(lane);
          }
          var lanes = [];
          for (var k = 0; k < Math.min(conc, signed.length); k++) lanes.push(lane());
          return Promise.all(lanes).then(nextBatch);
        });
      }
      if (!pending.length) return requestComplete();
      emit({ type: "progress", frac: totalConfirmed / plan.length });
      return nextBatch();
    }

    function requestComplete() {
      // 全部 confirmed → 幂等记录「浏览器侧完成」；409 状态冲突视为已完成过
      //（服务端状态是唯一权威，直接转入阶段轮询）。重复回放（complete 后
      // 状态仍停在 uploading，如 worker 尚未处理完成请求）做限速重放，避免
      // 无延时的热循环打爆控制 API。
      // retryCompleteOnNetworkError（工具页）：完成请求已到达服务端但响应
      // 丢失（网络层失败）→ 重发 complete——同任务幂等，409 冲突即已完成过。
      // 注意 post()/send() 分层：网络重试的递归只回归 {ok,status,body}，
      // 响应判定与 drive() 只在 send() 里发生一次（递归里再跑响应链会把
      // drive 的结果当响应再判一遍——双重轮询）。
      function post() {
        completePosts++;
        return apiFetch("/api/ingestions/" + encodeURIComponent(jobId) +
                        "/upload-complete", { method: "POST" })
          .then(jsonBody, function (netErr) {
            if (opts.retryCompleteOnNetworkError && completePosts < 4) {
              return delay(1500).then(post);
            }
            throw netErr;
          });
      }
      function send() {
        return post().then(function (res) {
          if (res.ok || (res.status === 409 && res.body &&
                         res.body.code === "ingestion_state_conflict")) {
            return drive();
          }
          throw { status: res.status, data: res.body };
        });
      }
      if (completePosts > 0) return delay(1500).then(send);
      return send();
    }

    function succeed(b) {
      storage.complete(jobId, { succeeded: true, slide_id: b.slide_id || null });
      return { ok: true, body: b };
    }

    // —— 取消：停轮询 + abort 在途 PUT + 清记录 + POST cancel（幂等） ——
    function cancel() {
      if (stopped) return;
      stopped = true;
      stopTimers();
      if (abortCtl) { try { abortCtl.abort(); } catch (e) {} }
      if (jobId) {
        storage.remove(jobId);
        // 网络失败也照常停 UI：服务端等待超时/容量调度器会兜底清理
        apiFetch("/api/ingestions/" + encodeURIComponent(jobId) + "/cancel",
                 { method: "POST" }).catch(function () {});
      }
    }

    var done = Promise.resolve().then(function () {
      if (jobId) {
        // 显式续传（重试/继续按钮）：先读本地已确认分块；useResumeEndpoint
        //（工具页）时先核对服务端状态——已离开 uploading 而任务未收口（如
        // 卡在 completing）按 /resume 语义拉回 uploading（服务端拒绝即忽略，
        // 交由后续轮询判定）
        return loadConfirmedFromStorage().then(function () {
          if (opts.useResumeEndpoint) {
            return fetchStatus().then(function (res) {
              var st = res.ok && res.body && res.body.stage;
              if (st && st !== "uploading" && st !== "viewable" && st !== "terminal") {
                return apiFetch("/api/ingestions/" + encodeURIComponent(jobId) + "/resume",
                  { method: "POST" }).then(jsonBody).catch(function () { return null; });
              }
              return null;
            });
          }
          return null;
        });
      }
      // 同名同大小未完任务 → 询问后续传；用户拒绝 = 换新任务语义，
      // 先取消旧任务再全新创建（不双占、不静默复用）
      var prev = storage.findResumable ? storage.findResumable(source) : null;
      if (!prev) return null;
      var doResume = opts.skipConfirm ||
        (opts.confirmResume ? opts.confirmResume() : true);
      if (doResume) {
        jobId = prev.job_id;
        return loadConfirmedFromStorage();
      }
      var cancelPrev = apiFetch(
        "/api/ingestions/" + encodeURIComponent(prev.job_id) + "/cancel",
        { method: "POST" });
      return cancelPrev.then(function () {
        storage.remove(prev.job_id);
      }, function () {
        storage.remove(prev.job_id);
      });
    }).then(function () {
      if (jobId) return null;
      return apiFetch("/api/ingestions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: source.name, declared_size: source.size,
        }),
      }).then(jsonBody).then(function (res) {
        if (res.ok && res.body && res.body.job_id) {
          jobId = res.body.job_id;
          // 任何分块发出之前先持久化任务记录；异步适配器（OPFS）返回 Promise，
          // 等它落盘——否则此刻刷新会留下无记录的服务端任务，再点就建第二个
          return Promise.resolve(saveRecord()).then(function () {
            emit({ type: "created", jobId: jobId, body: res.body });
            // 创建响应自带初始阶段（waiting_capacity/preparing）：先照实展示
            if (res.body.stage) emit({ type: "status", body: res.body });
            return null;
          });
        }
        throw { status: res.status, data: res.body };   // 422/409 → 稳定码映射
      });
    }).then(function () {
      return drive();
    }).then(null, function (err) {
      if (stopped || (err && err.cancelled)) return { cancelled: true };
      // 网络层失败统一打标（适配器只看 network 标志，不做跨 realm instanceof）
      if (err instanceof TypeError) throw { network: true, status: 0, data: null };
      throw err;
    });

    return {
      cancel: cancel,
      done: done,
    };
  }

  window.HP_COS_UPLOAD = {
    resolveConfig: resolveConfig,
    createUpload: createUpload,
    buildPlan: buildPlan,
    putPart: putPart,
  };
})();
