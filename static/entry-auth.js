/* 登录弹窗：entry.html 内嵌 <dialog id="login-dialog">（_login_dialog.html）。
 * - 拦截站内 a[href="/login"]（含 /login?next=…、/login?password_changed=1），
 *   原地打开弹窗，不再整页跳转；
 * - 服务端 login_open=True（GET/POST /login）以非模态 open 属性直出弹窗，
 *   本脚本初始化时把它升级为 showModal()：获得 top-layer 原生焦点陷阱与
 *   ::backdrop 遮罩（焦点限制浏览器原生保证，关闭按钮天然可聚焦）；
 *   无 JS 时退化为 CSS 居中浮层（entry.css .login-dialog）。
 * - 打开期间给 body 加 login-dialog-open（CSS 锁背景滚动），关闭移除并还原焦点。
 * - 锁定（error_code=locked）时服务端渲染 #login-dialog-countdown[data-retry-seconds]：
 *   倒计时期间禁用提交按钮，归零自动恢复可提交（i18n 文案由 i18n.js 全局
 *   data-i18n 扫描覆盖，倒计时数字为语言无关内容，无需重渲染）。
 */
(function () {
  'use strict';

  var dialog = document.getElementById('login-dialog');
  if (!dialog) return;

  var form = document.getElementById('login-dialog-form');
  var submit = form ? form.querySelector('button[type="submit"]') : null;
  var opener = null;

  function setBodyClass(on) {
    document.body.classList.toggle('login-dialog-open', on);
  }

  function open() {
    if (dialog.open) return;
    opener = (document.activeElement instanceof HTMLElement) ? document.activeElement : null;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    setBodyClass(true);
    var field = document.getElementById('login-dialog-username');
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

  // 拦截 /login 与 /login?... 链接（顶栏「登录」等）
  Array.prototype.forEach.call(
    document.querySelectorAll('a[href="/login"], a[href^="/login?"]'),
    function (link) {
      link.addEventListener('click', function (ev) {
        ev.preventDefault();
        open();
      });
    });

  // 服务端 open 属性直出的弹窗：升级为模态（先摘 open，再 showModal）
  if (dialog.open) {
    dialog.removeAttribute('open');
    open();
  }

  // 锁定倒计时：归零前禁用提交按钮，归零恢复
  var countdown = document.getElementById('login-dialog-countdown');
  if (countdown) {
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
  }
})();
