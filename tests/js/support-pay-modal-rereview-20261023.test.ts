/**
 * 支付弹层 r2 复核修复回归（2026-10-23，re-review SIpZHT）。
 *
 * 修复的两个评审发现：
 *   [P2] 截止后任何 HTTP 200 created/paying 都被当成「确认未支付」→ 误判
 *        「已过期」+ regenerate、轮询终止。服务端 Alipay 查单超时（网关
 *        超时）时也答 200 裸 created/paying——本地状态未经支付宝确认绝不
 *        能当未支付，必须保持「暂时无法确认」并重试。
 *   [P3] 截止前查询正常、0:00 终查拿 429：429 分支只排程不切窗格，tick
 *        已停，重复 429 永远不切「暂时无法确认」，二维码停在 0:00。
 *
 * 服务端契约（round-2 起）：created/paying 一律 HTTP 200
 *   {"status": "<created|paying>", "confirmed": <bool>, "expired": <bool>}
 * confirmed=true：3s 内支付宝查证「未支付」成功；expired=true（只与
 * confirmed 同时出现）：服务端在支付宝截止 ≥10s 后查证未支付，是唯一
 * created/paying 系「已过期」结论（客户端时钟不再裁定）。paid / closed /
 * neutral / 429 不变。查单失败（网关超时）的响应体是跨层共享夹具
 * tests/fixtures/support/status_query_failed.json。
 *
 * 脚手架来源：/mnt/ssd2/tests/re-review-mac-pay-20261023-SIpZHT/
 * repro-pending-and-429.cjs = round-1 harness（/mnt/ssd2/tests/
 * review-mac-pay-20261023-sw5H5x/repro-payment-ui.cjs）逐字 + 评审人 r2 的
 * 唯一脚手架改动（harness() 返回值加 `intervals`）。场景 1/2 即该 repro
 * 的两个场景，按项目约定只反转「锁定 BUG 行为」的断言。
 *
 * 脚手架/负载改动清单（除此之外 harness() 逐字保留，含评审人的 intervals）：
 *   1. support.js 改为相对仓库根读取（原脚本用评审机的绝对路径）；
 *   2. 顶部 IIFE 拆分为若干 it() 用例；console.log 改为 vitest 断言；
 *   3. 新增辅助：fire(delayMs)（按任意延迟触发退避重试）、expiredOrder()
 *      （expires_at 在过去的订单，首帧倒计时即 0:00）、withFrozenNow()
 *      （评审人场景 2 的全局 Date.now 覆盖封装，finally 恢复）、
 *      reach429AtDeadline()（扩展用例复用场景 2 前奏）；
 *   4. 场景 1 负载改为共享夹具（评审人原脚本从其运行目录读
 *      gateway-timeout-status.json，由 Python 侧导出；夹具化后前后端共用
 *      同一契约），并新增其字面 legacy 变体 {"status":"paying"}；
 *   5. 场景 2 截止前的「正常」查询负载由 {status:'paying'} 改为
 *      {status:'paying', confirmed:true, expired:false}：新契约下裸负载是
 *      「未确认」查询——会按失败处理、提前弹「暂时无法确认」，走不到要
 *      复现的 429-at-0:00 路径（评审原脚本写于旧契约，裸负载当时表示
 *      「查询成功仍在等待」）。
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

// 跨层共享契约夹具：服务端 Alipay 查单失败（网关超时）时的响应体。
// 不得改tests/fixtures/support/status_query_failed.json——后端测试同一份。
const gatewayTimeoutPayload = JSON.parse(readFileSync(
  resolve(root, "tests/fixtures/support/status_query_failed.json"), "utf8"));

// ---- 评审脚手架（round-1 harness() 原文 + 评审人 r2 的 intervals 改动）----
function harness() {
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
  const ctx = {document, t: () => '', prefersReducedMotion: () => true, localDateIso: () => '2026-10-23',
    POLL_INTERVAL_MS: 3000, POLL_BACKOFF_MS: 6000, DESKTOP_QUERY: 'desktop', TOUCH_QUERY: 'touch',
    catWorld: null, Date, FormData: class {set() {}},
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
  return {nodes, timeouts, intervals, requests, events, submit, respond, poll, order};
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

/** 评审人场景 2 的全局 Date.now 覆盖（finally 恢复；clock.value 可推进）。 */
async function withFrozenNow(run: (clock: { value: number }) => Promise<void>) {
  const realNow = Date.now;
  const clock = { value: realNow() };
  Date.now = () => clock.value;
  try { await run(clock); } finally { Date.now = realNow; }
}

/** 扩展用例共用的场景 2 前奏（在 withFrozenNow 内调用）：把弹层驱动到
 *  「0:00 后第一次 429」——「暂时无法确认」可见、QR 隐藏、6s 退避已排程；
 *  返回 harness 供后续重试收口。 */
async function reach429AtDeadline(
  clock: { value: number }, ref: string): Promise<ReturnType<typeof harness>> {
  const r = harness();
  const ord = r.order(ref);
  ord.expires_at = new Date(clock.value + 7000).toISOString();
  r.submit(); r.respond(0, ord, 201); await tick();
  r.poll();
  // 截止前的「正常」查询：新契约下 confirmed=true 才算查询成功
  r.respond(1, { status: "paying", confirmed: true, expired: false }); await tick();
  clock.value += 7000;
  [...r.intervals.values()][0].f();          // tick 推进 7s → 0:00 → 到期收口
  assert.equal(r.intervals.size, 0);         // tick 已停
  r.respond(2, null, 429); await tick();
  assert.equal(r.nodes.get("pay-modal-countdown").textContent, "0:00");
  assert.equal(r.nodes.get("pay-modal-unconfirmed").hidden, false);
  assert.equal(r.nodes.get("pay-modal-qr").hidden, true);
  assert.equal([...r.timeouts.values()][0].delay, 6000);
  return r;
}

describe("支付弹层 r2 复核（2026-10-23 rereview SIpZHT）", () => {
  it("场景1（P2）：到期后网关超时负载（共享夹具）→ 「暂时无法确认」，绝不「已过期」，6s 退避重试", async () => {
    const h = harness();
    const order = h.order("gateway-timeout-order");
    order.expires_at = new Date(Date.now() - 1000).toISOString();
    h.submit(); h.respond(0, order, 201); await tick();
    h.respond(1, gatewayTimeoutPayload); await tick();
    // 反转的 BUG 断言（原脚本锁定 expired 可见、轮询永久停止）
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 6000);
  });

  it("场景1变体：到期后裸 {status:'paying'}（legacy/未确认负载）→ 同一安全结果", async () => {
    const h = harness();
    const order = h.order("gateway-timeout-order");
    order.expires_at = new Date(Date.now() - 1000).toISOString();
    h.submit(); h.respond(0, order, 201); await tick();
    h.respond(1, { status: "paying" }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 6000);
  });

  it("场景2（P3）：0:00 后的 429 → 立即「暂时无法确认」，固定 6s 退避；重复 429 不再回 QR", async () => {
    const realNow = Date.now;
    let now = realNow();
    Date.now = () => now;
    try {
      const r = harness();
      const order429 = r.order("limited-order");
      order429.expires_at = new Date(now + 7000).toISOString();
      r.submit(); r.respond(0, order429, 201); await tick();
      r.poll();
      // 截止前的「正常」查询负载改为 confirmed=true、expired=false（见
      // 文件头脚手架/负载改动 5：裸负载在新契约下走不到 429-at-0:00 路径）
      r.respond(1, { status: "paying", confirmed: true, expired: false }); await tick();
      now += 7000;
      [...r.intervals.values()][0].f();
      assert.equal(r.intervals.size, 0);
      r.respond(2, null, 429); await tick();
      assert.equal(r.nodes.get("pay-modal-countdown").textContent, "0:00");
      // 反转的 BUG 断言（原脚本锁定「unconfirmed 永不出现、QR 一直可见」）
      assert.equal(r.nodes.get("pay-modal-unconfirmed").hidden, false);
      assert.equal(r.nodes.get("pay-modal-qr").hidden, true);
      assert.equal([...r.timeouts.values()][0].delay, 6000);
      const retry = [...r.timeouts][0]; r.timeouts.delete(retry[0]); retry[1].f();
      r.respond(3, null, 429); await tick();
      assert.equal(r.nodes.get("pay-modal-unconfirmed").hidden, false);
      assert.equal(r.intervals.size, 0);
    } finally {
      Date.now = realNow;
    }
  });

  it("扩展：截止前裸 {status:'paying'}（未确认）→ QR 保持，按 6s 退避重试（不是 3s）", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "paying" }); await tick();
    assert.equal(h.nodes.get("pay-modal-qr").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 6000);
  });

  it("扩展：0:00 后 confirmed 未过期 → QR 停在 0:00、3s 轮询照排；随后 confirmed+expired → 过期收口", async () => {
    const h = harness();
    h.submit(); h.respond(0, expiredOrder(h, "orderA"), 201); await tick();
    h.respond(1, { status: "paying", confirmed: true, expired: false }); await tick();
    // 查询成功但未过期：不切窗格，QR 停在 0:00 等服务端结论
    assert.equal(h.nodes.get("pay-modal-qr").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.nodes.get("pay-modal-countdown").textContent, "0:00");
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 3000);
    fire(h, 3000);
    h.respond(2, { status: "created", confirmed: true, expired: true }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.timeouts.size, 0);
  });

  it("扩展：倒计时未归零时服务端就给出 confirmed+expired → 立即过期（服务端权威，客户端时钟不参与裁定）", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "created", confirmed: true, expired: true }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    assert.equal(h.nodes.get("pay-modal-qr").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.timeouts.size, 0);
    assert.equal(h.intervals.size, 0);
  });

  it("扩展：expired=true 但缺 confirmed → 不是过期结论（按未确认查询退避重试）", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "created", expired: true }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.nodes.get("pay-modal-qr").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 6000);
  });

  it("扩展：到期后 429 → 重试拿到 confirmed+expired：权威「已过期」收口", async () => {
    await withFrozenNow(async (clock) => {
      const r = await reach429AtDeadline(clock, "limited-order");
      const retry = [...r.timeouts][0]; r.timeouts.delete(retry[0]); retry[1].f();
      r.respond(3, { status: "paying", confirmed: true, expired: true }); await tick();
      assert.equal(r.nodes.get("pay-modal-expired").hidden, false);
      assert.equal(r.nodes.get("pay-modal-unconfirmed").hidden, true);
      assert.equal(r.timeouts.size, 0);
      assert.equal(r.intervals.size, 0);
    });
  });

  it("扩展：到期后 429 → 重试拿到 paid：打印窗格收口，绝不误报过期", async () => {
    await withFrozenNow(async (clock) => {
      const r = await reach429AtDeadline(clock, "limited-order");
      const retry = [...r.timeouts][0]; r.timeouts.delete(retry[0]); retry[1].f();
      r.respond(3, { status: "paid", amount_yuan: "6.00", gift: "cat_food" }); await tick();
      assert.equal(r.nodes.get("pay-modal-print").hidden, false);
      assert.equal(r.nodes.get("pay-print-ref").textContent, "limited-order");
      assert.equal(r.nodes.get("pay-modal-unconfirmed").hidden, true);
      assert.equal(r.nodes.get("pay-modal-expired").hidden, true);
      assert.equal(r.timeouts.size, 0);
    });
  });
});
