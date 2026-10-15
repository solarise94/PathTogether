/* =========================================================================
   inline-convert.js — 工作台上传面板内转换编排器（共享；classic script）。

   设计：docs/upload-inline-convert-design-20261015.md
     §3   队列状态机（一次拖入即授权整链：转换 → 校验 → 准入 → 上传）
     §4   每类格式判定表（路由 → 当场拒绝闸 → 引擎可转换终审）
     §4.3 转换产物的 direct_class 从**产物字节**嗅探得出
     §5   并发与锁：1 转换 + 1 上传；pt:upload-slot:<acct> + engine.uploadLockName
     §5.1 门禁作用域：viewable_formats 只作用于转换产物
     §6   刷新/续转：listJobs() 分「待继续组 / 待清理组」，绝不自动开始
     §7   失败 → 行内说明 + 「申请支持该格式」
     §8   开关与回退：enabled=false → 完全退回既有弹窗交接（控制流不变）

   形态约束（与 static/upload/cos-uploader.js 同风格）：
     - classic script：挂 window.HP_INLINE_CONVERT；无 ES import（app.js 是
       classic script，无法 import；运行器经注入的 async loader 动态引入）；
     - 无 DOM 依赖、无直接网络：渲染回调 / fetch / runner / 引擎全部注入；
     - 因此可被 vitest 用 new Function 直接加载并注入假依赖驱动。

   公开面：
     HP_INLINE_CONVERT.normalizeConfig(caps)  → {enabled, url, viewableFormats}|null
     HP_INLINE_CONVERT.parseSwitch(raw)       → bool（INLINE_CONVERT_ENABLED 同口径）
     HP_INLINE_CONVERT.routeFor(cls, engineSupported)
                                              → 'convert'|'upload'|'reject'|'bundle'|'need-bundle'
     HP_INLINE_CONVERT.artifactDeclaration(cls) → 'ome-tiff'|'converter-bigtiff'|null
     HP_INLINE_CONVERT.prefillFormatRequest(entry) → {format_ext, message}
     HP_INLINE_CONVERT.createOrchestrator(deps)   → 控制器（见下）
   ========================================================================= */
(function () {
  "use strict";

  // ------------------------------------------------------------- 开关/配置 --

  /// INLINE_CONVERT_ENABLED 同口径（设计 §8）：默认开；0/false/no（任意大小写、
  /// 可带空白）关。服务端已解析一次，这里只用于「无 bootstrap 时的测试注入」。
  function parseSwitch(raw) {
    if (raw === undefined || raw === null || raw === "") return true;
    var s = String(raw).trim().toLowerCase();
    return !(s === "0" || s === "false" || s === "no");
  }

  /// bootstrap.capabilities（或 cos_upload 载荷）→ 规范化配置。
  /// inline_convert 缺失/结构非法 → null：前端**不得**自行假设开启
  /// （旧后端/缓存 JS 的 fail-closed 语义，与 resolveConfig 同款）。
  function normalizeConfig(caps) {
    try {
      var c = caps && (caps.cos_upload || caps);
      var ic = c && c.inline_convert;
      if (!ic || typeof ic !== "object") return null;
      if (typeof ic.enabled !== "boolean") return null;
      var vf = ic.viewable_formats;
      if (!Array.isArray(vf)) return null;
      var out = [];
      for (var i = 0; i < vf.length; i++) {
        if (typeof vf[i] !== "string" || !vf[i]) return null;
        out.push(vf[i]);
      }
      return {
        enabled: ic.enabled,
        url: (typeof ic.url === "string" && ic.url) ? ic.url : "/tools/slides",
        viewableFormats: out,
      };
    } catch (e) {
      return null;
    }
  }

  // --------------------------------------------------------- 判定表（§4） --

  /// 路由判定（纯函数；设计 §4.1 步 1/3）。
  ///   cls.bundle===true         → 'bundle'（整包形态由调用方分派：文件夹走
  ///                               prepareBundle；散文件缺伴随成员 → 'need-bundle'）
  ///   cls.cls==='convert' +引擎真 → 'convert'
  ///   cls.cls==='convert' +引擎假 → 'upload'（不转换，按原样直传；服务端终审）
  ///   cls.cls==='ome-tiff'|'converter-bigtiff'|'temporary' → 'upload'
  ///   cls.cls==='unsupported'   → 'reject'
  ///   cls==null（嗅探不可用）    → 'upload'（既有词表路径降级）
  /// engineSupported 只对 'convert' 类有意义（未嗅探时传 null）。
  function routeFor(cls, engineSupported) {
    if (!cls) return "upload";
    if (cls.bundle === true) return "bundle";
    if (cls.cls === "unsupported") return "reject";
    if (cls.cls === "convert") {
      return engineSupported === false ? "upload" : "convert";
    }
    return "upload";
  }

  /// 转换产物的 direct_class（设计 §4.3）：**必须从产物字节嗅探**得出，
  /// 不能假定为 converter-bigtiff（bf-ome/fl-ome 的 IFD0 是 OME-XML，服务端
  /// 按声明字符串分派，错配 → worker convert_in_browser）。
  function artifactDeclaration(cls) {
    if (!cls) return null;
    if (cls.cls === "ome-tiff") return "ome-tiff";
    if (cls.cls === "converter-bigtiff") return "converter-bigtiff";
    return null;   // 嗅探不确定 → 不带声明（declaration_matches(None)=True）
  }

  /// 直传声明（设计 §4.2）：用**源文件**嗅探出的 directClass。
  /// `engineRejectedPassthrough`（引擎判不支持但 direct_import 开放 → 按原样
  /// 直传，§4.1「引擎拒绝分支」）时，convert 类也照发 `legacy-direct`
  /// （表里那一行的声明值）；除此之外 convert 类绝不发声明——`.svs` 等
  /// 直传关闭格式发 legacy-direct 会被服务端 422 convert_in_browser。
  function directDeclaration(cls, cfg, engineRejectedPassthrough) {
    if (!cls) return null;
    if (cls.cls === "ome-tiff") return "ome-tiff";
    if (cls.cls === "converter-bigtiff") return "converter-bigtiff";
    if (cls.svsJp2k) return "unconverted-variant:svs-jp2k";
    if (cls.cls === "temporary") return cls.directClass || null;
    if (cls.cls === "convert" && engineRejectedPassthrough) {
      return cls.directClass || null;
    }
    return null;
  }

  /// 转换产物门禁（设计 §5.1）——纯函数，与工具页
  /// tools-slides-upload.js:772-789 的判定序列等价：
  ///   ② 最终文件大小（job.result.outputBytes）↔ cfg.max_size_bytes
  ///   ③ 核心 result.format ∈ viewable_formats
  /// 字段缺失 = fail-closed（与工具页 `(cap.viewable_formats || [])` 落空同义）。
  /// §9.2 第 12 项的对照测试直接用它比对工具页控制器的 isDisabled 结论。
  function artifactGate(cfg, job, viewableFormats) {
    if (!cfg) {
      return { ok: false, code: "cos_unavailable",
               stageKey: "upload.cos.unavailable" };
    }
    var outBytes = (job && job.result && job.result.outputBytes) || 0;
    if (outBytes > cfg.max_size_bytes) {
      return { ok: false, code: "upload_too_large",
               stageKey: "upload.cos.err.too_large" };
    }
    var fmt = (job && job.result && job.result.format) || "";
    if (!Array.isArray(viewableFormats)) {
      return { ok: false, code: "viewable_formats_missing",
               stageKey: "upload.inline.err.notViewable" };
    }
    if (!fmt || viewableFormats.indexOf(fmt) < 0) {
      return { ok: false, code: "format_not_viewable",
               stageKey: "upload.inline.err.notViewable" };
    }
    return { ok: true, code: null, stageKey: null };
  }

  // ------------------------------------------------------------- 失败文案 --

  /// 引擎错误码 / 服务端码 → 行内文案键（设计 §7.3 + §7.4）。未知码 null
  /// （调用方回落原文，不猜语义）。
  function failureStageKey(code, detail) {
    if (code === "disk_precheck_failed") return "upload.inline.err.disk";
    if (code === "quota_exceeded_recoverable") return "upload.inline.err.quota";
    if (code === "resource_profile_insufficient") return "upload.inline.err.profile";
    if (code === "unsupported_input") return "upload.inline.err.unsupported";
    if (code === "conversion_output_too_large") return "upload.cos.err.too_large";
    if (code === "job_locked_other_tab") return "upload.inline.err.busy";
    if (code === "job_dir_missing") return "upload.inline.err.evicted";
    if (code === "resume_refused") return "upload.inline.err.resume";
    if (code === "source_changed_refuse_resume") return "upload.inline.err.resume";
    if (code === "io_recoverable") return "upload.inline.err.io";
    if (code === "upload_too_large") return "upload.cos.err.too_large";
    if (code === "cos_pool_below_product_limit") return "upload.cos.err.pool_config";
    if (code === "validation_failed") {
      return detail === "raster_format_mismatch"
        ? "upload.cos.err.raster_mismatch" : "upload.cos.err.validation";
    }
    if (code === "convert_in_browser") return "upload.inline.err.needs.convert";
    if (code === "client_gone") return "upload.cos.err.client_gone";
    if (code === "client_idle_timeout") return "upload.cos.err.client_idle_timeout";
    return null;
  }

  /// 「申请支持该格式」的预填（设计 §7.1）：format_ext + 带机器码的 message。
  function prefillFormatRequest(entry, caps) {
    var e = entry || {};
    var ext = "";
    var name = String(e.name || "");
    var dot = name.lastIndexOf(".");
    if (dot >= 0) ext = name.slice(dot + 1).toLowerCase();
    var code = (e.error && e.error.code) || "unsupported";
    var parts = ["code=" + code];
    if (e.error && e.error.detectedType) {
      parts.push("detected=" + e.error.detectedType);
    }
    if (ext) parts.push("declared=." + ext);
    if (e.error && e.error.engineReason) {
      parts.push("engine_reason=" + String(e.error.engineReason).slice(0, 300));
    }
    if (e.error && e.error.detail) parts.push("fail_detail=" + e.error.detail);
    if (caps && caps.inlineConvert && caps.inlineConvert.url) {
      parts.push("panel=" + caps.inlineConvert.url);
    }
    return { format_ext: ext, message: parts.join("; ") };
  }

  /// 哪些状态必须注册 beforeunload（设计 §6.2 第 7 点）：按**语义**判据
  /// 「未收口且本页持有用户的选择」，而不是枚举状态名（第二轮 J3 的根因就是
  /// 枚举漏了 need_disk_confirm）。
  var WARN_STATES = {
    converting: 1,
    need_disk_confirm: 1,
    convert_queued: 1,
    upload_queued: 1,
    uploading: 1,
    requeue_convert: 1,
    converted: 1,
  };
  function shouldWarn(state) {
    return !!WARN_STATES[state];
  }

  // 终态（不再有继续动作；只剩「删除本地产物」）
  var TERMINAL_STATES = {
    done: 1, rejected: 1, convert_failed: 1,
    upload_failed: 1, cancelled: 1,
  };
  function isTerminal(state) {
    return !!TERMINAL_STATES[state];
  }

  // -------------------------------------------------------- 转换阶段文案 --

  /// runner state → 行文案键（与工具页 PHASE_LABEL_KEY 同一套键，设计 §3.3）
  var PHASE_KEYS = {
    probing: "tools.phase.probing",
    planned: "tools.phase.planned",
    running: "tools.phase.running",
    paused: "tools.phase.paused",
    finalizing: "tools.phase.finalizing",
    validating: "tools.phase.validating",
    ready: "tools.phase.ready",
    cancelled: "tools.phase.cancelled",
    failed: "tools.phase.failed",
    cleanup_pending: "tools.phase.cleanup_pending",
  };

  /// worker progress → {key, frac, noteKey, noteVars}（设计 §3.3）
  function progressStage(p) {
    if (!p || typeof p !== "object") return null;
    if (p.unit === "stage" || p.unit === "stage-bundle") {
      var frac = p.total ? p.done / p.total : null;
      if (p.unit === "stage-bundle") {
        return { key: "tools.phase.probing", frac: frac,
                 noteKey: "tools.stage.bundle.bytes",
                 noteVars: { member: p.member, members: p.members } };
      }
      return { key: "tools.phase.probing", frac: frac, noteKey: null };
    }
    if (p.unit === "level" || p.unit === "tile-row") {
      var f2 = p.total ? p.done / p.total : null;
      return { key: "tools.phase.running", frac: f2, noteKey: null };
    }
    return null;
  }

  // ------------------------------------------------------------ 编排器 --

  /// createOrchestrator(deps) —— deps 全部注入（便于测试与 app.js 接线）：
  ///   runner        SlideToolsRunner 实例（probe/prepareBundle/startJob/
  ///                 resumeJob/cancelJob/listJobs/getJob/artifactView/
  ///                 setJobIntent/setJobUpload/discardJob）
  ///   engine        动态 import 的 engine.js 命名空间（sniff*/OUTPUT_PROFILES/
  ///                 ENCODING_PROFILES/outputFileName/errCode/errText/...）
  ///   sniff         HP_SLIDE_SNIFF（classifyFile/verifyUploadHeader/extOf）
  ///   cos           HP_COS_UPLOAD（createUpload/uploadLockName/...）
  ///   caps          {inlineConvert, directUploadFormats, browserConvertFormats,
  ///                  cosConfig}
  ///   ui            {row(entry), setStage(entry,key,frac,note,indeterminate),
  ///                  toast(key,vars,kind), buttons(entry), refresh()}
  ///   account()     当前账号（String(currentUserId) 加空串兜底，设计 §5 A6）
  ///   target()      入队时冻结的目标快照
  ///   onUploaded(entry)  完成回调（app.js 用它做 loadAll/关联/打开）
  ///   onArtifact(entry)  需要「上传产物」时由 app.js 提供（复用 uploadFileCos
  ///                      的直传语义；见 app.js 的 uploadEntryArtifact）
  ///   locks         navigator.locks（可注入假实现）
  ///   now/setTimer  定时器（测试可注入）
  ///   requestSupport(entry)  打开申请支持（app.js 提供）
  function createOrchestrator(deps) {
    deps = deps || {};
    var runner = deps.runner;
    var engine = deps.engine;
    var sniff = deps.sniff;
    var cos = deps.cos;
    var ui = deps.ui || {};
    var caps = deps.caps || {};
    var locks = deps.locks ||
      (typeof navigator !== "undefined" ? navigator.locks : null);
    var account = typeof deps.account === "function" ? deps.account : function () { return ""; };

    var entries = [];          // 队列（含终态，供行渲染与清理）
    var seq = 0;
    var activeConvert = null;  // 当前转换中的 entry
    var activeUpload = null;   // 当前上传中的 entry
    var resumed = {};          // jobId -> true（已恢复的行，避免重复）
    var pumpScheduled = false;

    function uuid() {
      if (deps.uuid) return deps.uuid();
      return "ic_" + Date.now().toString(36) + "_" +
        Math.random().toString(36).slice(2, 10);
    }

    function newEntry(fields) {
      seq += 1;
      var e = {
        id: fields.id || uuid(),
        seq: seq,
        file: fields.file || null,
        bundle: fields.bundle || null,
        name: fields.name || "",
        size: fields.size || 0,
        cls: fields.cls || null,
        directClass: null,
        needsConvert: false,
        jobId: fields.jobId || null,
        slideId: null,
        uploadJobId: null,
        target: fields.target || null,
        state: "sniffing",
        stage: null,
        error: null,
        requeueCount: 0,
        viaRestore: !!fields.viaRestore,
      };
      entries.push(e);
      return e;
    }

    function find(id) {
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].id === id) return entries[i];
      }
      return null;
    }

    function setStage(e, key, frac, note, indeterminate) {
      e.stage = { key: key, frac: frac, note: note || "", indeterminate: !!indeterminate };
      if (ui.setStage) ui.setStage(e, key, frac, note, indeterminate);
    }

    function markError(e, code, detail, extra) {
      e.error = {
        code: code || "io_recoverable",
        detail: detail || null,
        detectedType: (extra && extra.detectedType) || null,
        engineReason: (extra && extra.engineReason) || null,
      };
      if (ui.markError) ui.markError(e);
    }

    function toast(key, vars, kind) {
      if (ui.toast) ui.toast(key, vars, kind);
    }

    /// 终态收口（行按钮由 ui.buttons 按 state 渲染）
    function settle(e, state) {
      e.state = state;
      if (ui.buttons) ui.buttons(e);
      if (ui.refresh) ui.refresh(e);
      schedule();
    }

    // ---------------------------------------------------- §4 判定与入队 --

    /// 单文件入队（设计 §4.1）。opts.assocTarget 由 app.js 在**入队瞬间**冻结
    /// （设计 §3.5：越早越好；app.js 在调用 enqueue 之前取快照）。
    function enqueue(file, opts) {
      opts = opts || {};
      if (!file) return null;
      var e = newEntry({
        file: file,
        name: file.name || "?",
        size: typeof file.size === "number" ? file.size : 0,
        target: opts.assocTarget || null,
      });
      if (ui.row) ui.row(e);
      // 步 1：路由嗅探（有界读）
      var sniffP = (sniff && typeof sniff.classifyFile === "function")
        ? sniff.classifyFile(file)
        : Promise.resolve(null);
      sniffP.then(function (cls) {
        e.cls = cls || null;
        return refine(e, cls);
      }, function () {
        e.cls = null;
        return refine(e, null);
      });
      return e;
    }

    /// 整包入队（设计 §4.1.1）：成员行原样交给 prepareBundle；「是否完整包 /
    /// 缺哪个成员」只由运行器的 planner 判，面板不预判。
    function enqueueBundle(rows, folderName, opts) {
      opts = opts || {};
      var real = (rows || []).filter(function (r) { return r && r.file; });
      if (!real.length) return null;
      var total = 0;
      for (var i = 0; i < real.length; i++) {
        total += (typeof real[i].file.size === "number") ? real[i].file.size : 0;
      }
      var e = newEntry({
        bundle: { rows: real, folderName: folderName || "" },
        name: folderName || (real[0].file.name || "?"),
        size: total,
        target: opts.assocTarget || null,
      });
      e.cls = { cls: "convert", bundle: true, ext: "", directClass: null };
      e.needsConvert = true;
      e.state = "convert_queued";
      if (ui.row) ui.row(e);
      if (ui.buttons) ui.buttons(e);
      setStage(e, "upload.inline.stage.queued");
      schedule();
      return e;
    }

    /// 步 2（当场拒绝闸，零网络）+ 步 3（引擎可转换终审）+ 动作分派。
    function refine(e, cls) {
      // 步 2：内容与扩展名不符 → 当场拒绝（零网络请求）
      var verify = (sniff && typeof sniff.verifyUploadHeader === "function")
        ? sniff.verifyUploadHeader(e.name, e.file)
        : null;
      return Promise.resolve(verify).then(function (mismatch) {
        if (mismatch) {
          markError(e, "content_ext_mismatch", null,
            { detectedType: mismatch.detected });
          e.state = "rejected";
          setStage(e, "upload.inline.stage.rejected");
          if (ui.markError) ui.markError(e);
          toast("upload.inline.rejected.magic", { type: mismatch.detected }, "error");
          settle(e, "rejected");
          if (ui.buttons) ui.buttons(e);
          return e;
        }
        return sniffAndRoute(e, cls);
      });
    }

    function sniffAndRoute(e, cls) {
      if (!cls) {
        // 嗅探不可用：既有词表降级（服务端终审）
        e.needsConvert = false;
        e.state = "upload_queued";
        setStage(e, "upload.inline.stage.queued");
        if (ui.buttons) ui.buttons(e);
        schedule();
        return Promise.resolve(e);
      }
      if (cls.cls === "unsupported") {
        markError(e, "unsupported_ext", null, {});
        e.state = "rejected";
        setStage(e, "upload.inline.stage.rejected");
        toast("upload.fail", { e: "upload.err.unsupported" }, "error");
        if (ui.buttons) ui.buttons(e);
        settle(e, "rejected");
        return Promise.resolve(e);
      }
      if (cls.bundle === true) {
        // 散文件 bundle（设计 §4.1.1）：手上没有伴随成员 → 静态说明 + 选择文件夹
        e.needsConvert = true;
        markError(e, "need_bundle", null, {});
        e.state = "convert_failed";
        setStage(e, cls.ext === ".vms"
          ? "upload.inline.needBundle.vms" : "upload.inline.needBundle.mrxs");
        if (ui.buttons) ui.buttons(e);
        settle(e, "convert_failed");
        return Promise.resolve(e);
      }
      // 步 3：引擎可转换终审（面板不得比引擎更严——同一对函数）
      var headP = readHead(e.file);
      return headP.then(function (head) {
        var supported = null;
        var p;
        if (cls.cls === "convert") {
          if (engine && engine.isTiffHeader && engine.isTiffHeader(head)) {
            p = engine.sniffTiffSlideCapability(e.file);
          } else if (engine && engine.isRasterHeader && engine.isRasterHeader(head)) {
            p = engine.sniffRasterCapability(e.file);
          } else if (engine && engine.magicSupported) {
            p = Promise.resolve({ supported: engine.magicSupported(head) });
          } else {
            p = Promise.resolve({ supported: true });
          }
        } else {
          p = Promise.resolve({ supported: null });
        }
        return Promise.resolve(p).then(function (cap) {
          supported = cap ? cap.supported : null;
          e.engineCap = cap || null;
          var route = routeFor(cls, supported);
          if (route === "convert") {
            e.needsConvert = true;
            e.state = "convert_queued";
            setStage(e, "upload.inline.stage.queued");
            if (ui.buttons) ui.buttons(e);
            schedule();
            return e;
          }
          if (route === "reject") {
            markError(e, "unsupported_ext", null, {});
            e.state = "rejected";
            setStage(e, "upload.inline.stage.rejected");
            if (ui.buttons) ui.buttons(e);
            settle(e, "rejected");
            return e;
          }
          // route === 'upload'（含「引擎拒绝分支」）
          e.needsConvert = false;
          e.directClass = directDeclaration(cls, caps,
            cls.cls === "convert" && supported === false);
          if (cls.cls === "convert" && supported === false) {
            // 引擎拒绝但 direct_import 开放 → 按原样直传；关闭格式不上传
            if (!isDirectUploadOpen(e, cls)) {
              markError(e, "engine_unsupported_variant", null,
                { engineReason: (e.engineCap && e.engineCap.reason) || "" });
              e.state = "convert_failed";
              setStage(e, "upload.inline.stage.engineUnsupported");
              if (ui.buttons) ui.buttons(e);
              settle(e, "convert_failed");
              return e;
            }
          } else if (!cosUploadEligible(e, cls)) {
            markError(e, "format_not_accepted", null, {});
            e.state = "rejected";
            setStage(e, "upload.inline.stage.notAccepted");
            if (ui.buttons) ui.buttons(e);
            settle(e, "rejected");
            return e;
          }
          e.state = "upload_queued";
          setStage(e, "upload.inline.stage.queued");
          if (ui.buttons) ui.buttons(e);
          schedule();
          return e;
        });
      });
    }

    function readHead(file) {
      return Promise.resolve()
        .then(function () { return file.slice(0, 16).arrayBuffer(); })
        .then(function (buf) { return new Uint8Array(buf); },
              function () { return new Uint8Array(0); });
    }

    /// 直传开放判定（设计 §5.1）：能力 + 账号 + **源文件**大小 + 词表。
    /// viewable_formats **不适用**（直传条目没有 result.format）。
    function cosUploadEligible(e, cls) {
      var cfg = caps.cosConfig;
      if (!cfg) return false;
      if (!e.size || e.size > cfg.max_size_bytes) return false;
      var ext = sniff && sniff.extOf ? sniff.extOf(e.name)
        : (e.name.slice(e.name.lastIndexOf(".") + 1).toLowerCase());
      if (!ext) return false;
      ext = ext.replace(/^\./, "");
      return cfg.formats.indexOf(ext) >= 0;
    }

    /// direct_import 开放集（客户端只用服务端下发的清单做**提示**，最终由
    /// 服务端创建闸裁定）：direct_upload.formats。
    function isDirectUploadOpen(e, cls) {
      var list = caps.directUploadFormats;
      if (!Array.isArray(list)) return true;   // 清单缺失 → 交给服务端终审
      var ext = sniff && sniff.extOf ? sniff.extOf(e.name)
        : (e.name.lastIndexOf(".") >= 0
            ? e.name.slice(e.name.lastIndexOf(".")).toLowerCase() : "");
      return list.indexOf(String(ext).replace(/^\./, "")) >= 0;
    }

    // ------------------------------------------------------ §5 调度与锁 --

    function schedule() {
      if (pumpScheduled) return;
      pumpScheduled = true;
      Promise.resolve().then(function () {
        pumpScheduled = false;
        pump();
      });
    }

    function pick(states) {
      var best = null;
      for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
        if (states.indexOf(e.state) < 0) continue;
        if (!best || e.seq < best.seq) best = e;
      }
      return best;
    }

    /// 「1 个转换 + 1 个上传并行」（设计 §5）：两条独立推进。
    function pump() {
      if (!activeConvert) {
        var c = pick(["convert_queued"]) || pick(["requeue_convert"]);
        if (c) startConvert(c);
      }
      if (!activeUpload) {
        var u = pick(["upload_queued"]);
        if (u) startUpload(u);
      }
      if (ui.refresh) ui.refresh(null);
    }

    // ------------------------------------------------------- 转换执行 --

    /// 转换前的预检：能力可用 + 账号绑定 + 把目标写进 record.intent
    /// （设计 §3.5 第 3 点）。intent 落盘让刷新后 intent 里也有归属。
    function bindIntent(e) {
      if (!e.jobId || !runner.setJobIntent) return Promise.resolve();
      return Promise.resolve(runner.setJobIntent(e.jobId, {
        state: "pending",
        account: String(account() || ""),
        target: e.target || null,
        channel: "workbench-inline",
      })).then(null, function () { /* 记录写失败不阻塞转换 */ });
    }

    /// 转换执行（设计 §6.2 第 3 点 + §6.2.1）：四种进入方式，**按记录状态选**：
    ///   'probe'   新文件：probe(file) → startJob(file)
    ///   'bundle'  新文件夹：prepareBundle(rows) → startJob(null)（成员已在 OPFS）
    ///   'start'   刷新后 prepared/planned 无 journal：startJob(null,{jobId})
    ///             （源副本已在 OPFS，无需 File；设计 §6.2 第 3 点）
    ///   'resume'  刷新后 planned/paused 有 journal：resumeJob(jobId)
    /// 磁盘门确认（§6.2.1）**带原始输入重新起**：先 probe/prepareBundle 再 start，
    /// 绝不复用旧 jobId（运行器已丢弃旧 job）。
    function startConvert(e) {
      activeConvert = e;
      e.state = "converting";
      if (ui.buttons) ui.buttons(e);
      setStage(e, "tools.phase.probing", null, "", true);
      var jobOpts = {
        confirmUncertainDisk: !!e._confirmDisk,
        outputProfile: defaultOutputProfile(e),
        encodingProfile: engine && engine.ENCODING_PROFILES
          ? engine.ENCODING_PROFILES.PRESERVE : undefined,
      };
      var mode = e.mode || (e.bundle ? "bundle" : "probe");
      var first;
      try {
        if (mode === "resume") {
          // 续跑不传设置（记录里的值优先；设计 §6.2 第 3/4 点）
          first = runner.resumeJob(e.jobId);
        } else if (mode === "start") {
          // prepared：源副本已在 OPFS，直接开始（不需要 File）
          first = Promise.resolve({ jobId: e.jobId, __alreadyPrepared: true });
        } else if (mode === "bundle") {
          first = runner.prepareBundle(e.bundle.rows, jobOpts);
        } else {
          first = runner.probe(e.file, jobOpts);
        }
      } catch (err) {
        return failConvert(e, err);
      }
      return Promise.resolve(first).then(function (prep) {
        if (!prep) return null;
        e.jobId = prep.jobId || e.jobId;
        e.identity = prep.identity || e.identity || null;
        var doc = prep.probe && prep.probe.document;
        if (doc && doc.modality) e.modality = doc.modality;
        if (mode === "resume") {
          // resumeJob 直接返回 {jobId, done}；设置由记录决定
          if (prep.done) return prep.done;
          return runner.startJob(null, { jobId: e.jobId, outputProfile: defaultOutputProfile(e) }).done;
        }
        var startOpts = {
          jobId: e.jobId,
          outputProfile: defaultOutputProfile(e),
          encodingProfile: engine && engine.ENCODING_PROFILES
            ? engine.ENCODING_PROFILES.PRESERVE : undefined,
        };
        return bindIntent(e).then(function () {
          return runner.startJob(mode === "probe" ? e.file : null, startOpts);
        }).then(function (started) { return started && started.done; });
      }).then(function (result) {
        onConverted(e, result);
      }, function (err) {
        failConvert(e, err);
      });
    }

    /// 输出格式默认（设计 §4.4）：明场 bf-ome / 荧光 fl-ome；旧记录优先。
    function defaultOutputProfile(e) {
      var O = engine && engine.OUTPUT_PROFILES;
      if (!O) return undefined;
      if (e.modality === "fluorescence") return O.FL_OME;
      return O.BF_OME;
    }

    function onConverted(e, result) {
      activeConvert = null;
      if (!result || result.type === "cancelled" || (result && result.cancelled)) {
        e.state = "cancelled";
        setStage(e, "tools.phase.cancelled");
        if (ui.buttons) ui.buttons(e);
        settle(e, "cancelled");
        return;
      }
      if (!result.ok) {
        var info = (result && result.error) || {};
        var code = info.code || "io_recoverable";
        if (code === "disk_precheck_failed") {
          e.state = "need_disk_confirm";
          e._confirmDisk = false;
          e.diskInfo = info;
          markError(e, code, info.detail || null, {});
          setStage(e, "upload.inline.stage.disk");
          if (ui.buttons) ui.buttons(e);
          if (ui.refresh) ui.refresh(e);
          schedule();
          return;
        }
        return failConvert(e, result);
      }
      e.result = result.result || null;
      e.validation = result.validation || null;
      e.state = "converted";
      setStage(e, "tools.phase.ready", 1);
      if (ui.buttons) ui.buttons(e);
      // 自动进入上传（设计 §3.4：一次拖入即授权整链，不再第二次确认）
      e.state = "upload_queued";
      setStage(e, "upload.inline.stage.queued");
      if (ui.buttons) ui.buttons(e);
      schedule();
    }

    function failConvert(e, err) {
      activeConvert = null;
      var code = (err && (err.error ? err.error.code : err.code)) || "io_recoverable";
      var detail = (err && (err.error ? err.error.detail : err.detail)) || null;
      var reason = (err && (err.error ? err.error.message : err.message)) || null;
      if (engine && engine.errCode) {
        var c2 = engine.errCode(err);
        if (c2) code = c2;
      }
      markError(e, code, detail, { engineReason: reason });
      e.state = isTerminalCode(code) ? "convert_failed" : "convert_failed";
      setStage(e, "upload.inline.stage.convertFailed");
      toast("upload.fail", { e: failureStageKey(code, detail) || code }, "error");
      if (ui.buttons) ui.buttons(e);
      settle(e, "convert_failed");
    }

    function isTerminalCode(code) {
      return code !== "disk_precheck_failed";
    }

    // ----------------------------------------------------- 上传执行 --

    /// 上传槽 + 产物锁（设计 §5）：两把都拿才进临界区。
    ///   pt:upload-slot:<acct>            跨标签「1 个上传」的调度槽
    ///   engine.uploadLockName(jobId)     同任务产物的读写互斥（discardJob 的
    ///                                    保护对象；工具页也认这把）；直传条目
    ///                                    没有 jobId → 只有调度槽
    /// 两把都**持有到 release()**（never-resolving 回调占住，与运行器同款）。
    /// 返回值区分两种「没拿到」：
    ///   {held:false, unavailable:true}  navigator.locks 不可用 → 照常执行
    ///                                   （与既有 acquireContentLock 的退化语义一致）
    ///   {held:false, contended:true}    另一标签正持有 → **不并发**，排队等
    function acquireUploadLocks(e) {
      if (!locks || typeof locks.request !== "function") {
        return Promise.resolve({ held: false, unavailable: true,
                                 release: function () {} });
      }
      var acct = String(account() || "");
      var names = [];
      if (e.jobId && cos && cos.uploadLockName) names.push(cos.uploadLockName(e.jobId));
      names.push("pt:upload-slot:" + acct);
      var releases = [];
      function take(name) {
        return new Promise(function (resolve) {
          try {
            locks.request(name, { ifAvailable: true }, function (lock) {
              if (!lock) { resolve(false); return; }
              resolve(true);
              // 持锁直到 release()：占住临界区（Promise 永不在内部 resolve）
              return new Promise(function (release) { releases.push(release); });
            }).catch(function () { resolve(false); });
          } catch (err) {
            resolve(false);
          }
        });
      }
      function step(i) {
        if (i >= names.length) {
          return Promise.resolve({
            held: true,
            release: function () { releases.splice(0).forEach(function (r) { r(); }); },
          });
        }
        return take(names[i]).then(function (ok) {
          if (!ok) {
            releases.splice(0).forEach(function (r) { r(); });
            return { held: false, contended: true, release: function () {} };
          }
          return step(i + 1);
        });
      }
      return step(0);
    }

    function startUpload(e) {
      activeUpload = e;
      e.state = "uploading";
      if (ui.buttons) ui.buttons(e);
      setStage(e, "upload.inline.stage.queued", null, "", true);
      return acquireUploadLocks(e).then(function (locks) {
        if (!locks.held) {
          if (locks.contended) {
            // 另一标签正在上传/操作同一任务 → 排队等待（不并发，设计 §5）
            setStage(e, "upload.cos.other.tab", null, "", true);
            activeUpload = null;
            e.state = "upload_queued";
            if (ui.buttons) ui.buttons(e);
            if (deps.setTimer) {
              deps.setTimer(function () { schedule(); }, 1500);
            }
            return;
          }
          // navigator.locks 不可用：照常执行（退化语义，绝不阻塞）
          return runUpload(e).then(null, function () { /* failUpload 已收口 */ });
        }
        e._releaseLocks = locks.release;
        return runUpload(e).then(function () {
          locks.release();
        }, function () {
          locks.release();
        });
      });
    }

    function runUpload(e) {
      // 判别式：**有没有本机转换任务**（jobId）。没有 = 直传条目，门禁只到
      // 「直传口径」（能力 + 账号 + **源文件**大小 + 词表，入队判定已做过），
      // 直接交给 app.js 的直传实现（设计 §5.1：viewable_formats 不适用于直传）。
      // 有 = 本机产物（转换产物 / 刷新恢复的 ready 行）→ 走产物门禁。
      if (!e.jobId) {
        if (typeof deps.onArtifact !== "function") {
          failUpload(e, { data: { code: "cos_unavailable" } });
          return Promise.resolve();
        }
        return Promise.resolve(deps.onArtifact(e, null)).then(function (outcome) {
          return completeUpload(e, outcome);
        }, function (err) {
          failUpload(e, err);
        });
      }
      var job = null;
      var artifact = null;
      return Promise.resolve()
        .then(function () {
          return runner.getJob(e.jobId);
        })
        .then(function (j) {
          job = j;
          // 门禁（设计 §5.1）：能力 + 账号 + **最终产物**大小 + viewable_formats
          var gate = entryArtifactGate(e, job);
          if (!gate.ok) {
            markError(e, gate.code, null, {});
            e.state = "convert_failed";
            setStage(e, gate.stageKey);
            toast("upload.fail", { e: gate.stageKey }, "error");
            if (ui.buttons) ui.buttons(e);
            return Promise.reject({ __handled: true });
          }
          return runner.artifactView(e.jobId);
        })
        .then(function (file) {
          artifact = file;
          e.artifactSize = file.size;
          e.artifactName = engine && engine.outputFileName
            ? engine.outputFileName(e.name, job) : e.name;
          // §4.3：产物声明从**产物字节**嗅探
          var cp = (sniff && typeof sniff.classifyFile === "function")
            ? sniff.classifyFile(file) : Promise.resolve(null);
          return Promise.resolve(cp).then(function (prodCls) {
            e.artifactCls = prodCls || null;
            return null;
          }, function () { e.artifactCls = null; return null; });
        })
        .then(function () {
          if (typeof deps.onArtifact === "function") {
            // app.js 提供上传实现（复用 uploadFileCos 的直传语义与记录适配）
            return deps.onArtifact(e, artifact);
          }
          return uploadArtifact(e, artifact, job);
        })
        .then(function (outcome) {
          return completeUpload(e, outcome);
        })
        .then(null, function (err) {
          if (err && err.__handled) return;
          failUpload(e, err);
        });
    }

    /// 成功/取消收口（转换产物与直传共用）。
    function completeUpload(e, outcome) {
      if (outcome && outcome.cancelled) {
        e.state = "cancelled";
        setStage(e, "upload.cos.cancelled");
        if (ui.buttons) ui.buttons(e);
        settle(e, "cancelled");
        return;
      }
      var body = (outcome && outcome.body) || {};
      e.slideId = body.slide_id ||
        (body.conversion && body.conversion.slide_id) || null;
      e.state = "done";
      setStage(e, "upload.stage.done", 1);
      if (ui.buttons) ui.buttons(e);
      toast("upload.done", { name: e.artifactName || e.name }, "success");
      if (typeof deps.onUploaded === "function") deps.onUploaded(e, body);
      settle(e, "done");
    }

    /// 转换产物的门禁（设计 §5.1）：只有本机产物走这里（直传条目**不适用**）。
    /// 实现是下面的纯函数 artifactGate（与工具页 tools-slides-upload.js:772-789
    /// 的判定序列等价）；这里只负责取出本条目的 cfg 与 viewable_formats。
    function entryArtifactGate(e, job) {
      return artifactGate(caps.cosConfig, job,
        caps.inlineConvert ? caps.inlineConvert.viewableFormats : null);
    }

    function uploadArtifact(e, artifact, job) {
      var cfg = caps.cosConfig;
      var name = e.artifactName || e.name;
      var declaration = artifactDeclaration(e.artifactCls);
      var source = {
        name: name,
        size: artifact.size,
        slice: function (s, len) { return artifact.slice(s, len); },
      };
      var upload = cos.createUpload({
        source: source,
        apiFetch: deps.apiFetch,
        heartbeatFetch: deps.apiFetchNoRedirect || deps.apiFetch,
        config: cfg,
        createBody: declaration ? { direct_class: declaration } : null,
        storage: {
          save: function (rec) {
            if (!rec || !rec.job_id) return undefined;
            return runner.setJobUpload(e.jobId, {
              ingestionId: rec.job_id, filename: name, size: artifact.size,
              state: "uploading",
              confirmedParts: rec.confirmed || [],
              partDigests: rec.digests || {},
              digestScheme: rec.digest_scheme || "",
              account: String(account() || ""),
            });
          },
          complete: function (id, outcome) {
            return runner.setJobUpload(e.jobId, {
              ingestionId: id, filename: name, size: artifact.size,
              state: (outcome && outcome.succeeded) ? "published" : "failed",
              slideId: (outcome && outcome.slide_id) || null,
              error: (outcome && outcome.fail_code) || null,
              errorDetail: (outcome && outcome.fail_detail) || null,
            });
          },
          remove: function (id) {
            return runner.setJobUpload(e.jobId, {
              ingestionId: id, filename: name, size: artifact.size,
              state: "cancelled",
            });
          },
          readConfirmed: function (id) {
            return runner.getJob(e.jobId).then(function (j) {
              var up = j && j.upload;
              return (up && up.ingestionId === id) ? (up.confirmedParts || []) : [];
            });
          },
          readRecord: function (id) {
            return runner.getJob(e.jobId).then(function (j) {
              var up = j && j.upload;
              if (!up || up.ingestionId !== id) return null;
              return {
                confirmed: up.confirmedParts || [],
                digests: up.partDigests || null,
                digest_scheme: up.digestScheme || "",
                account: typeof up.account === "string" ? up.account : "",
              };
            });
          },
        },
        account: String(account() || ""),
        resumeJobId: (job && job.upload && job.upload.ingestionId &&
                      job.upload.state !== "published" &&
                      job.upload.state !== "failed" &&
                      job.upload.state !== "cancelled")
          ? job.upload.ingestionId : null,
        skipConfirm: true,
        confirmResume: function () { return true; },
        retryCompleteOnNetworkError: false,
        useResumeEndpoint: false,
        onEvent: function (ev) {
          if (ev.type === "progress") {
            setStage(e, "upload.cos.stage.uploading",
                     ev.determinate === false ? null : ev.frac, "", ev.determinate === false);
          } else if (ev.type === "status" && ev.body) {
            setStage(e, "upload.cos.stage." + (ev.body.stage || "uploading"), null, "", true);
          } else if (ev.type === "retry") {
            setStage(e, "upload.cos.stage.uploading", null, "upload.cos.retrying", true);
          } else if (ev.type === "created") {
            e.uploadJobId = ev.jobId;
          }
        },
      });
      e._upload = upload;
      return upload.done;
    }

    function failUpload(e, err) {
      activeUpload = null;
      if (e._releaseLocks) { e._releaseLocks(); e._releaseLocks = null; }
      var code = "";
      var detail = null;
      if (err && err.magicMismatch) {
        markError(e, "content_ext_mismatch", null,
          { detectedType: err.magicMismatch.detected });
        e.state = "upload_failed";
        setStage(e, "upload.inline.err.magic");
        if (ui.buttons) ui.buttons(e);
        settle(e, "upload_failed");
        return;
      }
      if (err && err.terminal) {
        code = (err.data && err.data.fail_code) || "";
        detail = (err.data && err.data.fail_detail) || null;
      } else if (err && err.data && err.data.code) {
        code = err.data.code;
        detail = err.data.fail_detail || null;
      } else if (err && err.network) {
        code = "network";
      } else if (err && typeof err.part === "number") {
        code = "part_failed";
      }
      // 服务端识别漏判（convert_in_browser）→ 自动回队**恰一次**（设计 §7.2）
      if (code === "convert_in_browser" && e.requeueCount < 1) {
        e.requeueCount += 1;
        e.directClass = null;
        e.jobId = null;
        e.uploadJobId = null;
        e.state = "requeue_convert";
        setStage(e, "upload.inline.stage.requeue");
        if (ui.buttons) ui.buttons(e);
        if (ui.refresh) ui.refresh(e);
        schedule();
        return;
      }
      markError(e, code || "upload_failed", detail, {});
      e.state = "upload_failed";
      setStage(e, failureStageKey(code, detail) || "upload.stage.failed");
      toast("upload.fail", { e: failureStageKey(code, detail) || code || "—" }, "error");
      if (ui.buttons) ui.buttons(e);
      settle(e, "upload_failed");
    }

    // ------------------------------------------------------ 动作（§6.3） --

    /// 用户动作：continue（继续转换 / 继续上传）、confirmDisk、cancel、discard
    function act(id, action) {
      var e = find(id);
      if (!e) return null;
      if (action === "continue") {
        if (e.state === "upload_queued" || e.state === "upload_failed") {
          e.state = "upload_queued";
          setStage(e, "upload.inline.stage.queued");
          if (ui.buttons) ui.buttons(e);
          schedule();
          return e;
        }
        e._resume = !!e.jobId;
        e.state = "convert_queued";
        setStage(e, "upload.inline.stage.queued");
        if (ui.buttons) ui.buttons(e);
        schedule();
        return e;
      }
      if (action === "confirmDisk") {
        // 确认后**带原始输入重新起**（设计 §6.2.1）：运行器已丢弃旧 job，
        // 因此绝不复用旧 jobId。
        e._confirmDisk = true;
        e.jobId = null;
        e._resume = false;
        e.error = null;
        e.state = "convert_queued";
        setStage(e, "upload.inline.stage.queued");
        if (ui.buttons) ui.buttons(e);
        schedule();
        return e;
      }
      if (action === "cancel") {
        cancel(e);
        return e;
      }
      if (action === "discard") {
        discard(e);
        return e;
      }
      return null;
    }

    function cancel(e) {
      if (e.state === "converting" && runner.cancelJob) {
        Promise.resolve(runner.cancelJob()).then(null, function () { /* */ });
      } else if (e.state === "uploading" && e._upload && e._upload.cancel) {
        e._upload.cancel();
      }
      activeConvert = activeConvert === e ? null : activeConvert;
      activeUpload = activeUpload === e ? null : activeUpload;
      if (e._releaseLocks) { e._releaseLocks(); e._releaseLocks = null; }
      setStage(e, "upload.cos.cancelled");
      settle(e, "cancelled");
    }

    function discard(e) {
      if (!e.jobId || !runner.discardJob) return Promise.resolve(false);
      return Promise.resolve(runner.discardJob(e.jobId)).then(function () {
        e.state = "discarded";
        if (ui.remove) ui.remove(e);
        if (ui.refresh) ui.refresh(e);
        return true;
      }, function (err) {
        var info = (err && err.error) || {};
        if (info.code === "upload_active" && info.lockHeld === false) {
          // 遗留上传记录且无锁持有者：再确认一次后放弃上传（工具页同一处置）
          return Promise.resolve(runner.discardJob(e.jobId, { abandonUpload: true }))
            .then(function () {
              e.state = "discarded";
              if (ui.remove) ui.remove(e);
              if (ui.refresh) ui.refresh(e);
              return true;
            }, function () { return false; });
        }
        return false;
      });
    }

    // ------------------------------------------------- 刷新恢复（§6.2） --

    /// 只**列出**，绝不自动开始（设计 §6.2 第 1 点）。分两组渲染：
    ///   待继续组：prepared/planned/paused/failed，或 ready/exported 且 upload 未收口
    ///   待清理组：其余（含 upload.state==='published'）——也要有行，否则
    ///             「删除本地产物」在刷新后不可达（第四轮 A2）
    function restore() {
      if (!runner || !runner.listJobs) return Promise.resolve([]);
      return Promise.resolve(runner.listJobs()).then(function (jobs) {
        var made = [];
        (jobs || []).forEach(function (job) {
          var intent = job && job.intent;
          if (!intent || intent.channel !== "workbench-inline") return;
          if (resumed[job.id]) return;
          resumed[job.id] = true;
          var hasSettledUpload = job.upload && (job.upload.state === "published" ||
            job.upload.state === "failed" || job.upload.state === "cancelled");
          var unfinished = job.state === "prepared" || job.state === "planned" ||
            job.state === "paused" || job.state === "failed" ||
            ((job.state === "ready" || job.state === "exported") &&
             !hasSettledUpload);
          var e = newEntry({
            id: job.id,
            name: (job.source && (job.source.folderName || job.source.name)) || job.id,
            size: (job.source && job.source.size) || 0,
            jobId: job.id,
            target: intent.target || null,
            viaRestore: true,
          });
          e.cls = intent.cls || null;
          e.modality = job.modality || null;
          e.committedBytes = job.committedBytes || 0;
          e.hasUpload = !!(job.upload && job.upload.ingestionId);
          e.uploadState = (job.upload && job.upload.state) || null;
          if (unfinished && (job.state === "ready" || job.state === "exported")) {
            // 产物已就绪 → 直接「继续上传」（跳过转换；设计 §6.2 第 3 点）
            e.mode = null;
            e.state = "upload_queued";
            setStage(e, "upload.inline.stage.resumable");
          } else if (unfinished) {
            // 运行器给的 nextAction 决定 resume / start（设计 §6.2 第 3 点）
            e.mode = job.nextAction === "resume" ? "resume" : "start";
            e.state = "convert_queued";
            setStage(e, "upload.inline.stage.resumable",
                     null, "upload.inline.note.committed", true);
          } else {
            e.mode = null;
            e.state = "done";
            setStage(e, "upload.inline.stage.settled");
          }
          if (ui.row) ui.row(e);
          if (ui.buttons) ui.buttons(e);
          made.push(e);
        });
        if (ui.refresh) ui.refresh(null);
        return made;
      }, function () { return []; });
    }

    function list() { return entries.slice(); }
    function get(id) { return find(id); }
    function busy() {
      return { converting: !!activeConvert, uploading: !!activeUpload };
    }
    /// beforeunload 提醒（设计 §6.2 第 7 点）：任一「未收口且持有用户选择」的 entry
    function warnNeeded() {
      for (var i = 0; i < entries.length; i++) {
        if (shouldWarn(entries[i].state)) return true;
      }
      return false;
    }

    return {
      enqueue: enqueue,
      enqueueBundle: enqueueBundle,
      act: act,
      cancel: cancel,
      discard: discard,
      restore: restore,
      list: list,
      get: get,
      busy: busy,
      warnNeeded: warnNeeded,
      _entries: entries,
    };
  }

  window.HP_INLINE_CONVERT = {
    parseSwitch: parseSwitch,
    normalizeConfig: normalizeConfig,
    routeFor: routeFor,
    artifactDeclaration: artifactDeclaration,
    directDeclaration: directDeclaration,
    artifactGate: artifactGate,
    failureStageKey: failureStageKey,
    prefillFormatRequest: prefillFormatRequest,
    shouldWarn: shouldWarn,
    isTerminal: isTerminal,
    PHASE_KEYS: PHASE_KEYS,
    progressStage: progressStage,
    WARN_STATES: WARN_STATES,
    createOrchestrator: createOrchestrator,
  };
})();
