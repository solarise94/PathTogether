// support.js — /support 支持项目页的渐进增强（页面内容本身由服务端渲染，
// 无 JavaScript 时仍完整可读）。
//
// 职责：
//  1. 跟随语言切换更新 document.title；
//  2. 下单提交防双击（UX 层；服务端幂等 key 才是权威）；
//  3. 桌面端（宽视口 + 非触控）拦截下单提交：JSON 建单后打开「支付状态」
//     弹层，iframe 加载同源 embed 页（/support/pay/<ref>/qr，未决订单被
//     服务端 303 到签名 GET 网关 URL，收银台在 iframe 内渲染二维码），轮询
//     既有状态端点。paid → 弹层切到第三窗格「致谢凭条打印」（不再跳转
//     回跳页），「收下礼物」关弹层、滚到小猫并把礼物放进猫卡；closed →
//     「二维码已过期」+「重新生成」（服务端先复核旧单：已支付直接出示
//     凭条，未支付才关旧建新）；neutral → 「暂时无法确认」会话变体（订单
//     号 + 联系入口，绝不从它宣判过期）。移动端/窄屏/触屏保持原整页跳转。
//     R3（2026-10-21 评审后 add-on），R4（喂猫互动 add-on，同日用户评审），
//     r4（neutral/重新生成语义修复，2026-10-23），r5（幂等 key 跟随展示
//     订单：建单/重建成功后把请求实际使用的 key 同步回表单隐藏输入；换额
//     提交改用未绑定的 next_idempotency_key，避免同 key 异额 409），r6
//     （重建遇命名 paid 负载 → 凭条切换到该命名订单）。
//  4. 喂猫互动：会话已支付订单的礼物停在猫卡地板（服务端渲染，localStorage
//     「已喂」指针只做装饰态）；拖到猫身上 / 点礼物 / 点托盘按钮 → 喂食场景
//     （复用原回跳页礼物动画的节拍关键帧，见 support.css）。
// 回跳页的凭条打印动画在 /support/return（support_pay.js）。
'use strict';

(function () {
  var POLL_INTERVAL_MS = 3000;   // 与服务端每订单 3s 查单节流一致
  var POLL_BACKOFF_MS = 6000;    // 状态端点 429 slow_down 时的退避
  var DESKTOP_QUERY = '(min-width: 900px)';
  var TOUCH_QUERY = '(hover: none), (pointer: coarse)';

  // initCatPlay 暴露给弹层的最小桥（交付礼物 + 聚焦喂食按钮）
  var catWorld = null;

  function t(key) {
    return (window.HP_I18N && window.HP_I18N.t) ? window.HP_I18N.t(key) : null;
  }

  function prefersReducedMotion() {
    return !!(window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function localDateIso() {
    var d = new Date();
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-'
      + pad(d.getDate());
  }

  function syncTitle() {
    var title = t('support.doc.title');
    if (title) document.title = title;
  }

  /* ------------------------------------------- 下单表单 + 桌面端弹层 -- */
  function initPayPickGuard() {
    var form = document.getElementById('pay-pick-form');
    if (!form) return;
    var modal = document.getElementById('pay-modal');
    if (!modal) {
      // 无弹层（支付关闭等形态）：只保留防双击（UX 层，服务端幂等才是权威）
      form.addEventListener('submit', function () {
        var button = document.getElementById('pay-pick-submit');
        if (button) button.disabled = true;
      });
      return;
    }
    initDesktopQrModal(form, modal);
  }

  function initDesktopQrModal(form, modal) {
    var button = document.getElementById('pay-pick-submit');
    var frameHost = document.getElementById('pay-modal-frame-host');
    var qrPane = document.getElementById('pay-modal-qr');
    var expiredPane = document.getElementById('pay-modal-expired');
    var printPane = document.getElementById('pay-modal-print');
    // 「暂时无法确认」窗格（2026-10-23 评审）：查询失败 ≠ 已过期——状态未知
    // 时不宣判过期、不诱导重复支付，只提示等待自动重试。
    var unconfirmedPane = document.getElementById('pay-modal-unconfirmed');
    // 窗格内变体元素（r4 2026-10-23）：会话失去订单（neutral）时换会话
    // 正文并露出订单号与联系入口。链接目标只走属性赋值（不 getAttribute）。
    var unconfirmedBody = document.getElementById('pay-modal-unconfirmed-body');
    var sessionBody = document.getElementById('pay-modal-unconfirmed-session-body');
    var sessionRefRow = document.getElementById('pay-modal-unconfirmed-ref-row');
    var sessionRefEl = document.getElementById('pay-modal-unconfirmed-ref');
    var contactRow = document.getElementById('pay-modal-unconfirmed-contact');
    var contactLink = document.getElementById('pay-modal-contact');
    var fallbackLink = document.getElementById('pay-modal-fallback');
    var closeBtn = document.getElementById('pay-modal-close');
    var regenerateBtn = document.getElementById('pay-modal-regenerate');
    var amountEl = document.getElementById('pay-modal-amount');
    var nameEl = document.getElementById('pay-modal-name');
    var giftEl = document.getElementById('pay-modal-gift');
    var countdownEl = document.getElementById('pay-modal-countdown');
    var titleEl = document.getElementById('pay-modal-title');
    var printReceipt = document.getElementById('pay-print-receipt');
    var printArtEl = document.getElementById('pay-print-gift-art');
    var printNameEl = document.getElementById('pay-print-gift-name');
    var printAmountEl = document.getElementById('pay-print-amount');
    var printDateEl = document.getElementById('pay-print-date');
    var printRefEl = document.getElementById('pay-print-ref');
    var deliverBtn = document.getElementById('pay-modal-deliver');
    var fullReceiptLink = document.getElementById('pay-modal-fullreceipt');
    var artSource = document.getElementById('pay-gift-art-source');

    // 查询退避封顶与「已处置」哨兵（2026-10-23 评审修复；须留在本函数内——
    // 回归测试只切取 initDesktopQrModal 源码段执行）。
    var POLL_BACKOFF_CAP_MS = 30000;   // 连续失败退避封顶
    var POLL_HANDLED = {};             // 响应已在第一段 then 处置（丢弃/429）

    var state = {
      ref: null,               // 当前弹层展示的订单号
      gift: null,              // 服务端下发的可收集礼物 kind（paid 时）
      pane: 'qr',              // 'qr' | 'expired' | 'print' | 'unconfirmed'
      printDone: false,        // 本单的「收下礼物」只触发一次
      nextKey: null,           // 服务端下发、未绑定任何订单的新幂等 key
      amountYuan: null,        // 当前展示订单的两位小数金额（r5 换额判定）
      expiresAtMs: 0,          // 服务端 expires_at（唯一权威）
      pollTimer: null,         // setTimeout 链（429 退避需要可变间隔）
      tickTimer: null,         // 倒计时
      lastFocus: null,         // 关闭后焦点还原
      inflight: false,
      gen: 0,                  // 展示订单代次：关弹层/换单即失效旧请求
      pollInflightGen: null,   // 在途状态查询所属代次（同代绝不并发轮询）
      pollFails: 0,            // 连续失败次数（退避翻倍；成功清零）
      lastCheckOk: true,       // 最近一次查询是否成功（false = 状态未知）
      sessionLost: false,      // 会话已失去该订单追踪（neutral；明确结论才解除）
      deadlineReached: false,  // 倒计时已收口（0:00）：本单此后按到期语义收口
    };

    function desktop() {
      return window.matchMedia(DESKTOP_QUERY).matches
        && !window.matchMedia(TOUCH_QUERY).matches;
    }

    function selectedTier() {
      var picked = form.querySelector('input[name="amount"]:checked');
      if (!picked) return null;
      var card = picked.closest('.pay-tier');
      return {
        value: picked.value || '',
        art: card ? card.querySelector('.pay-tier-art') : null,
        name: card ? card.querySelector('.pay-tier-name') : null,
      };
    }

    /* ---------------------- 幂等 key 跟单 + 换额判定（r5 评审修复）-- */
    // 表单里服务端渲染的隐藏幂等 key 输入；缺失（测试桩/异常形态）时
    // 读写一律静默跳过，行为同旧版。
    function keyInputEl() {
      return form.querySelector('input[name="idempotency_key"]');
    }

    // key 跟随展示订单：服务端把订单与本请求实际使用的 key 永久绑定——
    // 写回隐藏输入后，之后同额重开提交服务端会原样返回本单（不会拿旧
    // key 复生成已被替换的旧单）。
    function syncKeyInput(key) {
      var input = keyInputEl();
      if (input && key) input.value = key;
    }

    // 金额字符串 → 整数分（服务端口径 ^\d+(\.\d{1,2})?$，其余一律未知）。
    // 按字符串分段计算，不走浮点乘法；非字符串 → null。
    function parseAmountFen(yuan) {
      if (typeof yuan !== 'string') return null;
      var m = /^(\d+)(?:\.(\d{1,2}))?$/.exec(yuan);
      if (!m) return null;
      var cents = m[2] || '';
      while (cents.length < 2) cents += '0';
      return parseInt(m[1], 10) * 100 + parseInt(cents, 10);
    }

    // 本次提交金额（服务端同口径，分）：非空 trimmed amount_custom 优先；
    // 其次选中的 amount 单选；再退到非单选 amount 输入（如隐藏域）。
    // 取不到/解析不了 → null（按金额未知处理）。
    function submittedAmountFen() {
      var custom = form.querySelector('input[name="amount_custom"]');
      var customVal = custom && typeof custom.value === 'string'
        ? custom.value.trim() : '';
      if (customVal) return parseAmountFen(customVal);
      var picked = form.querySelector('input[name="amount"]:checked');
      if (picked) return parseAmountFen(picked.value);
      var fixed = form.querySelector('input[name="amount"]');
      return fixed ? parseAmountFen(fixed.value) : null;
    }

    // 提交应改用的幂等 key：弹层已有当前订单（ref + 未绑定 nextKey）且
    // 本次金额与当前订单不同 → nextKey（旧 key 已绑定本单，换单必 409）；
    // 同额/金额未知/无当前订单 → null，维持表单原 key 的既有行为。
    function submitKeyOverride() {
      if (!state.ref || !state.nextKey) return null;
      var want = submittedAmountFen();
      var have = parseAmountFen(state.amountYuan);
      if (want === null || have === null || want === have) return null;
      return state.nextKey;
    }

    /* ------------------------------------------------ 弹层开合/焦点 -- */
    function onKeydown(ev) {
      if (ev.key === 'Escape') {
        closeModal();
        return;
      }
      if (ev.key !== 'Tab') return;
      var candidates = modal.querySelectorAll(
        'button:not([disabled]), a[href], input:not([disabled]),'
        + ' [tabindex]:not([tabindex="-1"])');
      var visible = [];
      for (var i = 0; i < candidates.length; i++) {
        if (candidates[i].offsetParent !== null) visible.push(candidates[i]);
      }
      if (!visible.length) return;
      var first = visible[0];
      var last = visible[visible.length - 1];
      if (ev.shiftKey && document.activeElement === first) {
        ev.preventDefault();
        last.focus();
      } else if (!ev.shiftKey && document.activeElement === last) {
        ev.preventDefault();
        first.focus();
      }
    }

    function openModal() {
      if (!modal.hidden) return;
      modal.hidden = false;
      state.lastFocus = document.activeElement;
      document.addEventListener('keydown', onKeydown);
      if (closeBtn) closeBtn.focus();
    }

    function restoreTitle() {
      if (!titleEl) return;
      titleEl.setAttribute('data-i18n', 'support.modal.title');
      titleEl.textContent = t('support.modal.title') || '支付状态';
    }

    function resetPanes() {
      state.pane = 'qr';
      state.printDone = false;
      if (qrPane) qrPane.hidden = false;
      if (expiredPane) expiredPane.hidden = true;
      if (printPane) printPane.hidden = true;
      if (unconfirmedPane) unconfirmedPane.hidden = true;
      if (printReceipt) printReceipt.classList.remove('is-printing');
    }

    function closeModal() {
      if (modal.hidden) return;
      // 已支付窗格里 ×/Esc 同样「收下礼物」：关弹层 → 滚到小猫 → 交付
      var deliverPending = state.pane === 'print' && state.ref
        && !state.printDone;
      modal.hidden = true;
      // 关闭即换代：在途的建单/查单响应一律作废（评审 2026-10-23：旧单的
      // 迟到 paid 曾被当成新单的成功，且迟到响应会隐式续排轮询）。
      state.gen++;
      stopTimers();
      document.removeEventListener('keydown', onKeydown);
      restoreTitle();
      // 订单保持 pending：本会话未决订单由 /support 的会话提醒卡承接
      if (state.lastFocus && state.lastFocus.focus) state.lastFocus.focus();
      if (deliverPending) {
        state.printDone = true;
        deliverToCat(state.ref, state.gift);
      }
      resetPanes();
    }

    /* ------------------------------------------------- 倒计时/轮询 -- */
    function fmtCountdown(totalSeconds) {
      var m = Math.floor(totalSeconds / 60);
      var s = totalSeconds % 60;
      return m + ':' + (s < 10 ? '0' : '') + s;
    }

    function renderCountdown() {
      var seconds = Math.max(0, Math.floor(
        (state.expiresAtMs - Date.now()) / 1000));
      countdownEl.textContent = fmtCountdown(seconds);
      if (seconds <= 0) handleDeadline();
    }

    function startCountdown() {
      stopTick();
      renderCountdown();
      state.tickTimer = window.setInterval(renderCountdown, 1000);
    }

    /* 到期收口（评审 2026-10-23；r2 复核修订）：归零只停表一次。过期与否
       由服务端权威裁定（confirmed/expired），客户端时钟不再参与裁定——
       0:00 只决定「失败如何呈现」：若最近一次查询失败（或最后一次即时
       查询也失败），真实状态未知，改示「暂时无法确认」并继续退避重试；
       服务端随后给出明确状态时再相应收口（paid → 打印凭条；closed/
       neutral 或 confirmed+expired → 过期；confirmed 未过期 → 继续轮询）。 */
    function handleDeadline() {
      // 首帧 0:00 即视为到期（Math.floor 使其早于 expiresAtMs 最多 1s）：
      // 打标记后本单此后的查询失败/限流一律切「暂时无法确认」，不再依赖
      // 墙钟比较；是否「已过期」仍只认服务端 confirmed+expired。
      state.deadlineReached = true;
      stopTick();
      stopPoll();
      if (state.pollInflightGen !== state.gen) pollStatus();
      if (!state.lastCheckOk) showUnconfirmed();
    }

    function schedulePoll(delayMs) {
      stopPoll();
      state.pollTimer = window.setTimeout(pollStatus, delayMs);
    }

    // 本请求是否已失效：弹层已关，或展示订单已切换（代次/订单号变化）。
    // 失效响应绝不改 UI、绝不排程。
    function stale(gen, ref) {
      return gen !== state.gen || ref !== state.ref || modal.hidden;
    }

    // 连续失败后的退避间隔：POLL_BACKOFF_MS 起步、逐次翻倍、封顶。
    function backoffDelayMs() {
      var d = POLL_BACKOFF_MS;
      var left = state.pollFails - 1;
      while (left-- > 0 && d < POLL_BACKOFF_CAP_MS) d = d * 2;
      return d > POLL_BACKOFF_CAP_MS ? POLL_BACKOFF_CAP_MS : d;
    }

    // 到期判定（r2 复核修订）：倒计时收口（0:00）后对本单一律成立；否则
    // 按服务端时刻比较。只用于决定「失败如何呈现」（失败/429 时切「暂时
    // 无法确认」）；过期与否由服务端 confirmed/expired 权威裁定，不再由
    // 客户端时钟决定
    function pastDeadline() {
      return state.deadlineReached
        || (!!state.expiresAtMs && Date.now() >= state.expiresAtMs);
    }

    // 一次失败的查询（非 429 非 OK / 网络异常 / JSON 损坏 / 负载缺 status）：
    // 指数退避重试；到期后失败改示「暂时无法确认」，绝不误报「已过期」。
    function noteCheckFailed() {
      state.pollFails++;
      state.lastCheckOk = false;
      if (pastDeadline()) {
        showUnconfirmed();
      }
      schedulePoll(backoffDelayMs());
    }

    // 一次「本页已无力追踪这笔订单」的处置（r4 2026-10-23）：计一次失败、
    // 立即切「暂时无法确认」（无视倒计时——继续干等二维码已无意义）、按
    // 退避继续轮询本单；轮询指向不变（state.ref 未换）。
    function noteUnknownState() {
      state.pollFails++;
      state.lastCheckOk = false;
      showUnconfirmed();
      schedulePoll(backoffDelayMs());
    }

    // neutral（r4 2026-10-23 语义修复）：会话不再拥有这笔订单——不是「已
    // 过期」，只是本页失去追踪。标记会话变体（粘滞到本单出现明确结论）后
    // 按状态未知处置；过期与否仍只认 closed / confirmed+expired。
    function noteSessionLost() {
      state.sessionLost = true;
      noteUnknownState();
    }

    // 一次查询的收口（r2 复核修订）：created/paying 只有带 confirmed=true
    // （服务端 3s 内向支付宝查证「未支付」成功）才算查询成功——网关超时等
    // 情况下服务端答裸 created/paying（或 confirmed 缺省），本地状态未经
    // 支付宝确认，绝不能当作「未支付」宣判「已过期」，只能按查询失败退避
    // 重试。是否过期由服务端 expired 权威裁定；客户端时钟不参与裁定。
    function handleStatus(data) {
      // 任何非 neutral 结论都证明会话（重新）拥有这笔订单——服务端对不属于
      // 本会话的订单号一律答 neutral（r4 复核修正）：先解除会话变体再走
      // 常规分支——未到期把二维码窗格还回来；已到期把「暂时无法确认」换回
      // 常规文案。paid/closed/confirmed+expired 随后照常切窗格；未确认
      // 负载照常按失败退避（到期前保 QR、到期后示常规未知文案）。
      if (state.sessionLost && data.status !== 'neutral') {
        state.sessionLost = false;
        if (state.pane === 'unconfirmed') {
          if (pastDeadline()) renderUnconfirmedVariant();
          else resetPanes();
        }
      }
      if (data.status === 'paid') {
        state.pollFails = 0;
        state.lastCheckOk = true;
        stopTimers();
        // 不再跳转回跳页：弹层切到「致谢凭条打印」窗格，凭条上的礼物
        // 由用户收下后亲自去喂（回跳页仍可经「查看完整凭条」到达）。
        showPrintPane(data);
      } else if (data.status === 'closed') {
        // 服务端裁定订单已关闭（未支付且不可再付）：closed 系唯一过期结论
        // （r4 起 neutral 不再归入此处，见 noteSessionLost）
        state.pollFails = 0;
        state.lastCheckOk = true;
        showExpired();
      } else if (data.status === 'neutral') {
        noteSessionLost();
      } else if (data.confirmed !== true) {
        // 裸/未确认的 created|paying（旧服务端、网关超时）或未知状态：
        // 查询未成功、状态未知——退避重试，绝不据此切换窗格
        noteCheckFailed();
      } else if (data.expired === true) {
        // 服务端在支付宝截止 ≥10s 后查证未支付：权威「已过期」（可重新生成）
        state.pollFails = 0;
        state.lastCheckOk = true;
        showExpired();
      } else {
        // 已确认仍在等待支付：不切窗格——0:00 后二维码停在 0:00 等服务端
        // 结论；「暂时无法确认」已显示时保持原样，由后续结论收口
        // （会话变体的解除在上面分支派发前完成）
        state.pollFails = 0;
        state.lastCheckOk = true;
        schedulePoll(POLL_INTERVAL_MS);
      }
    }

    function pollStatus() {
      // 同一代次绝不并发两个查询；换代后旧代在途请求不阻塞新单轮询
      if (!state.ref || state.pollInflightGen === state.gen) return;
      var gen = state.gen;
      var ref = state.ref;
      state.pollInflightGen = gen;
      fetch('/support/orders/' + state.ref + '/status',
            { credentials: 'same-origin', cache: 'no-store' })
        .then(function (resp) {
          if (stale(gen, ref)) return POLL_HANDLED;   // 迟到响应：整体丢弃
          if (resp.status === 429) {          // 每 IP 限流：退避后重试
            state.pollInflightGen = null;
            state.lastCheckOk = false;        // 查询未成功：此刻状态未知
            schedulePoll(POLL_BACKOFF_MS);
            // 到期后 tick 已停、只排程不切窗格会让二维码永远停在 0:00
            // （r2 复核 2026-10-23）：限流同样是「状态未知」，切「暂时
            // 无法确认」，按固定 6s 退避继续重试
            if (pastDeadline()) showUnconfirmed();
            return POLL_HANDLED;
          }
          return resp.ok ? resp.json() : null;
        })
        .then(function (data) {
          if (data === POLL_HANDLED) return;  // 已处置（丢弃/429 已排程）
          if (stale(gen, ref)) return;        // 迟到响应：不进 UI、不排程
          state.pollInflightGen = null;
          if (!data || typeof data.status !== 'string' || !data.status) {
            noteCheckFailed();
            return;
          }
          handleStatus(data);
        })
        .catch(function () {
          if (stale(gen, ref)) return;        // 迟到响应：不进 UI、不排程
          state.pollInflightGen = null;
          noteCheckFailed();
        });
    }

    function stopPoll() {
      if (state.pollTimer) {
        window.clearTimeout(state.pollTimer);
        state.pollTimer = null;
      }
    }

    function stopTick() {
      if (state.tickTimer) {
        window.clearInterval(state.tickTimer);
        state.tickTimer = null;
      }
    }

    function stopTimers() {
      stopPoll();
      stopTick();
    }

    function showExpired() {
      stopTimers();
      state.pane = 'expired';
      if (qrPane) qrPane.hidden = true;
      if (expiredPane) expiredPane.hidden = false;
      if (printPane) printPane.hidden = true;
      if (unconfirmedPane) unconfirmedPane.hidden = true;
      if (regenerateBtn && !modal.hidden) regenerateBtn.focus();
    }

    // 「暂时无法确认」窗格的变体渲染（r4 2026-10-23）：sessionLost（会话
    // 失去订单）→ 隐常规正文，出示会话正文 + 订单号 + 联系入口（mailto 主题
    // 带订单号，作者据此核对）；常规网络失败/限流只显示常规正文。变体随
    // sessionLost 粘滞：后续失败/429 不降级，直到本单出现明确结论。
    function renderUnconfirmedVariant() {
      var lost = state.sessionLost;
      if (unconfirmedBody) unconfirmedBody.hidden = lost;
      if (sessionBody) sessionBody.hidden = !lost;
      if (sessionRefRow) sessionRefRow.hidden = !lost;
      if (contactRow) contactRow.hidden = !lost;
      if (!lost) return;
      if (sessionRefEl) sessionRefEl.textContent = state.ref || '';
      if (contactLink) {
        var subject = t('support.modal.unconfirmed.mail.subject')
          || '赞助订单核对';
        contactLink.href = 'mailto:'
          + ((contactLink.dataset && contactLink.dataset.email) || '')
          + '?subject=' + encodeURIComponent(subject + ' ' + (state.ref || ''));
      }
    }

    function showUnconfirmed() {
      // 状态未知 ≠ 已过期：不给「重新生成」，只提示不要重复支付
      state.pane = 'unconfirmed';
      if (qrPane) qrPane.hidden = true;
      if (expiredPane) expiredPane.hidden = true;
      if (printPane) printPane.hidden = true;
      if (unconfirmedPane) unconfirmedPane.hidden = false;
      renderUnconfirmedVariant();
    }

    /* ----------------------------------------- 已支付窗格：凭条打印 -- */
    function giftKindOf(data) {
      // kind 由服务端 amount_fen 推导（自定义金额 → fish_treat）；旧服务端
      // 无 gift 字段时按 tier 兜底（三档 kind 与 tier 同名）。
      return data.gift || data.tier || 'fish_treat';
    }

    function cloneGiftArt(kind, host) {
      if (!host) return;
      host.innerHTML = '';
      var tpl = artSource
        ? artSource.querySelector('[data-kind="' + kind + '"] svg') : null;
      if (tpl) host.appendChild(tpl.cloneNode(true));
    }

    function renderPrintTexts() {
      var kind = state.gift || 'fish_treat';
      if (titleEl) {
        titleEl.removeAttribute('data-i18n');
        titleEl.textContent = t('support.modal.print.title')
          || '谢谢你的支持！';
      }
      if (printNameEl) {
        printNameEl.setAttribute('data-i18n', 'support.tier.' + kind);
        printNameEl.textContent = t('support.tier.' + kind) || '';
      }
      if (deliverBtn) {
        deliverBtn.setAttribute('data-i18n',
                                'support.modal.print.cta.' + kind);
        deliverBtn.textContent = t('support.modal.print.cta.' + kind) || '';
      }
      if (printDateEl) printDateEl.textContent = localDateIso();
    }

    function showPrintPane(data) {
      state.pane = 'print';
      state.printDone = false;
      state.gift = giftKindOf(data);
      if (qrPane) qrPane.hidden = true;
      if (expiredPane) expiredPane.hidden = true;
      if (printPane) printPane.hidden = false;
      if (unconfirmedPane) unconfirmedPane.hidden = true;
      if (printAmountEl && data.amount_yuan != null) {
        // 服务端权威两位小数（本地订单行）
        printAmountEl.textContent = data.amount_yuan;
      }
      if (printRefEl) printRefEl.textContent = state.ref || '';
      if (fullReceiptLink) {
        fullReceiptLink.setAttribute(
          'href', '/support/return?out_trade_no=' + state.ref);
      }
      cloneGiftArt(state.gift, printArtEl);
      renderPrintTexts();
      if (printReceipt && !prefersReducedMotion()) {
        // 单段缓出滑出（同回跳页凭条动画）；重开弹层可重放
        printReceipt.classList.remove('is-printing');
        void printReceipt.getBoundingClientRect();
        printReceipt.classList.add('is-printing');
      }
      if (deliverBtn && !modal.hidden) deliverBtn.focus();
    }

    function deliverToCat(ref, gift) {
      if (!catWorld || !ref) return;
      var card = document.getElementById('cat-card');
      if (card && card.scrollIntoView) {
        try {
          card.scrollIntoView({
            behavior: prefersReducedMotion() ? 'auto' : 'smooth',
            block: 'center',
          });
        } catch (err) {
          card.scrollIntoView();
        }
      }
      catWorld.deliver(ref, gift || state.gift || 'fish_treat');
    }

    /* ------------------------------------------------- 建单/应用 -- */
    function renderOrderRow() {
      // 档位行只展示图标 + 档位名；金额由服务端权威值单独大字展示
      var sel = selectedTier();
      if (!sel) return;
      if (nameEl) nameEl.textContent = sel.name ? sel.name.textContent : '';
      if (giftEl) {
        giftEl.innerHTML = '';
        if (sel.art) giftEl.appendChild(sel.art.cloneNode(true));
      }
    }

    function applyOrder(data, sentKey) {
      // 展示订单切换：换代并清零查询簿记（旧单在途响应随后全部作废）
      state.gen++;
      state.pollFails = 0;
      state.lastCheckOk = true;
      state.sessionLost = false;   // 新单重新归属本会话：变体不跨单粘滞
      state.deadlineReached = false;
      resetPanes();
      state.ref = data.out_trade_no;
      state.nextKey = data.next_idempotency_key || null;
      state.amountYuan = data.amount_yuan || null;
      // key 跟单（r5 评审修复）：写回本请求实际使用的 key（迟到/作废响应
      // 到不了这里——两个调用点应用前都已过代次/stale 检查）
      if (sentKey) syncKeyInput(sentKey);
      state.expiresAtMs = Date.parse(data.expires_at) || 0;
      // 金额大字：服务端权威两位小数（不取客户端选择值）
      if (amountEl) amountEl.textContent = data.amount_yuan || '';
      // 「改用支付宝网页收银台」回退链：现有整页收银台跳转页（会话绑定）
      fallbackLink.setAttribute('href', data.pay_url);
      frameHost.innerHTML = '';
      var frame = document.createElement('iframe');
      frame.className = 'pay-modal-frame';
      frame.setAttribute('title',
        t('support.modal.frame.aria') || '支付宝收款台');
      // 严格 200×200（收银台 qr_pay_mode=4 二维码原尺寸；滚动关闭）
      frame.setAttribute('scrolling', 'no');
      // 同源 embed 页：未决订单被服务端 303 到签名 GET 网关 URL（qr_pay_mode=4）
      frame.src = '/support/pay/' + data.out_trade_no + '/qr';
      frameHost.appendChild(frame);
      if (qrPane) qrPane.hidden = false;
      if (expiredPane) expiredPane.hidden = true;
      startCountdown();
      schedulePoll(POLL_INTERVAL_MS);
    }

    function fallbackToFullPage() {
      // 建单失败（服务端错误契约/网络异常）：按原整页表单提交，pay_error
      // 白名单提示、限流 Retry-After、CSRF 失效等既有整页 UX 全部沿用
      if (button) button.disabled = true;
      form.submit();
    }

    function createOrder(idempotencyKey) {
      if (state.inflight) return;
      state.inflight = true;
      // 换额提交（r5 评审修复）：先写隐藏输入再建 FormData（整页回退提交
      // 同一 key），请求体再 fd.set 显式覆盖双保险。
      if (idempotencyKey) syncKeyInput(idempotencyKey);
      // 建单期间弹层可能被关（代次变化，评审 2026-10-23）：迟到的建单
      // 响应绝不重开弹层；首提路径（弹层隐藏、无代次变化）行为不变。
      var gen = state.gen;
      // 本请求实际发送的 key：显式覆盖值，否则即表单隐藏输入的当前值
      var keyInput = keyInputEl();
      var sentKey = idempotencyKey || (keyInput ? keyInput.value : null);
      var fd = new FormData(form);
      if (idempotencyKey) fd.set('idempotency_key', idempotencyKey);
      fetch(form.action, {
        method: 'POST',
        body: fd,
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'Accept': 'application/json' },
      }).then(function (resp) {
        if (resp.status === 200 || resp.status === 201) return resp.json();
        return null;
      }).then(function (data) {
        state.inflight = false;
        if (gen !== state.gen) return;   // 建单期间弹层已关：不重开、不应用
        if (!data || !data.out_trade_no) {
          fallbackToFullPage();
          return;
        }
        renderOrderRow();
        openModal();
        applyOrder(data, sentKey);
      }).catch(function () {
        state.inflight = false;
        if (gen !== state.gen) return;
        fallbackToFullPage();
      });
    }

    // 「重新生成」（r4 2026-10-23 语义修复）：不再客户端直接建新单——先交
    // 服务端复核旧单：paid → 出示凭条（r6：命名负载切换到该命名订单）；
    // 服务端确认关旧建新 → 全新二维码；
    // neutral → 会话变体；unconfirmed/非 2xx/JSON 损坏/网络异常 → 「暂时
    // 无法确认」+ 退避重查旧单。绝不回退整页表单提交（旧单未经复核关闭，
    // 整页重建会绕过核对直接再扣一笔的风险不可接受）。
    function regenerateOrder() {
      if (state.inflight || !state.ref || !state.nextKey) return;
      state.inflight = true;
      if (regenerateBtn) regenerateBtn.disabled = true;
      // 复核期间保持过期窗格原样（不预切空二维码窗格）；弹层若被关即换代，
      // 迟到响应不进 UI、不排程。
      var gen = state.gen;
      var oldRef = state.ref;
      // key 跟单（r5 评审修复）：先记住本请求实际使用的 key——applyOrder
      // 会把 state.nextKey 换成新单下发的 key，届时不能再用于同步
      var sentKey = state.nextKey;
      var fd = new FormData(form);
      fd.set('idempotency_key', state.nextKey);
      fetch('/support/orders/' + oldRef + '/regenerate', {
        method: 'POST',
        body: fd,
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'Accept': 'application/json' },
      }).then(function (resp) {
        if (resp.status === 200 || resp.status === 201) return resp.json();
        return null;
      }).then(function (data) {
        state.inflight = false;
        if (regenerateBtn) regenerateBtn.disabled = false;
        if (stale(gen, oldRef)) return;   // 换代/关弹层：整体丢弃
        // r6（2026-10-24 评审）：paid 判定必须先于订单负载分支——命名 paid
        // 负载也带 out_trade_no，先走下面分支会被误当成新建二维码单应用。
        // 服务端契约：out_trade_no 当且仅当已支付的不是 URL 里的旧单才携带；
        // paid 负载只在请求 key 未绑定时返回（state.nextKey 仍未绑定，凭条
        // 之后换额提交可按 r5 规则复用；paid 一律不碰隐藏 key 输入）。
        if (data && data.status === 'paid') {
          var paidRef = data.out_trade_no;
          if (typeof paidRef === 'string' && /^[0-9a-z]{26}$/.test(paidRef)
              && paidRef !== state.ref) {
            // 旧单已在他处换单且新单已支付：先把展示订单切换过去（换代作废
            // 旧单在途响应），凭条号/完整凭条链接/关弹层交付礼物全用新单；
            // 金额跟随两位小数负载，回退链接同 applyOrder 指向该单收银台。
            state.gen++;
            state.ref = paidRef;
            if (typeof data.amount_yuan === 'string' && data.amount_yuan) {
              state.amountYuan = data.amount_yuan;
            }
            fallbackLink.setAttribute('href', '/support/pay/' + paidRef);
          }
          // 未命名（旧单自身已支付）/同名/畸形 out_trade_no 落到这里：出示
          // 当前展示订单的凭条（簿记同 handleStatus paid）。200 明确结论
          // 同样证明会话拥有该单：解除会话变体标记。
          state.sessionLost = false;
          state.pollFails = 0;
          state.lastCheckOk = true;
          stopTimers();
          showPrintPane(data);
          return;
        }
        if (data && data.out_trade_no) {
          applyOrder(data, sentKey);      // 服务端确认关旧建新：全新二维码
          return;
        }
        if (data && data.status === 'neutral') {
          noteSessionLost();
          return;
        }
        if (data && data.status === 'unconfirmed') {
          // 200 unconfirmed 也证明会话拥有旧单（不属本会话一律答 neutral）：
          // 先解除标记，否则下面会错误渲染「会话可能已失效」文案
          state.sessionLost = false;
        }
        // unconfirmed / {error} / JSON 损坏 / 非 2xx：状态未知——立即切
        // 「暂时无法确认」并退避重查旧单
        noteUnknownState();
      }).catch(function () {
        state.inflight = false;
        if (regenerateBtn) regenerateBtn.disabled = false;
        if (stale(gen, oldRef)) return;
        noteUnknownState();
      });
    }

    /* ------------------------------------------------- 事件绑定 -- */
    form.addEventListener('submit', function (ev) {
      if (!desktop()) {
        // 移动端/窄屏/触屏：保持整页跳转（原防双击 UX 层）
        if (button) button.disabled = true;
        return;
      }
      ev.preventDefault();
      // 金额与当前订单不同 → 改用未绑定的 nextKey（见 submitKeyOverride）；
      // 其余情形 null → createOrder 沿用表单原 key（既有行为）
      createOrder(submitKeyOverride());
    });

    if (closeBtn) closeBtn.addEventListener('click', closeModal);
    if (deliverBtn) deliverBtn.addEventListener('click', closeModal);
    if (regenerateBtn) {
      regenerateBtn.addEventListener('click', function () {
        // 二维码过期：服务端先复核旧单再决定关旧建新（见 regenerateOrder）
        regenerateOrder();
      });
    }
    // 弹层打开时切语言：已支付窗格的动态文案跟随（data-i18n 元素由 i18n
    // 运行时自动刷新；标题在这里重渲染）
    document.addEventListener('hp-lang-change', function () {
      if (state.pane === 'print' && !modal.hidden) renderPrintTexts();
    });
  }

  // R2 add-on：档位预览——选中礼物档时，对应物品悬停在猫卡上方（纯展示；
  // 掉落/吃/安装动画只在付款核实后的喂猫互动播放）。自定义金额 → 无预览。
  function initTierPreview() {
    var form = document.getElementById('pay-pick-form');
    var stage = document.getElementById('cat-stage');
    if (!form || !stage) return;
    var TIERS = { '6.00': 'cat_food', '18.00': 'cat_toy', '68.00': 'mystery' };
    function sync() {
      var picked = form.querySelector('input[name="amount"]:checked');
      var tier = picked ? (TIERS[picked.value] || '') : '';
      stage.classList.remove('gift-preview-cat_food',
        'gift-preview-cat_toy', 'gift-preview-mystery');
      if (tier) stage.classList.add('gift-preview-' + tier);
    }
    form.addEventListener('change', sync);
    sync();
  }

  // 陪玩小猫（渐进增强；无 JS 时只有 CSS 待机动画）：视线跟随指针、点一下
  // 开心（^ω^ + 喵 + 飘心）、拖毛线球靠近会伸爪去拍、久不互动打盹。
  // 只切 class 与写 --eye-dx/--eye-dy（CSSOM 不受 style-src 限制）。
  // R4：同一闭包内实现喂猫互动（礼物交付/拖拽喂食/喂食场景），复用
  // pulse/wake/lookAt/bat 与拖拽钳制逻辑。
  function initCatPlay() {
    var stage = document.getElementById('cat-stage');
    if (!stage) return;
    var svg = stage.querySelector('.cat-svg');
    var toy = document.getElementById('cat-toy');
    var reduce = prefersReducedMotion();
    var SLEEP_MS = 25000;
    var VB_W = 260, VB_H = 190;
    var HEAD = { x: 108, y: 92 }, PAW = { x: 176, y: 170 };
    var timers = {};
    var lastBat = 0;
    var raf = 0, pending = null;

    /* -------------------------------------- 已喂指针（localStorage） -- */
    var FED_KEY = 'hp.support.gifts.fed.v1';
    // 纯装饰状态：礼物本体只能来自服务端核实的已支付订单（服务端渲染或
    // paid 状态 JSON）；这里只记录「喂过了」，存储异常时静默跳过。
    function loadFedMap() {
      try {
        var raw = window.localStorage.getItem(FED_KEY);
        var v = raw ? JSON.parse(raw) : null;
        return (v && typeof v === 'object' && !Array.isArray(v)) ? v : {};
      } catch (err) {
        return {};
      }
    }
    function saveFedRef(ref) {
      try {
        var m = loadFedMap();
        m[ref] = Date.now();
        window.localStorage.setItem(FED_KEY, JSON.stringify(m));
      } catch (err) { /* 存储不可用：跳过（刷新后礼物会再出现一次） */ }
    }

    /* ---------------------------------------------- 通用小工具 -- */
    function pulse(cls, ms) {
      stage.classList.remove(cls);
      void stage.getBoundingClientRect();
      stage.classList.add(cls);
      clearTimeout(timers[cls]);
      timers[cls] = setTimeout(function () { stage.classList.remove(cls); }, ms);
    }
    function look(dx, dy) {
      stage.style.setProperty('--eye-dx', dx.toFixed(2) + 'px');
      stage.style.setProperty('--eye-dy', dy.toFixed(2) + 'px');
    }
    function toClient(pt) {
      var r = svg.getBoundingClientRect();
      return { x: r.left + r.width * pt.x / VB_W, y: r.top + r.height * pt.y / VB_H };
    }
    function lookAt(x, y) {
      if (reduce || !svg || stage.classList.contains('is-sleeping')) return;
      var c = toClient(HEAD);
      var dx = x - c.x, dy = y - c.y;
      var d = Math.sqrt(dx * dx + dy * dy) || 1;
      var k = Math.min(1, d / 180);
      look(dx / d * 4.5 * k, dy / d * 3.5 * k);
    }
    function sleep() {
      look(0, 0);
      stage.classList.add('is-sleeping');
    }
    function wake() {
      stage.classList.remove('is-sleeping');
      clearTimeout(timers.sleep);
      timers.sleep = setTimeout(sleep, SLEEP_MS);
    }
    function pet() {
      if (feeding) return;          // 喂食场景进行中：不叠摸摸反应
      wake();
      pulse('is-happy', 1600);
    }
    function bat() {
      var now = Date.now();
      if (now - lastBat < 650) return;
      lastBat = now;
      pulse('is-batting', 500);
    }
    function announce(msg) {
      var live = document.getElementById('cat-live');
      if (live && msg) live.textContent = msg;
    }

    document.addEventListener('pointermove', function (ev) {
      pending = ev;
      if (raf) return;
      raf = window.requestAnimationFrame(function () {
        raf = 0;
        if (pending) lookAt(pending.clientX, pending.clientY);
      });
    }, { passive: true });

    stage.addEventListener('click', function (ev) {
      if (toy && toy.contains(ev.target)) return;
      var target = ev.target;
      if (target && target.closest && target.closest('.cat-gift')) return;
      pet();
    });
    stage.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        pet();
      }
    });

    /* ------------------------------------ 可拖元素（毛线球 + 礼物） -- */
    // 指针拖动：钳制在舞台内；拖动中猫眼跟随；靠近前爪会伸爪去拍。
    // opts.end(moved, ev) 返回 true 表示已自行处置（默认：拖过=开心，
    // 原地点按=拍爪）。
    function makeDraggable(el, opts) {
      opts = opts || {};
      var drag = null;
      el.addEventListener('pointerdown', function (ev) {
        if (feeding) return;              // 喂食场景进行中锁定
        if (ev.button !== undefined && ev.button !== 0) return;
        ev.preventDefault();
        if (el.setPointerCapture) el.setPointerCapture(ev.pointerId);
        drag = {
          id: ev.pointerId, x0: ev.clientX, y0: ev.clientY, moved: false,
          minX: -el.offsetLeft,
          maxX: stage.clientWidth - el.offsetLeft - el.offsetWidth,
          minY: -el.offsetTop,
          maxY: stage.clientHeight - el.offsetTop - el.offsetHeight
        };
        stage.classList.add('is-dragging');
        wake();
      });
      el.addEventListener('pointermove', function (ev) {
        if (!drag || ev.pointerId !== drag.id) return;
        var dx = Math.max(drag.minX, Math.min(drag.maxX, ev.clientX - drag.x0));
        var dy = Math.max(drag.minY, Math.min(drag.maxY, ev.clientY - drag.y0));
        if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
        drag.offset = { x: dx, y: dy };
        // 倾斜随拖拽横向位移（opts.tilt 度/像素；礼物比毛线球大，用小系数
        // 防止长距离拖拽时整圈打转）
        var tilt = dx * (opts.tilt || 1.5);
        el.style.transform = 'translate(' + dx.toFixed(1) + 'px,' +
          dy.toFixed(1) + 'px) rotate(' + tilt.toFixed(1) + 'deg)';
        var tr = el.getBoundingClientRect();
        var bx = tr.left + tr.width / 2, by = tr.top + tr.height / 2;
        lookAt(bx, by);
        if (svg) {
          var paw = toClient(PAW);
          var near = Math.max(56, svg.getBoundingClientRect().width * 0.16);
          if (Math.abs(bx - paw.x) + Math.abs(by - paw.y) < near) bat();
        }
      });
      var endDrag = function (ev) {
        if (!drag || ev.pointerId !== drag.id) return;
        var moved = drag.moved;
        var offset = drag.offset || { x: 0, y: 0 };
        drag = null;
        stage.classList.remove('is-dragging');
        if (opts.end) {
          // transform 的处置权交给 opts.end：喂食要「从落点滑向喂食点」
          //（保留当前视觉位），没喂才清 transform 弹回静息位。
          opts.end(moved, ev, offset);
          return;
        }
        el.style.transform = '';
        if (moved) pulse('is-happy', 1200);
        else bat();
      };
      el.addEventListener('pointerup', endDrag);
      el.addEventListener('pointercancel', endDrag);
    }

    if (toy) makeDraggable(toy);

    /* ------------------------------------------------ 喂猫互动 -- */
    // 各礼物的喂食点（舞台相对比例；猫粮/小鱼干在前爪前——原回跳页
    // cat_food 舞台的同一落点，老鼠在后爪外，光猫装在猫左手边）。
    var FEED_TARGETS = {
      cat_food: { left: 0.335, bottom: 0.045 },
      fish_treat: { left: 0.335, bottom: 0.045 },
      cat_toy: { left: 0.64, bottom: 0.05 },
      mystery: { left: 0.05, bottom: 0.05 },
    };
    var SCENE_CLASSES = ['is-feeding', 'feed-cat_food', 'feed-cat_toy',
      'feed-mystery', 'feed-fish_treat', 'is-eating', 'is-playing',
      'is-working', 'is-yum', 'is-gear-off', 'is-happy'];
    var GLIDE_MS = 700;
    var ACT_MS = { cat_food: 1600, fish_treat: 1600, cat_toy: 2000,
      mystery: 2000 };
    var HAPPY_MS = 1300;
    var feeding = false;

    function giftKind(item) {
      return item ? item.getAttribute('data-gift') : null;
    }

    function fedRef(item) {
      return item ? item.getAttribute('data-ref') : null;
    }

    function overCat(x, y) {
      if (!svg) return false;
      var r = svg.getBoundingClientRect();
      return x >= r.left + r.width * 0.22
        && x <= r.right - r.width * 0.22
        && y >= r.top + r.height * 0.18
        && y <= r.bottom - r.height * 0.08;
    }

    // 把礼物钉在当前（喂食点）位置：先写 left/bottom 再清滑行类与拖拽
    // transform 残留，避免 transform 过渡把礼物「弹回」静息位。
    function freezeAt(item) {
      var r = item.getBoundingClientRect();
      var sr = stage.getBoundingClientRect();
      item.style.transition = 'none';
      item.style.left = (r.left - sr.left) + 'px';
      item.style.bottom = (sr.bottom - r.bottom) + 'px';
      item.style.transform = '';
      void item.getBoundingClientRect();
      item.classList.remove('is-gliding');
      item.style.removeProperty('--feed-x0');
      item.style.removeProperty('--feed-y0');
      item.style.removeProperty('--feed-dx');
      item.style.removeProperty('--feed-dy');
      item.style.removeProperty('transition');
    }

    function syncTrayEmpty() {
      var tray = document.getElementById('cat-gift-tray');
      if (!tray) return;
      var visible = tray.querySelectorAll('.cat-gift-feed:not([hidden])');
      tray.classList.toggle('is-empty', visible.length === 0);
    }

    function removeTrayButton(ref) {
      var tray = document.getElementById('cat-gift-tray');
      var btn = tray
        ? tray.querySelector('.cat-gift-feed[data-ref="' + ref + '"]')
        : null;
      if (btn && btn.parentNode) btn.parentNode.removeChild(btn);
      syncTrayEmpty();
    }

    function setHint(key) {
      var hint = document.querySelector('.cat-hint');
      var msg = t(key);
      if (!hint || !msg) return msg;
      hint.setAttribute('data-i18n', key);
      hint.textContent = msg;
      return msg;
    }

    // 地板上一次只放一份待喂礼物（多份时其余排队，前一份喂完再掉落），
    // 免得礼物叠在猫爪/毛线球上。
    var giftQueue = [];

    function isPendingGift(item) {
      return !!item && !item.hidden && !item.classList.contains('is-spent')
        && !item.classList.contains('is-plaything')
        && !item.classList.contains('is-installed');
    }

    function floorGift() {
      var all = stage.querySelectorAll('.cat-gift');
      for (var i = 0; i < all.length; i++) {
        if (isPendingGift(all[i])) return all[i];
      }
      return null;
    }

    function syncGiftState() {
      var waiting = !!floorGift();
      stage.classList.toggle('has-gift', waiting);
      if (waiting && !feeding) setHint('support.cat.gift.waiting');
    }

    function placeOnFloor(item, animate) {
      // 左侧地板若已装好光猫，就往右挪一点，别叠在光猫上
      item.style.setProperty('--rest-x',
        stage.querySelector('.cat-gift.is-installed') ? '19%' : '4%');
      item.classList.remove('is-spent', 'is-eaten');
      item.hidden = false;
      wireGiftItem(item);
      if (animate && !reduce) {
        item.classList.remove('is-arriving');
        void item.getBoundingClientRect();
        item.classList.add('is-arriving');
        setTimeout(function () { item.classList.remove('is-arriving'); },
          950);
      }
      var btn = ensureTrayButton(fedRef(item), giftKind(item));
      if (btn) btn.hidden = false;
      syncTrayEmpty();
      syncGiftState();
      return btn;
    }

    function parkGift(item) {
      item.hidden = true;
      var btn = trayButtonFor(fedRef(item));
      if (btn) btn.hidden = true;
      giftQueue.unshift(item);
    }

    function finishFeed(item) {
      var kind = giftKind(item);
      var ref = fedRef(item);
      var eaten = kind === 'cat_food' || kind === 'fish_treat';
      if (eaten) item.classList.add('is-eaten');
      for (var i = 0; i < SCENE_CLASSES.length; i++) {
        stage.classList.remove(SCENE_CLASSES[i]);
      }
      if (eaten) {
        // 空碗/鱼骨淡出后移除（清掉拖拽 transform 残留，动画已持有位置）
        item.style.transform = '';
        item.classList.add('is-spent');
        setTimeout(function () { item.hidden = true; }, 450);
      } else {
        // 猫玩具留在舞台当第二件可拖玩具；光猫装在原地（LED 慢闪）
        freezeAt(item);
        item.classList.add(kind === 'cat_toy' ? 'is-plaything'
                                              : 'is-installed');
      }
      saveFedRef(ref);
      removeTrayButton(ref);
      feeding = false;
      var msg = setHint('support.cat.fed.' + kind);
      announce(msg || '');
      syncGiftState();
      if (giftQueue.length) {
        var next = giftQueue.shift();
        setTimeout(function () {
          if (!feeding && !floorGift()) placeOnFloor(next, true);
          else giftQueue.unshift(next);
        }, reduce ? 0 : 1600);
      }
    }

    function feedGift(item, from) {
      if (feeding || !item || item.hidden) return;
      var kind = giftKind(item);
      if (!FEED_TARGETS[kind]) return;
      feeding = true;
      wake();
      stage.classList.add('is-feeding');
      stage.classList.add('feed-' + kind);
      if (reduce) {
        // 减少动态：不走滑行/进食循环——礼物直接消失，猫呈现开心终态
        //（先回静息位：toy/mystery 驻留与 food 淡出都以静息位为准）
        item.style.transition = 'none';
        item.style.transform = '';
        item.style.removeProperty('transition');
        pulse('is-happy', HAPPY_MS);
        if (kind === 'cat_food' || kind === 'fish_treat') {
          item.classList.add('is-eaten', 'is-spent');
          pulse('is-yum', HAPPY_MS);
        }
        setTimeout(function () { finishFeed(item); }, HAPPY_MS + 100);
        return;
      }
      // 滑行到喂食点（起点/终点都由 JS 写入的 CSS 变量提供，keyframes 消费）。
      // keyframes 的位移作用在**静息布局盒**上：终点 = 喂食点 − 静息位，必须
      // 先清掉拖拽 transform 与回弹过渡、回静息位再测量，否则读到的是落点/
      // 弹回途中的位置，终点会算到舞台外（2026-10-21 视觉评审修复）。起点 =
      // 当前视觉位移（拖拽喂食为落点位移 from，点按/按钮为 0）——from 关键帧
      // 在同一帧内接管视觉位，不会先闪回静息位。
      var sr = stage.getBoundingClientRect();
      item.style.transition = 'none';
      item.style.transform = '';
      void item.getBoundingClientRect();
      var ir = item.getBoundingClientRect();
      var target = FEED_TARGETS[kind];
      var dx = (sr.left + sr.width * target.left) - ir.left;
      var dy = (sr.bottom - sr.height * target.bottom) - ir.bottom;
      item.style.setProperty('--feed-x0',
        ((from ? from.x : 0)).toFixed(1) + 'px');
      item.style.setProperty('--feed-y0',
        ((from ? from.y : 0)).toFixed(1) + 'px');
      item.style.setProperty('--feed-dx', dx.toFixed(1) + 'px');
      item.style.setProperty('--feed-dy', dy.toFixed(1) + 'px');
      item.classList.add('is-gliding');
      setTimeout(function () {
        if (kind === 'cat_food' || kind === 'fish_treat') {
          stage.classList.add('is-eating');
        } else if (kind === 'cat_toy') {
          stage.classList.add('is-playing');
        } else {
          stage.classList.add('is-working');
        }
      }, GLIDE_MS);
      setTimeout(function () {
        pulse('is-happy', HAPPY_MS);
        if (kind === 'cat_food' || kind === 'fish_treat') {
          pulse('is-yum', HAPPY_MS);
        }
        if (kind === 'mystery') stage.classList.add('is-gear-off');
      }, GLIDE_MS + ACT_MS[kind]);
      setTimeout(function () {
        finishFeed(item);
      }, GLIDE_MS + ACT_MS[kind] + HAPPY_MS + 100);
    }

    function wireGiftItem(item) {
      if (!item || item.dataset.giftWired === '1') return;
      item.dataset.giftWired = '1';
      makeDraggable(item, {
        tilt: 0.35,
        end: function (moved, ev, offset) {
          if (moved && overCat(ev.clientX, ev.clientY)) {
            feedGift(item, offset || { x: 0, y: 0 });  // 拖到猫身上：从落点滑向喂食点
            return;
          }
          item.style.transform = '';                   // 弹回静息位
          if (!moved) { feedGift(item); return; }      // 点/ tap = 喂
          pulse('is-happy', 1200);                     // 拖了没喂：开心一下
        },
      });
    }

    function trayButtonFor(ref) {
      var tray = document.getElementById('cat-gift-tray');
      return tray
        ? tray.querySelector('.cat-gift-feed[data-ref="' + ref + '"]')
        : null;
    }

    function ensureTrayButton(ref, kind) {
      var tray = document.getElementById('cat-gift-tray');
      if (!tray) return null;
      var btn = trayButtonFor(ref);
      if (!btn) {
        btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-secondary cat-gift-feed';
        btn.setAttribute('data-ref', ref);
        btn.setAttribute('data-gift', kind);
        btn.setAttribute('data-i18n', 'support.cat.feed.' + kind);
        tray.appendChild(btn);
      }
      // 服务端渲染的按钮同样要接喂食点击（幂等：只接一次）
      if (!btn.dataset.feedWired) {
        btn.dataset.feedWired = '1';
        btn.addEventListener('click', function () {
          var item = stage.querySelector('.cat-gift[data-ref="' + ref + '"]');
          if (item) feedGift(item);
        });
      }
      btn.textContent = t('support.cat.feed.' + kind)
        || btn.textContent;
      return btn;
    }

    function createGiftItem(ref, kind) {
      var item = document.createElement('div');
      item.className = 'cat-gift cat-gift-' + kind;
      item.setAttribute('data-ref', ref);
      item.setAttribute('data-gift', kind);
      item.setAttribute('aria-hidden', 'true');
      var src = document.getElementById('pay-gift-art-source');
      var tpl = src
        ? src.querySelector('[data-kind="' + kind + '"] svg') : null;
      if (tpl) item.appendChild(tpl.cloneNode(true));
      stage.appendChild(item);
      return item;
    }

    // 弹层「收下礼物」：交付到猫卡（掉落入场）+ 焦点移到喂食按钮
    function deliverGift(ref, kind) {
      if (!ref) return null;
      if (loadFedMap()[ref]) return trayButtonFor(ref);
      kind = FEED_TARGETS[kind] ? kind : 'fish_treat';
      var item = stage.querySelector('.cat-gift[data-ref="' + ref + '"]');
      if (!item) item = createGiftItem(ref, kind);
      giftQueue = giftQueue.filter(function (q) { return q !== item; });
      // 刚收下的礼物优先上地板；原先在地板上等着的那份回到队首
      var current = floorGift();
      if (current && current !== item && !feeding) parkGift(current);
      var btn;
      if (feeding && current !== item) {
        giftQueue.unshift(item);
        btn = null;
      } else {
        btn = placeOnFloor(item, true);
      }
      announce(t('support.cat.delivered') || '');
      setTimeout(function () {
        if (btn && !btn.hidden) {
          try { btn.focus({ preventScroll: true }); } catch (err) {
            btn.focus();
          }
        }
      }, reduce ? 0 : 600);
      return btn;
    }

    catWorld = { deliver: deliverGift };

    // 初始：只揭示本地未喂过的会话礼物（服务端渲染，默认 hidden）
    var items = stage.querySelectorAll('.cat-gift');
    var fedMap = loadFedMap();
    for (var gi = 0; gi < items.length; gi++) {
      var gItem = items[gi];
      var gRef = gItem.getAttribute('data-ref');
      var gKind = gItem.getAttribute('data-gift');
      if (!gRef || !FEED_TARGETS[gKind] || fedMap[gRef]) continue;
      if (floorGift()) giftQueue.push(gItem);
      else placeOnFloor(gItem, false);
    }
    syncTrayEmpty();

    // 动态创建的托盘按钮自带 data-i18n（语言切换由 i18n 运行时刷新）
    document.addEventListener('visibilitychange', function () {
      stage.classList.toggle('is-paused', document.hidden);
    });
    wake();
  }

  function init() {
    syncTitle();
    initPayPickGuard();
    initTierPreview();
    initCatPlay();
    document.addEventListener('hp-lang-change', syncTitle);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
