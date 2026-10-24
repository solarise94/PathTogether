/**
 * 支付弹层 r6 回归（2026-10-24 评审边界）：命名 paid 负载 → 凭条切换到
 * 命名订单。
 *
 * 评审边界：陈旧标签页展示已过期的 A；另一标签页里 A 已被换单为 B 且 B
 * 已支付。陈旧标签页点「重新生成」绝不能再建新单——服务端此时应答命名
 * paid 负载：200 {status:'paid', out_trade_no:<B>, amount_yuan:'6.66',
 * tier:null, gift:'fish_treat'}。服务端契约（权威）：paid 负载只在请求
 * key 未绑定时返回（state.nextKey 仍未绑定、可复用）；out_trade_no 当且
 * 仅当已支付的不是 URL 里的旧单才携带；旧单自身已支付的 paid 负载保持
 * 未命名；订单负载绝不带 status。
 * 本轮行为：regenerate 响应先按 status==='paid' 判定（先于订单负载分支，
 * 否则命名负载会被当成新建二维码单应用）；paid 且 out_trade_no 是 26 位
 * 小写字母数字且 ≠ 当前展示订单 → 先把展示订单切换过去（换代作废旧单
 * 在途响应；ref / amount_yuan 跟随；回退链接指向 /support/pay/<ref>）再
 * 出示凭条——凭条号、「查看完整凭条」链接、关弹层交付礼物的 ref 全部用
 * 命名订单，且不为新旧任一单排程轮询；未命名 / 同名 / 畸形 out_trade_no
 * → 维持 r4 行为（当前展示订单的凭条）。paid 结论一律不碰隐藏 key 输入
 * 与 nextKey——凭条之后换额提交仍按 r5 规则先写隐藏输入再改用该未绑定
 * nextKey；同额提交沿用隐藏输入原 key。
 *
 * 脚手架来源：tests/js/support-pay-modal-r5-20261023.test.ts 的 harness()
 * 逐字拷贝（含 r5 对 r4 的全部记录性扩展，见该文件头改动清单第 1–5 项）。
 * 逐字保留之外的脚手架改动（均为纯记录能力或字段桩，不改变被测代码在
 * 旧桩下的任何可见行为）：
 *   1. ctx.catWorld 由 null 换成记录桩 deliver(ref, gift) → deliveries
 *      数组——旧桩下 deliverToCat 因 catWorld 为 null 直接返回；记录桩只
 *      多记录不分支（cat-card 桩无 scrollIntoView，滚动照旧被守卫跳过）；
 *   2. 凭条「查看完整凭条」链接（pay-modal-fullreceipt）与回退链接
 *      （pay-modal-fallback）的 href 断言直接复用 r5 桩的 setAttribute →
 *      attrs 记录，无新桩；
 *   3. frameSrc() 自 r5 逐字拷贝（订单负载 sanity 用例断言 iframe src）。
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

// 服务端渲染的初始隐藏 key（每页渲染随机的 32-hex）
const INIT_KEY = "a".repeat(32);

// 26 位小写字母数字订单号（服务端口径）；命名 paid 负载里的新单 B
const B_REF = "b1a2c3d4e5f6a7b8c9d0e1f2a3";

// 命名 paid 负载（评审边界的服务端应答形态；tier 可为 null）
function namedPaid(ref: string, amount = "6.66") {
  return { status: "paid", out_trade_no: ref, amount_yuan: amount,
    tier: null, gift: "fish_treat" };
}

// ---- 脚手架（r5 harness() 原文 + 文件头改动清单第 1 项）----
function harness() {
  let seq = 0;
  const timeouts = new Map();
  const intervals = new Map();
  const events = {};
  function el(id) {
    return { id, hidden: id === 'pay-modal', textContent: '', dataset: {},
      attrs: {} as Record<string, string>, children: [] as any[],
      get innerHTML() { return ''; },
      set innerHTML(_) { this.children.length = 0; },
      addEventListener(k, f) { events[id + ':' + k] = f; },
      setAttribute(k, v) { this.attrs[k] = v; },
      removeAttribute() {}, focus() {},
      appendChild(child) { this.children.push(child); },
      querySelector() { return null; }, querySelectorAll() { return []; },
      getBoundingClientRect() { return {}; },
      classList: {add() {}, remove() {}},
    };
  }
  const nodes = new Map();
  const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id, el(id)); return nodes.get(id); },
    addEventListener() {}, removeEventListener() {}, activeElement: null,
    createElement() {return el('iframe');} };
  // 字段桩（r5 改动清单第 1 项）：idempotency_key / amount_custom / 单选 / 隐藏 amount
  const keyInput = { value: INIT_KEY };
  const customInput = { value: '' };
  const fixedAmount = { value: '' };
  let checkedRadio: { value: string } | null = null;
  const requests = [];
  const submits: { key: string }[] = [];
  const deliveries: { ref: string; gift: string }[] = [];   // r6 改动清单第 1 项
  const ctx = {document, t: () => '', prefersReducedMotion: () => true, localDateIso: () => '2026-10-23',
    POLL_INTERVAL_MS: 3000, POLL_BACKOFF_MS: 6000, DESKTOP_QUERY: 'desktop', TOUCH_QUERY: 'touch',
    catWorld: { deliver(ref: string, gift: string) { deliveries.push({ ref, gift }); } },
    Date,
    // FormData 桩（r5 改动清单第 2 项）：镜像真实浏览器对隐藏输入的序列化
    FormData: class {
      fields: [string, string][];
      constructor() { this.fields = [['idempotency_key', keyInput.value]]; }
      set(k: string, v: string) {
        this.fields = this.fields.filter((pair) => pair[0] !== k);
        this.fields.push([k, v]);
      }
    },
    fetch(url, opts) {return new Promise((resolve, reject) => requests.push({url, opts, resolve, reject}));},
    window: {matchMedia(q) {return {matches: q === 'desktop'};},
      setTimeout(f, delay) {const id=++seq; timeouts.set(id,{f,delay}); return id;},
      clearTimeout(id) {timeouts.delete(id);},
      setInterval(f, delay) {const id=++seq; intervals.set(id,{f,delay}); return id;},
      clearInterval(id) {intervals.delete(id);}},
  };
  vm.createContext(ctx);
  vm.runInContext(fn + '\nthis.start = initDesktopQrModal;', ctx);
  const form = document.getElementById('form'); form.action = '/support/orders';
  form.querySelector = (sel) => {
    if (sel === 'input[name="idempotency_key"]') return keyInput;
    if (sel === 'input[name="amount_custom"]') return customInput;
    if (sel === 'input[name="amount"]:checked') return checkedRadio;
    if (sel === 'input[name="amount"]') return fixedAmount;
    return null;
  };
  form.submit = () => { submits.push({ key: keyInput.value }); };
  ctx.start(form, document.getElementById('pay-modal'));
  function submit() {events['form:submit']({preventDefault(){}});}
  function respond(index, payload, status=200) {requests[index].resolve({status, ok: status>=200 && status<300, json:()=>Promise.resolve(payload)});}
  function poll() {const pair=[...timeouts].find(([,v])=>v.delay===3000); assert.ok(pair); timeouts.delete(pair[0]); pair[1].f();}
  function order(ref) {return {out_trade_no: ref, amount_yuan:'6.00', pay_url:'/support/pay/'+ref,
    expires_at: new Date(Date.now()+900000).toISOString(), next_idempotency_key:'key-'+ref};}
  return {nodes, timeouts, intervals, requests, events, submits, deliveries,
    keyInput, customInput, fixedAmount,
    setCheckedRadio(value: string) {
      checkedRadio = { value, closest: () => null };
    },
    submit, respond, poll, order};
}

/** 脚手架拷贝（r5 原文）：最新挂载 iframe 的 src。 */
function frameSrc(h: ReturnType<typeof harness>): string {
  const host = h.nodes.get("pay-modal-frame-host");
  assert.ok(host && host.children.length, "iframe 应已挂载");
  const frame = host.children[host.children.length - 1];
  return frame.src || frame.attrs.src;
}

/** 前奏（同 r5 各用例前奏）：建单 A → 轮询 closed → 过期窗格。 */
async function expiredPane() {
  const h = harness();
  h.setCheckedRadio("6.00");
  h.submit();
  h.respond(0, h.order("orderA"), 201); await tick();
  h.poll();
  h.respond(1, { status: "closed" }); await tick();
  assert.equal(h.nodes.get("pay-modal-expired").hidden, false);
  return h;
}

/** 断言凭条展示的是当前展示订单 A（未换单；r4 口径 + 链接/定时器簿记）。 */
function assertReceiptForA(h: ReturnType<typeof harness>, amount: string,
    label = ""): void {
  const at = label ? " (" + label + ")" : "";
  assert.equal(h.nodes.get("pay-modal-print").hidden, false, at);
  assert.equal(h.nodes.get("pay-modal-qr").hidden, true, at);
  assert.equal(h.nodes.get("pay-print-ref").textContent, "orderA", at);
  assert.equal(h.nodes.get("pay-print-amount").textContent, amount, at);
  assert.equal(h.nodes.get("pay-modal-fullreceipt").attrs.href,
    "/support/return?out_trade_no=orderA", at);
  assert.equal(h.nodes.get("pay-modal-fallback").attrs.href,
    "/support/pay/orderA", at);                  // 回退链接仍指向 A
  assert.equal(h.timeouts.size, 0, at);          // 凭条后不排轮询/倒计时
  assert.equal(h.intervals.size, 0, at);
  assert.equal(h.keyInput.value, INIT_KEY, at);  // paid 不碰隐藏 key
}

describe("支付弹层 r6：命名 paid 负载 → 凭条切换到命名订单（2026-10-24）", () => {
  /** 前奏：A 过期 → 重建遇命名 paid 负载（B 已支付）→ 凭条 → 关弹层交付。 */
  async function paidReceiptOnB() {
    const h = await expiredPane();
    h.events["pay-modal-regenerate:click"]();
    h.respond(2, namedPaid(B_REF)); await tick();
    h.events["pay-modal-close:click"]();
    return h;
  }

  it("陈旧标签页：A 过期重建 → 命名 paid 负载（他处 B 已支付）→ 凭条切到 B，不建新单", async () => {
    const h = await expiredPane();
    h.events["pay-modal-regenerate:click"]();
    // 重建请求携带未绑定的 nextKey；响应后不再发出任何建单请求
    assert.deepEqual(h.requests[2].opts.body.fields,
      [["idempotency_key", "key-orderA"]]);
    assert.equal(h.requests[2].url, "/support/orders/orderA/regenerate");
    h.respond(2, namedPaid(B_REF)); await tick();
    assert.equal(h.requests.length, 3);
    assert.equal(h.nodes.get("pay-modal-print").hidden, false);
    assert.equal(h.nodes.get("pay-modal-qr").hidden, true);
    assert.equal(h.nodes.get("pay-modal-expired").hidden, true);
    assert.equal(h.nodes.get("pay-modal-unconfirmed").hidden, true);
    // 凭条号与完整凭条链接都用命名订单 B
    assert.equal(h.nodes.get("pay-print-ref").textContent, B_REF);
    assert.equal(h.nodes.get("pay-print-amount").textContent, "6.66");
    assert.equal(h.nodes.get("pay-modal-fullreceipt").attrs.href,
      "/support/return?out_trade_no=" + B_REF);
    // 回退链接已切到 B 的收银台（同 applyOrder 口径）
    assert.equal(h.nodes.get("pay-modal-fallback").attrs.href,
      "/support/pay/" + B_REF);
    // 不为任一单排程轮询/倒计时
    assert.equal(h.timeouts.size, 0);
    assert.equal(h.intervals.size, 0);
    // paid 不碰隐藏 key（nextKey 未绑定语义由下一用例承接）
    assert.equal(h.keyInput.value, INIT_KEY);
    // 关弹层交付礼物：ref 用命名订单 B
    h.events["pay-modal-close:click"]();
    assert.deepEqual(h.deliveries, [{ ref: B_REF, gift: "fish_treat" }]);
  });

  it("凭条（B）之后：同额提交沿用隐藏输入原 key；换额提交先写重建用的 nextKey 再发请求", async () => {
    const h = await paidReceiptOnB();
    // 同额（6.66 = B 的金额）：金额未变 → 表单隐藏输入的原 key 原样发送
    h.customInput.value = "6.66";
    h.submit();
    assert.deepEqual(h.requests[3].opts.body.fields,
      [["idempotency_key", INIT_KEY]]);
    assert.equal(h.keyInput.value, INIT_KEY);
    h.respond(3, null, 500); await tick();       // 收掉在途：非 2xx 整页回退
    assert.equal(h.submits.length, 1);
    assert.equal(h.submits[0].key, INIT_KEY);
    // 换额（18.00 ≠ 6.66）：先写隐藏输入再建 FormData，请求体与输入均为
    // 重建请求用过的那个未绑定 nextKey（state.nextKey 从未被 paid 改动）
    h.customInput.value = "";
    h.setCheckedRadio("18.00");
    h.submit();
    assert.equal(h.requests[4].url, "/support/orders");
    assert.deepEqual(h.requests[4].opts.body.fields,
      [["idempotency_key", "key-orderA"]]);
    assert.equal(h.keyInput.value, "key-orderA");
  });
});

describe("支付弹层 r6：未命名/同名/畸形 out_trade_no 的 paid 负载 → 旧单凭条（2026-10-24）", () => {
  it("未命名 paid 负载（无 out_trade_no，r4 行为）→ 出示 A 的凭条", async () => {
    const h = await expiredPane();
    h.events["pay-modal-regenerate:click"]();
    h.respond(2, { status: "paid", amount_yuan: "18.00", tier: "cat_toy",
      gift: "cat_toy" });
    await tick();
    assertReceiptForA(h, "18.00");
    assert.deepEqual(h.deliveries, []);            // 未关弹层：不交付
  });

  it("命名 paid 负载 out_trade_no 等于当前展示订单 A → 同样出示 A 的凭条", async () => {
    const h = await expiredPane();
    h.events["pay-modal-regenerate:click"]();
    h.respond(2, namedPaid("orderA", "6.00")); await tick();
    assertReceiptForA(h, "6.00");
  });

  it("畸形 out_trade_no（../x / 123 / 大写 26 位）→ 不换单，出示 A 的凭条", async () => {
    for (const bad of ["../x", 123, "B1A2C3D4E5F6A7B8C9D0E1F2A3"]) {
      const h = await expiredPane();
      h.events["pay-modal-regenerate:click"]();
      h.respond(2, namedPaid(bad as string, "6.66")); await tick();
      assertReceiptForA(h, "6.66", "bad=" + String(bad));
    }
  });
});

describe("支付弹层 r6：既有口径不动（2026-10-24）", () => {
  it("订单负载（无 status，关旧建新）→ 仍按 r5 应用为全新二维码单", async () => {
    const h = await expiredPane();
    h.events["pay-modal-regenerate:click"]();
    h.respond(2, h.order("orderB"), 201); await tick();
    assert.equal(h.nodes.get("pay-modal-qr").hidden, false);
    assert.equal(h.nodes.get("pay-modal-print").hidden, true);
    assert.equal(frameSrc(h), "/support/pay/orderB/qr");
    assert.equal(h.keyInput.value, "key-orderA");  // r5：key 跟单
    assert.ok(h.timeouts.size >= 1);               // 新单继续轮询
  });

  it("在途重建遇关弹层：迟到命名 paid 负载整体丢弃（不换窗格、不交付、不换单）", async () => {
    const h = await expiredPane();
    h.events["pay-modal-regenerate:click"]();
    h.events["pay-modal-close:click"]();
    h.respond(2, namedPaid(B_REF)); await tick();
    assert.equal(h.nodes.get("pay-modal").hidden, true);
    assert.equal(h.nodes.get("pay-modal-print").hidden, true);
    assert.deepEqual(h.deliveries, []);
    assert.equal(h.timeouts.size, 0);
    assert.equal(h.intervals.size, 0);
    assert.equal(h.keyInput.value, INIT_KEY);
    // ref/金额未切到 B：按 A 的 6.00 同额提交 → 仍走表单原 key（无换额改写）
    h.customInput.value = "6.00";
    h.submit();
    assert.deepEqual(h.requests[3].opts.body.fields,
      [["idempotency_key", INIT_KEY]]);
  });
});
