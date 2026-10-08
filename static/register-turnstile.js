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
 *    禁用重发按钮；重发 widget 归零可用时才渲染（token 5 分钟过期，早渲染
 *    会在可提交前失效）。#register-resume-countdown[data-resume-at] 显示
 *    恢复自助发送的本地时间。
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
    noticeEl: null       // 就地提示元素（.register-turnstile-notice）
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
      },
      'expired-callback': function () { resetWidget(); },
      'timeout-callback': function () { resetWidget(); },
      'error-callback': function () {
        resetWidget();
        showUnavailable(box);
        return true; // 抑制 turnstile 向 console 抛未处理错误
      }
    });
    state.container = box;
  }

  /* 重发表单倒计时未结束：暂不渲染重发 widget（token 5 分钟过期，等按钮
     可用再渲染，避免用户还没能提交挑战就已失效）。状态由
     startResendCountdown 设置/清除（与按钮启用一致，不做时钟二次推断）。 */
  var resendWaitActive = false;

  function resendFormPending() {
    return resendWaitActive;
  }

  function onRegisterShown() {
    var box = container();
    if (!box || !registerPaneVisible()) return;
    if (resendFormPending()) return; // 倒计时归零后由 startResendCountdown 渲染
    if (state.loadFailed) { showUnavailable(box); return; }
    loadApi(false).then(function () {
      var current = container();
      if (!current || !registerPaneVisible()) return;
      if (state.widgetId != null && state.container === current) {
        resetWidget(); // 重新显示：token 可能已过期/被消费，重置拿新挑战
      } else {
        render(current); // 容器被替换或首次显示（重新）渲染
      }
    }).catch(function () {
      state.loadFailed = true;
      var current = container();
      if (current) showUnavailable(current);
    });
  }

  function retryLoad() {
    var box = container();
    removeNotice();
    state.loadFailed = false;
    if (!box) return;
    loadApi(true).then(function () {
      var current = container();
      if (current && registerPaneVisible()) {
        disposeWidget();
        render(current);
      }
    }).catch(function () {
      state.loadFailed = true;
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

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function formatClock(totalSeconds) {
    var s = Math.max(0, totalSeconds);
    return Math.floor(s / 60) + ':' + pad2(s % 60);
  }

  function startResendCountdown() {
    var span = document.getElementById('register-resend-countdown');
    var form = document.getElementById('register-resend-form');
    var at = parseInt((span && span.getAttribute('data-resend-at')) ||
      (form && form.getAttribute('data-resend-at')) || '0', 10);
    if (!at) return;
    var btn = (form && form.querySelector) ?
      form.querySelector('button[type="submit"]') : null;
    var box = (form && form.querySelector) ?
      form.querySelector('#register-turnstile') : null;
    var originalLabel = btn ? btn.textContent : '';
    var left = Math.max(0, at - nowSeconds());
    resendWaitActive = left > 0;

    function finish() {
      resendWaitActive = false;
      if (span) span.textContent = '';
      if (btn) {
        btn.disabled = false;
        btn.textContent = originalLabel;
      }
      // 按钮可用时才渲染重发 widget（token 5 分钟有效）
      if (box && registerPaneVisible()) onRegisterShown();
    }

    function tick() {
      if (left <= 0) { finish(); return; }
      var label = t('register.state.resend.wait', '{time} 后可重新发送')
        .replace('{time}', formatClock(left));
      if (span) span.textContent = label;
      if (btn) {
        btn.disabled = true;
        btn.textContent = label;
      }
      left -= 1;
      window.setTimeout(tick, 1000);
    }
    tick();
  }

  function startResumeNote() {
    var span = document.getElementById('register-resume-countdown');
    if (!span) return;
    var at = parseInt(span.getAttribute('data-resume-at') || '0', 10);
    if (!at) return;
    var d = new Date(at * 1000);
    var time = pad2(d.getHours()) + ':' + pad2(d.getMinutes());
    span.textContent =
      t('register.state.limit.resume', '预计 {time}（本地时间）后可再次自助发送')
        .replace('{time}', time);
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
    if (registerPaneVisible()) onRegisterShown();
  }

  function init() {
    // 捕获阶段：先于 entry-auth.js 的双击防护（后者 defaultPrevented 时跳过）
    document.addEventListener('submit', onDocumentSubmitCapture, true);
    document.addEventListener('hp-auth-view', onViewEvent);
    window.addEventListener('pageshow', onPageShow);
    document.addEventListener('hp-lang-change', function () {
      syncFormLocales();
    });
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
