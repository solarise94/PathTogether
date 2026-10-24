/**
 * 支付弹层评审修复回归（2026-10-23，mac 支付评审 sw5H5x）。
 *
 * 来源：/mnt/ssd2/tests/review-mac-pay-20261023-sw5H5x/repro-payment-ui.cjs。
 * 按项目约定保留评审脚手架原文，仅把「锁定 BUG 行为」的断言反转为安全
 * 行为，并补充扩展用例。被测源文件：static/support/support.js
 * （harness 只切取 `  function initDesktopQrModal(` 到 `  // R2 add-on：档位预览`
 * 的源码段，在 vm 上下文中执行）。
 *
 * 断言反转（原脚本锁定的是 BUG 行为）：
 *   - 503 用例：原断言 `timeouts.size === 0`（一次 HTTP 503 永久终止轮询）
 *     → 现断言恰好排程一个退避重试（timeouts.size === 1），且重试拿到
 *     paid 后正常进入打印窗格；
 *   - 迟到响应用例：原断言 orderA 的迟到 paid 渲染成 orderB 的凭条
 *     （ref='orderB'/18.00/打印窗格可见）→ 现断言凭条保持空白/隐藏，B 的
 *     3s 轮询仍在排程，B 自己的 paid 正常打印 orderB。
 *
 * 脚手架改动（除此之外 harness() 逐字保留）：
 *   1. support.js 改为相对仓库根读取（原脚本用评审机的绝对路径）；
 *   2. 新增 `fire(delayMs)` 辅助（按任意延迟取消防退避定时器并触发），
 *      供扩展用例驱动非 3000ms 的退避重试；原 `poll()`（只找 3000ms）保留；
 *   3. 顶部 IIFE 拆分为若干 it() 用例；截图/打印语句改为 vitest 断言；
 *   4. r2 复核（2026-10-23）新增：harness() 接受可选 `{ fixedNow }`——给定
 *      时长，vm 上下文的 Date 换成 now() 固定返回该值的 Date 子类（parse/
 *      构造仍是真 Date），用于确定性复现「首帧 0:00 但 Date.now() 仍 <
 *      expiresAtMs」的末秒窗口（免墙钟抖动）；未给定时与原脚手架一致
 *      （宿主 Date 直传）。配套新增两个末秒窗口用例（见文件末尾 describe）。
 *
 * round-2 契约更新（2026-10-23 rereview SIpZHT）：状态端点对 created/paying
 * 一律改答 HTTP 200 {"status": "...", "confirmed": <bool>, "expired": <bool>}
 * （confirmed=3s 内支付宝查证「未支付」成功；expired=服务端权威「已过期」，
 * 仅与 confirmed 同时出现）。只改以下响应负载，其余断言一律不动（round-1
 * repro 移植的 503 / 迟到响应 / 关弹层在途用例原文保留）：
 *   1. 「连续失败按指数退避…回到 3s 周期」末尾的 `{"status":"created"}` →
 *      `{"status":"created","confirmed":true,"expired":false}`（语义：查询
 *      成功、仍在等待支付 → 回 3s 正常周期）；
 *   2. 「到期时最近一次查询失败…」中收口用的 `{"status":"paying"}` →
 *      `{"status":"paying","confirmed":true,"expired":true}`（语义：到期后
 *      服务端确认未支付且已过支付宝截止 → 权威「已过期」）；
 *   3. 「到期后最后一次成功查询确认未支付（created）」的 `{"status":"created"}`
 *      → `{"status":"created","confirmed":true,"expired":true}`（同上）；
 *   4. 末秒窗口用例「最后查询返回 paying → 立即权威过期」编码的是旧客户端
 *      时钟规则，整体替换为两个用例：(a) confirmed+未过期 @0:00 → QR 保持
 *      0:00、照排 3s 轮询、不宣判过期；(b) confirmed+expired → 立即过期、
 *      不再排程（见文件末尾 describe）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const source = readFileSync(resolve(root, "static/support/support.js"), "utf8");
const fn = source.slice(
  source.indexOf("  function initDesktopQrModal("),
  source.indexOf("  // R2 add-on：档位预览"));
const tick = () => new Promise((resolve) => setImmediate(resolve));

// ---- 评审脚手架（repro-payment-ui.cjs harness() 原文；见文件头改动清单）----
function harness(opts?: { fixedNow?: number }) {
  // r2 复核新增（第 4 项脚手架改动）：fixedNow 给定时，vm 的 Date.now() 固定
  const fixedNow = opts && typeof opts.fixedNow === "number"
    ? opts.fixedNow : null;
  let seq = 0;
  const timeouts = new Map();
  const intervals = new Map();
  const events = {};
  function el(id) {
    return { id, hidden: id === 'pay-modal', textContent: '', dataset: {},
      addEventListener(k, f) { events[id + ':' + k] = f; },
      setAttribute() {}, removeAttribute() {}, focus() {}, appendChild() {},
      querySelector() { return null; }, querySelectorAll() { return []; },
      getBoundingClientRect() { return {}; },
      classList: {add() {}, remove() {}},
    };
  }
  const nodes = new Map();
  const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id, el(id)); return nodes.get(id); },
    addEventListener() {}, removeEventListener() {}, activeElement: null,
    createElement() {return el('iframe');} };
  const requests = [];
  class CtxDate extends Date {}
  if (fixedNow !== null) CtxDate.now = () => fixedNow;
  const ctx = {document, t: () => '', prefersReducedMotion: () => true, localDateIso: () => '2026-10-23',
    POLL_INTERVAL_MS: 3000, POLL_BACKOFF_MS: 6000, DESKTOP_QUERY: 'desktop', TOUCH_QUERY: 'touch',
    catWorld: null, Date: fixedNow !== null ? CtxDate : Date, FormData: class {set() {}},
    fetch(url) {return new Promise(resolve => requests.push({url, resolve}));},
    window: {matchMedia(q) {return {matches: q === 'desktop'};},
      setTimeout(f, delay) {const id=++seq; timeouts.set(id,{f,delay}); return id;},
      clearTimeout(id) {timeouts.delete(id);},
      setInterval(f, delay) {const id=++seq; intervals.set(id,{f,delay}); return id;},
      clearInterval(id) {intervals.delete(id);}},
  };
  vm.createContext(ctx);
  vm.runInContext(fn + '\nthis.start = initDesktopQrModal;', ctx);
  const form = document.getElementById('form'); form.action = '/support/orders'; form.submit = () => {};
  ctx.start(form, document.getElementById('pay-modal'));
  function submit() {events['form:submit']({preventDefault(){}});}
  function respond(index, payload, status=200) {requests[index].resolve({status, ok: status>=200 && status<300, json:()=>Promise.resolve(payload)});}
  function poll() {const pair=[...timeouts].find(([,v])=>v.delay===3000); assert.ok(pair); timeouts.delete(pair[0]); pair[1].f();}
  function order(ref) {return {out_trade_no: ref, amount_yuan:'6.00', pay_url:'/support/pay/'+ref,
    expires_at: new Date(Date.now()+900000).toISOString(), next_idempotency_key:'key-'+ref};}
  return {nodes, timeouts, requests, events, submit, respond, poll, order};
}

/** 扩展辅助：按任意延迟触发一个已排程的 setTimeout（退避重试不是 3000ms，
 *  评审脚手架的 poll() 只找 3000ms）。 */
function fire(h: ReturnType<typeof harness>, delay: number): void {
  const pair = [...h.timeouts].find(([, v]) => v.delay === delay);
  assert.ok(pair, "应有 delay=" + delay + "ms 的已排程定时器");
  h.timeouts.delete(pair[0]);
  pair[1].f();
}

/** 扩展辅助：过去某一时刻的 expires_at（applyOrder 的同步首帧倒计时即为
 *  「到期收口」路径，免驱动真实时钟）。 */
function expiredOrder(h: ReturnType<typeof harness>, ref: string) {
  const ord = h.order(ref);
  ord.expires_at = new Date(Date.now() - 1000).toISOString();
  return ord;
}

describe("支付弹层评审修复（2026-10-23）", () => {
  it("一次 HTTP 503 不再永久终止轮询：恰好排程一个退避重试", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, null, 503); await tick();
    assert.equal(h.timeouts.size, 1);
    // 退避起点 = POLL_BACKOFF_MS（6s）
    assert.equal([...h.timeouts.values()][0].delay, 6000);
  });

  it("503 后的退避重试继续查单：paid 正常进入打印窗格", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, null, 503); await tick();
    fire(h, 6000);
    h.respond(2, { status: "paid", amount_yuan: "6.00", gift: "cat_food" });
    await tick();
    assert.equal(h.nodes.get("pay-print-ref").textContent, "orderA");
    assert.equal(h.nodes.get("pay-modal-print").hidden, false);
    // 打印窗格即终态：不再排程任何轮询
    assert.equal(h.timeouts.size, 0);
  });

  it("关弹层后旧单的迟到 paid 绝不渲染、绝不排程；新单 B 轮询照常", async () => {
    const r = harness();
    r.submit(); r.respond(0, r.order("orderA"), 201); await tick(); r.poll();
    // A 的查单在途时关弹层，再下新单 B（不同金额档由 B 的响应体现）
    r.events["pay-modal-close:click"]();
    r.submit(); r.respond(2, r.order("orderB"), 201); await tick();
    // A 的迟到 paid：整体丢弃
    r.respond(1, { status: "paid", amount_yuan: "18.00", gift: "cat_toy" });
    await tick();
    assert.equal(r.nodes.get("pay-print-ref").textContent, "");
    assert.equal(r.nodes.get("pay-print-amount").textContent, "");
    assert.equal(r.nodes.get("pay-modal-print").hidden, true);
    // B 自己的 3s 轮询仍在排程（没有被 A 的迟到响应杀掉）
    assert.equal(r.timeouts.size, 1);
    assert.equal([...r.timeouts.values()][0].delay, 3000);
    // 扩展：B 自己的 paid 正常打印 B
    fire(r, 3000);
    r.respond(3, { status: "paid", amount_yuan: "18.00", gift: "cat_toy" });
    await tick();
    assert.equal(r.nodes.get("pay-print-ref").textContent, "orderB");
    assert.equal(r.nodes.get("pay-print-amount").textContent, "18.00");
    assert.equal(r.nodes.get("pay-modal-print").hidden, false);
  });

  it("弹层关闭时在途查单的响应：不进 UI、不隐式续排轮询", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.events["pay-modal-close:click"]();
    h.respond(1, { status: "paid", amount_yuan: "6.00", gift: "cat_food" });
    await tick();
    assert.equal(h.timeouts.size, 0);
    assert.equal(h.nodes.get("pay-modal-print").hidden, true);
    assert.equal(h.nodes.get("pay-print-ref").textContent, "");
  });

  it("连续失败按指数退避（非递减、封顶 30s）；一次成功回到 3s 周期", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    const delays: number[] = [];
    h.respond(1, null, 503); await tick();
    for (let i = 0; i < 5; i++) {
      const entry = [...h.timeouts.values()][0];
      assert.ok(entry);
      delays.push(entry.delay);
      fire(h, entry.delay);
      h.respond(2 + i, null, 503);
      await tick();
    }
    assert.deepEqual(delays, [6000, 12000, 24000, 30000, 30000]);
    // 成功一次：失败计数清零，回到 3s 正常周期
    // （round-2：created 需 confirmed=true 才算查询成功）
    fire(h, 30000);
    h.respond(7, { status: "created", confirmed: true, expired: false });
    await tick();
    assert.deepEqual([...h.timeouts.values()].map((v) => v.delay), [3000]);
  });

  it("429 限流保持既有固定退避（6s）", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, null, 429); await tick();
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 6000);
    // 二维码窗格保持（429 只是限流，不切窗格）
    assert.equal(h.nodes.get("pay-modal-qr").hidden, false);
  });

  it("未到期的查询失败：保持二维码窗格继续退避，不误报过期", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, null, 503); await tick();
    assert.equal(h.nodes.get("pay-modal-qr").hidden, false);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.timeouts.size, 1);
  });

  it("到期时最近一次查询失败：显示「暂时无法确认」而非「已过期」，并继续重试；paying 确认未支付 → 过期", async () => {
    const h = harness();
    h.submit(); h.respond(0, expiredOrder(h, "orderA"), 201); await tick();
    // applyOrder 的同步首帧已触发到期收口：立即最后一次查询（此时在途），
    // 尚无失败查询 → 不抢先宣判
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    h.respond(1, null, 503); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    // 继续重试：退避定时器仍在
    assert.equal(h.timeouts.size, 1);
    // 后续成功查询确认「未支付且已过支付宝截止」（round-2：confirmed+
    // expired 由服务端权威裁定）→ 权威「已过期」
    fire(h, 6000);
    h.respond(2, { status: "paying", confirmed: true, expired: true });
    await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.timeouts.size, 0);
  });

  it("到期后查询失败 → 重试拿到 paid：进入打印窗格，绝不误报过期", async () => {
    const h = harness();
    h.submit(); h.respond(0, expiredOrder(h, "orderA"), 201); await tick();
    h.respond(1, null, 503); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    fire(h, 6000);
    h.respond(2, { status: "paid", amount_yuan: "6.00", gift: "cat_food" });
    await tick();
    assert.equal(h.nodes.get("pay-modal-print").hidden, false);
    assert.equal(h.nodes.get("pay-print-ref").textContent, "orderA");
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
  });

  it("到期后最后一次成功查询确认未支付（created）→ 权威「已过期」", async () => {
    const h = harness();
    h.submit(); h.respond(0, expiredOrder(h, "orderA"), 201); await tick();
    // round-2：confirmed+expired 才是权威「已过期」
    h.respond(1, { status: "created", confirmed: true, expired: true }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.timeouts.size, 0);
  });

  it("「重新生成」请求在途时关弹层：迟到的建单响应不重开弹层", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "closed" }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    // 点「重新生成」：建新单请求在途
    h.events["pay-modal-regenerate:click"]();
    assert.equal(h.requests.length, 3);
    // 建单在途时关弹层
    h.events["pay-modal-close:click"]();
    const orderC = h.order("orderC");
    orderC.amount_yuan = "18.00";
    h.respond(2, orderC, 201); await tick();
    assert.equal(h.nodes.get("pay-modal").hidden, true);          // 未重开
    assert.equal(h.nodes.get("pay-modal-amount").textContent, "6.00"); // 未应用 C
    assert.equal(h.timeouts.size, 0);                             // 无轮询排程
  });

  it("模板/i18n：unconfirmed 窗格存在且默认 hidden，无重新生成按钮；zh/en 键成对", () => {
    const html = readFileSync(resolve(root, "templates/support.html"), "utf8");
    const panes = html.slice(html.indexOf('class="pay-modal-panes"'),
      html.indexOf('id="pay-gift-art-source"'));
    expect(panes).toContain('id="pay-modal-unconfirmed" hidden');
    const pane = html.slice(html.indexOf('id="pay-modal-unconfirmed"'),
      html.indexOf("</div>", html.indexOf('id="pay-modal-unconfirmed"')));
    expect(pane).toContain('data-i18n="support.modal.unconfirmed.title"');
    expect(pane).toContain('data-i18n="support.modal.unconfirmed.body"');
    expect(pane).not.toContain("pay-modal-regenerate");
    const i18n = readFileSync(resolve(root, "static/i18n.js"), "utf8");
    for (const key of ["support.modal.unconfirmed.title",
                       "support.modal.unconfirmed.body"]) {
      expect(i18n.split(`"${key}"`).length - 1).toBe(2); // zh + en 各一条
    }
  });
});

// r2 复核（2026-10-23；round-2 契约更新）：末秒窗口。倒计时用 Math.floor——
// 首帧 0:00 可早于 expiresAtMs 最多 1s。handleDeadline 一旦运行，「到期」
// 必须对本单其余生命周期成立（deadlineReached 标记），否则最后一次查询在
// 末秒内返回时：
//   (a) 失败 → QR 在 0:00 干等下一次退避（约 6s）才提示「暂时无法确认」；
//   (b) confirmed 未过期 → QR 保持 0:00、照排 3s 轮询等服务端下一步结论；
//   (c) confirmed+expired → 立即权威「已过期」。
// 过期与否由服务端 confirmed/expired 权威裁定（round-2 起），客户端时钟
// 只决定失败如何呈现。用固定 now 的 Date 子类（脚手架第 4 项）确定性复现：
// expires_at 在 0.9s 后，vm 内 Date.now() 固定在 t0+0.1s（首帧即 0:00，
// 且始终 < expiresAtMs）。
describe("支付弹层末秒窗口（r2 复核 2026-10-23）", () => {
  /** <1s 后到期的订单 + 固定 now 的 harness：首帧倒计时即 0:00 触发到期
   *  收口，而 Date.now() 仍 < expiresAtMs（缺陷窗口）。 */
  function endgameHarness(ref: string) {
    const t0 = Date.now();
    const h = harness({ fixedNow: t0 + 100 });
    const ord = h.order(ref);
    ord.expires_at = new Date(t0 + 900).toISOString();
    return { h, ord };
  }

  it("到期收口后的最后一次查询失败（此前从未失败）→ 立即显示「暂时无法确认」并恰好排程一个退避重试", async () => {
    const { h, ord } = endgameHarness("orderA");
    h.submit(); h.respond(0, ord, 201); await tick();
    // 首帧 0:00 已触发到期收口（最后一次查询在途）；此刻尚无失败查询，
    // 不得抢先宣判
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    h.respond(1, null, 503); await tick();
    // 必须立即切换，而不是让 QR 在 0:00 干等下一次退避重试
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 6000);
  });

  it("到期收口后的最后一次查询返回 confirmed 未过期 → QR 保持 0:00 等服务端结论，照排 3s 轮询，不宣判过期（round-2）", async () => {
    const { h, ord } = endgameHarness("orderA");
    h.submit(); h.respond(0, ord, 201); await tick();
    // round-2：裸 paying 已是「未确认」查询；契约定义 confirmed=true、
    // expired=false 表示「查询成功、仍在等待支付」
    h.respond(1, { status: "paying", confirmed: true, expired: false }); await tick();
    assert.equal(h.nodes.get("pay-modal-qr").hidden, false);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 3000);
  });

  it("到期收口后的最后一次查询返回 confirmed+expired → 立即权威「已过期」，不再排程任何轮询（round-2）", async () => {
    const { h, ord } = endgameHarness("orderA");
    h.submit(); h.respond(0, ord, 201); await tick();
    h.respond(1, { status: "paying", confirmed: true, expired: true }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.timeouts.size, 0);
  });
});
