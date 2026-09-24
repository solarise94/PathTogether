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
    "upload.stage.converting": { zh: "切片转换中", en: "Converting slide" },
    "upload.stage.done": { zh: "入库完成", en: "Completed" },
    "upload.stage.failed": { zh: "上传失败", en: "Upload failed" },
    "upload.err.conversion": { zh: "切片转换失败", en: "Slide conversion failed" },
    // COS 直传 Phase 4（§5 阶段文案固定六段 + 稳定机器码；i18n.js 为主源，
    // 此处兜底）。上传 100% ≠ 可查看，绝不合成全流程百分比
    "upload.cos.toggle": { zh: "云端直传", en: "Cloud direct upload" },
    "upload.cos.toggle.tip": { zh: "手动选择 COS 云端直传；可查看前还需服务器接收与校验", en: "Manual COS direct upload; the server still needs to receive and validate before viewing" },
    "upload.cos.stage.waiting_space": { zh: "等待暂存空间", en: "Waiting for staging space" },
    "upload.cos.stage.uploading": { zh: "正在上传", en: "Uploading" },
    "upload.cos.stage.awaiting_server": { zh: "等待服务器接收", en: "Waiting for server" },
    "upload.cos.stage.downloading": { zh: "服务器接收中", en: "Server downloading" },
    "upload.cos.stage.validating": { zh: "正在校验", en: "Validating" },
    "upload.cos.stage.readiness": { zh: "准备可查看", en: "Preparing to view" },
    "upload.cos.stage.viewable": { zh: "可查看", en: "Viewable" },
    "upload.cos.queue": { zh: "第 {n} 位", en: "position {n}" },
    "upload.cos.cancel": { zh: "取消", en: "Cancel" },
    "upload.cos.cancelled": { zh: "已取消", en: "Cancelled" },
    "upload.cos.retry": { zh: "重试", en: "Retry" },
    "upload.cos.retry_platform": { zh: "改用平台上传", en: "Retry with platform upload" },
    "upload.cos.resume_hint": { zh: "上传未完成；重新选择同名文件可续传", en: "Upload unfinished; re-select the same file to resume" },
    "upload.cos.resume_confirm": { zh: "检测到「{name}」有未完成的云端直传任务，续传已上传的分块？", en: "An unfinished cloud upload for \"{name}\" exists. Resume from the uploaded parts?" },
    "upload.cos.err.exceeds_admission": { zh: "文件超过云端直传大小上限，请改用平台上传", en: "File exceeds the cloud direct-upload size limit; please use platform upload" },
    "upload.cos.err.format_unsupported": { zh: "该格式暂不支持云端直传，请使用平台上传", en: "Format not supported for cloud direct upload; please use platform upload" },
    "upload.cos.err.waiting_limit": { zh: "已有等待中的云端直传任务", en: "Another cloud upload is already waiting" },
    "upload.cos.err.state": { zh: "任务状态冲突，请刷新页面后重试", en: "Job state conflict; please refresh and retry" },
    "upload.cos.err.rate": { zh: "签名请求过于频繁，请稍后重试", en: "Signing rate limited; please retry later" },
    "upload.cos.err.reconcile": { zh: "云端容量对账中，暂不可继续，请稍后重试", en: "Cloud capacity reconciliation in progress; retry later" },
    "upload.cos.err.unavailable": { zh: "云端直传暂不可用，请使用平台上传", en: "Cloud direct upload unavailable; please use platform upload" },
    // 升级 C（§6.1）：矩形工具文案（i18n.js 为主源；此处兜底）
    "roi.rect.tip": { zh: "矩形工具：在视野中拖出矩形，或输入宽高后点击中心放置；拖内部平移、边/角调整大小；Escape 取消",
                      en: "Rectangle tool: drag in the view, or enter width/height then click to place; drag inside to move, edges/corners to resize; Escape cancels" },
    "roi.cancelled": { zh: "已取消未保存的选区", en: "Unsaved selection cancelled" },
    "roi.input.invalid": { zh: "矩形尺寸非法或超出图像范围，已保留上次的合法框",
                           en: "Invalid rectangle size or out of image bounds; kept the last valid box" },
    // Wave 3（普通图片兼容）：无物理标尺语义（i18n.js 为主源；此处兜底，未改 i18n.js）
    "roi.no.scale.hint": { zh: "该图片无物理标尺，仅支持像素（px）测量",
                           en: "This image has no physical scale; only pixel (px) measurements are available" },
    "slide.meta.no.scale": { zh: "无物理标尺", en: "no physical scale" },
    "edit.conflict": { zh: "该标注已被他人修改（当前 revision {rev}），已显示当前版本；请基于最新版本重新编辑",
                       en: "This annotation was modified by someone else (current revision {rev}); showing the current version — please re-edit on top of it" },
  };
  function tt(key, vars) {
    try {
      var s = window.HP_I18N && window.HP_I18N.t(key, vars);
      if (s && s !== key) return s;
    } catch (e) {}
    var lang = (window.HP_I18N && window.HP_I18N.getLang()) || "zh";
    var e = _EXTRA_I18N[key];
    var raw = (e && (e[lang] || e.zh)) || key;
    // COS 直传引入：兜底文案同样支持 {n}/{name} 形参（照 i18n.js fmt 语义）；
    // 不传 vars 的既有调用方不受影响
    if (vars) {
      raw = String(raw).replace(/\{(\w+)\}/g, function (_, k) {
        return Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : ("{" + k + "}");
      });
    }
    return raw;
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
    // 上次已发 slide.opened 的 "name|revision" 键：去重依据（见 emitSlideOpened）
    lastSlideOpenedKey: null,
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
        // 尝试读 body 判断是否 auth_required（不消费主响应流：克隆一份）。
        // D2（2026-09-10）起 401 body 为 {error: 中文, code: "auth_required"}，
        // 机器码在 code 字段；兼容读取旧形态 error 字段。
        return resp.clone().json().then(
          function (body) {
            if (body && (body.code === "auth_required" ||
                         body.error === "auth_required")) {
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
    // 更换邮箱入口与改密/登出同级：预览态隐藏（未登录时 auth_enabled=false
    // 提前返回，入口保持模板里的 hidden，不会出现）
    if (els.changeemailBtn) { els.changeemailBtn.hidden = !!previewState; }
    // 数据共享入口与改密/改绑同级：预览态隐藏（服务端 actor 解析同样拒绝
    // 预览态变更用户授权）；未登录时 auth_enabled=false 提前返回保持 hidden
    if (els.datashareBtn) { els.datashareBtn.hidden = !!previewState; }
    // 管理台入口按真实 actor 判定（预览态隐藏——与改密/登出同级约定；
    // 预览中 /admin 仍可手动直达，宿主每条消息回查真实 owner）。
    if (els.adminEntryLink) {
      els.adminEntryLink.hidden = !!previewState || actorRole !== "owner";
    }
    // 账户 chip（§3.5）：视觉文字 = 规范化邮箱 @ 前的 local-part（非邮箱存量
    // 账号回退完整 login_id）；权威身份仍是完整邮箱，popover 展示完整邮箱。
    // 未登录（username 空）不显示；预览态显示被预览 subject 并在 popover
    // 顶部标「管理员预览」，真实 actor 的账户设置入口照旧隐藏。
    if (els.acctBtn) {
      var chipName = acctDisplayUsername(info.username);
      if (els.acctBtnName) els.acctBtnName.textContent = chipName;
      els.acctBtn.hidden = !chipName;
      if (els.acctPopPreview) els.acctPopPreview.hidden = !previewState;
      if (els.acctPopEmail) els.acctPopEmail.textContent = info.username || "";
      if (els.acctPopRole) {
        els.acctPopRole.textContent =
          t(currentRole === "owner" ? "acct.role.owner" : "acct.role.user");
      }
      if (els.acctSettingsBtn) {
        els.acctSettingsBtn.hidden = !!previewState || !info.username;
      }
    }
    if (window.HP_I18N && window.HP_I18N.setRole) { window.HP_I18N.setRole(currentRole); }
    if (els.sharePermHint) {
      els.sharePermHint.hidden = currentRole !== "user";
    }
    applyPreviewBanner(info);
    // P3：预览态不是用户本人操作——停研究采集（服务端写闸亦 403 兜底）
    if (previewState && researchTelemetry) researchTelemetry.stop();
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
    // P3：登出即停研究采集、清内存缓冲（旧数据不贴到新授权，§7.3）
    if (researchTelemetry) researchTelemetry.stop();
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
    // 更换邮箱提交（tests/js/account-email-change.test.ts 行为契约挂载点，
    // 与 apiFetch/applyAuthInfo 同级；init 在 bindEvents 里照常接线）
    changeemailSubmit: changeemailSubmit,
    // 账户 chip / 余额展示（升级 Review 2026-09-09 §3.5）纯函数挂载点
    // （tests/js/toolbar-account-upgrade.test.ts 锁 local-part 回退与
    // nano-CNY 精确换算锚点）
    acctDisplayUsername: acctDisplayUsername,
    acctCny: acctCny,
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

  // ---------- 更换邮箱（P1-3 身份收口；review-2026-09-08 P2-2 前端闭环） ----------
  // 与改密同一账户设置区、同一弹窗骨架。POST /api/account/email/change/start
  // （JSON + apiFetch 统一 CSRF 头）；成功响应只含掩码新邮箱——明文 token 只经
  // 确认邮件链接送达，绝不回传。用户在 /verify-email-change 确认后服务端才改
  // 绑并 auth_version+1 全端下线（重新登录由确认页文案负责，本页不处理）。
  function changeemailShowError(msg) {
    if (!els.changeemailError) return;
    els.changeemailError.textContent = msg || "";
    els.changeemailError.hidden = !msg;
  }

  function changeemailOpen() {
    if (!els.changeemailMask) return;
    changeemailShowError("");
    els.changeemailNew.value = "";
    els.changeemailMask.style.display = "";
    if (els.changeemailNew.focus) { setTimeout(function () { els.changeemailNew.focus(); }, 30); }
  }

  function changeemailClose() {
    if (!els.changeemailMask) return;
    els.changeemailMask.style.display = "none";
  }

  function changeemailSubmit() {
    var ne = (els.changeemailNew.value || "").trim();
    if (!ne) {
      changeemailShowError(t("acct.changeemail.err.required"));
      return;
    }
    var btn = els.changeemailSubmitBtn;
    // 防重复：在途（按钮禁用）时直接忽略后续触发（DOM 禁用之外的 JS 层兜底）
    if (btn && btn.disabled) { return; }
    if (btn) { btn.disabled = true; }
    apiFetch("/api/account/email/change/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_email: ne }),
    }).then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      // 到达终态（成功/失败）一律恢复按钮；成功分支随后关弹窗
      if (btn) { btn.disabled = false; }
      if (res.status === 200) {
        // 成功：确认邮件已入队（掩码邮箱回显；登录态此刻仍有效，不跳转）
        changeemailClose();
        toast(t("acct.changeemail.ok", { email: (res.body && res.body.email_masked) || "" }), "info");
        return;
      }
      // 按服务端机器码映射可读文案（与上传修复 U1 同口径：机器码不原样透出；
      // 未知码/缺码走本地化兜底，不透出服务端原始报文）
      var code = res.body && res.body.code;
      if (code === "email_taken") {
        changeemailShowError(t("acct.changeemail.err.taken"));
      } else if (code === "rate_limited") {
        changeemailShowError(t("acct.changeemail.err.locked"));
      } else if (code === "email_channel_unavailable") {
        changeemailShowError(t("acct.changeemail.err.channel"));
      } else if (code === "invalid_request") {
        changeemailShowError(t("acct.changeemail.err.invalid"));
      } else {
        changeemailShowError(t("acct.changeemail.err.generic"));
      }
    }).catch(function () {
      if (btn) { btn.disabled = false; }
      changeemailShowError(t("acct.changeemail.err.generic"));
    });
  }

  function initChangeEmail() {
    if (!els.changeemailMask) return;
    els.changeemailBtn.addEventListener("click", changeemailOpen);
    els.changeemailClose.addEventListener("click", changeemailClose);
    els.changeemailCancel.addEventListener("click", changeemailClose);
    els.changeemailMask.addEventListener("click", function (e) {
      if (e.target === els.changeemailMask) { changeemailClose(); }
    });
    els.changeemailSubmitBtn.addEventListener("click", changeemailSubmit);
    els.changeemailNew.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { changeemailSubmit(); }
    });
  }

  // ---------- 数据共享（P2 账户设置 §3.5：自愿研究授权） ----------
  // 服务端是唯一权威：GET /api/account/agreements（no-store）读当前状态，
  // PUT /api/account/research-consent 带 expected_epoch CAS 提交（409
  // epoch_conflict = 另一标签页先改过，自动重载后由用户重试，不盲覆盖），
  // POST /api/account/research-data/deletion 幂等申请删除研究副本（不删
  // 业务切片/标注/会话）。勾选默认不预选；协议链接只读不改选中状态。
  var datashareState = null;   // 最近一次 GET 的 research 视图（含 epoch）

  function datashareShowError(msg) {
    if (!els.datashareError) return;
    els.datashareError.textContent = msg || "";
    els.datashareError.hidden = !msg;
  }

  function datashareFmtTime(v) {
    if (!v) return "—";
    try {
      var d = new Date(v);
      if (isNaN(d.getTime())) return v;
      return d.toLocaleString();
    } catch (e) { return v; }
  }

  function datashareRender(body) {
    var research = (body && body.research) || {};
    datashareState = research;
    var docs = (body && body.documents) || [];
    var rsDoc = null;
    for (var di = 0; di < docs.length; di++) {
      if (docs[di].document_type === "research_sharing") { rsDoc = docs[di]; break; }
    }
    // grant 版本只取服务端下发的**当前 published** 文稿（draft 服务端必拒绝）
    datashareState._rs_doc_version =
      (rsDoc && rsDoc.status === "published" && rsDoc.version) || null;
    var statusKey;
    if (research.state === "granted") statusKey = "acct.datashare.state.granted";
    else if (research.state === "withdrawn") statusKey = "acct.datashare.state.withdrawn";
    else if (research.state === "declined") statusKey = "acct.datashare.state.declined";
    else statusKey = "acct.datashare.state.none";
    if (els.datashareStatus) {
      els.datashareStatus.textContent = t(statusKey) +
        (body.collection_enabled ? "" : "　·　" + t("acct.datashare.collect.off"));
    }
    if (els.datashareDoc) {
      var parts = [];
      if (rsDoc) {
        parts.push(t("acct.datashare.doc", { version: rsDoc.version || "—" }) +
          "（" + (rsDoc.status === "published" ? t("acct.datashare.doc.published")
                                             : t("acct.datashare.doc.draft")) + "）");
      }
      if (research.state === "granted") {
        parts.push(t("acct.datashare.grantedat", { time: datashareFmtTime(research.granted_at) }));
      } else if (research.state === "withdrawn") {
        parts.push(t("acct.datashare.withdrawnat", { time: datashareFmtTime(research.withdrawn_at) }));
      }
      els.datashareDoc.textContent = parts.join("　·　");
    }
    if (els.datashareScope) {
      els.datashareScope.textContent = t("acct.datashare.scope");
    }
    var legacy = research.legacy_test_application;
    if (els.datashareLegacy) {
      if (legacy && legacy.historical_only) {
        els.datashareLegacy.textContent = t("acct.datashare.legacy",
          { value: legacy.share_research_data ? t("acct.datashare.legacy.true")
                                               : t("acct.datashare.legacy.false") });
        els.datashareLegacy.hidden = false;
      } else {
        els.datashareLegacy.hidden = true;
      }
    }
    var job = research.active_deletion_job;
    if (els.datashareJob) {
      if (job) {
        els.datashareJob.textContent = t("acct.datashare.job", {
          status: job.status || "pending",
          time: datashareFmtTime(job.created_at),
        });
        els.datashareJob.hidden = false;
      } else {
        els.datashareJob.hidden = true;
      }
    }
    if (els.datashareCheck) {
      // 渲染当前选择（服务端权威），但保持「不预选自愿项」：仅 granted 时勾选
      els.datashareCheck.checked = research.state === "granted";
    }
    if (els.datashareWithdraw) {
      els.datashareWithdraw.hidden = research.state !== "granted";
    }
    if (els.datashareSubmit) {
      // 草稿文稿未发布时不能同意（服务端也会拒绝），提交按钮仅控制勾选变化
      els.datashareSubmit.disabled = false;
    }
  }

  function datashareLoad() {
    if (els.datashareStatus) {
      els.datashareStatus.textContent = t("acct.datashare.loading");
    }
    apiFetch("/api/account/agreements").then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      if (res.status !== 200) {
        datashareShowError((res.body && res.body.error) || t("acct.datashare.err.load"));
        return;
      }
      datashareShowError("");
      datashareRender(res.body);
    }).catch(function () {
      datashareShowError(t("acct.datashare.err.load"));
    });
  }

  function datashareOpen() {
    if (!els.datashareMask) return;
    datashareShowError("");
    datashareState = null;
    els.datashareMask.style.display = "";
    datashareLoad();
  }

  function datashareClose() {
    if (!els.datashareMask) return;
    els.datashareMask.style.display = "none";
  }

  // 统一 PUT：409 epoch_conflict（多标签页/旧页面）→ 自动重载状态并提示，
  // 绝不拿旧 epoch 盲目重试覆盖他人操作
  function datasharePut(enabled, btn) {
    var payload = { enabled: enabled, expected_epoch: datashareState ? datashareState.epoch : 0 };
    if (enabled) {
      // 版本必填：从服务端下发的当前 published 文稿取（draft 服务端必拒绝）
      var ver = datashareState && datashareState._rs_doc_version;
      payload.document_version = ver || null;
      if (!payload.document_version) {
        datashareShowError(t("acct.datashare.err.nodoc"));
        datashareLoad();
        return;
      }
    }
    if (btn && btn.disabled) { return; }
    if (btn) { btn.disabled = true; }
    apiFetch("/api/account/research-consent", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      if (btn) { btn.disabled = false; }
      if (res.status === 409 && res.body && res.body.code === "epoch_conflict") {
        datashareShowError(t("acct.datashare.err.epoch"));
        datashareLoad();
        return;
      }
      if (res.status !== 200) {
        datashareShowError((res.body && res.body.error) || t("acct.datashare.err.save"));
        return;
      }
      datashareShowError("");
      if (enabled) { toast(t("acct.datashare.ok.granted"), "info"); }
      else { toast(t("acct.datashare.ok.withdrawn"), "info"); }
      datashareLoad();
    }).catch(function () {
      if (btn) { btn.disabled = false; }
      datashareShowError(t("acct.datashare.err.save"));
    });
  }

  function datashareSubmit() {
    var want = !!(els.datashareCheck && els.datashareCheck.checked);
    var was = !!(datashareState && datashareState.state === "granted");
    if (want === was) { datashareClose(); return; }
    if (want) {
      var ver = datashareState && datashareState._rs_doc_version;
      if (!ver) {
        datashareShowError(t("acct.datashare.err.nodoc"));
        datashareLoad();
        return;
      }
    }
    datasharePut(want, els.datashareSubmit);
  }

  function datashareWithdraw() {
    datasharePut(false, els.datashareWithdraw);
  }

  function datashareDeleteRequest() {
    var btn = els.datashareDelete;
    if (btn && btn.disabled) { return; }
    if (btn) { btn.disabled = true; }
    apiFetch("/api/account/research-data/deletion", { method: "POST" })
      .then(function (r) {
        return r.json().then(function (b) { return { status: r.status, body: b }; });
      }).then(function (res) {
        if (btn) { btn.disabled = false; }
        if (res.status !== 200) {
          datashareShowError((res.body && res.body.error) || t("acct.datashare.err.del"));
          return;
        }
        datashareShowError("");
        toast(t(res.body && res.body.created
          ? "acct.datashare.ok.del" : "acct.datashare.ok.del.dupe"), "info");
        datashareLoad();
      }).catch(function () {
        if (btn) { btn.disabled = false; }
        datashareShowError(t("acct.datashare.err.del"));
      });
  }

  function initDataShare() {
    if (!els.datashareMask) return;
    els.datashareBtn.addEventListener("click", datashareOpen);
    els.datashareClose.addEventListener("click", datashareClose);
    els.datashareCancel.addEventListener("click", datashareClose);
    els.datashareMask.addEventListener("click", function (e) {
      if (e.target === els.datashareMask) { datashareClose(); }
    });
    els.datashareSubmit.addEventListener("click", datashareSubmit);
    els.datashareWithdraw.addEventListener("click", datashareWithdraw);
    if (els.datashareDelete) {
      els.datashareDelete.addEventListener("click", datashareDeleteRequest);
    }
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
    zoomIn: $("zoom-in"),
    zoomOut: $("zoom-out"),
    rotateBtn: $("rotate-btn"),
    flipBtn: $("flip-btn"),
    // 升级 C：单一矩形入口 + 紧凑设置区（旧 roi-6/roi-6-5/roi-box-btn 已移除）
    roiRectBtn: $("roi-rect-btn"),
    roiSettings: $("roi-settings"),
    roiSummary: $("roi-summary"),
    roiWInput: $("roi-w-input"),
    roiHInput: $("roi-h-input"),
    roiUnitSelect: $("roi-unit-select"),
    roiLockRatio: $("roi-lock-ratio"),
    roiPresetSelect: $("roi-preset-select"),
    // Wave 3（普通图片兼容）：无物理标尺提示（模板内联元素；demo 模板无此块时为 null）
    roiNoScaleHint: $("roi-no-scale-hint"),
    saveBtn: $("save-btn"),
    saveAnnoBtn: $("save-anno-btn"),
    annoBtn: $("anno-btn"),
    annoAllBtn: $("anno-all-btn"),
    annoArrowBtn: $("anno-arrow-btn"),
    annoFreeBtn: $("anno-free-btn"),
    annoLabelInput: $("anno-label-input"),
    annoMoreBtn: $("anno-more-btn"),
    annoPop: $("anno-pop"),
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
    // W3/W4/W6（2026-09-14）：统一「导入切片」抽屉 + 新建项目对话框
    importSlidesBtn: $("import-slides-btn"),
    importDrawer: $("import-drawer"),
    importDrawerMask: $("import-drawer-mask"),
    importDrawerClose: $("import-drawer-close"),
    importTabLocal: $("import-tab-local"),
    importTabBaidu: $("import-tab-baidu"),
    importPanelLocal: $("import-panel-local"),
    importPanelBaidu: $("import-panel-baidu"),
    importPickFiles: $("import-pick-files"),
    importTargetSelect: $("import-target-select"),
    importTargetNewName: $("import-target-new-name"),
    importFormatCatalog: $("import-format-catalog"),
    importTaskList: $("import-task-list"),
    frSampleMax: $("fr-sample-max"),
    frResult: $("fr-result"),
    frRecent: $("fr-recent"),
    baiduCapStatus: $("baidu-cap-status"),
    baiduInputBlock: $("baidu-input-block"),
    baiduShareText: $("baidu-share-text"),
    baiduExtractCode: $("baidu-extract-code"),
    baiduListBtn: $("baidu-list-btn"),
    baiduEnumStatus: $("baidu-enum-status"),
    baiduCandidatesBlock: $("baidu-candidates-block"),
    baiduSearch: $("baidu-search"),
    baiduCandidateList: $("baidu-candidate-list"),
    baiduPrevBtn: $("baidu-prev-btn"),
    baiduNextBtn: $("baidu-next-btn"),
    baiduPageInfo: $("baidu-page-info"),
    baiduSelectionSummary: $("baidu-selection-summary"),
    baiduTargetSelect: $("baidu-target-select"),
    baiduImportBtn: $("baidu-import-btn"),
    baiduImportStatus: $("baidu-import-status"),
    baiduImportItems: $("baidu-import-items"),
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
    // 更换邮箱（P1-3 身份收口 review-2026-09-08 P2-2；弹窗骨架复用 changepw）
    changeemailBtn: $("changeemail-btn"),
    changeemailMask: $("changeemail-mask"),
    changeemailClose: $("changeemail-close"),
    changeemailCancel: $("changeemail-cancel"),
    changeemailSubmitBtn: $("changeemail-submit"),
    changeemailNew: $("changeemail-new"),
    changeemailError: $("changeemail-error"),
    // 数据共享（P2 账户设置 §3.5：自愿研究授权；弹窗骨架复用 changepw）
    datashareBtn: $("datashare-btn"),
    datashareMask: $("datashare-mask"),
    datashareClose: $("datashare-close"),
    datashareCancel: $("datashare-cancel"),
    datashareSubmit: $("datashare-submit"),
    datashareWithdraw: $("datashare-withdraw"),
    datashareDelete: $("datashare-delete"),
    datashareCheck: $("datashare-check"),
    datashareStatus: $("datashare-status"),
    datashareDoc: $("datashare-doc"),
    datashareScope: $("datashare-scope"),
    datashareLegacy: $("datashare-legacy"),
    datashareJob: $("datashare-job"),
    datashareError: $("datashare-error"),
    annoAllToggle: $("anno-all-toggle"),
    // 手机端侧栏抽屉
    menuBtn: $("menu-btn"),
    sidebar: $("sidebar"),
    sidebarMask: $("sidebar-mask"),
    // 升级 A（2026-09-22 重做）：搜索输入框按需创建——首屏只有按钮 + 空容器
    slideSearchBtn: $("slide-search-btn"),
    slideSearchArea: $("slide-search-area"),
    viewerEmpty: $("viewer-empty"),
    viewerEmptyPick: $("viewer-empty-pick"),
    // 项目
    // 其他格式请求兼容
    formatReqBtn: $("format-req-btn"),
    formatReqForm: $("format-req-form"),
    frExt: $("fr-ext"),
    frMessage: $("fr-message"),
    frContact: $("fr-contact"),
    frSample: $("fr-sample"),
    frSubmit: $("fr-submit"),
    frCancel: $("fr-cancel"),
    newProjectBtn: $("new-project-btn"),
    // W3：新建项目对话框（替换内联表单；R3 empty/selection 草稿 + R4 提交锁）
    projectCreateMask: $("project-create-mask"),
    projectCreateDialog: $("project-create-dialog"),
    pcdClose: $("pcd-close"),
    pcdName: $("pcd-name"),
    pcdNote: $("pcd-note"),
    pcdSlidesSummary: $("pcd-slides-summary"),
    pcdSlidesList: $("pcd-slides-list"),
    pcdError: $("pcd-error"),
    pcdCancel: $("pcd-cancel"),
    pcdConfirm: $("pcd-confirm"),
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
    // viewer 画质档（image-transport-upgrade；能力存在才显示）
    qualityControl: $("quality-control"),
    // 账户 chip + popover（升级 Review 2026-09-09 §3.5；Demo 壳不渲染，全部可空）
    acctBtn: $("acct-btn"),
    acctBtnName: $("acct-btn-name"),
    acctPop: $("acct-pop"),
    acctPopPreview: $("acct-pop-preview"),
    acctPopEmail: $("acct-pop-email"),
    acctPopRole: $("acct-pop-role"),
    acctPopScope: $("acct-pop-scope"),
    acctPopRemaining: $("acct-pop-remaining"),
    acctPopDetail: $("acct-pop-detail"),
    acctSettingsBtn: $("acct-settings-btn"),
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
  // 工单 B：人数不再按 label 组数计（一个作者多个 label 组会被重复计人）。
  // 按条目收集**唯一作者**：
  //   - 优先条目上的 author_key/author_kind（标注 ACL 批次注入）；
  //     author_kind === "unknown" / 缺 key 不计（不虚构人）；
  //   - 旧数据回落 owner_user_id，再回落 visitor；
  //   - source === "ai" 的条目计标记不计人。
  function annoAuthorKey(item) {
    if (!item || item.source === "ai") return null;
    if (item.author_key != null && item.author_key !== "") {
      if (!item.author_kind || item.author_kind === "unknown") return null;
      return item.author_kind + ":" + item.author_key;
    }
    if (item.owner_user_id != null && item.owner_user_id !== "") {
      return "user:" + item.owner_user_id;
    }
    if (item.visitor) return "visitor:" + item.visitor;
    return null;
  }

  function annoBadgeText(slideName) {
    if (!allAnnotationsBySlide) return null;
    var grps = allAnnotationsBySlide[slideName];
    if (!grps || grps.length === 0) return null;
    var total = 0;
    var seen = {};
    grps.forEach(function (g) {
      total += g.count || 0;
      (g.items || []).forEach(function (it) {
        var key = annoAuthorKey(it);
        if (key) seen[key] = true;
      });
    });
    var people = Object.keys(seen).length;
    if (people > 0) return t("badge.marks", { n: total, m: people });
    // 无可识别作者（全部匿名/AI）：只显示标记数，不虚构 0 人
    return t("badge.marks.only", { n: total });
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

  // =========================================================================
  // P3 人工读片行为采集装配（docs/agent-plan-20260921-registration-consent-
  // research.md §7.1）：只由本正式工作台显式装配——demo/公开分享/预览不加载
  // research-viewer-telemetry.js 也不初始化；服务端 capabilities.
  // research_collection（全局开关，默认关闭）关闭时零装配零网络请求。采集
  // 模块自持手势归并/observe_pause/批次发送逻辑，本文件只提供装配点与
  // 「业务写入成功后」的标注回调（失败不产事件）。
  // =========================================================================
  var researchTelemetry = null;

  function initResearchTelemetry() {
    if (!window.HP_ResearchTelemetry || !window.HP_ResearchTelemetry.create) return;
    var caps = (window.HP_APP_BOOTSTRAP && window.HP_APP_BOOTSTRAP.capabilities) || {};
    if (!caps.research_collection) return; // 全量关闭：不装配（无网络请求）
    researchTelemetry = window.HP_ResearchTelemetry.create({});
    researchTelemetry.attach(viewer);
  }

  // 切片打开（emitSlideOpened 同一去重口径）：为本切片建研究会话
  function syncResearchTelemetrySlide() {
    if (!researchTelemetry || !state.slide) return;
    if (previewState) return; // 预览态不是用户本人操作（服务端亦 403 兜底）
    researchTelemetry.startSlide({
      slide: state.slide.name,
      width: state.slide.width,
      height: state.slide.height,
    });
  }

  function updateResearchBusy() {
    if (!researchTelemetry) return;
    researchTelemetry.setBusy("drawing",
      !!(state.drawMode || drawPhase !== "idle"));
    researchTelemetry.setBusy("roi", !!state.roiMode);
    researchTelemetry.setBusy("panel", !!(annoPanelOpen || editing));
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
    // 切片关闭时清理旧底图；P3：同时结束研究读片会话、清旧缓冲
    viewer.addHandler("close", function () {
      clearBaseThumb();
      if (researchTelemetry) researchTelemetry.endSlide();
    });
  }

  // slide.opened 发射（name|revision 去重）。语义是「插件应按此切片重置/恢复
  // 状态」：键相同（同切片同 revision，如换配色重开）跳过——不重置插件 AI
  // 会话；键变化（切换切片，或同名文件替换 → revision 变化）必然重发。
  // 正常路径与 channelReopening 轻量路径都必须走这里：多通道切片带本地持久化
  // 配色时，「首开默认 token → applySelection 同步 viewer.close() + 置位重开」
  // 时序下第一次 open 事件被 close 吃掉、第二次 open 只会从轻量路径到达——
  // 轻量路径若不发，插件停留旧切片（生产 bug：slide.opened 3/3 丢失，会话
  // 恢复/切片替换保护全部失效）；发了则由键去重保证纯换配色不重发、且两次
  // open 事件无论谁先到达都恰好送达一次。
  function emitSlideOpened() {
    if (!state.slide || !state.slide.name) return;
    var key = state.slide.name + "|" + (state.slide.revision || "");
    if (key === state.lastSlideOpenedKey) return;
    state.lastSlideOpenedKey = key;
    hpEmit("slide.opened", { slide: {
      name: state.slide.name, width: state.slide.width, height: state.slide.height,
      mppX: state.slide.mppX, mppY: state.slide.mppY,
      // 资产 revision（服务端口径 = "mtime_ns:size"）：插件快照回看时比对
      // view.slide_revision，检测切片被替换（宽容缺省 → null，保护退化为不拦截）
      revision: state.slide.revision || null,
    } });
    // P3：为本切片建研究读片会话（采集开关关闭/预览态时为无操作）
    syncResearchTelemetrySlide();
  }

  function onViewerOpen() {
    // AI 助手：切片一旦可用即启用触发按钮（插件停用时 aiBtn 不渲染，跳过；
    // 平台人工读片不受影响）。必须放在 channelReopening 分支之前：模板初始
    // disabled，而多通道切片带本地配色时首开被 close 吃掉、最终 open 只走
    // 轻量路径——若在普通分支才解除禁用，首开该切片时 AI 入口保持灰色。
    if (els.aiBtn) els.aiBtn.disabled = false;
    if (els.tbbMoreAi) els.tbbMoreAi.disabled = false;  // ⋯ 面板里的 AI 钮同步
    // 通道配色重开（同一切片换 TileSource，§8.2）：走轻量路径——只同步倍率
    // 徽章与底图缩略图；不退绘制模式、不重置标注/AI 面板。slide.opened 仍经
    // emitSlideOpened 补发（键相同自动去重，见上），不再无条件吞掉。
    if (state.channelReopening) {
      state.channelReopening = false;
      updateZoomBadge();
      syncBaseThumb();
      emitSlideOpened();
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
    syncAnnoAllBtns();
    state.showAnno = false;
    state.focusAnno = null;
    // 平台侧清叠加层（切片隔离）。插件侧会话 UI/SSE/游标由 slide.opened event 触发其自行 reset+restore。
    aiOverlay = [];
    redrawAnnoCanvas();
    emitSlideOpened();
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

  // ---------- 文档标题（§3.4：品牌区 Beta 徽标后切片名只进 document.title） ----------
  function updateDocTitle(slideName) {
    var base = t("app.doc.title");
    try {
      document.title = slideName ? (slideName + " · " + base) : base;
    } catch (e) { /* 非 DOM 环境忽略 */ }
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
          // info.asset_revision（"mtime_ns:size"）随 info 响应下发；边缘路径
          // （render fields 读取失败等）可能缺键 → 宽容置 null
          revision: info.asset_revision || null,
        };
        state.mppX = info.mpp_x;
        state.rotation = 0;
        // 升级 A：切片已打开，隐藏无切片空态入口
        updateViewerEmptyState();
        // §3.4：常驻 #current-slide 已下架——切片名进 document.title
        //（"切片名 · PathTogether Beta"）；侧栏选中行/切片信息菜单/面板标题仍在。
        updateDocTitle(info.alias || info.name);
        updateMppSetterVisibility();
        exitRoi();
        // Wave 3（普通图片兼容）：缺物理标尺（mpp_source="missing"）时单位区
        // 直接落到 px 并禁用 mm/µm 预设——首次打开即可进行像素操作，不要求
        // 先填写虚假标尺；显式 setMpp 后由 syncUnitAvailability 重新放开。
        syncUnitAvailability();
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
        } else if (plan.thumbnailUrl && baseThumbEl && !baseThumbEl.src) {
          // render 计划首开（多通道 / RGB 画质路径）：底图预览由计划提供
          // （缩略图与瓦片同 context；仅当未设置过 src 时补设）
          baseThumbEl.src = plan.thumbnailUrl;
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

  // ---------- 无物理标尺（普通图片 mpp_source="missing"）单位区可用性 ----------
  // Wave 3 产品语义（§4.4）：没有可信 MPP 时保留像素尺寸/像素标注/缩放百分比，
  // 物理单位（mm/µm）操作禁用并明确提示，而不是要求先填一个虚假标尺。
  // 显式手动校准（setMpp，前端态）后 mppX/mppY 有效，物理单位重新可用。
  function slideHasPhysicalScale() {
    return posNum(Number(state.mppX)) && posNum(Number(state.slide && state.slide.mppY));
  }
  function syncUnitAvailability() {
    var hasPhys = !!state.slide && slideHasPhysicalScale();
    if (els.roiUnitSelect) {
      var opts = els.roiUnitSelect.options || [];
      Array.prototype.forEach.call(opts, function (opt) {
        if (opt && opt.value !== "px") opt.disabled = !hasPhys;
      });
      if (!hasPhys && state.roiUnit !== "px") {
        // 普通图片首次打开：单位默认直接落到像素，不停在 mm 上报错
        state.roiUnit = "px";
        els.roiUnitSelect.value = "px";
      }
    }
    if (els.roiPresetSelect) {
      // mm 预设（6/6.5）属于物理单位操作，一并禁用
      els.roiPresetSelect.disabled = !hasPhys;
    }
    if (els.roiNoScaleHint) {
      els.roiNoScaleHint.hidden = hasPhys || !state.slide;
      if (!els.roiNoScaleHint.hidden) {
        els.roiNoScaleHint.textContent = tt("roi.no.scale.hint");
      }
    }
  }

  // ---------- 缩放 / 旋转 / 复位 ----------
  // P3 研究采集：缩放按钮/快捷键是人工输入（§7.1），进入采集模块的
  // gesture context；采集模块由 initResearchTelemetry 在采集开关开启时装配，
  // 未装配时这里是无操作。
  function notifyResearchZoomTool() {
    if (researchTelemetry) researchTelemetry.notifyToolInteraction({ inputKind: "button" });
  }
  function zoomIn() {
    if (!viewer || !viewer.viewport) return;
    notifyResearchZoomTool();
    viewer.viewport.zoomBy(1.4);
    viewer.viewport.applyConstraints();
  }
  function zoomOut() {
    if (!viewer || !viewer.viewport) return;
    notifyResearchZoomTool();
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
  // 创建只受单边上限 40000px + 切片边界约束（出界保持上个合法框）；
  // w*h 面积预算是导出/裁剪闸（saveCrop 交服务端 crop_guard，超限 413 明确
  // 报错），与「矩形跨度」不是同一件事，创建不设面积闸（大 ROI 是旧主流工作流）。

  // rect 单边像素上限（与后端 RECT_MAX_SIDE_PX 一致）
  var RECT_MAX_SIDE_PX = 40000;

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
  // 不做 w*h 面积闸：创建/编辑必须保留用户真实几何，不得静默缩放或截停；
  // 超预算只在导出/裁剪时由服务端 crop_guard 明确报错。
  function normalizeRect(x0, y0, x1, y1) {
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
    if (els.roiSettings) {
      els.roiSettings.hidden = false;
      // §3.3：尺寸区改为按钮下方锚定 popover（fixed 定位，JS 计算）
      positionToolbarPop(els.roiRectBtn, els.roiSettings);
    }
    els.roiRectBtn.setAttribute("aria-expanded", "true");
    if (viewer) viewer.setMouseNavEnabled(false);
    updateRoiButtons();
    updateCtxBar();
    toast(t("roi.rect.tip"), "info");
    updateResearchBusy();  // P3：矩形工具激活取消稳定观察检测
  }

  function exitRoi() {
    state.roiMode = null;
    // 工单 D：退出工具 = 放弃未保存矩形草稿（含失败重试态）
    if (retryDraft && retryDraft.kind === "rect") retryDraft = null;
    setDrawUnsaved(false);
    setDrawPhase("idle");
    updateResearchBusy();  // P3
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
    if (els.roiSettings) els.roiSettings.hidden = true;
    if (els.roiSummary) els.roiSummary.hidden = true;
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
    updateRoiSummary();
  }

  // 主行简短摘要（§3.3）：有选区时显示如「6×6 mm」，无选区隐藏。
  // 与 rectPhysicalText 同口径（分轴反算、当前单位），去掉尾随 0。
  function rectSummaryText() {
    var r = state.roi;
    if (!r || !(r.w > 0) || !(r.h > 0)) return "";
    var unit = state.roiUnit;
    if (unit === "px") return r.w + "×" + r.h + " px";
    var mx = Number(state.mppX), my = Number(state.slide && state.slide.mppY);
    if (!posNum(mx) || !posNum(my)) return r.w + "×" + r.h + " px";
    var wx = r.w * mx, hy = r.h * my; // µm
    function trimNum(n) {
      return String(n).replace(/\.0$/, "").replace(/(\.\d*?)0+$/, "$1");
    }
    if (unit === "mm") {
      return trimNum(+(wx / 1000).toFixed(2)) + "×" + trimNum(+(hy / 1000).toFixed(2)) + " mm";
    }
    return trimNum(+wx.toFixed(1)) + "×" + trimNum(+hy.toFixed(1)) + " μm";
  }

  function updateRoiSummary() {
    if (!els.roiSummary) return;
    var s = rectSummaryText();
    els.roiSummary.textContent = s;
    els.roiSummary.hidden = !s;
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
    // 工单 D：pointercancel = 取消恢复（绝不按「完成」提交当前位移）
    roiBox.addEventListener("pointercancel", onRoiPointerCancel);
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
      if (!n) return; // 非法（NaN）保持上帧
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
    roiBox.removeEventListener("pointercancel", onRoiPointerCancel);
    dragInfo = null;
    viewer.setMouseNavEnabled(true);
  }

  // 工单 D：拖拽被打断（pointercancel / 丢失捕获）→ 恢复拖前几何，不提交位移
  function onRoiPointerCancel(e) {
    if (!dragInfo) return;
    if (e && e.preventDefault) { try { e.preventDefault(); } catch (err) {} }
    try { roiBox.releasePointerCapture(dragInfo.pointerId); } catch (err) {}
    roiBox.removeEventListener("pointermove", onRoiPointerMove);
    roiBox.removeEventListener("pointerup", onRoiPointerUp);
    roiBox.removeEventListener("pointercancel", onRoiPointerCancel);
    var s = dragInfo.startRoi;
    dragInfo = null;
    state.roi.x = s.x; state.roi.y = s.y; state.roi.w = s.w; state.roi.h = s.h;
    if (roiBox && viewer && s.w > 0) {
      try {
        viewer.updateOverlay(
          roiBox,
          viewer.viewport.imageToViewportRectangle(s.x, s.y, s.w, s.h),
          OpenSeadragon.Placement.TOP_LEFT
        );
      } catch (err) {}
    }
    syncRoiSettings();
    viewer.setMouseNavEnabled(true);
  }

  function getViewerRect() { return viewer.container.getBoundingClientRect(); }

  // ---------- 画布层拖出矩形 / 点选中心放置（矩形工具激活时） ----------
  // 工单 D（§5）：自由矩形 = 拖动完成即自动保存（QuPath 习惯）；预设尺寸矩形
  // = 点击中心放置后由 Enter/「保存标记」提交。两种模式用「屏幕像素」位移阈值
  // 区分（CSS px，与缩放倍率无关——旧的图像像素阈值在高倍率下会把轻抖动放大
  // 成有效拖动）。右键（button===2）不启动绘制（留给右键菜单工单 E）。
  var rectDrawInfo = null;
  var RECT_DRAG_SCREEN_PX = 4; // 判定「拖动」的屏幕位移阈值（CSS px）

  function onRectCanvasPointerDown(e) {
    if (!rectToolActive() || !state.slide) return false;
    if (e.button === 2) return false; // 右键不启动绘制
    e.preventDefault(); e.stopPropagation();
    // 新的画布交互 = 放弃失败草稿的重试（按钮/Enter 重试在此之前仍可用），
    // 否则新拖出的几何会被旧 retryDraft 顶替提交。
    if (retryDraft && retryDraft.kind === "rect") {
      retryDraft = null;
      setDrawUnsaved(false);
    }
    var c = els.annoCanvas;
    try { c.setPointerCapture(e.pointerId); } catch (err) {}
    var img0 = screenToImg(e);
    rectDrawInfo = {
      pointerId: e.pointerId,
      x0: img0.x, y0: img0.y, x1: img0.x, y1: img0.y,
      sx0: e.clientX, sy0: e.clientY,   // 屏幕起点（阈值判定用）
      startRoi: { x: state.roi.x, y: state.roi.y, w: state.roi.w, h: state.roi.h },
      moved: false,
    };
    viewer.setMouseNavEnabled(false);
    setDrawPhase("drawing");
    return true;
  }

  function onRectCanvasPointerMove(e) {
    if (!rectDrawInfo) return false;
    e.preventDefault(); e.stopPropagation();
    // 屏幕（CSS）像素阈值：拖动判定与缩放倍率解耦
    if (Math.hypot(e.clientX - rectDrawInfo.sx0, e.clientY - rectDrawInfo.sy0)
        > RECT_DRAG_SCREEN_PX) {
      rectDrawInfo.moved = true;
    }
    var img = screenToImg(e);
    rectDrawInfo.x1 = img.x; rectDrawInfo.y1 = img.y;
    if (rectDrawInfo.moved) {
      var x1 = img.x, y1 = img.y;
      // Shift 约束正方形（QuPath 习惯）：按较大位移轴取边长，方向保留
      if (e.shiftKey) {
        var dx = x1 - rectDrawInfo.x0, dy = y1 - rectDrawInfo.y0;
        var side = Math.max(Math.abs(dx), Math.abs(dy));
        x1 = rectDrawInfo.x0 + (dx < 0 ? -side : side);
        y1 = rectDrawInfo.y0 + (dy < 0 ? -side : side);
      }
      var n = normalizeRect(rectDrawInfo.x0, rectDrawInfo.y0, x1, y1);
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
    setDrawPhase("idle");
    if (!info.moved) {
      // 点击放置（预设尺寸模式）：需要设置区已给出有效宽/高
      // （§6.1「先输入大小，再点击中心放置」）；放置后不自动保存，
      // 由 Enter / 「保存标记」提交。
      var px = rectInputsToPx();
      if (!px) {
        var unit = state.roiUnit;
        if (unit !== "px" && !unitToPxFactors(unit)) { toast(t("roi.need.mpp"), "error"); }
        else { toast(t("roi.input.invalid"), "error"); }
        viewer.setMouseNavEnabled(true);
        return true;
      }
      px = lockRatioAdjust(px.w, px.h);
      if (px.w > state.slide.width || px.h > state.slide.height) {
        toast(t("roi.input.invalid"), "error");
        viewer.setMouseNavEnabled(true);
        return true;
      }
      var img = screenToImg(e);
      placeRectAtCenter(img.x, img.y, px.w, px.h);
      createRoiBox();
      updateRoiOverlay();
      if (state.roi.w > 0) {
        els.saveBtn.disabled = false;
        els.saveAnnoBtn.disabled = false;
      }
      viewer.setMouseNavEnabled(true);
      return true;
    }
    // 自由矩形拖动完成：自动保存（与箭头一致），不再要求二次点击「保存标记」
    if (state.roi.w > 0) { submitCurrentDraft(); }
    viewer.setMouseNavEnabled(true);
    return true;
  }

  // 工单 D：拖动被打断（pointercancel / 丢失捕获）→ 取消并恢复拖前选区，
  // 绝不走完成/保存路径。
  function onRectCanvasPointerCancel(e) {
    if (!rectDrawInfo) return false;
    if (e && e.preventDefault) { try { e.preventDefault(); } catch (err) {} }
    var c = els.annoCanvas;
    try { c.releasePointerCapture(rectDrawInfo.pointerId); } catch (err) {}
    var info = rectDrawInfo;
    rectDrawInfo = null;
    // 恢复拖前选区（工具刚激活时为空选区）
    if (info.startRoi) {
      state.roi.x = info.startRoi.x; state.roi.y = info.startRoi.y;
      state.roi.w = info.startRoi.w; state.roi.h = info.startRoi.h;
    }
    if (!(state.roi.w > 0) && roiBox) {
      try { if (viewer && viewer.currentOverlays) viewer.removeOverlay(roiBox); } catch (err) {}
      if (roiBox.parentNode) roiBox.parentNode.removeChild(roiBox);
      roiBox = null;
    } else {
      updateRoiOverlay();
    }
    viewer.setMouseNavEnabled(true);
    setDrawPhase("idle");
    return true;
  }

  // Escape 取消未保存选区（§6.1）：恢复 viewer 导航；
  // Enter 提交有效未提交草稿（工单 D：预设放置/失败重试）。
  function onRectKeydown(e) {
    if (e.key === "Escape") {
      if (rectDrawInfo) {
        onRectCanvasPointerCancel(e);
        return;
      }
      if (rectToolActive()) {
        e.preventDefault();
        // 有未提交草稿（放置/失败重试）→ 先撤草稿；再次 Escape 才退出工具
        if (discardRectDraft()) {
          toast(t("draw.cancelled"), "info");
          return;
        }
        exitRoi();
        toast(t("roi.cancelled"), "info");
      }
      return;
    }
    if (e.key === "Enter") {
      if (rectDrawInfo) return; // 拖动中不提交
      if (!rectToolActive()) return;
      e.preventDefault();
      submitCurrentDraft();
    }
  }

  // 撤销当前未提交矩形草稿（选区/失败重试态）：清选区、留在工具内。
  // 返回是否真的撤掉了东西。
  function discardRectDraft() {
    var had = !!(retryDraft && retryDraft.kind === "rect") || state.roi.w > 0;
    if (!had) return false;
    retryDraft = null;
    state.roi = { x: 0, y: 0, w: 0, h: 0 };
    if (roiBox) {
      try { if (viewer && viewer.currentOverlays) viewer.removeOverlay(roiBox); } catch (err) {}
      if (roiBox.parentNode) roiBox.parentNode.removeChild(roiBox);
      roiBox = null;
    }
    if (els.saveBtn) els.saveBtn.disabled = true;
    if (els.saveAnnoBtn) els.saveAnnoBtn.disabled = true;
    setDrawUnsaved(false);
    syncRoiSettings();
    updateRoiSummary();
    return true;
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
  // 工单 D：与拖动完成的自动保存共用一条提交路径（幂等键 + 草稿保留 +
  // 成功后选中新标注）。「保存图片」（saveCrop，裁剪导出）保持独立，
  // 不因本函数产生标注。
  function saveAnno() {
    if (!state.slide || !rectToolActive()) return;
    if (!(state.roi.w > 0)) return;
    submitCurrentDraft();
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
    // Wave 3：显式手动校准后（前端态，非持久化），物理单位重新可用
    syncUnitAvailability();
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
      // Wave 3（普通图片兼容）：产品语义说「无物理标尺」，不说「mpp 缺失」
      // （缺 mpp ≠ 数据缺陷；普通图片本就不携带物理标尺）
      parts.push(tt("slide.meta.no.scale"));
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
      // 升级 A：列表重渲后重放当前搜索条件（搜索条件不因收起/重渲丢失；
      // 输入框未创建/已移除时 getSlideQuery 返回空 → 显示全部）
      applySlideFilter(getSlideQuery());
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

    // 第一行：名称独占整行（别名优先，无别名则截断文件名）；第二行：标注
    // pill（标记/作者数）；第三行：meta（工单 B 布局：名称与徽章分行，窄
    // 侧栏/长英文名互不挤压，截断保留可辨认编号）
    var top = document.createElement("div");
    top.className = "slide-top";
    var nameEl = document.createElement("span");
    nameEl.className = "slide-name";
    if (alias) {
      nameEl.classList.add("alias-first");
      nameEl.innerHTML = esc(alias) +
        '<span class="alias-filename">' + esc(truncateMiddle(sname, 20)) + "</span>";
    } else {
      nameEl.textContent = truncateMiddle(sname, 24) + (failed ? t("slide.read.fail.short") : "");
    }
    // 完整名称（含读取失败提示）经 tooltip 与可访问名称提供（截断不丢信息）
    nameEl.title = sname + (failed ? " " + t("slide.read.fail") : "");
    nameEl.setAttribute("aria-label", sname);
    top.appendChild(nameEl);
    mid.appendChild(top);

    // 标注 pill（独立次行，不再与名称同行）
    var badgeText = annoBadgeText(sname);
    if (badgeText) {
      var badges = document.createElement("div");
      badges.className = "slide-badges";
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
      badges.appendChild(badge);
      mid.appendChild(badges);
    }

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
      // 真实空态（没有未归类切片）不是过滤结果：清掉过滤残留并复位计数
      applySlideFilter(getSlideQuery());
      return;
    }
    unfiled.forEach(function (s) {
      els.unfiledList.appendChild(renderSlideRow(s.name, true));
    });
    // 升级 A：列表重渲后重放当前搜索条件（搜索条件不因收起/重渲丢失）
    applySlideFilter(getSlideQuery());
  }

  // ---------- 新建项目（W3：独立对话框；修 R3 草稿残留 / R4 重复提交） ----------
  // R3：旧实现把「显式空数组」回退到 pendingNewProjectSlides（未归类勾选的
  // 隐式全局），取消含选中后普通新建仍夹带旧选择。现在 empty / selection
  // 两种模式显式分离：selection 打开时**复制**当前勾选快照进草稿，取消 /
  // Esc / 重新以 empty 打开一律清空草稿；空数组必须表示空项目，绝无 fallback。
  // R4：inFlight 提交锁由确认按钮与 Enter 共用；Idempotency-Key 按草稿内容
  // 指纹生成，同一份草稿（含失败后重试）复用，编辑后换新键。
  var projectDialog = {
    open: false,
    mode: null,            // null | "empty" | "selection"
    slides: [],            // 草稿切片（快照副本，可逐项移除）
    inFlight: false,       // R4 提交锁
    idemKey: null,         // 当前草稿的 Idempotency-Key（未发送/已失败时保留）
    idemFingerprint: "",   // 生成 idemKey 时的载荷指纹
    lastFocusEl: null,     // 打开者（Esc/关闭后归还焦点）
  };

  function uuid() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
      }
    } catch (e) { /* 回退 */ }
    // RFC4122 v4 形态的伪随机回退（crypto.getRandomValues 缺失时的兜底）
    var s = "";
    for (var i = 0; i < 36; i++) {
      if (i === 8 || i === 13 || i === 18 || i === 23) { s += "-"; continue; }
      s += Math.floor(Math.random() * 16).toString(16);
    }
    return s;
  }

  function projectDialogFingerprint(name, note, slides) {
    return JSON.stringify([name, note, slides.slice().sort()]);
  }

  function pcdShowError(msg) {
    if (!els.pcdError) return;
    els.pcdError.textContent = msg || "";
    els.pcdError.hidden = !msg;
  }

  function renderProjectDialogSlides() {
    if (!els.pcdSlidesList) return;
    els.pcdSlidesList.innerHTML = "";
    var n = projectDialog.slides.length;
    if (els.pcdSlidesSummary) {
      els.pcdSlidesSummary.textContent = n
        ? t("pcd.slides.contains", { n: n })
        : t("pcd.slides.empty.hint");
    }
    if (!n) return;
    projectDialog.slides.forEach(function (name) {
      var chip = document.createElement("span");
      chip.className = "pcd-slide-chip";
      var label = document.createElement("span");
      label.className = "pcd-slide-chip-name";
      label.textContent = truncateMiddle(name, 28);
      label.title = name;
      chip.appendChild(label);
      var rm = document.createElement("button");
      rm.type = "button";
      rm.className = "pcd-slide-chip-remove";
      rm.textContent = "×";
      rm.setAttribute("aria-label", t("pcd.slides.remove", { name: truncateMiddle(name, 20) }));
      rm.addEventListener("click", function () {
        if (projectDialog.inFlight) return;  // 提交中不允许改草稿
        projectDialog.slides = projectDialog.slides.filter(function (s) { return s !== name; });
        renderProjectDialogSlides();
      });
      chip.appendChild(rm);
      els.pcdSlidesList.appendChild(chip);
    });
  }

  // 手机端：侧栏抽屉与导入抽屉/对话框同为全屏浮层——打开后者前先收起侧栏
  // 抽屉（移动端 #sidebar 抽屉 z-index 300，若不收起会盖住遮罩劫持点击）。
  // 触发按钮多半在侧栏里（导入切片/新建项目都在侧栏顶部），随抽屉收起后
  // 不可聚焦，焦点归还目标改指向 ☰（closeDrawer 的 a11y 路径通常已先还给它）。
  function closeSidebarDrawerUnderOverlay(triggerEl) {
    if (!isMobileWidth() || !sidebarCtrl || !sidebarCtrl.isDrawerOpen ||
        !sidebarCtrl.isDrawerOpen()) {
      return triggerEl;
    }
    sidebarCtrl.closeDrawer();
    if (triggerEl && els.sidebar && els.sidebar.contains && els.sidebar.contains(triggerEl)) {
      return els.menuBtn || triggerEl;
    }
    return triggerEl;
  }

  // 打开对话框。mode="empty"：永远 slides=[]（普通新建）；
  // mode="selection"：复制 slidesSnapshot 快照（「新建项目(含选中)」）。
  function openProjectDialog(mode, slidesSnapshot, triggerEl) {
    if (!els.projectCreateMask) return;
    triggerEl = closeSidebarDrawerUnderOverlay(triggerEl);
    projectDialog.open = true;
    projectDialog.mode = mode === "selection" ? "selection" : "empty";
    projectDialog.slides = projectDialog.mode === "selection" && Array.isArray(slidesSnapshot)
      ? slidesSnapshot.slice() : [];
    projectDialog.inFlight = false;
    // R3：每次打开都是新草稿——旧键/旧指纹/旧输入一并清空
    projectDialog.idemKey = null;
    projectDialog.idemFingerprint = "";
    projectDialog.lastFocusEl = triggerEl || null;
    if (els.pcdName) els.pcdName.value = "";
    if (els.pcdNote) els.pcdNote.value = "";
    pcdShowError("");
    renderProjectDialogSlides();
    if (els.pcdConfirm) {
      els.pcdConfirm.disabled = false;
      els.pcdConfirm.textContent = t("pcd.confirm");
    }
    els.projectCreateMask.hidden = false;
    if (els.pcdName && typeof els.pcdName.focus === "function") {
      try { els.pcdName.focus(); } catch (e) { /* 忽略聚焦失败 */ }
    }
  }

  // 关闭并**清空草稿**（取消 / Esc / 遮罩点击 / 成功后共用）。
  // 提交进行中不允许关闭产生新状态：直接返回（按钮已禁用，防御 Enter 路径）。
  function closeProjectDialog() {
    if (!els.projectCreateMask) return;
    if (projectDialog.inFlight) return;
    projectDialog.open = false;
    projectDialog.mode = null;
    projectDialog.slides = [];
    projectDialog.idemKey = null;
    projectDialog.idemFingerprint = "";
    els.projectCreateMask.hidden = true;
    pcdShowError("");
    var back = projectDialog.lastFocusEl;
    projectDialog.lastFocusEl = null;
    if (back && typeof back.focus === "function") {
      try { back.focus(); } catch (e) { /* 触发按钮可能已移除 */ }
    }
  }

  // 定位新项目：展开行并滚到可见（成功后的「locate pid」）
  function locateProject(pid) {
    if (!pid || !els.projectList) return;
    var rows = els.projectList.children || [];
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      if (row.dataset && row.dataset.pid === pid) {
        if (row.classList) row.classList.add("expanded");
        if (typeof row.scrollIntoView === "function") {
          try { row.scrollIntoView({ block: "nearest" }); } catch (e) {}
        }
        return;
      }
    }
  }

  function ensureProjectIdemKey(payloadFp) {
    if (projectDialog.idemKey && projectDialog.idemFingerprint === payloadFp) {
      return projectDialog.idemKey;  // 同一草稿重试：复用（响应丢失幂等收口）
    }
    projectDialog.idemKey = uuid();
    projectDialog.idemFingerprint = payloadFp;
    return projectDialog.idemKey;
  }

  function submitProjectDialog() {
    if (!projectDialog.open || projectDialog.inFlight) return;  // R4 双保险
    if (!els.pcdName) return;
    var name = (els.pcdName.value || "").trim();
    if (!name) { pcdShowError(t("newproj.need.name")); try { els.pcdName.focus(); } catch (e) {} return; }
    var note = (els.pcdNote && els.pcdNote.value) || "";
    // slides 永远显式：空数组=空项目（R3），无任何隐式回退
    var slides = projectDialog.slides.slice();
    var fp = projectDialogFingerprint(name, note, slides);
    var idemKey = ensureProjectIdemKey(fp);
    projectDialog.inFlight = true;
    pcdShowError("");
    els.pcdConfirm.disabled = true;
    els.pcdConfirm.textContent = t("pcd.creating");
    var headers = { "Content-Type": "application/json", "Idempotency-Key": idemKey };
    apiFetch("/api/project/create", {
      method: "POST",
      headers: headers,
      body: JSON.stringify({ name: name, note: note, slides: slides }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, status: r.status, body: j };
        }, function () {
          return { ok: r.ok, status: r.status, body: null };
        });
      })
      .then(function (res) {
        if (!res.ok) {
          var msg = (res.body && res.body.error) || t("newproj.create.fail");
          throw new Error(msg);
        }
        return res.body || {};
      })
      .then(function (created) {
        toast(t("newproj.created"), "success");
        // 成功：关闭 + 清空草稿 + 勾选清零 + 重载并定位
        projectDialog.inFlight = false;
        projectDialog.open = false;
        projectDialog.slides = [];
        projectDialog.idemKey = null;
        projectDialog.idemFingerprint = "";
        if (els.projectCreateMask) els.projectCreateMask.hidden = true;
        if (els.pcdConfirm) {
          els.pcdConfirm.disabled = false;
          els.pcdConfirm.textContent = t("pcd.confirm");
        }
        slideChecked = {};
        renderUnfiled();
        reloadProjectsAndUnfiled().then(function () {
          locateProject(created && (created.pid || created.id));
        }).catch(function () {
          // 列表刷新失败不影响创建结果（对话框已关闭、草稿已清）
          locateProject(created && (created.pid || created.id));
        });
      })
      .catch(function (e) {
        // 失败：保留草稿与 idemKey（同草稿重试复用），恢复按钮
        projectDialog.inFlight = false;
        if (els.pcdConfirm) {
          els.pcdConfirm.disabled = false;
          els.pcdConfirm.textContent = t("pcd.confirm");
        }
        pcdShowError(t("newproj.create.fail2", { e: (e && e.message) ? e.message : e }));
      });
  }

  // 对话框焦点圈闭（Tab 不落回背景）：在最后一个可聚焦元素上 Tab 回到第一个。
  // 注意 els 表是 camelCase 键（els.pcdClose…），不能用字面 id 索引——
  // 旧写法 els["pcd-close"] 全为 undefined → focusables 恒空 → 圈闭失效。
  function pcdFocusables() {
    var list = [els.pcdClose, els.pcdName, els.pcdNote, els.pcdCancel, els.pcdConfirm];
    var out = [];
    list.forEach(function (el) {
      if (el && !(el.hidden)) out.push(el);
    });
    return out;
  }

  function handleProjectDialogKeydown(e) {
    if (!projectDialog.open) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeProjectDialog();
      return;
    }
    if (e.key === "Tab") {
      var focusables = pcdFocusables();
      if (!focusables.length) return;
      var active = document.activeElement;
      var first = focusables[0], last = focusables[focusables.length - 1];
      var inDialog = focusables.indexOf(active) >= 0;
      if (e.shiftKey && (!inDialog || active === first)) {
        e.preventDefault();
        try { last.focus(); } catch (err) {}
      } else if (!e.shiftKey && (!inDialog || active === last)) {
        e.preventDefault();
        try { first.focus(); } catch (err) {}
      }
    }
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
      // 账户设置入口（§3.5）：只展开侧栏/开抽屉（不抢搜索框焦点），
      // 随后由调用方滚动到既有改密/改绑卡片区
      expand: function () {
        if (isMobile()) {
          if (!drawerOpen()) applyDrawer(true);
          return;
        }
        if (collapsed) {
          collapsed = false;
          userTouched = true;
          applyDesktop();
          writeSidebarPref(deps.storage, scopeName(), collapsed);
        }
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

  // ---------- 切片搜索（2026-09-22 重做，slide-search-autofill-bug） ----------
  // 按需创建：默认 DOM 没有搜索输入框，点「搜索切片」才创建；关闭即清空
  // 过滤、移除输入框并把焦点还给按钮。首屏无输入框 + 防自动填充属性 +
  // 原生 autofill 标记检测，避免浏览器/密码管理器把账号邮箱填进侧栏顶部、
  // 造成「有数量、无列表、无解释」的误填状态。查询保留在输入框里，侧栏
  // 收起/展开、列表重渲不丢失；重渲后由 renderTail 重放当前条件。
  var slideSearchState = { open: false, wrap: null, label: null, input: null, closeBtn: null };

  // 原生 autofill 状态检测：能力检测 + try/catch，不支持的浏览器一律按
  // 「非自动填充」处理，绝不让检测异常中断列表加载/过滤
  function isNativeAutofilled(el) {
    if (!el || typeof el.matches !== "function") return false;
    try { return el.matches(":-webkit-autofill"); } catch (e) { return false; }
  }

  // 当前有效查询：输入框不存在（未创建/已移除/脱离 DOM）时为空串。读取时
  // 发现明确标记为自动填充的值：清掉该值并返回空查询（列表保持全量）。
  // 不按「含 @ / 像邮箱」拒绝——用户可能合法地按文件名/别名搜索。
  function getSlideQuery() {
    var st = slideSearchState;
    if (!st.open || !st.input || !st.input.isConnected) return "";
    if (st.input.value && isNativeAutofilled(st.input)) {
      st.input.value = "";
      return "";
    }
    return st.input.value;
  }

  // 动态控件文案/aria 随语言刷新（静态节点由 i18n.js applyLang 处理）
  function refreshSlideSearchTexts() {
    var st = slideSearchState;
    if (st.label) st.label.textContent = t("sb.search.label");
    if (st.input) {
      st.input.setAttribute("placeholder", t("sb.search.ph"));
      st.input.setAttribute("aria-label", t("sb.search.aria"));
    }
    if (st.closeBtn) {
      var closeLabel = t("sb.search.close");
      st.closeBtn.setAttribute("aria-label", closeLabel);
      st.closeBtn.title = closeLabel;
    }
  }

  function openSlideSearch() {
    var st = slideSearchState;
    if (!els.slideSearchArea) return;
    if (st.open) {
      // 已打开：按钮点击只把焦点送回输入框（关闭走 × / Escape）
      if (st.input && typeof st.input.focus === "function") {
        try { st.input.focus(); } catch (e) { /* 忽略聚焦失败 */ }
      }
      return;
    }
    var wrap = document.createElement("div");
    wrap.className = "slide-search-wrap";

    // 用途标签（可见 label，不只靠 placeholder 表明用途）
    var label = document.createElement("label");
    label.className = "slide-search-label";
    label.setAttribute("for", "slide-search");
    wrap.appendChild(label);

    var row = document.createElement("div");
    row.className = "slide-search-row";
    var input = document.createElement("input");
    input.type = "search";
    input.id = "slide-search";
    input.className = "slide-search";
    // 防自动填充（辅助措施）：非账号含义的字段名 + autocomplete off + 各
    // 密码管理器忽略标记；主要保障是首屏没有输入框与明确的用途展示
    input.name = "slide-filter";
    input.setAttribute("autocomplete", "off");
    input.setAttribute("data-lpignore", "true");
    input.setAttribute("data-1p-ignore", "");
    input.setAttribute("data-bwignore", "");
    input.setAttribute("data-form-type", "other");
    row.appendChild(input);

    var closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.id = "slide-search-close";
    closeBtn.className = "slide-search-close";
    closeBtn.textContent = "×";
    row.appendChild(closeBtn);
    wrap.appendChild(row);

    els.slideSearchArea.appendChild(wrap);
    st.open = true;
    st.wrap = wrap;
    st.label = label;
    st.input = input;
    st.closeBtn = closeBtn;

    function onSearchValue() {
      // 明确标记为浏览器原生 autofill 的值：清空并恢复列表（正常输入照常过滤）
      if (input.value && isNativeAutofilled(input)) {
        input.value = "";
        applySlideFilter("");
        return;
      }
      applySlideFilter(input.value);
    }
    input.addEventListener("input", onSearchValue);
    input.addEventListener("change", onSearchValue);
    // Escape 关闭搜索；阻止同一事件继续冒泡去关手机抽屉
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        closeSlideSearch();
      }
    });
    closeBtn.addEventListener("click", closeSlideSearch);

    if (els.slideSearchBtn) els.slideSearchBtn.setAttribute("aria-expanded", "true");
    refreshSlideSearchTexts();
    try { input.focus(); } catch (e) { /* 忽略聚焦失败 */ }
  }

  function closeSlideSearch() {
    var st = slideSearchState;
    if (!st.open) return;
    if (st.wrap && st.wrap.parentNode) st.wrap.parentNode.removeChild(st.wrap);
    st.open = false;
    st.wrap = null;
    st.label = null;
    st.input = null;
    st.closeBtn = null;
    // 清空过滤、恢复全部可见切片；计数与无匹配提示由 applySlideFilter 复位
    applySlideFilter("");
    if (els.slideSearchBtn) {
      els.slideSearchBtn.setAttribute("aria-expanded", "false");
      try { els.slideSearchBtn.focus(); } catch (e) { /* 忽略聚焦失败 */ }
    }
  }

  // ---------- 切片搜索过滤（升级 A：纯前端；收起不丢搜索条件） ----------
  // 匹配完整文件名（data-name）与显示别名（.slide-name 文本），大小写不敏感。
  // 项目行：名称命中 → 项目及其全部切片可见；仅切片命中 → 展开显示命中行；
  // 无命中隐藏。未归类：过滤时计数显示 匹配数/总数（如 0/4），全部被滤掉时
  // 显示「没有匹配的切片」提示，不留空白列表、不误写成没有上传切片。
  function applySlideFilter(raw) {
    if (!els.sidebar) return;
    var q = String(raw == null ? "" : raw).trim().toLowerCase();
    function rowMatches(row) {
      if (!q) return true;
      var nameEl = row.querySelector(".slide-name");
      var hay = String(row.getAttribute("data-name") || "") + " " +
        String((nameEl && nameEl.textContent) || "");
      return hay.toLowerCase().indexOf(q) >= 0;
    }
    // 未归类行 + 匹配数/总数计数 + 无匹配提示
    var unfiledRows = els.sidebar.querySelectorAll("#unfiled-list .slide-row");
    var unfiledVisible = 0;
    Array.prototype.forEach.call(unfiledRows, function (row) {
      var hit = rowMatches(row);
      row.style.display = hit ? "" : "none";
      if (hit) unfiledVisible += 1;
    });
    var filterEmpty = els.unfiledList
      ? els.unfiledList.querySelector(".unfiled-filter-empty") : null;
    var needEmpty = !!q && unfiledRows.length > 0 && unfiledVisible === 0;
    if (needEmpty) {
      if (!filterEmpty && els.unfiledList) {
        filterEmpty = document.createElement("div");
        filterEmpty.className = "unfiled-filter-empty";
        els.unfiledList.appendChild(filterEmpty);
      }
      if (filterEmpty) filterEmpty.textContent = t("sb.search.empty");
    } else if (filterEmpty && filterEmpty.parentNode) {
      filterEmpty.parentNode.removeChild(filterEmpty);
    }
    if (els.unfiledCount) {
      els.unfiledCount.textContent = (q && unfiledRows.length > 0)
        ? unfiledVisible + "/" + unfiledRows.length
        : String(unfiledRows.length);
    }
    // 有查询且存在未归类切片时展开未归类分区：命中行或「没有匹配的切片」
    // 提示必须可见（用户手动折叠态在查询期间不适用；清空查询不回收展开）
    if (q && unfiledRows.length > 0 && els.unfiledBody) {
      var unfiledSec = els.unfiledBody.closest(".section");
      if (unfiledSec) unfiledSec.classList.remove("collapsed");
    }
    // 项目行
    var projRows = els.sidebar.querySelectorAll(".proj-row");
    var projVisible = 0;
    Array.prototype.forEach.call(projRows, function (row) {
      var nameEl = row.querySelector(".proj-name");
      var nameHit = !!(q && nameEl &&
        String(nameEl.textContent || "").toLowerCase().indexOf(q) >= 0);
      var slideHit = false;
      Array.prototype.forEach.call(row.querySelectorAll(".slide-row"), function (s) {
        var hit = nameHit || rowMatches(s);
        s.style.display = hit ? "" : "none";
        if (hit) slideHit = true;
      });
      var show = !q || nameHit || slideHit;
      row.style.display = show ? "" : "none";
      if (show) projVisible += 1;
      // 过滤时命中即展开：项目名命中要整组可见，切片命中要看到命中行
      // （折叠体会把切片藏住；清空查询不回收用户手动折叠态）
      if (q && show && row.classList) row.classList.add("expanded");
    });
    // 项目区过滤反馈：有查询且没有可见项目行时显示「没有匹配的项目」，
    // 不留只有「项目」标题的空白；真实空态（暂无项目）在过滤期间让位
    var projList = els.projectList;
    if (projList) {
      var projEmpty = projList.querySelector(".proj-empty");
      var projHint = projList.querySelector(".proj-filter-empty");
      if (q && projVisible === 0) {
        if (projEmpty) projEmpty.style.display = "none";
        if (!projHint) {
          projHint = document.createElement("div");
          projHint.className = "proj-filter-empty";
          projList.appendChild(projHint);
        }
        projHint.textContent = t("sb.search.empty.projects");
      } else {
        if (projEmpty) projEmpty.style.display = "";
        if (projHint && projHint.parentNode) projHint.parentNode.removeChild(projHint);
      }
    }
  }

  // ---------- 移动端上下文动作条显隐 ----------
  // ROI 模式或箭头/描图绘制模式任一激活时，显示底部主栏上方的上下文条
  // （标注人输入 + 保存标记/保存图片）。桌面端不受影响（display:contents）。
  function updateCtxBar() {
    var on = state.roiMode != null || state.drawMode != null;
    document.body.classList.toggle("ctx-on", on);
  }

  // ---------- ⋯ 溢出菜单（§3.3 宽度分组的统一折叠目标；装 AI 副本/Reset/
  //   显示全部标记，以及按断点搬入的视图/标注/保存等真实 DOM 节点） ----------
  // 外点关闭走 document 级监听：#tbb-more-mask（z 490）会盖住 #app-header
  // （z 10）导致菜单项点击被 mask 吞掉（只关菜单不触发动作），故不再展示
  // mask（元素保留，移动端 CSS 不受影响）。
  function bindTbbMore() {
    if (!els.tbbMoreBtn || !els.tbbMore) return;
    var mask = $("tbb-more-mask");
    function closeMore() {
      els.tbbMore.classList.remove("open");
      if (mask) mask.classList.remove("open");
      els.tbbMoreBtn.setAttribute("aria-expanded", "false");
    }
    function openMore() {
      // 同一时间至多一个工具栏浮层：打开 ⋯ 前收起其它浮层
      toolbarPopClosers.forEach(function (fn) { fn(); });
      els.tbbMore.classList.add("open");
      els.tbbMoreBtn.setAttribute("aria-expanded", "true");
    }
    closeTbbMoreMenu = closeMore;
    els.tbbMoreBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (els.tbbMore.classList.contains("open")) { closeMore(); } else { openMore(); }
    });
    // 点击菜单外（viewer / 侧栏 / 工具栏其它控件）即关闭；菜单内部点击照常生效
    document.addEventListener("click", function (e) {
      if (!els.tbbMore.classList.contains("open")) return;
      var tgt = e.target;
      if (tgt && tgt.closest &&
          (tgt.closest("#tbb-more") || tgt.closest("#tbb-more-btn"))) return;
      closeMore();
    });
    if (mask) mask.addEventListener("click", closeMore);
    // Esc 关菜单（HIG：弹出层必须可键盘退出）
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && els.tbbMore.classList.contains("open")) closeMore();
    });
    // 菜单项点击后收起菜单（各自的原处理逻辑不变）
    els.tbbMore.addEventListener("click", function (e) {
      if (e.target && e.target.closest && e.target.closest(".tool-btn")) closeMore();
    });
    // ⋯ 菜单里的 AI 钮：转发给主 AI 钮（打开/关闭 AI 面板）
    if (els.tbbMoreAi) {
      els.tbbMoreAi.addEventListener("click", function () {
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

  // 工单 D：选中即快照（服务端态）——后续编辑（拖柄/改备注）的「上一版」，
  // commitAdminEdit 成功后作为撤销依据。重新选中会刷新快照。
  var editBeforeSnapshot = null;

  function selectEditItem(it) {
    editItem = it;
    state.focusAnno = it; // 选中某条 → 只显示它（focus 可见性）
    editing = false;  // 选中只是查看，不进入可拖动编辑态
    editBeforeSnapshot = {
      token: it.token,
      annotationId: it.annotation_id || null,
      index: it.index != null ? it.index : null,
      geom: snapshotGeom(it),
      note: it.note != null ? it.note : "",
      revision: Number(it.revision) > 0 ? Number(it.revision) : 0,
    };
    redrawAnnoCanvas();
    openEditCard(it);
  }

  function clearEditItem() {
    editItem = null;
    state.focusAnno = null; // 取消选中 → 恢复显示全部
    editing = false;
    editBeforeSnapshot = null;
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

  // =========================================================================
  // 工单 D（§5）：绘制会话状态机 + 可重试草稿 + 幂等提交
  //   idle → drawing → saving →（成功）selected（选中新标注/回到移动工具）
  //   saving 失败 → idle + retryDraft（几何保留、显示未保存、可 Enter 重试）
  // =========================================================================
  var drawPhase = "idle";     // "idle" | "drawing" | "saving"
  var retryDraft = null;      // 保存失败后的可重试草稿 {kind, geom, clientActionId}
  var drawUnsaved = false;    // 「未保存」提示（title + 状态标记）

  function setDrawPhase(p) {
    drawPhase = p;
    if (p === "idle" && !retryDraft) setDrawUnsaved(false);
    updateResearchBusy();  // P3：绘制/保存中取消稳定观察检测
  }

  // 未保存提示：不动布局（工单 D 约束），只在「保存标记」按钮 title/状态上
  // 标记 + 由失败 toast 告知。
  function setDrawUnsaved(on) {
    drawUnsaved = !!on;
    var btn = els.saveAnnoBtn;
    if (!btn) return;
    if (on) {
      btn.title = t("draw.unsaved.tip");
      btn.setAttribute("data-unsaved", "1");
    } else {
      // 防御：部分最小 DOM 桩没有 removeAttribute（真实浏览器恒有）
      if (typeof btn.removeAttribute === "function") {
        try { btn.removeAttribute("data-unsaved"); } catch (err) {}
      } else {
        btn.setAttribute("data-unsaved", "0");
      }
      var key = btn.getAttribute("data-i18n-title");
      btn.title = key ? t(key) : "";
    }
  }

  function newClientActionId() {
    return "web-" + Date.now().toString(36) + "-" +
      Math.random().toString(36).slice(2, 10);
  }

  // 从当前 UI 态收集「有效未提交草稿」：失败重试草稿优先，其次矩形选区，
  // 最后箭头/描图预览。无草稿返回 null。
  function collectPendingDraft() {
    if (retryDraft) return retryDraft;
    if (rectToolActive() && state.roi.w > 0) {
      return {
        kind: "rect",
        geom: { x: Math.round(state.roi.x), y: Math.round(state.roi.y),
                w: Math.round(state.roi.w), h: Math.round(state.roi.h) },
        clientActionId: null,
      };
    }
    if (state.drawMode && drawPreview) {
      var g = validPreviewGeom(drawPreview);
      if (g) {
        return { kind: drawPreview.type, geom: g, clientActionId: null };
      }
    }
    return null;
  }

  // 预览几何的有效性判定（与 finishDraw 的取消阈值同口径）：
  // 有效返回可提交 geom，无效返回 null。
  function validPreviewGeom(dp) {
    if (!dp) return null;
    if (dp.type === "arrow") {
      if (Math.hypot(dp.x2 - dp.x1, dp.y2 - dp.y1) < 10) return null;
      return { x1: dp.x1, y1: dp.y1, x2: dp.x2, y2: dp.y2 };
    }
    if (dp.type === "freehand") {
      var pts = dp.points || [];
      if (pts.length < 3) return null;
      var xs = pts.map(function (p) { return p[0]; });
      var ys = pts.map(function (p) { return p[1]; });
      var bb = Math.max(Math.max.apply(null, xs) - Math.min.apply(null, xs),
                        Math.max.apply(null, ys) - Math.min.apply(null, ys));
      if (bb < 10) return null;
      return { points: pts.map(function (p) { return [p[0], p[1]]; }) };
    }
    return null;
  }

  // Enter / 拖动完成 / 「保存标记」的统一入口：只提交有效未提交草稿
  function submitCurrentDraft() {
    var draft = collectPendingDraft();
    if (!draft) return;
    submitAnnotationDraft(draft);
  }

  // 保存失败后把草稿恢复成可见预览（箭头/描图），几何不清空
  function restorePreviewFromDraft(draft) {
    if (!draft || draft.kind === "rect") return;
    if (draft.kind === "arrow") {
      drawPreview = { type: "arrow", x1: draft.geom.x1, y1: draft.geom.y1,
                      x2: draft.geom.x2, y2: draft.geom.y2, armed: false };
    } else if (draft.kind === "freehand") {
      drawPreview = { type: "freehand",
                      points: draft.geom.points.map(function (p) { return [p[0], p[1]]; }),
                      lastScreen: null };
    }
    redrawAnnoCanvas();
  }

  // 统一提交（工单 D）：冻结 slide/几何/身份/幂等键；保存中忽略重复提交；
  // 切片切换后的回包不落到新切片；失败保留可重试草稿（不退工具、不清几何）。
  function submitAnnotationDraft(draft) {
    if (!state.slide) return;
    if (drawPhase === "saving") return; // 保存中禁止重复提交
    var slideName = state.slide.name;   // 冻结提交时的切片
    var label = (els.annoLabelInput.value || "").trim();
    if (!label) label = t("anno.default.user");
    var body = { slide: slideName, type: draft.kind, label: label, shared: false, note: "" };
    for (var k in draft.geom) body[k] = draft.geom[k];
    // 幂等键：重试沿用同一键（双击/重试不产生第二条）
    body.client_action_id = draft.clientActionId ||
      (retryDraft && retryDraft.clientActionId) || newClientActionId();
    var actionId = body.client_action_id;
    setDrawPhase("saving");
    if (els.saveAnnoBtn) els.saveAnnoBtn.disabled = true;
    apiFetch("/api/annotation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) {
          throw new Error(j.error || t("save.fail"));
        });
        return r.json();
      })
      .then(function (j) {
        // 切片已切换：回包不污染新切片（草稿静默丢弃，不套用/不选中）
        if (!state.slide || state.slide.name !== slideName) {
          retryDraft = null;
          drawPreview = null;
          drawPointer = null;
          setDrawPhase("idle");
          return;
        }
        retryDraft = null;
        setDrawUnsaved(false);
        // P3 研究采集：业务写入成功才记 annotation_create（失败路径在 catch，
        // 不产事件；§7.1）。业务标注 id 由采集模块换成会话内匿名局部 ID。
        if (researchTelemetry) {
          researchTelemetry.notifyAnnotation({
            action: "annotation_create",
            shapeType: draft.kind,
            geom: draft.geom,
            annotationId: j && j.annotation_id,
          });
        }
        // 撤销单元 = 语义操作（这次创建），压入撤销栈（限定本身份/本切片）
        var entry = pushUndoEntry({
          kind: "create",
          slide: slideName,
          userId: currentUserId,
          annoKind: draft.kind,
          geom: JSON.parse(JSON.stringify(draft.geom)),
          label: label,
          annotationId: j && j.annotation_id,
          index: j && j.index,
          revision: j && j.revision,
          clientActionId: actionId,
        });
        if (draft.kind === "rect") {
          toast(t("anno.saved.tip"), "success");
        } else {
          toast(t("anno.saved"), "success");
        }
        // 成功后回到移动/平移工具；刷新并按 annotation_id 选中新标注
        if (draft.kind === "rect") { exitRoi(); } else { exitDrawMode(); }
        refreshCurrentAnnotations().then(function () {
          selectCreatedAnnotation(j && j.annotation_id);
        });
        loadAnnotationsIndex().then(function () {
          renderProjects(allProjects);
          renderUnfiled();
        });
        // 保存期间用户按过撤销 → 现在执行该次创建的逆操作（仍是权限内 DELETE）
        if (pendingUndoAfterSave && entry) {
          pendingUndoAfterSave = false;
          performUndo();
        }
      })
      .catch(function (e) {
        // 失败：可重试草稿；不清几何、不退工具（工单 D 核心契约）。
        // 保存未成功 → 之前记录的撤销意图作废（没有创建就无逆操作）。
        pendingUndoAfterSave = false;
        retryDraft = {
          kind: draft.kind,
          geom: draft.geom,
          clientActionId: actionId,
        };
        setDrawPhase("idle");
        setDrawUnsaved(true);
        if (draft.kind === "rect") {
          if (els.saveAnnoBtn) els.saveAnnoBtn.disabled = false; // 允许按钮重试
          if (els.saveBtn) els.saveBtn.disabled = false;         // 裁剪导出不受保存失败影响
        } else {
          restorePreviewFromDraft(retryDraft);
        }
        toast(t("draw.unsaved.retry", { e: e.message }), "error");
      });
  }

  function enterDrawMode(mode) {
    if (!state.slide) { toast(t("roi.need.slide"), "error"); return; }
    exitRoi();
    state.drawMode = mode;
    els.annoArrowBtn.classList.toggle("active", mode === "arrow");
    els.annoFreeBtn.classList.toggle("active", mode === "freehand");
    var c = els.annoCanvas;
    c.classList.add("drawing");
    if (viewer) viewer.setMouseNavEnabled(false);
    syncAnnoAllBtns();
    redrawAnnoCanvas();
    updateCtxBar();
    // §3.3 narrow 档「只留当前工具」：激活的绘制工具所在组保留在主行
    applyToolbarTier();
    toast(mode === "arrow" ? t("draw.arrow.tip") : t("draw.free.tip"), "info");
    updateResearchBusy();  // P3
  }

  function exitDrawMode() {
    state.drawMode = null;
    drawPreview = null;
    drawPointer = null;
    retryDraft = null;           // 工单 D：退出工具 = 放弃未保存草稿
    setDrawUnsaved(false);
    setDrawPhase("idle");
    els.annoArrowBtn.classList.remove("active");
    els.annoFreeBtn.classList.remove("active");
    if (els.annoCanvas) els.annoCanvas.classList.remove("drawing");
    if (viewer) viewer.setMouseNavEnabled(true);
    redrawAnnoCanvas();
    updateCtxBar();
    applyToolbarTier();
    updateResearchBusy();  // P3
  }

  function toggleDrawMode(mode) {
    if (state.drawMode === mode) { exitDrawMode(); return; }
    enterDrawMode(mode);
  }

  function onAnnoPointerDown(e) {
    if (!state.slide) return;
    // 工单 E：右键（button=2）不进入绘制/编辑/选中路径——右键归上下文
    // 菜单（contextmenu 事件统一处理）；绘制中的右键取消逻辑归工单 D。
    if (e && e.button === 2) return;
    // 矩形工具优先（升级 C：画布层拖出矩形/点击中心放置）
    if (rectToolActive()) {
      onRectCanvasPointerDown(e);
      return;
    }
    // 绘制模式优先
    if (state.drawMode) {
      if (e.button === 2) return; // 右键不启动绘制（留给右键菜单）
      e.preventDefault(); e.stopPropagation();
      // 重新落笔 = 放弃失败草稿的重试（Enter 重试在此之前仍可用）
      if (retryDraft) { retryDraft = null; setDrawUnsaved(false); }
      var c = els.annoCanvas;
      var img0 = screenToImg(e);
      if (state.drawMode === "arrow") {
        // 箭头（工单 D）：支持两种完成方式——
        //  a) 按下拖到终点松开（原有）；
        //  b) 单击起点 → 移动预览 → 再单击/双击/Enter 定终点。
        // 已有 armed 起点 → 本次按下即定终点（松开时判定）。
        if (drawPreview && drawPreview.type === "arrow" && drawPreview.armed) {
          try { c.setPointerCapture(e.pointerId); } catch (err) {}
          drawPointer = { id: e.pointerId };
          drawPreview.armed = false;
          drawPreview.pressing = true;
          drawPreview.x2 = img0.x; drawPreview.y2 = img0.y;
          drawPreview.sx0 = e.clientX; drawPreview.sy0 = e.clientY;
        } else {
          try { c.setPointerCapture(e.pointerId); } catch (err) {}
          drawPointer = { id: e.pointerId };
          drawPreview = { type: "arrow", x1: img0.x, y1: img0.y, x2: img0.x, y2: img0.y,
                          armed: true, pressing: true,
                          sx0: e.clientX, sy0: e.clientY };
        }
        setDrawPhase("drawing");
      } else {
        try { c.setPointerCapture(e.pointerId); } catch (err) {}
        drawPointer = { id: e.pointerId };
        drawPreview = { type: "freehand", points: [[img0.x, img0.y]],
                        lastScreen: screenPt(e),
                        sx0: e.clientX, sy0: e.clientY, moved: false };
        setDrawPhase("drawing");
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
        // 拖动中或 armed 悬停都更新终点（armed 时无指针捕获也可预览）
        drawPreview.x2 = img.x; drawPreview.y2 = img.y;
      } else if (drawPointer) {
        // 描图只在按住时收集点（悬停不加点；恢复的失败草稿不被鼠标漂移污染）
        var sp0 = screenPt(e);
        if (Math.hypot(e.clientX - drawPreview.sx0, e.clientY - drawPreview.sy0)
            > RECT_DRAG_SCREEN_PX) {
          drawPreview.moved = true;
        }
        var last = drawPreview.lastScreen;
        if (!last || Math.hypot(sp0.x - last.x, sp0.y - last.y) > 4) {
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
      var c = els.annoCanvas;
      if (drawPointer) { try { c.releasePointerCapture(drawPointer.id); } catch (err) {} }
      drawPointer = null;
      var dragged = Math.hypot(e.clientX - drawPreview.sx0,
                               e.clientY - drawPreview.sy0) > RECT_DRAG_SCREEN_PX;
      if (drawPreview.type === "arrow") {
        if (drawPreview.armed && !dragged) {
          // 首次单击：只定起点，不产生零长度记录（工单 D）
          drawPreview.pressing = false;
          setDrawPhase("idle");
          redrawAnnoCanvas();
          return;
        }
        // 拖动松开（原有路径）或第二击定终点 → 完成
        finishDraw();
        return;
      }
      // 描图：拖动松开完成；纯单击（无位移）静默清稿留在工具内
      if (!drawPreview.moved && drawPreview.points.length < 3) {
        drawPreview = null;
        setDrawPhase("idle");
        redrawAnnoCanvas();
        return;
      }
      finishDraw();
      return;
    }
    if (!editDrag) return;
    e.preventDefault(); e.stopPropagation();
    endEditDrag(e);
  }

  // 工单 D：pointercancel / 丢失指针捕获 = 取消恢复，绝不走完成/保存路径。
  // - 矩形：恢复拖前选区（onRectCanvasPointerCancel）
  // - 箭头：armed 起点保留（等待终点），活动笔画丢弃
  // - 描图：丢弃当前笔画，留在工具内
  function onAnnoPointerCancel(e) {
    if (rectToolActive() && rectDrawInfo) {
      onRectCanvasPointerCancel(e);
      return;
    }
    if (state.drawMode && drawPreview) {
      if (e && e.preventDefault) { try { e.preventDefault(); } catch (err) {} }
      var c = els.annoCanvas;
      if (drawPointer) { try { c.releasePointerCapture(drawPointer.id); } catch (err) {} }
      drawPointer = null;
      if (drawPreview.type === "arrow" && drawPreview.armed) {
        drawPreview.pressing = false; // 起点仍在，等待终点
      } else {
        drawPreview = null;           // 丢弃被中断的笔画
      }
      setDrawPhase("idle");
      redrawAnnoCanvas();
      return;
    }
    if (editDrag) { cancelEditDragRestore(); }
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
        var n = normalizeRect(x0, y0, x1, y1);
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

  // 工单 D：编辑拖拽被打断 → 恢复拖前几何（本地快照），不保留半截修改
  function cancelEditDragRestore() {
    var c = els.annoCanvas;
    var d = editDrag;
    if (d) { try { c.releasePointerCapture(d.pointerId); } catch (err) {} }
    editDrag = null;
    if (d && d.item && d.start) {
      var s = d.start, it = d.item;
      var typ = it.type || "rect";
      if (typ === "rect") {
        it.x = s.x; it.y = s.y; it.w = s.w; it.h = s.h;
        if (s.side_px != null) it.side_px = s.side_px;
      } else if (typ === "arrow") {
        it.x1 = s.x1; it.y1 = s.y1; it.x2 = s.x2; it.y2 = s.y2;
      } else if (typ === "freehand") {
        it.points = s.points;
      }
    }
    if (viewer) viewer.setMouseNavEnabled(true);
    redrawAnnoCanvas();
  }

  // 完成绘制（工单 D）：几何无效 → 只撤当前草稿（留在工具内，QuPath 习惯）；
  // 有效 → submitAnnotationDraft 自动保存。失败路径由 submitAnnotationDraft
  // 保留可重试草稿，不在这里退工具。
  function finishDraw() {
    var dp = drawPreview;
    var c = els.annoCanvas;
    if (drawPointer) { try { c.releasePointerCapture(drawPointer.id); } catch (err) {} }
    drawPointer = null;
    drawPreview = null;
    if (!dp) { exitDrawMode(); return; }
    var g = validPreviewGeom(dp);
    if (!g) {
      if (dp.type === "arrow") { toast(t("draw.short.cancel"), "info"); }
      else if ((dp.points || []).length < 3) { toast(t("draw.few.cancel"), "info"); }
      else { toast(t("draw.small.cancel"), "info"); }
      setDrawPhase("idle");
      redrawAnnoCanvas();
      return;
    }
    submitAnnotationDraft({ kind: dp.type, geom: g, clientActionId: null });
  }

  // 撤销当前绘制草稿（不清别人的东西）：矩形走 discardRectDraft；
  // 描图先撤最后一个控制点，点数不足再清整条；箭头直接清。
  function undoWhileDrawing() {
    if (rectDrawInfo) { onRectCanvasPointerCancel(null); return true; }
    if (rectToolActive()) {
      if (retryDraft && retryDraft.kind === "rect") { discardRectDraft(); return true; }
      if (state.roi.w > 0) { discardRectDraft(); return true; }
      return false;
    }
    if (state.drawMode && drawPreview) {
      if (drawPreview.type === "freehand" && drawPreview.points.length > 1) {
        drawPreview.points.pop();
        if (!drawPreview.lastScreen) drawPreview.lastScreen = null;
        redrawAnnoCanvas();
        return true;
      }
      drawPreview = null;
      drawPointer = null;
      setDrawPhase("idle");
      redrawAnnoCanvas();
      return true;
    }
    if (state.drawMode && retryDraft) {
      // 失败草稿：撤草稿、留在工具内
      retryDraft = null;
      setDrawUnsaved(false);
      drawPreview = null;
      redrawAnnoCanvas();
      return true;
    }
    return false;
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

  // 保存管理员标注（arrow/freehand/rect 统一入口；工单 D 后为幂等提交包装）
  function saveAnnotation(geom) {
    if (!state.slide) return;
    if (!geom || !geom.type) return;
    var g = {};
    for (var k in geom) {
      if (k === "type") continue;
      g[k] = geom[k];
    }
    submitAnnotationDraft({ kind: geom.type, geom: g, clientActionId: null });
  }

  // 创建成功后按 annotation_id 选中新标注（0056 响应字段；A 工单已加）
  function selectCreatedAnnotation(annotationId) {
    if (!annotationId) return;
    var item = findItemByAnnotationId(annotationId);
    if (!item) return;
    // 选中即显示：打开 showAnno 以便新标注可见（focus 只显示该条）
    if (!state.showAnno) {
      state.showAnno = true;
      syncAnnoAllBtns();
    }
    selectEditItem(item);
  }

  function findItemByAnnotationId(annotationId) {
    if (!annotationId) return null;
    var items = flatAnnoItems();
    for (var i = 0; i < items.length; i++) {
      if (String(items[i].annotation_id || "") === String(annotationId)) {
        return items[i];
      }
    }
    return null;
  }

  // =========================================================================
  // 工单 D：Ctrl/Cmd+Z 撤销 / Ctrl/Cmd+Shift+Z（及 Ctrl+Y）重做
  // 撤销单元 = 语义操作（创建/编辑），不是每次 pointermove。
  // 栈限定：当前身份 + 当前切片 + 本地发起的操作（他人更新不入栈，也绝不
  // 被撤销）。创建的逆 = DELETE（expected_revision CAS）；编辑的逆 = PATCH
  // 回上一版几何/备注（expected_revision CAS）；409 → 不覆盖，toast 冲突。
  // =========================================================================
  var undoStack = [];
  var redoStack = [];
  var pendingUndoAfterSave = false; // 保存进行中收到撤销意图 → 成功后补执行

  function pushUndoEntry(fields) {
    redoStack.length = 0; // 新操作清空重做栈（标准撤销语义）
    var entry = fields;
    undoStack.push(entry);
    return entry;
  }

  function undoEntryInScope(entry) {
    if (!entry) return false;
    if (entry.slide && state.slide && entry.slide !== state.slide.name) return false;
    if (entry.userId != null && currentUserId != null &&
        String(entry.userId) !== String(currentUserId)) return false;
    return true;
  }

  // 弹出栈顶直到找到仍属当前身份/切片的条目（切换后旧条目直接作废）
  function popScoped(stack) {
    while (stack.length) {
      var top = stack[stack.length - 1];
      if (undoEntryInScope(top)) return stack.pop();
      stack.pop();
    }
    return null;
  }

  function annoIdUrl(annotationId, suffix) {
    var path = "/api/annotation/id/" + encodeURIComponent(annotationId);
    return suffix ? path + suffix : path;
  }

  function sendAnnoDelete(entry, expectedRevision) {
    if (!entry || !entry.annotationId) {
      return Promise.resolve({ ok: false, status: 404, error: "missing_id" });
    }
    var body = {};
    if (expectedRevision != null) body.expected_revision = expectedRevision;
    return apiFetch(annoIdUrl(entry.annotationId), {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (r.ok) {
        return r.json().then(function (j) {
          // P3 研究采集：删除业务写入成功才记事件（显式删除与撤销创建
          // 同一 DELETE 入口；§7.1 匿名局部标注 ID，无几何字段）
          if (researchTelemetry) {
            researchTelemetry.notifyAnnotation({
              action: "annotation_delete",
              annotationId: entry.annotationId,
            });
          }
          return { ok: true, status: r.status, revision: j && j.revision };
        }).catch(function () { return { ok: true, status: r.status }; });
      }
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: false, status: r.status, error: j && j.error,
                 conflict: r.status === 409,
                 currentRevision: j && j.current_revision };
      });
    });
  }

  function sendAnnoPatch(entry, geom, note, expectedRevision) {
    if (!entry || !entry.annotationId) {
      return Promise.resolve({ ok: false, status: 404, error: "missing_id" });
    }
    var body = { geom: geom, note: note };
    if (expectedRevision != null) body.expected_revision = expectedRevision;
    return apiFetch(annoIdUrl(entry.annotationId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (r.ok) {
        return r.json().then(function (j) {
          return { ok: true, status: r.status, revision: j && j.revision,
                   annotationId: j && j.annotation_id };
        }).catch(function () { return { ok: true, status: r.status }; });
      }
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: false, status: r.status, error: j && j.error,
                 conflict: r.status === 409,
                 currentRevision: j && j.current_revision };
      });
    });
  }

  // 撤销「创建」：按稳定 annotation_id 删除。目标不存在视为已撤销，绝不回退 index。
  function undoCreateEntry(entry) {
    if (!entry.annotationId) return Promise.resolve(true);
    var rev = Number(entry.revision) > 0 ? Number(entry.revision) : null;
    return sendAnnoDelete(entry, rev).then(function (res) {
      if (res.ok || res.status === 404) {
        if (res.ok && res.revision != null) entry.revision = res.revision;
        refreshAfterUndo(entry);
        return true;
      }
      if (res.conflict) {
        toast(t("undo.conflict", {
          rev: res.currentRevision != null ? res.currentRevision : "?",
        }), "error");
        return false;
      }
      toast(t("undo.fail", { e: res.error || res.status }), "error");
      return false;
    });
  }

  // PATCH geom 清洗：去掉 side_px（v2 w/h 与 side_px 冲突会被 store 拒绝；
  // 快照兼容旧字段的读取，但提交只走 v2 成对 w/h）
  function cleanPatchGeom(g) {
    var out = {};
    for (var k in g) {
      if (k === "side_px") continue;
      out[k] = g[k];
    }
    return out;
  }

  // 撤销「编辑」：PATCH 回上一版；CAS 用本次操作完成后的 revision，不用列表最新值。
  function undoEditEntry(entry) {
    if (!entry.annotationId) {
      toast(t("undo.fail", { e: "" }), "error");
      return Promise.resolve(false);
    }
    var rev = Number(entry.revision) > 0 ? Number(entry.revision) : null;
    return sendAnnoPatch(entry, cleanPatchGeom(entry.before.geom),
                         entry.before.note, rev)
      .then(function (res) {
        if (res.ok) {
          if (res.revision != null) entry.revision = res.revision;
          refreshAfterUndo(entry);
          return true;
        }
        if (res.conflict) {
          toast(t("undo.conflict", {
            rev: res.currentRevision != null ? res.currentRevision : "?",
          }), "error");
          return false;
        }
        toast(t("undo.fail", { e: res.error || res.status }), "error");
        return false;
      });
  }

  function refreshAfterUndo(entry) {
    refreshCurrentAnnotations();
    loadAnnotationsIndex().then(function () {
      renderProjects(allProjects);
      renderUnfiled();
    });
  }

  function performUndo() {
    if (drawPhase === "saving") {
      // 保存进行中：记录意图，成功后补执行（不能只隐藏前端对象）
      pendingUndoAfterSave = true;
      toast(t("undo.pending"), "info");
      return;
    }
    if (undoWhileDrawing()) return; // 绘制中：先撤草稿/上一控制点
    var entry = popScoped(undoStack);
    if (!entry) return;
    var p = entry.kind === "create" ? undoCreateEntry(entry) : undoEditEntry(entry);
    p.then(function (ok) {
      if (ok) {
        redoStack.push(entry);
        toast(t("undo.done"), "success");
      } else {
        undoStack.push(entry); // 冲突/失败：条目留在撤销栈
      }
    });
  }

  function performRedo() {
    if (drawPhase === "saving") return;
    var entry = popScoped(redoStack);
    if (!entry) return;
    var p;
    if (entry.kind === "create") {
      // 重做创建：恢复原 annotation_id 的 tombstone，不重用 client_action_id 再 INSERT。
      if (!entry.annotationId) {
        toast(t("redo.fail", { e: "" }), "error");
        redoStack.push(entry);
        return;
      }
      var restoreBody = {};
      if (Number(entry.revision) > 0) restoreBody.expected_revision = Number(entry.revision);
      p = apiFetch(annoIdUrl(entry.annotationId, "/restore"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(restoreBody),
      }).then(function (r) {
        if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) {
          return { ok: false, error: j && j.error, status: r.status,
                   conflict: r.status === 409, currentRevision: j && j.current_revision };
        });
        return r.json().then(function (j) { return { ok: true, j: j }; });
      }).then(function (res) {
        if (res.ok) {
          if (res.j && res.j.revision != null) entry.revision = res.j.revision;
          refreshAfterUndo(entry);
          return true;
        }
        if (res.conflict) {
          toast(t("undo.conflict", {
            rev: res.currentRevision != null ? res.currentRevision : "?",
          }), "error");
          return false;
        }
        toast(t("redo.fail", { e: res.error || res.status }), "error");
        return false;
      });
    } else {
      if (!entry.annotationId) {
        toast(t("redo.fail", { e: "" }), "error");
        redoStack.push(entry);
        return;
      }
      var rev = Number(entry.revision) > 0 ? Number(entry.revision) : null;
      p = sendAnnoPatch(entry, entry.after.geom, entry.after.note, rev)
        .then(function (res) {
          if (res.ok) {
            if (res.revision != null) entry.revision = res.revision;
            refreshAfterUndo(entry);
            return true;
          }
          if (res.conflict) {
            toast(t("undo.conflict", {
              rev: res.currentRevision != null ? res.currentRevision : "?",
            }), "error");
            return false;
          }
          toast(t("redo.fail", { e: res.error || res.status }), "error");
          return false;
        });
    }
    p.then(function (ok) {
      if (ok) {
        undoStack.push(entry);
        toast(t("redo.done"), "success");
      } else {
        redoStack.push(entry);
      }
    });
  }

  function canUndo() { return undoStack.some(undoEntryInScope); }
  function canRedo() { return redoStack.some(undoEntryInScope); }

  // 查看器级键盘（工单 D）：Escape / Enter / Ctrl(Cmd)+Z / +Shift+Z / Ctrl+Y。
  // 输入控件（含 contenteditable）内不劫持——保留原生文字撤销与表单行为。
  function onViewerKeydown(e) {
    var tgt = e.target;
    var tag = tgt && tgt.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
        (tgt && tgt.isContentEditable)) return;
    var mod = !!(e.ctrlKey || e.metaKey);
    if (mod && !e.altKey && (e.key === "z" || e.key === "Z")) {
      e.preventDefault();
      if (e.shiftKey) { performRedo(); } else { performUndo(); }
      return;
    }
    if (mod && !e.altKey && (e.key === "y" || e.key === "Y")) {
      e.preventDefault();
      performRedo();
      return;
    }
    if (e.key === "Escape") {
      if (rectToolActive()) { onRectKeydown(e); return; }
      if (state.drawMode) {
        e.preventDefault();
        if (drawPreview || retryDraft) {
          // 有当前绘制/失败草稿 → 只撤草稿，留在工具内
          drawPreview = null;
          drawPointer = null;
          retryDraft = null;
          setDrawUnsaved(false);
          setDrawPhase("idle");
          redrawAnnoCanvas();
          toast(t("draw.cancelled"), "info");
        } else {
          exitDrawMode();
        }
      }
      return;
    }
    if (e.key === "Enter") {
      if (rectToolActive()) { onRectKeydown(e); return; }
      if (state.drawMode) {
        // Enter 只提交「有效未提交草稿」（armed 箭头 / 保存失败重试）
        var draft = collectPendingDraft();
        if (draft) {
          e.preventDefault();
          submitAnnotationDraft(draft);
        }
      }
    }
  }

  // 重新拉取当前切片标注并重绘（工单 D：返回 Promise 供「选中新标注」等待）
  function refreshCurrentAnnotations() {
    if (!state.slide) { redrawAnnoCanvas(); return Promise.resolve(); }
    return apiFetch("/api/annotations?slide=" + encodeURIComponent(state.slide.name))
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
    updateResearchBusy();  // P3：面板打开取消稳定观察检测
  }

  function closeAnnoPanel() {
    annoPanelOpen = false;
    els.annoPanel.style.display = "none";
    updateResearchBusy();  // P3
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
        // P3 研究采集：AI 标注审核写入成功才记 annotation_accept/reject
        // （§7.1：来源 human_review，与 AI 自动标注不混同）
        if (researchTelemetry) {
          researchTelemetry.notifyAnnotation({
            action: action === "accept"
              ? "annotation_accept" : "annotation_reject",
            annotationId: it.annotation_id,
          });
        }
        refreshCurrentAnnotations();
      })
      .catch(function (e) { toast(e.message || t("anno.update.fail"), "error"); });
  }

  function toggleAnnoShared(it, btnEl, rowEl) {
    if (!it.annotation_id) { toast(t("anno.no.src.token"), "error"); return; }
    var target = !it.shared;
    btnEl.disabled = true;
    var slideName = it.slide || (state.slide && state.slide.name);
    apiFetch("/api/share/list")
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (shares) {
        var active = (shares || []).filter(function (sh) {
          return sh.status === "active" && Array.isArray(sh.slides)
            && sh.slides.indexOf(slideName) >= 0;
        });
        if (target && active.length === 0) {
          throw new Error(t("anno.share.need.link"));
        }
        var chain = Promise.resolve();
        active.forEach(function (sh) {
          chain = chain.then(function () {
            return apiFetch("/api/annotation/id/" + encodeURIComponent(it.annotation_id), {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                grantee_kind: "share_token",
                grantee_id: sh.token,
                revoke_grant: !target,
              }),
            }).then(function (r) {
              if (!r.ok) return r.json().then(function (j) {
                throw new Error(j.error || (t("anno.update.fail") + " " + r.status));
              });
              return r.json();
            });
          });
        });
        return chain;
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
        updateResearchBusy();  // P3：编辑卡打开取消稳定观察检测
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
    if (!it || !it.annotation_id) {
      toast(t("save.fail"), "error");
      return;
    }
    var geom = buildEditGeom(it);
    var body = { geom: geom, note: noteVal };
    if (Number(it.revision) > 0) body.expected_revision = Number(it.revision);
    // rect 的 size_mm 前端重算（仅真正方形的兼容展示字段；v2 附属信息）
    if ((it.type || "rect") === "rect" && geom.w === geom.h &&
        state.mppX && state.mppX > 0) {
      body.geom.size_mm = Math.round(geom.w * state.mppX / 1000 * 100) / 100;
    }
    apiFetch("/api/annotation/id/" + encodeURIComponent(it.annotation_id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (r.status === 409) {
        return r.json().then(function (j) {
          var err = new Error(j.error || "revision_conflict");
          err.conflict = true;
          err.currentRevision = j.current_revision;
          throw err;
        });
      }
      if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("save.fail")); });
      return r.json();
    })
      .then(function (j) {
        toast(t("edit.saved"), "success");
        // P3 研究采集：编辑业务写入成功才记 annotation_update（§7.1）
        if (researchTelemetry) {
          researchTelemetry.notifyAnnotation({
            action: "annotation_update",
            shapeType: it.type || "rect",
            geom: geom,
            annotationId: it.annotation_id,
          });
        }
        var snap = editBeforeSnapshot;
        if (snap && state.slide && (snap.annotationId || it.annotation_id)) {
          pushUndoEntry({
            kind: "edit",
            slide: state.slide.name,
            userId: currentUserId,
            annotationId: snap.annotationId || it.annotation_id,
            revision: (j && j.revision != null) ? j.revision : it.revision,
            before: { geom: snap.geom, note: snap.note },
            after: { geom: geom, note: noteVal },
          });
        }
        editItem = null;
        editing = false;
        editBeforeSnapshot = null;
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
          updateDocTitle(null);
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
      // note（可选，COS 直传引入）：阶段后的补充说明（如排队位置/续传提示），
      // 既有调用方不传不受影响
      setStage: function (stageKey, frac, note) {
        var label = tt(stageKey);
        if (frac !== undefined && frac !== null && isFinite(frac)) {
          label += " " + Math.round(frac * 100) + "%";
        }
        if (note) { label += " · " + note; }
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
            if (!body) return null;
            if (body.state === "committed") {
              // commit 已收口但转换入队/响应可能丢失：重放 commit 取 conversion_job_id
              return { upload_id: saved.upload_id, committed: true,
                       chunk_size: body.chunk_size,
                       offset: body.confirmed_offset | 0 };
            }
            if (body.state !== "active") return null;
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
      // 3) 串行传完全部分片；committed 恢复跳过 PUT，直接重放 commit
      if (task && task.committed) return;
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
    }).then(function (body) {
      try { localStorage.removeItem(key); } catch (e) { /* 同上 */ }
      if (body && body.conversion_job_id && body.state && body.state !== "ready") {
        return pollConversionJob(body, row);
      }
      var openName = (body && body.state === "ready" && body.canonical_name)
        ? body.canonical_name : file.name;
      row.setStage("upload.stage.done");
      row.finish();
      toast(t("upload.done", { name: openName }), "success");
      loadAll();
      importAssociateUploaded(openName);   // W4：上传行自身的目标关联（若有）
      openSlide(openName);
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

  // 转换轮询（W4/R5 修复）：后台任务状态是成功的唯一权威。
  //  - 本地观察超过 15 分钟**不再判失败**：提示「仍在后台处理」后继续退避轮询；
  //  - 401/403：权限失效 → 停止轮询（不再无意义重试）；
  //  - 404：任务不存在（终态提示，不冒充失败重试）；
  //  - 其他非 2xx / 网络异常：显示「暂时无法获取进度」并退避重试，绝不永久判失败；
  //  - ready：保持既有行为（完成 + 刷新 + 打开本行上传的切片——这是上传行
  //    自身的原行为，不属于持久任务列表的自动抢占）；失败/取消仍以后端为准。
  function pollConversionJob(body, row) {
    var jobId = body.conversion_job_id;
    var canonical = body.canonical_name;
    row.setStage("upload.stage.converting");
    var started = Date.now();
    var OBSERVE_NOTE_MS = 15 * 60 * 1000;   // 仅切换提示文案，不停轮询
    var notedStillProcessing = false;
    var stopped = false;
    var delay = 2000;                       // 退避：2s 起步，上限 15s
    function schedule() {
      if (stopped) return;
      delay = Math.min(delay * 2, 15000);
      setTimeout(tick, delay);
    }
    function tick() {
      if (stopped) return;
      if (!notedStillProcessing && Date.now() - started > OBSERVE_NOTE_MS) {
        notedStillProcessing = true;
        row.setStage("imp.conv.still.processing");  // 「仍在后台处理」（不是失败）
      }
      apiFetch("/api/conversions/" + encodeURIComponent(jobId))
        .then(function (r) {
          return r.json().then(function (j) {
            return { ok: r.ok, status: r.status, body: j };
          }, function () {
            return { ok: r.ok, status: r.status, body: null };  // body 非 JSON
          });
        })
        .then(function (res) {
          if (stopped) return;
          if (res.status === 401 || res.status === 403) {
            stopped = true;
            row.setStage("imp.conv.auth.stop");
            row.finish(10000);
            return;
          }
          if (res.status === 404) {
            stopped = true;
            row.setStage("imp.conv.missing");
            row.finish(10000);
            return;
          }
          if (!res.ok) {
            row.setStage("imp.conv.progress.unavailable");
            schedule();
            return;
          }
          var st = res.body && res.body.state;
          if (st === "ready") {
            var name = (res.body.canonical_name || canonical);
            row.setStage("upload.stage.done");
            row.finish();
            toast(t("upload.done", { name: name }), "success");
            loadAll();
            importAssociateUploaded(name);   // W4：上传行自身的目标关联（若有）
            if (name) openSlide(name);
            return;
          }
          if (st === "failed" || st === "cancelled") {
            row.markError();
            row.setStage("upload.stage.failed");
            row.finish(10000);
            toast(t("upload.fail", { e: t("upload.err.conversion") }), "error");
            return;
          }
          schedule();
        })
        .catch(function () {
          if (stopped) return;
          row.setStage("imp.conv.progress.unavailable");
          schedule();
        });
    }
    tick();
  }

  function uploadFile(file, opts) {
    if (!file) return;
    opts = opts || {};
    // COS 选路（Phase 4，manual_only）：只在任务开始前判一次（D8 开始后
    // transport 冻结）。opts.platform 是用户点了「改用平台上传」的显式选择
    // （COS 422 超上限后的全新平台任务），不是自动换路；不勾选/不可用/
    // 不 eligible 一律照旧平台路径
    if (!opts.platform && cosManual && cosUploadEligible(file)) {
      var cosRow = makeUploadRow(file);
      uploadFileCos(file, cosRow,
        opts.cosRetry ? { resumeJobId: opts.cosRetry, skipConfirm: true } : null);
      return;
    }
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
    if (importTargetState && importTargetState.pid) {
      formData.append("target_project_id", importTargetState.pid);
    }
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
        if (data && data.conversion_job_id && data.state && data.state !== "ready") {
          pollConversionJob(data, row);
          return;
        }
        var openName = (data && data.state === "ready" && data.canonical_name)
          ? data.canonical_name : data.name;
        row.setStage("upload.stage.done");
        row.finish();
        toast(t("upload.done", { name: openName }), "success");
        loadAll();
        importAssociateUploaded(openName);   // W4：上传行自身的目标关联（若有）
        openSlide(openName);
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

  // =========================================================================
  // COS 直传（Phase 4；docs/cos-direct-upload-audit-plan.md §5/§10 Phase 4、
  // docs/upload-routing-open-source-review.md D3/D6/D7/D8/D10）
  // 授权决议 A-presign-parts：浏览器只持有绑定 Content-Length 的 UploadPart
  // 预签名 URL；Initiate/Complete/Abort 全在 worker。控制 API（创建/签名/
  // 完成/取消）走 apiFetch（登录会话 + CSRF）；COS PUT 必须走独立传输——
  // 裸 fetch + credentials:"omit"，绝不带平台 Cookie/CSRF（§5）。
  // 选路 manual_only（校准规则 3）：默认关，用户勾选后才对 eligible 文件
  // 走 COS，不按大小自动导流；任务开始后 transport 冻结（D8）——
  // uploadFileCos 内部不回退平台路径，超上限只给「改用平台上传」显式按钮
  //（用户明确选择的全新平台任务，不算自动换路）。
  // =========================================================================
  var COS_JOBS_KEY = "pt.cos.jobs";

  function resolveCosConfig() {
    // 仿 resolveUploadV2Threshold：唯一权威是 bootstrap.capabilities.cos_upload
    //（app.py _cos_upload_capability_payload）。capability off 时只下发
    // {available:false,...}——available 非 true 直接 null，前端不得因文件大
    // 而自行启用 COS（§5）。缺字段/结构非法一律 null：宁可不走 COS，也不拿
    // 坏参数拼请求（D3：十进制字节整数原样使用，前端不自算另一份上限）。
    try {
      var caps = window.HP_APP_BOOTSTRAP && window.HP_APP_BOOTSTRAP.capabilities;
      var c = caps && caps.cos_upload;
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

  var COS_UPLOAD_CONFIG = resolveCosConfig();

  // manual_only 手动开关状态：仅存内存——刷新回到默认平台路径，不留
  //「隐性开启」的自动导流状态（D6/D7：首期禁止按大小/繁忙自动切 COS）
  var cosManual = false;

  function cosUploadEligible(file) {
    // D10：capability/格式白名单/大小在任务开始前一次判定。
    // D11：首期 COS 只收原生单文件白名单；ZIP/MRXS 恒 V1——与
    // shouldChunkUpload 同一例外双保险，即使服务端白名单误配也不放行。
    if (!COS_UPLOAD_CONFIG) return false;
    if (!file || typeof file.size !== "number") return false;
    if (file.size <= 0 || file.size > COS_UPLOAD_CONFIG.max_size_bytes) return false;
    var name = file.name || "";
    var ext = name.slice(name.lastIndexOf(".") + 1).toLowerCase();
    if (ext === "zip" || ext === "mrxs") return false;
    return COS_UPLOAD_CONFIG.formats.indexOf(ext) >= 0;
  }

  // ---------- 刷新恢复（§5：只存非秘密 job id 与文件提示） ----------
  // 签名 URL 短 TTL 且属凭证，绝不落 localStorage；隐私模式写入可能抛错。
  function cosJobsRead() {
    try {
      var arr = JSON.parse(localStorage.getItem(COS_JOBS_KEY) || "[]");
      if (!Array.isArray(arr)) return [];
      return arr.filter(function (j) {
        return j && typeof j.job_id === "string" && j.job_id &&
          typeof j.filename === "string" && typeof j.size === "number" &&
          Array.isArray(j.confirmed);
      });
    } catch (e) {
      return [];
    }
  }

  function cosJobsWrite(jobs) {
    try { localStorage.setItem(COS_JOBS_KEY, JSON.stringify(jobs)); }
    catch (e) { /* localStorage 不可用（隐私模式）：仅失去刷新恢复 */ }
  }

  function cosJobSave(job) {
    if (!job || !job.job_id) return;
    var jobs = cosJobsRead().filter(function (j) { return j.job_id !== job.job_id; });
    jobs.push({
      job_id: job.job_id, filename: job.filename, size: job.size,
      confirmed: (job.confirmed || []).slice().sort(function (a, b) { return a - b; }),
    });
    cosJobsWrite(jobs);
  }

  function cosJobRemove(jobId) {
    if (!jobId) return;
    cosJobsWrite(cosJobsRead().filter(function (j) { return j.job_id !== jobId; }));
  }

  function cosFindResumableJob(file) {
    // 同名同大小才视为同一候选（§5：文件名/大小只是候选不是证明；真正的
    // 字节核验在 worker ListParts，浏览器侧 ETag 仅提示）
    var jobs = cosJobsRead();
    for (var i = 0; i < jobs.length; i++) {
      if (jobs[i].filename === (file && file.name) &&
          jobs[i].size === (file && file.size)) {
        return jobs[i];
      }
    }
    return null;
  }

  // ---------- COS PUT 独立传输（§5/Phase 4-2） ----------
  function cosPutPart(url, blob, abortCtl) {
    // 绝不走 apiFetch/xhrSend——它们会注入 X-CSRF-Token（对 COS 是污染头，
    // 还会触发不必要的 CORS 预检）。credentials:"omit" 显式不带平台 Cookie；
    // mode:"cors" 走 COS 暴露的响应头读 ETag（仅提示，§3.1）。Content-Length
    // 由浏览器按 body 自动设置（与签名绑定值一致），手动设置既多余又会被
    // CORS 拒绝，因此这里不设任何请求头。
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

  // ---------- 稳定机器码 → 可读文案（照 uploadErrorMessage 的兜底模式） ----------
  function cosErrorMessage(status, data) {
    var code = (data && (data.code || data.error)) || "";
    if (code === "cos_exceeds_admission") return tt("upload.cos.err.exceeds_admission");
    if (code === "cos_format_unsupported") return tt("upload.cos.err.format_unsupported");
    if (code === "cos_waiting_limit") return tt("upload.cos.err.waiting_limit");
    if (code === "ingestion_state_conflict") return tt("upload.cos.err.state");
    if (code === "cos_sign_rate_limited") return tt("upload.cos.err.rate");
    if (code === "cos_capacity_reconcile_required") return tt("upload.cos.err.reconcile");
    if (code === "name_unavailable") return tt("upload.err.name");
    if (code === "invalid_declared_size") return tt("upload.err.size_mismatch");
    if (code === "cos_unavailable") return tt("upload.cos.err.unavailable");
    if (status === 403) return tt("upload.err.csrf");
    // 未知码保留原文（排障需要机器码，不猜测语义）
    return code || status || tt("upload.stage.failed");
  }

  // 阶段名 → 文案键（§5 固定六段 + terminal/未知兜底）
  function cosStageKey(stage) {
    var known = { waiting_space: 1, uploading: 1, awaiting_server: 1,
                  downloading: 1, validating: 1, readiness: 1, viewable: 1 };
    return known[stage] ? "upload.cos.stage." + stage : "upload.stage.failed";
  }

  function cosShowStage(row, b, noteOverride) {
    // 阶段文案唯一入口：上传阶段百分比由分块确认驱动（调用方另设），
    // 下载进度用服务端持久 checkpoint（downloaded_bytes/declared_size）——
    // 上传 100% ≠ 可查看，绝不合成全流程百分比（§5）
    var frac = null;
    if (b && b.stage === "downloading" && typeof b.downloaded_bytes === "number" &&
        typeof b.declared_size === "number" && b.declared_size > 0) {
      frac = Math.min(b.downloaded_bytes / b.declared_size, 1);
    }
    var note = noteOverride || "";
    if (!note && b && b.stage === "waiting_space" &&
        typeof b.queue_position === "number") {
      // 排队位置 0 基 → 人类序数；绝不显示预计时间（§6.1 不承诺 ETA）
      note = tt("upload.cos.queue", { n: b.queue_position + 1 });
    }
    row.setStage(cosStageKey(b && b.stage), frac, note);
  }

  // COS 行操作按钮（取消/重试/改用平台上传）：现有上传行无按钮先例，
  // 借 upload-item 的紧凑文本习惯用行内样式兜底（不改样式表）
  function addRowButton(row, label, onClick) {
    if (!row || !row._row || !row._row.appendChild) return null;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "upload-item-btn";
    btn.textContent = label;
    btn.style.marginTop = "4px";
    btn.style.fontSize = "11px";
    btn.style.padding = "2px 8px";
    btn.style.cursor = "pointer";
    btn.addEventListener("click", function (e) {
      if (e && e.preventDefault) e.preventDefault();
      onClick(e);
    });
    row._row.appendChild(btn);
    return btn;
  }

  // 服务端冻结计划 {part_number,length} → 前端切片表：编号排序后按顺序
  // 累加推导 offset（worker 按同一顺序初始化，编号连续；计划不含 offset）
  function cosBuildPlan(parts) {
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

  function uploadFileCos(file, row, opts) {
    opts = opts || {};
    var cfg = COS_UPLOAD_CONFIG;   // 选路时已判可用（D8：进入即冻结为 COS）
    var jobId = opts.resumeJobId || null;
    var confirmedMap = {};         // part_number -> ETag|""（ETag 仅提示，§3.1）
    var plan = null;               // [{part_number, offset, length}]
    var totalConfirmed = 0;
    var abortCtl = (typeof AbortController === "function") ? new AbortController() : null;
    var stopped = false;           // 用户取消/行终结后停一切后续动作
    var timerHandle = null;

    // —— 取消（§4 cancel 幂等）：停轮询 + abort 在途 PUT + POST cancel ——
    addRowButton(row, tt("upload.cos.cancel"), function () {
      if (stopped) return;
      stopped = true;
      if (timerHandle) { clearTimeout(timerHandle); timerHandle = null; }
      if (abortCtl) { try { abortCtl.abort(); } catch (e) {} }
      cosJobRemove(jobId);
      row.setStage("upload.cos.cancelled");
      row.finish(10000);
      if (jobId) {
        // 网络失败也照常停 UI：服务端等待超时/容量调度器会兜底清理
        apiFetch("/api/ingestions/" + encodeURIComponent(jobId) + "/cancel",
                 { method: "POST" }).catch(function () {});
      }
    });

    function delay(ms) {
      // 所有等待统一走可 clearTimeout 的定时器：取消后不再推进状态机
      return new Promise(function (resolve) {
        timerHandle = setTimeout(function () { timerHandle = null; resolve(); }, ms);
      });
    }

    function confirmedList() {
      var out = [];
      for (var k in confirmedMap) {
        if (confirmedMap.hasOwnProperty(k)) out.push(parseInt(k, 10));
      }
      return out;
    }

    function confirmPart(n, etag) {
      if (confirmedMap.hasOwnProperty(n)) return;
      confirmedMap[n] = etag || "";
      totalConfirmed++;
      // 进度 = 已确认分块/总块数：只代表上传阶段（§5），重试不重复计数
      if (plan && plan.length) {
        row.setStage("upload.cos.stage.uploading", totalConfirmed / plan.length);
      }
      cosJobSave({ job_id: jobId, filename: file.name, size: file.size,
                   confirmed: confirmedList() });
    }

    function loadConfirmedFromStorage() {
      // 续传起点以本地记录为准（编号即已确认；worker ListParts 才是权威，
      // 多传的分块只是同编号覆盖，绑定长度保证不越界——resume 语义）
      var jobs = cosJobsRead().filter(function (j) { return j.job_id === jobId; });
      var saved = jobs.length ? jobs[0] : null;
      (saved && saved.confirmed || []).forEach(function (n) {
        if (!confirmedMap.hasOwnProperty(n)) {
          confirmedMap[n] = "";
          totalConfirmed++;
        }
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
        var st = b.stage;
        if (st === "waiting_space") {
          cosShowStage(row, b);
          return delay(5000).then(drive);   // 等待期间可取消（行上按钮）
        }
        if (st === "uploading") {
          if (b.parts && b.parts.length) {
            plan = cosBuildPlan(b.parts);
            return uploadPendingParts();
          }
          // preparing：worker 尚未初始化 multipart（无分块计划）→ 短间隔再查
          row.setStage("upload.cos.stage.uploading", 0);
          return delay(2000).then(drive);
        }
        if (st === "awaiting_server" || st === "downloading" ||
            st === "validating" || st === "readiness" || st === "viewable") {
          if (st === "viewable") return succeed(b);
          cosShowStage(row, b);
          return delay(2000).then(drive);
        }
        if (st === "terminal") {
          cosJobRemove(jobId);   // 等待超时/过期/失败：终态任务不再恢复
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
        return cosPutPart(url, file.slice(item.part.offset,
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
      row.setStage("upload.cos.stage.uploading",
        totalConfirmed / plan.length);
      return nextBatch();
    }

    var completePosts = 0;   // upload-complete 回放计数（限速热循环）

    function requestComplete() {
      // 全部 confirmed → 幂等记录「浏览器侧完成」；409 状态冲突视为已完成过
      //（服务端状态是唯一权威，直接转入阶段轮询）。重复回放（complete 后
      // 状态仍停在 uploading，如 worker 尚未处理完成请求）做限速重放，避免
      // 无延时的热循环打爆控制 API
      function send() {
        completePosts++;
        return apiFetch("/api/ingestions/" + encodeURIComponent(jobId) +
                        "/upload-complete", { method: "POST" })
          .then(jsonBody)
          .then(function (res) {
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
      // viewable：照 V2 commit 后的跳转习惯（完成 → 刷新列表 → 关联 → 打开）
      cosJobRemove(jobId);
      var name = b.slide || file.name;
      row.setStage("upload.stage.done");
      row.finish();
      toast(t("upload.done", { name: name }), "success");
      loadAll();
      importAssociateUploaded(name);
      openSlide(name);
    }

    Promise.resolve().then(function () {
      if (jobId) return;   // 显式续传（重试按钮）：跳过询问直接进状态机
      // 同名同大小未完任务 → 询问后续传（§5）；用户拒绝 = 换新任务语义，
      // 按 D8 先取消旧任务再全新创建（不双占、不静默复用）
      var prev = cosFindResumableJob(file);
      if (!prev) return;
      var doResume = opts.skipConfirm ||
        window.confirm(tt("upload.cos.resume_confirm", { name: file.name }));
      if (doResume) {
        jobId = prev.job_id;
        return;
      }
      var cancelPrev = apiFetch(
        "/api/ingestions/" + encodeURIComponent(prev.job_id) + "/cancel",
        { method: "POST" });
      return cancelPrev.then(function () {
        cosJobRemove(prev.job_id);
      }, function () {
        cosJobRemove(prev.job_id);
      });
    }).then(function () {
      if (jobId) { loadConfirmedFromStorage(); return; }
      return apiFetch("/api/ingestions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, declared_size: file.size }),
      }).then(jsonBody).then(function (res) {
        if (res.ok && res.body && res.body.job_id) {
          jobId = res.body.job_id;
          cosJobSave({ job_id: jobId, filename: file.name, size: file.size,
                       confirmed: [] });
          // 创建响应自带初始阶段（waiting_capacity/preparing）：先照实展示
          if (res.body.stage) cosShowStage(row, res.body);
          return;
        }
        throw { status: res.status, data: res.body };   // 422/409 → 稳定码映射
      });
    }).then(function () {
      return drive();
    }).catch(function (err) {
      if (stopped || (err && err.cancelled)) return;
      var code = err && err.data && (err.data.code || err.data.error);
      var msg;
      if (err && err.terminal) {
        msg = cosErrorMessage(0, { code: (err.data && err.data.fail_code) || "" });
      } else if (err instanceof TypeError || (err && err.network)) {
        // 网络层失败：任务与已确认分块保留，重选同名文件可续传（§5）
        msg = tt("upload.cos.resume_hint");
      } else {
        msg = cosErrorMessage(err && err.status, err && err.data);
      }
      row.markError();
      row.setStage("upload.stage.failed");
      if (code === "cos_exceeds_admission") {
        // §6.1：说明超限 + 显式「改用平台上传」（用户明确选择，非自动换路；
        // 平台路径仍受 V2/legacy 自身校验约束，不承诺必然接收）
        addRowButton(row, tt("upload.cos.retry_platform"), function () {
          uploadFile(file, { platform: true });
        });
      } else if (err && typeof err.part === "number") {
        // 分块最终失败：从 confirmed 续传（服务端计划仍在，跳过已确认块）
        addRowButton(row, tt("upload.cos.retry"), function () {
          uploadFile(file, { cosRetry: jobId });
        });
      }
      row.finish(10000);
      toast(t("upload.fail", { e: msg }), "error");
    });
  }

  // ---------- 启动：手动开关渲染 + 未完任务只读恢复 ----------
  function initCosUploadUi() {
    // capability 可用才渲染开关（§5：off 时前端零 COS 痕迹，不因文件大
    // 走 COS）。开关放在上传进度行容器正上方（侧栏上传入口旁），沿用
    // upload-item 的 11px 紧凑文字习惯——不改模板与样式表。
    restoreCosJobs();
    if (!COS_UPLOAD_CONFIG) return;
    var host = els.uploadProgressList;
    if (!host || !host.parentNode ||
        typeof host.parentNode.insertBefore !== "function") return;
    var label = document.createElement("label");
    label.className = "cos-manual-toggle";
    label.title = tt("upload.cos.toggle.tip");
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = cosManual;
    var span = document.createElement("span");
    span.textContent = tt("upload.cos.toggle");
    label.style.display = "flex";
    label.style.alignItems = "center";
    label.style.gap = "6px";
    label.style.margin = "6px 0 0";
    label.style.fontSize = "11px";
    label.style.cursor = "pointer";
    cb.addEventListener("change", function () { cosManual = !!cb.checked; });
    label.appendChild(cb);
    label.appendChild(span);
    host.parentNode.insertBefore(label, host);
  }

  function restoreCosJobs() {
    // 刷新恢复（§5）：未终态任务显示只读进度行继续轮询（浏览器没有文件
    // 句柄，不能替用户续传分块）；uploading 未完成 → 提示重选同名文件续传。
    // 查询失败保留记录，下次启动再试；终态/已可查看/查无此任务即清理。
    cosJobsRead().forEach(function (j) {
      pollRestoredRow(j);
    });
  }

  function pollRestoredRow(job) {
    var done = false;
    var row = null;
    function ensureRow() {
      if (!row) row = makeUploadRow({ name: job.filename, size: job.size });
      return row;
    }
    function tick() {
      apiFetch("/api/ingestions/" + encodeURIComponent(job.job_id))
        .then(jsonBody)
        .then(function (res) {
          if (done) return;
          var b = res.ok ? res.body : null;
          var st = b && b.stage;
          if (!st) { setTimeout(tick, 5000); return; }
          if (st === "viewable") {
            done = true;
            cosJobRemove(job.job_id);
            var r0 = ensureRow();
            r0.setStage("upload.stage.done");
            r0.finish();
            toast(t("upload.done", { name: b.slide || job.filename }), "success");
            loadAll();
            return;
          }
          if (st === "terminal") {
            done = true;
            cosJobRemove(job.job_id);
            var r1 = ensureRow();
            r1.markError();
            r1.setStage("upload.stage.failed");
            r1.finish(10000);
            return;
          }
          var note = (st === "uploading") ? tt("upload.cos.resume_hint") : "";
          cosShowStage(ensureRow(), b, note);
          setTimeout(tick, 3000);
        })
        .catch(function () {
          if (!done) setTimeout(tick, 5000);
        });
    }
    tick();
  }
  // 供测试（tests/js/*.test.ts loadApp harness）驱动真实上传路径；
  // 与 HP_AUTH 同风格的命名空间导出，不进业务调用面
  window.HP_UPLOAD = {
    uploadFile: uploadFile,
    uploadFileV2: uploadFileV2,
    shouldChunkUpload: shouldChunkUpload,
    UPLOAD_V2_THRESHOLD: UPLOAD_V2_THRESHOLD,
    // W4/R5：转换轮询（观察超时不判失败；401/403/404 分级处理）
    pollConversionJob: pollConversionJob,
    // COS 直传 Phase 4（测试入口：真实路由/状态机/独立传输的驱动面）
    cosUploadEligible: cosUploadEligible,
    uploadFileCos: uploadFileCos,
    resolveCosConfig: resolveCosConfig,
    cosStageKey: cosStageKey,
    setCosManual: function (on) { cosManual = !!on; },
    isCosManual: function () { return cosManual; },
    initCosUploadUi: initCosUploadUi,
    restoreCosJobs: restoreCosJobs,
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

  // =========================================================================
  // W4/W6（2026-09-14）：统一「导入切片」抽屉 —— 本地文件 / 百度分享两页签、
  // 格式目录（GET /api/slide-formats）、「申请新格式支持」次级入口与
  // 后台任务列表（GET /api/conversions?group=open|recent）。
  // 本地上传仍走既有 legacy/V2 管线（不重写）；抽屉只负责入口、目标位置
  // 与任务可视化。百度页签按能力探测结果降级：不可枚举 → 显示可行动原因；
  // 可枚举不可导入 → 允许读列表、禁用导入。分享文本/提取码只进请求体，
  // 绝不写日志或 console。
  // =========================================================================
  var importDrawerState = {
    open: false,
    tab: "local",            // "local" | "baidu"
    formatsLoaded: false,
    formatCatalog: [],
    maxSampleBytes: 64 * 1024 * 1024,   // /api/slide-formats 下发后覆盖
    taskTimer: null,
    taskPollStopped: false,  // 401/403 后停止（权限失效，重试无意义）
    lastFocusEl: null,
  };

  // 本地页签目标位置（抽屉会话内保留）："" = 未归类；"<pid>" = 已有项目；
  // "new" = 新项目（首个上传成功时懒创建一次，幂等键固定，后续复用 pid）。
  var importTargetState = {
    pid: "",
    newProjectName: "",
    createdPid: "",
    createKey: null,
  };
  // 可观测镜像（W4 spec 命名）：上传成功后据此 POST /api/project/<pid>/slides
  window.__importTargetPid = "";

  function jsonBody(r) {
    return r.json().then(function (b) { return { ok: r.ok, status: r.status, body: b }; },
                           function () { return { ok: r.ok, status: r.status, body: null }; });
  }

  // ---------- 本地页签：目标位置 ----------
  function impOptionEl(value, label) {
    var o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    return o;
  }

  function renderImportTargetSelects() {
    var projects = allProjects || [];
    if (els.importTargetSelect) {
      var keep = els.importTargetSelect.value;
      els.importTargetSelect.innerHTML = "";
      els.importTargetSelect.appendChild(impOptionEl("", t("imp.target.unfiled")));
      projects.forEach(function (p) {
        els.importTargetSelect.appendChild(impOptionEl(p.pid, p.name || p.pid));
      });
      els.importTargetSelect.appendChild(impOptionEl("new", t("imp.target.new")));
      els.importTargetSelect.value = keep || "";
    }
    if (els.baiduTargetSelect) {
      var keepB = els.baiduTargetSelect.value;
      els.baiduTargetSelect.innerHTML = "";
      els.baiduTargetSelect.appendChild(impOptionEl("", t("imp.target.unfiled")));
      projects.forEach(function (p) {
        els.baiduTargetSelect.appendChild(impOptionEl(p.pid, p.name || p.pid));
      });
      els.baiduTargetSelect.value = keepB || "";
    }
  }

  function syncImportTargetFromSelect() {
    if (!els.importTargetSelect) return;
    var v = els.importTargetSelect.value;
    if (els.importTargetNewName) els.importTargetNewName.hidden = v !== "new";
    if (v === "new") {
      importTargetState.pid = "";
      importTargetState.newProjectName = (els.importTargetNewName.value || "").trim();
    } else {
      importTargetState.pid = v || "";
      importTargetState.newProjectName = "";
    }
    window.__importTargetPid = importTargetState.pid;
  }

  // 上传成功后的目标关联（W4）：有目标才动作；目标被删/权限撤销 → 提示
  // 关联失败，产物保留在未归类（不静默改目标）。
  function importAssociateUploaded(slideName) {
    if (!slideName) return Promise.resolve();
    var st = importTargetState;
    if (!st.pid && !st.newProjectName) return Promise.resolve();
    var ensureProject;
    if (st.pid) {
      ensureProject = Promise.resolve(st.pid);
    } else if (st.createdPid) {
      ensureProject = Promise.resolve(st.createdPid);
    } else {
      if (!st.createKey) st.createKey = uuid();
      var projectName = st.newProjectName;
      ensureProject = apiFetch("/api/project/create", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": st.createKey },
        body: JSON.stringify({ name: projectName, note: "", slides: [] }),
      }).then(jsonBody).then(function (res) {
        if (!res.ok || !res.body || !res.body.pid) {
          throw new Error((res.body && res.body.error) || t("newproj.create.fail"));
        }
        st.createdPid = res.body.pid;
        return st.createdPid;
      });
    }
    return ensureProject.then(function (pid) {
      return apiFetch("/api/project/" + encodeURIComponent(pid) + "/slides", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slides: [slideName] }),
      }).then(jsonBody).then(function (res) {
        if (!res.ok) throw new Error((res.body && res.body.error) || t("imp.assoc.fail"));
        toast(t("imp.assoc.ok", { name: truncateMiddle(slideName, 24) }), "success");
        return reloadProjectsAndUnfiled();
      });
    }).catch(function (e) {
      toast(t("imp.assoc.fail2", { e: (e && e.message) ? e.message : e }), "error");
    });
  }

  // ---------- 格式目录（GET /api/slide-formats，打开抽屉时拉一次） ----------
  // Wave 3（普通图片兼容）：目录是格式词表的唯一权威（§3），raster-image 行
  // 与后端 slide_format_registry._CATALOG_DISPLAY 同文；一致性由
  // tests/js/raster-image-compat.test.ts 对后端源码做契约校验，防漂移。
  var FORMAT_CATALOG_FALLBACK = [
    { display_name: "SVS / TIFF / BigTIFF / OME-TIFF / NDPI / VMS / VMU / SCN / BIF / SVSlide",
      extensions: [".svs", ".tif", ".tiff", ".ome.tif", ".ome.tiff", ".ndpi", ".vms", ".vmu", ".scn", ".bif", ".svslide"],
      import_mode: "direct", limits: [] },
    { id: "raster-image", display_name: "普通图片（BMP / JPEG）",
      extensions: [".bmp", ".jpg", ".jpeg"], import_mode: "direct",
      limits: ["普通图片、支持像素坐标、无物理标尺"] },
    { display_name: "KFB / KFBF", extensions: [".kfb", ".kfbf"],
      import_mode: "convert",
      limits: ["KFB 上传后后台转换为 BigTIFF（明场）；KFBF 转换为多通道 OME-TIFF（荧光）"] },
    { display_name: "MRXS", extensions: [".mrxs"], import_mode: "bundle",
      limits: ["需要完整包（主文件 + 同名伴随目录），请打包 zip 上传"] },
  ];

  // 文件选择器 accept 的静态 fallback：= acceptFromCatalog(FORMAT_CATALOG_FALLBACK)
  // 的展开结果（单一权威链：后端目录 → fallback 目录 → accept → 模板属性）。
  // 契约（两处断言）由 raster-image-compat.test.ts 锁定；接口可用时该串会被
  // 同一函数对线上目录的派生结果覆盖。
  var FILE_INPUT_ACCEPT_FALLBACK = ".svs,.tif,.tiff,.ome.tif,.ome.tiff,.ndpi,.vms,.vmu,.scn,.bif,.svslide,.bmp,.jpg,.jpeg,.kfb,.kfbf,.mrxs,.zip";

  // 由格式目录派生文件选择器 accept（Wave 3）：
  //   - 只取 selectable_for_upload !== false 的条目扩展名（小写、去重、保序）；
  //   - .zip 恒定保留：它是 MRXS 完整包的运输容器，不属于任何目录条目扩展名；
  //   - 目录为空/形态异常时返回 null（调用方保留静态 fallback，不缩窄能力）。
  function acceptFromCatalog(items) {
    if (!Array.isArray(items) || items.length === 0) return null;
    var seen = {};
    var parts = [];
    items.forEach(function (f) {
      if (!f || f.selectable_for_upload === false) return;
      (f.extensions || []).forEach(function (ext) {
        var e = String(ext || "").trim().toLowerCase();
        if (!e || e.charAt(0) !== "." || seen[e]) return;
        seen[e] = true;
        parts.push(e);
      });
    });
    if (!parts.length) return null;
    if (!seen[".zip"]) { seen[".zip"] = true; parts.push(".zip"); }
    return parts.join(",");
  }

  // 目录拉取成功后把派生 accept 写回 #file-input（静态属性作为兜底先行存在）
  function applyFileInputAccept(items) {
    if (!els.fileInput) return;
    var a = acceptFromCatalog(items);
    if (a) els.fileInput.accept = a;
  }

  function formatModeLabel(mode) {
    if (mode === "convert") return t("imp.formats.mode.convert");
    if (mode === "bundle") return t("imp.formats.mode.bundle");
    return t("imp.formats.mode.direct");
  }

  function renderFormatCatalog(items) {
    if (!els.importFormatCatalog) return;
    els.importFormatCatalog.innerHTML = "";
    (items || []).forEach(function (f) {
      var row = document.createElement("div");
      row.className = "imp-format-row";
      var head = document.createElement("div");
      head.className = "imp-format-head";
      var nm = document.createElement("span");
      nm.className = "imp-format-name";
      nm.textContent = (f.display_name || f.id || "") + "  " + (f.extensions || []).join(" / ");
      var badge = document.createElement("span");
      badge.className = "imp-format-badge mode-" + (f.import_mode || "direct");
      badge.textContent = formatModeLabel(f.import_mode);
      head.appendChild(nm);
      head.appendChild(badge);
      row.appendChild(head);
      (f.limits || []).forEach(function (lim) {
        var l = document.createElement("div");
        l.className = "imp-format-limit";
        l.textContent = lim;
        row.appendChild(l);
      });
      els.importFormatCatalog.appendChild(row);
    });
  }

  function loadImportFormatCatalog() {
    if (!els.importFormatCatalog) return Promise.resolve();
    if (importDrawerState.formatsLoaded) return Promise.resolve();
    els.importFormatCatalog.textContent = t("imp.formats.loading");
    return apiFetch("/api/slide-formats").then(jsonBody).then(function (res) {
      var body = res.body;
      var items = body && (body.formats || body.items);
      if (!Array.isArray(items) && Array.isArray(body)) items = body;
      if (!res.ok || !Array.isArray(items)) {
        renderFormatCatalog(FORMAT_CATALOG_FALLBACK);   // 后端暂缺：静态产品文案兜底
        return;
      }
      importDrawerState.formatCatalog = items;
      var maxSample = Number(body && body.max_sample_bytes);
      if (maxSample > 0) importDrawerState.maxSampleBytes = maxSample;
      importDrawerState.formatsLoaded = true;
      renderFormatCatalog(items);
      // Wave 3：文件选择器 accept 由目录派生（selectable_for_upload 条目并集 +
      // .zip），目录不可达时保持模板静态 fallback（含 .bmp/.jpg/.jpeg）
      applyFileInputAccept(items);
      renderFrSampleMax();
    }).catch(function () {
      renderFormatCatalog(FORMAT_CATALOG_FALLBACK);
    });
  }

  function renderFrSampleMax() {
    if (!els.frSampleMax) return;
    els.frSampleMax.textContent = t("fr.sample.max", {
      size: fmtSize(importDrawerState.maxSampleBytes),
    });
  }

  // ---------- 申请新格式支持（次级入口；202 = 已登记，等待评估） ----------
  function frBusinessStatusLabel(status) {
    if (status === "reviewing") return t("fr.status.reviewing");
    if (status === "supported") return t("fr.status.supported");
    if (status === "declined") return t("fr.status.declined");
    return t("fr.status.submitted");   // submitted / 未知一律「已登记，等待评估」
  }

  function renderFormatRequestResult(rec) {
    if (!els.frResult) return;
    els.frResult.innerHTML = "";
    els.frResult.hidden = false;
    var line = document.createElement("span");
    line.textContent = t("fr.ok.registered", { id: (rec && rec.request_id) || (rec && rec.id) || "" }) +
      " · " + frBusinessStatusLabel(rec && rec.business_status);
    els.frResult.appendChild(line);
    var refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "link-btn";
    refresh.textContent = t("fr.refresh");
    refresh.addEventListener("click", function () {
      var rid = rec && (rec.request_id || rec.id);
      if (!rid) return;
      apiFetch("/api/format-requests/" + encodeURIComponent(rid))
        .then(jsonBody)
        .then(function (res) {
          if (res.ok && res.body) {
            renderFormatRequestResult(res.body);
            loadRecentFormatRequests();
          } else {
            toast(t("fr.refresh.fail"), "error");
          }
        })
        .catch(function () { toast(t("fr.refresh.fail"), "error"); });
    });
    els.frResult.appendChild(refresh);
  }

  function loadRecentFormatRequests() {
    if (!els.frRecent) return;
    apiFetch("/api/format-requests").then(jsonBody).then(function (res) {
      if (!res.ok || !res.body || !Array.isArray(res.body.items)) {
        els.frRecent.innerHTML = "";   // 接口暂缺/失败：静默（次级入口不打扰）
        return;
      }
      els.frRecent.innerHTML = "";
      if (!res.body.items.length) return;
      var title = document.createElement("div");
      title.className = "fr-recent-title";
      title.textContent = t("fr.recent.title");
      els.frRecent.appendChild(title);
      res.body.items.slice(0, 5).forEach(function (it) {
        var row = document.createElement("div");
        row.className = "fr-recent-row";
        row.textContent = (it.format_ext || "") + " · " + frBusinessStatusLabel(it.business_status);
        els.frRecent.appendChild(row);
      });
    }).catch(function () { /* 静默 */ });
  }

  function submitFormatRequest() {
    if (!els.frExt || !els.frSubmit) return;
    var ext = (els.frExt.value || "").trim();
    if (!ext) { toast(t("fr.need.ext"), "error"); return; }
    // 前端预检样本大小（服务端仍权威校验）
    if (els.frSample && els.frSample.files && els.frSample.files[0] &&
        els.frSample.files[0].size > importDrawerState.maxSampleBytes) {
      toast(t("fr.sample.too.large", {
        size: fmtSize(importDrawerState.maxSampleBytes),
      }), "error");
      return;
    }
    var fd = new FormData();
    fd.append("format_ext", ext);
    fd.append("message", (els.frMessage.value || "").trim());
    fd.append("contact", (els.frContact.value || "").trim());
    if (els.frSample.files && els.frSample.files[0]) {
      fd.append("sample", els.frSample.files[0]);
    }
    els.frSubmit.disabled = true;
    apiFetch("/api/format-requests", { method: "POST", body: fd })
      .then(jsonBody)
      .then(function (res) {
        if (res.status === 202) {
          // 202 只表示登记成功：展示申请编号 + 业务状态（不说「已发送/已兼容」）
          renderFormatRequestResult(res.body);
          els.frExt.value = ""; els.frMessage.value = "";
          els.frContact.value = ""; els.frSample.value = "";
          loadRecentFormatRequests();
        } else {
          // 413/429/其他失败：保留草稿（输入不清空），就地提示
          toast((res.body && res.body.error) || t("fr.fail"), "error");
        }
      })
      .catch(function () { toast(t("fr.fail"), "error"); })
      .finally(function () { els.frSubmit.disabled = false; });
  }

  // ---------- 后台任务列表（GET /api/conversions?group=…；轮询） ----------
  function conversionStateLabel(state) {
    switch (state) {
      case "queued": return t("imp.task.state.queued");
      case "converting": case "validating": return t("imp.task.state.running");
      case "ready": return t("imp.task.state.ready");
      case "failed": return t("imp.task.state.failed");
      case "cancelled": return t("imp.task.state.cancelled");
      default: return state || "";
    }
  }

  function refreshImportTasks() {
    if (!els.importTaskList || !importDrawerState.open) return;
    apiFetch("/api/conversions?group=open").then(jsonBody)
      .then(function (openRes) {
        if (openRes.status === 401 || openRes.status === 403) {
          importDrawerState.taskPollStopped = true;
          els.importTaskList.textContent = t("imp.tasks.auth.stop");
          return null;
        }
        // recent 合并展示（open 优先去重；失败不影响 open 列表）
        return apiFetch("/api/conversions?group=recent").then(jsonBody)
          .then(function (recentRes) { return { openRes: openRes, recentRes: recentRes }; });
      })
      .then(function (res) {
        if (!res) return;
        var seen = {};
        var items = [];
        ((res.openRes.body && res.openRes.body.items) || []).forEach(function (j) {
          if (!seen[j.conversion_job_id]) { seen[j.conversion_job_id] = true; items.push(j); }
        });
        ((res.recentRes.body && res.recentRes.body.items) || []).forEach(function (j) {
          if (!seen[j.conversion_job_id]) { seen[j.conversion_job_id] = true; items.push(j); }
        });
        renderImportTasks(items);
      })
      .catch(function () {
        // 网络故障：明确「暂时无法获取进度」，下次轮询继续（不判失败）
        if (els.importTaskList) els.importTaskList.textContent = t("imp.tasks.net.err");
      });
  }

  function renderImportTasks(items) {
    if (!els.importTaskList) return;
    els.importTaskList.innerHTML = "";
    if (!items || !items.length) {
      els.importTaskList.textContent = t("imp.tasks.empty");
      return;
    }
    items.slice(0, 20).forEach(function (job) {
      var row = document.createElement("div");
      row.className = "imp-task-row state-" + (job.state || "");
      var mid = document.createElement("div");
      mid.className = "imp-task-mid";
      var nm = document.createElement("div");
      nm.className = "imp-task-name";
      nm.textContent = truncateMiddle(job.source_name || job.canonical_name || job.conversion_job_id, 36);
      var st = document.createElement("div");
      st.className = "imp-task-state";
      st.textContent = conversionStateLabel(job.state) +
        (job.error_code ? " · " + job.error_code : "");
      mid.appendChild(nm);
      mid.appendChild(st);
      row.appendChild(mid);
      if (job.state === "ready") {
        var openBtn = document.createElement("button");
        openBtn.type = "button";
        openBtn.className = "btn secondary small";
        openBtn.textContent = t("imp.task.open");
        openBtn.addEventListener("click", function () {
          // 用户显式点击才打开（持久列表绝不自动抢占正在看的切片）
          var n = job.canonical_name || job.source_name;
          if (n) openSlide(n);
        });
        row.appendChild(openBtn);
      } else if (job.state === "failed" || job.state === "cancelled") {
        var retryBtn = document.createElement("button");
        retryBtn.type = "button";
        retryBtn.className = "btn secondary small";
        retryBtn.textContent = t("imp.task.retry");
        retryBtn.addEventListener("click", function () {
          retryBtn.disabled = true;
          apiFetch("/api/conversions/" + encodeURIComponent(job.conversion_job_id) + "/retry",
                   { method: "POST" })
            .then(jsonBody)
            .then(function (res) {
              if (!res.ok) {
                toast((res.body && res.body.error) || t("imp.task.retry.fail"), "error");
                retryBtn.disabled = false;
                return;
              }
              toast(t("imp.task.retry.queued"), "success");
              refreshImportTasks();
            })
            .catch(function () { toast(t("imp.task.retry.fail"), "error"); retryBtn.disabled = false; });
        });
        row.appendChild(retryBtn);
      }
      els.importTaskList.appendChild(row);
    });
  }

  function startImportTaskPolling() {
    stopImportTaskPolling();
    importDrawerState.taskPollStopped = false;
    importDrawerState.taskTimer = setInterval(function () {
      if (!importDrawerState.taskPollStopped) refreshImportTasks();
    }, 5000);
  }

  function stopImportTaskPolling() {
    if (importDrawerState.taskTimer) {
      clearInterval(importDrawerState.taskTimer);
      importDrawerState.taskTimer = null;
    }
  }

  // ---------- 百度分享页签 ----------
  var baiduState = {
    capsChecked: false,
    enumerationAvailable: false,
    importAvailable: false,
    workerEnabled: true,
    reasonCode: "",
    enumId: null,
    enumState: null,
    enumComplete: false,
    enumErrorCode: "",
    enumTimer: null,
    candidates: [],      // 当前页条目
    cursor: null,        // 当前页游标（null = 第一页）
    nextCursor: null,
    prevStack: [],
    selected: {},        // candidate id → candidate（跨页保留）
    importId: null,
    importTimer: null,
    importIdemKey: null,
  };

  function baiduCapReasonText(code) {
    switch (code) {
      case "enumeration_disabled": return t("bd.cap.reason.enumeration_disabled");
      case "connector_missing": return t("bd.cap.reason.connector_missing");
      case "secret_unconfigured": return t("bd.cap.reason.secret_unconfigured");
      case "import_disabled": return t("bd.cap.reason.import_disabled");
      default: return t("bd.cap.reason.unknown");
    }
  }

  function setBaiduListEnabled(enabled) {
    if (els.baiduListBtn) els.baiduListBtn.disabled = !enabled;
  }

  function setBaiduImportEnabled(enabled, reason) {
    if (!els.baiduImportBtn) return;
    els.baiduImportBtn.disabled = !enabled;
    els.baiduImportBtn.title = enabled ? "" : (reason || "");
  }

  function renderBaiduCapabilities() {
    if (!els.baiduCapStatus) return;
    if (!baiduState.enumerationAvailable) {
      // 页签仍可见：给出可行动原因（不呈现无响应按钮）
      els.baiduCapStatus.textContent = t("bd.cap.unavailable", {
        reason: baiduCapReasonText(baiduState.reasonCode),
      });
      els.baiduCapStatus.hidden = false;
      setBaiduListEnabled(false);
      setBaiduImportEnabled(false, baiduCapReasonText(baiduState.reasonCode));
      if (els.baiduInputBlock) els.baiduInputBlock.hidden = true;
      if (els.baiduCandidatesBlock) els.baiduCandidatesBlock.hidden = true;
      return;
    }
    els.baiduCapStatus.textContent = baiduState.importAvailable
      ? t("bd.cap.ready")
      : t("bd.cap.enum.only");
    // 可枚举但部署未拉起 worker：提交后会一直排队。只提示、不藏表单
    // （worker 可能在另一进程/容器；是否开启属部署配置问题）
    if (!baiduState.workerEnabled) {
      els.baiduCapStatus.textContent += " · " + t("bd.cap.worker.off");
    }
    els.baiduCapStatus.hidden = false;
    setBaiduListEnabled(true);
    if (els.baiduInputBlock) els.baiduInputBlock.hidden = false;
    // 可枚举但不可导入：允许读列表，禁用导入
    setBaiduImportEnabled(false, t("bd.cap.reason.import_disabled"));
  }

  function refreshBaiduCapabilities() {
    if (!els.baiduCapStatus) return Promise.resolve();
    return apiFetch("/api/remote-imports/baidu/capabilities").then(jsonBody).then(function (res) {
      baiduState.capsChecked = true;
      if (!res.ok || !res.body) {
        // 503 connector_unavailable 等：按不可用处理（带原因）
        baiduState.enumerationAvailable = false;
        baiduState.importAvailable = false;
        baiduState.reasonCode = (res.body && res.body.code) || "";
        renderBaiduCapabilities();
        return;
      }
      baiduState.enumerationAvailable = !!res.body.enumeration_available;
      baiduState.importAvailable = !!res.body.import_available;
      baiduState.workerEnabled = !!res.body.worker_enabled;
      baiduState.reasonCode = res.body.reason_code || "";
      renderBaiduCapabilities();
    }).catch(function () {
      baiduState.capsChecked = true;
      baiduState.enumerationAvailable = false;
      baiduState.importAvailable = false;
      baiduState.reasonCode = "";
      renderBaiduCapabilities();
    });
  }

  function setBaiduEnumStatus(msg) {
    if (!els.baiduEnumStatus) return;
    els.baiduEnumStatus.textContent = msg || "";
    els.baiduEnumStatus.hidden = !msg;
  }

  function startBaiduEnumeration() {
    if (!els.baiduShareText || !baiduState.enumerationAvailable) return;
    var shareText = (els.baiduShareText.value || "").trim();
    if (!shareText) { toast(t("bd.share.need"), "error"); return; }
    var code = (els.baiduExtractCode && els.baiduExtractCode.value || "").trim();
    var payload = { share_text: shareText };
    if (code) payload.extraction_code = code;   // 提取码只进请求体，不写日志
    setBaiduListEnabled(false);
    setBaiduEnumStatus(t("bd.enum.starting"));
    apiFetch("/api/remote-imports/baidu/enumerations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(jsonBody).then(function (res) {
      if (res.status !== 202 || !res.body || !res.body.id) {
        setBaiduListEnabled(true);
        setBaiduEnumStatus(t("bd.enum.create.fail", {
          e: (res.body && (res.body.error || res.body.code)) || ("HTTP " + res.status),
        }));
        return;
      }
      baiduState.enumId = res.body.id;
      baiduState.enumState = res.body.state || "queued";
      baiduState.selected = {};
      pollBaiduEnumeration();
    }).catch(function () {
      setBaiduListEnabled(true);
      setBaiduEnumStatus(t("bd.enum.create.fail", { e: t("imp.tasks.net.err") }));
    });
  }

  function pollBaiduEnumeration() {
    if (!baiduState.enumId) return;
    if (baiduState.enumTimer) clearTimeout(baiduState.enumTimer);
    var tick = function () {
      apiFetch("/api/remote-imports/baidu/enumerations/" +
               encodeURIComponent(baiduState.enumId)).then(jsonBody).then(function (res) {
        if (!res.ok || !res.body) {
          setBaiduEnumStatus(t("bd.enum.poll.fail", {
            e: (res.body && res.body.error) || ("HTTP " + res.status),
          }));
          baiduState.enumTimer = setTimeout(tick, 5000);
          return;
        }
        var b = res.body;
        baiduState.enumState = b.state;
        if (b.state === "queued" || b.state === "enumerating") {
          // 排队 ≠ 已扫出 0 项：queued 用独立文案，enumerating 才报进度
          setBaiduEnumStatus(t(
            b.state === "queued" ? "bd.enum.queued" : "bd.enum.progress",
            { n: b.scanned_count || 0 }));
          baiduState.enumTimer = setTimeout(tick, 2000);
          return;
        }
        setBaiduListEnabled(true);
        if (b.state === "failed" || b.state === "expired") {
          baiduState.enumComplete = false;
          baiduState.enumErrorCode = b.error_code || b.state;
          setBaiduEnumStatus(t("bd.enum.failed", {
            code: baiduState.enumErrorCode,
          }));
          if (els.baiduCandidatesBlock) els.baiduCandidatesBlock.hidden = true;
          return;
        }
        // ready：complete 才可导入；不完整禁用导入并说明
        baiduState.enumComplete = !!b.complete;
        setBaiduEnumStatus(t("bd.enum.ready", {
          n: b.candidate_count != null ? b.candidate_count : (b.scanned_count || 0),
        }) + (b.complete ? "" : " · " + t("bd.enum.incomplete", {
          reason: b.incomplete_reason || b.error_code || "",
        })));
        if (els.baiduCandidatesBlock) els.baiduCandidatesBlock.hidden = false;
        baiduState.cursor = null;
        baiduState.nextCursor = null;
        baiduState.prevStack = [];
        loadBaiduCandidates();
        updateBaiduImportGate();
      }).catch(function () {
        setBaiduEnumStatus(t("bd.enum.poll.fail", { e: t("imp.tasks.net.err") }));
        baiduState.enumTimer = setTimeout(tick, 5000);
      });
    };
    tick();
  }

  // 大小展示：十进制字符串 → 可读单位（BigInt 全程，不经 Number）
  function formatDecBytes(dec) {
    try {
      var b = BigInt(dec == null ? 0 : dec);
      if (b < 1024n) return b.toString() + " B";
      var units = ["KB", "MB", "GB", "TB"];
      var u = -1n;
      var v = b;
      do {
        v /= 1024n;
        u += 1n;
      } while (v >= 1024n && u < BigInt(units.length - 1));
      // 一位小数（整数部分 + 千分位余数；不再回落 Number）
      var tenths = ((b * 10n) / (1024n ** (u + 1n))) % 10n;
      var whole = v.toString();
      return whole + "." + tenths.toString() + " " + units[Number(u)];
    } catch (e) {
      return String(dec == null ? "" : dec);
    }
  }

  function loadBaiduCandidates() {
    if (!baiduState.enumId || !els.baiduCandidateList) return Promise.resolve();
    var url = "/api/remote-imports/baidu/enumerations/" +
      encodeURIComponent(baiduState.enumId) + "/candidates?limit=50";
    if (baiduState.cursor) url += "&cursor=" + encodeURIComponent(baiduState.cursor);
    return apiFetch(url).then(jsonBody).then(function (res) {
      if (!res.ok || !res.body || !Array.isArray(res.body.items)) {
        toast((res.body && res.body.error) || t("bd.candidates.fail"), "error");
        return;
      }
      baiduState.candidates = res.body.items;
      baiduState.nextCursor = res.body.next_cursor || null;
      renderBaiduCandidates();
    }).catch(function () { toast(t("bd.candidates.fail"), "error"); });
  }

  function baiduCandidateReasonText(code) {
    if (!code) return "";
    if (code === "directory") return t("bd.cand.reason.directory");
    if (code === "bundle_incomplete") return t("bd.cand.reason.bundle_incomplete");
    if (code === "unsupported") return t("bd.cand.reason.unsupported");
    return code;
  }

  function renderBaiduCandidates() {
    if (!els.baiduCandidateList) return;
    var filter = (els.baiduSearch && els.baiduSearch.value || "").trim().toLowerCase();
    var items = baiduState.candidates.filter(function (c) {
      if (!filter) return true;
      return ((c.name || "") + " " + (c.relative_path || "")).toLowerCase().indexOf(filter) >= 0;
    });
    els.baiduCandidateList.innerHTML = "";
    if (!items.length) {
      var empty = document.createElement("div");
      empty.className = "imp-hint";
      empty.textContent = t("bd.candidates.empty");
      els.baiduCandidateList.appendChild(empty);
    }
    items.forEach(function (c) {
      var row = document.createElement("label");
      row.className = "baidu-cand-row" + (c.selectable ? "" : " not-selectable");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = c.id;
      cb.checked = !!baiduState.selected[c.id];
      cb.disabled = !c.selectable;   // 目录/缺包/不支持：不可选
      cb.addEventListener("change", function () {
        toggleBaiduCandidate(c, cb.checked);
      });
      row.appendChild(cb);
      var info = document.createElement("span");
      info.className = "bc-info";
      var nm = document.createElement("span");
      nm.className = "bc-name";
      nm.textContent = c.name || c.id;
      nm.title = c.relative_path || c.name || "";
      var meta = document.createElement("span");
      meta.className = "bc-meta";
      meta.textContent = (c.relative_path || "") + " · " + formatDecBytes(c.size_bytes) +
        (c.format ? " · " + c.format : "") +
        (c.selectable ? "" : " · " + baiduCandidateReasonText(c.reason_code));
      info.appendChild(nm);
      info.appendChild(meta);
      row.appendChild(info);
      els.baiduCandidateList.appendChild(row);
    });
    if (els.baiduPrevBtn) els.baiduPrevBtn.disabled = baiduState.prevStack.length === 0;
    if (els.baiduNextBtn) els.baiduNextBtn.disabled = !baiduState.nextCursor;
    if (els.baiduPageInfo) {
      els.baiduPageInfo.textContent = t("bd.page.info", {
        n: baiduState.candidates.length,
        sel: Object.keys(baiduState.selected).length,
      });
    }
    updateBaiduSelectionSummary();
  }

  // 选择跨页保留（Map：candidate id → candidate）
  function toggleBaiduCandidate(c, on) {
    if (!c || !c.selectable) return;
    if (on) baiduState.selected[c.id] = c;
    else delete baiduState.selected[c.id];
    updateBaiduSelectionSummary();
    if (els.baiduPageInfo) {
      els.baiduPageInfo.textContent = t("bd.page.info", {
        n: baiduState.candidates.length,
        sel: Object.keys(baiduState.selected).length,
      });
    }
  }

  function baiduSelectedTotalBytes() {
    var total = 0n;
    Object.keys(baiduState.selected).forEach(function (k) {
      try { total += BigInt(baiduState.selected[k].size_bytes || 0); }
      catch (e) { /* 畸形条目跳过 */ }
    });
    return total;   // BigInt（十进制字符串来源，不经 Number）
  }

  function updateBaiduSelectionSummary() {
    if (!els.baiduSelectionSummary) return;
    var n = Object.keys(baiduState.selected).length;
    if (!n) {
      els.baiduSelectionSummary.textContent = t("bd.selected.none");
      return;
    }
    els.baiduSelectionSummary.textContent = t("bd.selected.summary", {
      n: n,
      size: formatDecBytes(baiduSelectedTotalBytes().toString()),
    });
  }

  // 导入门：枚举 ready+complete、能力允许、有选择
  function updateBaiduImportGate() {
    var ok = baiduState.importAvailable && baiduState.enumComplete &&
      Object.keys(baiduState.selected).length > 0;
    setBaiduImportEnabled(ok, !baiduState.importAvailable
      ? t("bd.cap.reason.import_disabled")
      : (!baiduState.enumComplete ? t("bd.enum.incomplete.short") : ""));
  }

  function startBaiduImport() {
    if (!baiduState.importAvailable || !baiduState.enumId || !baiduState.enumComplete) return;
    var ids = Object.keys(baiduState.selected);
    if (!ids.length) { toast(t("bd.selected.none"), "error"); return; }
    var payload = { enumeration_id: baiduState.enumId, candidate_ids: ids };
    var targetPid = (els.baiduTargetSelect && els.baiduTargetSelect.value) || "";
    if (targetPid) payload.target_project_id = targetPid;
    // Idempotency-Key：每次确认点击生成；网络失败重试复用（成功后清除）
    if (!baiduState.importIdemKey) baiduState.importIdemKey = uuid();
    var key = baiduState.importIdemKey;
    if (els.baiduImportBtn) els.baiduImportBtn.disabled = true;
    setBaiduImportStatus(t("bd.import.submitting"));
    apiFetch("/api/remote-imports/baidu/imports", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify(payload),
    }).then(jsonBody).then(function (res) {
      if (res.status !== 202 || !res.body || !res.body.id) {
        baiduState.importIdemKey = key;   // 保留：同一确认重试复用
        if (els.baiduImportBtn) els.baiduImportBtn.disabled = false;
        setBaiduImportStatus(t("bd.import.fail", {
          e: (res.body && (res.body.error || res.body.code)) || ("HTTP " + res.status),
        }));
        return;
      }
      baiduState.importIdemKey = null;    // 成功：下次确认重新生成
      baiduState.importId = res.body.id;
      setBaiduImportStatus(t("bd.import.started", { id: res.body.id }));
      pollBaiduImport();
    }).catch(function () {
      baiduState.importIdemKey = key;     // 网络失败：重试复用同一键
      if (els.baiduImportBtn) els.baiduImportBtn.disabled = false;
      setBaiduImportStatus(t("bd.import.fail", { e: t("imp.tasks.net.err") }));
    });
  }

  function setBaiduImportStatus(msg) {
    if (!els.baiduImportStatus) return;
    els.baiduImportStatus.textContent = msg || "";
    els.baiduImportStatus.hidden = !msg;
  }

  function bdStageLabel(stage) {
    switch (stage) {
      case "queued": return t("bd.stage.queued");
      case "transferring": return t("bd.stage.transferring");
      case "downloading": return t("bd.stage.downloading");
      case "validating": return t("bd.stage.validating");
      case "converting": return t("bd.stage.converting");
      case "ingesting": return t("bd.stage.ingesting");
      case "ready": return t("bd.stage.ready");
      case "failed": return t("bd.stage.failed");
      case "cancelled": return t("bd.stage.cancelled");
      default: return stage || "";
    }
  }

  function renderBaiduImport(batch) {
    if (!els.baiduImportItems) return;
    els.baiduImportItems.innerHTML = "";
    var counts = { ready: 0, failed: 0, other: 0 };
    (batch.items || []).forEach(function (it) {
      if (it.stage === "ready") counts.ready++;
      else if (it.stage === "failed" || it.stage === "cancelled") counts.failed++;
      else counts.other++;
      var row = document.createElement("div");
      row.className = "baidu-import-row stage-" + (it.stage || "");
      var nm = document.createElement("span");
      nm.className = "bi-name";
      nm.textContent = truncateMiddle(it.name || it.id, 30);
      var stg = document.createElement("span");
      stg.className = "bi-stage";
      stg.textContent = bdStageLabel(it.stage) +
        (it.error_code ? " · " + it.error_code : "");
      row.appendChild(nm);
      row.appendChild(stg);
      els.baiduImportItems.appendChild(row);
    });
    setBaiduImportStatus(t("bd.import.progress", {
      state: batch.state || "",
      ok: counts.ready,
      fail: counts.failed,
      total: (batch.items || []).length,
    }));
    // 部分失败（批次终态）：显示统计 + 仅重试失败项
    if (batch.state === "partial_failed" || batch.state === "failed") {
      var failedIds = (batch.items || [])
        .filter(function (it) { return it.stage === "failed"; })
        .map(function (it) { return it.id; });
      if (failedIds.length) {
        var retryBtn = document.createElement("button");
        retryBtn.type = "button";
        retryBtn.className = "btn secondary small";
        retryBtn.textContent = t("bd.import.retry.failed", { n: failedIds.length });
        retryBtn.addEventListener("click", function () {
          retryBtn.disabled = true;
          apiFetch("/api/remote-imports/baidu/imports/" +
                   encodeURIComponent(batch.id) + "/retry", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Idempotency-Key": uuid() },
            body: JSON.stringify({ item_ids: failedIds }),
          }).then(jsonBody).then(function (res) {
            if (!res.ok) {
              toast((res.body && res.body.error) || t("bd.import.fail.short"), "error");
              retryBtn.disabled = false;
              return;
            }
            toast(t("imp.task.retry.queued"), "success");
            pollBaiduImport();
          }).catch(function () {
            toast(t("bd.import.fail.short"), "error");
            retryBtn.disabled = false;
          });
        });
        els.baiduImportItems.appendChild(retryBtn);
      }
    }
  }

  function pollBaiduImport() {
    if (!baiduState.importId) return;
    if (baiduState.importTimer) clearTimeout(baiduState.importTimer);
    var tick = function () {
      apiFetch("/api/remote-imports/baidu/imports/" +
               encodeURIComponent(baiduState.importId)).then(jsonBody).then(function (res) {
        if (!res.ok || !res.body) {
          setBaiduImportStatus(t("bd.import.fail", {
            e: (res.body && res.body.error) || ("HTTP " + res.status),
          }));
          baiduState.importTimer = setTimeout(tick, 5000);
          return;
        }
        renderBaiduImport(res.body);
        var st = res.body.state;
        if (st === "succeeded" || st === "partial_failed" || st === "failed" ||
            st === "cancelled") {
          baiduState.importTimer = null;   // 终态：停止轮询
          reloadProjectsAndUnfiled();
          updateBaiduImportGate();
          return;
        }
        baiduState.importTimer = setTimeout(tick, 2500);
      }).catch(function () {
        setBaiduImportStatus(t("bd.import.fail", { e: t("imp.tasks.net.err") }));
        baiduState.importTimer = setTimeout(tick, 5000);
      });
    };
    tick();
  }

  // ---------- 抽屉开合 / 页签 ----------
  function switchImportTab(which) {
    importDrawerState.tab = which === "baidu" ? "baidu" : "local";
    var isLocal = importDrawerState.tab === "local";
    if (els.importTabLocal) {
      els.importTabLocal.classList.toggle("active", isLocal);
      els.importTabLocal.setAttribute("aria-selected", isLocal ? "true" : "false");
    }
    if (els.importTabBaidu) {
      els.importTabBaidu.classList.toggle("active", !isLocal);
      els.importTabBaidu.setAttribute("aria-selected", !isLocal ? "true" : "false");
    }
    if (els.importPanelLocal) els.importPanelLocal.hidden = !isLocal;
    if (els.importPanelBaidu) els.importPanelBaidu.hidden = isLocal;
    if (!isLocal && !baiduState.capsChecked) refreshBaiduCapabilities();
  }

  function openImportDrawer(triggerEl) {
    if (!els.importDrawer) return;
    triggerEl = closeSidebarDrawerUnderOverlay(triggerEl);
    importDrawerState.open = true;
    importDrawerState.lastFocusEl = triggerEl || null;
    els.importDrawer.hidden = false;
    if (els.importDrawerMask) els.importDrawerMask.hidden = false;
    // 目标下拉按当前项目列表重建（会话内选择保留）
    apiFetch("/api/projects").then(jsonBody).then(function (res) {
      if (res.ok && Array.isArray(res.body)) {
        allProjects = res.body;
      }
    }).catch(function () { /* 保留当前缓存 */ }).then(function () {
      renderImportTargetSelects();
      syncImportTargetFromSelect();
    });
    loadImportFormatCatalog();
    renderFrSampleMax();
    loadRecentFormatRequests();
    refreshImportTasks();
    startImportTaskPolling();
    switchImportTab(importDrawerState.tab);
    try {
      var first = els.importDrawerClose || els.importDrawer;
      if (first && typeof first.focus === "function") first.focus();
    } catch (e) { /* 忽略聚焦失败 */ }
  }

  function closeImportDrawer() {
    if (!els.importDrawer) return;
    importDrawerState.open = false;
    els.importDrawer.hidden = true;
    if (els.importDrawerMask) els.importDrawerMask.hidden = true;
    stopImportTaskPolling();
    var back = importDrawerState.lastFocusEl;
    importDrawerState.lastFocusEl = null;
    if (back && typeof back.focus === "function") {
      try { back.focus(); } catch (e) { /* 触发按钮可能已移除 */ }
    }
  }

  // 抽屉焦点圈闭：Tab 在抽屉可聚焦元素间循环（不落回背景）
  function trapDrawerFocus(e) {
    var drawer = els.importDrawer;
    if (!drawer) return;
    var focusables = [];
    try {
      focusables = Array.prototype.slice.call(drawer.querySelectorAll(
        "button:not([disabled]), input:not([disabled]), select, textarea"))
        .filter(function (el) { return !el.hidden; });
    } catch (err) { return; }
    if (!focusables.length) return;
    var active = document.activeElement;
    var first = focusables[0], last = focusables[focusables.length - 1];
    var inside = focusables.indexOf(active) >= 0;
    if (e.shiftKey && (!inside || active === first)) {
      e.preventDefault();
      try { last.focus(); } catch (err) {}
    } else if (!e.shiftKey && (!inside || active === last)) {
      e.preventDefault();
      try { first.focus(); } catch (err) {}
    }
  }

  // ---------- 事件绑定 ----------
  // =========================================================================
  // 顶栏信息架构（升级 Review 2026-09-09 §3.3–3.5）：
  //   - 工具栏 popover 通用定位/开关机制（矩形设置、标注选项、账户）
  //   - 账户 chip + popover + GET /api/account/balance 自助余额
  //   - 宽度断点分组（>=1440 全展开；1024–1439 折视图/标注组；<1024 只留
  //     当前工具、AI、倍率、账户，其余入 ⋯）
  // =========================================================================

  // ---------- nano-CNY 精确换算（§4.2；与 plugins/pathtogether-admin
  // ui/main.js formatCny2 同一算法：十进制字符串/BigInt → 两位小数，半分
  // 进位、绝对值方向舍入，全程不经 Number/toFixed，>2^53 不失真） ----------
  function formatCny2(v) {
    if (v === null || v === undefined || v === "") return null;
    var b;
    try { b = BigInt(v); } catch (e) { return null; }
    var neg = b < 0n;
    if (neg) b = -b;
    // 1 分 = 1e7 nano；+5e6 后整除 = 半分进位（away from zero）
    var cents = (b + 5000000n) / 10000000n;
    var whole = cents / 100n;
    var frac = (cents % 100n).toString().padStart(2, "0");
    return (neg && cents !== 0n ? "-" : "") + whole.toString() + "." + frac;
  }
  function acctCny(v) {
    var s = formatCny2(v);
    return s === null ? null : s + " CNY";
  }

  // 规范化邮箱 @ 前的 local-part；非邮箱存量账号回退完整 login_id
  function acctDisplayUsername(username) {
    var s = String(username == null ? "" : username);
    if (!s) return "";
    var at = s.indexOf("@");
    return at > 0 ? s.slice(0, at) : s;
  }

  // ---------- 工具栏 popover 机制 ----------
  // #toolbar overflow-x:auto 会裁剪内部绝对定位后代，因此浮层用 fixed 定位，
  // 打开时按触发按钮 getBoundingClientRect 计算；窗口 resize / 工具栏滚动 /
  // Escape / 点击外部即关闭。同一机制服务矩形设置、标注选项与账户三个浮层。
  var toolbarPopClosers = [];   // 已注册浮层的关闭函数（打开 ⋯ 菜单时统一收起）
  var closeTbbMoreMenu = function () {}; // bindTbbMore 里被真实 closeMore 覆盖

  // 打开期间把浮层挂到 body：#toolbar 的 overflow 裁剪与移动端底栏的
  // backdrop-filter（fixed 包含块）都不再影响定位。
  function ensurePopInBody(pop) {
    try {
      if (pop && pop.parentNode !== document.body && document.body && document.body.appendChild) {
        document.body.appendChild(pop);
      }
    } catch (e) { /* 保持原位 */ }
  }

  function positionToolbarPop(btn, pop) {
    if (!btn || !pop) return;
    ensurePopInBody(pop);
    var r = btn.getBoundingClientRect();
    if (!r) return;
    var pw = pop.offsetWidth || 0;
    var ph = pop.offsetHeight || 0;
    var vw = window.innerWidth || 1024;
    var vh = window.innerHeight || 768;
    var left = r.right - pw;
    if (left < 8) left = 8;
    if (left + pw > vw - 8) left = vw - 8 - pw;
    if (left < 8) left = 8;
    var top = r.bottom + 6;
    // 下方空间不足（如移动端底栏触发）：向上弹
    if (top + ph > vh - 8 && r.top - ph - 6 >= 8) {
      top = r.top - ph - 6;
    }
    pop.style.left = Math.round(left) + "px";
    pop.style.top = Math.round(top) + "px";
  }

  function bindToolbarPop(btn, pop, opts) {
    opts = opts || {};
    if (!btn || !pop) return null;
    var open = false;
    function setOpen(v) {
      v = !!v;
      if (v === open) { if (v) positionToolbarPop(btn, pop); return; }
      open = v;
      pop.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        positionToolbarPop(btn, pop);
        // 打开一个浮层时收起其它浮层与 ⋯ 菜单（同一时间至多一个）
        toolbarPopClosers.forEach(function (fn) {
          if (fn !== closer) fn();
        });
        closeTbbMoreMenu();
        if (typeof opts.onOpen === "function") opts.onOpen();
      } else if (typeof opts.onClose === "function") {
        opts.onClose();
      }
    }
    function closer() { setOpen(false); }
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      setOpen(!open);
    });
    document.addEventListener("click", function (e) {
      if (!open) return;
      var tgt = e.target;
      if (tgt && tgt.closest &&
          (tgt.closest("#" + pop.id) || tgt.closest("#" + btn.id))) return;
      setOpen(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && open) setOpen(false);
    });
    window.addEventListener("resize", function () { if (open) setOpen(false); });
    if (els.tbbMoreHost()) {
      els.tbbMoreHost().addEventListener("scroll", function () {
        if (open) setOpen(false);
      });
    }
    toolbarPopClosers.push(closer);
    return {
      open: function () { setOpen(true); },
      close: closer,
      isOpen: function () { return open; },
    };
  }

  // ---------- 账户 popover：余额拉取与渲染（§3.5） ----------
  // user=一次性总额度口径（total_allowance）；owner=当月窗口口径
  // （owner_month_window）。额度缺失（400 spend_total_allowance_missing）/
  // DB 不可用（503）/网络失败一律显示「额度信息暂不可用（原因）」，
  // 绝不显示 ¥0。金额是十进制字符串，经 formatCny2 精确换算两位小数。
  function acctBalanceUnavailable(status, code) {
    var key;
    if (status === 400 && code === "spend_total_allowance_missing") {
      key = "acct.balance.reason.missing";
    } else if (status === 503) {
      key = "acct.balance.reason.db";
    } else if (status === 401 || status === 403) {
      key = "acct.balance.reason.auth";
    } else if (status) {
      key = "acct.balance.reason.http";
    } else {
      key = "acct.balance.reason.network";
    }
    var reason = t(key, key === "acct.balance.reason.http" ? { status: String(status) } : null);
    if (els.acctPopScope) els.acctPopScope.textContent = "";
    if (els.acctPopRemaining) els.acctPopRemaining.textContent = "—";
    if (els.acctPopDetail) {
      els.acctPopDetail.textContent = t("acct.balance.unavailable", { reason: reason });
    }
  }

  function acctBalanceRender(data) {
    data = data || {};
    var isMonth = data.spend_target === "owner_month_window";
    if (els.acctPopScope) {
      els.acctPopScope.textContent = t(isMonth ? "acct.balance.scope.month" : "acct.balance.scope.total");
    }
    if (els.acctPopRemaining) {
      var remaining = acctCny(data.remaining_nano_cny);
      els.acctPopRemaining.textContent =
        remaining === null ? "—" : t("acct.balance.remaining") + " " + remaining;
    }
    if (els.acctPopDetail) {
      var rows = [];
      var limit = acctCny(data.limit_nano_cny);
      var spent = acctCny(data.spent_nano_cny);
      var reserved = acctCny(data.reserved_nano_cny);
      if (limit !== null) rows.push(t("acct.balance.limit") + " " + limit);
      if (spent !== null) rows.push(t("acct.balance.spent") + " " + spent);
      if (reserved !== null && String(data.reserved_nano_cny) !== "0") {
        rows.push(t("acct.balance.reserved") + " " + reserved);
      }
      if (isMonth && data.period_start && data.period_end) {
        rows.push(t("acct.balance.period") + " " +
          String(data.period_start).slice(0, 10) + " → " +
          String(data.period_end).slice(0, 10));
      }
      els.acctPopDetail.textContent = rows.join(" · ");
    }
  }

  function loadAccountBalance() {
    if (els.acctPopRemaining) els.acctPopRemaining.textContent = t("acct.balance.loading");
    if (els.acctPopDetail) els.acctPopDetail.textContent = "";
    if (els.acctPopScope) els.acctPopScope.textContent = "";
    apiFetch("/api/account/balance").then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        return { status: r.status, body: body };
      });
    }).then(function (res) {
      if (res.status !== 200 || !res.body || !res.body.subject) {
        acctBalanceUnavailable(res.status, res.body && res.body.code);
        return;
      }
      acctBalanceRender(res.body);
    }).catch(function () {
      acctBalanceUnavailable(0, null);
    });
  }

  function openAccountSettings() {
    if (acctPopCtl) acctPopCtl.close();
    if (!sidebarCtrl) return;
    // 沿用既有改密/改绑入口：展开侧栏（手机开抽屉）并滚到侧栏账户区
    sidebarCtrl.expand();
    var target = els.changepwBtn || els.changeemailBtn;
    if (target && typeof target.scrollIntoView === "function") {
      try { target.scrollIntoView({ block: "center" }); }
      catch (e) { try { target.scrollIntoView(); } catch (e2) {} }
    }
  }

  var acctPopCtl = null;
  var annoPopCtl = null;

  // els.tbbMore 的宿主：正式版在 #toolbar 内；浮层随工具栏横向滚动收起
  els.tbbMoreHost = function () { return els.tbbMore && els.tbbMore.parentNode; };

  function initToolbarPops() {
    acctPopCtl = bindToolbarPop(els.acctBtn, els.acctPop, {
      onOpen: function () { loadAccountBalance(); },
    });
    annoPopCtl = bindToolbarPop(els.annoMoreBtn, els.annoPop, {
      onOpen: function () {
        var input = els.annoLabelInput;
        if (input && typeof input.focus === "function") {
          setTimeout(function () { try { input.focus(); } catch (e) {} }, 30);
        }
      },
    });
    if (els.acctSettingsBtn) {
      els.acctSettingsBtn.addEventListener("click", openAccountSettings);
    }
    // 矩形设置 popover：开合由工具激活状态驱动（toggleRectTool/exitRoi）；
    // 这里只补「点击外部关闭 popover（工具保持激活）」的语义。
    document.addEventListener("click", function (e) {
      if (!rectToolActive() || !els.roiSettings || els.roiSettings.hidden) return;
      var tgt = e.target;
      if (tgt && tgt.closest &&
          (tgt.closest("#roi-settings") || tgt.closest("#roi-rect-btn"))) return;
      els.roiSettings.hidden = true;
      els.roiRectBtn.setAttribute("aria-expanded", "false");
    });
    toolbarPopClosers.push(function () {
      if (rectToolActive() && els.roiSettings && !els.roiSettings.hidden) {
        els.roiSettings.hidden = true;
        els.roiRectBtn.setAttribute("aria-expanded", "false");
      }
    });
    window.addEventListener("resize", function () {
      if (rectToolActive() && els.roiSettings && !els.roiSettings.hidden) {
        els.roiSettings.hidden = true;
        els.roiRectBtn.setAttribute("aria-expanded", "false");
      }
    });
  }

  // ---------- 宽度断点分组（§3.3） ----------
  // >=1440 全展开；1024–1439 折「视图」组（旋转/镜像/画质/通道）与「标注」组；
  // <1024 只留当前工具、AI、倍率、账户，其余入 ⋯。<=768 交还既有移动端布局
  //（底栏/上下文条 CSS 自管）。搬移真实 DOM 节点（监听器/状态机不复制），
  // 折叠目标统一是现有 #tbb-more 菜单。
  var TB_FOLD_SPECS = [
    { id: "view-tools-group", tiers: ["mid", "narrow"] },
    { id: "quality-control", tiers: ["mid", "narrow"] },
    { id: "channel-btn", tiers: ["mid", "narrow"] },
    { id: "anno-tools-group", tiers: ["mid", "narrow"] },
    { id: "anno-btn", tiers: ["mid", "narrow"] },
    { id: "save-anno-btn", tiers: ["mid", "narrow"] },
    { id: "zoom-group", tiers: ["narrow"] },
    { id: "save-btn", tiers: ["narrow"] },
    { id: "mpp-setter", tiers: ["narrow"] },
    { id: "zoom-native", tiers: ["narrow"] },
  ];

  function tbTierForWidth() {
    try {
      if (window.matchMedia) {
        if (window.matchMedia("(min-width: 1440px)").matches) return "wide";
        if (window.matchMedia("(min-width: 1024px)").matches) return "mid";
        return "narrow";
      }
    } catch (e) { /* fallthrough */ }
    var w = Number(window.innerWidth) || 1440;
    if (w >= 1440) return "wide";
    if (w >= 1024) return "mid";
    return "narrow";
  }

  // narrow 档「只留当前工具」：绘制工具激活时其所在组保留在主行
  function tbSpecPinned(id) {
    if (id === "anno-tools-group" && state.drawMode) return true;
    return false;
  }

  function tbMoveIntoMore(el, more) {
    if (el.parentNode === more) return;
    el.__tbHome = { parent: el.parentNode, next: el.nextSibling };
    more.appendChild(el);
  }

  function tbRestore(el) {
    var home = el.__tbHome;
    if (!home || !home.parent) return;
    try { home.parent.insertBefore(el, home.next); }
    catch (e) { try { home.parent.appendChild(el); } catch (e2) {} }
  }

  var tbCurrentTier = null;

  function applyToolbarTier() {
    var more = els.tbbMore;
    if (!more) return;
    var tier = tbTierForWidth();
    // <=768：既有移动端布局（底栏/上下文条）全权接管，节点全部归位
    try {
      if (window.matchMedia && window.matchMedia(SB_MOBILE_QUERY).matches) tier = "mobile";
    } catch (e) {}
    TB_FOLD_SPECS.forEach(function (spec) {
      var el = document.getElementById(spec.id);
      if (!el) return;
      var wantFolded = spec.tiers.indexOf(tier) >= 0 && !tbSpecPinned(spec.id);
      var isFolded = el.parentNode === more;
      if (wantFolded === isFolded) return;
      if (wantFolded) tbMoveIntoMore(el, more);
      else tbRestore(el);
    });
    // 档位类名（幂等）：CSS 据此隐藏悬空分隔线等
    var toolbar = more.parentNode;
    if (toolbar && toolbar.classList) {
      toolbar.classList.toggle("tb-tier-mid", tier === "mid");
      toolbar.classList.toggle("tb-tier-narrow", tier === "narrow");
    }
    if (tier !== tbCurrentTier) {
      tbCurrentTier = tier;
      // 档位切换时收起已开的工具栏浮层（几何已失效）
      if (annoPopCtl) annoPopCtl.close();
      if (acctPopCtl) acctPopCtl.close();
      if (rectToolActive() && els.roiSettings && !els.roiSettings.hidden) {
        els.roiSettings.hidden = true;
        els.roiRectBtn.setAttribute("aria-expanded", "false");
      }
    }
  }

  function initToolbarTier() {
    applyToolbarTier();
    window.addEventListener("resize", function () { applyToolbarTier(); });
  }

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

    // W4/W6：侧栏主入口 = 导入抽屉；#file-input 保持隐藏（抽屉内/拖拽仍触发）
    if (els.importSlidesBtn) {
      els.importSlidesBtn.addEventListener("click", function () {
        openImportDrawer(els.importSlidesBtn);
      });
    }
    if (els.uploadBtn) {   // 兼容：旧模板若仍渲染 #upload-btn，保持直开文件框
      els.uploadBtn.addEventListener("click", function () { els.fileInput.click(); });
    }
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
        updateRoiSummary();
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
    // 工单 D：pointercancel ≠ pointerup——取消恢复，绝不触发完成/保存
    c.addEventListener("pointercancel", onAnnoPointerCancel);
    // 工单 E：查看器右键菜单（preventDefault + 冻结载荷的附件意图）
    c.addEventListener("contextmenu", onViewerContextMenu);
    // 箭头单击-起点模式：双击终点完成（与第二次单击同效，防御性兜底）
    c.addEventListener("dblclick", function (e) {
      if (state.drawMode === "arrow" && drawPreview && drawPreview.type === "arrow" &&
          drawPreview.armed && validPreviewGeom(drawPreview)) {
        e.preventDefault();
        finishDraw();
      }
    });
    window.addEventListener("resize", function () { resizeAnnoCanvas(); redrawAnnoCanvas(); });
    // 工单 D：查看器级键盘——Escape 取消绘制/选区、Enter 提交有效未提交草稿、
    // Ctrl/Cmd+Z 撤销、Ctrl/Cmd+Shift+Z（及 Ctrl+Y）重做。
    // 输入框/textarea/contenteditable 内保留原生行为（含原生文字撤销）。
    window.addEventListener("keydown", onViewerKeydown);

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
    // 切片搜索（2026-09-22 重做）：点「搜索切片」才创建输入框；关闭即清空
    // 过滤并移除输入框。查询保留在输入框里，侧栏收起/展开、列表重渲不丢失
    if (els.slideSearchBtn) {
      els.slideSearchBtn.addEventListener("click", openSlideSearch);
    }

    // 移动端 ⋯ 溢出面板（AI 读片 + 缩放徽章）；§3.3 宽度分组的统一折叠目标
    bindTbbMore();
    // 工具栏浮层（账户/标注选项/矩形设置外点关闭）：§3.3/§3.5
    initToolbarPops();

    // 新建项目（W3：对话框；empty 永远空草稿，selection 复制勾选快照）
    if (els.newProjectBtn) {
      els.newProjectBtn.addEventListener("click", function () {
        openProjectDialog("empty", null, els.newProjectBtn);
      });
    }
    if (els.pcdClose) els.pcdClose.addEventListener("click", closeProjectDialog);
    if (els.pcdCancel) els.pcdCancel.addEventListener("click", closeProjectDialog);
    if (els.pcdConfirm) els.pcdConfirm.addEventListener("click", submitProjectDialog);
    if (els.pcdName) {
      els.pcdName.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          if (els.pcdNote && typeof els.pcdNote.focus === "function") {
            try { els.pcdNote.focus(); } catch (err) {}
          }
        }
      });
    }
    if (els.pcdNote) {
      els.pcdNote.addEventListener("keydown", function (e) {
        // R4：Enter 与确认按钮共用同一提交锁（inFlight 双保险）
        if (e.key === "Enter") { e.preventDefault(); submitProjectDialog(); }
      });
    }
    if (els.projectCreateMask) {
      els.projectCreateMask.addEventListener("click", function (e) {
        if (e.target === els.projectCreateMask) closeProjectDialog();
      });
    }

    // 导入抽屉（W4/W6）
    if (els.importDrawerClose) {
      els.importDrawerClose.addEventListener("click", closeImportDrawer);
    }
    if (els.importDrawerMask) {
      els.importDrawerMask.addEventListener("click", closeImportDrawer);
    }
    if (els.importTabLocal) {
      els.importTabLocal.addEventListener("click", function () { switchImportTab("local"); });
    }
    if (els.importTabBaidu) {
      els.importTabBaidu.addEventListener("click", function () { switchImportTab("baidu"); });
    }
    if (els.importPickFiles) {
      els.importPickFiles.addEventListener("click", function () {
        if (els.fileInput) els.fileInput.click();
      });
    }
    if (els.importTargetSelect) {
      els.importTargetSelect.addEventListener("change", syncImportTargetFromSelect);
    }
    if (els.importTargetNewName) {
      els.importTargetNewName.addEventListener("input", function () {
        importTargetState.newProjectName = (els.importTargetNewName.value || "").trim();
      });
    }

    // 申请新格式支持（抽屉内次级入口；提交逻辑在 submitFormatRequest）
    if (els.formatReqBtn && els.formatReqForm) {
      els.formatReqBtn.addEventListener("click", function () {
        els.formatReqForm.hidden = !els.formatReqForm.hidden;
      });
      els.frCancel.addEventListener("click", function () {
        els.formatReqForm.hidden = true;
      });
      els.frSubmit.addEventListener("click", submitFormatRequest);
    }

    // 百度分享页签（W5/W6）
    if (els.baiduListBtn) {
      els.baiduListBtn.addEventListener("click", startBaiduEnumeration);
    }
    if (els.baiduSearch) {
      els.baiduSearch.addEventListener("input", renderBaiduCandidates);
    }
    if (els.baiduPrevBtn) {
      els.baiduPrevBtn.addEventListener("click", function () {
        if (!baiduState.prevStack.length) return;
        baiduState.cursor = baiduState.prevStack.pop();
        loadBaiduCandidates();
      });
    }
    if (els.baiduNextBtn) {
      els.baiduNextBtn.addEventListener("click", function () {
        if (!baiduState.nextCursor) return;
        if (baiduState.cursor) baiduState.prevStack.push(baiduState.cursor);
        baiduState.cursor = baiduState.nextCursor;
        loadBaiduCandidates();
      });
    }
    if (els.baiduImportBtn) {
      els.baiduImportBtn.addEventListener("click", startBaiduImport);
    }

    // Esc 关闭：项目对话框优先，其次导入抽屉；Tab 圈闭焦点
    document.addEventListener("keydown", function (e) {
      if (projectDialog.open) { handleProjectDialogKeydown(e); return; }
      if (!importDrawerState.open) return;
      if (e.key === "Escape") {
        e.preventDefault();
        closeImportDrawer();
      } else if (e.key === "Tab") {
        trapDrawerFocus(e);
      }
    });

    // 未归类
    els.unfiledToggle.addEventListener("click", function () {
      var sec = els.unfiledBody.closest(".section");
      if (sec) sec.classList.toggle("collapsed");
    });
    els.unfiledNewProject.addEventListener("click", function () {
      var slides = Object.keys(slideChecked).filter(function (k) { return slideChecked[k]; });
      if (slides.length === 0) { toast(t("unfiled.need.check"), "error"); return; }
      // selection 模式：把当前勾选**快照**传入对话框（可逐项移除；取消/重开
      // 清空，不再有 pendingNewProjectSlides 隐式全局回退）
      openProjectDialog("selection", slides, els.unfiledNewProject);
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

    // 更换邮箱（P1-3 身份收口 review-2026-09-08 P2-2；与改密同级账户入口）
    initChangeEmail();

    // 数据共享（P2 账户设置 §3.5；与改密/改绑同级的自愿研究授权入口）
    initDataShare();

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
    // viewer.navigate / viewer.getViewport / viewer.highlight /
    // viewer.applyRenderContext / annotation.create / annotation.read /
    // annotation.focus）
    host.onRequest("slide.getCurrent", gate("slide.getCurrent", function () {
      if (!state.slide) return null;
      return { name: state.slide.name, width: state.slide.width, height: state.slide.height,
               mppX: state.slide.mppX, mppY: state.slide.mppY };
    }));
    host.onRequest("selection.getBbox", gate("selection.getBbox", function () { return currentSelectionBbox(); }));
    host.onRequest("viewer.getViewport", gate("viewer.getViewport", function () {
      // P1「普通发送绑定浏览器当前视野」：返回当前 OpenSeadragon 视野的
      // level-0 像素 bbox {x,y,w,h}，供插件随 run/continue 发送附带，使
      // 「分析/判读当前视野」类指令落到用户真实在看的范围（而非 AI 上次
      // 快照）。R1（同 viewer.navigate 惯例）：viewer 未就绪回真实 error
      // code（retryable），不吞异常伪报 ok；无切片返回 null。
      if (!state.slide) return null;
      if (!viewer || !viewer.viewport) {
        throw { code: "viewer_not_ready", message: "查看器未就绪", retryable: true };
      }
      var bb = level0ViewportBbox();
      if (!bb) {
        throw { code: "invalid_geometry", message: "视野几何非法", retryable: false };
      }
      return bb;
    }));
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

  // =========================================================================
  // 工单 E（plan §6）：查看器右键菜单 —— 把当前视野 / 标注加入 AI 会话草稿
  // -------------------------------------------------------------------------
  // - 查看器 overlay 右键：preventDefault（不弹浏览器菜单、不启动绘制）；
  //   非绘制进行中时弹平台上下文菜单。菜单**打开时冻结**载荷（level-0 bbox
  //   权威、视野中心与右键点位分列、倍率、render context、marker 元数据），
  //   点击菜单项才经 HostBridge emit `conversation.attachIntent` 给插件——
  //   只进会话草稿，不自动发送、不启动 AI、不开分支。
  // - 插件未加载：toast「AI 插件未加载」，不假装已加入。
  // - 绘制进行中（箭头/描图/矩形拖出）：右键仅 preventDefault 不弹菜单，
  //   绘制取消逻辑归绘制工单（D），不在此抢交互。
  // =========================================================================

  // 当前视野 level-0 像素 bbox（viewer.getViewport 桥方法与右键菜单共用的
  // 单一实现）：getBounds(true) → viewportToImageRectangle → 钳到切片边界
  // [0,0,width,height] → 取整。viewer 未就绪 / 几何非法 → null（调用方决定
  // 错误语义）。
  function level0ViewportBbox() {
    if (!state.slide || !viewer || !viewer.viewport) return null;
    try {
      var bounds = viewer.viewport.getBounds(true);
      var rect = viewer.viewport.viewportToImageRectangle(bounds);
      var vx = Number(rect && rect.x), vy = Number(rect && rect.y);
      var vw = Number(rect && rect.width), vh = Number(rect && rect.height);
      if (!isFinite(vx) || !isFinite(vy) || !isFinite(vw) || !isFinite(vh)) return null;
      var sw = Number(state.slide.width) || 0, sh = Number(state.slide.height) || 0;
      var x0 = Math.min(Math.max(vx, 0), sw);
      var y0 = Math.min(Math.max(vy, 0), sh);
      var x1 = Math.min(Math.max(vx + vw, 0), sw);
      var y1 = Math.min(Math.max(vy + vh, 0), sh);
      var rx0 = Math.round(x0), ry0 = Math.round(y0);
      return {
        x: rx0,
        y: ry0,
        w: Math.max(0, Math.round(x1) - rx0),
        h: Math.max(0, Math.round(y1) - ry0),
      };
    } catch (e) { return null; }
  }

  // 当前倍率文案（zoomText 同源："20×" / "35%"）；不可得 → null
  function currentViewerMagnification() {
    try {
      if (window.HP_ViewerCore && HP_ViewerCore.zoomText) {
        var s = HP_ViewerCore.zoomText(viewer, state.mppX);
        return (s && s !== "—") ? s : null;
      }
    } catch (e) {}
    return null;
  }

  // 当前查看器 render context（Batch 4 桥接口径：window.PathTogether.renderState；
  // RGB/未启用 → null）——快照冻结用，不含短期令牌。
  function viewerRenderContextSnapshot() {
    try {
      var rs = window.PathTogether && window.PathTogether.renderState;
      return (rs && rs.renderContext) || null;
    } catch (e) { return null; }
  }

  // 标注几何 → level-0 包围盒（rect/arrow/freehand 三形；无效 → null）
  function annoItemBbox(it) {
    if (!it) return null;
    var typ = it.type || "rect";
    var x, y, w, h;
    if (typ === "arrow") {
      x = Math.min(Number(it.x1), Number(it.x2)); y = Math.min(Number(it.y1), Number(it.y2));
      w = Math.abs(Number(it.x2) - Number(it.x1)); h = Math.abs(Number(it.y2) - Number(it.y1));
    } else if (typ === "freehand" && it.points && it.points.length) {
      var xs = it.points.map(function (p) { return Number(p[0]); });
      var ys = it.points.map(function (p) { return Number(p[1]); });
      x = Math.min.apply(null, xs); y = Math.min.apply(null, ys);
      w = Math.max.apply(null, xs) - x; h = Math.max.apply(null, ys) - y;
    } else {
      x = Number(it.x); y = Number(it.y);
      w = Number(rectItemW(it)); h = Number(rectItemH(it));
    }
    if (!isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h) || w <= 0 || h <= 0) return null;
    return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
  }

  var ctxMenuEl = null;       // 惰性创建的菜单 DOM（#viewer-ctx-menu）
  var ctxMenuFrozen = null;   // 菜单打开时冻结的载荷 {viewport: payload|null, marker: payload|null}
  var ctxMenuCloser = null;   // 打开期间挂的 document 级关闭监听清理函数

  function ensureViewerCtxMenu() {
    if (ctxMenuEl && ctxMenuEl.parentNode) return ctxMenuEl;
    var m = document.createElement("div");
    m.id = "viewer-ctx-menu";
    m.className = "viewer-ctx-menu";
    m.setAttribute("role", "menu");
    m.style.display = "none";
    // 菜单自身右键：吞掉，不再叠一层浏览器默认菜单
    m.addEventListener("contextmenu", function (e) {
      e.preventDefault(); e.stopPropagation();
    });
    document.body.appendChild(m);
    ctxMenuEl = m;
    return m;
  }

  function closeViewerCtxMenu() {
    if (ctxMenuCloser) { try { ctxMenuCloser(); } catch (e) {} ctxMenuCloser = null; }
    ctxMenuFrozen = null;
    if (ctxMenuEl) ctxMenuEl.style.display = "none";
  }

  // 菜单项点击 → emit attachIntent（仅加入草稿；插件未加载明确提示，不假装成功）
  function emitConversationAttachIntent(payload) {
    if (!payload) return;
    if (!hpReady()) {
      toast(t("ctxmenu.attach.plugin.missing"), "error");
      return;
    }
    hpEmit("conversation.attachIntent", payload);
    toast(t(payload.kind === "marker" ? "ctxmenu.attach.anno.added" : "ctxmenu.attach.view.added"), "success");
  }

  // ---------- 载荷冻结（菜单打开时执行；字段与工单 E 契约一一对应） ----------
  // clickPt 为右键图像点位（不可得 → null）；**绝不**把点位冒充视野中心：
  // center 一律由冻结 bbox 推导，与 click_point 分列。
  function freezeViewportAttachPayload(clickPt) {
    var bbox = level0ViewportBbox();
    if (!bbox) return null;
    return {
      kind: "viewport",
      slide: state.slide.name,
      bbox: bbox,
      click_point: clickPt || null,
      center: { x: Math.round(bbox.x + bbox.w / 2), y: Math.round(bbox.y + bbox.h / 2) },
      magnification: currentViewerMagnification(),
      render_context: viewerRenderContextSnapshot(),
      annotation_id: null, revision: null, type: null, geometry: null, note: null,
      frozen_at: Math.floor(Date.now() / 1000),
    };
  }

  function freezeMarkerAttachPayload(it, clickPt) {
    if (!it) return null;
    var bbox = annoItemBbox(it);
    if (!bbox) return null;
    var geom = null;
    try { geom = snapshotGeom(it); } catch (e) { geom = null; }
    return {
      kind: "marker",
      slide: state.slide.name,
      bbox: bbox,   // 标注包围盒（发送时作为冻结 viewport，不重查实时视野）
      click_point: clickPt || null,
      center: { x: Math.round(bbox.x + bbox.w / 2), y: Math.round(bbox.y + bbox.h / 2) },
      magnification: currentViewerMagnification(),
      render_context: viewerRenderContextSnapshot(),
      annotation_id: (it.annotation_id != null ? String(it.annotation_id) : null),
      revision: (it.revision != null ? Number(it.revision) : null),
      type: it.type || "rect",
      geometry: geom,
      note: (it.note != null ? String(it.note) : null),   // 未受信文本：仅透传展示
      frozen_at: Math.floor(Date.now() / 1000),
    };
  }

  // ---------- 打开 / 事件入口 ----------
  function openViewerContextMenu(e) {
    if (!state.slide) return;
    var m = ensureViewerCtxMenu();
    closeViewerCtxMenu();
    // 冻结（菜单打开时刻）：右键点位（图像坐标，可得则记）+ 视野 + 命中/选中标注
    var clickPt = null;
    try { clickPt = screenToImg(e); } catch (err) { clickPt = null; }
    var frozen = { viewport: freezeViewportAttachPayload(clickPt), marker: null };
    var markerIt = null;
    if (state.showAnno && !state.drawMode) {
      try { markerIt = hitAnno(screenPt(e).x, screenPt(e).y); } catch (err) { markerIt = null; }
    }
    if (!markerIt) markerIt = editItem || state.focusAnno;   // 显式选中兜底
    if (markerIt) frozen.marker = freezeMarkerAttachPayload(markerIt, clickPt);
    ctxMenuFrozen = frozen;

    // 菜单项构建：视野项（冻结失败不显示死项）；标注项（无命中且无选中不显示）
    m.innerHTML = "";
    var appendItem = function (storeKey, label) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "viewer-ctx-menu-item";
      b.setAttribute("role", "menuitem");
      b.dataset.attachKind = storeKey;
      b.textContent = label;
      b.addEventListener("click", function (ev) {
        ev.preventDefault(); ev.stopPropagation();
        var payload = (ctxMenuFrozen && ctxMenuFrozen[storeKey]) || null;
        closeViewerCtxMenu();
        emitConversationAttachIntent(payload);
      });
      m.appendChild(b);
    };
    if (frozen.viewport) appendItem("viewport", t("ctxmenu.attach.view"));
    if (frozen.marker) appendItem("marker", t("ctxmenu.attach.anno"));
    if (!m.children.length) return;   // 无可附内容（如 viewer 未就绪）：不开菜单

    // 定位：右键点附近；越界按窗口尺寸钳回
    var x = e.clientX || 0, y = e.clientY || 0;
    m.style.display = "block";
    try {
      var vw0 = window.innerWidth || 0, vh0 = window.innerHeight || 0;
      var mw = m.offsetWidth || 180, mh = m.offsetHeight || 60;
      if (vw0 && x + mw > vw0 - 8) x = Math.max(8, vw0 - mw - 8);
      if (vh0 && y + mh > vh0 - 8) y = Math.max(8, vh0 - mh - 8);
    } catch (err) { /* 无布局环境（测试）：原样定位 */ }
    m.style.left = x + "px";
    m.style.top = y + "px";

    // 关闭：外部 pointerdown/click、Escape、另处右键、窗口 resize
    var onDocClose = function (ev) {
      try {
        if (ev && ev.target === m) return;   // 菜单内事件不关（click 由菜单项自理）
      } catch (err) {}
      closeViewerCtxMenu();
    };
    var onKey = function (ev) {
      if (ev && ev.key === "Escape") closeViewerCtxMenu();
    };
    document.addEventListener("pointerdown", onDocClose, true);
    document.addEventListener("click", onDocClose, true);
    document.addEventListener("contextmenu", onDocClose, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", closeViewerCtxMenu);
    ctxMenuCloser = function () {
      document.removeEventListener("pointerdown", onDocClose, true);
      document.removeEventListener("click", onDocClose, true);
      document.removeEventListener("contextmenu", onDocClose, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", closeViewerCtxMenu);
    };
  }

  // contextmenu 入口（绑定在标注画布层）：绘制进行中只吞默认菜单（取消逻辑
  // 归工单 D），否则弹附件菜单。
  function onViewerContextMenu(e) {
    e.preventDefault();
    e.stopPropagation();
    if (!state.slide) return;
    if ((state.drawMode && drawPreview) || (rectToolActive() && rectDrawInfo)) return;
    openViewerContextMenu(e);
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
        // 2026-09-22 重做：搜索输入框按需创建，「选择切片」只展开侧栏、
        // 不创建输入框；焦点落在「搜索切片」按钮（侧栏内首个入口）
        if (els.slideSearchBtn && typeof els.slideSearchBtn.focus === "function") {
          try { els.slideSearchBtn.focus(); } catch (e) { /* 忽略聚焦失败 */ }
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

  // viewer 画质档（image-transport-upgrade §3.3/§5.2）：三入口共用
  // HP_ViewerEncoding；本页只注入宿主/文案与重开回调。画质切换走
  // channelCtrl.reopenForQuality（轻量路径：不改 context、不重绑 AI）。
  function initQualityControl() {
    if (!window.HP_ViewerEncoding) return;
    HP_ViewerEncoding.mount({
      host: els.qualityControl,
      t: t,
      toast: function (msg, type) { toast(msg, type); },
      onQualityReopen: function () {
        if (channelCtrl) channelCtrl.reopenForQuality();
      },
    });
    HP_ViewerEncoding.installConflictRecovery({
      viewer: viewer,
      onConflict: function () {
        // 409 display_version_conflict：只刷新 info 并重建一次（保留选择/视口）
        if (channelCtrl) channelCtrl.recoverDisplayConflict();
      },
    });
  }

  function init() {
    initViewer();
    initQualityControl();
    initSidebarController();
    bindEvents();
    // §3.3 宽度断点分组：先于首次布局执行（把折叠档的节点搬入 ⋯ 菜单）
    initToolbarTier();
    setupDragDrop();
    // COS 直传 Phase 4：capability 可用时渲染手动开关，并恢复未完任务的
    // 只读进度行（off 时均为无操作，上传管线行为不变）
    initCosUploadUi();
    initAuth();
    // P3 研究采集装配（capabilities.research_collection 开启才工作）
    initResearchTelemetry();
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

  // 供测试（tests/js/project-import-upgrade.test.ts）：__PT_TEST_HOOKS 由测试
  // 预置后再加载 app.js 才挂载；生产环境不暴露任何额外全局（与 HP_AUTH/
  // HP_UPLOAD 同风格）。置于 IIFE 末尾：确保引用的状态对象已初始化。
  if (window.__PT_TEST_HOOKS) {
    window.HP_PROJECT_UI = {
      projectDialog: projectDialog,
      openProjectDialog: openProjectDialog,
      closeProjectDialog: closeProjectDialog,
      submitProjectDialog: submitProjectDialog,
      submitFormatRequest: submitFormatRequest,
      importDrawer: {
        state: importDrawerState,
        targetState: importTargetState,
        open: openImportDrawer,
        close: closeImportDrawer,
        switchTab: switchImportTab,
        refreshTasks: refreshImportTasks,
        loadFormatCatalog: loadImportFormatCatalog,
        associate: importAssociateUploaded,
      },
      baidu: {
        state: baiduState,
        refreshCapabilities: refreshBaiduCapabilities,
        startEnumeration: startBaiduEnumeration,
        loadCandidates: loadBaiduCandidates,
        toggleCandidate: toggleBaiduCandidate,
        startImport: startBaiduImport,
        formatDecBytes: formatDecBytes,
      },
      // Wave 3（普通图片兼容）测试入口：accept 派生/无物理标尺单位区/像素标注保存。
      // 与 HP_UPLOAD 同约定：仅当测试预置 __PT_TEST_HOOKS 才挂载，生产不暴露。
      formats: {
        fallbackCatalog: FORMAT_CATALOG_FALLBACK,
        acceptFallback: FILE_INPUT_ACCEPT_FALLBACK,
        acceptFromCatalog: acceptFromCatalog,
        applyFileInputAccept: applyFileInputAccept,
      },
      viewerState: state,
      openSlide: openSlide,
      slideHasPhysicalScale: slideHasPhysicalScale,
      syncUnitAvailability: syncUnitAvailability,
      setMpp: setMpp,
      saveAnno: saveAnno,
      slideMetaTags: slideMetaTags,
    };
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
    // 切片搜索（按需创建）：动态控件文案/aria 随语言刷新
    try { refreshSlideSearchTexts(); } catch (e) {}
    // §3.4：document.title 的产品名后缀随语言刷新（切片名不变）
    try {
      if (state.slide && state.slide.name) updateDocTitle(state.slide.alias || state.slide.name);
    } catch (e) {}
    // 账户 popover 角色文案随语言刷新
    try {
      if (els.acctPopRole && currentRole) {
        els.acctPopRole.textContent = t(currentRole === "owner" ? "acct.role.owner" : "acct.role.user");
      }
    } catch (e) {}
    // W3/W4/W6：导入抽屉 / 新建项目对话框的动态文案随语言重渲
    try {
      if (projectDialog.open) renderProjectDialogSlides();
      if (els.pcdConfirm && !projectDialog.inFlight) {
        els.pcdConfirm.textContent = t("pcd.confirm");
      }
    } catch (e) {}
    try {
      if (importDrawerState.open) {
        if (importDrawerState.formatsLoaded) renderFormatCatalog(importDrawerState.formatCatalog);
        renderFrSampleMax();
        refreshImportTasks();
      }
    } catch (e) {}
    // AI 配置摘要 / 会话切换器的语言重渲由 HistoPilot 插件 bundle 自行监听 hp-lang-change 处理。
  });
})();
