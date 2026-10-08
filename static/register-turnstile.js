/* 注册弹窗 Cloudflare Turnstile 按需加载器（2026-10-08 设计 §5/§7）。
 *
 * 职责（全部为展示/交互层，服务端 Siteverify 才是权威校验）：
 *  - 按需加载：仅当注册视图真正可见（弹窗打开且 #register-view 未隐藏）且
 *    存在 #register-turnstile 容器时，才动态加载 challenges.cloudflare.com
 *    的 api.js?render=explicit；打开登录视图绝不加载；整个生命周期只加载
 *    一次（失败后允许「重试」重新发起）。
 *  - 显式渲染：turnstile.render(container, {sitekey, action, theme:'auto',
 *    language, size:'flexible', callback, expired/error/timeout-callback})；
 *    language 跟随当前 UI 语言（zh→zh-cn / en→en）。过期/超时/出错 → 清空
 *    本地 token 并 reset（token 一次一用，5 分钟有效）。
 *  - 提交护栏：document 捕获阶段拦截 submit——表单带 #register-turnstile
 *    但还没有 token 时 preventDefault 并就地给中性提示（不发任何网络请求，
 *    绝不说「机器人」）；token 已就绪则放行（双击防护由 entry-auth.js 武装，
 *    故此处 preventDefault 后 entry-auth 会因 defaultPrevented 跳过武装）。
 *  - 加载失败 / 10s 未就绪 / error-callback：中性提示 + 「重试」按钮（重新
 *    加载渲染）+ 求助链接（/registration-help?reason=challenge）。
 *  - 重置时机：弹窗重新打开/切回注册视图、pageshow 自 bfcache 恢复、容器
 *    被服务端重渲染替换（重渲染新 widget）。
 *  - form_locale：初始与 hp-lang-change 时把所有 input[name=form_locale]
 *    同步为当前 UI 语言（仅 zh|en；服务端仍按白名单兜底）。
 *  - 倒计时（仅展示；服务端权威）：#register-resend-countdown 与
 *    #register-resend-form[data-resend-at] 的「X:YY 后可重新发送」，归零前
 *    禁用重发按钮。剩余时间每 tick 按 Date.now 重算（后台标签页节流定时器
 *    也不会落后于服务端），visibilitychange / pageshow 回前台立即校正；
 *    重发 widget 归零可用时才渲染（token 5 分钟过期，早渲染会在可提交前
 *    失效），倒计时未结束前容器收起、不预留高度。
 *    #register-resume-countdown[data-resume-at] 显示恢复自助发送的本地时间
 *    （非当天带日期，跟随 UI 语言，hp-lang-change 重渲染）。
 *  - 渲染前占位：表单容器在 widget 渲染成功前显示一行中性小字
 *    「正在加载安全验证…」（不预留 widget 高度）；渲染成功/失败让位于
 *    widget 本体或不可用提示。
 *
 * 本文件不含任何内联事件/内联样式（CSP style-src/script-src 'self'）。
 */
(function () {
  'use strict';

  var TURNSTILE_API_SRC =
    'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  var LOAD_TIMEOUT_MS = 10000;

  var state = {
    loadPromise: null,   // api.js 加载中的 Promise（成功后缓存）
    loadFailed: false,   // 上次加载失败（显示不可用提示 + 重试）
    widgetId: null,      // turnstile.render 返回的 widget id
    container: null,     // 当前 widget 渲染进的容器元素
    token: '',           // 最近一次 callback 的 token（提交护栏用）
    noticeEl: null,      // 就地提示元素（.register-turnstile-notice）
    placeholderEl: null  // 渲染前占位元素（.register-turnstile-placeholder）
  };

  /* ---------------- i18n 小工具（缺 HP_I18N 时用模板同款中文兜底） -------- */

  function t(key, fallback) {
    try {
      var s = window.HP_I18N && window.HP_I18N.t(key);
      return (s && s !== key) ? s : fallback;
    } catch (e) { return fallback; }
  }

  function uiLang() {
    try {
      return (window.HP_I18N && window.HP_I18N.getLang() === 'en') ? 'en' : 'zh';
    } catch (e) { return 'zh'; }
  }

  function turnstileLanguage() {
    return uiLang() === 'en' ? 'en' : 'zh-cn';
  }

  /* ---------------- DOM 定位 ---------------- */

  function container() {
    return document.getElementById('register-turnstile');
  }

  function registerPaneVisible() {
    var pane = document.getElementById('register-view');
    if (!pane || pane.hidden) return false;
    var dialog = document.getElementById('login-dialog');
    if (dialog && !dialog.open && !dialog.hasAttribute('open')) return false;
    return true;
  }

  function nowSeconds() {
    return Math.floor(Date.now() / 1000);
  }

  /* ---------------- api.js 按需加载（只加载一次；失败可重试） ------------- */

  function loadApi(forceReload) {
    if (window.turnstile && typeof window.turnstile.render === 'function') {
      state.loadFailed = false;
      return Promise.resolve(window.turnstile);
    }
    if (state.loadPromise && !forceReload) return state.loadPromise;
    state.loadFailed = false;
    var attempt = new Promise(function (resolve, reject) {
      var timer = window.setTimeout(function () {
        reject(new Error('turnstile-load-timeout'));
      }, LOAD_TIMEOUT_MS);
      var script = document.createElement('script');
      script.setAttribute('src', TURNSTILE_API_SRC);
      script.async = true;
      script.onerror = function () {
        window.clearTimeout(timer);
        reject(new Error('turnstile-load-error'));
      };
      script.onload = function () {
        if (window.turnstile &&
            typeof window.turnstile.render === 'function') {
          window.clearTimeout(timer);
          resolve(window.turnstile);
        } // onload 先到、api.js 尚未就绪：交给超时/轮询路径，不提前 resolve
      };
      (document.head || document.documentElement).appendChild(script);
    });
    // 失败清缓存：下次「重试」重新注入 script（网络恢复后可成功）
    state.loadPromise = attempt.catch(function (err) {
      state.loadPromise = null;
      throw err;
    });
    return state.loadPromise;
  }

/* ---------------- 就地提示（中性措辞；绝不说「机器人」） ---------------- */

function removeNotice() {
  if (state.noticeEl && state.noticeEl.parentNode &&
      typeof state.noticeEl.parentNode.removeChild === 'function') {
    state.noticeEl.parentNode.removeChild(state.noticeEl);
  }
  state.noticeEl = null;
}

function showNotice(box, message, withRetry) {
  removePlaceholder(); // 提示与占位不并存
  var n = state.noticeEl;
    if (!n || n.parentNode !== box.parentNode) {
      removeNotice();
      n = document.createElement('div');
      n.className = 'register-turnstile-notice';
      n.setAttribute('role', 'status');
      if (box.parentNode && box.parentNode.insertBefore) {
        box.parentNode.insertBefore(n, box.nextSibling);
      } else if (box.parentNode) {
        box.parentNode.appendChild(n);
      }
      state.noticeEl = n;
    }
    // 重建子节点（textContent 逐个建，无 innerHTML）
    while (n.firstChild) n.removeChild(n.firstChild);
    var msg = document.createElement('span');
    msg.className = 'register-turnstile-notice-text';
    msg.textContent = message;
    n.appendChild(msg);
    if (withRetry) {
      var retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'register-turnstile-retry';
      retry.textContent = t('register.turnstile.retry', '重试');
      retry.addEventListener('click', function () { retryLoad(); });
      n.appendChild(retry);
      var help = document.createElement('a');
      help.className = 'register-turnstile-notice-help';
      help.setAttribute('href', '/registration-help?reason=challenge');
      help.textContent = t('register.help.link', '注册遇到问题？给作者发邮件');
      n.appendChild(help);
    }
    return n;
  }

  function showUnavailable(box) {
    showNotice(box,
      t('register.turnstile.unavailable', '安全验证暂时无法加载，请检查网络后重试'),
      true);
  }

  function showPending(box) {
    showNotice(box,
      t('register.turnstile.pending', '请先完成下方的安全验证'), false);
  }

  /* ---------------- 渲染前占位（小号中性一行；不预留 widget 高度） ---------- */

  function removePlaceholder() {
    if (state.placeholderEl && state.placeholderEl.parentNode &&
        typeof state.placeholderEl.parentNode.removeChild === 'function') {
      state.placeholderEl.parentNode.removeChild(state.placeholderEl);
    }
    state.placeholderEl = null;
  }

  function showPlaceholder(box) {
    if (state.placeholderEl &&
        state.placeholderEl.parentNode === box.parentNode) {
      return; // 已显示
    }
    removePlaceholder();
    var n = document.createElement('div');
    n.className = 'register-turnstile-placeholder';
    n.setAttribute('role', 'status');
    n.textContent = t('register.turnstile.loading', '正在加载安全验证…');
    if (box.parentNode && box.parentNode.insertBefore) {
      box.parentNode.insertBefore(n, box.nextSibling);
    } else if (box.parentNode) {
      box.parentNode.appendChild(n);
    }
    state.placeholderEl = n;
  }

  /* ---------------- 渲染 / 重置 ---------------- */

  function disposeWidget() {
    if (state.widgetId != null && window.turnstile &&
        typeof window.turnstile.remove === 'function') {
      try { window.turnstile.remove(state.widgetId); } catch (e) { /* 已失效 */ }
    }
    state.widgetId = null;
    state.container = null;
    state.token = '';
  }

  function resetWidget() {
    state.token = '';
    if (state.widgetId != null && window.turnstile &&
        typeof window.turnstile.reset === 'function') {
      try { window.turnstile.reset(state.widgetId); } catch (e) { /* 已失效 */ }
    }
  }

  function render(box) {
    if (!window.turnstile || typeof window.turnstile.render !== 'function') {
      return;
    }
    // 同一容器已渲染过：不重复渲染（reset 走 resetWidget）
    if (state.widgetId != null && state.container === box) return;
    disposeWidget();
    state.token = '';
    state.widgetId = window.turnstile.render(box, {
      sitekey: box.getAttribute('data-sitekey') || '',
      action: box.getAttribute('data-action') || undefined,
      size: 'flexible',
      theme: 'auto',
      language: turnstileLanguage(),
      callback: function (token) {
        state.token = String(token || '');
        removeNotice();
        removePlaceholder();
      },
      'expired-callback': function () { resetWidget(); },
      'timeout-callback': function () { resetWidget(); },
      'error-callback': function () {
        resetWidget();
        removePlaceholder();
        showUnavailable(box);
        return true; // 抑制 turnstile 向 console 抛未处理错误
      }
    });
    state.container = box;
    removePlaceholder(); // 渲染成功：撤掉占位，widget 取自然尺寸
  }

  /* 重发表单倒计时未结束：暂不渲染重发 widget（token 5 分钟过期，等按钮
     可用再渲染，避免用户还没能提交挑战就已失效）。容器不占位、无预留高度。
     状态由 startResendCountdown 设置/清除（与按钮启用一致）。 */
  var resendWaitActive = false;

  function resendFormPending() {
    return resendWaitActive;
  }

  function onRegisterShown() {
    var box = container();
    if (!box || !registerPaneVisible()) return;
    if (resendWaitActive) return; // 倒计时中：容器收起，无占位无渲染
    if (state.loadFailed) { showUnavailable(box); return; }
    if (state.widgetId != null && state.container === box) {
      resetWidget(); // 重新显示：token 可能已过期/被消费，重置拿新挑战
      return;
    }
    showPlaceholder(box);
    loadApi(false).then(function () {
      var current = container();
      if (!current || !registerPaneVisible()) return;
      render(current);
    }).catch(function () {
      state.loadFailed = true;
      removePlaceholder();
      var current = container();
      if (current) showUnavailable(current);
    });
  }

  function retryLoad() {
    var box = container();
    removeNotice();
    removePlaceholder();
    state.loadFailed = false;
    if (!box) return;
    showPlaceholder(box);
    loadApi(true).then(function () {
      var current = container();
      if (current && registerPaneVisible()) {
        disposeWidget();
        render(current);
      }
    }).catch(function () {
      state.loadFailed = true;
      removePlaceholder();
      var current = container();
      if (current) showUnavailable(current);
    });
  }

  /* ---------------- 提交护栏（捕获阶段；无 token 不发请求） --------------- */

  function onDocumentSubmitCapture(ev) {
    var form = ev.target;
    if (!form || typeof form.querySelector !== 'function') return;
    var box = form.querySelector('#register-turnstile');
    if (!box) return;                    // 非注册/重发表单
    if (state.token) return;             // token 就绪 → 放行
    ev.preventDefault();
    if (state.loadFailed) showUnavailable(box);
    else showPending(box);
  }

  /* ---------------- form_locale 同步（初始 + 语言切换） ------------------- */

  function syncFormLocales() {
    var lang = uiLang();
    if (document.querySelectorAll) {
      document.querySelectorAll('input[name="form_locale"]')
        .forEach(function (input) { input.value = lang; });
    }
  }

  /* ---------------- 倒计时（仅展示；服务端权威） ------------------------- */

  var resendAt = 0;            // 重发可用时间（unix 秒）
  var resendSpan = null;       // cooldown 态的倒计时 span
  var resendBtn = null;        // 重发表单提交按钮
  var resendBox = null;        // 重发表单内的 widget 容器
  var resendOriginalLabel = '';
  var resendTickPending = false;

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function formatClock(totalSeconds) {
    var s = Math.max(0, totalSeconds);
    return Math.floor(s / 60) + ':' + pad2(s % 60);
  }

  function startResendCountdown() {
    var span = document.getElementById('register-resend-countdown');
    var form = document.getElementById('register-resend-form');
    resendAt = parseInt((span && span.getAttribute('data-resend-at')) ||
      (form && form.getAttribute('data-resend-at')) || '0', 10);
    if (!resendAt) return;
    resendSpan = span;
    resendBtn = (form && form.querySelector) ?
      form.querySelector('button[type="submit"]') : null;
    resendBox = (form && form.querySelector) ?
      form.querySelector('#register-turnstile') : null;
    resendOriginalLabel = resendBtn ? resendBtn.textContent : '';
    resendWaitActive = resendAt > nowSeconds();
    tickResend();
  }

  /* 每次按真实时钟重算剩余秒数：后台标签页会节流 setTimeout（切去邮件
     客户端等场景），递减计数会落后于服务端要求；重算保证回到前台/任意
     延迟的 tick 都显示并恢复到与服务器一致的状态。visibilitychange /
     pageshow 再补一次立即刷新。 */
  function tickResend() {
    if (!resendWaitActive) return;
    var left = Math.max(0, resendAt - nowSeconds());
    if (left <= 0) { finishResend(); return; }
    var label = t('register.state.resend.wait', '{time} 后可重新发送')
      .replace('{time}', formatClock(left));
    if (resendSpan) resendSpan.textContent = label;
    if (resendBtn) {
      resendBtn.disabled = true;
      resendBtn.textContent = label;
    }
    scheduleResendTick();
  }

  function scheduleResendTick() {
    if (resendTickPending || !resendWaitActive) return;
    resendTickPending = true;
    window.setTimeout(function () {
      resendTickPending = false;
      tickResend();
    }, 1000);
  }

  function finishResend() {
    resendWaitActive = false;
    if (resendSpan) resendSpan.textContent = '';
    if (resendBtn) {
      resendBtn.disabled = false;
      resendBtn.textContent = resendOriginalLabel;
    }
    // 按钮可用时才渲染重发 widget（token 5 分钟有效）
    if (resendBox && registerPaneVisible()) onRegisterShown();
  }

  function startResumeNote() {
    var span = document.getElementById('register-resume-countdown');
    if (!span) return;
    var at = parseInt(span.getAttribute('data-resume-at') || '0', 10);
    if (!at) return;
    span.textContent =
      t('register.state.limit.resume', '预计 {time}（本地时间）后可再次自助发送')
        .replace('{time}', formatResumeTime(at));
  }

  /* 恢复时间展示：当天只显示 HH:MM；非当天补充日期（跟随 UI 语言的
     Intl 格式，如 “10月9日 12:35” / “Oct 9, 12:35”）。 */
  function formatResumeTime(atSeconds) {
    var d = new Date(atSeconds * 1000);
    var locale = uiLang() === 'en' ? 'en' : 'zh-CN';
    var time = formatIntl(d, locale, { hour: '2-digit', minute: '2-digit' });
    var now = new Date();
    var sameDay = d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
    if (sameDay) return time;
    var date = formatIntl(d, locale, { month: 'short', day: 'numeric' });
    return date + ' ' + time;
  }

  function formatIntl(d, locale, opts) {
    try {
      return new Intl.DateTimeFormat(locale, opts).format(d);
    } catch (e) {
      // 无 Intl 的环境退回本地时间 HH:MM（日期不补，避免硬编码错格式）
      return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
    }
  }

  /* ---------------- 初始化 ---------------- */

  function onViewEvent(ev) {
    var detail = (ev && ev.detail) || {};
    if (detail.view === 'register' && detail.open) onRegisterShown();
  }

  function onPageShow(ev) {
    syncFormLocales();
    if (ev && ev.persisted) {
      // bfcache 恢复：token/挑战可能早已失效
      resetWidget();
    }
    // bfcache 恢复后计时器状态未知：立即按真实时钟校正倒计时
    if (resendWaitActive) tickResend();
    if (registerPaneVisible()) onRegisterShown();
  }

  function onVisibilityChange() {
    // 后台标签页定时器被节流：回前台立即按真实时钟校正一次
    if (resendWaitActive) tickResend();
  }

  function onLangChange() {
    syncFormLocales();
    startResumeNote(); // 恢复时间格式跟随 UI 语言
    if (state.placeholderEl) {
      state.placeholderEl.textContent =
        t('register.turnstile.loading', '正在加载安全验证…');
    }
  }

  function init() {
    // 捕获阶段：先于 entry-auth.js 的双击防护（后者 defaultPrevented 时跳过）
    document.addEventListener('submit', onDocumentSubmitCapture, true);
    document.addEventListener('hp-auth-view', onViewEvent);
    window.addEventListener('pageshow', onPageShow);
    document.addEventListener('visibilitychange', onVisibilityChange);
    document.addEventListener('hp-lang-change', onLangChange);
    syncFormLocales();
    startResendCountdown();
    startResumeNote();
    if (registerPaneVisible() && !resendFormPending()) onRegisterShown();
  }

  init();

  // 测试/调试钩子（只读+显式操作，不改全局行为）
  window.RegisterTurnstile = {
    getToken: function () { return state.token; },
    reset: resetWidget,
    retry: retryLoad
  };
})();
