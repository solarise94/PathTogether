/* 注册帮助页交互（/registration-help，2026-10-08 设计 §5）。
 *
 * 页面 CSP：script-src 'self' —— 本文件为外链脚本，无内联事件/内联样式。
 * 职责：
 *  - mailto 语言跟随：主按钮 href 按当前 UI 语言在 data-mailto-zh /
 *    data-mailto-en 间切换（初始 + hp-lang-change）；无 JS 时服务端渲染的
 *    href（入口默认语言）照常可用；
 *  - 邮件预览：从当前语言的 mailto body 参数解码刷新 <pre>（textContent，
 *    不经 innerHTML）；
 *  - 复制作者邮箱：navigator.clipboard 优先（安全上下文），退化回退为
 *    选中可见邮箱文本 + execCommand('copy')；成功后按钮临时显示「已复制」。
 * 用户始终主动审阅并发送；本页绝不自动发送。
 */
(function () {
  'use strict';

  function t(key, fallback) {
    try {
      var s = window.HP_I18N && window.HP_I18N.t(key);
      return (s && s !== key) ? s : fallback;
    } catch (e) { return fallback; }
  }

  function currentLang() {
    try {
      return (window.HP_I18N && window.HP_I18N.getLang() === 'en') ? 'en' : 'zh';
    } catch (e) { return 'zh'; }
  }

  var mailLink = document.getElementById('reghelp-mail');
  var preview = document.getElementById('reghelp-template');
  var copyBtn = document.getElementById('reghelp-copy');

  /* ---- mailto：按语言取对应预填版本（属性缺失时保留服务端默认 href） ---- */

  function mailtoFor(lang) {
    if (!mailLink) return '';
    var attr = 'data-mailto-' + lang;
    return (mailLink.getAttribute && mailLink.getAttribute(attr)) || '';
  }

  function applyLang() {
    var lang = currentLang();
    var href = mailtoFor(lang) || mailtoFor('zh') || '';
    if (href && mailLink) mailLink.setAttribute('href', href);
    updatePreview(href);
  }

  function updatePreview(href) {
    if (!preview || !href) return;
    var body = null;
    try {
      var idx = href.indexOf('body=');
      if (idx >= 0) body = decodeURIComponent(href.slice(idx + 5));
    } catch (e) { body = null; }
    if (body) preview.textContent = body;
  }

  /* ---- 复制作者邮箱（含非安全上下文回退） ---- */

  function fallbackCopy(text) {
    // 回退：选中页面上的可见邮箱文本节点再执行 copy 命令（无剪贴板 API 的
    // 环境；execCommand 属 JS API，不受 CSP 限制）
    var node = document.getElementById('reghelp-email-text');
    var ok = false;
    try {
      var range = document.createRange();
      range.selectNodeContents(node);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      ok = document.execCommand('copy');
      sel.removeAllRanges();
    } catch (e) { ok = false; }
    return ok ? Promise.resolve() : Promise.reject(new Error('copy-fallback-failed'));
  }

  function flashCopied() {
    if (!copyBtn) return;
    var original = copyBtn.textContent;
    copyBtn.textContent = t('reghelp.copied', '已复制');
    copyBtn.classList.add('btn-copied');
    window.setTimeout(function () {
      copyBtn.textContent = original;
      copyBtn.classList.remove('btn-copied');
    }, 2000);
  }

  function onCopy(ev) {
    var btn = ev.currentTarget || ev.target;
    var email = (btn.getAttribute && btn.getAttribute('data-author-email')) || '';
    if (!email) return;
    var done = function () { flashCopied(); };
    var fail = function () {
      // 极端环境（无剪贴板 API 且 execCommand 失败）：选中邮箱让用户手动复制
      try {
        var node = document.getElementById('reghelp-email-text');
        var range = document.createRange();
        range.selectNodeContents(node);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      } catch (e) { /* 忽略 */ }
    };
    var pr = null;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        pr = navigator.clipboard.writeText(email);
      }
    } catch (e) { pr = null; }
    if (!pr) pr = fallbackCopy(email);
    pr.then(done, fail);
  }

  if (copyBtn) copyBtn.addEventListener('click', onCopy);

  applyLang();
  document.addEventListener('hp-lang-change', applyLang);
})();
