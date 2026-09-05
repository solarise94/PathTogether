/* =========================================================================
   SVS 病理图像查看器 —— 前端逻辑（OpenSeadragon + ROI + 项目/标注）
   ========================================================================= */
(function () {
  "use strict";

  // AI 空状态建议提示条：点击只填入输入框（不自动发送——用户可改写后再发）。
  // 委托绑定覆盖模板初始空状态与插件重渲的空状态两种来源。
  document.addEventListener("click", function (e) {
    var chip = e.target && e.target.closest ? e.target.closest(".ai-suggest-chip") : null;
    if (!chip) return;
    var key = chip.getAttribute("data-task-key");
    var ta = document.getElementById("ai-task");
    if (!ta || !key) return;
    var v = t(key);
    if (v && v !== key) {
      ta.value = v;
      ta.focus();
      try { ta.dispatchEvent(new Event("input", { bubbles: true })); } catch (err) {}
    }
  });

  // 中英双语：i18n.js 在本脚本之前加载，提供 window.HP_I18N.t
  function t(key, vars) {
    return window.HP_I18N ? window.HP_I18N.t(key, vars) : key;
  }

  // 本轮新增的 i18n 文案（暂未落入 i18n.js 字典的兜底表）。优先取 i18n.js 的值；
  // 缺失时按当前界面语言走本地兜底，避免回退成 key 本身。
  var _EXTRA_I18N = {
    "ai.fork.sending": { zh: "发送中…", en: "Sending…" },
    "anno.fork.quick": { zh: "快速问答", en: "Quick Q&A" },
    "anno.fork.quick.tip": { zh: "就此标注快速提问（轻量批注对话）", en: "Ask about this annotation (lightweight fork chat)" },
    "anno.branch.deep": { zh: "从此处深读", en: "Deep dive" },
    "anno.branch.deep.tip": { zh: "在 AI 面板从此标注开分支会话（全量工具深读）", en: "Open a branch session from here (full tools, deep read)" },
    "anno.private.badge": { zh: "私有", en: "Private" },
    // 上传错误码 → 可读文案（上传修复 U1：服务端返回的机器码不再原样透出；
    // U3 补充 V2 分片机器码与三段状态文案）
    "upload.err.csrf": { zh: "登录状态已失效，请刷新页面后重试", en: "Session expired, please refresh and retry" },
    "upload.err.guard": { zh: "上传配额服务暂不可用，请稍后重试", en: "Upload quota service unavailable, please retry later" },
    "upload.err.name": { zh: "名称不可用，请重命名后重试", en: "Name unavailable, please rename and retry" },
    "upload.err.too_large": { zh: "文件超过单次上传上限", en: "File exceeds the per-request upload limit" },
    "upload.err.disk": { zh: "服务器磁盘空间不足", en: "Insufficient disk space on server" },
    "upload.err.offset_mismatch": { zh: "分片偏移与服务端不一致，正在对齐重传", en: "Chunk offset out of sync, realigning and retrying" },
    "upload.err.hash_mismatch": { zh: "内容校验失败（哈希不匹配），请重新上传", en: "Integrity check failed (hash mismatch), please re-upload" },
    "upload.err.state_conflict": { zh: "上传任务状态冲突，请刷新页面后重试", en: "Upload task state conflict, please refresh and retry" },
    "upload.err.use_legacy": { zh: "ZIP/MRXS 请使用单请求上传", en: "ZIP/MRXS must use the single-request upload" },
    "upload.err.invalid_slide": { zh: "文件校验失败：不是有效的切片文件", en: "Validation failed: not a valid slide file" },
    // 上传修复 A0：新增稳定机器码的中文文案（保留机器码供排障）
    "upload.err.slide_open_unsupported": { zh: "文件格式不受支持：服务端无法按切片格式打开该文件", en: "Unsupported format: the server cannot open this file as a slide" },
    "upload.err.slide_open_failed": { zh: "切片解析失败：文件可能损坏或不完整，请重试上传", en: "Slide parsing failed: the file may be corrupted or incomplete, please retry" },
    "upload.err.commit_retry": { zh: "服务端校验暂时失败，请稍后重试提交", en: "Server validation failed temporarily, retry commit later" },
    "upload.err.size_mismatch": { zh: "文件大小与声明不符，请重新上传", en: "File size mismatch, please re-upload" },
    "upload.err.resume": { zh: "上传中断；刷新页面后将从断点续传", en: "Upload interrupted; refresh to resume from breakpoint" },
    // U3 三段状态（§3.5：正在传输 → 服务端校验 → 入库完成）
    "upload.stage.transferring": { zh: "正在传输", en: "Transferring" },
    "upload.stage.validating": { zh: "服务端校验中", en: "Validating on server" },
    "upload.stage.done": { zh: "入库完成", en: "Completed" },
    "upload.stage.failed": { zh: "上传失败", en: "Upload failed" },
    // 升级 C（§6.1）：矩形工具文案（i18n.js 为主源；此处兜底）
    "roi.rect.tip": { zh: "矩形工具：在视野中拖出矩形，或输入宽高后点击中心放置；拖内部平移、边/角调整大小；Escape 取消",
                      en: "Rectangle tool: drag in the view, or enter width/height then click to place; drag inside to move, edges/corners to resize; Escape cancels" },
    "roi.cancelled": { zh: "已取消未保存的选区", en: "Unsaved selection cancelled" },
    "roi.input.invalid": { zh: "矩形尺寸非法或超出图像范围，已保留上次的合法框",
                           en: "Invalid rectangle size or out of image bounds; kept the last valid box" },
    "roi.too.large": { zh: "矩形超过像素预算（{n} 像素），请缩小选区",
                       en: "Rectangle exceeds the pixel budget ({n} px); please shrink the selection" },
    "edit.conflict": { zh: "该标注已被他人修改（当前 revision {rev}），已显示当前版本；请基于最新版本重新编辑",
                       en: "This annotation was modified by someone else (current revision {rev}); showing the current version — please re-edit on top of it" },
  };
  function tt(key) {
    try {
      var s = window.HP_I18N && window.HP_I18N.t(key);
      if (s && s !== key) return s;
    } catch (e) {}
    var lang = (window.HP_I18N && window.HP_I18N.getLang()) || "zh";
    var e = _EXTRA_I18N[key];
    return (e && (e[lang] || e.zh)) || key;
  }

  // ---------- 全局状态 ----------
  var state = {
    slide: null,          // 当前切片 {name,width,height,mppX,mppY,mppSource}
    mppX: null,           // 当前生效的 µm/px
    // 升级 C（§6.1）：单一「矩形」入口。roiMode 为工具激活标志（null|"rect"）；
    // 选区本身是 level-0 像素矩形 roi={x,y,w,h}（权威几何，§6.2）。
    roiMode: null,        // null | "rect"
    roi: { x: 0, y: 0, w: 0, h: 0 },
    roiLockRatio: false,  // 锁定宽高比（默认不锁定）
    roiUnit: "mm",        // 设置区单位：mm | um | px
    roiPreset: "",        // "" | "6" | "6.5"（mm 预设只填数值不强制锁定）
    rotation: 0,
    flipped: false,       // 是否水平翻转（镜像）
    drawMode: null,       // null | "arrow" | "freehand"（与 roiMode 互斥）
    showAnno: false,      // 是否在画布层显示已保存标注
    focusAnno: null,      // null=显示全部；否则只显示该条标注（flatItems 中的引用）
    channelReopening: false, // 通道配色重开（同一切片换 TileSource，非新切片）
  };

  // ---------- 401 认证处理 ----------
  // fetch 包装：
  //  - 非安全方法自动附带 X-CSRF-Token（统一 CSRF 设施：非 HttpOnly 的 csrf_token
  //    cookie 与 session 绑定，双提交校验）；
  //  - 响应 401 且 body 含 auth_required 时跳登录页。
  // 对现有调用透明——仍返回 Response，调用方照常 .json()/.ok 判断。
  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function apiFetch(url, opts) {
    opts = opts || {};
    var method = (opts.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
      var tok = csrfToken();
      if (tok) {
        var headers = Object.assign({}, opts.headers || {});
        if (!headers["X-CSRF-Token"]) { headers["X-CSRF-Token"] = tok; }
        opts.headers = headers;
      }
    }
    return fetch(url, opts).then(function (resp) {
      if (resp.status === 401) {
        // 尝试读 body 判断是否 auth_required（不消费主响应流：克隆一份）
        return resp.clone().json().then(
          function (body) {
            if (body && body.error === "auth_required") {
              location.href = "/login?next=" + encodeURIComponent(location.pathname);
            }
            return resp;
          },
          function () { return resp; }  // body 非 JSON，原样返回
        );
      }
      return resp;
    });
  }

  // 当前登录用户角色（/api/auth/info 缓存）。currentRole/currentUserId 是
  // effective subject（预览中为被预览用户）；actorRole 永远是真实管理员。
  var currentRole = null;
  var currentUserId = null;
  var actorRole = null;
  var actorUserId = null;
  var previewState = null;

  function applyAuthInfo(info) {
    if (!info || !info.auth_enabled) return info;
    var actor = info.actor || {};
    previewState = info.preview || null;
    currentRole = info.role || null;
    currentUserId = info.user_id || null;
    actorRole = actor.role || info.role || null;
    actorUserId = actor.user_id || info.user_id || null;
    var actorName = actor.username || info.username;
    if (els.logoutBtn) {
      var label = t("toast.logout");
      if (actorName) { label += " (" + actorName + ")"; }
      els.logoutBtn.textContent = label;
      els.logoutBtn.hidden = !!previewState;
    }
    if (els.changepwBtn) { els.changepwBtn.hidden = !!previewState; }
    // 管理台入口按真实 actor 判定（预览态隐藏——与改密/登出同级约定；
    // 预览中 /admin 仍可手动直达，宿主每条消息回查真实 owner）。
    if (els.adminEntryLink) {
      els.adminEntryLink.hidden = !!previewState || actorRole !== "owner";
    }
    if (window.HP_I18N && window.HP_I18N.setRole) { window.HP_I18N.setRole(currentRole); }
    if (els.sharePermHint) {
      els.sharePermHint.hidden = currentRole !== "user";
    }
    applyPreviewBanner(info);
    if (currentRole === "owner") loadDemoCatalog();
    return info;
  }

  function applyPreviewBanner(info) {
    var banner = els.previewBanner;
    if (!banner) return;
    var pv = (info && info.preview) || null;
    if (!pv) {
      banner.hidden = true;
      return;
    }
    var mins = Math.max(1, Math.round(((pv.expires_at || 0) * 1000 - Date.now()) / 60000));
    if (els.previewBannerText) {
      els.previewBannerText.textContent = t("preview.banner", {
        user: pv.subject_username || pv.subject_user_id || "",
        role: pv.subject_role || "",
        mins: mins,
      });
    }
    banner.hidden = false;
  }

  function startIdentityPreview(uid) {
    apiFetch("/api/admin/preview/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: uid }),
    }).then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      if (res.status !== 200) { toast(res.body.error || "预览失败", "error"); return; }
      location.reload();
    }).catch(function (e) {
      toast((e && e.message) ? e.message : "预览失败", "error");
    });
  }

  function stopIdentityPreview() {
    apiFetch("/api/admin/preview/stop", { method: "POST" }).then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      if (res.status !== 200) { toast(res.body.error || "退出预览失败", "error"); return; }
      location.reload();
    }).catch(function (e) {
      toast((e && e.message) ? e.message : "退出预览失败", "error");
    });
  }

  // 页面初始化时拉取认证状态：启用认证则显示「修改我的密码」与退出登录
  function initAuth() {
    apiFetch("/api/auth/info").then(function (r) { return r.json(); }).then(function (info) {
      applyAuthInfo(info);
      // 升级 A：身份到位后按「站点:账号」重读侧栏偏好（无偏好保持默认收起）
      if (sidebarCtrl) sidebarCtrl.onScopeReady();
    }).catch(function () { /* 忽略，不影响主功能 */ });
    if (els.previewStopBtn) {
      els.previewStopBtn.addEventListener("click", stopIdentityPreview);
    }
  }

  // 退出登录：POST /logout + CSRF（docs §10.14；GET /logout 入口已随
  // r3-wave1 物理删除——仅存 POST，无兼容期）
  // 产品语义：只有服务端确认退出成功才跳登录页；网络/HTTP 失败留在当前页并提示。
  function doLogout() {
    apiFetch("/logout", { method: "POST" }).then(function (resp) {
      if (!resp || !resp.ok) {
        throw new Error((resp && resp.status) ? ("HTTP " + resp.status) : "logout failed");
      }
      location.href = "/login";
    }).catch(function (e) {
      toast(t("toast.logout.fail", { e: (e && e.message) ? e.message : e }), "error");
    });
  }
  // apiFetch 一并导出：tests/js/api-fetch-401.test.ts 对 401→/login?next=...
  // 跳转契约做行为测试（与 doLogout 同一挂载点）。
  window.HP_AUTH = {
    doLogout: doLogout,
    apiFetch: apiFetch,
    applyAuthInfo: applyAuthInfo,
    startIdentityPreview: startIdentityPreview,
    stopIdentityPreview: stopIdentityPreview,
  };

  // ---------- 修改我的密码（账户系统批次 A docs §7.1；owner/user 通用） ----------
  // 弹窗三字段（当前/新/确认，minlength=15 maxlength=200 由模板约束）；
  // POST /api/account/password（JSON + 现有 CSRF header 机制）；成功后服务端
  // 已清空全部 session，前端跳 /login?password_changed=1 重新登录。
  function changepwShowError(msg) {
    if (!els.changepwError) return;
    els.changepwError.textContent = msg || "";
    els.changepwError.hidden = !msg;
  }

  function changepwOpen() {
    if (!els.changepwMask) return;
    changepwShowError("");
    els.changepwCurrent.value = "";
    els.changepwNew.value = "";
    els.changepwConfirm.value = "";
    els.changepwMask.style.display = "";
    if (els.changepwCurrent.focus) { setTimeout(function () { els.changepwCurrent.focus(); }, 30); }
  }

  function changepwClose() {
    if (!els.changepwMask) return;
    els.changepwMask.style.display = "none";
  }

  function changepwSubmit() {
    var cur = els.changepwCurrent.value || "";
    var np = els.changepwNew.value || "";
    var cf = els.changepwConfirm.value || "";
    if (!cur || !np || !cf) {
      changepwShowError(tt("acct.changepw.err.required"));
      return;
    }
    if (np !== cf) {
      changepwShowError(tt("acct.changepw.err.mismatch"));
      return;
    }
    var btn = els.changepwSubmitBtn;
    if (btn) { btn.disabled = true; }
    apiFetch("/api/account/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: cur, new_password: np }),
    }).then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b, retry: r.headers.get("Retry-After") }; });
    }).then(function (res) {
      if (btn) { btn.disabled = false; }
      if (res.status === 200) {
        // 成功：全部 session 已失效（含本设备），跳登录页带提示参数
        location.href = "/login?password_changed=1";
        return;
      }
      var b = res.body || {};
      if (b.error === "invalid_current_password") {
        changepwShowError(tt("acct.changepw.err.current"));
        return;
      }
      if (res.status === 429) {
        changepwShowError(tt("acct.changepw.err.locked"));
        return;
      }
      if (b.error === "新密码不能与当前密码相同") {
        changepwShowError(tt("acct.changepw.err.same"));
        return;
      }
      // 其余（长度策略等）：直接展示服务端文案（与 store 口径一致）
      changepwShowError(b.error || tt("acct.changepw.err.generic"));
    }).catch(function () {
      if (btn) { btn.disabled = false; }
      changepwShowError(tt("acct.changepw.err.generic"));
    });
  }

  function initChangePw() {
    if (!els.changepwMask) return;
    els.changepwBtn.addEventListener("click", changepwOpen);
    els.changepwClose.addEventListener("click", changepwClose);
    els.changepwCancel.addEventListener("click", changepwClose);
    els.changepwMask.addEventListener("click", function (e) {
      if (e.target === els.changepwMask) { changepwClose(); }
    });
    els.changepwSubmitBtn.addEventListener("click", changepwSubmit);
    els.changepwConfirm.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { changepwSubmit(); }
    });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(function () { /* ignore */ });
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(ta);
    }
  }

  // ---------- user max_steps 只读同步（docs §8.3/§9.2，PT-3） ----------
  // AI 配置面板 DOM 在本页（index.html），但加载/保存逻辑归 HistoPilot 插件
  // bundle（config-panel.js）。平台侧只做轻量补充：use_platform 勾选时把步数
  // 输入框置只读并显示平台生效步数；切回自带 API 后恢复可编辑（默认 20）。
  // 服务端注入规则（_build_sidecar_config）才是权威，这里仅是 UI 提示。
  function syncAiMaxStepsInput() {
    var usePlatform = document.getElementById("ai-use-platform");
    var steps = document.getElementById("ai-max-steps");
    if (!usePlatform || !steps) return;
    var platformOn = usePlatform.checked && !usePlatform.disabled;
    if (platformOn) {
      if (!steps.readOnly) {
        // 暂存自带值，切回自带 API 时恢复
        if (steps.value && !steps.dataset.ownSteps) steps.dataset.ownSteps = steps.value;
        steps.readOnly = true;
        steps.title = tt("ai.field.maxsteps.platform.title");
      }
    } else if (steps.readOnly) {
      steps.readOnly = false;
      steps.title = tt("ai.field.maxsteps.title");
      if (steps.dataset.ownSteps) {
        steps.value = steps.dataset.ownSteps;
        delete steps.dataset.ownSteps;
      } else if (!steps.value) {
        steps.value = 20; // 自带 API 默认步数（docs §4.1）
      }
    }
  }

  function initAiMaxStepsSync() {
    document.addEventListener("change", function (e) {
      if (e.target && e.target.id === "ai-use-platform") syncAiMaxStepsInput();
    });
    // 插件 bundle 加载配置后不派发事件（只设 input.value），用低频轮询补一次
    // 初始只读状态；仅在面板存在且状态未同步时动作（幂等、开销可忽略）。
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      var usePlatform = document.getElementById("ai-use-platform");
      var steps = document.getElementById("ai-max-steps");
      if (!usePlatform || !steps || tries > 20) { clearInterval(timer); return; }
      if (usePlatform.checked) {
        syncAiMaxStepsInput();
        clearInterval(timer);
      }
    }, 1000);
  }

  // 缓存：全部切片、全部项目、全部分享
  var allSlides = [];      // [{name,width,height,mpp_x,...}]
  var allProjects = [];    // [{pid,name,note,slides,roi_count,...}]
  var currentAnnotations = null; // 当前切片的标注 {slide, annotations:[{label,count,items}]}
  var annoOverlays = [];   // 兼容旧引用，已不再新增（标注改画到 canvas）
  var annoPanelOpen = false;
  var allSharesCache = null; // 缓存分享列表，供语言切换时重渲

  // ---------- AI 读片助手状态（Stage 2：平台侧仅保留叠加层） ----------
  // AI 运行状态（aiConfig/aiRunning/aiSessionId/activeAiSession/mainAiCtx 等）已随
  // HistoPilot 插件 bundle 整体迁出（plugins/histopilot/ui/）。平台只保留叠加层：插件
  // 通过 HostBridge viewer.highlight 请求写入 aiOverlay，redrawAnnoCanvas 据此绘制。
  var aiOverlay = [];        // canvas 叠加：agent 的 bbox（goto/snapshot），由 HostBridge 写入
  // AI 判读区配色：在 H&E 粉白底上需高对比；外圈深色描边 + 琥珀色主色 + 标签底衬
  var AI_OVERLAY_FILL = "rgba(255, 149, 0, 0.14)";
  var AI_OVERLAY_STROKE = "#FF9500";
  var AI_OVERLAY_HALO = "rgba(0, 0, 0, 0.82)";
  var AI_ANNO_FILL = "rgba(255, 149, 0, 0.16)";   // 进标注库的 AI 标注填充（比人工略浓）

  // 编辑模式状态：选中/拖动（管理端所有标注可编辑）
  // editItem：flatItems 中的引用（可改本地几何）；editDrag：拖动会话
  // editing：是否处于「显式编辑态」（进入后画手柄、可拖动，防误挪位置）
  var editItem = null;
  var editDrag = null;
  var editing = false;

  // 临时选择器状态
  var pickerCtx = { targetPid: null, selected: {} };

  // 未归类勾选
  var slideChecked = {};   // 切片勾选状态（项目内 + 未归类统一，供分享/新建项目用）

  // 分享创建用的临时切片集（分享选中 / 项目分享）
  var sharePendingSlides = null; // 若非 null，则用此切片集创建分享

  // ---------- DOM ----------
  var viewer = null;
  function $(id) { return document.getElementById(id); }
  var els = {
    currentSlide: $("current-slide"),
    zoomIn: $("zoom-in"),
    zoomOut: $("zoom-out"),
    rotateBtn: $("rotate-btn"),
    flipBtn: $("flip-btn"),
    // 升级 C：单一矩形入口 + 紧凑设置区（旧 roi-6/roi-6-5/roi-box-btn 已移除）
    roiRectBtn: $("roi-rect-btn"),
    roiSettings: $("roi-settings"),
    roiWInput: $("roi-w-input"),
    roiHInput: $("roi-h-input"),
    roiUnitSelect: $("roi-unit-select"),
    roiLockRatio: $("roi-lock-ratio"),
    roiPresetSelect: $("roi-preset-select"),
    saveBtn: $("save-btn"),
    saveAnnoBtn: $("save-anno-btn"),
    annoBtn: $("anno-btn"),
    annoAllBtn: $("anno-all-btn"),
    annoArrowBtn: $("anno-arrow-btn"),
    annoFreeBtn: $("anno-free-btn"),
    annoLabelInput: $("anno-label-input"),
    annoCanvas: $("anno-canvas"),
    resetBtn: $("reset-btn"),
    mppSetter: $("mpp-setter"),
    mppInput: $("mpp-input"),
    mppSetBtn: $("mpp-set-btn"),
    zoomBadge: $("zoom-badge"),
    headerZoomBadge: $("header-zoom-badge"),
    zoomNative: $("zoom-native"),
    tbbMoreBtn: $("tbb-more-btn"),
    tbbMore: $("tbb-more"),
    tbbMoreAi: $("tbb-more-ai"),
    uploadBtn: $("upload-btn"),
    fileInput: $("file-input"),
    progressWrap: $("progress-wrap"),
    progressBar: $("progress-bar"),
    progressText: $("progress-text"),
    uploadProgressList: $("upload-progress-list"),
    viewerWrap: $("viewer-wrap"),
    dropOverlay: $("drop-overlay"),
    toastContainer: $("toast-container"),
    logoutBtn: $("logout-btn"),
    // 管理工作台入口（仅 owner 可见；PR5 后 /admin 的唯一 UI 入口）
    adminEntryLink: $("admin-entry-link"),
    // 修改我的密码（账户系统批次 A docs §8.1）
    changepwBtn: $("changepw-btn"),
    changepwMask: $("changepw-mask"),
    changepwClose: $("changepw-close"),
    changepwCancel: $("changepw-cancel"),
    changepwSubmitBtn: $("changepw-submit"),
    changepwCurrent: $("changepw-current"),
    changepwNew: $("changepw-new"),
    changepwConfirm: $("changepw-confirm"),
    changepwError: $("changepw-error"),
    annoAllToggle: $("anno-all-toggle"),
    // 手机端侧栏抽屉
    menuBtn: $("menu-btn"),
    sidebar: $("sidebar"),
    sidebarMask: $("sidebar-mask"),
    // 升级 A：切片搜索 + 无切片空态入口
    slideSearch: $("slide-search"),
    viewerEmpty: $("viewer-empty"),
    viewerEmptyPick: $("viewer-empty-pick"),
    // 项目
    newProjectBtn: $("new-project-btn"),
    newProjectForm: $("new-project-form"),
    npName: $("np-name"),
    npNote: $("np-note"),
    npConfirm: $("np-confirm"),
    npCancel: $("np-cancel"),
    projectList: $("project-list"),
    unfiledToggle: $("unfiled-toggle"),
    unfiledCount: $("unfiled-count"),
    unfiledBody: $("unfiled-body"),
    unfiledList: $("unfiled-list"),
    unfiledNewProject: $("unfiled-new-project"),
    unfiledShare: $("unfiled-share"),
    // 身份预览 banner（S4：预览态提示 + 退出按钮；用户管理 UI 已迁 admin 插件）
    previewBanner: $("preview-banner"),
    previewBannerText: $("preview-banner-text"),
    previewStopBtn: $("preview-stop-btn"),
    // 分享
    shareMgrToggle: $("share-mgr-toggle"),
    shareMgrBody: $("share-mgr-body"),
    shareExpiresSelect: $("share-expires-select"),
    shareExpiresCustom: $("share-expires-custom"),
    shareRoiSizeSelect: $("share-roi-size-select"),
    shareRectPolicySelect: $("share-rect-policy-select"),
    sharePermAnnotate: $("share-perm-annotate"),
    sharePermDownload: $("share-perm-download"),
    sharePermHint: $("share-perm-hint"),
    shareCreateBtn: $("share-create-btn"),
    shareResult: $("share-result"),
    shareResultUrl: $("share-result-url"),
    shareResultCopy: $("share-result-copy"),
    shareList: $("share-list"),
    // 用户管理（owner）
    // 切片选择器
    pickerMask: $("slide-picker-mask"),
    pickerTitleText: $("picker-title-text"),
    pickerClose: $("picker-close"),
    pickerList: $("picker-list"),
    pickerSelectedCount: $("picker-selected-count"),
    pickerConfirm: $("picker-confirm"),
    // 标注面板
    annoPanel: $("anno-panel"),
    annoPanelTitle: $("anno-panel-title"),
    annoPanelClose: $("anno-panel-close"),
    annoPanelList: $("anno-panel-list"),
    // AI 读片助手（Stage 2：仅保留触发按钮；面板 DOM 与逻辑归插件 bundle）
    aiBtn: $("ai-btn"),
    // 多通道通道着色（Batch 4；元素在 _app_shell.html，RGB/flag 关时保持隐藏）
    channelBtn: $("channel-btn"),
    rgbBadge: $("rgb-badge"),
    channelPanelHost: $("channel-panel"),
  };

  var roiBox = null;
  var dragInfo = null;
  // 底图缩略图层：铺在瓦片层后面的模糊预览，慢网下避免瓦片未到区域变白
  var baseThumbEl = null;

  // 多通道通道着色控制器（Batch 4；channel-controls.js 三页面共用，本页只接
  // adapter/权限。RGB 或 flag 关时 handleInfo 返回 legacy，行为与旧版一致）
  var channelCtrl = null;

  // ---------- 工具函数 ----------
  function toast(msg, type) {
    type = type || "info";
    var el = document.createElement("div");
    el.className = "toast " + type;
    el.textContent = msg;
    els.toastContainer.appendChild(el);
    setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 3000);
  }

  function fmtSize(bytes) {
    if (bytes == null) return "-";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
    if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + " MB";
    return (bytes / 1073741824).toFixed(2) + " GB";
  }

  function mppTagClass(src) { return src || "missing"; }

  function clamp(v, lo, hi) {
    if (hi < lo) hi = lo;
    return Math.max(lo, Math.min(hi, v));
  }

  function esc(s) {
    // 简易转义，用于 innerHTML 注入（标题等用户输入）
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // 标注徽章文本（如 "标记 3 · 2 人"）
  function annoBadgeText(slideName) {
    if (!allAnnotationsBySlide) return null;
    var grps = allAnnotationsBySlide[slideName];
    if (!grps || grps.length === 0) return null;
    var total = 0;
    var people = 0;
    grps.forEach(function (g) { total += g.count || 0; people += 1; });
    return t("badge.marks", { n: total, m: people });
  }

  // 文件名中间截断：保留首尾，中间用 … 连接
  function truncateMiddle(s, max) {
    s = String(s == null ? "" : s);
    if (!max || max < 6) max = 18;
    if (s.length <= max) return s;
    var head = Math.ceil((max - 1) / 2);
    var tail = Math.floor((max - 1) / 2);
    return s.slice(0, head) + "…" + s.slice(s.length - tail);
  }

  // 缓存 annotations_by_slide（从 /api/annotations 拉取全量后缓存）
  var allAnnotationsBySlide = null;
  function loadAnnotationsIndex() {
    return apiFetch("/api/annotations")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        allAnnotationsBySlide = data.by_slide || {};
      })
      .catch(function () { allAnnotationsBySlide = {}; });
  }

  // 某切片是否属于任一项目
  function isSlideInAnyProject(slideName) {
    for (var i = 0; i < allProjects.length; i++) {
      if (allProjects[i].slides && allProjects[i].slides.indexOf(slideName) >= 0) {
        return true;
      }
    }
    return false;
  }

  // label -> 颜色（哈希着色）
  function labelColor(label) {
    var s = String(label || "");
    var h = 0;
    for (var i = 0; i < s.length; i++) {
      h = (h * 31 + s.charCodeAt(i)) >>> 0;
    }
    var hue = h % 360;
    return { fill: "hsla(" + hue + ",70%,55%,0.18)", stroke: "hsl(" + hue + ",70%,45%)" };
  }

  // ---------- 初始化 OpenSeadragon ----------
  function initViewer() {
    if (window.HP_ViewerCore && HP_ViewerCore.create) {
      viewer = HP_ViewerCore.create($("viewer"));
    } else {
      viewer = OpenSeadragon({
        element: $("viewer"),
        showNavigationControl: false,
        imageLoaderLimit: 8,
        placeholderFillStyle: null,
        compositeOperation: "source-over",
        minZoomImageRatio: 0.5,
        maxZoomPixelRatio: 10,
        minPixelRatio: 0.4,
        defaultZoomLevel: 0,
        immediateRender: false,
        preload: false,
        wrapHorizontal: false,
        wrapVertical: false,
        preserveImageSizeOnResize: true,
        pixelsPerWheelLine: 40,
        gestureSettingsMouse: {
          scrollToZoom: true,
          clickToZoom: false,
          dblClickToZoom: true,
        },
        gestureSettingsTouch: {
          pinchToZoom: true,
          flickEnabled: false,
        },
        animationTime: 0.3,
        visibilityRatio: 0.1,
        prefixUrl: "",
      });
      viewer.container.style.backgroundColor = "#262a30";
    }
    viewer.addHandler("zoom", function () { updateZoomBadge(); syncBaseThumb(); });
    viewer.addHandler("open", onViewerOpen);
    // 底图随平移/缩放实时跟随（animation 每帧触发，跟随最平滑）
    viewer.addHandler("animation", function () { syncBaseThumb(); redrawAnnoCanvas(); });
    // 动画结束补画文本（标签/气泡）：动画期间为流畅省略了文本绘制
    viewer.addHandler("animation-finish", function () { redrawAnnoCanvas(); });
    viewer.addHandler("rotate", function () { syncBaseThumb(); redrawAnnoCanvas(); });
    // 镜像翻转：OSD 'flip' 事件 → 同步底图 transform / 重绘标注画布 / ROI 框重对位
    viewer.addHandler("flip", function () {
      state.flipped = !!viewer.viewport.getFlip();
      applyBaseThumbFlip();
      syncBaseThumb();
      redrawAnnoCanvas();
      updateRoiOverlay();
    });
    // 容器尺寸变化（窗口 resize、侧栏收起/展开、断点切换）：OSD 重算 viewport
    // 后触发；先同步画布背衬尺寸再重绘，保证标注/AI overlay 与新容器对齐（§4.2）
    viewer.addHandler("resize", function () {
      resizeAnnoCanvas();
      redrawAnnoCanvas();
    });
    // 切片关闭时清理旧底图
    viewer.addHandler("close", clearBaseThumb);
  }

  function onViewerOpen() {
    // 通道配色重开（同一切片换 TileSource，§8.2）：走轻量路径——只同步倍率
    // 徽章与底图缩略图；不退绘制模式、不重置标注/AI 面板、不向插件重发
    // slide.opened（避免换色清空 AI 会话 UI）。viewport 恢复由控制器负责。
    if (state.channelReopening) {
      state.channelReopening = false;
      updateZoomBadge();
      syncBaseThumb();
      return;
    }
    updateZoomBadge();
    // 打开后把底图缩略图对齐到当前视口
    syncBaseThumb();
    // 打开新切片：退出绘制模式、清面板、重置标注画布尺寸
    exitDrawMode();
    resizeAnnoCanvas();
    els.annoBtn.disabled = true;
    els.annoAllBtn.disabled = true;
    els.annoPanel.style.display = "none";
    annoPanelOpen = false;
    // AI 助手：启用触发按钮（插件停用时 aiBtn 不渲染，跳过；平台人工读片不受影响）
    if (els.aiBtn) els.aiBtn.disabled = false;
    if (els.tbbMoreAi) els.tbbMoreAi.disabled = false;  // ⋯ 面板里的 AI 钮同步
    syncAnnoAllBtns();
    state.showAnno = false;
    state.focusAnno = null;
    // 平台侧清叠加层（切片隔离）。插件侧会话 UI/SSE/游标由 slide.opened event 触发其自行 reset+restore。
    aiOverlay = [];
    redrawAnnoCanvas();
    var slideName = state.slide ? state.slide.name : null;
    if (slideName && state.slide) {
      hpEmit("slide.opened", { slide: {
        name: slideName, width: state.slide.width, height: state.slide.height,
        mppX: state.slide.mppX, mppY: state.slide.mppY,
      } });
    }
    if (state.slide) {
      // 管理员标注工具在任意打开的切片上可用（箭头/描图不依赖 mpp）
      els.annoArrowBtn.disabled = false;
      els.annoFreeBtn.disabled = false;
      // 拉取该切片标注
      apiFetch("/api/annotations?slide=" + encodeURIComponent(state.slide.name))
        .then(function (r) { return r.json(); })
        .then(function (data) {
          currentAnnotations = data;
          var annos = data.annotations || [];
          if (annos.length > 0) {
            els.annoBtn.disabled = false;
            els.annoAllBtn.disabled = false;
          }
          editItem = null;
          state.focusAnno = null;
          editing = false;
          rebuildFlatItems();
          redrawAnnoCanvas();
        })
        .catch(function () { currentAnnotations = null; editItem = null; state.focusAnno = null; editing = false; rebuildFlatItems(); redrawAnnoCanvas(); });
    } else {
      els.annoArrowBtn.disabled = true;
      els.annoFreeBtn.disabled = true;
      redrawAnnoCanvas();
    }
  }

  // 把"图像缩放比"换算成读片软件常用的物镜等效倍率（如 20× / 40×）。
  // 约定屏幕 96 DPI（1 屏像素 ≈ 25400/96 µm）；缺 mpp 时无法换算，回退百分比。
  function formatMag(mag) {
    // 全片概览时屏显等效倍率会到天文数字（无物理意义），缩写为 k 避免撑爆徽章
    if (mag >= 1000000) return (mag / 1000000).toFixed(1).replace(/\.0$/, "") + "M×";
    if (mag >= 10000) return Math.round(mag / 1000) + "k×";
    if (mag >= 10) return Math.round(mag) + "×";
    if (mag >= 1) return mag.toFixed(1) + "×";
    return mag.toFixed(2) + "×";
  }
  // AI 轨迹里的倍率：可能是数字（需格式化）或已带单位的字符串（如 "20x (high power)"）
  function fmtAiMag(mag) {
    if (mag === null || mag === undefined || mag === "") return "";
    if (typeof mag === "string") return mag;  // 已格式化（如 ai_agent 的 magnification_label）
    var m = Number(mag);
    if (!isFinite(m)) return String(mag);
    return (m >= 10 ? Math.round(m) : m.toFixed(1)) + "x";
  }
  function updateZoomBadge() {
    var text = (window.HP_ViewerCore && HP_ViewerCore.zoomText)
      ? HP_ViewerCore.zoomText(viewer, state.mppX)
      : "—";
    if (text === "—" && !(window.HP_ViewerCore && HP_ViewerCore.zoomText)) {
      try {
        if (viewer && viewer.viewport && viewer.source) {
          var zoom = viewer.viewport.getZoom(true);
          var containerW = viewer.viewport.getContainerSize().x;
          var imgW = viewer.source.dimensions.x;
          var imageZoom = (zoom * containerW) / imgW;
          var mpp = state.mppX;
          if (mpp && mpp > 0 && imageZoom > 0) {
            text = formatMag(imageZoom * (10 / mpp));
          } else {
            text = Math.round(imageZoom * 100) + "%";
          }
        }
      } catch (e) { /* 保持 — */ }
    }
    if (els.zoomBadge) els.zoomBadge.textContent = text;
    if (els.headerZoomBadge) els.headerZoomBadge.textContent = text;
  }

  // ---------- 底图缩略图层（慢网下瓦片未到区域的模糊预览） ----------
  function applyBaseThumbFlip() {
    if (!baseThumbEl) return;
    baseThumbEl.style.transformOrigin = "center";
    baseThumbEl.style.transform = state.flipped ? "scaleX(-1)" : "";
  }


  function clearBaseThumb() {
    if (baseThumbEl) {
      if (baseThumbEl.parentNode) baseThumbEl.parentNode.removeChild(baseThumbEl);
      baseThumbEl = null;
    }
  }

  function syncBaseThumb() {
    if (!baseThumbEl || !viewer || !viewer.viewport || !state.slide) return;
    var W = state.slide.width, H = state.slide.height;
    if (!W || !H) return;
    try {
      var tl = viewer.viewport.imageToViewerElementCoordinates(new OpenSeadragon.Point(0, 0));
      var br = viewer.viewport.imageToViewerElementCoordinates(new OpenSeadragon.Point(W, H));
      var left = Math.min(tl.x, br.x);
      var top = Math.min(tl.y, br.y);
      var width = Math.abs(br.x - tl.x);
      var height = Math.abs(br.y - tl.y);
      baseThumbEl.style.left = left + "px";
      baseThumbEl.style.top = top + "px";
      baseThumbEl.style.width = width + "px";
      baseThumbEl.style.height = height + "px";
      // 仅当旋转角为 0/180 时显示底图，避免 90/270 错位（瓦片本身正常旋转显示）
      baseThumbEl.style.display = (state.rotation % 180 === 0) ? "block" : "none";
    } catch (e) {}
  }

  // ---------- 多通道通道着色（Batch 4；channel-controls.js 三页面共用） ----------
  // localStorage 用户作用域（§8.3）：登录用户区分（预览中为被预览用户），
  // 本机免登录统一 local
  function userScope() {
    return "official:" + (currentUserId || "local");
  }

  function createChannelController() {
    return HP_Channels.createChannelController({
      adapter: window.HP_API,
      viewer: viewer,
      button: els.channelBtn,
      badge: els.rgbBadge,
      panelHost: els.channelPanelHost,
      t: t,
      toast: toast,
      storage: window.localStorage,
      // 通道重开（同一切片换配色，§8.2）：onViewerOpen 走轻量路径
      onReopening: function () { state.channelReopening = true; },
      onReopened: function () { syncBaseThumb(); redrawAnnoCanvas(); },
      // render 计划的打开统一由控制器发起（含 409 刷新 info 重建路径）；
      // legacy 计划由 openSlide 走原 DZI 路径
      open: function (plan) {
        if (viewer && plan && plan.tileSource) viewer.open(plan.tileSource);
      },
      // 底图缩略图与屏幕瓦片同 token（§4.4）；viewer.close 清掉后这里重建
      setThumbnail: function (url) {
        if (!url) return;
        if (!baseThumbEl && viewer && viewer.container) {
          baseThumbEl = document.createElement("img");
          baseThumbEl.className = "osd-base-thumb";
          baseThumbEl.alt = "";
          viewer.container.insertBefore(baseThumbEl, viewer.canvas);
          applyBaseThumbFlip();
        }
        if (baseThumbEl) baseThumbEl.src = url;
      },
      // 409 slide_revision_conflict：只刷新 info 并重建一次（§6.3）
      refreshInfo: function () {
        if (!state.slide) return Promise.resolve(null);
        var adapter = window.HP_API;
        var url = (adapter && adapter.slideInfoUrl)
          ? adapter.slideInfoUrl(state.slide.name)
          : "/api/slide/" + encodeURIComponent(state.slide.name) + "/info";
        return apiFetch(url).then(function (r) { return r.json(); });
      },
    });
  }

  // ---------- 打开切片 ----------
  function openSlide(name) {
    // 切换切片前移除旧底图
    clearBaseThumb();
    var adapter = window.HP_API;
    var url = (adapter && adapter.slideInfoUrl)
      ? adapter.slideInfoUrl(name)
      : "/api/slide/" + encodeURIComponent(name) + "/info";
    apiFetch(url)
      .then(function (r) { return r.json(); })
      .then(function (info) {
        if (info.error) { toast(t("open.fail", { e: info.error }), "error"); return; }
        state.slide = {
          name: info.name,
          width: info.width,
          height: info.height,
          mppX: info.mpp_x,
          mppY: info.mpp_y,
          mppSource: info.mpp_source,
        };
        state.mppX = info.mpp_x;
        state.rotation = 0;
        // 升级 A：切片已打开，隐藏无切片空态入口
        updateViewerEmptyState();
        els.currentSlide.textContent = info.alias || info.name;
        els.currentSlide.title = info.name + (info.note ? " · " + info.note : "");
        updateMppSetterVisibility();
        exitRoi();
        // 创建底图缩略图层：铺在瓦片 canvas 之前（下层），慢网下透出模糊预览
        // （src 由通道控制器 setThumbnail 或 legacy 路径填充）
        baseThumbEl = document.createElement("img");
        baseThumbEl.className = "osd-base-thumb";
        baseThumbEl.alt = "";
        viewer.container.insertBefore(baseThumbEl, viewer.canvas);
        applyBaseThumbFlip();
        // 多通道通道着色：共用组件按 info 决定 inline custom TileSource（携带
        // render token）或原 DZI 路径；RGB / flag 关返回 legacy，行为不变。
        // render 计划的 viewer.open 由控制器经 opts.open 完成（409 刷新等
        // 路径同入口）；本函数只负责 legacy 打开。
        var plan = null;
        if (window.HP_Channels) {
          channelCtrl = channelCtrl || createChannelController();
          plan = channelCtrl.handleInfo(info, {
            id: info.slide_id || info.name,
            scope: userScope(),
          });
        }
        if (!plan || plan.kind !== "render") {
          baseThumbEl.src = (adapter && adapter.thumbnailUrl)
            ? adapter.thumbnailUrl(name)
            : "/api/slide/" + encodeURIComponent(name) + "/thumbnail";
          viewer.open((adapter && adapter.dziUrl)
            ? adapter.dziUrl(name)
            : "/api/slide/" + encodeURIComponent(name) + ".dzi");
        }
        // 高亮列表项（未归类与项目切片行）
        document.querySelectorAll(".slide-row").forEach(function (it) {
          it.classList.toggle("active", it.dataset.name === name);
        });
        // 手机端：打开切片后自动收起侧栏抽屉，让用户立刻看到查看器；
        // 收起后走统一布局同步（抽屉关闭 + viewer resize 链，§4.2）。
        // 桌面端保持当前收起/展开偏好，不打断读片。
        if (isMobileWidth()) sidebarCtrl.closeDrawer();
      })
      .catch(function (e) { toast(t("open.info.fail", { e: e }), "error"); });
  }

  // ---------- mpp 设置区显示控制 ----------
  function updateMppSetterVisibility() {
    if (!state.slide) { els.mppSetter.style.display = "none"; return; }
    var src = state.slide.mppSource;
    if (src === "missing" || src === "estimated") {
      els.mppSetter.style.display = "flex";
      els.mppInput.value = state.mppX != null ? state.mppX : "";
    } else {
      els.mppSetter.style.display = "none";
    }
  }

  // ---------- 缩放 / 旋转 / 复位 ----------
  function zoomIn() {
    if (!viewer || !viewer.viewport) return;
    viewer.viewport.zoomBy(1.4);
    viewer.viewport.applyConstraints();
  }
  function zoomOut() {
    if (!viewer || !viewer.viewport) return;
    viewer.viewport.zoomBy(1 / 1.4);
    viewer.viewport.applyConstraints();
  }
  // 1:1 原始像素（F3）：口径唯一在 HP_ViewerCore.zoomToNative；其未加载时才本地兜底
  function zoomNative() {
    if (window.HP_ViewerCore && HP_ViewerCore.zoomToNative) {
      HP_ViewerCore.zoomToNative(viewer);
      return;
    }
    try {
      if (!viewer || !viewer.viewport || !viewer.source) return;
      var vp = viewer.viewport;
      vp.zoomTo(viewer.source.dimensions.x / vp.getContainerSize().x, vp.getCenter());
      vp.applyConstraints();
    } catch (e) { /* 忽略 */ }
  }
  function rotate() {
    if (!viewer || !viewer.viewport) return;
    state.rotation = (state.rotation + 90) % 360;
    viewer.viewport.setRotation(state.rotation);
    updateRoiOverlay();
    redrawAnnoCanvas();
  }
  function flip() {
    if (!viewer || !viewer.viewport || !viewer.viewport.toggleFlip) return;
    viewer.viewport.toggleFlip();
    // 'flip' 事件负责同步 state/各层；toggleFlip 可能未触发事件时兜底
    state.flipped = !!viewer.viewport.getFlip();
    applyBaseThumbFlip();
    syncBaseThumb();
    redrawAnnoCanvas();
    updateRoiOverlay();
  }
  function reset() {
    if (!viewer || !viewer.viewport) return;
    state.rotation = 0;
    viewer.viewport.setRotation(0);
    // 复位时取消镜像（回到默认朝向）
    if (viewer.viewport.getFlip && viewer.viewport.getFlip()) {
      viewer.viewport.toggleFlip();
    }
    viewer.viewport.goHome(true);
  }

  // ---------- 矩形工具（升级 C §6.1/§6.2） ----------
  // 权威几何：level-0 像素 x/y/w/h（state.roi）。物理单位仅是输入/展示口径：
  //   w_px = round(width × 1000 / mpp_x)、h_px = round(height × 1000 / mpp_y)
  //   （μm 不乘 1000）；显示值按实际像素 + 分轴校准反算。
  // 单边上限 40000px + w*h 像素预算（与后端同一口径）；出界保持上个合法框。

  // rect 单边像素上限（与后端 RECT_MAX_SIDE_PX 一致）
  var RECT_MAX_SIDE_PX = 40000;
  // rect 像素预算（与后端 crop_guard.CROP_MAX_PIXELS 默认一致；服务端为权威）
  var RECT_MAX_PIXELS = 4096 * 4096;

  function rectToolActive() { return state.roiMode === "rect"; }
  function roiW() { return state.roi ? state.roi.w : 0; }
  function roiH() { return state.roi ? state.roi.h : 0; }

  // 数值是否可用（有限且 > 0）
  function posNum(v) { return typeof v === "number" && isFinite(v) && v > 0; }

  // 当前单位 → 像素的换算系数（分轴 MPP，§6.2）。返回 [kx, ky]（px per unit）
  // 或 null（物理单位缺任一轴可信 MPP：像素可用、物理单位需先校准）。
  function unitToPxFactors(unit) {
    if (unit === "px") return [1, 1];
    var mx = Number(state.mppX);
    var my = Number(state.slide && state.slide.mppY);
    if (unit === "mm") { mx = mx / 1000; my = my / 1000; } // µm/px → mm/px
    if (!posNum(mx) || !posNum(my)) return null;
    return [1 / mx, 1 / my]; // px per (mm|µm)
  }

  // 把设置区数值（当前单位）换算为像素 w/h；非法/缺 MPP 返回 null（附错误文案 key）
  function rectInputsToPx() {
    var unit = state.roiUnit;
    var wv = parseFloat(els.roiWInput && els.roiWInput.value);
    var hv = parseFloat(els.roiHInput && els.roiHInput.value);
    if (!isFinite(wv) || !isFinite(hv) || wv <= 0 || hv <= 0) return null;
    if (unit === "px") return { w: Math.round(wv), h: Math.round(hv) };
    var f = unitToPxFactors(unit);
    if (!f) return null;
    return { w: Math.round(wv * f[0]), h: Math.round(hv * f[1]) };
  }

  // 像素 → 物理展示串（分轴反算；mm 保留 2 位、μm 保留 1 位、px 原样）
  function rectPhysicalText(w, h) {
    var unit = state.roiUnit;
    if (unit === "px") return w + " × " + h + " px";
    var mx = Number(state.mppX), my = Number(state.slide && state.slide.mppY);
    if (!posNum(mx) || !posNum(my)) return w + " × " + h + " px";
    var wx = w * mx, hy = h * my; // µm
    if (unit === "mm") {
      return (wx / 1000).toFixed(2) + " × " + (hy / 1000).toFixed(2) + " mm";
    }
    return wx.toFixed(1) + " × " + hy.toFixed(1) + " μm";
  }

  // 矩形几何归一（§6.2）：负方向归一、整数、边界约束
  // x>=0,y>=0,w>=1,h>=1,x+w<=W,y+h<=H。非法输入（NaN/Infinity/负值）返回 null。
  // applyBudget=false 用于已存大标注的拖动编辑（像素预算是创建/导出闸，
  // 不追溯限制既有合法记录的编辑）。
  function normalizeRect(x0, y0, x1, y1, applyBudget) {
    var W = state.slide.width, H = state.slide.height;
    if (![x0, y0, x1, y1].every(isFinite)) return null;
    var x = Math.max(0, Math.round(Math.min(x0, x1)));
    var y = Math.max(0, Math.round(Math.min(y0, y1)));
    var w = Math.round(Math.abs(x1 - x0));
    var h = Math.round(Math.abs(y1 - y0));
    w = Math.min(Math.max(w, 1), RECT_MAX_SIDE_PX);
    h = Math.min(Math.max(h, 1), RECT_MAX_SIDE_PX);
    x = Math.min(x, Math.max(0, W - w));
    y = Math.min(y, Math.max(0, H - h));
    if (x < 0 || y < 0 || w < 1 || h < 1 || x + w > W || y + h > H) return null;
    if (applyBudget !== false && w * h > RECT_MAX_PIXELS) return null;
    return { x: x, y: y, w: w, h: h };
  }

  // 已有矩形的等比换算（锁定比例时拖边/输数用）：按高度推宽度
  function lockRatioAdjust(wPx, hPx) {
    if (!state.roiLockRatio || !(state.roi.w > 0) || !(state.roi.h > 0)) {
      return { w: wPx, h: hPx };
    }
    var ratio = state.roi.w / state.roi.h;
    // 以较大变化轴为准推另一轴
    if (Math.abs(hPx - state.roi.h) > Math.abs(wPx - state.roi.w)) {
      return { w: Math.max(1, Math.round(hPx * ratio)), h: hPx };
    }
    return { w: wPx, h: Math.max(1, Math.round(wPx / ratio)) };
  }

  // 在给定中心放置 w×h（clamp 到切片内，保大小移到边缘）
  function placeRectAtCenter(cx, cy, w, h) {
    var W = state.slide.width, H = state.slide.height;
    var x = clamp(Math.round(cx - w / 2), 0, Math.max(0, W - w));
    var y = clamp(Math.round(cy - h / 2), 0, Math.max(0, H - h));
    state.roi.x = x; state.roi.y = y; state.roi.w = w; state.roi.h = h;
  }

  // 进入/退出矩形工具（与箭头/自由手绘互斥；完成/取消后恢复导航）
  function toggleRectTool() {
    if (rectToolActive()) { exitRoi(); return; }
    if (!state.slide) { toast(t("roi.need.slide"), "error"); return; }
    var mx = Number(state.mppX), my = Number(state.slide.mppY);
    if (state.roiUnit !== "px" && (!posNum(mx) || !posNum(my))) {
      toast(t("roi.need.mpp"), "error");
      return;
    }
    if (state.slide.mppSource === "estimated") {
      toast(t("roi.estimate.tip"), "info");
    }
    // 与 arrow/freehand 互斥
    exitDrawMode();
    state.roiMode = "rect";
    state.roi = { x: 0, y: 0, w: 0, h: 0 };
    els.annoCanvas.classList.add("drawing");
    els.roiRectBtn.classList.add("active");
    if (els.roiSettings) els.roiSettings.hidden = false;
    els.roiRectBtn.setAttribute("aria-expanded", "true");
    if (viewer) viewer.setMouseNavEnabled(false);
    updateRoiButtons();
    updateCtxBar();
    toast(t("roi.rect.tip"), "info");
  }

  function exitRoi() {
    state.roiMode = null;
    if (roiBox && viewer && viewer.currentOverlays) {
      try { viewer.removeOverlay(roiBox); } catch (e) {}
    }
    if (roiBox && roiBox.parentNode) roiBox.parentNode.removeChild(roiBox);
    roiBox = null;
    if (els.annoCanvas) els.annoCanvas.classList.remove("drawing");
    if (els.roiRectBtn) {
      els.roiRectBtn.classList.remove("active");
      els.roiRectBtn.setAttribute("aria-expanded", "false");
    }
    if (viewer) viewer.setMouseNavEnabled(true);
    updateRoiButtons();
    els.saveBtn.disabled = true;
    els.saveAnnoBtn.disabled = true;
    updateCtxBar();
  }

  function updateRoiButtons() {
    if (els.roiRectBtn) {
      els.roiRectBtn.classList.toggle("active", rectToolActive());
    }
    syncRoiSettings();
  }

  // 紧凑设置区：数值随选区/单位同步（显示按实际像素反算，§6.2）
  function syncRoiSettings() {
    if (!els.roiWInput || !els.roiHInput) return;
    if (!(state.roi.w > 0)) return; // 无选区时保留用户输入
    var unit = state.roiUnit;
    if (unit === "px") {
      els.roiWInput.value = state.roi.w;
      els.roiHInput.value = state.roi.h;
      return;
    }
    var mx = Number(state.mppX), my = Number(state.slide && state.slide.mppY);
    if (!posNum(mx) || !posNum(my)) return;
    if (unit === "mm") {
      els.roiWInput.value = +(state.roi.w * mx / 1000).toFixed(2);
      els.roiHInput.value = +(state.roi.h * my / 1000).toFixed(2);
    } else {
      els.roiWInput.value = +(state.roi.w * mx).toFixed(1);
      els.roiHInput.value = +(state.roi.h * my).toFixed(1);
    }
  }

  // 设置区数值确认（Enter/change）：以当前中心调整；出界保持上个合法框并提示
  function applyRectInputs() {
    if (!state.slide || !rectToolActive()) return;
    var px = rectInputsToPx();
    if (!px) {
      var unit = state.roiUnit;
      if (unit !== "px" && !unitToPxFactors(unit)) { toast(t("roi.need.mpp"), "error"); }
      else { toast(t("roi.input.invalid"), "error"); }
      syncRoiSettings();
      return;
    }
    px = lockRatioAdjust(px.w, px.h);
    var W = state.slide.width, H = state.slide.height;
    if (px.w > W || px.h > H) { toast(t("roi.input.invalid"), "error"); syncRoiSettings(); return; }
    if (px.w * px.h > RECT_MAX_PIXELS) {
      toast(t("roi.too.large", { n: RECT_MAX_PIXELS }), "error");
      syncRoiSettings();
      return;
    }
    var cx = state.roi.x + state.roi.w / 2;
    var cy = state.roi.y + state.roi.h / 2;
    if (!(state.roi.w > 0)) { cx = W / 2; cy = H / 2; }
    placeRectAtCenter(cx, cy, px.w, px.h);
    createRoiBox();
    updateRoiOverlay();
    els.saveBtn.disabled = false;
    els.saveAnnoBtn.disabled = false;
  }

  function createRoiBox() {
    if (roiBox) return;
    roiBox = document.createElement("div");
    roiBox.id = "roi-box";
    // 四角（双轴）+ 四边（单轴）手柄（§6.1：边改单轴、角改双轴）
    ["tl", "tr", "bl", "br", "t", "b", "l", "r"].forEach(function (id) {
      var hd = document.createElement("div");
      hd.className = "roi-handle roi-" + id;
      hd.dataset.handle = id;
      roiBox.appendChild(hd);
    });
    var label = document.createElement("div");
    label.className = "roi-label";
    roiBox.appendChild(label);
    roiBox.addEventListener("pointerdown", onRoiPointerDown);
    viewer.container.appendChild(roiBox);
  }

  function updateRoiOverlay() {
    if (!roiBox || !state.slide) return;
    var r = state.roi;
    if (!(r.w > 0) || !(r.h > 0)) return;
    var label = roiBox.querySelector(".roi-label");
    if (label) label.textContent = rectPhysicalText(r.w, r.h);
    var rect = viewer.viewport.imageToViewportRectangle(r.x, r.y, r.w, r.h);
    var existing = viewer.getOverlayById(roiBox);
    if (existing) {
      viewer.updateOverlay(roiBox, rect, OpenSeadragon.Placement.TOP_LEFT);
    } else {
      var opts = { element: roiBox, location: rect,
                   placement: OpenSeadragon.Placement.TOP_LEFT };
      if (state.rotation % 360 !== 0 && OpenSeadragon.OverlayRotationMode &&
          OpenSeadragon.OverlayRotationMode.BOUNDING_BOX) {
        opts.rotationMode = OpenSeadragon.OverlayRotationMode.BOUNDING_BOX;
      }
      viewer.addOverlay(opts);
    }
    syncRoiSettings();
  }

  // ---------- 矩形拖拽：内部平移 / 四边单轴 / 四角双轴（pointer 捕获） ----------
  function onRoiPointerDown(e) {
    if (!state.slide) return;
    e.preventDefault(); e.stopPropagation();
    try { roiBox.setPointerCapture(e.pointerId); } catch (err) {}
    var handle = null;
    var t = e.target;
    if (t && t.dataset && t.dataset.handle) handle = t.dataset.handle;
    dragInfo = {
      pointerId: e.pointerId,
      handle: handle || "move",
      startRoi: { x: state.roi.x, y: state.roi.y, w: state.roi.w, h: state.roi.h },
      startImg: viewer.viewport.viewerElementToImageCoordinates(
        new OpenSeadragon.Point(e.clientX - getViewerRect().left,
                                e.clientY - getViewerRect().top)),
    };
    viewer.setMouseNavEnabled(false);
    roiBox.addEventListener("pointermove", onRoiPointerMove);
    roiBox.addEventListener("pointerup", onRoiPointerUp);
    roiBox.addEventListener("pointercancel", onRoiPointerUp);
  }

  function onRoiPointerMove(e) {
    if (!dragInfo) return;
    e.preventDefault(); e.stopPropagation();
    var rect = getViewerRect();
    var curImg = viewer.viewport.viewerElementToImageCoordinates(
      new OpenSeadragon.Point(e.clientX - rect.left, e.clientY - rect.top));
    var s = dragInfo.startRoi;
    var handle = dragInfo.handle;
    var W = state.slide.width, H = state.slide.height;
    var nx = s.x, ny = s.y, nw = s.w, nh = s.h;
    if (handle === "move") {
      // 平移：保大小，clamp 到边界（到边缘保留大小）
      var dx = Math.round(curImg.x - dragInfo.startImg.x);
      var dy = Math.round(curImg.y - dragInfo.startImg.y);
      nx = clamp(s.x + dx, 0, Math.max(0, W - s.w));
      ny = clamp(s.y + dy, 0, Math.max(0, H - s.h));
    } else {
      // 边/角：锚定对边/对角，跟随指针（负方向随后归一，§6.2）
      var anchorX = s.x + s.w, anchorY = s.y + s.h; // 默认锚右/下
      var moveL = handle.indexOf("l") >= 0;
      var moveR = handle.indexOf("r") >= 0;
      var moveT = handle.indexOf("t") >= 0;
      var moveB = handle.indexOf("b") >= 0;
      if (moveL) anchorX = s.x + s.w;
      else if (moveR) anchorX = s.x;
      if (moveT) anchorY = s.y + s.h;
      else if (moveB) anchorY = s.y;
      var x0 = moveL || moveR ? anchorX : s.x;
      var x1 = moveL || moveR ? curImg.x : s.x + s.w;
      var y0 = moveT || moveB ? anchorY : s.y;
      var y1 = moveT || moveB ? curImg.y : s.y + s.h;
      var n = normalizeRect(x0, y0, x1, y1);
      if (!n) return; // 非法（NaN/超预算）保持上帧
      nx = n.x; ny = n.y; nw = n.w; nh = n.h;
    }
    state.roi.x = nx; state.roi.y = ny; state.roi.w = nw; state.roi.h = nh;
    viewer.updateOverlay(
      roiBox,
      viewer.viewport.imageToViewportRectangle(nx, ny, nw, nh),
      OpenSeadragon.Placement.TOP_LEFT
    );
    syncRoiSettings();
  }

  function onRoiPointerUp(e) {
    if (!dragInfo) return;
    e.preventDefault(); e.stopPropagation();
    try { roiBox.releasePointerCapture(dragInfo.pointerId); } catch (err) {}
    roiBox.removeEventListener("pointermove", onRoiPointerMove);
    roiBox.removeEventListener("pointerup", onRoiPointerUp);
    roiBox.removeEventListener("pointercancel", onRoiPointerUp);
    dragInfo = null;
    viewer.setMouseNavEnabled(true);
  }

  function getViewerRect() { return viewer.container.getBoundingClientRect(); }

  // ---------- 画布层拖出矩形 / 点选中心放置（矩形工具激活时） ----------
  var rectDrawInfo = null;

  function onRectCanvasPointerDown(e) {
    if (!rectToolActive() || !state.slide) return false;
    e.preventDefault(); e.stopPropagation();
    var c = els.annoCanvas;
    try { c.setPointerCapture(e.pointerId); } catch (err) {}
    var img0 = screenToImg(e);
    rectDrawInfo = { pointerId: e.pointerId, x0: img0.x, y0: img0.y, x1: img0.x, y1: img0.y, moved: false };
    viewer.setMouseNavEnabled(false);
    return true;
  }

  function onRectCanvasPointerMove(e) {
    if (!rectDrawInfo) return false;
    e.preventDefault(); e.stopPropagation();
    var img = screenToImg(e);
    if (Math.abs(img.x - rectDrawInfo.x0) + Math.abs(img.y - rectDrawInfo.y0) > 2) {
      rectDrawInfo.moved = true;
    }
    rectDrawInfo.x1 = img.x; rectDrawInfo.y1 = img.y;
    if (rectDrawInfo.moved) {
      var n = normalizeRect(rectDrawInfo.x0, rectDrawInfo.y0,
                            rectDrawInfo.x1, rectDrawInfo.y1);
      if (n) {
        state.roi = n;
        createRoiBox();
        updateRoiOverlay();
      }
    }
    return true;
  }

  function onRectCanvasPointerUp(e) {
    if (!rectDrawInfo) return false;
    e.preventDefault(); e.stopPropagation();
    var c = els.annoCanvas;
    try { c.releasePointerCapture(rectDrawInfo.pointerId); } catch (err) {}
    var info = rectDrawInfo;
    rectDrawInfo = null;
    if (!info.moved) {
      // 点击放置：需要设置区已给出有效宽/高（§6.1「先输入大小，再点击中心放置」）
      var px = rectInputsToPx();
      if (!px) {
        var unit = state.roiUnit;
        if (unit !== "px" && !unitToPxFactors(unit)) { toast(t("roi.need.mpp"), "error"); }
        else { toast(t("roi.input.invalid"), "error"); }
        viewer.setMouseNavEnabled(true);
        return true;
      }
      px = lockRatioAdjust(px.w, px.h);
      if (px.w > state.slide.width || px.h > state.slide.height ||
          px.w * px.h > RECT_MAX_PIXELS) {
        toast(t("roi.input.invalid"), "error");
        viewer.setMouseNavEnabled(true);
        return true;
      }
      var img = screenToImg(e);
      placeRectAtCenter(img.x, img.y, px.w, px.h);
      createRoiBox();
      updateRoiOverlay();
    }
    if (state.roi.w > 0) {
      els.saveBtn.disabled = false;
      els.saveAnnoBtn.disabled = false;
    }
    viewer.setMouseNavEnabled(true);
    return true;
  }

  // Escape 取消未保存选区（§6.1）：恢复 viewer 导航
  function onRectKeydown(e) {
    if (e.key !== "Escape") return;
    if (rectDrawInfo) {
      rectDrawInfo = null;
      if (viewer) viewer.setMouseNavEnabled(true);
      return;
    }
    if (rectToolActive()) {
      e.preventDefault();
      exitRoi();
      toast(t("roi.cancelled"), "info");
    }
  }

  // 已有矩形标注的 w/h 读取（升级 C：v2 w/h 权威；旧 side_px 正方形兼容）。
  // 不重新正方形化：非正方形取各自轴，绝不 max/min 冒充。
  function rectItemW(it) {
    var w = Number(it.w);
    if (isFinite(w) && w > 0) return w;
    return Number(it.side_px) > 0 ? Number(it.side_px) : 0;
  }
  function rectItemH(it) {
    var h = Number(it.h);
    if (isFinite(h) && h > 0) return h;
    return Number(it.side_px) > 0 ? Number(it.side_px) : 0;
  }

  // 标注的物理尺寸展示（分轴反算；AI 落标 size_mm 常为 0 → 用 mpp 现算）
  function rectItemSizeText(it) {
    var w = rectItemW(it), h = rectItemH(it);
    if (!(w > 0) || !(h > 0)) return "";
    var mx = Number(state.mppX), my = Number(state.slide && state.slide.mppY);
    if (!posNum(mx) || !posNum(my)) {
      var mm0 = Number(it.size_mm);
      return (mm0 > 0 && w === h) ? (mm0 + "mm") : "";
    }
    var wx = w * mx, hy = h * my; // µm
    return (wx / 1000).toFixed(2) + "×" + (hy / 1000).toFixed(2) + "mm";
  }

  // ---------- 保存图片（裁剪） ----------
  function saveCrop() {
    if (!state.slide || !rectToolActive()) return;
    var r = state.roi;
    var name = state.slide.name;
    // Batch 4（§4.4）：多通道切片 crop 与屏幕瓦片同 render_token（服务端用同
    // 一 context 合成，且像素预算按启用通道数计）；RGB/legacy 不带参数
    var token = (channelCtrl && channelCtrl.isMultichannel())
      ? channelCtrl.getToken() : null;
    var adapter = window.HP_API;
    // 升级 C（§6.3-5）：v2 矩形走 x/y/w/h（输出尺寸精确等于 w/h）
    var url = (adapter && adapter.cropUrl)
      ? adapter.cropUrl(name, Math.round(r.x), Math.round(r.y), Math.round(r.w), token)
      : "/api/slide/" + encodeURIComponent(name) +
        "/crop?x=" + Math.round(r.x) + "&y=" + Math.round(r.y) +
        "&w=" + Math.round(r.w) + "&h=" + Math.round(r.h) +
        (token ? "&render=" + encodeURIComponent(token) : "");
    var fp8 = ((channelCtrl && channelCtrl.getFingerprint()) || "").slice(0, 8);
    var multi = !!(channelCtrl && channelCtrl.isMultichannel() && token);
    var originalText = els.saveBtn.textContent;
    els.saveBtn.textContent = t("export.busy");
    els.saveBtn.disabled = true;
    apiFetch(url)
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (j) {
            throw new Error(j.error || (t("export.fail") + " " + res.status));
          });
        }
        return res.blob();
      })
      .then(function (blob) {
        var stem = name.replace(/\.[^.]+$/, "");
        // 下载文件名与后端 Content-Disposition 同形：含宽高 + 多通道 fp 前 8 位
        var fname = stem + "_x" + Math.round(r.x) + "_y" + Math.round(r.y) +
          "_" + Math.round(r.w) + "x" + Math.round(r.h) + "px" +
          (multi && fp8 ? "_fp" + fp8 : "") + ".png";
        var a = document.createElement("a");
        var objUrl = URL.createObjectURL(blob);
        a.href = objUrl; a.download = fname;
        document.body.appendChild(a); a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(objUrl); }, 1000);
        // 确认文案明确「导出当前伪彩合成图」，不冒充原始科学数据（§3.2/§4.4）
        toast(multi
          ? t("export.pseudo.done", { name: fname })
          : t("export.done", { name: fname }), "success");
      })
      .catch(function (e) { toast(t("export.fail2", { s: e.message }), "error"); })
      .finally(function () {
        els.saveBtn.textContent = originalText;
        els.saveBtn.disabled = !rectToolActive();
      });
  }

  // ---------- 保存矩形选区为标注（管理员 rect 标注，v2 成对 w/h） ----------
  function saveAnno() {
    if (!state.slide || !rectToolActive()) return;
    var r = state.roi;
    var label = (els.annoLabelInput.value || "").trim() || t("anno.default.user");
    var body = {
      slide: state.slide.name,
      type: "rect",
      label: label,
      x: Math.round(r.x),
      y: Math.round(r.y),
      w: Math.round(r.w),
      h: Math.round(r.h),
      shared: false,
      note: "",
    };
    els.saveAnnoBtn.disabled = true;
    apiFetch("/api/annotation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (res) {
        if (!res.ok) return res.json().then(function (j) {
          throw new Error(j.error || (t("save.fail") + " " + res.status));
        });
        return res.json();
      })
      .then(function () {
        toast(t("anno.saved.tip"), "success");
        refreshCurrentAnnotations();
        loadAnnotationsIndex().then(function () {
          renderProjects(allProjects);
          renderUnfiled();
        });
      })
      .catch(function (e) { toast(t("save.fail2", { e: e.message }), "error"); })
      .finally(function () {
        els.saveAnnoBtn.disabled = !rectToolActive();
      });
  }

  // ---------- 手动设置 mpp（等轴校准：显式操作，来源标记 manual） ----------
  function setMpp() {
    var v = parseFloat(els.mppInput.value);
    if (!isFinite(v) || v <= 0) { toast(t("mpp.invalid"), "error"); return; }
    state.mppX = v;
    if (state.slide) {
      state.slide.mppX = v;
      // 升级 C（§6.2）：单一输入的等轴校准是显式选择——两轴同值并标 manual
      state.slide.mppY = v;
      state.slide.mppSource = "manual";
    }
    // 升级 C（§6.2）：MPP/校准更新不重写已有标注（及当前选区）的像素范围，
    // 只更新物理显示——refresh 显示层即可。
    updateRoiOverlay();
    updateMppSetterVisibility();
    toast(t("mpp.set.ok", { v: v }), "success");
  }

  // =========================================================================
  // 项目渲染与管理
  // =========================================================================
  function loadAll() {
    // 并行加载切片、项目、分享、标注索引（AI 配置由 HistoPilot 插件 bundle 自行加载）
    return Promise.all([
      fetch("/api/slides").then(function (r) { return r.json(); }),
      fetch("/api/projects").then(function (r) { return r.json(); }),
      fetch("/api/share/list").then(function (r) { return r.json(); }),
      loadAnnotationsIndex(),
    ]).then(function (results) {
      allSlides = results[0] || [];
      allProjects = results[1] || [];
      renderProjects(allProjects);
      renderUnfiled();
      renderShareList((results[2] && results[2].shares) || []);
    }).catch(function (e) {
      toast(t("load.fail", { e: e }), "error");
    });
  }

  function reloadProjectsAndUnfiled() {
    return Promise.all([
      fetch("/api/projects").then(function (r) { return r.json(); }),
      fetch("/api/slides").then(function (r) { return r.json(); }),
      loadAnnotationsIndex(),
    ]).then(function (results) {
      allProjects = results[0] || [];
      allSlides = results[1] || [];
      renderProjects(allProjects);
      renderUnfiled();
    });
  }

  function reloadShares() {
    return apiFetch("/api/share/list")
      .then(function (r) { return r.json(); })
      .then(function (data) { renderShareList((data && data.shares) || []); });
  }

  // 渲染单个切片信息块（用于项目行、未归类项、选择器项）
  // 行式版：纯文本 "宽×高 · mpp x.xx"，估算值带 *
  function slideMetaTags(s) {
    var parts = [];
    if (s.width && s.height) {
      parts.push(s.width + "×" + s.height);
    }
    if (s.mpp_x != null) {
      // mpp 保留 3 位小数，避免副行过长被截断
      var mpp = Math.round(s.mpp_x * 1000) / 1000;
      parts.push("mpp " + mpp + (s.mpp_source === "estimated" ? "*" : ""));
    } else {
      parts.push(t("slide.mpp.missing"));
    }
    return parts.join(" · ");
  }

  function renderProjects(projects) {
    els.projectList.innerHTML = "";
    if (!projects || projects.length === 0) {
      var empty = document.createElement("div");
      empty.className = "proj-empty";
      empty.textContent = t("proj.empty");
      els.projectList.appendChild(empty);
      return;
    }
    var renderTail = function () {
      // 升级 A：列表重渲后重放当前搜索条件（搜索条件不因收起/重渲丢失）
      if (els.slideSearch) applySlideFilter(els.slideSearch.value);
    };
    projects.forEach(function (p) {
      var row = document.createElement("div");
      row.className = "proj-row";
      row.dataset.pid = p.pid;

      var slideCount = p.slide_count != null ? p.slide_count : (p.slides || []).length;
      var roiCount = p.roi_count || 0;

      // 头部行：chevron + 图标 + 名称/副行 + 计数 + 操作
      var head = document.createElement("div");
      head.className = "proj-head";

      var chevron = document.createElement("span");
      chevron.className = "chevron";
      chevron.textContent = "▸";
      chevron.title = t("proj.chevron");
      head.appendChild(chevron);

      var icon = document.createElement("span");
      icon.className = "icon";
      icon.textContent = "📁";
      head.appendChild(icon);

      var main = document.createElement("div");
      main.className = "ph-main";
      var nameEl = document.createElement("div");
      nameEl.className = "proj-name";
      nameEl.textContent = p.name || t("proj.unnamed");
      var meta = document.createElement("div");
      meta.className = "proj-meta";
      meta.textContent = t("proj.meta", { s: slideCount, r: roiCount }) +
        (p.note ? " · " + p.note : "");
      main.appendChild(nameEl);
      main.appendChild(meta);
      head.appendChild(main);

      var countBadge = document.createElement("span");
      countBadge.className = "proj-count";
      countBadge.textContent = String(slideCount);
      head.appendChild(countBadge);

      // 操作按钮（hover 浮现）
      var ops = document.createElement("div");
      ops.className = "proj-ops";
      function opBtn(cls, glyph, title) {
        var b = document.createElement("button");
        b.className = "proj-op " + cls;
        b.textContent = glyph; b.title = title || "";
        return b;
      }
      var shareBtn = opBtn("po-share", "↗", t("proj.op.share"));
      var editBtn = opBtn("po-edit", "✎", t("proj.op.edit"));
      var addBtn = opBtn("po-add", "＋", t("proj.op.add"));
      var delBtn = opBtn("po-del", "🗑", t("proj.op.del"));
      ops.appendChild(shareBtn);
      ops.appendChild(editBtn);
      ops.appendChild(addBtn);
      ops.appendChild(delBtn);
      head.appendChild(ops);
      row.appendChild(head);

      shareBtn.addEventListener("click", function (e) { e.stopPropagation(); shareProject(p); });
      editBtn.addEventListener("click", function (e) { e.stopPropagation(); editProject(p); });
      addBtn.addEventListener("click", function (e) { e.stopPropagation(); openSlidePicker(p.pid, p.name); });
      delBtn.addEventListener("click", function (e) { e.stopPropagation(); deleteProject(p); });

      // 展开体：切片行
      var body = document.createElement("div");
      body.className = "proj-body";
      (p.slides || []).forEach(function (sname) {
        body.appendChild(renderSlideRow(sname, false));
      });
      row.appendChild(body);

      // 点击头部（chevron 或名称区）展开/收起
      function toggleExpand(e) {
        if (e.target.closest(".proj-ops")) return; // 操作按钮不触发展开
        row.classList.toggle("expanded");
      }
      chevron.addEventListener("click", toggleExpand);
      main.addEventListener("click", toggleExpand);
      countBadge.addEventListener("click", toggleExpand);

      els.projectList.appendChild(row);
    });
    renderTail();
  }

  // 切片行（项目展开体内 / 未归类）。unfiled=true 时显示复选框。
  function renderSlideRow(sname, unfiled) {
    var sinfo = findSlideInfo(sname);
    var row = document.createElement("div");
    row.className = "slide-row";
    row.dataset.name = sname;
    if (state.slide && state.slide.name === sname) row.classList.add("active");

    // 所有切片行（项目内 + 未归类）都带复选框，可勾选用于分享/新建项目
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "slide-check";
    cb.title = t("proj.cb.title");
    if (slideChecked[sname]) cb.checked = true;
    cb.addEventListener("click", function (ev) { ev.stopPropagation(); });
    cb.addEventListener("change", function () { slideChecked[sname] = cb.checked; });
    row.appendChild(cb);

    var mid = document.createElement("div");
    mid.className = "slide-mid";
    var failed = (sinfo && sinfo.error) || (!sinfo);
    var alias = (sinfo && sinfo.alias) || "";

    // 第一行：别名优先（无别名则截断文件名）+ 标注 pill，第二行：meta
    var top = document.createElement("div");
    top.className = "slide-top";
    var nameEl = document.createElement("span");
    nameEl.className = "slide-name";
    if (alias) {
      nameEl.classList.add("alias-first");
      nameEl.innerHTML = esc(alias) +
        '<span class="alias-filename">' + esc(truncateMiddle(sname, 20)) + "</span>";
      nameEl.title = sname + (failed ? t("slide.read.fail") : "");
    } else {
      nameEl.textContent = truncateMiddle(sname, 24) + (failed ? t("slide.read.fail.short") : "");
    }
    top.appendChild(nameEl);

    // 标注 pill
    var badgeText = annoBadgeText(sname);
    if (badgeText) {
      var badge = document.createElement("button");
      badge.className = "anno-pill";
      badge.textContent = badgeText;
      badge.title = t("slide.anno.badge.title");
      badge.addEventListener("click", function (e) {
        e.stopPropagation();
        openSlide(sname);
        // 打开后自动展开标注面板
        setTimeout(function () { openAnnoPanel(); }, 600);
      });
      top.appendChild(badge);
    }
    mid.appendChild(top);

    var meta = document.createElement("div");
    meta.className = "slide-meta";
    var metaParts = [];
    if (sinfo) {
      metaParts.push(slideMetaTags(sinfo));
      if (unfiled && sinfo.size_bytes) metaParts.push(fmtSize(sinfo.size_bytes));
      if (sinfo.note) metaParts.push('<span class="sm-note">' + esc(sinfo.note) + "</span>");
    } else {
      metaParts.push(t("slide.not.found"));
    }
    meta.innerHTML = metaParts.join(" · ");
    mid.appendChild(meta);
    row.appendChild(mid);

    // 别名/备注编辑钮（hover 浮现）
    var editBtn = document.createElement("button");
    editBtn.className = "slide-edit";
    editBtn.textContent = "✎";
    editBtn.title = t("slide.op.alias");
    editBtn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      enterSlideMetaEdit(row, sname, sinfo);
    });
    row.appendChild(editBtn);

    // 单独分享按钮（hover 浮现）：直接分享这一张，无需勾选
    var shareBtn = document.createElement("button");
    shareBtn.className = "slide-share";
    shareBtn.textContent = "↗";
    shareBtn.title = t("slide.op.share");
    shareBtn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      doCreateShare([sname]);
    });
    row.appendChild(shareBtn);

    // 删除按钮（hover 浮现）
    var delBtn = document.createElement("button");
    delBtn.className = "slide-del";
    delBtn.textContent = "×";
    delBtn.title = t("slide.op.del");
    delBtn.addEventListener("click", function (ev) { ev.stopPropagation(); deleteSlide(sname); });
    row.appendChild(delBtn);

    // Demo 目录按钮（仅 owner；加入后无需登录即可从互联网访问，docs §5.1）
    var demoBtn = document.createElement("button");
    demoBtn.className = "slide-demo";
    demoBtn.type = "button";
    demoBtn.dataset.name = sname;
    demoBtn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      toggleDemoCatalog(sname);
    });
    row.appendChild(demoBtn);
    updateDemoBtn(demoBtn, sname);

    row.addEventListener("click", function () { openSlide(sname); });
    return row;
  }

  // ---------- Demo 目录（owner allowlist，PT-4 docs §5.1） ----------
  var demoCatalogNames = {};
  var demoCatalogLoaded = false;

  function loadDemoCatalog() {
    apiFetch("/api/admin/demo-catalog").then(function (r) {
      if (!r.ok) return null; // json/dual 503 / 非 owner 403：按钮维持隐藏
      return r.json();
    }).then(function (data) {
      if (!data) return;
      demoCatalogNames = {};
      (data.slides || []).forEach(function (s) {
        if (s && s.name) demoCatalogNames[s.name] = true;
      });
      demoCatalogLoaded = true;
      document.querySelectorAll(".slide-demo").forEach(function (btn) {
        updateDemoBtn(btn, btn.dataset.name);
      });
    }).catch(function () { /* 目录状态读失败：按钮保持隐藏（fail-closed） */ });
  }

  function updateDemoBtn(btn, name) {
    if (!btn) return;
    if (!demoCatalogLoaded) { btn.style.display = "none"; return; }
    var inCatalog = !!demoCatalogNames[name];
    btn.style.display = "";
    btn.textContent = inCatalog ? "▣" : "▢";
    btn.title = inCatalog ? t("demo.catalog.remove") : t("demo.catalog.add");
    btn.setAttribute("aria-pressed", inCatalog ? "true" : "false");
  }

  function toggleDemoCatalog(name) {
    var inCatalog = !!demoCatalogNames[name];
    var confirmKey = inCatalog ? "demo.catalog.remove.confirm" : "demo.catalog.add.confirm";
    if (!confirm(t(confirmKey, { name: name }))) return;
    var req = inCatalog
      ? apiFetch("/api/admin/demo-catalog?slide=" + encodeURIComponent(name), { method: "DELETE" })
      : apiFetch("/api/admin/demo-catalog", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ slide: name }),
        });
    req.then(function (r) {
      return r.json().then(function (b) { return { ok: r.ok, body: b || {} }; },
                            function () { return { ok: r.ok, body: {} }; });
    }).then(function (res) {
      if (!res.ok) {
        toast(t("demo.catalog.fail", { e: res.body.error || "HTTP " }), "error");
        return;
      }
      toast(t(inCatalog ? "demo.catalog.done.remove" : "demo.catalog.done.add",
              { name: name }), "success");
      loadDemoCatalog(); // 重新拉权威状态（PUT/DELETE 响应不含展示名集合）
    }).catch(function (e) {
      toast(t("demo.catalog.fail", { e: (e && e.message) || e }), "error");
    });
  }

  // 行内别名/备注编辑态
  function enterSlideMetaEdit(row, sname, sinfo) {
    if (!row) return;
    var alias0 = (sinfo && sinfo.alias) || "";
    var note0 = (sinfo && sinfo.note) || "";
    // 清空行内容，替换为编辑表单
    row.innerHTML = "";
    row.classList.add("editing");
    row.removeEventListener("click", openSlide);
    var form = document.createElement("div");
    form.className = "slide-edit-form";
    var aInput = document.createElement("input");
    aInput.type = "text"; aInput.maxLength = 60; aInput.placeholder = t("edit.alias.ph");
    aInput.value = alias0;
    var nInput = document.createElement("input");
    nInput.type = "text"; nInput.maxLength = 200; nInput.placeholder = t("edit.note.ph");
    nInput.value = note0;
    var actions = document.createElement("div");
    actions.className = "sef-actions";
    var okBtn = document.createElement("button");
    okBtn.className = "btn primary small"; okBtn.textContent = t("edit.confirm");
    var cancelBtn = document.createElement("button");
    cancelBtn.className = "btn secondary small"; cancelBtn.textContent = t("edit.cancel");
    actions.appendChild(okBtn); actions.appendChild(cancelBtn);
    form.appendChild(aInput); form.appendChild(nInput); form.appendChild(actions);
    row.appendChild(form);
    aInput.focus();

    function commit() {
      var alias = aInput.value;
      var note = nInput.value;
      apiFetch("/api/slide/" + encodeURIComponent(sname) + "/meta", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alias: alias, note: note }),
      })
        .then(function (r) {
          if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("save.fail")); });
          return r.json();
        })
        .then(function () {
          toast(t("common.updated"), "success");
          reloadProjectsAndUnfiled();
        })
        .catch(function (e) { toast(t("save.fail2", { e: e.message }), "error"); });
    }
    okBtn.addEventListener("click", function (e) { e.stopPropagation(); commit(); });
    cancelBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      reloadProjectsAndUnfiled();
    });
    aInput.addEventListener("keydown", function (e) { if (e.key === "Enter") nInput.focus(); });
    nInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.stopPropagation(); commit(); }
      if (e.key === "Escape") { e.stopPropagation(); reloadProjectsAndUnfiled(); }
    });
  }

  function findSlideInfo(name) {
    for (var i = 0; i < allSlides.length; i++) {
      if (allSlides[i].name === name) return allSlides[i];
    }
    return null;
  }

  // ---------- 未归类切片 ----------
  function renderUnfiled() {
    var unfiled = allSlides.filter(function (s) { return !isSlideInAnyProject(s.name); });
    els.unfiledCount.textContent = String(unfiled.length);
    els.unfiledList.innerHTML = "";
    if (unfiled.length === 0) {
      var empty = document.createElement("div");
      empty.className = "unfiled-empty";
      empty.textContent = t("unfiled.empty");
      els.unfiledList.appendChild(empty);
      return;
    }
    unfiled.forEach(function (s) {
      els.unfiledList.appendChild(renderSlideRow(s.name, true));
    });
    // 升级 A：列表重渲后重放当前搜索条件（搜索条件不因收起/重渲丢失）
    if (els.slideSearch) applySlideFilter(els.slideSearch.value);
  }

  // ---------- 新建项目 ----------
  // 待加入新建项目的切片（来自未归类勾选）；表单确认时带上
  var pendingNewProjectSlides = null;

  function toggleNewProjectForm(show) {
    els.newProjectForm.style.display = show ? "block" : "none";
    if (show) { els.npName.value = ""; els.npNote.value = ""; els.npName.focus(); }
  }

  // slidesArg 为显式传入的切片（如顶部"新建项目"为空数组）；
  // 为空时回退到 pendingNewProjectSlides（未归类勾选预填）
  function createProjectFromForm(slidesArg) {
    var slides = (slidesArg && slidesArg.length) ? slidesArg : (pendingNewProjectSlides || []);
    var name = (els.npName.value || "").trim();
    if (!name) { toast(t("newproj.need.name"), "error"); els.npName.focus(); return; }
    var note = els.npNote.value || "";
    apiFetch("/api/project/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, note: note, slides: slides }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("newproj.create.fail")); });
        return r.json();
      })
      .then(function () {
        toast(t("newproj.created"), "success");
        toggleNewProjectForm(false);
        pendingNewProjectSlides = null;
        slideChecked = {};
        reloadProjectsAndUnfiled();
      })
      .catch(function (e) { toast(t("newproj.create.fail2", { e: e.message }), "error"); });
  }

  // ---------- 编辑项目 ----------
  function editProject(p) {
    var name = prompt(t("rename.name.prompt"), p.name || "");
    if (name == null) return;
    var note = prompt(t("rename.note.prompt"), p.note || "");
    if (note == null) return;
    apiFetch("/api/project/" + encodeURIComponent(p.pid), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), note: note }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("rename.update.fail")); });
        return r.json();
      })
      .then(function () { toast(t("common.updated"), "success"); reloadProjectsAndUnfiled(); })
      .catch(function (e) { toast(t("rename.update.fail2", { e: e.message }), "error"); });
  }

  // ---------- 删除项目 ----------
  function deleteProject(p) {
    if (!confirm(t("delproj.confirm", { name: (p.name || "") }))) return;
    apiFetch("/api/project/" + encodeURIComponent(p.pid), { method: "DELETE" })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("delproj.fail")); });
        return r.json();
      })
      .then(function () { toast(t("delproj.deleted"), "success"); reloadProjectsAndUnfiled(); })
      .catch(function (e) { toast(t("delproj.fail2", { e: e.message }), "error"); });
  }

  // =========================================================================
  // 切片选择器（添加切片到项目）
  // =========================================================================
  function openSlidePicker(pid, pname) {
    pickerCtx.targetPid = pid;
    pickerCtx.selected = {};
    els.pickerTitleText.textContent = pname
      ? t("picker.title.with", { name: pname })
      : t("picker.title");
    els.pickerList.innerHTML = "";
    allSlides.forEach(function (s) {
      var row = document.createElement("label");
      row.className = "picker-item";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = s.name;
      cb.addEventListener("change", function () {
        pickerCtx.selected[s.name] = cb.checked;
        updatePickerCount();
      });
      row.appendChild(cb);
      var info = document.createElement("span");
      info.className = "pi-info";
      var nameHtml = s.alias
        ? '<span class="pi-alias">' + esc(s.alias) + "</span>" +
          '<span class="alias-filename">' + esc(truncateMiddle(s.name, 24)) + "</span>"
        : esc(truncateMiddle(s.name, 30));
      info.innerHTML = '<span class="pi-name">' + nameHtml + "</span>" +
        '<span class="pi-meta">' + slideMetaTags(s) + "</span>";
      row.appendChild(info);
      els.pickerList.appendChild(row);
    });
    updatePickerCount();
    els.pickerMask.style.display = "flex";
  }

  function updatePickerCount() {
    var n = 0;
    Object.keys(pickerCtx.selected).forEach(function (k) { if (pickerCtx.selected[k]) n++; });
    els.pickerSelectedCount.textContent = t("picker.selected", { n: n });
  }

  function closeSlidePicker() {
    els.pickerMask.style.display = "none";
    pickerCtx.targetPid = null;
    pickerCtx.selected = {};
  }

  function confirmSlidePicker() {
    var slides = Object.keys(pickerCtx.selected).filter(function (k) { return pickerCtx.selected[k]; });
    if (slides.length === 0) { toast(t("picker.need.slide"), "error"); return; }
    var pid = pickerCtx.targetPid;
    if (!pid) return;
    apiFetch("/api/project/" + encodeURIComponent(pid) + "/slides", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slides: slides }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("picker.add.fail")); });
        return r.json();
      })
      .then(function () {
        toast(t("picker.added", { n: slides.length }), "success");
        closeSlidePicker();
        reloadProjectsAndUnfiled();
      })
      .catch(function (e) { toast(t("picker.add.fail2", { e: e.message }), "error"); });
  }

  // =========================================================================
  // 分享功能
  // =========================================================================
  function getExpiresHours() {
    var v = els.shareExpiresSelect.value;
    if (v === "custom") {
      var c = parseFloat(els.shareExpiresCustom.value);
      if (!isFinite(c) || c <= 0) return null;
      return c;
    }
    return parseFloat(v);
  }

  // 读取"标记尺寸"下拉值 → roi_sizes 数组（[6,6.5]/[6]/[6.5]）
  function getShareRoiSizes() {
    var v = els.shareRoiSizeSelect ? els.shareRoiSizeSelect.value : "both";
    if (v === "6") return [6];
    if (v === "6.5") return [6.5];
    return [6, 6.5];
  }

  // 读取分享链接权限（docs §8.3：显式选择，view 为基线，不无提示默认 annotate）
  function getSharePermissions() {
    var perms = ["view"];
    if (els.sharePermAnnotate && els.sharePermAnnotate.checked) { perms.push("annotate"); }
    if (els.sharePermDownload && els.sharePermDownload.checked) { perms.push("download"); }
    return perms;
  }

  // roi_sizes 数组 → 人类可读标签（用于分享列表 meta）
  function roiSizesLabel(sizes) {
    if (!sizes || !sizes.length) return "6/6.5mm";
    var set = {};
    sizes.forEach(function (s) { set[Number(s)] = true; });
    if (set[6] && set[6.5]) return "6/6.5mm";
    if (set[6.5]) return t("share.size.only.6.5");
    if (set[6]) return t("share.size.only.6");
    return "6/6.5mm";
  }

  // 统一创建分享入口：slides 为要分享的切片名数组
  function doCreateShare(slides) {
    if (!slides || slides.length === 0) { toast(t("share.need.slide"), "error"); return; }
    var hours = getExpiresHours();
    if (hours == null) { toast(t("share.need.hours"), "error"); return; }
    var roiSizes = getShareRoiSizes();
    var permissions = getSharePermissions();
    // 升级 C（§6.4）：矩形策略档位（preset_only 缺省；custom 显式选择）
    var rectPolicy = (els.shareRectPolicySelect && els.shareRectPolicySelect.value)
      || "preset_only";
    els.shareCreateBtn.disabled = true;
    els.shareCreateBtn.textContent = t("share.creating");
    apiFetch("/api/share/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        slides: slides, expires_hours: hours, roi_sizes: roiSizes,
        permissions: permissions, rect_policy: rectPolicy,
      }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || (t("share.create.fail") + " " + r.status)); });
        return r.json();
      })
      .then(function (data) {
        els.shareResult.style.display = "flex";
        els.shareResultUrl.value = data.url;
        copyText(data.url);
        toast(t("share.created"), "success");
        sharePendingSlides = null;
        slideChecked = {};
        renderUnfiled();
        reloadShares();
      })
      .catch(function (e) { toast(t("share.create.fail2", { s: e.message }), "error"); })
      .finally(function () {
        els.shareCreateBtn.disabled = false;
        els.shareCreateBtn.textContent = t("sb.share.create");
      });
  }

  // 分享本项目
  function shareProject(p) {
    var slides = p.slides || [];
    if (slides.length === 0) { toast(t("share.project.empty"), "error"); return; }
    sharePendingSlides = slides.slice();
    // 展开分享管理区，预填提示
    var shareSec = els.shareMgrBody.closest(".section");
    if (shareSec) shareSec.classList.remove("collapsed");
    els.shareCreateBtn.textContent = t("share.project.btn", { name: (p.name || ""), n: slides.length });
    toast(t("share.project.selected.tip", { n: slides.length }), "info");
    els.shareResult.style.display = "none";
  }

  // 分享管理区按钮：若有 sharePendingSlides 则用它，否则用未归类勾选
  function onShareCreateClick() {
    var slides;
    if (sharePendingSlides) {
      slides = sharePendingSlides;
    } else {
      slides = Object.keys(slideChecked).filter(function (k) { return slideChecked[k]; });
    }
    doCreateShare(slides);
  }

  // 未归类"分享选中"
  function onUnfiledShare() {
    var slides = Object.keys(slideChecked).filter(function (k) { return slideChecked[k]; });
    if (slides.length === 0) { toast(t("unfiled.need.check"), "error"); return; }
    doCreateShare(slides);
  }

  function renderShareList(shares) {
    allSharesCache = shares || [];
    els.shareList.innerHTML = "";
    if (!shares || shares.length === 0) {
      var empty = document.createElement("div");
      empty.className = "share-empty";
      empty.textContent = t("share.empty");
      els.shareList.appendChild(empty);
      return;
    }
    shares.forEach(function (sh) {
      var row = document.createElement("div");
      row.className = "share-row-item";

      // 状态彩色圆点
      var dot = document.createElement("span");
      dot.className = "sr-status-dot " + sh.status;
      dot.title = sh.status === "active" ? t("share.status.active") :
                  (sh.status === "expired" ? t("share.status.expired") : t("share.status.revoked"));
      row.appendChild(dot);

      // 中部：token（等宽） + 副行 meta
      var mid = document.createElement("div");
      mid.className = "sr-mid";
      var shortTok = sh.token.length > 8 ? sh.token.slice(0, 8) : sh.token;
      var tokEl = document.createElement("span");
      tokEl.className = "sr-token";
      tokEl.textContent = shortTok;
      tokEl.title = sh.url;
      mid.appendChild(tokEl);

      var meta = document.createElement("span");
      meta.className = "sr-meta";
      var slidesTxt = t("share.slides.tip", { n: sh.slides.length, list: sh.slides.join(", ") });
      meta.innerHTML =
        '<span title="' + esc(slidesTxt) + '">' + sh.slides.length + " " + esc(t("share.slides.unit")) + "</span>" +
        '<span class="sr-sep">·</span>' +
        "<span>" + esc(t("share.expires", { e: fmtExpire(sh.expires_at) })) + "</span>" +
        '<span class="sr-sep">·</span>' +
        "<span>" + esc(t("share.rois", { n: (sh.roi_count || 0) })) + "</span>" +
        '<span class="sr-sep">·</span>' +
        "<span>" + esc(roiSizesLabel(sh.roi_sizes)) + "</span>";
      mid.appendChild(meta);
      row.appendChild(mid);

      // 操作按钮（hover 浮现）
      var ops = document.createElement("div");
      ops.className = "sr-ops";
      var copyBtn = document.createElement("button");
      copyBtn.className = "sr-btn sr-copy";
      copyBtn.textContent = "⧉";
      copyBtn.title = t("share.copy.title");
      copyBtn.addEventListener("click", function () { copyText(sh.url); });
      ops.appendChild(copyBtn);
      var revBtn = document.createElement("button");
      revBtn.className = "sr-btn sr-revoke";
      revBtn.textContent = "⊘";
      revBtn.title = t("share.revoke.title");
      revBtn.addEventListener("click", function () { revokeShare(sh.token); });
      if (sh.status !== "active") revBtn.disabled = true;
      ops.appendChild(revBtn);
      row.appendChild(ops);
      els.shareList.appendChild(row);
    });
  }

  function revokeShare(token) {
    if (!confirm(t("share.revoke.confirm"))) return;
    apiFetch("/api/share/revoke", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || (t("share.revoke.fail") + " " + r.status)); });
        return r.json();
      })
      .then(function () { toast(t("share.revoked"), "success"); reloadShares(); })
      .catch(function (e) { toast(t("share.revoke.fail2", { s: e.message }), "error"); });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text)
        .then(function () { toast(t("share.copied"), "success"); })
        .catch(function () { fallbackCopy(text); });
    } else { fallbackCopy(text); }
  }
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast(t("share.copied"), "success"); }
    catch (e) { toast(t("share.copy.fail"), "error"); }
    document.body.removeChild(ta);
  }
  function fmtExpire(ts) {
    if (!ts) return "-";
    var d = new Date(ts * 1000);
    var p = function (n) { return n < 10 ? "0" + n : n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  // =========================================================================
  // 侧栏开合控制器（升级 A §4.1/§4.2）
  // -------------------------------------------------------------------------
  // 桌面（>768px）：默认收起；顶部 #menu-btn 在展开/收起间切换。偏好按
  // 「站点:账号」维度写 localStorage（pt.sb.v1| 前缀，与通道配色 pt.rc.v1 的
  // userScope 同一身份口径）；读取失败/无偏好默认收起；存储不可用不阻塞。
  // 手机（≤768px）：维持侧滑抽屉，默认关闭；桌面偏好不把抽屉自动打开。
  // 纯逻辑集中在 createSidebarController(deps)：DOM/mq/storage 经 deps 注入，
  // vitest（tests/js/sidebar-layout.test.ts）以假元素驱动真实决策逻辑。
  // =========================================================================
  var SIDEBAR_PREF_PREFIX = "pt.sb.v1|";
  var SB_MOBILE_QUERY = "(max-width: 768px)";

  function sidebarPrefKey(scope) {
    return SIDEBAR_PREF_PREFIX + String(scope || "anonymous");
  }
  // 解析存储的偏好：结构不符/非 JSON 一律返回 null（调用方回落默认收起）
  function parseSidebarPref(raw) {
    if (raw == null) return null;
    try {
      var v = JSON.parse(raw);
      if (v && typeof v === "object" && typeof v.collapsed === "boolean") {
        return { collapsed: v.collapsed };
      }
    } catch (e) { /* 损坏数据视为无偏好 */ }
    return null;
  }
  function readSidebarPref(storage, scope) {
    if (!storage || typeof storage.getItem !== "function") return null;
    try {
      return parseSidebarPref(storage.getItem(sidebarPrefKey(scope)));
    } catch (e) { return null; }  // 隐私模式等 getItem 抛错：无偏好
  }
  function writeSidebarPref(storage, scope, collapsed) {
    if (!storage || typeof storage.setItem !== "function") return;
    try {
      storage.setItem(sidebarPrefKey(scope),
        JSON.stringify({ collapsed: !!collapsed, t: Date.now() }));
    } catch (e) { /* 配额/隐私模式写失败：仅失去持久化 */ }
  }

  function createSidebarController(deps) {
    var sidebar = deps.sidebar;
    var mask = deps.sidebarMask;
    var btn = deps.menuBtn;
    var collapsed = true;      // 桌面意图态（首次进入默认收起）
    var userTouched = false;   // 身份到位回填偏好时不得覆盖用户已做的切换

    function scopeName() {
      return (typeof deps.scope === "function") ? deps.scope() : (deps.scope || "anonymous");
    }
    function isMobile() {
      return !!(deps.mq && typeof deps.mq.matches === "boolean" && deps.mq.matches);
    }
    // 按钮 a11y 状态：桌面 expanded=侧栏可见；手机 expanded=抽屉打开
    function setBtnState(expanded) {
      if (!btn || !btn.setAttribute) return;
      btn.setAttribute("aria-expanded", String(!!expanded));
      var label = t(expanded ? "tb.sidebar.collapse" : "tb.sidebar.expand");
      btn.setAttribute("aria-label", label);
      btn.title = label;
    }
    function applyDesktop() {
      if (sidebar && sidebar.classList) sidebar.classList.toggle("collapsed", collapsed);
      setBtnState(!collapsed);
      if (typeof deps.onLayoutChange === "function") deps.onLayoutChange();
    }
    function drawerOpen() {
      return !!(sidebar && sidebar.classList && sidebar.classList.contains("open"));
    }
    function applyDrawer(open) {
      open = !!open;
      if (sidebar && sidebar.classList) sidebar.classList.toggle("open", open);
      if (mask && mask.classList) mask.classList.toggle("open", open);
      setBtnState(open);
      // a11y：抽屉关闭后焦点若留在抽屉内（将被移出视口/不可达），回到触发按钮
      var doc = deps.doc;
      if (!open && doc && doc.activeElement && sidebar && sidebar.contains &&
          sidebar.contains(doc.activeElement) && btn && typeof btn.focus === "function") {
        btn.focus();
      }
      if (typeof deps.onLayoutChange === "function") deps.onLayoutChange();
    }

    return {
      // 启动：按当前断点应用布局。手机抽屉固定默认关闭（桌面偏好不外溢）；
      // 桌面读偏好（含启动时身份未知的 official:local 维度），无偏好/读取失败
      // 默认收起（§4.1）
      init: function () {
        if (btn && btn.setAttribute) btn.setAttribute("aria-controls", "sidebar");
        if (isMobile()) {
          applyDrawer(false);
        } else {
          var pref = readSidebarPref(deps.storage, scopeName());
          if (pref) collapsed = pref.collapsed;
          applyDesktop();
        }
      },
      isMobile: isMobile,
      isDesktopCollapsed: function () { return collapsed; },
      isDrawerOpen: drawerOpen,
      // #menu-btn 点击：桌面=切换收起并持久化；手机=开关抽屉（不写偏好）
      toggle: function () {
        if (isMobile()) { applyDrawer(!drawerOpen()); return; }
        collapsed = !collapsed;
        userTouched = true;
        applyDesktop();
        writeSidebarPref(deps.storage, scopeName(), collapsed);
      },
      closeDrawer: function () {
        if (drawerOpen()) applyDrawer(false);
      },
      // /api/auth/info 到位后按真实身份重读偏好（用户已手动操作则不覆盖）
      onScopeReady: function () {
        if (userTouched || isMobile()) return;
        var pref = readSidebarPref(deps.storage, scopeName());
        if (pref && pref.collapsed !== collapsed) {
          collapsed = pref.collapsed;
          applyDesktop();
        }
      },
      // 断点切换：清理手机遮罩/抽屉与桌面收起类的残留，恢复当前设备布局状态
      onBreakpointChange: function () {
        if (sidebar && sidebar.classList) sidebar.classList.remove("open", "collapsed");
        if (mask && mask.classList) mask.classList.remove("open");
        if (isMobile()) applyDrawer(false);
        else applyDesktop();
      },
      // 空态「选择切片」：展开侧栏（桌面同时持久化为展开）并聚焦搜索框（§4.1）
      expandAndFocusSearch: function () {
        if (isMobile()) {
          if (!drawerOpen()) applyDrawer(true);
        } else if (collapsed) {
          collapsed = false;
          userTouched = true;
          applyDesktop();
          writeSidebarPref(deps.storage, scopeName(), collapsed);
        }
        if (typeof deps.focusSearch === "function") deps.focusSearch();
      },
      // 语言切换后同步按钮文案/aria（状态不变，仅 label）
      refreshButton: function () {
        setBtnState(isMobile() ? drawerOpen() : !collapsed);
      },
    };
  }

  // 存储访问兜底：隐私模式下访问 window.localStorage 本身可能抛错
  function safeLocalStorage() {
    try {
      var s = window.localStorage;
      if (s && typeof s.getItem === "function") { s.getItem("pt.sb.probe"); return s; }
    } catch (e) { /* 不可用：偏好不持久化，页面照常启动 */ }
    return null;
  }

  var sidebarCtrl = null;   // init() 里创建（createSidebarController 装配真实依赖）

  // 断点判定（控制器外的兜底路径沿用同一媒体查询口径）
  function isMobileWidth() {
    if (sidebarCtrl && typeof sidebarCtrl.isMobile === "function") return sidebarCtrl.isMobile();
    return !!(window.matchMedia && window.matchMedia(SB_MOBILE_QUERY).matches);
  }

  // ---------- 无切片空态（升级 A §4.1） ----------
  // 侧栏默认收起后，空态保留明显的「选择切片」入口；切片打开后隐藏。
  function updateViewerEmptyState() {
    if (els.viewerEmpty) els.viewerEmpty.hidden = !!state.slide;
  }

  // ---------- 侧栏宽度变化 → Viewer resize 链（升级 A §4.2，几何验收核心） ----------
  // 侧栏/抽屉宽度变化必须让 OSD 重算 viewport 再重绘叠加层，不能沿用旧容器
  // 尺寸的画布坐标。OSD 5 的容器 ResizeObserver 在下一帧执行
  // viewport.resize(preserveImageSizeOnResize) 并 panTo(原中心)——图像中心与
  // 缩放保留、绝不 goHome；随后触发 viewer "resize" 事件 → 重绘标注/AI overlay。
  // 这里在布局落定（下一帧）后补画布背衬尺寸同步与 ROI 框/底图缩略图对位。
  function syncViewerLayoutNow() {
    // forceResize 兜底：容器尺寸未越过 OSD 内部阈值或 observer 时机抖动时强制重算
    try { if (viewer && viewer.forceResize) viewer.forceResize(); } catch (e) { /* 忽略 */ }
    resizeAnnoCanvas();
    redrawAnnoCanvas();
    updateRoiOverlay();
    syncBaseThumb();
  }
  function syncViewerLayoutAfterSidebar() {
    // 等一帧：flex 布局/宽度过渡先落定，再按新容器尺寸同步
    if (typeof window.requestAnimationFrame === "function") {
      window.requestAnimationFrame(function () { syncViewerLayoutNow(); });
    } else {
      syncViewerLayoutNow();
    }
  }

  // ---------- 切片搜索过滤（升级 A：纯前端；收起不丢搜索条件） ----------
  // 匹配切片行 data-name 与行文本（别名优先）。项目行：名称命中整组显示，
  // 否则任一切片命中则展开显示；无命中隐藏。列表重渲后需重放当前条件。
  function applySlideFilter(raw) {
    if (!els.sidebar) return;
    var q = String(raw == null ? "" : raw).trim().toLowerCase();
    var rows = els.sidebar.querySelectorAll(".slide-row");
    Array.prototype.forEach.call(rows, function (row) {
      var hay = String(row.getAttribute("data-name") || row.textContent || "").toLowerCase();
      row.style.display = (q && hay.indexOf(q) < 0) ? "none" : "";
    });
    Array.prototype.forEach.call(els.sidebar.querySelectorAll(".proj-row"), function (row) {
      var nameEl = row.querySelector(".proj-name");
      var nameHit = !!(q && nameEl &&
        String(nameEl.textContent || "").toLowerCase().indexOf(q) >= 0);
      var slideHit = false;
      Array.prototype.forEach.call(row.querySelectorAll(".slide-row"), function (s) {
        if (s.style.display !== "none") slideHit = true;
      });
      var show = !q || nameHit || slideHit;
      row.style.display = show ? "" : "none";
      if (q && show && !nameHit && row.classList) row.classList.add("expanded");
    });
  }

  // ---------- 移动端上下文动作条显隐 ----------
  // ROI 模式或箭头/描图绘制模式任一激活时，显示底部主栏上方的上下文条
  // （标注人输入 + 保存标记/保存图片）。桌面端不受影响（display:contents）。
  function updateCtxBar() {
    var on = state.roiMode != null || state.drawMode != null;
    document.body.classList.toggle("ctx-on", on);
  }

  // ---------- 移动端 ⋯ 溢出面板（装 AI 读片 + 缩放徽章，避免挤爆底栏） ----------
  function bindTbbMore() {
    if (!els.tbbMoreBtn || !els.tbbMore) return;
    var mask = $("tbb-more-mask");
    function closeMore() {
      els.tbbMore.classList.remove("open");
      if (mask) mask.classList.remove("open");
    }
    function openMore() {
      els.tbbMore.classList.add("open");
      if (mask) mask.classList.add("open");
    }
    els.tbbMoreBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (els.tbbMore.classList.contains("open")) { closeMore(); } else { openMore(); }
    });
    if (mask) mask.addEventListener("click", closeMore);
    // ⋯ 面板里的 AI 钮：转发给主 AI 钮（打开/关闭 AI 面板），并关闭 ⋯
    if (els.tbbMoreAi) {
      els.tbbMoreAi.addEventListener("click", function () {
        closeMore();
        if (els.aiBtn && !els.aiBtn.disabled) els.aiBtn.click();
      });
    }
  }

  // =========================================================================
  // 标注画布层（rect/arrow/freehand 统一绘制）
  // =========================================================================
  var annoCtx = null;

  function resizeAnnoCanvas() {
    var c = els.annoCanvas;
    if (!c || !viewer) return;
    var rect = viewer.container.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    c.width = Math.max(1, Math.floor(rect.width * dpr));
    c.height = Math.max(1, Math.floor(rect.height * dpr));
    c.style.width = rect.width + "px";
    c.style.height = rect.height + "px";
    annoCtx = c.getContext("2d");
    annoCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // 把图像坐标转为画布层屏幕坐标（自带旋转支持）
  function imgToCanvas(ix, iy) {
    if (!viewer || !viewer.viewport) return { x: 0, y: 0 };
    var p = viewer.viewport.imageToViewerElementCoordinates(
      new OpenSeadragon.Point(ix, iy));
    return { x: p.x, y: p.y };
  }

  // 当前切片的标注展开为扁平 item 列表（带 label/type/几何）
  // flatItems 为持久缓存（编辑拖动时改本地几何），每次标注刷新时重建
  var flatItems = [];
  function flatAnnoItems() {
    return flatItems;
  }
  function rebuildFlatItems() {
    var out = [];
    if (currentAnnotations) {
      (currentAnnotations.annotations || []).forEach(function (grp) {
        (grp.items || []).forEach(function (it) {
          var copy = {};
          for (var k in it) copy[k] = it[k];
          copy.label = grp.label;
          out.push(copy);
        });
      });
    }
    flatItems = out;
  }

  function redrawAnnoCanvas() {
    var c = els.annoCanvas;
    if (!c || !annoCtx) { if (c) resizeAnnoCanvas(); }
    if (!annoCtx) return;
    var rect = viewer ? viewer.container.getBoundingClientRect() : { width: c.clientWidth, height: c.clientHeight };
    annoCtx.clearRect(0, 0, rect.width, rect.height);
    // AI overlay（青色虚线框）独立于 showAnno：agent 进行中/完成后始终画
    var hasAiOverlay = aiOverlay && aiOverlay.length > 0;
    if (!state.showAnno && state.drawMode == null && !hasAiOverlay) return;
    if (!state.slide) return;
    // 性能：缩放/平移动画期间省略文本（标签/气泡）只画矢量，
    // 动画结束（animation-finish）再补全，避免每帧逐条 measureText/fillText
    var animating = !!(viewer && viewer.viewport &&
      typeof viewer.viewport.isAnimating === "function" && viewer.viewport.isAnimating());
    // 拖动编辑中只保留选中项的气泡，其余气泡暂停（视图静止时减少文本重绘）
    var dragging = !!(editDrag && editItem);
    // 已保存标注（focus 过滤：有 focusAnno 时只画它）
    if (state.showAnno) {
      flatAnnoItems().forEach(function (it) {
        if (state.focusAnno && it !== state.focusAnno) return;
        var selected = (editItem === it);
        drawAnnoItem(it, labelColor(it.label), selected, !animating);
      });
    }
    // AI overlay：当前视角 / 历史路径 = 虚线框；局部观察 = 绿色实线。不跟人眼视野走。
    if (hasAiOverlay) {
      aiOverlay.forEach(function (bb) {
        var tl = imgToCanvas(bb.x, bb.y);
        var br = imgToCanvas(bb.x + bb.w, bb.y + bb.h);
        var x = Math.min(tl.x, br.x), y = Math.min(tl.y, br.y);
        var w = Math.abs(br.x - tl.x), h = Math.abs(br.y - tl.y);
        var role = bb.role || "view";
        var fill = AI_OVERLAY_FILL, stroke = AI_OVERLAY_STROKE, halo = AI_OVERLAY_HALO, dash = [7, 4];
        if (role === "path") {
          fill = null;
          stroke = "rgba(255, 149, 0, 0.72)";
          halo = "rgba(0, 0, 0, 0.45)";
          dash = [6, 4];
        } else if (role === "obs") {
          fill = "rgba(52, 199, 89, 0.10)";
          stroke = "#34C759";
          halo = "rgba(0, 0, 0, 0.45)";
          dash = [];
        }
        annoCtx.save();
        if (fill) {
          annoCtx.fillStyle = fill;
          annoCtx.fillRect(x, y, w, h);
        }
        if (dash.length) annoCtx.setLineDash(dash);
        // 深色外描边：在粉白组织上托住主色
        annoCtx.lineWidth = role === "path" ? 3 : 4;
        annoCtx.strokeStyle = halo;
        annoCtx.strokeRect(x, y, w, h);
        annoCtx.lineWidth = 2;
        annoCtx.strokeStyle = stroke;
        annoCtx.strokeRect(x, y, w, h);
        annoCtx.setLineDash([]);
        if (!animating && bb.magnification) {
          var label = "AI · " + fmtAiMag(bb.magnification);
          annoCtx.font = "600 12px -apple-system, BlinkMacSystemFont, sans-serif";
          var tw = annoCtx.measureText(label).width;
          var padX = 6, padY = 3, boxH = 18;
          var bx = x + 3, by = y + 3;
          annoCtx.fillStyle = "rgba(0, 0, 0, 0.78)";
          if (typeof annoCtx.roundRect === "function") {
            annoCtx.beginPath();
            annoCtx.roundRect(bx, by, tw + padX * 2, boxH, 4);
            annoCtx.fill();
          } else {
            annoCtx.fillRect(bx, by, tw + padX * 2, boxH);
          }
          annoCtx.fillStyle = "#FFFFFF";
          annoCtx.textBaseline = "middle";
          annoCtx.fillText(label, bx + padX, by + boxH / 2 + 0.5);
        }
        annoCtx.restore();
      });
    }
    // 编辑手柄（仅显式编辑态才画，纯选中不画，防误挪位置）
    if (editItem && editing && state.showAnno) {
      drawEditHandles(editItem);
    }
    // 绘制中的预览
    if (state.drawMode === "arrow" && drawPreview && drawPreview.type === "arrow") {
      drawArrow(drawPreview.x1, drawPreview.y1, drawPreview.x2, drawPreview.y2, "#FFD700", t("draw.preview"));
    }
    if (state.drawMode === "freehand" && drawPreview && drawPreview.type === "freehand" && drawPreview.points.length >= 2) {
      drawFreehand(drawPreview.points, { fill: "rgba(255,215,0,0.12)", stroke: "#FFD700" }, t("draw.preview"));
    }
    // 备注气泡（在标注与手柄之上；动画/拖动期间按需精简；focus 过滤同步）
    if (state.showAnno && !animating) {
      flatAnnoItems().forEach(function (it) {
        if (state.focusAnno && it !== state.focusAnno) return; // focus 模式只显示该条气泡
        if (dragging && it !== editItem) return; // 拖动中只画选中项气泡
        var note = String(it.note || "");
        // P2-8：note 为空时不画气泡。矩形边上的短标签（drawLabel 的"标签 · 尺寸"）
        // 已展示 label+尺寸，旧实现为选中项额外生成"标签（尺寸）"气泡，内容重复。
        // 现仅在 note 非空时画气泡，且气泡只展示 note 内容本身。
        if (!note) return;
        var selected = (editItem === it);
        drawNoteBubble(it, note, selected);
      });
    }
  }

  // 绘制编辑手柄（管理端所有标注可编辑）
  function drawEditHandles(it) {
    var hs = editHandles(it);
    annoCtx.fillStyle = "#fff";
    annoCtx.strokeStyle = "#007AFF";
    annoCtx.lineWidth = 2;
    hs.forEach(function (h) {
      var isMid = (h.id === "mid" || h.id === "fmid" || h.id === "move");
      if (isMid) {
        annoCtx.beginPath();
        annoCtx.arc(h.x, h.y, 6, 0, Math.PI * 2);
        annoCtx.fill(); annoCtx.stroke();
      } else {
        annoCtx.fillRect(h.x - 5, h.y - 5, 10, 10);
        annoCtx.strokeRect(h.x - 5, h.y - 5, 10, 10);
      }
    });
  }

  function drawAnnoItem(it, color, selected, showText) {
    var typ = it.type || "rect";
    var hlStroke = selected ? "#007AFF" : null;
    var lbl = showText ? it.label : null;
    // AI 落标（进标注库 source=ai）给半透明青色填充，区别于人工标注（#3）
    var isAi = (it.source === "ai");
    if (typ === "rect") {
      var w0 = rectItemW(it), h0 = rectItemH(it);
      var tl = imgToCanvas(it.x, it.y);
      var br = imgToCanvas(it.x + w0, it.y + h0);
      var w = Math.abs(br.x - tl.x), h = Math.abs(br.y - tl.y);
      var x = Math.min(tl.x, br.x), y = Math.min(tl.y, br.y);
      // 半透明填充：AI 标注青色（更醒目），人工标注用 label 哈希淡色
      annoCtx.fillStyle = isAi ? AI_ANNO_FILL : (color.fill || "rgba(0,0,0,0)");
      annoCtx.fillRect(x, y, w, h);
      if (hlStroke) {
        annoCtx.lineWidth = 6;
        annoCtx.strokeStyle = hlStroke;
        annoCtx.strokeRect(x, y, w, h);
      }
      annoCtx.lineWidth = 3;
      annoCtx.strokeStyle = "#FFD700";
      annoCtx.strokeRect(x, y, w, h);
      // 角点
      annoCtx.fillStyle = "#FFD700";
      [[x, y], [x + w, y], [x, y + h], [x + w, y + h]].forEach(function (p) {
        annoCtx.beginPath(); annoCtx.arc(p[0], p[1], 3, 0, Math.PI * 2); annoCtx.fill();
      });
      if (lbl) {
        // 有备注气泡时标签改画在框内左上，避免和框顶居中的 callout 叠在一起
        var hasNote = String(it.note || "").trim();
        var sizeTxt = rectItemSizeText(it);
        drawLabel(it.label, x, y, sizeTxt, null, !!hasNote);
      }
    } else if (typ === "arrow") {
      drawArrow(it.x1, it.y1, it.x2, it.y2, hlStroke || color.stroke, lbl);
    } else if (typ === "freehand") {
      drawFreehand(it.points, { fill: color.fill, stroke: hlStroke || color.stroke }, lbl);
    }
  }

  function drawArrow(x1, y1, x2, y2, stroke, label) {
    var a = imgToCanvas(x1, y1), b = imgToCanvas(x2, y2);
    annoCtx.lineWidth = 3;
    annoCtx.strokeStyle = stroke;
    annoCtx.fillStyle = stroke;
    annoCtx.beginPath();
    annoCtx.moveTo(a.x, a.y);
    annoCtx.lineTo(b.x, b.y);
    annoCtx.stroke();
    // 箭头三角头部（根据两端点屏幕坐标算角度）
    var ang = Math.atan2(b.y - a.y, b.x - a.x);
    var head = 12;
    annoCtx.beginPath();
    annoCtx.moveTo(b.x, b.y);
    annoCtx.lineTo(b.x - head * Math.cos(ang - Math.PI / 6), b.y - head * Math.sin(ang - Math.PI / 6));
    annoCtx.lineTo(b.x - head * Math.cos(ang + Math.PI / 6), b.y - head * Math.sin(ang + Math.PI / 6));
    annoCtx.closePath();
    annoCtx.fill();
    if (label) drawLabel(label, b.x + 6, b.y - 6, "", stroke);
  }

  function drawFreehand(points, color, label) {
    if (!points || points.length < 2) return;
    annoCtx.lineWidth = 3;
    annoCtx.strokeStyle = color.stroke;
    annoCtx.fillStyle = color.fill || "rgba(0,0,0,0.12)";
    annoCtx.beginPath();
    var p0 = imgToCanvas(points[0][0], points[0][1]);
    annoCtx.moveTo(p0.x, p0.y);
    for (var i = 1; i < points.length; i++) {
      var p = imgToCanvas(points[i][0], points[i][1]);
      annoCtx.lineTo(p.x, p.y);
    }
    annoCtx.closePath();
    annoCtx.fill();
    annoCtx.stroke();
    if (label) drawLabel(label, p0.x, p0.y, "", color.stroke);
  }

  // 标签文字：黄底深字（与现有 ROI 标签风格一致）
  // inside=true：画在矩形内左上（有备注气泡时用，避免和框顶 callout 重叠）
  function drawLabel(label, x, y, sizeText, strokeColor, inside) {
    var text = String(label || "");
    if (sizeText) text = (text ? text + " · " : "") + sizeText;
    if (!text) return;
    annoCtx.font = "600 11px " + "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif";
    var padX = 5, padY = 3;
    var m = annoCtx.measureText(text);
    var w = m.width + padX * 2;
    var h = 16;
    var bx = inside ? x + 3 : x;
    var by = inside ? y + 3 : y - h - 2;
    if (strokeColor && strokeColor !== "#FFD700") {
      annoCtx.fillStyle = strokeColor;
    } else {
      annoCtx.fillStyle = "#FFD700";
    }
    annoCtx.fillRect(bx, by, w, h);
    annoCtx.fillStyle = (strokeColor && strokeColor !== "#FFD700") ? "#fff" : "#5a3500";
    annoCtx.textBaseline = "middle";
    annoCtx.fillText(text, bx + padX, by + h / 2 + 0.5);
  }

  // ---------- 备注气泡（macOS callout 风格，与 share.js 一致） ----------
  var BUBBLE_FONT = "12px " + "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif";
  // 布局缓存：note 文本 → {lines, boxW, boxH}（font/maxWidth 固定，布局与视图无关，
  // 避免每帧逐字符 measureText——标注多时这是动画卡顿的主因）
  var _bubbleLayoutCache = {};
  function bubbleLayout(note) {
    var hit = _bubbleLayoutCache[note];
    if (hit) return hit;
    annoCtx.font = BUBBLE_FONT;
    var maxWidth = 240;
    var lines = wrapText(note, maxWidth);
    var padX = 8, padY = 6, lineH = 15;
    var textW = 0;
    lines.forEach(function (ln) {
      var w = annoCtx.measureText(ln).width;
      if (w > textW) textW = w;
    });
    var out = {
      lines: lines,
      boxW: Math.min(maxWidth, Math.max(20, textW)) + padX * 2,
      boxH: lines.length * lineH + padY * 2,
    };
    if (Object.keys(_bubbleLayoutCache).length > 300) _bubbleLayoutCache = {};
    _bubbleLayoutCache[note] = out;
    return out;
  }

  function wrapText(text, maxWidth) {
    annoCtx.font = BUBBLE_FONT;
    var lines = [];
    String(text).split("\n").forEach(function (para) {
      if (para === "") { lines.push(""); return; }
      var cur = "";
      for (var i = 0; i < para.length; i++) {
        var test = cur + para[i];
        if (annoCtx.measureText(test).width > maxWidth && cur) {
          lines.push(cur);
          cur = para[i];
        } else {
          cur = test;
        }
      }
      if (cur) lines.push(cur);
    });
    return lines;
  }

  function annoAnchor(it) {
    var typ = it.type || "rect";
    if (typ === "rect") {
      var tl = imgToCanvas(it.x, it.y);
      var br = imgToCanvas(it.x + rectItemW(it), it.y + rectItemH(it));
      var x = Math.min(tl.x, br.x), y = Math.min(tl.y, br.y);
      var w = Math.abs(br.x - tl.x), h = Math.abs(br.y - tl.y);
      return { x: x + w / 2, y: y, minSide: Math.min(w, h) };
    } else if (typ === "arrow") {
      var a = imgToCanvas(it.x1, it.y1), b = imgToCanvas(it.x2, it.y2);
      return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, minSide: 40 };
    } else if (typ === "freehand") {
      var pts = (it.points || []).map(function (p) { return imgToCanvas(p[0], p[1]); });
      var xs = pts.map(function (p) { return p.x; });
      var ys = pts.map(function (p) { return p.y; });
      var minx = Math.min.apply(null, xs), maxx = Math.max.apply(null, xs);
      var miny = Math.min.apply(null, ys), maxy = Math.max.apply(null, ys);
      return { x: (minx + maxx) / 2, y: miny, minSide: Math.min(maxx - minx, maxy - miny) };
    }
    return { x: 0, y: 0, minSide: 0 };
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function drawNoteBubble(it, note, selected) {
    var anchor = annoAnchor(it);
    if (anchor.minSide < 24) return;
    var c = els.annoCanvas;
    var canvasW = c.clientWidth, canvasH = c.clientHeight;

    // 布局走缓存（避免每帧逐字符 measureText）
    var layout = bubbleLayout(note);
    var lines = layout.lines;
    var boxW = layout.boxW, boxH = layout.boxH;
    var padX = 8, padY = 6, lineH = 15;

    var cx = anchor.x;
    var above = true;
    var boxX = cx - boxW / 2;
    var boxY = anchor.y - 8 - boxH;

    if (boxY < 4) { above = false; boxY = anchor.y + 10; }
    if (boxX < 4) boxX = 4;
    if (boxX + boxW > canvasW - 4) boxX = canvasW - 4 - boxW;
    if (boxY + boxH > canvasH - 4) boxY = Math.max(4, canvasH - 4 - boxH);

    var borderColor = selected ? "#007AFF" : "rgba(0,0,0,0.15)";
    var triSize = 6;
    var triTipX = cx;
    annoCtx.save();
    annoCtx.globalAlpha = 0.85;
    annoCtx.fillStyle = "#ffffff";
    roundRect(annoCtx, boxX, boxY, boxW, boxH, 8);
    annoCtx.fill();
    annoCtx.globalAlpha = 1;
    annoCtx.strokeStyle = borderColor;
    annoCtx.lineWidth = 1;
    annoCtx.stroke();
    annoCtx.restore();

    annoCtx.save();
    annoCtx.fillStyle = "#ffffff";
    annoCtx.strokeStyle = borderColor;
    annoCtx.lineWidth = 1;
    annoCtx.beginPath();
    if (above) {
      var baseY = boxY + boxH;
      annoCtx.moveTo(triTipX - triSize, baseY - 0.5);
      annoCtx.lineTo(triTipX, baseY + triSize);
      annoCtx.lineTo(triTipX + triSize, baseY - 0.5);
    } else {
      var baseY2 = boxY;
      annoCtx.moveTo(triTipX - triSize, baseY2 + 0.5);
      annoCtx.lineTo(triTipX, baseY2 - triSize);
      annoCtx.lineTo(triTipX + triSize, baseY2 + 0.5);
    }
    annoCtx.closePath();
    annoCtx.fill();
    annoCtx.stroke();
    annoCtx.restore();

    annoCtx.fillStyle = "#333";
    annoCtx.font = "12px " + "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif";
    annoCtx.textBaseline = "top";
    lines.forEach(function (ln, i) {
      annoCtx.fillText(ln, boxX + padX, boxY + padY + i * lineH);
    });
  }

  // =========================================================================
  // 编辑模式：非绘制模式下点击标注画布层，命中检测 + 选中 + 拖动手柄
  // （管理端所有标注可编辑）
  // =========================================================================
  function pointSegDist(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1, dy = y2 - y1;
    var len2 = dx * dx + dy * dy;
    if (len2 <= 0) return Math.hypot(px - x1, py - y1);
    var projT = ((px - x1) * dx + (py - y1) * dy) / len2;
    if (projT < 0) projT = 0; else if (projT > 1) projT = 1;
    return Math.hypot(px - (x1 + projT * dx), py - (y1 + projT * dy));
  }

  function pointInPolygon(px, py, pts) {
    var inside = false;
    for (var i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      var xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
      var intersect = ((yi > py) !== (yj > py)) &&
        (px < (xj - xi) * (py - yi) / ((yj - yi) || 1e-9) + xi);
      if (intersect) inside = !inside;
    }
    return inside;
  }

  function hitAnno(sx, sy) {
    var items = flatAnnoItems();
    for (var i = items.length - 1; i >= 0; i--) {
      var it = items[i];
      var typ = it.type || "rect";
      if (typ === "rect") {
        var tl = imgToCanvas(it.x, it.y);
        var br = imgToCanvas(it.x + rectItemW(it), it.y + rectItemH(it));
        var x = Math.min(tl.x, br.x), y = Math.min(tl.y, br.y);
        var w = Math.abs(br.x - tl.x), h = Math.abs(br.y - tl.y);
        if (sx >= x - 6 && sx <= x + w + 6 && sy >= y - 6 && sy <= y + h + 6) return it;
      } else if (typ === "arrow") {
        var a = imgToCanvas(it.x1, it.y1), b = imgToCanvas(it.x2, it.y2);
        if (pointSegDist(sx, sy, a.x, a.y, b.x, b.y) <= 8) return it;
      } else if (typ === "freehand") {
        var pts = (it.points || []).map(function (p) { return imgToCanvas(p[0], p[1]); });
        if (pts.length >= 3 && pointInPolygon(sx, sy, pts)) return it;
        for (var k = 0; k < pts.length - 1; k++) {
          if (pointSegDist(sx, sy, pts[k].x, pts[k].y, pts[k + 1].x, pts[k + 1].y) <= 8) return it;
        }
      }
    }
    return null;
  }

  function editHandles(it) {
    var typ = it.type || "rect";
    var out = [];
    if (typ === "rect") {
      var tl = imgToCanvas(it.x, it.y);
      var br = imgToCanvas(it.x + rectItemW(it), it.y + rectItemH(it));
      var x = Math.min(tl.x, br.x), y = Math.min(tl.y, br.y);
      var w = Math.abs(br.x - tl.x), h = Math.abs(br.y - tl.y);
      out = [
        { id: "tl", x: x, y: y }, { id: "t", x: x + w / 2, y: y },
        { id: "tr", x: x + w, y: y }, { id: "r", x: x + w, y: y + h / 2 },
        { id: "br", x: x + w, y: y + h }, { id: "b", x: x + w / 2, y: y + h },
        { id: "bl", x: x, y: y + h }, { id: "l", x: x, y: y + h / 2 },
      ];
    } else if (typ === "arrow") {
      var a = imgToCanvas(it.x1, it.y1), b = imgToCanvas(it.x2, it.y2);
      out = [
        { id: "p1", x: a.x, y: a.y }, { id: "p2", x: b.x, y: b.y },
        { id: "mid", x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
      ];
    } else if (typ === "freehand") {
      var xs = it.points.map(function (p) { return p[0]; });
      var ys = it.points.map(function (p) { return p[1]; });
      var minx = Math.min.apply(null, xs), miny = Math.min.apply(null, ys);
      var maxx = Math.max.apply(null, xs), maxy = Math.max.apply(null, ys);
      var tl2 = imgToCanvas(minx, miny), br2 = imgToCanvas(maxx, maxy);
      var x2 = Math.min(tl2.x, br2.x), y2 = Math.min(tl2.y, br2.y);
      var w2 = Math.abs(br2.x - tl2.x), h2 = Math.abs(br2.y - tl2.y);
      out = [
        { id: "ftl", x: x2, y: y2 }, { id: "ftr", x: x2 + w2, y: y2 },
        { id: "fbr", x: x2 + w2, y: y2 + h2 }, { id: "fbl", x: x2, y: y2 + h2 },
        { id: "fmid", x: x2 + w2 / 2, y: y2 + h2 / 2 },
      ];
    }
    return out;
  }

  function hitHandle(sx, sy, it) {
    var hs = editHandles(it);
    for (var i = 0; i < hs.length; i++) {
      if (Math.hypot(sx - hs[i].x, sy - hs[i].y) <= 8) return hs[i].id;
    }
    return null;
  }

  function selectEditItem(it) {
    editItem = it;
    state.focusAnno = it; // 选中某条 → 只显示它（focus 可见性）
    editing = false;  // 选中只是查看，不进入可拖动编辑态
    redrawAnnoCanvas();
    openEditCard(it);
  }

  function clearEditItem() {
    editItem = null;
    state.focusAnno = null; // 取消选中 → 恢复显示全部
    editing = false;
    closeEditCard();
    redrawAnnoCanvas();
  }

  // ---------- 显示全部标记（切换画布层显隐） ----------
  // 同步所有相关按钮的 active 态：旧 #anno-all-btn + 新面板头部 #anno-all-toggle
  function syncAnnoAllBtns() {
    if (els.annoAllBtn) els.annoAllBtn.classList.toggle("active", state.showAnno);
    if (els.annoAllToggle) {
      els.annoAllToggle.classList.toggle("active", state.showAnno);
      els.annoAllToggle.setAttribute("aria-pressed", state.showAnno ? "true" : "false");
    }
  }
  function toggleAnnoAll() {
    // 👁 =「显示全部标记」语义：若当前处于"只看选中那条"的 focus 状态，
    // 先清空 focus 恢复显示全部；否则在「显示全部 ↔ 全部隐藏」之间切换。
    // （画布层非绘制时 pointer-events:none，无法点空白取消 focus，故由该钮兜底。）
    if (state.focusAnno) {
      state.focusAnno = null;
      state.showAnno = true;
    } else {
      state.showAnno = !state.showAnno;
    }
    syncAnnoAllBtns();
    redrawAnnoCanvas();
  }
  // 旧函数别名（兼容）
  function clearAnnoOverlays() { annoOverlays = []; redrawAnnoCanvas(); }
  function refreshAnnoOverlays() { redrawAnnoCanvas(); }

  // =========================================================================
  // 标注绘制工具（arrow / freehand）
  // =========================================================================
  var drawPreview = null;     // {type, ...}
  var drawPointer = null;     // 当前指针捕获信息

  function enterDrawMode(mode) {
    if (!state.slide) { toast(t("roi.need.slide"), "error"); return; }
    exitRoi();
    state.drawMode = mode;
    els.annoArrowBtn.classList.toggle("active", mode === "arrow");
    els.annoFreeBtn.classList.toggle("active", mode === "freehand");
    var c = els.annoCanvas;
    c.classList.add("drawing");
    if (viewer) viewer.setMouseNavEnabled(false);
    state.showAnno = true;
    syncAnnoAllBtns();
    redrawAnnoCanvas();
    updateCtxBar();
    toast(mode === "arrow" ? t("draw.arrow.tip") : t("draw.free.tip"), "info");
  }

  function exitDrawMode() {
    state.drawMode = null;
    drawPreview = null;
    drawPointer = null;
    els.annoArrowBtn.classList.remove("active");
    els.annoFreeBtn.classList.remove("active");
    if (els.annoCanvas) els.annoCanvas.classList.remove("drawing");
    if (viewer) viewer.setMouseNavEnabled(true);
    redrawAnnoCanvas();
    updateCtxBar();
  }

  function toggleDrawMode(mode) {
    if (state.drawMode === mode) { exitDrawMode(); return; }
    enterDrawMode(mode);
  }

  function onAnnoPointerDown(e) {
    if (!state.slide) return;
    // 矩形工具优先（升级 C：画布层拖出矩形/点击中心放置）
    if (rectToolActive()) {
      onRectCanvasPointerDown(e);
      return;
    }
    // 绘制模式优先
    if (state.drawMode) {
      e.preventDefault(); e.stopPropagation();
      var c = els.annoCanvas;
      try { c.setPointerCapture(e.pointerId); } catch (err) {}
      drawPointer = { id: e.pointerId };
      var img0 = screenToImg(e);
      if (state.drawMode === "arrow") {
        drawPreview = { type: "arrow", x1: img0.x, y1: img0.y, x2: img0.x, y2: img0.y };
      } else {
        drawPreview = { type: "freehand", points: [[img0.x, img0.y]], lastScreen: screenPt(e) };
      }
      redrawAnnoCanvas();
      return;
    }
    // 非绘制模式：编辑/选中
    if (!state.showAnno) return;
    e.preventDefault(); e.stopPropagation();
    var sp = screenPt(e);
    // 显式编辑态且点中手柄 → 拖动手柄（平移/缩放必须先进入编辑态）
    if (editItem && editing) {
      var handleId = hitHandle(sp.x, sp.y, editItem);
      if (handleId) {
        startEditDrag(e, editItem, handleId);
        return;
      }
    }
    // 命中标注 → 重新选中查看（editing 复位，不直接平移；要改需先点"✎ 编辑"）
    var hit = hitAnno(sp.x, sp.y);
    if (hit) {
      selectEditItem(hit);
      return;
    }
    // 点空白 → 取消选中
    clearEditItem();
  }

  function onAnnoPointerMove(e) {
    // 矩形工具：拖出预览（升级 C）
    if (rectToolActive() && rectDrawInfo) {
      onRectCanvasPointerMove(e);
      return;
    }
    if (state.drawMode && drawPreview) {
      e.preventDefault(); e.stopPropagation();
      var img = screenToImg(e);
      if (drawPreview.type === "arrow") {
        drawPreview.x2 = img.x; drawPreview.y2 = img.y;
      } else {
        var sp0 = screenPt(e);
        var last = drawPreview.lastScreen;
        if (Math.hypot(sp0.x - last.x, sp0.y - last.y) > 4) {
          drawPreview.points.push([img.x, img.y]);
          drawPreview.lastScreen = sp0;
          if (drawPreview.points.length >= 500) { finishDraw(); return; }
        }
      }
      redrawAnnoCanvas();
      return;
    }
    if (!editDrag) return;
    e.preventDefault(); e.stopPropagation();
    applyEditDrag(e);
  }

  function onAnnoPointerUp(e) {
    // 矩形工具：完成拖出 / 点击放置（升级 C）
    if (rectToolActive() && rectDrawInfo) {
      onRectCanvasPointerUp(e);
      return;
    }
    if (state.drawMode && drawPreview) {
      e.preventDefault(); e.stopPropagation();
      finishDraw();
      return;
    }
    if (!editDrag) return;
    e.preventDefault(); e.stopPropagation();
    endEditDrag(e);
  }

  // ---------- 编辑拖动会话（与 share.js 同构） ----------
  function startEditDrag(e, it, handleId) {
    var c = els.annoCanvas;
    try { c.setPointerCapture(e.pointerId); } catch (err) {}
    editDrag = {
      pointerId: e.pointerId,
      handle: handleId,
      item: it,
      start: snapshotGeom(it),
      startImg: screenToImg(e),
    };
    if (viewer) viewer.setMouseNavEnabled(false);
  }

  function snapshotGeom(it) {
    var typ = it.type || "rect";
    if (typ === "rect") {
      return { x: it.x, y: it.y, w: rectItemW(it), h: rectItemH(it),
               side_px: it.side_px };
    }
    if (typ === "arrow") return { x1: it.x1, y1: it.y1, x2: it.x2, y2: it.y2 };
    if (typ === "freehand") return { points: (it.points || []).map(function (p) { return [p[0], p[1]]; }) };
    return {};
  }

  function applyEditDrag(e) {
    var d = editDrag;
    var it = d.item;
    var typ = it.type || "rect";
    var cur = screenToImg(e);
    var dx = cur.x - d.startImg.x;
    var dy = cur.y - d.startImg.y;
    var s = d.start;

    if (typ === "rect") {
      // 升级 C（§6.1）：四角改双轴、四边改单轴——不再 max(w,h) 正方形化。
      var W = state.slide.width, H = state.slide.height;
      if (d.handle === "move") {
        it.x = clamp(Math.round(s.x + dx), 0, Math.max(0, W - s.w));
        it.y = clamp(Math.round(s.y + dy), 0, Math.max(0, H - s.h));
      } else {
        var moveL = d.handle.indexOf("l") >= 0;
        var moveR = d.handle.indexOf("r") >= 0;
        var moveT = d.handle.indexOf("t") >= 0;
        var moveB = d.handle.indexOf("b") >= 0;
        var anchorX = moveL ? s.x + s.w : (moveR ? s.x : s.x);
        if (!moveL && !moveR) anchorX = s.x; // t/b 不改 x 轴
        var anchorY = moveT ? s.y + s.h : (moveB ? s.y : s.y);
        if (!moveT && !moveB) anchorY = s.y;
        var x0 = (moveL || moveR) ? anchorX : s.x;
        var x1 = (moveL || moveR) ? cur.x : s.x + s.w;
        var y0 = (moveT || moveB) ? anchorY : s.y;
        var y1 = (moveT || moveB) ? cur.y : s.y + s.h;
        var n = normalizeRect(x0, y0, x1, y1, false);
        if (n) {
          it.x = n.x; it.y = n.y; it.w = n.w; it.h = n.h;
        }
      }
    } else if (typ === "arrow") {
      if (d.handle === "p1") {
        it.x1 = Math.max(0, Math.round(s.x1 + dx));
        it.y1 = Math.max(0, Math.round(s.y1 + dy));
      } else if (d.handle === "p2") {
        it.x2 = Math.max(0, Math.round(s.x2 + dx));
        it.y2 = Math.max(0, Math.round(s.y2 + dy));
      } else if (d.handle === "mid") {
        it.x1 = Math.max(0, Math.round(s.x1 + dx));
        it.y1 = Math.max(0, Math.round(s.y1 + dy));
        it.x2 = Math.max(0, Math.round(s.x2 + dx));
        it.y2 = Math.max(0, Math.round(s.y2 + dy));
      }
    } else if (typ === "freehand") {
      if (d.handle === "fmid") {
        it.points = s.points.map(function (p) {
          return [Math.max(0, Math.round(p[0] + dx)), Math.max(0, Math.round(p[1] + dy))];
        });
      } else {
        var pts = s.points;
        var xs0 = pts.map(function (p) { return p[0]; });
        var ys0 = pts.map(function (p) { return p[1]; });
        var minx0 = Math.min.apply(null, xs0), maxx0 = Math.max.apply(null, xs0);
        var miny0 = Math.min.apply(null, ys0), maxy0 = Math.max.apply(null, ys0);
        var w0 = Math.max(1, maxx0 - minx0), h0 = Math.max(1, maxy0 - miny0);
        var aX = (d.handle === "ftl") ? maxx0 : minx0;
        var aY = (d.handle === "ftl") ? maxy0 : miny0;
        if (d.handle === "ftr") { aX = minx0; aY = maxy0; }
        if (d.handle === "fbr") { aX = minx0; aY = miny0; }
        if (d.handle === "fbl") { aX = maxx0; aY = miny0; }
        var newW = Math.max(2, Math.abs(cur.x - aX));
        var newH = Math.max(2, Math.abs(cur.y - aY));
        var scale = Math.max(newW / w0, newH / h0);
        var newPts = pts.map(function (p) {
          return [Math.round(aX + (p[0] - aX) * scale), Math.round(aY + (p[1] - aY) * scale)];
        });
        var nminx = Math.min.apply(null, newPts.map(function (p) { return p[0]; }));
        var nminy = Math.min.apply(null, newPts.map(function (p) { return p[1]; }));
        var offX = nminx < 0 ? -nminx : 0;
        var offY = nminy < 0 ? -nminy : 0;
        it.points = newPts.map(function (p) { return [p[0] + offX, p[1] + offY]; });
      }
    }
    redrawAnnoCanvas();
  }

  function endEditDrag(e) {
    var c = els.annoCanvas;
    if (editDrag) {
      try { c.releasePointerCapture(editDrag.pointerId); } catch (err) {}
    }
    editDrag = null;
    if (viewer) viewer.setMouseNavEnabled(true);
  }

  function finishDraw() {
    var dp = drawPreview;
    var c = els.annoCanvas;
    if (drawPointer) { try { c.releasePointerCapture(drawPointer.id); } catch (err) {} }
    drawPointer = null;
    drawPreview = null;
    if (!dp) { exitDrawMode(); return; }
    if (dp.type === "arrow") {
      var dist = Math.hypot(dp.x2 - dp.x1, dp.y2 - dp.y1);
      if (dist < 10) { toast(t("draw.short.cancel"), "info"); exitDrawMode(); return; }
      saveAnnotation({ type: "arrow", x1: dp.x1, y1: dp.y1, x2: dp.x2, y2: dp.y2 });
    } else {
      var pts = dp.points;
      if (pts.length < 3) { toast(t("draw.few.cancel"), "info"); exitDrawMode(); return; }
      // 包围盒 > 10px
      var xs = pts.map(function (p) { return p[0]; });
      var ys = pts.map(function (p) { return p[1]; });
      var bb = Math.max(Math.max.apply(null, xs) - Math.min.apply(null, xs),
                        Math.max.apply(null, ys) - Math.min.apply(null, ys));
      if (bb < 10) { toast(t("draw.small.cancel"), "info"); exitDrawMode(); return; }
      saveAnnotation({ type: "freehand", points: pts });
    }
  }

  // 屏幕坐标 → 图像坐标
  function screenToImg(e) {
    var rect = viewer.container.getBoundingClientRect();
    var p = viewer.viewport.viewerElementToImageCoordinates(
      new OpenSeadragon.Point(e.clientX - rect.left, e.clientY - rect.top));
    return { x: Math.round(p.x), y: Math.round(p.y) };
  }
  function screenPt(e) {
    var rect = viewer.container.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  // 保存管理员标注
  function saveAnnotation(geom) {
    if (!state.slide) return;
    var label = (els.annoLabelInput.value || "").trim();
    if (!label) label = t("anno.default.user");
    var body = { slide: state.slide.name, type: geom.type, label: label };
    for (var k in geom) body[k] = geom[k];
    apiFetch("/api/annotation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("save.fail")); });
        return r.json();
      })
      .then(function () {
        toast(t("anno.saved"), "success");
        exitDrawMode();
        refreshCurrentAnnotations();
        loadAnnotationsIndex().then(function () {
          renderProjects(allProjects);
          renderUnfiled();
        });
      })
      .catch(function (e) { toast(t("save.fail2", { e: e.message }), "error"); exitDrawMode(); });
  }

  // 重新拉取当前切片标注并重绘
  function refreshCurrentAnnotations() {
    if (!state.slide) { redrawAnnoCanvas(); return; }
    apiFetch("/api/annotations?slide=" + encodeURIComponent(state.slide.name))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        currentAnnotations = data;
        var annos = data.annotations || [];
        els.annoBtn.disabled = annos.length === 0;
        els.annoAllBtn.disabled = annos.length === 0;
        if (annos.length === 0) { state.showAnno = false; syncAnnoAllBtns(); }
        if (editItem && flatItems.indexOf(editItem) < 0) { editItem = null; editing = false; }
        // focusAnno 引用失效（flatItems 重建）→ 清空，恢复显示全部
        if (state.focusAnno && flatItems.indexOf(state.focusAnno) < 0) { state.focusAnno = null; }
        rebuildFlatItems();
        redrawAnnoCanvas();
      })
      .catch(function () {});
  }

  // =========================================================================
  // 标注面板 + 全部标记叠加（查看器）
  // =========================================================================
  function openAnnoPanel() {
    if (!state.slide || !currentAnnotations) { toast(t("anno.none.current"), "info"); return; }
    // 开标注面板时关闭 AI 面板（协调权交给插件：发 panel.toggle {open:false}）
    hpRequest("panel.toggle", { open: false }).catch(function () { /* 插件未启用：静默 */ });
    annoPanelOpen = true;
    els.annoPanel.style.display = "flex";
    els.annoPanelTitle.textContent = t("anno.panel.title.with", { name: truncateMiddle(state.slide.name, 28) });
    renderAnnoPanel(currentAnnotations.annotations || []);
  }

  function closeAnnoPanel() {
    annoPanelOpen = false;
    els.annoPanel.style.display = "none";
  }

  function renderAnnoPanel(groups) {
    els.annoPanelList.innerHTML = "";
    if (!groups || groups.length === 0) {
      var empty = document.createElement("div");
      empty.className = "anno-panel-empty";
      empty.textContent = t("anno.panel.empty");
      els.annoPanelList.appendChild(empty);
      return;
    }
    groups.forEach(function (grp) {
      // 分组标题
      var gh = document.createElement("div");
      gh.className = "anno-group-head";
      gh.innerHTML = '<span class="agh-label">' + esc(grp.label) + "</span>" +
        '<span class="agh-count">' + esc(t("anno.group.count", { n: grp.count })) + "</span>";
      els.annoPanelList.appendChild(gh);

      (grp.items || []).forEach(function (it) {
        var row = document.createElement("div");
        row.className = "anno-item";
        if (!it.shared) row.classList.add("anno-private");
        var left = document.createElement("div");
        left.className = "ai-info";
        var typIcon = (it.type === "arrow") ? "↗" : (it.type === "freehand" ? "〰" : "▭");
        var sizeStr = "";
        if ((it.type || "rect") === "rect") {
          var mmTxt = rectItemSizeText(it);
          if (mmTxt) sizeStr = " · " + mmTxt;
        }
        else if (it.type === "arrow") sizeStr = " · (" + it.x1 + "," + it.y1 + ")→(" + it.x2 + "," + it.y2 + ")";
        else if (it.type === "freehand") sizeStr = " · " + t("anno.free.points", { n: (it.points ? it.points.length : 0) });
        // P1-7：私有标注用「私有」徽章表达，不再整行降透明度。
        var privateBadge = (!it.shared)
          ? '<span class="anno-private-badge">' + esc(t("anno.private.badge")) + "</span>"
          : "";
        left.innerHTML =
          '<div class="ai-title"><span class="ai-type-icon">' + typIcon + "</span>" +
          '<span class="ai-label">' + esc(grp.label) + "</span>" + privateBadge + sizeStr + "</div>" +
          '<div class="ai-sub">' + fmtTime(it.ts) +
          (it.token ? esc(t("anno.sub.source", { s: String(it.token).slice(0, 6) })) : "") +
          (it.visitor ? esc(t("anno.sub.visitor", { s: String(it.visitor).slice(0, 6) })) : "") + "</div>";
        row.appendChild(left);

        // 「公开」切换钮：管理员可策展任意来源标注
        var sharedBtn = document.createElement("button");
        sharedBtn.className = "ai-share" + (it.shared ? " on" : "");
        sharedBtn.textContent = it.shared ? "🌐" : "👁";
        sharedBtn.title = it.shared ? t("anno.shared.on.title")
                                    : t("anno.shared.off.title");
        sharedBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          toggleAnnoShared(it, sharedBtn, row);
        });
        row.appendChild(sharedBtn);

        // Stage 3c-1：AI 标注审核状态（接受/驳回）。仅 source=ai 且 pending 显示按钮；
        // 已 accepted/rejected 显示状态徽章。
        if (it.source === "ai") {
          if (it.review_status === "pending") {
            var acceptBtn = document.createElement("button");
            acceptBtn.className = "ai-op ai-review-accept";
            acceptBtn.textContent = t("anno.review.accept");
            acceptBtn.title = t("anno.review.accept.tip");
            acceptBtn.addEventListener("click", function (ev) {
              ev.stopPropagation();
              reviewAnnotation(it, "accept");
            });
            row.appendChild(acceptBtn);
            var rejectBtn = document.createElement("button");
            rejectBtn.className = "ai-op ai-review-reject";
            rejectBtn.textContent = t("anno.review.reject");
            rejectBtn.title = t("anno.review.reject.tip");
            rejectBtn.addEventListener("click", function (ev) {
              ev.stopPropagation();
              reviewAnnotation(it, "reject");
            });
            row.appendChild(rejectBtn);
          } else if (it.review_status === "accepted" || it.review_status === "rejected") {
            var revBadge = document.createElement("span");
            revBadge.className = "anno-review-badge " + it.review_status;
            revBadge.textContent = t(it.review_status === "accepted"
              ? "anno.review.accepted" : "anno.review.rejected");
            row.appendChild(revBadge);
          }
        }

        // AI 动作（P1-6）：所有带 annotation_id 的标注都挂 fork「快速问答」+ branch
        // 「从此处深读」两个小按钮（图标+短文字），不再按 source 区分。fork 轻量就地
        // 展开；branch 进 AI 面板开/续分支会话（复用既有或新建）。
        if (it.annotation_id) {
          var annoAid = it.annotation_id;
          buildAnnoAiActions(row, annoAid, "op");
        }

        // 编辑钮：跳转到该标注并进入选中编辑态
        var editBtn = document.createElement("button");
        editBtn.className = "ai-op ai-edit";
        editBtn.textContent = "✎";
        editBtn.title = t("anno.edit.title");
        editBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          jumpAndEditAnno(it);
        });
        row.appendChild(editBtn);

        // 删除钮：调 DELETE 接口
        var delBtn = document.createElement("button");
        delBtn.className = "ai-op ai-del";
        delBtn.textContent = "🗑";
        delBtn.title = t("anno.del.title");
        delBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          deleteAnnoItem(it);
        });
        row.appendChild(delBtn);

        row.style.cursor = "pointer";
        row.addEventListener("click", function (ev) {
          // 点击落在操作按钮区（分享/AI 动作/编辑/删除）则交给按钮自身处理，不触发跳转
          if (ev.target.closest(".ai-share, .ai-op, .ai-action-chip, .fork-chat")) return;
          // §任务3：点击=聚焦切换。若该行已是 focusAnno → 再点一次取消 focus（恢复全量）。
          // 注意：it 是面板分组副本，state.focusAnno 是 flatItems 中的另一副本，
          // 引用不等，需按 token+ts+type 判定"是否同一标注"。
          if (state.focusAnno &&
              state.focusAnno.token === it.token &&
              Number(state.focusAnno.ts) === Number(it.ts) &&
              (state.focusAnno.type || "rect") === (it.type || "rect")) {
            state.focusAnno = null;
            editItem = null;
            redrawAnnoCanvas();
          } else {
            jumpToAnno(it);
          }
        });
        els.annoPanelList.appendChild(row);
      });
    });
  }

  function fmtTime(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    var p = function (n) { return n < 10 ? "0" + n : n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  // 兜底：解析某标注条目在其 token 下的 index（仅旧缓存无 index 时使用；
  // annotations 接口现已直接带 index，正常路径走 resolveIndexFast 不会到这。
  // 通过 /api/share/rois 取该 token 列表，按 slide+ts+几何匹配）
  function resolveAnnoIndex(it) {
    var token = it.token;
    if (!token) return Promise.reject(new Error(t("anno.no.token")));
    // annotations 接口的条目可能不带 slide（旧数据），用当前切片名兜底
    var slideName = it.slide || (state.slide ? state.slide.name : null);
    return apiFetch("/api/share/rois")
      .then(function (r) { return r.json(); })
      .then(function (rois) {
        var cands = (rois || []).filter(function (r) { return r.token === token; });
        // 优先按 slide+ts 精确匹配；ts 不在则退回 slide+几何
        var match = null;
        for (var i = 0; i < cands.length; i++) {
          var r = cands[i];
          if (r.slide === slideName && Number(r.ts) === Number(it.ts)) { match = r; break; }
        }
        if (!match) {
          for (var j = 0; j < cands.length; j++) {
            var rr = cands[j];
            if (rr.slide !== slideName || (rr.type || "rect") !== (it.type || "rect")) continue;
            if ((rr.type || "rect") === "rect" &&
                Number(rr.x) === Number(it.x) && Number(rr.y) === Number(it.y) &&
                Number(rr.side_px) === Number(it.side_px)) { match = rr; break; }
            if (rr.type === "arrow" &&
                Number(rr.x1) === Number(it.x1) && Number(rr.y1) === Number(it.y1) &&
                Number(rr.x2) === Number(it.x2) && Number(rr.y2) === Number(it.y2)) { match = rr; break; }
            if (rr.type === "freehand" && rr.points && it.points &&
                rr.points.length === it.points.length) { match = rr; break; }
          }
        }
        if (!match) throw new Error(t("anno.not.found"));
        return match.index;
      });
  }

  // 快速取 index：新数据（annotations 接口已带 index）直接用本地 it.index，
  // 省掉一次 /api/share/rois 全量拉取；仅极端旧缓存（无 index）才回退
  // resolveAnnoIndex 全量反推。
  function resolveIndexFast(it) {
    if (it && it.index != null) return Promise.resolve(it.index);
    return resolveAnnoIndex(it);
  }

  // 切换某标注的「公开」状态（策展）
  // Stage 3c-1：AI 标注审核（接受/驳回）。POST review → 刷新当前切片标注。
  function reviewAnnotation(it, action) {
    var token = it.token;
    if (!token) { toast(t("anno.no.src.token"), "error"); return; }
    resolveIndexFast(it)
      .then(function (index) {
        return apiFetch(
          "/api/annotation/" + encodeURIComponent(token) + "/" + index + "/review",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: action }),
          }
        ).then(function (r) {
          if (!r.ok) return r.json().then(function (j) {
            throw new Error(j.error || (t("anno.update.fail") + " " + r.status));
          });
          return r.json();
        });
      })
      .then(function () {
        toast(t(action === "accept" ? "anno.review.accepted" : "anno.review.rejected"),
              "success");
        refreshCurrentAnnotations();
      })
      .catch(function (e) { toast(e.message || t("anno.update.fail"), "error"); });
  }

  function toggleAnnoShared(it, btnEl, rowEl) {
    var token = it.token;
    if (!token) { toast(t("anno.no.src.token"), "error"); return; }
    var target = !it.shared;
    btnEl.disabled = true;
    resolveIndexFast(it)
      .then(function (index) {
        return apiFetch("/api/annotation/" + encodeURIComponent(token) + "/" + index, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ shared: target }),
        }).then(function (r) {
          if (!r.ok) return r.json().then(function (j) {
            throw new Error(j.error || (t("anno.update.fail") + " " + r.status));
          });
          return r.json();
        });
      })
      .then(function () {
        it.shared = target;
        btnEl.classList.toggle("on", target);
        btnEl.textContent = target ? "🌐" : "👁";
        btnEl.title = target ? t("anno.shared.on.title")
                             : t("anno.shared.off.title");
        if (rowEl) {
          rowEl.classList.toggle("anno-private", !target);
          // P1-7：同步「私有」徽章（增/删 DOM），避免切换后徽章残留/缺失。
          var labelEl = rowEl.querySelector(".ai-title .ai-label");
          if (labelEl) {
            var old = labelEl.parentNode.querySelector(".anno-private-badge");
            if (target) {
              if (old) old.remove();
            } else if (!old) {
              var badge = document.createElement("span");
              badge.className = "anno-private-badge";
              badge.textContent = t("anno.private.badge");
              labelEl.parentNode.insertBefore(badge, labelEl.nextSibling);
            }
          }
        }
        toast(target ? t("anno.set.public") : t("anno.set.private"), "success");
      })
      .catch(function (e) { toast(t("anno.update.fail3", { e: e.message }), "error"); })
      .finally(function () { btnEl.disabled = false; });
  }

  // 点击标注条目：fitBounds（按类型算包围盒）+ 在画布上选中高亮该标注。
  // 不再画黄色临时 ROI 框（旧实现会残留且对箭头/描图显示 "0mm × 0mm"，
  // 还会覆盖 state.roi 破坏 ROI 模式选区）。改为复用既有的"选中态高亮"：
  // 被 editItem 选中的标注在 redrawAnnoCanvas/drawAnnoItem 中以蓝色描边。
  function jumpToAnno(it) {
    if (!state.slide || !viewer || !viewer.viewport) return;
    var typ = it.type || "rect";
    var x, y, side;
    if (typ === "arrow") {
      x = Math.min(it.x1, it.x2); y = Math.min(it.y1, it.y2);
      side = Math.max(Math.abs(it.x2 - it.x1), Math.abs(it.y2 - it.y1));
      side = Math.max(side, 1);
    } else if (typ === "freehand") {
      var xs = it.points.map(function (p) { return p[0]; });
      var ys = it.points.map(function (p) { return p[1]; });
      x = Math.min.apply(null, xs); y = Math.min.apply(null, ys);
      side = Math.max(Math.max.apply(null, xs) - x, Math.max.apply(null, ys) - y);
      side = Math.max(side, 1);
    } else {
      // 升级 C：按真实 w/h 包围（不重新正方形化）
      x = it.x; y = it.y;
      side = Math.max(rectItemW(it), rectItemH(it));
    }
    // 扩 20% 边距
    var pad = side * 0.2;
    try {
      var rect = viewer.viewport.imageToViewportRectangle(
        x - pad, y - pad, side + pad * 2, side + pad * 2);
      viewer.viewport.fitBounds(rect);
    } catch (e) {}

    // 选中高亮：flatItems 是 rebuildFlatItems 生成的副本，it 来自面板分组，
    // 引用不同，需按 token+ts+type 在 flatAnnoItems() 里找到匹配副本再选中。
    if (!state.showAnno) {
      state.showAnno = true;
      syncAnnoAllBtns();
    }
    var match = null;
    var items = flatAnnoItems();
    for (var i = 0; i < items.length; i++) {
      var f = items[i];
      if (f.token === it.token && Number(f.ts) === Number(it.ts) &&
          (f.type || "rect") === (it.type || "rect")) { match = f; break; }
    }
    if (match) {
      editItem = match;     // 选中态：drawAnnoItem 会给蓝色描边
      state.focusAnno = match; // 跳转/选中该条 → 只显示它
      editing = false;      // 只高亮，不开可拖动编辑态
      closeEditCard();      // 不弹编辑卡（仅点击行，非"编辑"按钮）
      redrawAnnoCanvas();
    }
    return match;           // 供 jumpAndEditAnno 复用匹配结果
  }

  // ---------- 编辑卡（标注面板顶部） + 删除 ----------
  // 显式编辑态：非编辑态只显示「✎ 编辑」入口，点它才进入可拖动编辑态；
  // 备注 textarea 两种状态下都可直接改（备注改动不属于"移动"）。
  function openEditCard(it) {
    var wrap = $("anno-edit-wrap");
    if (!wrap) return;
    var typ = it.type || "rect";
    var titleText = typ === "arrow" ? t("edit.title.arrow") : (typ === "freehand" ? t("edit.title.free") : t("edit.title.rect"));
    wrap.innerHTML = "";
    var card = document.createElement("div");
    card.className = "anno-edit-card";
    var head = document.createElement("div");
    head.className = "aec-head";
    head.textContent = titleText;
    card.appendChild(head);
    var ta = document.createElement("textarea");
    ta.className = "aec-note";
    ta.maxLength = 500;
    ta.placeholder = t("edit.note.ph");
    ta.value = it.note || "";
    ta.rows = 2;
    card.appendChild(ta);
    var ops = document.createElement("div");
    ops.className = "aec-ops";
    if (editing) {
      // 编辑态：保存 / 取消 / 删除
      var saveB = document.createElement("button");
      saveB.className = "btn primary small"; saveB.textContent = t("edit.save");
      var cancelB = document.createElement("button");
      cancelB.className = "btn secondary small"; cancelB.textContent = t("edit.cancel");
      var delB = document.createElement("button");
      delB.className = "btn danger small"; delB.textContent = t("edit.del");
      ops.appendChild(delB); ops.appendChild(cancelB); ops.appendChild(saveB);
      card.appendChild(ops);
      wrap.appendChild(card);
      wrap.style.display = "block";

      saveB.addEventListener("click", function () { commitAdminEdit(it, ta.value); });
      cancelB.addEventListener("click", function () { cancelAdminEdit(it); });
      delB.addEventListener("click", function () {
        delB.disabled = true;
        deleteAnnoItem(it);
      });
      ta.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); commitAdminEdit(it, ta.value); }
      });
    } else {
      // 非编辑态：✎ 编辑 / 保存 / 删除
      var editB = document.createElement("button");
      editB.className = "btn small"; editB.textContent = t("edit.enter");
      editB.title = t("edit.enter.title");
      var saveB2 = document.createElement("button");
      saveB2.className = "btn primary small"; saveB2.textContent = t("edit.save");
      var delB2 = document.createElement("button");
      delB2.className = "btn danger small"; delB2.textContent = t("edit.del");
      ops.appendChild(delB2); ops.appendChild(editB); ops.appendChild(saveB2);
      card.appendChild(ops);
      wrap.appendChild(card);
      wrap.style.display = "block";

      editB.addEventListener("click", function () {
        editing = true;
        redrawAnnoCanvas();
        openEditCard(it);
      });
      saveB2.addEventListener("click", function () { commitAdminEdit(it, ta.value); });
      delB2.addEventListener("click", function () {
        delB2.disabled = true;
        deleteAnnoItem(it);
      });
      ta.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); commitAdminEdit(it, ta.value); }
      });
    }
  }

  function closeEditCard() {
    var wrap = $("anno-edit-wrap");
    if (wrap) { wrap.innerHTML = ""; wrap.style.display = "none"; }
  }

  // 收集编辑后几何（图片坐标，round 整数，clamp ≥0）。升级 C：rect 以成对
  // w/h 提交（v2 契约），附带 expected_revision（CAS，§6.1）。
  function buildEditGeom(it) {
    var typ = it.type || "rect";
    var g = {};
    if (typ === "rect") {
      g.x = Math.max(0, Math.round(it.x));
      g.y = Math.max(0, Math.round(it.y));
      g.w = clamp(Math.round(rectItemW(it)), 1, RECT_MAX_SIDE_PX);
      g.h = clamp(Math.round(rectItemH(it)), 1, RECT_MAX_SIDE_PX);
    } else if (typ === "arrow") {
      g.x1 = Math.max(0, Math.round(it.x1));
      g.y1 = Math.max(0, Math.round(it.y1));
      g.x2 = Math.max(0, Math.round(it.x2));
      g.y2 = Math.max(0, Math.round(it.y2));
    } else if (typ === "freehand") {
      g.points = (it.points || []).map(function (p) {
        return [Math.max(0, Math.round(p[0])), Math.max(0, Math.round(p[1]))];
      });
    }
    return g;
  }

  // 提交管理员编辑：PATCH geom + note（index 直接用 it.index，无则兜底反推）。
  // 升级 C（§6.1）：携带 expected_revision（CAS）；冲突（409）显示当前版本
  // ——重新拉取服务端最新状态，不静默覆盖。
  function commitAdminEdit(it, noteVal) {
    var geom = buildEditGeom(it);
    var body = { geom: geom, note: noteVal };
    if (Number(it.revision) > 0) body.expected_revision = Number(it.revision);
    // rect 的 size_mm 前端重算（仅真正方形的兼容展示字段；v2 附属信息）
    if ((it.type || "rect") === "rect" && geom.w === geom.h &&
        state.mppX && state.mppX > 0) {
      body.geom.size_mm = Math.round(geom.w * state.mppX / 1000 * 100) / 100;
    }
    resolveIndexFast(it)
      .then(function (index) {
        return apiFetch("/api/annotation/" + encodeURIComponent(it.token) + "/" + index, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }).then(function (r) {
          if (r.status === 409) {
            // CAS 冲突：显示当前版本，不静默覆盖
            return r.json().then(function (j) {
              var err = new Error(j.error || "revision_conflict");
              err.conflict = true;
              err.currentRevision = j.current_revision;
              throw err;
            });
          }
          if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("save.fail")); });
          return r.json();
        });
      })
      .then(function () {
        toast(t("edit.saved"), "success");
        editItem = null;
        editing = false;
        closeEditCard();
        refreshCurrentAnnotations();
        loadAnnotationsIndex().then(function () {
          renderProjects(allProjects);
          renderUnfiled();
        });
      })
      .catch(function (e) {
        if (e && e.conflict) {
          toast(t("edit.conflict", { rev: e.currentRevision != null ? e.currentRevision : "?" }), "error");
          // 拉取当前版本并恢复显示（不保留本地未提交的几何修改）
          editItem = null;
          editing = false;
          closeEditCard();
          refreshCurrentAnnotations();
        } else {
          toast(t("save.fail2", { e: e.message }), "error");
        }
      });
  }

  function cancelAdminEdit(it) {
    editItem = null;
    editing = false;
    closeEditCard();
    refreshCurrentAnnotations();
  }

  // 删除标注（管理员，任意来源）：
  // 幂等 + 过期自动重试。后端 index 是该 token 下按插入序的序号，数据变动后
  // 本地缓存 index（it.index）可能过期 → 后端 404「标注不存在」。处理：
  //   1) 先按 resolveIndexFast（优先 it.index）发 DELETE；
  //   2) 若 404：改用 resolveAnnoIndex（重新拉 /api/share/rois 按 slide+ts+几何
  //      反推最新 index）重试 DELETE 一次；
  //   3) 若 resolveAnnoIndex 也找不到（抛"未找到对应标注"）或重试仍 404 → 说明
  //      该标注在服务端已不存在，删除本就幂等，视为成功，走乐观移除 + toast；
  //   4) 非 404 错误（网络/403 等）按原逻辑 toast「删除失败」并刷新恢复。
  function deleteAnnoItem(it) {
    // 发 DELETE，返回 { ok, status }：成功 ok=true；失败携带 HTTP status 供上层
    // 区分 404（幂等可放过）与其他错误（需报错恢复）。
    function sendDelete(index) {
      return apiFetch("/api/annotation/" + encodeURIComponent(it.token) + "/" + index, {
        method: "DELETE",
      }).then(function (r) {
        if (r.ok) return { ok: true, status: r.status };
        // 消费 body 以释放流，失败也无所谓（仅取 status）
        return r.json().catch(function () { return {}; }).then(function () {
          return { ok: false, status: r.status };
        });
      });
    }

    // 乐观更新：成功路径与"视为已删除"路径共用，立即反馈 + 后台异步同步。
    function applyAnnoRemoved() {
      // 1) flatItems 按引用移除（画布数据源）
      var items = flatAnnoItems();
      var fi = items.indexOf(it);
      if (fi >= 0) items.splice(fi, 1);
      // 2) currentAnnotations 分组中按引用移除，grp.count--，空组剔除
      if (currentAnnotations && currentAnnotations.annotations) {
        var groups = currentAnnotations.annotations;
        for (var gi = groups.length - 1; gi >= 0; gi--) {
          var g = groups[gi];
          var ii = (g.items || []).indexOf(it);
          if (ii >= 0) {
            g.items.splice(ii, 1);
            g.count = Math.max(0, (g.count || 1) - 1);
            if (g.items.length === 0) groups.splice(gi, 1);
          }
        }
      }
      // 3) 若当前编辑/选中项正是它，清选中并关编辑卡
      //    （editItem 多为 flatItems 副本，引用不等时按 token+ts+type 判定）
      if (editItem && (editItem === it ||
          (editItem.token === it.token && Number(editItem.ts) === Number(it.ts) &&
           (editItem.type || "rect") === (it.type || "rect")))) {
        editItem = null;
        editing = false;
        closeEditCard();
      }
      // focusAnno 若指向被删项（按引用或 token+ts+type 判定）→ 清空恢复显示全部
      if (state.focusAnno && (state.focusAnno === it ||
          (state.focusAnno.token === it.token && Number(state.focusAnno.ts) === Number(it.ts) &&
           (state.focusAnno.type || "rect") === (it.type || "rect")))) {
        state.focusAnno = null;
      }
      // 4) 重建扁平缓存 + 重绘 + 面板即时重渲 + 立即 toast
      rebuildFlatItems();
      redrawAnnoCanvas();
      if (annoPanelOpen) renderAnnoPanel((currentAnnotations || {}).annotations || []);
      toast(t("del.anno.done"), "success");
      // ---- 后台异步同步（不阻塞上面的即时反馈）----
      refreshCurrentAnnotations();
      // 全量索引只影响项目/未归类行的计数徽章，后台慢慢同步即可
      loadAnnotationsIndex().then(function () {
        renderProjects(allProjects);
        renderUnfiled();
      });
    }

    resolveIndexFast(it)
      .then(function (index) {
        return sendDelete(index).then(function (res) {
          if (res.ok) return { treated: true };
          // 第一次 404：index 可能过期，用 resolveAnnoIndex 反推最新 index 重试一次
          if (res.status === 404) {
            return resolveAnnoIndex(it)
              .then(function (freshIndex) { return sendDelete(freshIndex); })
              .then(function (res2) {
                if (res2.ok) return { treated: true };
                // 重试仍 404 → 服务端已无此标注，删除幂等，视为成功
                if (res2.status === 404) return { treated: true, alreadyGone: true };
                // 其他错误冒泡到 catch
                throw new Error(t("del.fail") + " (" + res2.status + ")");
              })
              .catch(function (e) {
                // resolveAnnoIndex 抛"未找到对应标注"/"Annotation not found" → 服务端已无此标注，视为成功
                if (e && /未找到对应标注|Annotation not found|not found/i.test(e.message)) {
                  return { treated: true, alreadyGone: true };
                }
                throw e; // 其余错误继续冒泡
              });
          }
          // 非 404 错误：报错并在 catch 中刷新恢复
          throw new Error(t("del.fail") + " (" + res.status + ")");
        });
      })
      .then(function (outcome) {
        // 成功或"已不存在视为成功"，统一走乐观移除
        applyAnnoRemoved();
      })
      .catch(function (e) {
        toast(t("del.fail2", { e: (e && e.message ? e.message : t("del.unknown")) }), "error");
        // 失败恢复：重新拉取真实状态
        refreshCurrentAnnotations();
      });
  }

  // 跳转并打开编辑卡（标注面板"编辑"按钮）：
  // 复用 jumpToAnno 的定位 + 选中高亮，再对匹配项打开编辑卡。
  function jumpAndEditAnno(it) {
    var match = jumpToAnno(it);
    if (match) {
      editing = false;        // 打开"查看态"编辑卡（含 ✎ 编辑入口）
      openEditCard(match);
    }
  }

  // ---------- 删除切片 ----------
  function deleteSlide(name) {
    if (!confirm(t("del.slide.confirm", { name: name }))) return;
    apiFetch("/api/slide/" + encodeURIComponent(name), { method: "DELETE" })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error); });
        if (state.slide && state.slide.name === name) {
          state.slide = null; state.mppX = null; state.roiMode = null;
          els.currentSlide.textContent = t("header.no.slide");
          updateMppSetterVisibility();
          if (roiBox) exitRoi();
          if (viewer) viewer.close();
        }
        toast(t("del.slide.done", { name: name }), "success");
        loadAll();
      })
      .catch(function (e) { toast(t("del.slide.fail", { e: e.message }), "error"); });
  }

  // ---------- 上传 ----------
  // 上传错误信息：已知机器码翻成可读文案，其余回退服务端 error 字段
  // （多为中文描述）或 HTTP 状态码。U3 补充 V2 分片机器码（offset_mismatch/
  // hash_mismatch/upload_state_conflict 等，upload-resumable-fix-plan §3.6）。
  function uploadErrorMessage(xhr, data) {
    var code = (data && (data.code || data.error)) || "";
    var status = (xhr && xhr.status) || 0;
    if (code === "csrf_required") return tt("upload.err.csrf");
    if (code === "upload_guard_unavailable") return tt("upload.err.guard");
    if (code === "name_unavailable") return tt("upload.err.name");
    if (code === "offset_mismatch") return tt("upload.err.offset_mismatch");
    if (code === "hash_mismatch") return tt("upload.err.hash_mismatch");
    if (code === "upload_state_conflict") return tt("upload.err.state_conflict");
    if (code === "use_legacy_upload") return tt("upload.err.use_legacy");
    if (code === "invalid_slide") return tt("upload.err.invalid_slide");
    if (code === "slide_open_unsupported") return tt("upload.err.slide_open_unsupported");
    if (code === "slide_open_failed") return tt("upload.err.slide_open_failed");
    if (code === "commit_retryable") return tt("upload.err.commit_retry");
    if (code === "size_mismatch") return tt("upload.err.size_mismatch");
    if (code === "upload_too_large" || status === 413) return tt("upload.err.too_large");
    if (status === 507) return tt("upload.err.disk");
    return code || status;
  }

  // ---------- 多文件独立进度行（U3 §3.5：修共用进度条的 bug） ----------
  // 每个上传文件一行（名称 + 独立进度条 + 三段状态文本）；行挂在侧栏
  // #upload-progress-list 容器下，完成后短暂保留再移除。容器缺失（旧模板）
  // 时回退到旧的单进度条元素。
  function humanSize(n) {
    if (n >= 1024 * 1024 * 1024) return (n / (1024 * 1024 * 1024)).toFixed(1) + " GB";
    if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }

  function makeUploadRow(file) {
    var host = els.uploadProgressList;
    var row = document.createElement("div");
    var nameEl = document.createElement("div");
    var barWrap = document.createElement("div");
    var bar = document.createElement("div");
    var statusEl = document.createElement("div");
    row.className = "upload-item";
    nameEl.className = "upload-item-name";
    barWrap.className = "upload-item-bar";
    bar.className = "upload-item-bar-fill";
    statusEl.className = "upload-item-status";
    nameEl.textContent = (file && file.name || "?") + " · " +
      humanSize((file && file.size) || 0);
    barWrap.appendChild(bar);
    row.appendChild(nameEl);
    row.appendChild(barWrap);
    row.appendChild(statusEl);
    var fallback = !host || !host.appendChild;
    if (!fallback) host.appendChild(row);
    var removed = false;
    function removeLater(delay) {
      if (removed) return;
      removed = true;
      setTimeout(function () {
        if (row.parentNode && row.parentNode.removeChild) {
          row.parentNode.removeChild(row);
        }
      }, delay);
    }
    return {
      // 三段状态：正在传输（confirmed_offset 为准）→ 服务端校验 → 入库完成
      setStage: function (stageKey, frac) {
        var label = tt(stageKey);
        if (frac !== undefined && frac !== null && isFinite(frac)) {
          label += " " + Math.round(frac * 100) + "%";
        }
        statusEl.textContent = label;
        if (fallback) {
          // 旧模板回退：写入共用进度条（单文件场景行为与旧版一致）
          if (els.progressWrap && els.progressWrap.style) els.progressWrap.style.display = "block";
          if (els.progressBar && els.progressBar.style) els.progressBar.style.width = Math.round((frac || 0) * 100) + "%";
          if (els.progressText) els.progressText.textContent = label;
        } else if (bar.style) {
          bar.style.width = Math.round((frac || 0) * 100) + "%";
        }
      },
      markError: function () {
        if (row.classList) row.classList.add("upload-item-error");
        if (bar && bar.classList) bar.classList.add("upload-item-bar-error");
      },
      finish: function (keepMs) {
        if (fallback) {
          if (els.progressWrap && els.progressWrap.style) els.progressWrap.style.display = "none";
        } else {
          removeLater(keepMs === undefined ? 6000 : keepMs);
        }
      },
      _row: row,
    };
  }

  // ---------- Upload V2：分片续传（U3；docs/upload-resumable-fix-plan §3） ----------
  // 阈值唯一权威来源是服务端 UPLOAD_V2_THRESHOLD_BYTES，经模板 bootstrap
  // （HP_APP_BOOTSTRAP.capabilities.upload_v2_threshold_bytes）下发（上传修复
  // A1，替换旧前端 128MiB 硬编码双来源）；解析失败回落 16MiB。ZIP/MRXS 留旧
  // 接口（§3.4 首版只支持单文件 WSI）。分片严格串行单并发（§3.2.2），每片算
  // SHA-256（Web Crypto 对该片 ArrayBuffer，不整文件入内存）；offset 以服务端
  // confirmed_offset 为权威，offset_mismatch 时对齐重传（§3.2.1）。
  var UPLOAD_V2_THRESHOLD_FALLBACK = 16 * 1024 * 1024;

  function resolveUploadV2Threshold() {
    try {
      var caps = window.HP_APP_BOOTSTRAP && window.HP_APP_BOOTSTRAP.capabilities;
      var n = caps && Number(caps.upload_v2_threshold_bytes);
      if (typeof n === "number" && isFinite(n) && n > 0) return n;
    } catch (e) { /* bootstrap 缺失/畸形：回落 */ }
    return UPLOAD_V2_THRESHOLD_FALLBACK;
  }

  var UPLOAD_V2_THRESHOLD = resolveUploadV2Threshold();

  function shouldChunkUpload(file) {
    if (!file || typeof file.size !== "number") return false;
    var name = file.name || "";
    var ext = name.slice(name.lastIndexOf(".") + 1).toLowerCase();
    if (ext === "zip" || ext === "mrxs") return false;  // §3.4：ZIP/MRXS 走旧接口
    return file.size >= UPLOAD_V2_THRESHOLD;
  }

  function uploadResumeKey(file) {
    // 文件指纹（名+大小+mtime）：刷新后据此找回未完成任务（§3.5 断点恢复）
    return "pt.upload.v2::" + (file.name || "") + ":" + file.size + ":" +
      (file.lastModified || 0);
  }

  function sha256Hex(buf) {
    // 单片哈希：Web Crypto 只需该片入内存（整文件哈希受限于无增量 API，
    // 创建时不带 sha256_expected；commit 时服务端复算为权威，§3.2.3 裁决）
    return crypto.subtle.digest("SHA-256", buf).then(function (digest) {
      var bytes = new Uint8Array(digest);
      var hex = "";
      for (var i = 0; i < bytes.length; i++) {
        hex += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16);
      }
      return hex;
    });
  }

  // XHR 发送（PUT 分片二进制用；与 apiFetch 同一 CSRF 双提交头契约）
  function xhrSend(method, url, body, opts) {
    opts = opts || {};
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      if (opts.onProgress) {
        xhr.upload.addEventListener("progress", opts.onProgress);
      }
      xhr.addEventListener("load", function () {
        var data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) { /* 非 JSON body */ }
        resolve({ status: xhr.status, data: data });
      });
      xhr.addEventListener("error", function () { reject({ network: true }); });
      xhr.open(method, url);
      var tok = csrfToken();
      if (tok) xhr.setRequestHeader("X-CSRF-Token", tok);
      xhr.send(body);
    });
  }

  function uploadV2Chunks(file, task, row) {
    // 严格串行：从 task.offset（= 服务端 confirmed_offset）逐片推进
    var size = file.size;
    function nextChunk() {
      var offset = task.offset;
      if (offset >= size) return Promise.resolve();
      var end = Math.min(offset + task.chunk_size, size);
      return file.slice(offset, end).arrayBuffer().then(function (buf) {
        return sha256Hex(buf).then(function (hex) {
          return xhrSend(
            "PUT",
            "/api/uploads/" + encodeURIComponent(task.upload_id) +
              "/chunk?offset=" + offset + "&sha256=" + hex,
            buf,
            {
              onProgress: function (e) {
                // 乐观的片内发送进度（上限 99.9%，最终以 confirmed_offset 为准）
                if (e.lengthComputable) {
                  row.setStage("upload.stage.transferring",
                    Math.min((offset + e.loaded) / size, 0.999));
                }
              },
            });
        });
      }).then(function (resp) {
        if (resp.status === 200 && resp.data &&
            typeof resp.data.confirmed_offset === "number") {
          // 服务端权威进度（§3.5：不用 XHR 本地发送进度）
          task.offset = resp.data.confirmed_offset;
          row.setStage("upload.stage.transferring", task.offset / size);
          return nextChunk();
        }
        if (resp.status === 409 && resp.data &&
            resp.data.code === "offset_mismatch" &&
            typeof resp.data.confirmed_offset === "number") {
          // 对齐重传（§3.2.1）：按服务端 confirmed_offset 回退/前进后重发
          task.offset = resp.data.confirmed_offset;
          return nextChunk();
        }
        throw { status: resp.status, data: resp.data };
      });
    }
    return nextChunk();
  }

  function uploadFileV2(file, row) {
    var key = uploadResumeKey(file);
    var task = null;
    Promise.resolve().then(function () {
      // 1) 刷新恢复：按文件指纹找未完成任务，GET 状态后从 confirmed_offset 续传
      var saved = null;
      try { saved = JSON.parse(localStorage.getItem(key) || "null"); } catch (e) { saved = null; }
      if (!saved || !saved.upload_id || saved.declared_size !== file.size) return null;
      row.setStage("upload.stage.transferring", 0);
      return apiFetch("/api/uploads/" + encodeURIComponent(saved.upload_id))
        .then(function (r) {
          if (!r.ok) return null;  // 403/404/409 等：任务没了 → 重新创建
          return r.json().then(function (body) {
            if (!body || body.state !== "active") return null;
            return { upload_id: saved.upload_id,
                     chunk_size: body.chunk_size,
                     offset: body.confirmed_offset | 0 };
          });
        })
        .catch(function () { return null; });
    }).then(function (resumed) {
      if (resumed) { task = resumed; return; }
      // 2) 创建新任务（初始化即预占配额，服务端给 chunk_size）
      row.setStage("upload.stage.transferring", 0);
      return apiFetch("/api/uploads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, declared_size: file.size }),
      }).then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok || !body || !body.upload_id) {
            throw { status: r.status, data: body };
          }
          task = { upload_id: body.upload_id,
                   chunk_size: body.chunk_size,
                   offset: body.confirmed_offset | 0 };
          try {
            localStorage.setItem(key, JSON.stringify({
              upload_id: task.upload_id, declared_size: file.size,
              chunk_size: task.chunk_size }));
          } catch (e) { /* localStorage 不可用：仅失去刷新恢复 */ }
        });
      });
    }).then(function () {
      // 3) 串行传完全部分片
      return uploadV2Chunks(file, task, row);
    }).then(function () {
      // 4) 服务端校验（commit 三段式：整文件复算 + OpenSlide + 原子提升）
      row.setStage("upload.stage.validating");
      return apiFetch("/api/uploads/" + encodeURIComponent(task.upload_id) +
                      "/commit", { method: "POST" });
    }).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) throw { status: r.status, data: body };
        return body;
      });
    }).then(function () {
      try { localStorage.removeItem(key); } catch (e) { /* 同上 */ }
      row.setStage("upload.stage.done");
      row.finish();
      toast(t("upload.done", { name: file.name }), "success");
      loadAll();
      openSlide(file.name);
    }).catch(function (err) {
      var data = (err && err.data) || null;
      var status = (err && err.status) || 0;
      var msg;
      if (err && err.network) {
        // 网络中断：任务与已传分片保留，刷新后可从 confirmed_offset 续传
        msg = tt("upload.err.resume");
      } else {
        msg = uploadErrorMessage({ status: status }, data);
      }
      // 确定性失败（§3.1）：原任务不可再用，清恢复记录（下次全新上传）；
      // A0 新增 slide_open_* 稳定码同属确定性失败
      var code = data && data.code;
      if (code === "hash_mismatch" || code === "invalid_slide" ||
          code === "slide_open_unsupported" || code === "slide_open_failed" ||
          code === "name_unavailable" || code === "size_mismatch") {
        try { localStorage.removeItem(key); } catch (e) { /* 同上 */ }
      }
      row.markError();
      row.setStage("upload.stage.failed");
      row.finish(10000);
      toast(t("upload.fail", { e: msg }), "error");
    });
  }

  function uploadFile(file) {
    if (!file) return;
    var row = makeUploadRow(file);
    if (shouldChunkUpload(file)) {
      uploadFileV2(file, row);
      return;
    }
    uploadFileLegacy(file, row);
  }

  // 旧单请求上传：小文件与 ZIP/MRXS（§3.4 并存）
  function uploadFileLegacy(file, row) {
    var formData = new FormData();
    formData.append("file", file);
    var xhr = new XMLHttpRequest();
    row.setStage("upload.stage.transferring", 0);
    xhr.upload.addEventListener("progress", function (e) {
      if (e.lengthComputable) {
        row.setStage("upload.stage.transferring", e.loaded / e.total);
      }
    });
    xhr.addEventListener("load", function () {
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) { row.finish(); toast(t("upload.parse.fail"), "error"); return; }
      if (xhr.status >= 200 && xhr.status < 300) {
        row.setStage("upload.stage.done");
        row.finish();
        toast(t("upload.done", { name: data.name }), "success");
        loadAll();
        openSlide(data.name);
      } else {
        row.markError();
        row.setStage("upload.stage.failed");
        row.finish(10000);
        toast(t("upload.fail", { e: uploadErrorMessage(xhr, data) }), "error");
      }
    });
    xhr.addEventListener("error", function () {
      row.markError();
      row.setStage("upload.stage.failed");
      row.finish(10000);
      toast(t("upload.net.fail"), "error");
    });
    xhr.open("POST", "/api/upload");
    // 裸 XHR 与 apiFetch 同一 CSRF 契约：双提交头必须带上（上传修复 U1，
    // 漏头会被服务端 400 csrf_required 拒绝）
    xhr.setRequestHeader("X-CSRF-Token", csrfToken());
    xhr.send(formData);
  }
  // 供测试（tests/js/*.test.ts loadApp harness）驱动真实上传路径；
  // 与 HP_AUTH 同风格的命名空间导出，不进业务调用面
  window.HP_UPLOAD = {
    uploadFile: uploadFile,
    uploadFileV2: uploadFileV2,
    shouldChunkUpload: shouldChunkUpload,
    UPLOAD_V2_THRESHOLD: UPLOAD_V2_THRESHOLD,
  };
  // 供测试（升级 A）：侧栏开合控制器与偏好存取的真实逻辑入口
  window.HP_SIDEBAR = {
    createSidebarController: createSidebarController,
    sidebarPrefKey: sidebarPrefKey,
    parseSidebarPref: parseSidebarPref,
    readSidebarPref: readSidebarPref,
    writeSidebarPref: writeSidebarPref,
    syncViewerLayoutNow: syncViewerLayoutNow,
  };

  // ---------- 拖拽上传 ----------
  function setupDragDrop() {
    var wrap = els.viewerWrap;
    var counter = 0;
    wrap.addEventListener("dragenter", function (e) { e.preventDefault(); counter++; els.dropOverlay.classList.add("active"); });
    wrap.addEventListener("dragover", function (e) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
    wrap.addEventListener("dragleave", function (e) { e.preventDefault(); counter--; if (counter <= 0) { counter = 0; els.dropOverlay.classList.remove("active"); } });
    wrap.addEventListener("drop", function (e) {
      e.preventDefault(); counter = 0; els.dropOverlay.classList.remove("active");
      var files = e.dataTransfer.files;
      if (files && files.length > 0) { for (var i = 0; i < files.length; i++) uploadFile(files[i]); }
    });
  }

  // ---------- 事件绑定 ----------
  function bindEvents() {
    els.zoomIn.addEventListener("click", zoomIn);
    els.zoomOut.addEventListener("click", zoomOut);
    if (els.zoomNative) els.zoomNative.addEventListener("click", zoomNative);
    els.rotateBtn.addEventListener("click", rotate);
    els.flipBtn.addEventListener("click", flip);
    els.resetBtn.addEventListener("click", reset);
    els.saveBtn.addEventListener("click", saveCrop);
    els.saveAnnoBtn.addEventListener("click", saveAnno);
    els.mppSetBtn.addEventListener("click", setMpp);
    els.mppInput.addEventListener("keydown", function (e) { if (e.key === "Enter") setMpp(); });

    // 退出登录：POST /logout + CSRF（docs §10.14）
    if (els.logoutBtn) { els.logoutBtn.addEventListener("click", doLogout); }

    els.uploadBtn.addEventListener("click", function () { els.fileInput.click(); });
    els.fileInput.addEventListener("change", function () {
      if (this.files && this.files[0]) { uploadFile(this.files[0]); this.value = ""; }
    });

    // 标注
    els.annoBtn.addEventListener("click", function () {
      if (annoPanelOpen) { closeAnnoPanel(); } else { openAnnoPanel(); }
    });
    els.annoPanelClose.addEventListener("click", closeAnnoPanel);
    els.annoAllBtn.addEventListener("click", toggleAnnoAll);
    // 面板头部「显示全部标记」切换钮（与 toggleAnnoAll 同一逻辑）
    if (els.annoAllToggle) els.annoAllToggle.addEventListener("click", toggleAnnoAll);
    els.annoArrowBtn.addEventListener("click", function () { toggleDrawMode("arrow"); });
    els.annoFreeBtn.addEventListener("click", function () { toggleDrawMode("freehand"); });
    // 升级 C：单一矩形入口 + 紧凑设置区（旧 6/6.5 分段/滑块已移除）
    if (els.roiRectBtn) {
      els.roiRectBtn.addEventListener("click", toggleRectTool);
    }
    if (els.roiUnitSelect) {
      els.roiUnitSelect.addEventListener("change", function () {
        state.roiUnit = els.roiUnitSelect.value;
        syncRoiSettings();
      });
    }
    if (els.roiLockRatio) {
      els.roiLockRatio.addEventListener("change", function () {
        state.roiLockRatio = !!els.roiLockRatio.checked;
      });
    }
    if (els.roiPresetSelect) {
      els.roiPresetSelect.addEventListener("change", function () {
        state.roiPreset = els.roiPresetSelect.value;
        if (!state.roiPreset) return;
        // 预设只填入宽高数值（mm），不强制永远锁成正方形（§6.1）
        state.roiUnit = "mm";
        if (els.roiUnitSelect) els.roiUnitSelect.value = "mm";
        els.roiWInput.value = state.roiPreset;
        els.roiHInput.value = state.roiPreset;
        applyRectInputs();
      });
    }
    [els.roiWInput, els.roiHInput].forEach(function (inp) {
      if (!inp) return;
      inp.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); applyRectInputs(); }
      });
      inp.addEventListener("change", applyRectInputs);
    });

    // 标注画布层绘制事件
    var c = els.annoCanvas;
    c.addEventListener("pointerdown", onAnnoPointerDown);
    c.addEventListener("pointermove", onAnnoPointerMove);
    c.addEventListener("pointerup", onAnnoPointerUp);
    c.addEventListener("pointercancel", onAnnoPointerUp);
    window.addEventListener("resize", function () { resizeAnnoCanvas(); redrawAnnoCanvas(); });
    // 升级 C（§6.1）：Escape 取消未保存矩形选区（无输入焦点时）
    window.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      var t = e.target;
      var tag = t && t.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (rectToolActive()) onRectKeydown(e);
    });

    // 侧栏开合（升级 A）：菜单按钮切换（桌面=收起/展开、手机=抽屉）、
    // 遮罩点击关闭、Escape 关闭手机抽屉
    if (els.menuBtn) {
      els.menuBtn.addEventListener("click", function () { sidebarCtrl.toggle(); });
    }
    if (els.sidebarMask) {
      els.sidebarMask.addEventListener("click", function () { sidebarCtrl.closeDrawer(); });
    }
    document.addEventListener("keydown", function (e) {
      if (e && e.key === "Escape" && sidebarCtrl.isMobile() && sidebarCtrl.isDrawerOpen()) {
        sidebarCtrl.closeDrawer();
      }
    });

    // 无切片空态「选择切片」：展开侧栏并聚焦搜索框（§4.1）
    if (els.viewerEmptyPick) {
      els.viewerEmptyPick.addEventListener("click", function () {
        sidebarCtrl.expandAndFocusSearch();
      });
    }
    // 切片搜索：输入即过滤；条件保留在输入框里，侧栏收起/展开不丢失
    if (els.slideSearch) {
      els.slideSearch.addEventListener("input", function () {
        applySlideFilter(this.value);
      });
    }

    // 移动端 ⋯ 溢出面板（AI 读片 + 缩放徽章）
    bindTbbMore();

    // 新建项目
    els.newProjectBtn.addEventListener("click", function () {
      var showing = els.newProjectForm.style.display !== "none";
      toggleNewProjectForm(!showing);
    });
    els.npConfirm.addEventListener("click", function () { createProjectFromForm([]); });
    els.npCancel.addEventListener("click", function () { toggleNewProjectForm(false); });
    els.npName.addEventListener("keydown", function (e) { if (e.key === "Enter") els.npNote.focus(); });
    els.npNote.addEventListener("keydown", function (e) { if (e.key === "Enter") createProjectFromForm([]); });

    // 未归类
    els.unfiledToggle.addEventListener("click", function () {
      var sec = els.unfiledBody.closest(".section");
      if (sec) sec.classList.toggle("collapsed");
    });
    els.unfiledNewProject.addEventListener("click", function () {
      var slides = Object.keys(slideChecked).filter(function (k) { return slideChecked[k]; });
      if (slides.length === 0) { toast(t("unfiled.need.check"), "error"); return; }
      // 预填并打开表单：这里直接以选中切片创建项目
      toggleNewProjectForm(true);
      // 记录待加入切片，确认时带上
      pendingNewProjectSlides = slides;
      toast(t("unfiled.selected.tip", { n: slides.length }), "info");
    });

    // 分享
    els.shareExpiresSelect.addEventListener("change", function () {
      els.shareExpiresCustom.style.display = this.value === "custom" ? "inline-block" : "none";
    });
    els.shareCreateBtn.addEventListener("click", onShareCreateClick);
    els.shareResultCopy.addEventListener("click", function () { copyText(els.shareResultUrl.value); });
    els.shareMgrToggle.addEventListener("click", function () {
      var sec = els.shareMgrBody.closest(".section");
      if (sec) sec.classList.toggle("collapsed");
    });

    // 修改我的密码（owner/user 通用；docs §8.1）
    initChangePw();

    // user max_steps 只读同步（AI 预算管理 UI 已迁入 admin 插件，PR5）
    initAiMaxStepsSync();

    // 切片选择器
    els.pickerClose.addEventListener("click", closeSlidePicker);
    els.pickerConfirm.addEventListener("click", confirmSlidePicker);
    els.pickerMask.addEventListener("click", function (e) {
      if (e.target === els.pickerMask) closeSlidePicker();
    });

    // AI 读片助手：点击交由插件处理（发 panel.toggle）。插件未启用时 aiBtn 不渲染。
    if (els.aiBtn) {
      els.aiBtn.addEventListener("click", function () {
        hpRequest("panel.toggle", {}).catch(function () { /* 插件未启用：静默 */ });
      });
    }
  }

  // =========================================================================
  // HistoPilot HostBridge host 适配（Stage 2：同源同窗口）
  // -------------------------------------------------------------------------
  // 平台 host：把 viewer/state/selection/annotation 能力经 HostBridge 暴露给插件，
  // 并把插件的 notification/annotation/panel 事件转回平台动作。插件缺失（flag 关闭）时
  // 全部 hp* 调用静默降级，人工读片不受影响（Stage 2 验收项）。
  function hpReady() {
    return !!(window.HostBridgeHost && window.HistoPilot);
  }
  // Host→Plugin event（单向）
  function hpEmit(type, payload) {
    try { if (hpReady()) window.HostBridgeHost.emit(type, payload); } catch (e) {}
  }
  // Host→Plugin request（Promise；插件未启用时 reject，调用方自行 catch）
  function hpRequest(method, payload) {
    if (!hpReady()) return Promise.reject({ code: "plugin_disabled" });
    try { return window.HostBridgeHost.request(method, payload); }
    catch (e) { return Promise.reject({ code: "plugin_disabled" }); }
  }

  // 注册 host 侧能力（在 init 中调用，viewer 在 initViewer 后才就绪）
  function registerHostBridgeHandlers() {
    var host = window.HostBridgeHost;
    if (!host) return;
    // 通用插件权限门（Stage 5-2）：每个被 gate 的 host 方法入口先查
    // env.pluginInstallationId。未知 ID fail-closed；histopilot 仅因在
    // PRIVILEGED_PLUGIN_IDS 显式名单中才放行（不能靠「不在权限表」冒充）。
    // 同窗口执行仍不是安全边界（插件可触达 host 全局）；iframe sandbox 另做。
    // 用法：gate(method, fn(payload, env))，把 fn 包成 fn(payload, env) → 先 gate 再执行业务。
    function gate(method, fn) {
      return function (payload, env) {
        var pluginId = env && env.pluginInstallationId;
        var pp = window.PluginPermissions;
        if (pp && pp.gatePermission) {
          var denied = pp.gatePermission(pluginId, method, window.SVS_PLUGIN_PERMISSIONS);
          if (denied) throw denied;
        } else if (pluginId !== "histopilot") {
          throw { code: "permission_denied", message: "未知插件身份", retryable: false };
        }
        return fn(payload, env);
      };
    }
    // 握手期 bridge.negotiate 不在此注册：host-bridge.js 路由器原生应答（2026-08-16
    // 修复）——插件脚本先于 app.js 加载并立即握手时，等这里的 onRequest 注册会先
    // 收到 unknown_method（demo 实测）。业务方法才走下方注册表。
    // Plugin→Host request（被 gate 的方法：slide.getCurrent / selection.getBbox /
    // viewer.navigate / viewer.highlight / viewer.applyRenderContext /
    // annotation.create / annotation.read / annotation.focus）
    host.onRequest("slide.getCurrent", gate("slide.getCurrent", function () {
      if (!state.slide) return null;
      return { name: state.slide.name, width: state.slide.width, height: state.slide.height,
               mppX: state.slide.mppX, mppY: state.slide.mppY };
    }));
    host.onRequest("selection.getBbox", gate("selection.getBbox", function () { return currentSelectionBbox(); }));
    host.onRequest("viewer.navigate", gate("viewer.navigate", function (p) {
      // AI goto/snapshot 跳转：level-0 bbox → viewport.fitBounds。
      // （文档 {x,y,level} 在本阶段以 level-0 bbox 表达，agent 全程在图像坐标系工作）
      // R1（2026-09-05）：viewer 未就绪/几何非法回真实 error code，不再吞异常
      // 仍报 ok:true；请求携带 slide 标识且与当前切片不符（过期操作）时拒绝
      // （宽容缺省：旧插件不带 slide 时不拒绝）。
      p = p || {};
      if (p.slide && state.slide && String(p.slide) !== String(state.slide.name)) {
        throw { code: "stale_slide", message: "导航请求属于另一切片", retryable: false };
      }
      if (!viewer || !viewer.viewport) {
        throw { code: "viewer_not_ready", message: "查看器未就绪", retryable: true };
      }
      var nx = Number(p.x), ny = Number(p.y), nw = Number(p.w), nh = Number(p.h);
      if (!isFinite(nx) || !isFinite(ny) || !isFinite(nw) || !isFinite(nh) || nw <= 0 || nh <= 0) {
        throw { code: "invalid_geometry", message: "导航几何非法", retryable: false };
      }
      viewer.viewport.fitBounds(
        viewer.viewport.imageToViewportRectangle(nx, ny, nw, nh));
      return { ok: true };
    }));
    host.onRequest("viewer.highlight", gate("viewer.highlight", function (p) {
      // 插件叠加层：写入平台 aiOverlay 并重绘画布（替代插件直接写 aiOverlay/redrawAnnoCanvas）。
      // R1：逐框几何校验，非法回 invalid_geometry；slide 标识不符拒绝过期画框
      // （宽容缺省：旧插件不带时不拒绝）。
      p = p || {};
      if (p.slide && state.slide && String(p.slide) !== String(state.slide.name)) {
        throw { code: "stale_slide", message: "画框请求属于另一切片", retryable: false };
      }
      var boxes = Array.isArray(p.boxes) ? p.boxes : [];
      for (var bi = 0; bi < boxes.length; bi++) {
        var bb = boxes[bi] || {};
        var bx = Number(bb.x), by = Number(bb.y), bw = Number(bb.w), bh = Number(bb.h);
        if (!isFinite(bx) || !isFinite(by) || !isFinite(bw) || !isFinite(bh) || bw <= 0 || bh <= 0) {
          throw { code: "invalid_geometry", message: "叠加框几何非法", retryable: false };
        }
      }
      aiOverlay = boxes;
      redrawAnnoCanvas();
      return { ok: true };
    }));
    // 升级 E §8.2-3：历史配色恢复——插件把已持久化的 wire render_context
    //（通道配置，无短期令牌）发回平台，由平台现有通道控制器经公开 setter
    // 应用并按既有管线刷新显示令牌（只作用于人眼 Viewer，不触碰模型绑定）。
    // 校验：slide 匹配、fingerprint 形态、通道 1..8、index/颜色合法；不通过
    // 回 {ok:true, applied:false}，插件侧据此显示「历史配色未知」，不伪称一致。
    host.onRequest("viewer.applyRenderContext", gate("viewer.applyRenderContext", function (p) {
      p = p || {};
      if (p.slide && state.slide && String(p.slide) !== String(state.slide.name)) {
        throw { code: "stale_slide", message: "配色恢复请求属于另一切片", retryable: false };
      }
      var ctx = p.render_context;
      if (!ctx || !ctx.fingerprint || !Array.isArray(ctx.active_channels) || !ctx.active_channels.length) {
        return { ok: true, applied: false, reason: "no_context" };
      }
      if (!/^[0-9a-f]{64}$/i.test(String(ctx.fingerprint))) {
        return { ok: true, applied: false, reason: "bad_fingerprint" };
      }
      var chans = ctx.active_channels;
      if (chans.length > 8) return { ok: true, applied: false, reason: "too_many_channels" };
      for (var ci = 0; ci < chans.length; ci++) {
        var ch = chans[ci] || {};
        if (!Number.isInteger(ch.index) || ch.index < 0) return { ok: true, applied: false, reason: "bad_channel" };
        if (typeof ch.color !== "string" || !/^#[0-9a-fA-F]{6}$/.test(ch.color)) {
          return { ok: true, applied: false, reason: "bad_color" };
        }
      }
      if (!channelCtrl || !channelCtrl.isMultichannel || !channelCtrl.isMultichannel()) {
        return { ok: true, applied: false, reason: "not_multichannel" };
      }
      // 平台当前显示已是同一 context（fingerprint 一致）→ 无需恢复。
      var curFp = (channelCtrl.getFingerprint && channelCtrl.getFingerprint()) || null;
      if (curFp && String(curFp).toLowerCase() === String(ctx.fingerprint).toLowerCase()) {
        return { ok: true, applied: false, reason: "already_current" };
      }
      var wanted = {};
      chans.forEach(function (ch) { wanted[ch.index] = ch; });
      // 应用顺序（受服务端 1..8 约束）：先关掉不在目标的通道（至少保留 1 个，
      // 剩余的等目标通道激活后再关），再激活目标通道，最后逐通道套历史颜色
      //（经 setChannelColor 的 hex 校验）。任一通道被平台拒绝即视为未完全
      // 应用 → 插件侧显示「历史配色未知」，不伪称一致。
      var applied = true;
      (channelCtrl.selection || []).slice().forEach(function (idx) {
        if (!wanted[idx] && (channelCtrl.selection || []).length > 1) {
          if (!channelCtrl.setChannelActive(idx, false)) applied = false;
        }
      });
      chans.forEach(function (ch) {
        if (!channelCtrl.setChannelActive(ch.index, true)) applied = false;
      });
      (channelCtrl.selection || []).slice().forEach(function (idx) {
        if (!wanted[idx] && !channelCtrl.setChannelActive(idx, false)) applied = false;
      });
      chans.forEach(function (ch) {
        if (!channelCtrl.setChannelColor(ch.index, ch.color)) applied = false;
      });
      return { ok: true, applied: applied };
    }));
    // Stage 5-2：通用 SDK 插件经 bridge 创建测试标注。gate 后复用平台现有
    // /api/annotation POST 路径（rect 类型，payload 带 slide/x/y/side_px/label=text），
    // 成功后按现有模式刷新标注面板与索引并 return {ok:true,id}；失败 throw → host 回
    // ok:false, error={code:"annotation_create_failed",...}。
    host.onRequest("annotation.create", gate("annotation.create", function (p) {
      return createPluginAnnotation(p);
    }));
    host.onRequest("annotation.read", gate("annotation.read", function () {
      // 通用权限门演示方法（manifest 未声明 annotation:read 的插件会被稳定拒绝）。
      // 非特权插件不被允许批量读标注；已授权路径走 REST /api/annotations。
      throw { code: "permission_denied", message: "annotation.read 需经平台 REST 读取", retryable: false };
    }));
    // 标注卡点击聚焦：优先按 annotation_id 在 flatAnnoItems 里匹配 → 复用 jumpToAnno
    // （20% 边距 fitBounds + 选中蓝色描边 + focusAnno）。匹配不到但有几何 → 只定位
    // 视野并画临时 overlay 框（不动已有标注，不造假标注）。
    host.onRequest("annotation.focus", gate("annotation.focus", function (p) {
      p = p || {};
      var match = null;
      if (p.annotation_id) {
        var items = flatAnnoItems();
        for (var i = 0; i < items.length; i++) {
          if (String(items[i].annotation_id || "") === String(p.annotation_id)) { match = items[i]; break; }
        }
      }
      if (match) { jumpToAnno(match); return { ok: true, focused: true }; }
      // 升级 C：聚焦兜底优先成对 w/h；旧 side_px = 正方形兼容（不 max/min 冒充）
      var fw = Number(p.w) > 0 ? Number(p.w)
        : (Number(p.width_px) > 0 ? Number(p.width_px)
          : (Number(p.side_px) > 0 ? Number(p.side_px) : 0));
      var fh = Number(p.h) > 0 ? Number(p.h)
        : (Number(p.height_px) > 0 ? Number(p.height_px)
          : (Number(p.side_px) > 0 ? Number(p.side_px) : 0));
      if (viewer && viewer.viewport && p.x != null && fw > 0 && fh > 0) {
        var padX = fw * 0.2, padY = fh * 0.2;
        try {
          viewer.viewport.fitBounds(
            viewer.viewport.imageToViewportRectangle(p.x - padX, p.y - padY, fw + padX * 2, fh + padY * 2));
        } catch (e) {}
        aiOverlay = [{ x: p.x, y: p.y, w: fw, h: fh, magnification: "" }];
        redrawAnnoCanvas();
      }
      return { ok: true, focused: false };
    }));
    // Plugin→Host event
    host.onEvent("notification.show", function (p) {
      toast(p && p.msg, (p && p.type) || "info");
    });
    host.onEvent("annotation.changed", function () {
      // 插件落 AI 标注后 → 刷新当前切片标注面板与索引
      refreshCurrentAnnotations();
      loadAnnotationsIndex();
    });
    host.onEvent("panel.stateChanged", function (p) {
      // 插件打开 AI 面板时关闭标注面板（保留原 openAiPanel/openAiPanel 的互斥语义）
      if (p && p.open && annoPanelOpen) closeAnnoPanel();
    });
  }

  // 通用插件 annotation.create 的 host 端实现（Stage 5-2 / 升级 C）。入参
  // p: {text, x, y, w, h}（level-0 坐标，v2 成对 w/h 直通）或旧形态
  // {text, x, y, side_px}（正方形兼容）。**不再取 max(w,h) 正方形化**。
  // 复用 app.js 现有创建标注的 fetch 形态（见 saveAnnotation，POST /api/annotation，
  // body 含 slide/type/label + 几何字段），成功触发 refreshCurrentAnnotations +
  // loadAnnotationsIndex（与 saveAnnotation / 插件 annotation.changed 一致），
  // 返回 {ok:true, id}。失败 throw {code:"annotation_create_failed", message}。
  function createPluginAnnotation(p) {
    if (!state.slide) throw { code: "annotation_create_failed", message: "当前无切片", retryable: false };
    p = p || {};
    var body = {
      slide: state.slide.name,
      type: "rect",
      label: String(p.text != null ? p.text : "插件标注"),
      x: Math.round(Number(p.x) || 0),
      y: Math.round(Number(p.y) || 0),
    };
    var pw = Number(p.w), ph = Number(p.h), ps = Number(p.side_px);
    if (isFinite(pw) && pw > 0 && isFinite(ph) && ph > 0) {
      // v2：w/h 直通（插件的矩形不再被取 max 转正方形）
      body.w = Math.round(pw);
      body.h = Math.round(ph);
      // v2 与 side_px 同给：仅一致正方形的冗余兼容，矛盾组合拒绝
      if (isFinite(ps) && ps > 0 && !(Math.round(ps) === body.w && Math.round(ps) === body.h)) {
        throw { code: "annotation_create_failed", message: "side_px 与 w/h 冲突", retryable: false };
      }
    } else if (isFinite(ps) && ps > 0) {
      body.side_px = Math.round(ps); // 旧调用形态：正方形兼容
    } else {
      throw { code: "annotation_create_failed", message: "标注尺寸非法", retryable: false };
    }
    return apiFetch("/api/annotation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (!r.ok) {
        return r.json().then(function (j) {
          throw { code: "annotation_create_failed", message: (j && j.error) || "标注创建失败", retryable: false };
        });
      }
      return r.json();
    }).then(function (res) {
      refreshCurrentAnnotations();
      loadAnnotationsIndex().then(function () {
        if (typeof renderProjects === "function") { renderProjects(allProjects); renderUnfiled(); }
      }).catch(function () {});
      return { ok: true, id: res && (res.id || res.index) };
    });
  }

  // 当前选区 bbox（ROI 矩形 或 选中标注），供插件 selection.getBbox 使用。
  // 升级 C：level-0 {x,y,w,h}；不再以单边长冒充 w/h。
  function currentSelectionBbox() {
    if (state.roi && state.roi.w > 0 && state.roi.h > 0) {
      return { x: state.roi.x, y: state.roi.y, w: state.roi.w, h: state.roi.h };
    }
    if (editItem && editItem.type === "rect" && rectItemW(editItem) > 0) {
      return { x: editItem.x, y: editItem.y,
               w: rectItemW(editItem), h: rectItemH(editItem) };
    }
    return null;
  }

  // 标注面板行内的 AI 动作按钮（fork 快速问答 / branch 从此处深读）。
  // Stage 2：点击改为发 HostBridge 请求，由插件处理。
  // fork.open 的 anchorEl 为该行 DOM（STAGE2-DEVIATION：信封夹带 DOM 引用，仅同窗口可用）。
  function buildAnnoAiActions(container, annotationId, style) {
    if (!annotationId || !container) return;
    // 插件停用时不渲染 fork/branch 按钮（避免无功能的死按钮）
    if (!hpReady()) return;
    var op = (style === "op");
    var forkBtn = document.createElement("button");
    forkBtn.type = "button";
    forkBtn.className = op ? "ai-op ai-fork" : "ai-action-chip ai-fork";
    forkBtn.title = tt("anno.fork.quick.tip");
    forkBtn.innerHTML = '<span class="ai-act-ic">💬</span><span class="ai-act-tx">' +
      esc(tt("anno.fork.quick")) + "</span>";
    forkBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      hpRequest("fork.open", { annotationId: annotationId, anchorEl: container })
        .catch(function () { /* 插件未启用：静默 */ });
    });
    container.appendChild(forkBtn);

    var branchBtn = document.createElement("button");
    branchBtn.type = "button";
    branchBtn.className = op ? "ai-op ai-branch" : "ai-action-chip ai-branch";
    branchBtn.title = tt("anno.branch.deep.tip");
    branchBtn.innerHTML = '<span class="ai-act-ic">⑂</span><span class="ai-act-tx">' +
      esc(tt("anno.branch.deep")) + "</span>";
    branchBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      hpRequest("branch.open", { annotationId: annotationId })
        .catch(function () { /* 插件未启用：静默 */ });
    });
    container.appendChild(branchBtn);
  }

  // ---------- 启动 ----------
  // 装配侧栏开合控制器（升级 A）：真实 DOM/媒体查询/storage/userScope 注入
  function initSidebarController() {
    var mq = window.matchMedia ? window.matchMedia(SB_MOBILE_QUERY) : null;
    sidebarCtrl = createSidebarController({
      sidebar: els.sidebar,
      sidebarMask: els.sidebarMask,
      menuBtn: els.menuBtn,
      mq: mq,
      storage: safeLocalStorage(),
      scope: userScope,
      doc: document,
      onLayoutChange: syncViewerLayoutAfterSidebar,
      focusSearch: function () {
        if (els.slideSearch && typeof els.slideSearch.focus === "function") {
          try { els.slideSearch.focus(); } catch (e) { /* 忽略聚焦失败 */ }
        }
      },
    });
    sidebarCtrl.init();
    // 断点切换：清理手机遮罩、恢复当前设备布局状态（§4.1 末条）
    var mq = window.matchMedia ? window.matchMedia(SB_MOBILE_QUERY) : null;
    if (mq) {
      var onMqChange = function () { sidebarCtrl.onBreakpointChange(); };
      if (typeof mq.addEventListener === "function") mq.addEventListener("change", onMqChange);
      else if (typeof mq.addListener === "function") mq.addListener(onMqChange);
    }
  }

  function init() {
    initViewer();
    initSidebarController();
    bindEvents();
    setupDragDrop();
    initAuth();
    // 注册 HistoPilot HostBridge host 能力（插件未启用时为空操作）
    registerHostBridgeHandlers();
    // 初始折叠区状态（默认展开）
    var unfiledSec = els.unfiledBody.closest(".section");
    var shareSec = els.shareMgrBody.closest(".section");
    if (unfiledSec) unfiledSec.classList.remove("collapsed");
    if (shareSec) shareSec.classList.remove("collapsed");
    // 升级 A：启动时无切片，显示空态入口（openSlide 成功后隐藏）
    updateViewerEmptyState();
    loadAll();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // 语言切换：重渲染当前可见的动态面板（动态文本走 t()，重渲染即换语言）。
  // 静态 [data-i18n] 节点由 i18n.js 的 applyLang 直接刷新，这里只处理 JS 渲染的部分。
  document.addEventListener("hp-lang-change", function () {
    try {
      // 项目 / 未归类 / 分享列表（只要数据已加载就重渲）
      if (allProjects && allProjects.length >= 0) renderProjects(allProjects);
      renderUnfiled();
      if (allSharesCache) renderShareList(allSharesCache);
    } catch (e) {}
    try {
      // 标注面板（打开时才重渲）
      if (annoPanelOpen && currentAnnotations) {
        renderAnnoPanel(currentAnnotations.annotations || []);
      }
    } catch (e) {}
    // 升级 A：侧栏按钮文案/aria 随状态（展开↔收起）变化，切语言后重写
    try { if (sidebarCtrl) sidebarCtrl.refreshButton(); } catch (e) {}
    // AI 配置摘要 / 会话切换器的语言重渲由 HistoPilot 插件 bundle 自行监听 hp-lang-change 处理。
  });
})();
