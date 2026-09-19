/* 等待页（templates/activate.html「申请测试」pending 区块）轮询控制器
 * （R7 2026-09-19 修复：轮询上限失效）。
 *
 * 修复前 activate.html 内联的 startPolling() 每次 loadState 收到 pending 都被
 * showApplyBlock("pending") 重新调用，pollRequestsLeft 重置为 20——预算永不
 * 耗尽。本文件把轮询控制抽成不依赖真实 DOM 的控制器工厂，由 activate.html
 * 内联脚本注入依赖后装配；tests/js/activate-waiting.test.ts 用假定时器/
 * 假 loadState 锁定行为。
 *
 * 预算语义（ reviewer 要求显式写明）：
 *   - 只在「进入等待状态」（从非 pending 区块切到 pending，或页面加载后
 *     首次进入 pending）时初始化预算并启动定时器；
 *   - 已在 pending 态时再次收到 pending（普通轮询/刷新回调）**不得**重置
 *     预算、不得重启定时器；
 *   - 预算耗尽后保持停止：后续 pending 响应不重新武装；
 *   - 手动「刷新状态」是用户显式操作：只发单次请求，**不**重新武装轮询
 *     预算（也不重置剩余次数）——避免「手动刷几下又续命 20 次」绕过上限；
 *   - 页面 hidden 时定时器照走但不发请求（预算不动）；恢复可见后继续，
 *     剩余预算保留；
 *   - 离开 pending 区块 / pagehide / beforeunload 调 stop()：停表清预算；
 *     之后若再次进入 pending（新等待回合，如 load-error 恢复）重新武装。
 */
(function (global) {
  'use strict';

  function createWaitingController(deps) {
    deps = deps || {};
    var loadState = deps.loadState || function () {};
    var intervalMs = deps.intervalMs || 15000;
    var maxRequests = deps.maxRequests || 20;
    var isHidden = deps.isHidden || function () { return false; };
    var setIntervalFn = deps.setInterval ||
      function (fn, ms) { return global.setInterval(fn, ms); };
    var clearIntervalFn = deps.clearInterval ||
      function (id) { return global.clearInterval(id); };

    var timer = null;      // 定时器句柄（null = 未在轮询）
    var requestsLeft = 0;  // 剩余自动请求预算
    var drained = false;   // 预算已耗尽：保持停止，直到离开等待状态（stop）

    function tick() {
      if (isHidden()) return;          // 后台标签页不发请求（预算不动）
      if (requestsLeft <= 0) {
        // 预算耗尽：停表并保持停止——pending 响应再次到达不得重新武装；
        // 手动刷新是用户显式操作，只发单次请求（见文件头预算语义）。
        if (timer !== null) {
          clearIntervalFn(timer);
          timer = null;
        }
        drained = true;
        return;
      }
      requestsLeft -= 1;
      loadState();
    }

    return {
      /* 进入等待态（showApplyBlock("pending") 调用）：幂等——已在轮询或
       * 已耗尽时不重置预算、不重启定时器（本轮修复的核心）。 */
      enterPending: function () {
        if (timer !== null || drained) return;
        requestsLeft = maxRequests;
        timer = setIntervalFn(tick, intervalMs);
      },
      /* 离开等待态 / 离开页面（非 pending 区块、pagehide、beforeunload）。 */
      stop: function () {
        if (timer !== null) {
          clearIntervalFn(timer);
          timer = null;
        }
        requestsLeft = 0;
        drained = false;
      },
      isPolling: function () { return timer !== null; },
      requestsLeft: function () { return requestsLeft; }
    };
  }

  global.HPActivateWaiting = { createWaitingController: createWaitingController };
})(typeof window !== 'undefined' ? window : this);
