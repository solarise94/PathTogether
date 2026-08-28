/* =========================================================================
   pathtogether-admin 桥接客户端 + 只读业务页面（PR3b）

   docs/admin-billing-plugin-implementation-plan.md §8.3/§8.4/§10：
     - 本页运行在 /admin 宿主页的 opaque iframe（sandbox="allow-scripts"）内；
     - 宿主在每次 iframe load 后 postMessage 一条 {kind:"init"} 携带一次性
       256-bit nonce 与协议版本；本端保存 nonce（仅内存），此后所有请求回带
       nonce + 本次会话内递增的 requestId；
     - 响应按 requestId 配对；超时（15s）与拒绝都有稳定错误码；
     - opaque origin：不能读写 localStorage/cookie，全部页面状态（当前页/
       筛选/分页游标）只在内存。
   页面只读（§10.1/§10.2/§10.4/§10.5）：操作按钮与写方法留给 PR5。
   渲染只用 textContent / createElement（不拼 HTML，插件数据永不进标记）。
   ========================================================================= */
(function () {
  "use strict";

  var BRIDGE = "admin";
  var PROTOCOL_FALLBACK = "1.0.0";
  var REQUEST_TIMEOUT_MS = 15000;

  // ---- 内存态（opaque origin：无 localStorage/cookie，一切仅存本页生命周期）----
  var state = {
    nonce: null,            // 宿主 init 下发的一次性 nonce（仅内存）
    protocolVersion: null,  // 宿主声明的桥协议版本
    granted: [],            // 宿主确认的 manifest adminPermissions（展示用）
    seq: 0,                 // 请求序号（本页生命周期内单调递增）
    pending: {},            // requestId -> {resolve, reject, timer, method}
    dead: false,            // 宿主作废（reload/登出/切换）后不再发任何请求
    page: "overview",       // 当前页（内存态导航）
    // 分页游标（每列表独立；仅内存）
    cursors: { users: null, usage: null, unpriced: null, ledger: null,
               audit: null, acqUsers: null },
    filters: { users: {}, usage: {}, audit: {} },
  };

  function $(id) { return document.getElementById(id); }

  var els = {
    handshake: $("adm-handshake-status"),
    nav: $("adm-nav"),
    pages: {
      overview: $("adm-page-overview"),
      users: $("adm-page-users"),
      invites: $("adm-page-invites"),
      billing: $("adm-page-billing"),
      audit: $("adm-page-audit"),
    },
    errorCard: $("adm-error-card"),
    errorText: $("adm-error-text"),
  };

  function setHandshake(text) {
    if (els.handshake) els.handshake.textContent = text;
  }

  function showError(code, message) {
    if (!els.errorCard || !els.errorText) return;
    els.errorCard.hidden = false;
    els.errorText.textContent = (code || "error") + (message ? (": " + message) : "");
  }

  function hideError() {
    if (els.errorCard) els.errorCard.hidden = true;
  }

  // ---- 格式化 ----
  function fmtTs(epoch) {
    if (epoch === null || epoch === undefined) return "—";
    try {
      var d = new Date(Number(epoch) * 1000);
      return d.toISOString().replace("T", " ").replace(".000Z", "Z");
    } catch (e) { return String(epoch); }
  }

  function fmtNum(v) {
    if (v === null || v === undefined) return "—";
    if (typeof v !== "number") return String(v);
    return v.toLocaleString("en-US");
  }

  // nano-CNY（1e9 = 1 CNY）：同时给 CNY 近似值与原始 nano 整数
  function fmtNano(v) {
    if (v === null || v === undefined) return "—";
    var n = Number(v);
    return (n / 1e9).toFixed(6) + " CNY（" + n + " nano）";
  }

  function fmtRatio(v) {
    if (v === null || v === undefined) return "—";
    return (Math.round(v * 1000) / 10) + "%";
  }

  function kvRow(dl, key, value) {
    var dt = document.createElement("dt");
    dt.textContent = key;
    var dd = document.createElement("dd");
    dd.textContent = value === null || value === undefined ? "—" : String(value);
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  function td(text) {
    var cell = document.createElement("td");
    cell.textContent = text === null || text === undefined ? "—" : String(text);
    return cell;
  }

  // ---- 请求：nonce + 递增 requestId；targetOrigin "*" —— iframe 是 opaque
  //      origin，读不到宿主 origin；安全边界在宿主侧（精确 WindowProxy +
  //      高熵 nonce + 服务端 owner/CSRF 复核，docs §8.3）。
  function request(method, payload) {
    return new Promise(function (resolve, reject) {
      if (state.dead) {
        reject({ code: "bridge_invalidated", message: "宿主已作废本次桥接会话" });
        return;
      }
      if (!state.nonce) {
        reject({ code: "not_ready", message: "尚未收到宿主初始化消息" });
        return;
      }
      var requestId = "adm_req_" + (++state.seq);
      var env = {
        kind: "request",
        bridge: BRIDGE,
        protocolVersion: state.protocolVersion || PROTOCOL_FALLBACK,
        nonce: state.nonce,
        requestId: requestId,
        method: method,
        payload: payload || {},
      };
      var timer = setTimeout(function () {
        if (!state.pending[requestId]) return;
        delete state.pending[requestId];
        reject({ code: "bridge_timeout", message: "宿主未在 " + REQUEST_TIMEOUT_MS + "ms 内响应 " + method });
      }, REQUEST_TIMEOUT_MS);
      state.pending[requestId] = { resolve: resolve, reject: reject, timer: timer, method: method };
      window.parent.postMessage(env, "*");
    });
  }

  function failAllPending(err) {
    Object.keys(state.pending).forEach(function (requestId) {
      var p = state.pending[requestId];
      clearTimeout(p.timer);
      p.reject(err);
      delete state.pending[requestId];
    });
  }

  function onMessage(event) {
    var env = event.data;
    if (!env || typeof env !== "object" || env.bridge !== BRIDGE) return;

    if (env.kind === "init") {
      // 握手：宿主每次 load 重新下发 nonce —— 直接覆盖旧值并丢弃旧在途请求
      state.nonce = typeof env.nonce === "string" ? env.nonce : null;
      state.protocolVersion = env.protocolVersion || PROTOCOL_FALLBACK;
      state.granted = Array.isArray(env.adminPermissions) ? env.adminPermissions.slice() : [];
      state.dead = false;
      setHandshake("桥接已建立（protocolVersion=" + state.protocolVersion +
        "，管理能力 " + state.granted.length + " 项）");
      resetLists();
      showPage("overview");
      loadOverview();
      return;
    }

    if (env.kind === "response") {
      var p = state.pending[env.requestId];
      if (!p) return; // 未知/已超时/已作废的响应：静默丢弃
      clearTimeout(p.timer);
      delete state.pending[env.requestId];
      if (env.ok) {
        p.resolve(env.result == null ? null : env.result);
      } else {
        p.reject(env.error || { code: "bridge_error", message: "宿主返回未知错误" });
      }
      return;
    }

    if (env.kind === "event" && env.type === "bridge_invalidated") {
      // 宿主作废（reload/登出/插件切换）：立即停止一切请求
      state.dead = true;
      state.nonce = null;
      failAllPending({ code: "bridge_invalidated", message: env.message || "宿主已作废桥接会话" });
      setHandshake("桥接会话已作废（" + (env.reason || "unknown") + "），等待重新握手…");
    }
  }

  // ---- 错误呈现（稳定 code → 人话；不泄露任何敏感内容）----
  function errText(err) {
    var code = err && err.code ? err.code : "bridge_error";
    var msg = err && err.message ? err.message : "";
    if (code === "pg_backend_required") {
      return "该数据要求 PostgreSQL 后端（当前部署为 json/dual，稳定拒绝；不降级显示）";
    }
    if (code === "refresh_throttled") return "刷新过于频繁，请稍后再试（服务端限速）";
    if (code === "bridge_invalidated") return "桥接会话已失效，请重新加载插件";
    return code + (msg ? ("：" + msg) : "");
  }

  function handleErr(err, statusEl) {
    showError(err && err.code, err && err.message);
    if (statusEl) statusEl.textContent = errText(err);
  }

  // ------------------------------------------------------------------
  // 概览（§10.1）——「对话额度」与「金额余额」必须并列、不得只显示一个
  // 模糊「额度」；json/dual 后端 billing/turn 段标记不可用。
  // ------------------------------------------------------------------
  function loadOverview() {
    var actorDl = $("adm-actor-info");
    request("admin.auth.get", {}).then(function (identity) {
      if (!actorDl) return;
      actorDl.textContent = "";
      kvRow(actorDl, "当前 actor 角色", identity && identity.role);
      kvRow(actorDl, "登录账号（掩码）", identity && identity.loginIdMasked);
      kvRow(actorDl, "预览态", identity && identity.previewActive ? "是" : "否");
    }).catch(function () { /* 身份卡失败不阻塞概览 */ });

    request("admin.overview.get", {}).then(function (ov) {
      hideError();
      renderOverview(ov);
    }).catch(function (err) { handleErr(err, null); });
  }

  function renderOverview(ov) {
    var usersDl = $("adm-ov-users");
    if (usersDl) {
      usersDl.textContent = "";
      var u = ov.users || {};
      kvRow(usersDl, "总用户数", fmtNum(u.total));
      kvRow(usersDl, "启用中", fmtNum(u.active));
      kvRow(usersDl, "已禁用", fmtNum(u.disabled));
      kvRow(usersDl, "AI access 用户", fmtNum(u.ai_access));
    }

    // 对话额度（turn budget）
    var turnDl = $("adm-ov-turn");
    if (turnDl) {
      turnDl.textContent = "";
      var t = ov.turn_budget || {};
      if (t.available === false) {
        kvRow(turnDl, "可用性", "不可用（" + (t.code || "pg_backend_required") + "）");
      } else {
        kvRow(turnDl, "周期", "#" + fmtNum(t.period_id) + "（" + fmtTs(t.period_started_at) + " 起）");
        kvRow(turnDl, "平台总额度（用/上限）",
              fmtNum(t.platform && t.platform.total) + " / " + fmtNum(t.platform && t.platform.limit));
        kvRow(turnDl, "用户共享池（用/上限）",
              fmtNum(t.user_pool && t.user_pool.total) + " / " + fmtNum(t.user_pool && t.user_pool.limit));
        kvRow(turnDl, "Demo（24h 窗口/日上限）",
              fmtNum(t.demo && t.demo.total) + " / " + fmtNum(t.demo && t.demo.limit));
        kvRow(turnDl, "owner 保留池（用/保留）",
              fmtNum(t.owner && t.owner.total) + " / " + fmtNum(t.owner && t.owner.reserved_limit));
      }
    }

    // 金额余额（billing）
    var billDl = $("adm-ov-billing");
    if (billDl) {
      billDl.textContent = "";
      var b = ov.billing || {};
      if (b.available === false) {
        kvRow(billDl, "可用性", "不可用（" + (b.code || "pg_backend_required") + "）");
      } else {
        kvRow(billDl, "provider 成本合计（本周期）", fmtNano(b.provider_cost_nano_cny));
        kvRow(billDl, "用户 charge 合计（本周期）", fmtNano(b.charge_nano_cny));
        var snap = b.provider_balance_snapshot;
        kvRow(billDl, "DeepSeek 总余额", snap ? fmtNano(snap.total_balance_nano) : "（暂无快照）");
        kvRow(billDl, "余额快照年龄",
              b.provider_balance_age_seconds === null || b.provider_balance_age_seconds === undefined
                ? "—" : Math.round(b.provider_balance_age_seconds) + " 秒前");
        kvRow(billDl, "unpriced 事件数（本周期）", fmtNum(b.unpriced_count));
        kvRow(billDl, "usage 入库滞后（最大/均值）",
              Math.round(b.ingestion_lag_seconds_max || 0) + "s / " +
              Math.round(b.ingestion_lag_seconds_avg || 0) + "s");
      }
    }

    var usageDl = $("adm-ov-usage");
    if (usageDl) {
      usageDl.textContent = "";
      var b2 = ov.billing || {};
      if (b2.available === false) {
        kvRow(usageDl, "可用性", "不可用（" + (b2.code || "pg_backend_required") + "）");
      } else {
        kvRow(usageDl, "模型调用（今日/本周期）",
              fmtNum(b2.model_calls_today) + " / " + fmtNum(b2.model_calls_period));
        kvRow(usageDl, "cache 命中输入 tokens", fmtNum(b2.cache_hit_input_tokens));
        kvRow(usageDl, "cache 未命中输入 tokens", fmtNum(b2.cache_miss_input_tokens));
        kvRow(usageDl, "输出 tokens", fmtNum(b2.output_tokens));
        kvRow(usageDl, "cache 命中率", fmtRatio(b2.cache_hit_ratio));
      }
    }

    // 告警区（unpriced 单独告警，不混入 0 元）
    var alertCard = $("adm-ov-alert-card");
    var alertList = $("adm-ov-alerts");
    if (alertCard && alertList) {
      alertList.textContent = "";
      var b3 = ov.billing || {};
      var alerts = [];
      if (b3.available !== false) {
        if (Number(b3.unpriced_count) > 0) {
          alerts.push("本周期有 " + b3.unpriced_count + " 条 unpriced 事件（未计价 ≠ 0 元，见「额度与账单」页告警区）");
        }
        var maxLag = Number(b3.ingestion_lag_seconds_max || 0);
        if (maxLag > 900) {
          alerts.push("usage 入库滞后最大 " + Math.round(maxLag) + "s（>15 分钟，检查 HistoPilot outbox 投递）");
        }
        var snapAge = b3.provider_balance_age_seconds;
        if (b3.provider_balance_snapshot && snapAge !== null && snapAge !== undefined
            && Number(snapAge) > 86400) {
          alerts.push("DeepSeek 余额快照已超过 24 小时未更新（" +
                      Math.round(Number(snapAge) / 3600) + " 小时）");
        }
      }
      alertCard.hidden = alerts.length === 0;
      alerts.forEach(function (text) {
        var li = document.createElement("li");
        li.textContent = text;
        alertList.appendChild(li);
      });
    }
  }

  // ------------------------------------------------------------------
  // 用户（§10.2 只读）
  // ------------------------------------------------------------------
  function resetLists() {
    state.cursors = { users: null, usage: null, unpriced: null, ledger: null,
                      audit: null, acqUsers: null };
    ["adm-users-tbody", "adm-usage-tbody", "adm-unpriced-tbody",
     "adm-ledger-tbody", "adm-audit-tbody",
     "adm-acq-funnel-tbody", "adm-acq-users-tbody"].forEach(function (id) {
      var el = $(id);
      if (el) el.textContent = "";
    });
  }

  function loadUsers(append) {
    var f = state.filters.users || {};
    var payload = {
      limit: 50,
      cursor: append ? state.cursors.users : null,
    };
    if (f.q) payload.q = f.q;
    if (f.enabled === "true" || f.enabled === "false") payload.enabled = f.enabled === "true";
    if (f.ai === "true" || f.ai === "false") payload.ai_access = f.ai === "true";
    var status = $("adm-users-status");
    request("admin.users.list", payload).then(function (res) {
      hideError();
      renderUsers(res.items || [], append);
      state.cursors.users = res.next_cursor || null;
      var more = $("adm-users-more-btn");
      if (more) more.disabled = !res.next_cursor;
      if (status) status.textContent = res.next_cursor ? "还有更多" : "已到底";
    }).catch(function (err) { handleErr(err, status); });
  }

  function renderUsers(items, append) {
    var tbody = $("adm-users-tbody");
    if (!tbody) return;
    if (!append) tbody.textContent = "";
    items.forEach(function (u) {
      var tr = document.createElement("tr");
      tr.appendChild(td(u.display_name));
      tr.appendChild(td(u.login_id_masked));
      tr.appendChild(td(u.role));
      tr.appendChild(td(u.enabled ? "启用" : "禁用"));
      tr.appendChild(td(u.ai_access ? "是" : "否"));
      tr.appendChild(td(fmtTs(u.created_at)));
      tr.appendChild(td(u.registration_method));
      tr.appendChild(td(u.turn_used === null || u.turn_used === undefined
                       ? "—" : (u.turn_used + " / " + u.turn_limit)));
      // 金额余额：null = 尚未开户（不显示 0）
      tr.appendChild(td(u.billing ? fmtNano(u.billing.balance_nano) : "未开户"));
      tr.appendChild(td(u.billing
                       ? ((u.billing.soft_spend_cap_nano === null ? "—" : fmtNano(u.billing.soft_spend_cap_nano)) +
                          " / " +
                          (u.billing.hard_spend_cap_nano === null ? "—" : fmtNano(u.billing.hard_spend_cap_nano)))
                       : "—"));
      tr.appendChild(td(fmtTs(u.last_ai_call_at)));
      tbody.appendChild(tr);
    });
  }

  // ------------------------------------------------------------------
  // 邀请与来源（§10.3 只读部分，PR4）：注册模式 + campaign 漏斗 +
  // 用户来源明细（first/last touch 分列）。邀请创建/撤销按钮属 PR5。
  // ------------------------------------------------------------------
  function loadAcquisitionPage() {
    loadAcqModeCard();
    loadAcqFunnel();
    loadAcqUsers(false);
  }

  function loadAcqModeCard() {
    // 注册模式随漏斗汇总一并返回；失败时单独给出错误卡内容
    var dl = $("adm-acq-mode");
    if (!dl) return;
    dl.textContent = "";
    kvRow(dl, "可用性", "等待漏斗汇总加载…");
  }

  function renderAcqMode(mode) {
    var dl = $("adm-acq-mode");
    if (!dl) return;
    dl.textContent = "";
    kvRow(dl, "当前模式", mode || "—");
  }

  function loadAcqFunnel() {
    request("admin.acquisition.summary", {}).then(function (res) {
      hideError();
      renderAcqMode(res.registration_mode);
      var tbody = $("adm-acq-funnel-tbody");
      if (tbody) {
        tbody.textContent = "";
        (res.items || []).forEach(function (row) {
          var tr = document.createElement("tr");
          tr.appendChild(td(row.source_code));
          tr.appendChild(td(row.campaign_id));
          tr.appendChild(td(row.campaign_id === null ? "—" : (row.campaign_status || "—")));
          tr.appendChild(td(fmtNum(row.visits)));
          tr.appendChild(td(fmtNum(row.visitors)));
          tr.appendChild(td(fmtNum(row.registrations)));
          tr.appendChild(td(fmtNum(row.first_ai_count)));
          tbody.appendChild(tr);
        });
      }
      var totals = $("adm-acq-totals");
      if (totals) {
        totals.textContent = "";
        var t = res.totals || {};
        kvRow(totals, "合计", "访问 " + fmtNum(t.visits) + " · 注册 " +
              fmtNum(t.registrations) + " · 首次 AI " + fmtNum(t.first_ai_count));
      }
    }).catch(function (err) {
      var dl = $("adm-acq-mode");
      if (dl) { dl.textContent = ""; kvRow(dl, "可用性", errText(err)); }
      handleErr(err, null);
    });
  }

  // 触点列：source/campaign + 时间（visitor 只给 hash 前缀；无完整 IP/query）
  function acqTouchText(touch) {
    if (!touch) return "—（无触点）";
    var label = (touch.source_code || "?") +
      (touch.campaign_id ? (" / " + touch.campaign_id) : "");
    return label + "（" + fmtTs(touch.touched_at) + "）";
  }

  function loadAcqUsers(append) {
    var payload = { limit: 50, cursor: append ? state.cursors.acqUsers : null };
    var status = $("adm-acq-status");
    request("admin.acquisition.list", payload).then(function (res) {
      hideError();
      var tbody = $("adm-acq-users-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      (res.items || []).forEach(function (u) {
        var tr = document.createElement("tr");
        tr.appendChild(td(u.display_name));
        tr.appendChild(td(u.login_id_masked));
        tr.appendChild(td(u.source_code));
        tr.appendChild(td(u.campaign_id));
        tr.appendChild(td(u.attribution_method));
        tr.appendChild(td(acqTouchText(u.first_touch)));
        tr.appendChild(td(acqTouchText(u.last_touch)));
        tr.appendChild(td(fmtTs(u.attributed_at)));
        tbody.appendChild(tr);
      });
      state.cursors.acqUsers = res.next_cursor || null;
      var more = $("adm-acq-more-btn");
      if (more) more.disabled = !res.next_cursor;
      if (status) status.textContent = res.next_cursor ? "还有更多" : "已到底";
    }).catch(function (err) { handleErr(err, status); });
  }

  // ------------------------------------------------------------------
  // 额度与账单（§10.4 只读部分）
  // ------------------------------------------------------------------
  function loadBillingPage() {
    loadTurnBudgetCard();
    loadProviderBalanceCard();
    loadUsage(false);
    loadUnpriced(false);
    loadLedger(false);
  }

  function loadTurnBudgetCard() {
    var dl = $("adm-billing-turn");
    request("admin.turnBudgets.get", {}).then(function (res) {
      hideError();
      if (!dl) return;
      dl.textContent = "";
      var period = res.period || {};
      var usage = res.usage || {};
      kvRow(dl, "周期", "#" + fmtNum(period.id) + "（" + fmtTs(period.started_at) + " 起）");
      kvRow(dl, "平台（用/上限）",
            fmtNum(usage.platform && usage.platform.total) + " / " +
            fmtNum(usage.platform && usage.platform.limit));
      kvRow(dl, "用户共享池（用/上限）",
            fmtNum(usage.user_pool && usage.user_pool.total) + " / " +
            fmtNum(usage.user_pool && usage.user_pool.limit));
      kvRow(dl, "用户单人额度上限", fmtNum(res.limits && res.limits.user_turn_limit));
      kvRow(dl, "Demo（24h 窗口/日上限）",
            fmtNum(usage.demo && usage.demo.total) + " / " + fmtNum(usage.demo && usage.demo.limit));
      kvRow(dl, "owner 保留池（用/保留）",
            fmtNum(usage.owner && usage.owner.total) + " / " +
            fmtNum(usage.owner && usage.owner.reserved_limit));
      var per = (usage.per_user || []).length;
      kvRow(dl, "本周期有用量的用户数", fmtNum(per));
    }).catch(function (err) {
      if (dl) { dl.textContent = ""; kvRow(dl, "可用性", errText(err)); }
      else handleErr(err, null);
    });
  }

  function renderProviderBalance(payload) {
    var dl = $("adm-billing-provider");
    if (!dl) return;
    dl.textContent = "";
    var snap = payload.snapshot;
    kvRow(dl, "供应商", payload.provider);
    if (!snap) {
      kvRow(dl, "最新快照", "（暂无；可点下方按钮抓取）");
      return;
    }
    kvRow(dl, "总余额", fmtNano(snap.total_balance_nano));
    kvRow(dl, "赠送余额", fmtNano(snap.granted_balance_nano));
    kvRow(dl, "充值余额", fmtNano(snap.topped_up_balance_nano));
    kvRow(dl, "可用状态", snap.is_available ? "可用" : "不可用");
    kvRow(dl, "快照时间", fmtTs(snap.observed_at));
    kvRow(dl, "快照年龄",
          payload.age_seconds === null || payload.age_seconds === undefined
          ? "—" : Math.round(payload.age_seconds) + " 秒前");
  }

  function loadProviderBalanceCard() {
    request("admin.billing.providerBalance.get", {}).then(function (payload) {
      hideError();
      renderProviderBalance(payload);
    }).catch(function (err) {
      var dl = $("adm-billing-provider");
      if (dl) { dl.textContent = ""; kvRow(dl, "可用性", errText(err)); }
      else handleErr(err, null);
    });
  }

  function refreshProviderBalance() {
    var btn = $("adm-balance-refresh-btn");
    var status = $("adm-balance-refresh-status");
    if (btn) btn.disabled = true;
    if (status) status.textContent = "抓取中…";
    request("admin.billing.providerBalance.refresh", {}).then(function (res) {
      if (btn) btn.disabled = false;
      if (status) status.textContent = "已更新（" + fmtTs(res.snapshot && res.snapshot.observed_at) + "）";
      renderProviderBalance({
        provider: res.provider,
        snapshot: res.snapshot,
        age_seconds: res.age_seconds,
      });
    }).catch(function (err) {
      if (btn) btn.disabled = false;
      if (status) status.textContent = errText(err);
    });
  }

  function loadUsage(append) {
    var f = state.filters.usage || {};
    var payload = { limit: 50, cursor: append ? state.cursors.usage : null };
    if (f.model) payload.model = f.model;
    if (f.user_id) payload.user_id = f.user_id;
    if (f.status) payload.status = f.status;
    var status = $("adm-usage-status");
    request("admin.billing.usage.list", payload).then(function (res) {
      hideError();
      renderUsage(res.items || [], append);
      state.cursors.usage = res.next_cursor || null;
      var more = $("adm-usage-more-btn");
      if (more) more.disabled = !res.next_cursor;
      if (status) status.textContent = res.next_cursor ? "还有更多" : "已到底";
    }).catch(function (err) { handleErr(err, status); });
  }

  function renderUsage(items, append) {
    var tbody = $("adm-usage-tbody");
    if (!tbody) return;
    if (!append) tbody.textContent = "";
    items.forEach(function (e) {
      var tr = document.createElement("tr");
      tr.appendChild(td(fmtTs(e.occurred_at)));
      tr.appendChild(td(e.model));
      tr.appendChild(td(e.subject_type + (e.user_id ? "" : "（无用户镜像）")));
      tr.appendChild(td(e.status === "priced" ? "priced" : ("unpriced：" + (e.unpriced_reason || "?"))));
      tr.appendChild(td(fmtNum(e.cache_hit_input_tokens) + " / " +
                        fmtNum(e.cache_miss_input_tokens) + " / " +
                        fmtNum(e.output_tokens)));
      // unpriced 金额保持 null 呈现「—」，绝不显示成 0 元（§10.4 红线）
      tr.appendChild(td(e.provider_cost_nano_cny === null ? "—" : String(e.provider_cost_nano_cny)));
      tr.appendChild(td(e.charge_nano_cny === null ? "—" : String(e.charge_nano_cny)));
      tr.appendChild(td(e.event_id));
      tbody.appendChild(tr);
    });
  }

  function loadUnpriced(append) {
    var payload = { limit: 50, status: "unpriced", cursor: append ? state.cursors.unpriced : null };
    request("admin.billing.usage.list", payload).then(function (res) {
      var card = $("adm-unpriced-card");
      var tbody = $("adm-unpriced-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      (res.items || []).forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.occurred_at)));
        tr.appendChild(td(e.model));
        tr.appendChild(td(e.unpriced_reason));
        tr.appendChild(td(e.event_id));
        tbody.appendChild(tr);
      });
      state.cursors.unpriced = res.next_cursor || null;
      var more = $("adm-unpriced-more-btn");
      if (more) more.disabled = !res.next_cursor;
      if (card) card.hidden = false; // 有无条目都显示告警区（明确「当前 0 条」）
      if (!res.next_cursor && !res.items) tbody.textContent = "";
    }).catch(function () {
      // json 后端：告警区静默隐藏（概览页已给出 pg_backend_required 说明）
      var card = $("adm-unpriced-card");
      if (card) card.hidden = true;
    });
  }

  function loadLedger(append) {
    var payload = { limit: 50, cursor: append ? state.cursors.ledger : null };
    request("admin.billing.ledger.list", payload).then(function (res) {
      hideError();
      var tbody = $("adm-ledger-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      (res.items || []).forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.created_at)));
        tr.appendChild(td(e.account_id));
        tr.appendChild(td(e.kind));
        tr.appendChild(td(e.amount_nano_cny));
        tr.appendChild(td(e.reason));
        tr.appendChild(td(e.entry_id));
        tbody.appendChild(tr);
      });
      state.cursors.ledger = res.next_cursor || null;
      var more = $("adm-ledger-more-btn");
      if (more) more.disabled = !res.next_cursor;
    }).catch(function (err) { handleErr(err, null); });
  }

  // ------------------------------------------------------------------
  // 审计（§10.5）
  // ------------------------------------------------------------------
  function loadAudit(append) {
    var f = state.filters.audit || {};
    var payload = { limit: 50, cursor: append ? state.cursors.audit : null };
    if (f.action) payload.action = f.action;
    var status = $("adm-audit-status");
    request("admin.audit.list", payload).then(function (res) {
      hideError();
      var tbody = $("adm-audit-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      (res.items || []).forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.ts)));
        tr.appendChild(td((e.actor_role || "") + (e.actor_user_id ? ("·" + e.actor_user_id) : "")));
        tr.appendChild(td(e.action));
        tr.appendChild(td((e.target_type || "") + (e.target_id ? ("·" + e.target_id) : "")));
        tr.appendChild(td(JSON.stringify(e.detail || {})));
        tbody.appendChild(tr);
      });
      state.cursors.audit = res.next_cursor || null;
      var more = $("adm-audit-more-btn");
      if (more) more.disabled = !res.next_cursor;
      if (status) status.textContent = res.next_cursor ? "还有更多" : "已到底";
    }).catch(function (err) { handleErr(err, status); });
  }

  // ------------------------------------------------------------------
  // 内存态导航（opaque origin：不用 location.hash，避免任何存储型状态）
  // ------------------------------------------------------------------
  function showPage(name) {
    state.page = name;
    Object.keys(els.pages).forEach(function (key) {
      if (els.pages[key]) els.pages[key].hidden = key !== name;
    });
    if (els.nav && els.nav.querySelectorAll) {
      var buttons = els.nav.querySelectorAll(".adm-nav-btn");
      for (var i = 0; i < buttons.length; i++) {
        var on = buttons[i].getAttribute("data-page") === name;
        buttons[i].className = on ? "adm-nav-btn adm-nav-btn--active" : "adm-nav-btn";
      }
    }
    hideError();
    if (name === "overview") loadOverview();
    else if (name === "users") loadUsers(false);
    else if (name === "invites") loadAcquisitionPage();
    else if (name === "billing") loadBillingPage();
    else if (name === "audit") loadAudit(false);
  }

  function bindNav() {
    if (els.nav && els.nav.addEventListener) {
      els.nav.addEventListener("click", function (ev) {
        var btn = ev.target && ev.target.closest ? ev.target.closest(".adm-nav-btn") : null;
        if (!btn) return;
        var name = btn.getAttribute("data-page");
        if (name) showPage(name);
      });
    }
    function onClick(id, handler) {
      var el = $(id);
      if (el) el.addEventListener("click", handler);
    }
    // 用户页筛选
    onClick("adm-users-search-btn", function () {
      state.filters.users = {
        q: ($("adm-users-q") && $("adm-users-q").value || "").trim(),
        enabled: $("adm-users-enabled") ? $("adm-users-enabled").value : "",
        ai: $("adm-users-ai") ? $("adm-users-ai").value : "",
      };
      state.cursors.users = null;
      loadUsers(false);
    });
    onClick("adm-users-more-btn", function () { loadUsers(true); });
    // 邀请与来源页
    onClick("adm-acq-more-btn", function () { loadAcqUsers(true); });
    // 账单页
    onClick("adm-balance-refresh-btn", refreshProviderBalance);
    onClick("adm-usage-search-btn", function () {
      state.filters.usage = {
        model: ($("adm-usage-model") && $("adm-usage-model").value || "").trim(),
        user_id: ($("adm-usage-user") && $("adm-usage-user").value || "").trim(),
        status: $("adm-usage-status") ? $("adm-usage-status").value : "",
      };
      state.cursors.usage = null;
      loadUsage(false);
    });
    onClick("adm-usage-more-btn", function () { loadUsage(true); });
    onClick("adm-unpriced-more-btn", function () { loadUnpriced(true); });
    onClick("adm-ledger-more-btn", function () { loadLedger(true); });
    // 审计页
    onClick("adm-audit-search-btn", function () {
      state.filters.audit = {
        action: ($("adm-audit-action") && $("adm-audit-action").value || "").trim(),
      };
      state.cursors.audit = null;
      loadAudit(false);
    });
    onClick("adm-audit-more-btn", function () { loadAudit(true); });
  }

  window.addEventListener("message", onMessage);
  bindNav();

  // 导出（仅调试/测试用；不含 nonce 读取器）
  window.PathTogetherAdminClient = {
    request: request,
    showPage: showPage,
    handshakeState: function () {
      return {
        ready: !!state.nonce && !state.dead,
        protocolVersion: state.protocolVersion,
        grantedCount: state.granted.length,
      };
    },
  };
})();
