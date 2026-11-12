/* 找回密码申请页的语言同步（/password-reset）。
 *
 * 只做一件事：把表单隐藏域 form_locale 同步为当前界面语言（zh|en），
 * 让服务端按用户实际看到的语言冻结邮件正文（与注册表单同口径）。
 * Turnstile 使用官方隐式渲染（.cf-turnstile），无需额外 JS。
 */
(function () {
  "use strict";

  function currentLang() {
    try {
      return window.HP_I18N && window.HP_I18N.getLang() === "en"
        ? "en" : "zh";
    } catch (e) {
      return "zh";
    }
  }

  function syncFormLocale() {
    var lang = currentLang();
    var inputs = document.querySelectorAll('input[name="form_locale"]');
    for (var i = 0; i < inputs.length; i += 1) {
      inputs[i].value = lang;
    }
  }

  document.addEventListener("hp-lang-change", syncFormLocale);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", syncFormLocale);
  } else {
    syncFormLocale();
  }
})();
