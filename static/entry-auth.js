/* 登录 / 注册统一弹窗（entry.html 内嵌 <dialog id="login-dialog">，_login_dialog.html）。
 * - 拦截站内 a[href="/login"]（含 /login?next=…、/login?password_changed=1）与
 *   a[href="/register"]：原地打开弹窗并切到对应视图，不再整页跳转；
 * - 弹窗内双视图（#login-view / #register-view，data-auth-pane）：一次只显示
 *   一个视图；切换保留两个视图的已输入内容、各自错误提示与登录安全 next
 *   （同一 DOM，切换不清空）；aria-labelledby 同步到当前视图标题。
 * - 服务端 login_open / register_open=True（GET/POST /login、/register 深链接、
 *   错误回显）以非模态 open 属性直出弹窗，本脚本初始化时把它升级为
 *   showModal()：获得 top-layer 原生焦点陷阱与 ::backdrop 遮罩；无 JS 时退化
 *   为 CSS 居中浮层（entry.css .login-dialog）。
 * - 打开期间给 body 加 login-dialog-open（CSS 锁背景滚动），关闭移除并还原焦点。
 * - 限流倒计时：服务端渲染 [data-retry-seconds]（登录/注册视图各自 span），
 *   倒计时期间禁用所属表单提交按钮，归零自动恢复（i18n 文案由 i18n.js 全局
 *   data-i18n 扫描覆盖，倒计时数字为语言无关内容，无需重渲染）。
 * - 表单双击防护：提交中禁用提交按钮并吞掉重复 submit（CSP 禁止内联脚本，
 *   防护统一放本文件）。
 */
(function () {
  'use strict';

  var dialog = document.getElementById('login-dialog');
  if (!dialog) return;

  var opener = null;

  function setBodyClass(on) {
    document.body.classList.toggle('login-dialog-open', on);
  }

  /* ---------------- 双视图：登录 / 注册 ---------------- */

  function panes() {
    return {
      login: document.getElementById('login-view'),
      register: document.getElementById('register-view')
    };
  }

  function currentView() {
    var p = panes();
    if (p.register && !p.register.hidden) return 'register';
    return 'login';
  }

  function setView(name, moveFocus) {
    var p = panes();
    ['login', 'register'].forEach(function (key) {
      var pane = p[key];
      if (!pane) return;
      pane.hidden = (key !== name);
    });
    // aria-labelledby 指向当前视图标题（无障碍：可见标题才是弹窗名称）
    var h2 = (p[name] || dialog).querySelector('h2');
    if (h2 && h2.id) dialog.setAttribute('aria-labelledby', h2.id);
    if (moveFocus) {
      var field = (p[name] || dialog).querySelector('input:not([type="hidden"])');
      if (field) field.focus();
    }
  }

  function switchTo(name) {
    if (dialog.open) {
      if (currentView() !== name) setView(name, true);
      return;
    }
    open();
    setView(name, true);
  }

  function open() {
    if (dialog.open) return;
    // activeElement 在浏览器中为当前元素或 null（不依赖 HTMLElement 判断）
    opener = document.activeElement || null;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    setBodyClass(true);
    var field = (document.getElementById(currentView() === 'register'
      ? 'register-view' : 'login-view') || dialog)
      .querySelector('input:not([type="hidden"])');
    if (field) field.focus();
  }

  function close() {
    if (!dialog.open) { afterClose(); return; }
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
    // close 事件与兜底双路径收尾（部分环境 close 事件不触发，afterClose 幂等）
    afterClose();
  }

  function afterClose() {
    setBodyClass(false);
    if (opener && typeof opener.focus === 'function') {
      try { opener.focus(); } catch (e) { /* 触发元素可能已不在 DOM */ }
    }
    opener = null;
  }

  dialog.addEventListener('close', afterClose);
  dialog.querySelectorAll('[data-login-close]').forEach(function (el) {
    el.addEventListener('click', close);
  });
  // 点击背景（dialog 自身即遮罩区域）关闭；ESC 走 cancel → close()
  dialog.addEventListener('click', function (ev) { if (ev.target === dialog) close(); });
  dialog.addEventListener('cancel', function (ev) { ev.preventDefault(); close(); });

  // 拦截 /login 与 /register 站内链接（顶栏「登录」、弹窗内「没有账号？注册」
  // /「已有账号？登录」等）：原地打开弹窗并切到对应视图。
  // 无 JS 时链接照常导航到服务端直开的同一弹窗（深链接兜底）。
  // data-auth-nav = 显式 opt-out：带该属性的链接不拦截，允许真实导航到
  // 服务端深链接（发送成功视图的「重新填写邮箱」——注册视图已是发送成功
  // 态，原地切换等于没动；深链接会重新渲染干净表单 register_done=False）。
  function wantsRealNavigation(link) {
    return typeof link.hasAttribute === 'function' &&
      link.hasAttribute('data-auth-nav');
  }
  Array.prototype.forEach.call(
    document.querySelectorAll('a[href="/login"], a[href^="/login?"]'),
    function (link) {
      if (wantsRealNavigation(link)) return;
      link.addEventListener('click', function (ev) {
        ev.preventDefault();
        switchTo('login');
      });
    });
  Array.prototype.forEach.call(
    document.querySelectorAll('a[href="/register"], a[href^="/register?"]'),
    function (link) {
      if (wantsRealNavigation(link)) return;
      link.addEventListener('click', function (ev) {
        ev.preventDefault();
        switchTo('register');
      });
    });

  // 服务端 open 属性直出的弹窗：升级为模态（先摘 open，再 showModal）
  if (dialog.open) {
    dialog.removeAttribute('open');
    open();
  }

  /* ---------------- 表单：双击防护 + 限流倒计时 ---------------- */

  dialog.querySelectorAll('form').forEach(function (form) {
    form.addEventListener('submit', function (ev) {
      if (form.dataset.submitting === '1') { ev.preventDefault(); return; }
      form.dataset.submitting = '1';
      var btn = form.querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;
    });
  });

  // 限流倒计时：归零前禁用所属表单提交按钮，归零恢复（登录/注册视图通用）。
  // 倒计时 span 在 .login-dialog-error 区块内（表单之外）→ 按所属视图
  // （.auth-view）定位其中的表单按钮，而不是 closest('form')。
  document.querySelectorAll('[data-retry-seconds]').forEach(function (countdown) {
    var pane = countdown.closest ? countdown.closest('.auth-view') : null;
    var submit = pane ? pane.querySelector('form button[type="submit"]') : null;
    var left = parseInt(countdown.getAttribute('data-retry-seconds') || '0', 10);
    var tick = function () {
      if (left <= 0) {
        countdown.textContent = '';
        if (submit) submit.disabled = false;
        return;
      }
      if (submit) submit.disabled = true;
      countdown.textContent = '（' + left + 's）';
      left -= 1;
      window.setTimeout(tick, 1000);
    };
    tick();
  });
})();
