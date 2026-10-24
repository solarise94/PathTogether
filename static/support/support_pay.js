// support_pay.js — 支付宝赞助订单三个页面的渐进增强：
//   /support/pay/<ref>  收银台跳转页：自动提交签名表单（含返回/前进不重提）；
//   /support/return     结果页：pending 轮询 + 已支付态致谢凭条打印/重播；
//   标题本地化。
//
// 约束（CSP script-src 'self'）：本文件是同源外链脚本，页面里没有内联脚本、
// 不使用内联 style（视觉状态一律切换 class/hidden 属性）。
'use strict';

(function () {
  var POLL_INTERVAL_MS = 3000;   // R2 B2：轮询周期 3s（与服务端节流一致）
  var POLL_MAX_ATTEMPTS = 60;    // 最多约 3 分钟，之后只留手动「刷新」

  function t(key) {
    return (window.HP_I18N && window.HP_I18N.t) ? window.HP_I18N.t(key) : null;
  }

  function syncTitle() {
    var page = document.body.getAttribute('data-page');
    var key;
    if (page === 'support-pay-qr') {
      key = 'supportpayqr.doc.title';       // 弹层内嵌二维码页
    } else if (document.body.querySelector('[data-pay-autosubmit]') != null) {
      key = 'supportpay.doc.title';
    } else {
      key = 'supportreturn.doc.title';
    }
    var title = t(key);
    if (title) document.title = title;
  }

  /* ------------------------------------------------ /support/pay 自动提交 -- */
  function autoSubmitPayForm() {
    var host = document.querySelector('.pay-form-host');
    if (!host) return;
    var form = host.querySelector('form.pay-form') || host.querySelector('form');
    if (!form) return;
    // 只提交一次；提交会导航离站（支付宝网关），随后脚本不再执行。
    if (form.dataset && form.dataset.submitted === '1') return;
    if (form.dataset) form.dataset.submitted = '1';
    // 防双击（UX 层，R1 add-on；服务端订单状态机才是权威）：
    // 禁用表单内的提交按钮（SDK 按钮无 name，不参与签名）。
    var button = form.querySelector('input[type="submit"], button[type="submit"]');
    if (button) button.disabled = true;
    // HTMLFormElement.submit() 不触发 submit 事件/约束校验——与本场景一致
    //（表单字段全部由服务端签名生成，浏览器侧无需校验）。
    window.setTimeout(function () {
      try {
        form.submit();
      } catch (err) {
        // 提交失败（极少数环境）：保留手动按钮，用户可自行点击。
        if (button) button.disabled = false;
      }
    }, 0);
  }

  /* -------------------------------------- /support/pay 返回/前进不重提交 -- */
  // R2 B3：买家扫码后按浏览器返回（bfcache pageshow persisted 或
  // back_forward 导航）时，不再自动重提交到支付宝——改显示
  // 「查看结果 / 继续付款」恢复面板；全新首次访问仍然自动提交。
  function showPayResumePanel() {
    var host = document.querySelector('.pay-form-host');
    var panel = document.getElementById('pay-resume');
    if (panel && panel.hidden) panel.hidden = false;
    if (host) host.hidden = true;
  }

  function continueToAlipay() {
    var host = document.querySelector('.pay-form-host');
    var panel = document.getElementById('pay-resume');
    var form = host
      ? (host.querySelector('form.pay-form') || host.querySelector('form'))
      : null;
    if (panel) panel.hidden = true;
    if (host) host.hidden = false;
    if (form) {
      if (form.dataset) form.dataset.submitted = '1';
      try {
        form.submit();
      } catch (err) { /* 保持手动按钮可用 */ }
    }
  }

  function initPayBackNav() {
    var panel = document.getElementById('pay-resume');
    if (!panel) return; // 非支付跳转页
    window.addEventListener('pageshow', function (e) {
      if (e.persisted) showPayResumePanel();
    });
    if (window.performance && window.performance.getEntriesByType) {
      var entries = window.performance.getEntriesByType('navigation');
      if (entries.length && entries[0].type === 'back_forward') {
        showPayResumePanel();
      }
    }
    var cont = document.getElementById('pay-resume-continue');
    if (cont) cont.addEventListener('click', continueToAlipay);
  }

  /* -------------------------------------- /support/return 弹层内 breakout -- */
  // R3（桌面端弹层）：付款后收银台会在**iframe 内**导航到 return_url（沙箱
  // 实测：queryQRStatus 轮询到已付 → 自身跳 unitradeprod cashierReturn.htm →
  // return_url）。iframe 内的跨站子框架导航不带 SameSite=Lax Cookie → 先渲染
  // 中性页；本函数把回跳页提升到顶层窗口（顶层跨站 GET 导航会带 Cookie →
  // 收据正常）。防循环：顶部已经是回跳页（弹层轮询先到）就不再跳。
  function breakoutReturnFromIframe() {
    if (window.top === window.self) return;
    var sameTop = false;
    try {
      // 顶层同源时可读路径；跨站（理论上的遗留形态）读取会抛 → 视为需 breakout
      sameTop = window.top.location.pathname === window.location.pathname;
    } catch (err) {
      sameTop = false;
    }
    if (sameTop) return;
    window.top.location.replace(window.location.href);
  }

  /* ------------------------------------------------ /support/return 轮询 -- */
  // R2 B2：pending 态每 3s 轮询一次会话绑定的状态 JSON；paid/closed →
  // 重新加载页面（服务端已按 verified 推进过状态，重载即展示收据/关闭态）。
  // 最多约 3 分钟，之后停止（页面保留手动「刷新支付结果」链接）。
  function initReturnPolling() {
    var section = document.querySelector('[data-return-pending]');
    if (!section) return;
    var url = section.getAttribute('data-status-url');
    if (!url) return;
    var attempts = 0;
    var timer = null;
    window.setTimeout(function () {
      window.clearInterval(timer);
    }, POLL_INTERVAL_MS * POLL_MAX_ATTEMPTS + 1000);
    timer = window.setInterval(function () {
      attempts += 1;
      if (attempts > POLL_MAX_ATTEMPTS) {
        window.clearInterval(timer);
        return;
      }
      var done = false;
      fetch(url, { credentials: 'same-origin', cache: 'no-store' })
        .then(function (resp) { return resp.ok ? resp.json() : null; })
        .then(function (data) {
          if (!data) return;               // 429/网络错误：留待下轮
          if (data.status === 'paid' || data.status === 'closed') {
            done = true;
            window.clearInterval(timer);
            window.location.reload();
          }
        })
        .catch(function () { /* 网络抖动：留待下轮 */ })
        .finally(function () {
          if (done && timer) window.clearInterval(timer);
        });
    }, POLL_INTERVAL_MS);
  }

  /* --------------------------------------------- /support/return 凭条打印 -- */
  // R2 视觉：已支付态的致谢凭条以单段缓出 transform 滑出（无 steps）。
  // 「再打一张」重播同一动画（移除再添加动画类，强制浏览器重启动画）。
  // prefers-reduced-motion: reduce 下凭条静态呈现、不显示重播按钮。
  // 礼物动画不在这里播放（喂猫互动：礼物本体在 /support 小猫身边，由用户
  // 亲手喂；本页只发「领取卡」）。
  function initReturnReceipt() {
    var receipt = document.getElementById('receipt');
    if (!receipt) return;
    // 凭条日期用浏览器本地时区的今天（服务端渲染多为 UTC 容器时间，仅作
    // 无 JS 回退；北京时间早上 8 点前不应显示成“昨天”）。
    var time = receipt.querySelector('time');
    if (time) {
      var d = new Date();
      function pad(n) { return (n < 10 ? '0' : '') + n; }
      var iso = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-'
        + pad(d.getDate());
      time.setAttribute('datetime', iso);
      time.textContent = iso;
    }
    // 「再看一次」：JS 可用时本地重播凭条滑出（不刷新）；
    // prefers-reduced-motion 下不做本地重播，让链接导航到静态呈现的页面。
    var replay = document.getElementById('receipt-replay');
    if (!replay) return;
    var media = window.matchMedia
      ? window.matchMedia('(prefers-reduced-motion: reduce)')
      : null;
    function reduced() {
      return !!(media && media.matches);
    }
    replay.addEventListener('click', function (e) {
      if (reduced()) return;      // 静态呈现：交给链接正常刷新
      e.preventDefault();         // 本地重播凭条滑出
      receipt.classList.remove('receipt-live');
      void receipt.offsetWidth;   // 强制重排，重启动画
      receipt.classList.add('receipt-live');
    });
  }

  function init() {
    syncTitle();
    autoSubmitPayForm();
    initPayBackNav();
    if (document.documentElement.getAttribute('data-page')
        === 'support-return') {
      // 回跳页任一状态（含 iframe 内无会话的中性渲染）都要尝试提升到顶层
      breakoutReturnFromIframe();
    }
    initReturnPolling();
    initReturnReceipt();
    document.addEventListener('hp-lang-change', syncTitle);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
