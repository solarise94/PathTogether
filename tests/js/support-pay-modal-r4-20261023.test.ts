/**
 * 支付弹层 r4 语义修复回归（2026-10-23）。
 *
 * 本轮用户决定的两处行为变更：
 *   A. neutral（会话不再拥有这笔订单）不得再当作「已过期」：立即切「暂时
 *      无法确认」窗格的会话变体（会话正文 + 订单号 + 联系入口，mailto 主题
 *      带订单号），到不到期都立即切；变体对本单粘滞（后续失败/429 不降级）。
 *      后续明确结论照常收口：paid → 凭条；closed / confirmed+expired →
 *      过期；confirmed 未过期 → 清除标记（未到期回二维码窗格；已到期保持
 *      窗格但换回常规正文）。
 *   B. 「重新生成」改走服务端复核端点 POST /support/orders/<ref>/regenerate
 *      （表单带 idempotency_key=nextKey）：201/200 带 out_trade_no → 新单
 *      二维码；{status:'paid'} → 旧单凭条；{status:'neutral'} → 会话变体；
 *      {status:'unconfirmed'} / 非 2xx / JSON 损坏 / 网络异常 → 「暂时无法
 *      确认」+ 退避轮询旧单；绝不回退整页表单提交；双击只发一次请求；
 *      在途时关弹层 → 响应整体丢弃（不重开、不排程）。
 *
 * r4 复核修正（同日）：服务端对不属于本会话的订单号一律答 neutral，所以
 * 任何非 neutral 结论（paid / closed / created|paying 无论 confirmed 与否、
 * regenerate 的 200 paid/unconfirmed）都证明会话拥有这笔订单——一律先解除
 * 会话变体再走常规收口；失败/429/网络异常证明不了什么，变体保持粘滞。
 *
 * 脚手架来源：tests/js/support-pay-modal-rereview-20261023.test.ts 的
 * harness() 逐字拷贝（含 r2 的 intervals 返回）。逐字保留之外的脚手架改动
 * （均为纯记录能力，不改变被测代码可见的原有行为）：
 *   1. FormData 桩改为记录 set() 调用（fields 数组；原桩 no-op）——断言
 *      regenerate 请求携带的 idempotency_key；
 *   2. fetch 桩改为 (url, opts) 双参并额外捕获 promise 的 reject（原桩只存
 *      url 与 resolve）——断言请求方法/头/体，并配合新增辅助模拟网络异常；
 *   3. 新增辅助：rejectRequest(h, index)（让第 index 个请求以网络异常收场）、
 *      trackSubmits(h)（重写 form.submit 记数，断言绝不回退整页提交）；
 *   4. fire()/expiredOrder() 自 rereview 原文保留；dictEntries() 自
 *      support-pay-modal.test.ts 逐字拷贝（模板/i18n 用例切词典用）。
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

// ---- 脚手架（rereview harness() 原文 + 文件头改动清单第 1/2 项）----
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
    catWorld: null, Date, FormData: class {constructor() {this.fields = [];} set(k, v) {this.fields.push([k, v]);}},
    fetch(url, opts) {return new Promise((resolve, reject) => requests.push({url, opts, resolve, reject}));},
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

/** 脚手架保留（rereview 原文）：按任意延迟触发一个已排程 setTimeout。 */
function fire(h: ReturnType<typeof harness>, delay: number): void {
  const pair = [...h.timeouts].find(([, v]) => v.delay === delay);
  assert.ok(pair, "应有 delay=" + delay + "ms 的已排程定时器");
  h.timeouts.delete(pair[0]);
  pair[1].f();
}

/** 脚手架保留（rereview 原文）：过去某一时刻到期的订单（首帧即 0:00）。 */
function expiredOrder(h: ReturnType<typeof harness>, ref: string) {
  const ord = h.order(ref);
  ord.expires_at = new Date(Date.now() - 1000).toISOString();
  return ord;
}

/** 扩展辅助（文件头改动清单第 3 项）：让第 index 个请求以网络异常收场。 */
function rejectRequest(h: ReturnType<typeof harness>, index: number): void {
  const req = h.requests[index];
  assert.ok(req && req.reject, "该请求应可 reject（网络异常路径）");
  req.reject(new TypeError("fetch failed"));
}

/** 扩展辅助（文件头改动清单第 3 项）：记录整页表单提交次数。 */
function trackSubmits(h: ReturnType<typeof harness>): () => number {
  let calls = 0;
  h.nodes.get("form").submit = () => { calls++; };
  return () => calls;
}

/** 脚手架拷贝（support-pay-modal.test.ts 原文）：切出 zh/en 词典条目。 */
function dictEntries(lang: "zh" | "en"): Map<string, string> {
	const i18nSrc = readFileSync(resolve(root, "static/i18n.js"), "utf8");
	const start = lang === "zh"
		? i18nSrc.indexOf("zh: {") : i18nSrc.indexOf("en: {");
	const end = lang === "zh"
		? i18nSrc.indexOf("en: {") : i18nSrc.indexOf("\n  };", start);
	const block = i18nSrc.slice(start, end);
	const out = new Map<string, string>();
	const re = /"([^"\\]+)":\s*"((?:[^"\\]|\\.)*)"/g;
	let m: RegExpExecArray | null;
	while ((m = re.exec(block)) !== null) {
		if (!out.has(m[1])) out.set(m[1], m[2]);
	}
	return out;
}

describe("支付弹层 r4：neutral 会话变体（2026-10-23）", () => {
  it("到期后 neutral → 立即会话变体（订单号 + mailto 联系入口），QR/过期都隐藏，6s 退避继续轮询", async () => {
    const h = harness();
    h.submit(); h.respond(0, expiredOrder(h, "orderA"), 201); await tick();
    h.nodes.get("pay-modal-contact").dataset.email = "author@example.com";
    h.respond(1, { status: "neutral" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-qr").hidden, true);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.nodes.get("pay-modal-print").hidden, true);
    // 会话变体：常规正文隐藏，会话正文/订单号/联系入口露出
    assert.equal(h.nodes.get("pay-modal-unconfirmed-body").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-ref-row").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-ref").textContent, "orderA");
    assert.equal(h.nodes.get("pay-modal-unconfirmed-contact").hidden, false);
    // mailto 用模板注入的作者邮箱，主题 = i18n 兜底文案 + 订单号
    assert.equal(h.nodes.get("pay-modal-contact").href,
      "mailto:author@example.com?subject="
      + encodeURIComponent("赞助订单核对 orderA"));
    // 恰好一个退避重试（6s 起步），轮询不终止
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 6000);
  });

  it("到期前 neutral → 同样立即会话变体（不等 0:00），倒计时与退避轮询都继续", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.nodes.get("pay-modal-contact").dataset.email = "author@example.com";
    h.respond(1, { status: "neutral" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-qr").hidden, true);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-body").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-ref-row").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-contact").hidden, false);
    assert.equal(h.intervals.size, 1);           // 倒计时未到期照走
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 6000);
  });

  it("会话变体粘滞：neutral 后的 503/429 都不降级回常规正文", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "neutral" }); await tick();
    fire(h, 6000);
    h.respond(2, null, 503); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-body").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-ref-row").hidden, false);
    assert.equal([...h.timeouts.values()][0].delay, 12000);
    fire(h, 12000);
    h.respond(3, null, 429); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-body").hidden, true);
  });

  it("neutral → paid：凭条收口（旧单），绝不留在会话变体", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "neutral" }); await tick();
    fire(h, 6000);
    h.respond(2, { status: "paid", amount_yuan: "6.00", gift: "cat_food" });
    await tick();
    assert.equal(h.nodes.get("pay-modal-print").hidden, false);
    assert.equal(h.nodes.get("pay-print-ref").textContent, "orderA");
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.timeouts.size, 0);
    assert.equal(h.intervals.size, 0);
  });

  it("neutral → closed：过期收口（closed 才是过期结论）", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "neutral" }); await tick();
    fire(h, 6000);
    h.respond(2, { status: "closed" }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.nodes.get("pay-modal-qr").hidden, true);
    assert.equal(h.timeouts.size, 0);
    assert.equal(h.intervals.size, 0);
  });

  it("neutral → confirmed 未过期（到期前）：回二维码窗格，照排 3s 轮询", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "neutral" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    fire(h, 6000);
    h.respond(2, { status: "paying", confirmed: true, expired: false });
    await tick();
    assert.equal(h.nodes.get("pay-modal-qr").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 3000);
  });

  it("neutral → confirmed 未过期（已到期）：保持「暂时无法确认」窗格但换回常规正文", async () => {
    const h = harness();
    h.submit(); h.respond(0, expiredOrder(h, "orderA"), 201); await tick();
    h.respond(1, { status: "neutral" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, false);
    fire(h, 6000);
    h.respond(2, { status: "created", confirmed: true, expired: false });
    await tick();
    // 明确答案胜出：sessionLost 解除，窗格保留但变体换回常规
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-qr").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-body").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-ref-row").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-contact").hidden, true);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 3000);
  });

  it("新订单重置会话标记：A 会话变体后建新单 B，B 自己的失败按常规未知态呈现", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "neutral" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, false);
    // 关弹层后下新单 B（applyOrder 重置 sessionLost；B 到期即 0:00）
    h.events["pay-modal-close:click"]();
    h.submit(); h.respond(2, expiredOrder(h, "orderB"), 201); await tick();
    // B 的到期查询失败 → 「暂时无法确认」必须是常规变体（会话变体不跨单）
    h.respond(3, null, 503); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-body").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-ref-row").hidden, true);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
  });
});

describe("支付弹层 r4 复核：非 neutral 结论解除会话变体（2026-10-23）", () => {
  it("neutral → closed → 重新生成 200 unconfirmed → 常规变体（会话无恙，绝不出「会话可能已失效」）", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "neutral" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, false);
    fire(h, 6000);
    h.respond(2, { status: "closed" }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    h.events["pay-modal-regenerate:click"]();
    h.respond(3, { status: "unconfirmed" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-body").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-ref-row").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-contact").hidden, true);
  });

  it("到期前 neutral → {paying, confirmed:false} → 二维码窗格回来，按 12s 退避（未确认负载）", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "neutral" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, false);
    fire(h, 6000);
    h.respond(2, { status: "paying", confirmed: false }); await tick();
    // 非 neutral 即证明拥有：QR 立刻回来；confirmed:false 按失败翻倍退避
    assert.equal(h.nodes.get("pay-modal-qr").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 12000);
  });

  it("到期后 neutral → {paying, confirmed:false} → 「暂时无法确认」换回常规变体", async () => {
    const h = harness();
    h.submit(); h.respond(0, expiredOrder(h, "orderA"), 201); await tick();
    h.respond(1, { status: "neutral" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, false);
    fire(h, 6000);
    h.respond(2, { status: "paying", confirmed: false }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-body").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-ref-row").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-contact").hidden, true);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 12000);
  });

  it("会话变体在显示时 regenerate 200 unconfirmed → 解除标记出常规变体（处理器直驱；该窗格本无此按钮）", async () => {
    const h = harness();
    h.submit(); h.respond(0, h.order("orderA"), 201); await tick(); h.poll();
    h.respond(1, { status: "neutral" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, false);
    h.events["pay-modal-regenerate:click"]();
    h.respond(2, { status: "unconfirmed" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-body").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-ref-row").hidden, true);
  });
});

describe("支付弹层 r4：重新生成走服务端复核（2026-10-23）", () => {
  /** 前奏：下单 A → closed → 过期窗格，返回待断言的 harness。 */
  async function expiredPane(ref: string) {
    const h = harness();
    h.submit(); h.respond(0, h.order(ref), 201); await tick(); h.poll();
    h.respond(1, { status: "closed" }); await tick();
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    return h;
  }

  it("请求契约：POST /support/orders/<旧ref>/regenerate、幂等 key=nextKey、Accept JSON；在途时按钮禁用且过期窗格保持", async () => {
    const h = await expiredPane("orderA");
    h.events["pay-modal-regenerate:click"]();
    assert.equal(h.requests.length, 3);
    const req = h.requests[2];
    assert.equal(req.url, "/support/orders/orderA/regenerate");
    assert.equal(req.opts.method, "POST");
    assert.deepEqual(req.opts.body.fields,
      [["idempotency_key", "key-orderA"]]);
    assert.equal(req.opts.headers.Accept, "application/json");
    assert.equal(req.opts.credentials, "same-origin");
    assert.equal(req.opts.cache, "no-store");
    assert.equal(h.nodes.get("pay-modal-regenerate").disabled, true);
    // 不预切空二维码窗格：过期窗格原地保持
    assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
    assert.equal(h.nodes.get("pay-modal-qr").hidden, true);
  });

  it("201 新单 → 全新二维码；后续轮询指向新单", async () => {
    const h = await expiredPane("orderA");
    h.events["pay-modal-regenerate:click"]();
    const orderB = h.order("orderB");
    orderB.amount_yuan = "18.00";
    h.respond(2, orderB, 201); await tick();
    assert.equal(h.nodes.get("pay-modal-qr").hidden, false);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.nodes.get("pay-modal-amount").textContent, "18.00");
    assert.equal(h.nodes.get("pay-modal-regenerate").disabled, false);
    h.poll();
    assert.equal(h.requests[3].url, "/support/orders/orderB/status");
  });

  it("200 {status:'paid'} → 旧单凭条（ref/金额都是旧单），定时器全停", async () => {
    const h = await expiredPane("orderA");
    h.events["pay-modal-regenerate:click"]();
    h.respond(2, { status: "paid", amount_yuan: "18.00", tier: "cat_toy",
      gift: "cat_toy" });
    await tick();
    assert.equal(h.nodes.get("pay-modal-print").hidden, false);
    assert.equal(h.nodes.get("pay-print-ref").textContent, "orderA");
    assert.equal(h.nodes.get("pay-print-amount").textContent, "18.00");
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    assert.equal(h.timeouts.size, 0);
    assert.equal(h.intervals.size, 0);
  });

  it("200 {status:'unconfirmed'} → 立即「暂时无法确认」（常规变体）+ 退避轮询旧单", async () => {
    const h = await expiredPane("orderA");
    h.events["pay-modal-regenerate:click"]();
    h.respond(2, { status: "unconfirmed" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-body").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-ref-row").hidden, true);
    assert.equal(h.nodes.get("pay-modal-regenerate").disabled, false);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 6000);
    fire(h, 6000);
    assert.equal(h.requests[3].url, "/support/orders/orderA/status");
  });

  it("200 {status:'neutral'} → 会话变体 + 退避轮询旧单", async () => {
    const h = await expiredPane("orderA");
    h.nodes.get("pay-modal-contact").dataset.email = "author@example.com";
    h.events["pay-modal-regenerate:click"]();
    h.respond(2, { status: "neutral" }); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-body").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-session-body").hidden, false);
    assert.equal(h.nodes.get("pay-modal-unconfirmed-ref").textContent, "orderA");
    assert.equal(h.nodes.get("pay-modal-contact").href,
      "mailto:author@example.com?subject="
      + encodeURIComponent("赞助订单核对 orderA"));
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 6000);
    fire(h, 6000);
    assert.equal(h.requests[3].url, "/support/orders/orderA/status");
  });

  it("409/429/500 → 「暂时无法确认」+ 退避轮询旧单；绝不回退整页表单提交", async () => {
    for (const status of [409, 429, 500]) {
      const h = await expiredPane("orderA");
      const submits = trackSubmits(h);
      h.events["pay-modal-regenerate:click"]();
      h.respond(2, null, status); await tick();
      assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false, "status=" + status);
      assert.equal(h.timeouts.size, 1, "status=" + status);
      assert.equal([...h.timeouts.values()][0].delay, 6000, "status=" + status);
      fire(h, 6000);
      assert.equal(h.requests[3].url, "/support/orders/orderA/status", "status=" + status);
      assert.equal(submits(), 0, "status=" + status);
    }
  });

  it("fetch 网络异常 → 同一安全结果（「暂时无法确认」+ 轮询，不整页提交）", async () => {
    const h = await expiredPane("orderA");
    const submits = trackSubmits(h);
    h.events["pay-modal-regenerate:click"]();
    rejectRequest(h, 2); await tick();
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, false);
    assert.equal(h.timeouts.size, 1);
    assert.equal([...h.timeouts.values()][0].delay, 6000);
    fire(h, 6000);
    assert.equal(h.requests[3].url, "/support/orders/orderA/status");
    assert.equal(submits(), 0);
  });

  it("双击「重新生成」→ 只发一次请求；响应后按钮恢复可用", async () => {
    const h = await expiredPane("orderA");
    h.events["pay-modal-regenerate:click"]();
    h.events["pay-modal-regenerate:click"]();
    assert.equal(h.requests.length, 3);
    assert.equal(h.nodes.get("pay-modal-regenerate").disabled, true);
    h.respond(2, h.order("orderB"), 201); await tick();
    assert.equal(h.nodes.get("pay-modal-regenerate").disabled, false);
    assert.equal(h.nodes.get("pay-modal-qr").hidden, false);
  });

  it("在途时关弹层 → 迟到的 regenerate 响应整体丢弃：不重开、不应用、不排程", async () => {
    const h = await expiredPane("orderA");
    h.events["pay-modal-regenerate:click"]();
    h.events["pay-modal-close:click"]();
    const orderB = h.order("orderB");
    orderB.amount_yuan = "18.00";
    h.respond(2, orderB, 201); await tick();
    assert.equal(h.nodes.get("pay-modal").hidden, true);
    assert.equal(h.nodes.get("pay-modal-amount").textContent, "6.00");
    assert.equal(h.timeouts.size, 0);
    assert.equal(h.intervals.size, 0);
  });
});

describe("支付弹层 r4：模板/i18n（2026-10-23）", () => {
  const html = readFileSync(resolve(root, "templates/support.html"), "utf8");
  // 既有口径：窗格切到第一个 </div>（窗格内只允许 <p> 系元素）
  const pane = html.slice(html.indexOf('id="pay-modal-unconfirmed"'),
    html.indexOf("</div>", html.indexOf('id="pay-modal-unconfirmed"')));

  it("unconfirmed 窗格含会话变体元素且默认 hidden，无重新生成按钮", () => {
    // 常规正文仍在最前（只新增 id）
    expect(pane).toContain('id="pay-modal-unconfirmed-body" data-i18n="support.modal.unconfirmed.body"');
    // 会话变体三件套：会话正文、订单号行、联系行（全部默认 hidden）
    expect(pane).toContain('id="pay-modal-unconfirmed-session-body"');
    expect(pane).toContain('data-i18n="support.modal.unconfirmed.session.body" hidden');
    expect(pane).toContain('id="pay-modal-unconfirmed-ref-row" hidden');
    expect(pane).toContain('data-i18n="support.modal.unconfirmed.ref"');
    expect(pane).toContain('id="pay-modal-unconfirmed-ref"');
    expect(pane).toContain('id="pay-modal-unconfirmed-contact" hidden');
    expect(pane).toContain('id="pay-modal-contact"');
    expect(pane).toContain('data-i18n="support.modal.unconfirmed.contact"');
    // 联系入口用模板注入的作者邮箱（href + data-email 双写）
    expect(pane).toContain('href="mailto:{{ author_email }}"');
    expect(pane).toContain('data-email="{{ author_email }}"');
    // 会话态不给「重新生成」（旧单归属不明，重建必须先经服务端复核）
    expect(pane).not.toContain("pay-modal-regenerate");
  });

  it("四个新 i18n 键 zh/en 各一条且成对；mailto 主题键就位", () => {
    const keys = ["support.modal.unconfirmed.session.body",
                  "support.modal.unconfirmed.ref",
                  "support.modal.unconfirmed.contact",
                  "support.modal.unconfirmed.mail.subject"];
    const i18n = readFileSync(resolve(root, "static/i18n.js"), "utf8");
    const zh = dictEntries("zh");
    const en = dictEntries("en");
    for (const key of keys) {
      expect(i18n.split(`"${key}"`).length - 1).toBe(2); // zh + en 各一条
      expect(zh.has(key), "zh 缺键：" + key).toBe(true);
      expect(en.has(key), "en 缺键：" + key).toBe(true);
    }
    expect(zh.get("support.modal.unconfirmed.ref")).toBe("订单号");
    expect(zh.get("support.modal.unconfirmed.contact")).toBe("联系作者核对");
    expect(zh.get("support.modal.unconfirmed.mail.subject")).toBe("赞助订单核对");
    expect(en.get("support.modal.unconfirmed.ref")).toBe("Order number");
    expect(en.get("support.modal.unconfirmed.contact")).toBe("Contact the author");
    expect(en.get("support.modal.unconfirmed.mail.subject")).toBe("Support order check");
  });
});
